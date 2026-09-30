"""
F.R.I.D.A.Y. — ui/ws_server.py
WebSocket server bridging Electron UI ↔ Python backend.
"""

import asyncio
import base64
import json
import logging
import platform
import re
import secrets
import subprocess
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger("friday.ws")

# Files dropped/attached from the UI land here — a project-root sibling of
# vault/ (FRIDAY's own memory notes), kept separate since these are the
# user's own files, not something FRIDAY wrote. Never git-committed (see
# .gitignore) since it can hold arbitrary personal documents/images.
UPLOADS_DIR = Path(__file__).resolve().parent.parent / "user_uploads"
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB raw file — generous for docs/images,
                                       # not for video; base64 in transit runs ~33% bigger
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_clients: set = set()
_server = None
_last_stats: dict = {}
_current_state: str = "idle"

# Real hardware labels — detected once at startup, sent to UI on connect.
# (Used to replace hardcoded model strings in the renderer.)
_hw_info: dict = {"cpu_name": "CPU", "gpu_name": "GPU"}

# Event loop the server runs in — captured in start_server() so worker
# threads (VAD mic capture, TTS playback) can push events via
# broadcast_from_thread() without needing to be coroutines themselves.
_main_loop: asyncio.AbstractEventLoop | None = None

# Typed messages from the UI's text box land here. _voice_loop races this
# queue against the mic trigger / wake word so typing works as an
# alternative to speaking, not a separate silent mode.
_text_input_queue: "asyncio.Queue[str] | None" = None

# Mic trigger
_mic_event = threading.Event()

# Stop flag — threading.Event for thread-safe access
_stop_event = threading.Event()

# Confirmation system
_pending_confirmations: dict = {}

# pynvml handle — initialized once, kept open
_nvml_handle = None
_nvml_ready = False


def _init_nvml():
    """Initialize pynvml once at startup."""
    global _nvml_handle, _nvml_ready
    try:
        import nvidia_smi
        nvidia_smi.nvmlInit()
        _nvml_handle = nvidia_smi.nvmlDeviceGetHandleByIndex(0)
        _nvml_ready = True
        logger.info("NVML initialized (nvidia-ml-py)")
        return "nvidia_smi"
    except Exception:
        pass
    try:
        import pynvml
        pynvml.nvmlInit()
        _nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        _nvml_ready = True
        logger.info("NVML initialized (pynvml)")
        return "pynvml"
    except Exception:
        logger.warning("NVML unavailable — GPU stats disabled")
        return None

_nvml_lib = _init_nvml()

# Fallback when neither NVML python package works — shells out to the
# nvidia-smi CLI directly, which only needs the driver installed, not any
# extra Python package. None = not yet tried, True/False after first try
# so we don't retry a subprocess call every 2s once we know it's absent.
_smi_cli_available = None


def _nvidia_smi_cli_query() -> dict | None:
    global _smi_cli_available
    if _smi_cli_available is False:
        return None
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        )
        line = out.strip().splitlines()[0]
        name, util, mem_used, mem_total, temp = [p.strip() for p in line.split(",")]
        _smi_cli_available = True
        return {
            "name": name,
            "gpu": float(util),
            "vram_used": round(float(mem_used) / 1024, 1),    # MiB → GiB
            "vram_total": round(float(mem_total) / 1024, 1),
            "gpu_temp": float(temp),
        }
    except Exception:
        _smi_cli_available = False
        return None


def _detect_hw_info() -> dict:
    """Best-effort real CPU/GPU model detection. Never raises — falls back
    to generic labels so the UI never shows a hardcoded machine's specs."""
    info = {"cpu_name": "CPU", "gpu_name": "GPU"}

    # CPU name
    try:
        name = None
        if platform.system() == "Windows":
            try:
                out = subprocess.check_output(
                    ["wmic", "cpu", "get", "name"], text=True, timeout=3,
                    stderr=subprocess.DEVNULL,
                )
                lines = [l.strip() for l in out.splitlines() if l.strip() and "Name" not in l]
                if lines:
                    name = lines[0]
            except Exception:
                name = None
            if not name:
                # wmic is deprecated/removed on newer Windows builds (11
                # 24H2+, Server 2025) — this is why the raw
                # platform.processor() string ("Intel64 Family 6 Model...")
                # was showing up instead of a real name. PowerShell's CIM
                # cmdlets replace wmic and are present everywhere wmic was.
                try:
                    out = subprocess.check_output(
                        ["powershell", "-NoProfile", "-Command",
                         "(Get-CimInstance Win32_Processor).Name"],
                        text=True, timeout=5, stderr=subprocess.DEVNULL,
                    )
                    stripped = out.strip()
                    if stripped:
                        name = stripped
                except Exception:
                    name = None
        if not name:
            try:
                import cpuinfo  # py-cpuinfo, optional
                name = cpuinfo.get_cpu_info().get("brand_raw")
            except Exception:
                name = None
        if not name:
            name = platform.processor() or None
        if name:
            info["cpu_name"] = name
    except Exception:
        pass

    # GPU name via already-initialized NVML handle
    try:
        if _nvml_ready and _nvml_handle:
            if _nvml_lib == "nvidia_smi":
                import nvidia_smi as nvml
            else:
                import pynvml as nvml
            raw = nvml.nvmlDeviceGetName(_nvml_handle)
            info["gpu_name"] = raw.decode() if isinstance(raw, bytes) else raw
        else:
            smi = _nvidia_smi_cli_query()
            if smi:
                info["gpu_name"] = smi["name"]
    except Exception:
        pass

    return info


def client_count() -> int:
    return len(_clients)


# The live TTS instance, registered once at startup (see start.py run()).
# Lets WS handlers (interrupt/stop_listen) kill playback immediately
# instead of only setting a flag the generation loop might not check
# again for a while (e.g. while blocked on a slow network call).
_tts_instance = None


def register_tts(tts):
    global _tts_instance
    _tts_instance = tts


def get_mic_event() -> threading.Event:
    return _mic_event


def get_text_input_queue() -> asyncio.Queue:
    global _text_input_queue
    if _text_input_queue is None:
        _text_input_queue = asyncio.Queue()
    return _text_input_queue


def get_last_stats() -> dict:
    """Most recent stats snapshot (cpu/ram/gpu/gpu_temp/vram_*) — same
    dict broadcast to the UI every 2s. Empty until the first stats tick."""
    return dict(_last_stats)


def get_stop_flag() -> bool:
    return _stop_event.is_set()


def clear_stop_flag():
    _stop_event.clear()


AUTH_TIMEOUT_SECONDS = 5


def _tokens_match(provided, expected: str) -> bool:
    if not isinstance(provided, str) or not expected:
        return False
    return secrets.compare_digest(provided, expected)


async def _handler(websocket):
    global _clients, _stop_flag

    from config import config
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT_SECONDS)
        auth = json.loads(raw)
    except Exception:
        auth = None

    if not isinstance(auth, dict) or auth.get("cmd") != "auth" or \
            not _tokens_match(auth.get("token"), config.ws_auth_token):
        logger.warning(
            f"Rejected WS connection from {getattr(websocket, 'remote_address', '?')} — "
            f"missing or invalid auth token."
        )
        try:
            await websocket.close(code=4001, reason="unauthorized")
        except Exception:
            pass
        return

    _clients.add(websocket)
    logger.info(f"UI connected ({len(_clients)} clients)")

    await _safe_send(websocket, {"event": "ready"})
    await _safe_send(websocket, {"event": "hwinfo", **_hw_info})
    await _safe_send(websocket, {"event": "state", "value": _current_state})
    if _last_stats:
        await _safe_send(websocket, {**_last_stats, "event": "stats"})

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                cmd = data.get("cmd")

                if cmd == "start_listen":
                    logger.info("UI: mic triggered")
                    _mic_event.set()

                elif cmd == "stop_listen":
                    _mic_event.clear()
                    # Also interrupt any ongoing speech
                    _stop_event.set()
                    if _tts_instance is not None:
                        _tts_instance.stop_current()

                elif cmd == "interrupt":
                    _stop_event.set()
                    if _tts_instance is not None:
                        _tts_instance.stop_current()
                    logger.info("UI: speech interrupted")

                elif cmd == "text_input":
                    text = (data.get("text") or "").strip()
                    if text:
                        logger.info(f"UI: typed input — {text[:80]!r}")
                        low = text.lower()
                        if low in ("stop", "cancel", "shut up", "be quiet", "never mind"):
                            # Same as clicking interrupt — act now, don't
                            # wait for _voice_loop to reach its next
                            # iteration, by which point she'd already be
                            # done talking anyway.
                            _stop_event.set()
                            if _tts_instance is not None:
                                _tts_instance.stop_current()
                            logger.info("UI: speech interrupted (typed)")
                        else:
                            await get_text_input_queue().put(text)

                elif cmd == "upload_file":
                    filename  = data.get("filename") or "upload"
                    mime_type = data.get("mime_type") or "application/octet-stream"
                    b64       = data.get("data") or ""
                    caption   = (data.get("caption") or "").strip()
                    try:
                        raw = base64.b64decode(b64, validate=True)
                    except Exception:
                        await _safe_send(websocket, {"event": "upload_error", "filename": filename,
                                                       "message": "Couldn't decode that file — try again?"})
                    else:
                        if len(raw) > _MAX_UPLOAD_BYTES:
                            await _safe_send(websocket, {"event": "upload_error", "filename": filename,
                                                           "message": f"That's over the {_human_size(_MAX_UPLOAD_BYTES)} limit."})
                        elif not raw:
                            await _safe_send(websocket, {"event": "upload_error", "filename": filename,
                                                           "message": "That file came through empty."})
                        else:
                            try:
                                dest = _save_upload(filename, raw)
                            except Exception as e:
                                logger.error(f"[Upload] Couldn't save {filename!r}: {e}")
                                await _safe_send(websocket, {"event": "upload_error", "filename": filename,
                                                               "message": "Couldn't save that file on this end."})
                            else:
                                logger.info(f"UI: file uploaded — {dest.name} ({_human_size(len(raw))}, {mime_type})")
                                # Ack straight back to the uploading client so the
                                # input box can clear its "uploading…" state —
                                # this is NOT the transcript line; that comes from
                                # send_transcript("user", ...) once _handle_user_text
                                # actually dequeues and processes the message below,
                                # same as it does for typed text.
                                await _safe_send(websocket, {
                                    "event": "upload_saved", "filename": dest.name,
                                    "path": str(dest), "mime_type": mime_type, "size": len(raw),
                                })
                                synthesized = (
                                    f'{caption}\n\n📎 Attached "{filename}" ({_human_size(len(raw))}) '
                                    f"— saved at {dest}"
                                ) if caption else (
                                    f'📎 Attached "{filename}" ({_human_size(len(raw))}) — saved at {dest}. '
                                    f"Take a look and tell me what's in it."
                                )
                                await get_text_input_queue().put(synthesized)

                elif cmd in ("confirm_yes", "confirm_no"):
                    conf_id = data.get("id")
                    if conf_id and conf_id in _pending_confirmations:
                        _pending_confirmations[conf_id]["result"] = (cmd == "confirm_yes")
                        _pending_confirmations[conf_id]["event"].set()

                elif cmd == "get_settings":
                    payload = await _gather_settings_data()
                    await _safe_send(websocket, {"event": "settings_data", **payload})

                elif cmd == "get_watchlist":
                    payload = await _gather_watchlist_data()
                    await _safe_send(websocket, {"event": "watchlist_data", **payload})

                elif cmd == "get_memory":
                    payload = await _gather_memory_data()
                    await _safe_send(websocket, {"event": "memory_data", **payload})

                elif cmd == "get_graph":
                    payload = await _gather_graph_data()
                    await _safe_send(websocket, {"event": "graph_data", **payload})

                elif cmd == "get_focus_status":
                    from actions.focus_session import focus_state
                    await _safe_send(websocket, {"event": "focus_status", **focus_state()})

                elif cmd == "posture_state":
                    # Posture booleans from the renderer's own webcam
                    # analysis (ui/renderer/components/PostureWatch.jsx).
                    # Coerced to bool here as well as there — this
                    # command accepts nothing but booleans, so no frame,
                    # landmark array, or measurement can ride along even
                    # if a future caller tried to send one.
                    from actions.focus_session import update_posture
                    await update_posture(
                        present=bool(data.get("present", True)),
                        head_down=bool(data.get("head_down", False)),
                        slouched=bool(data.get("slouched", False)),
                        monitoring=bool(data.get("monitoring", True)),
                    )

                elif cmd == "screen_stuck":
                    # Ambient screen watch (ScreenWatch.jsx). Unlike
                    # posture_state, this one DOES carry a frame — one,
                    # only when the screen has genuinely been unchanged
                    # past the threshold. Used for a single vision call
                    # and not retained anywhere.
                    from actions.focus_session import report_screen_stuck
                    await report_screen_stuck(
                        image_b64=data.get("image_b64", "") or "",
                        image_format=(data.get("image_format") or "jpeg"),
                    )

                elif cmd == "screen_changed":
                    from actions.focus_session import clear_screen_stuck
                    await clear_screen_stuck()

                elif cmd == "focus_control":
                    # Control surface for the always-on-top card. The
                    # source tag matters for retarget — see the card
                    # trap in actions/focus_session.py.
                    from actions import focus_session as _fs
                    action = str(data.get("action", "")).strip().lower()
                    src = str(data.get("source", "") or "voice")
                    if action == "retarget":
                        result = await _fs.retarget(source=src)
                    else:
                        result = await _fs.focus_session({
                            "action": action,
                            "minutes": data.get("minutes"),
                            "label": data.get("label", ""),
                        })
                    if _tts_instance is not None and result:
                        _tts_instance.enqueue(result)
                    await broadcast({"event": "focus_status", **_fs.focus_state()})

                elif cmd == "set_llm_provider":
                    from brain.llm import set_active_provider
                    ok = set_active_provider(data.get("provider", ""))
                    payload = await _gather_settings_data()
                    await _safe_send(websocket, {"event": "settings_data", "set_ok": ok, **payload})

                elif cmd == "set_tts_provider":
                    from voice.tts import set_active_tts_provider
                    ok = set_active_tts_provider(data.get("provider", ""))
                    payload = await _gather_settings_data()
                    await _safe_send(websocket, {"event": "settings_data", "set_ok": ok, **payload})

                elif cmd == "set_audio_devices":
                    # Session-only, same convention as set_llm_provider/
                    # set_tts_provider above — no .env rewrite, reverts to
                    # whatever MIC_DEVICE_NAME/SPEAKER_DEVICE_NAME say on
                    # next launch. Empty string means "system default".
                    from voice import vad as _vad
                    mic_name = data.get("mic_name")
                    spk_name = data.get("speaker_name")
                    if mic_name is not None:
                        config.voice.mic_device_name = str(mic_name)
                        _vad.reset_recorder()  # next recording picks up the new device
                    if spk_name is not None:
                        config.voice.speaker_device_name = str(spk_name)
                        # No reset needed on the speaker side — TTS opens
                        # a fresh output stream per utterance and reads
                        # config fresh each time (see voice/tts.py).
                    payload = await _gather_settings_data()
                    await _safe_send(websocket, {"event": "settings_data", "set_ok": True, **payload})

            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    finally:
        _clients.discard(websocket)
        logger.info(f"UI disconnected ({len(_clients)} clients)")


async def _safe_send(websocket, data: dict):
    try:
        await websocket.send(json.dumps(data))
    except Exception:
        pass


def _safe_upload_filename(name: str) -> str:
    """Strip path separators/control chars and collapse to a bare filename
    — the browser-supplied name is untrusted input, and this file gets
    joined onto UPLOADS_DIR next, so no '..' or absolute path can escape
    it. Falls back to a generic name if sanitizing empties it out."""
    name = Path((name or "").strip()).name  # drops any directory component
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).strip(" .")
    return name[:180] or "upload"


def _save_upload(filename: str, raw: bytes) -> Path:
    """Timestamp-prefixed so two uploads with the same name never collide
    and the newest is always obvious at a glance in the folder."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_upload_filename(filename)
    dest = UPLOADS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe_name}"
    n = 1
    while dest.exists():  # extremely unlikely (same second + same name) but stay safe
        dest = UPLOADS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{n}_{safe_name}"
        n += 1
    dest.write_bytes(raw)
    return dest


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


async def broadcast(data: dict):
    global _clients
    if not _clients:
        return
    message = json.dumps(data)
    dead = set()
    for ws in _clients.copy():
        try:
            await ws.send(message)
        except Exception:
            dead.add(ws)
    _clients -= dead


async def set_state(state: str):
    global _current_state
    _current_state = state
    await broadcast({"event": "state", "value": state})


async def send_transcript(role: str, text: str):
    await broadcast({"event": "transcript", "role": role, "text": text})


async def send_intent(intent: str):
    await broadcast({"event": "intent", "value": intent})


async def send_toast(message: str, kind: str = "info"):
    await broadcast({"event": "toast", "message": message, "kind": kind})


async def send_activity(text: str, icon: str = "◆"):
    await broadcast({"event": "activity", "text": text, "icon": icon})


async def send_screen_description(description: str):
    await broadcast({"event": "screen", "description": description})


async def _gather_watchlist_data() -> dict:
    loop = asyncio.get_running_loop()
    from actions import topic_monitor, reminder_store
    from memory import open_loops

    monitors = await loop.run_in_executor(None, topic_monitor.list_monitors_full)
    reminders = await loop.run_in_executor(None, reminder_store.list_reminders)
    loops = await loop.run_in_executor(None, open_loops.list_open)
    return {"monitors": monitors, "reminders": reminders, "open_loops": loops}


async def send_watchlist_update():
    """Pushes the current monitors+reminders state to every connected
    client. Called after any add/remove so the watchlist panel updates
    live — the user never has to ask or hit refresh to see a change
    they just made reflected."""
    payload = await _gather_watchlist_data()
    await broadcast({"event": "watchlist_data", **payload})


async def _gather_memory_data() -> dict:
    loop = asyncio.get_running_loop()
    from memory.long_term import get_all
    from config import config

    memory = await loop.run_in_executor(None, get_all)
    return {"memory": memory, "vault_path": str(config.obsidian_vault_path)}


async def _gather_graph_data() -> dict:
    loop = asyncio.get_running_loop()
    from memory.graph_export import build_graph

    graph = await loop.run_in_executor(None, build_graph)
    return {"graph": graph}


def _service_display(service_key: str, entry: dict) -> dict:
    """One row of the usage table — real quota where we have it
    (ElevenLabs, Fish Audio), otherwise an honestly-labeled call count.
    `balance` is the prepaid-credit case (Fish Audio with no top-up on
    record) — a real number with no natural cap to bar against, shown
    as a plain remaining-balance figure instead of a used/limit bar."""
    row = {"service": service_key, "calls": entry.get("calls", 0), "status": entry.get("status", "ok")}
    real = entry.get("real_quota")
    if real and real.get("limit"):
        row["used"] = real.get("used")
        row["limit"] = real.get("limit")
        row["unit"] = real.get("unit", "")
    elif real and real.get("balance") is not None:
        row["balance"] = real.get("balance")
        row["unit"] = real.get("unit", "")
    return row


def _list_audio_devices() -> dict:
    """Clean, deduplicated device names via voice/audio_devices.py — one
    real entry per physical device, not one per (device × host API)."""
    from voice import audio_devices
    try:
        mics = audio_devices.list_devices("input")
        speakers = audio_devices.list_devices("output")
        try:
            import sounddevice as sd
            default_in, default_out = sd.default.device
        except Exception:
            default_in, default_out = -1, -1
        return {
            "mics": [{"name": n, "is_default": audio_devices.resolve(n, "input") == default_in}
                     for n in mics],
            "speakers": [{"name": n, "is_default": audio_devices.resolve(n, "output") == default_out}
                         for n in speakers],
        }
    except Exception as e:
        logger.warning(f"[Settings] Couldn't enumerate audio devices: {e}")
        return {"mics": [], "speakers": []}


async def _gather_settings_data() -> dict:
    from brain.llm import get_active_provider, KNOWN_PROVIDERS
    from brain import model_router
    from voice.tts import get_active_tts_provider, KNOWN_TTS_PROVIDERS
    from memory import usage_tracker as ut
    from config import config

    loop = asyncio.get_running_loop()

    # Refresh real quota data where a simple endpoint exists. ElevenLabs,
    # Fish Audio, and Tavily all expose one. Groq's real numbers are
    # captured passively from response headers on calls already being
    # made (see _record_groq_rate_limit_headers) — nothing to fetch here.
    # Exa and Gemini don't have an equivalently simple endpoint (Exa's
    # needs a separate team-admin key, Gemini's needs full GCP monitoring
    # setup) — both stay as call counts rather than a fabricated number.
    if config.voice.elevenlabs_api_key:
        try:
            from voice.tts import get_elevenlabs_usage
            await loop.run_in_executor(None, get_elevenlabs_usage)
        except Exception:
            pass
    if config.voice.fish_audio_api_key:
        try:
            from voice.tts import get_fish_audio_usage
            await loop.run_in_executor(None, get_fish_audio_usage)
        except Exception:
            pass
    if config.brain.tavily_api_key:
        try:
            from actions.search_providers import fetch_tavily_usage
            await loop.run_in_executor(None, fetch_tavily_usage)
        except Exception:
            pass

    snapshot = ut.get_usage_snapshot()
    usage_rows = [_service_display(name, entry) for name, entry in sorted(snapshot.items())]

    # Services with a configured key but zero calls yet (never gets a
    # snapshot entry from record_call) — show as "not used yet" rather
    # than omitting them, so the settings panel reflects what's actually
    # set up, not just what's been exercised this session.
    configured_but_unused = {
        "llm_ollama": bool(config.brain.ollama_base_url),
        "llm_nvidia": bool(config.brain.nvidia_nim_api_key),
        "llm_gemini": bool(config.brain.gemini_api_key),
        "groq_router": bool(config.brain.groq_api_key),
        "groq_vision": bool(config.brain.groq_api_key),
        "tavily": bool(config.brain.tavily_api_key),
        "exa": bool(config.brain.exa_api_key),
        "elevenlabs": bool(config.voice.elevenlabs_api_key),
        "fish_audio": bool(config.voice.fish_audio_api_key),
    }
    seen = {row["service"] for row in usage_rows}
    for key, configured in configured_but_unused.items():
        if key not in seen:
            usage_rows.append({
                "service": key, "calls": 0,
                "status": "not used yet" if configured else "not configured",
            })

    usage_rows.sort(key=lambda r: r["service"])

    audio_devices = await loop.run_in_executor(None, _list_audio_devices)

    return {
        "active_provider": get_active_provider(),
        "available_providers": list(KNOWN_PROVIDERS),
        "active_tts_provider": get_active_tts_provider(),
        "available_tts_providers": list(KNOWN_TTS_PROVIDERS),
        "usage": usage_rows,
        "mic_devices": audio_devices["mics"],
        "speaker_devices": audio_devices["speakers"],
        "active_mic_device": config.voice.mic_device_name,
        "active_speaker_device": config.voice.speaker_device_name,
        "router_tiers": model_router.tier_status(),
    }


async def send_audio_level(level: float):
    """Real mic/TTS amplitude, 0..1. Replaces the old client-side Math.random() fake."""
    await broadcast({"event": "audio_level", "value": max(0.0, min(1.0, level))})


def broadcast_from_thread(data: dict):
    """Thread-safe broadcast for callers running outside the asyncio loop
    (VAD mic capture, TTS playback both run in worker threads)."""
    if _main_loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(broadcast(data), _main_loop)
    except Exception:
        pass


def send_audio_level_from_thread(level: float):
    broadcast_from_thread({"event": "audio_level", "value": max(0.0, min(1.0, level))})


async def send_error(message: str):
    await set_state("error")
    await broadcast({"event": "error", "message": message})
    await send_toast(message, "error")
    await asyncio.sleep(3)
    await set_state("idle")


async def request_confirmation(message: str, detail: str = "") -> bool:
    if not _clients:
        logger.warning(f"Confirmation requested but no UI: {message}")
        return False

    conf_id = str(uuid.uuid4())[:8]
    event = asyncio.Event()
    _pending_confirmations[conf_id] = {"event": event, "result": False}

    await broadcast({
        "event": "confirm",
        "id": conf_id,
        "message": message,
        "detail": detail,
    })

    try:
        await asyncio.wait_for(event.wait(), timeout=30)
        result = _pending_confirmations[conf_id]["result"]
    except asyncio.TimeoutError:
        result = False
        logger.info(f"Confirmation timed out: {message}")
    finally:
        _pending_confirmations.pop(conf_id, None)

    return result


async def _stats_loop():
    global _last_stats
    while True:
        try:
            stats = _collect_stats_sync()
            _last_stats = stats
            await broadcast({**stats, "event": "stats"})
        except Exception as e:
            logger.debug(f"Stats error: {e}")
        await asyncio.sleep(2)


def _collect_stats_sync() -> dict:
    """Collect stats synchronously using pre-initialized NVML handle."""
    stats = {}

    # CPU + RAM via psutil
    try:
        import psutil
        stats["cpu"] = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        stats["ram"] = ram.percent
        stats["ram_used"] = round(ram.used / (1024 ** 3), 1)
        stats["ram_total"] = round(ram.total / (1024 ** 3), 1)
    except Exception:
        stats.update({"cpu": 0, "ram": 0, "ram_used": 0, "ram_total": 0})

    # GPU via pre-initialized handle (no init/shutdown overhead)
    if _nvml_ready and _nvml_handle:
        try:
            if _nvml_lib == "nvidia_smi":
                import nvidia_smi as nvml
            else:
                import pynvml as nvml

            util = nvml.nvmlDeviceGetUtilizationRates(_nvml_handle)
            mem  = nvml.nvmlDeviceGetMemoryInfo(_nvml_handle)
            temp = nvml.nvmlDeviceGetTemperature(_nvml_handle, nvml.NVML_TEMPERATURE_GPU)
            stats["gpu"]        = util.gpu
            stats["gpu_temp"]   = temp
            stats["vram_used"]  = round(mem.used  / (1024 ** 3), 1)
            stats["vram_total"] = round(mem.total / (1024 ** 3), 1)
        except Exception:
            stats.update({"gpu": 0, "gpu_temp": 0, "vram_used": 0, "vram_total": 0})
    else:
        smi = _nvidia_smi_cli_query()
        if smi:
            stats["gpu"] = smi["gpu"]
            stats["gpu_temp"] = smi["gpu_temp"]
            stats["vram_used"] = smi["vram_used"]
            stats["vram_total"] = smi["vram_total"]
        else:
            stats.update({"gpu": 0, "gpu_temp": 0, "vram_used": 0, "vram_total": 0})

    return stats


async def start_server(host: str = "localhost", port: int = 8765):
    global _server, _main_loop, _hw_info, _text_input_queue
    _mic_event.clear()
    _main_loop = asyncio.get_running_loop()
    _text_input_queue = asyncio.Queue()
    _hw_info = _detect_hw_info()
    logger.info(f"Detected hardware: {_hw_info}")
    try:
        import websockets
        # Default max_size is 1MiB — plenty for control messages, but a
        # base64-encoded upload runs ~33% bigger than the raw file, so the
        # 25MB upload cap (_MAX_UPLOAD_BYTES) needs real headroom above it
        # or legitimate uploads get killed at the transport layer before
        # the app-level size check above ever runs.
        _server = await websockets.serve(_handler, host, port, max_size=40 * 1024 * 1024)
        logger.info(f"WebSocket server running on ws://{host}:{port}")
        asyncio.create_task(_stats_loop())
    except ImportError:
        logger.warning("websockets not installed — run: pip install websockets")
    except Exception as e:
        logger.error(f"WebSocket server failed to start: {e}")


async def stop_server():
    global _server, _nvml_ready
    if _server:
        _server.close()
        await _server.wait_closed()
    # Clean up NVML
    if _nvml_ready:
        try:
            if _nvml_lib == "nvidia_smi":
                import nvidia_smi
                nvidia_smi.nvmlShutdown()
            else:
                import pynvml
                pynvml.nvmlShutdown()
        except Exception:
            pass
        _nvml_ready = False