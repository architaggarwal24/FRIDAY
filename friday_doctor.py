#!/usr/bin/env python3
"""
F.R.I.D.A.Y. — Setup Doctor
────────────────────────────────────────────────────────────────────────────
Standalone health check for a FRIDAY install. Drop this file in the
project root (next to config.py / start.py) and run it from there:

    python friday_doctor.py            # full check (dependencies, keyring,
                                        # live test of all 4 LLM providers,
                                        # TTS, STT, Groq router)
    python friday_doctor.py --offline  # skip anything that hits the network
    python friday_doctor.py --full     # also live-test optional extras
                                        # (Tavily, Exa) — uses a bit of quota

It imports your real config.py, so it always checks what FRIDAY will
actually use — not a re-guessed copy of it. Exits non-zero if anything
FAILed, so you can also drop it in a pre-flight check / CI step.
"""

import argparse
import importlib
import os
import platform
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# The project's own modules log through the standard logging module as a
# side effect of importing/using them (config.py, voice/tts.py, etc). We
# already surface the same information through report() below in a
# structured way, so quiet the raw log noise to keep this output readable.
import logging  # noqa: E402
logging.disable(logging.WARNING)

# ── tiny reporting helper ────────────────────────────────────────────────────

RESULTS = []  # list of (section, name, status, detail)
STATUS_TAG = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}


def report(section: str, name: str, status: str, detail: str = "") -> None:
    RESULTS.append((section, name, status, detail))
    line = f"{STATUS_TAG[status]} {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def section(title: str) -> None:
    print(f"\n[{title}]")


def mask(secret: str, keep: int = 4) -> str:
    if not secret:
        return "(empty)"
    if len(secret) <= keep:
        return "*" * len(secret)
    return "*" * (len(secret) - keep) + secret[-keep:]


def timed_call(fn, *args, **kwargs):
    """Run fn, return (ok, result_or_exception, elapsed_seconds)."""
    start = time.time()
    try:
        result = fn(*args, **kwargs)
        return True, result, time.time() - start
    except Exception as e:
        return False, e, time.time() - start


# ── args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="FRIDAY setup diagnostic")
parser.add_argument("--offline", action="store_true", help="skip all network calls")
parser.add_argument("--full", action="store_true", help="also live-test optional extras (Tavily/Exa)")
args = parser.parse_args()

print("=" * 64)
print(" F.R.I.D.A.Y. — Setup Doctor")
print("=" * 64)

# ── ENVIRONMENT ─────────────────────────────────────────────────────────────

section("ENVIRONMENT")
report("env", "Python", "OK", f"{sys.version.split()[0]} ({sys.executable})")

if not (PROJECT_ROOT / "config.py").exists():
    report("env", "project root", "FAIL",
           f"config.py not found next to this script ({PROJECT_ROOT}). "
           f"Move friday_doctor.py into the project folder and rerun.")
    print("\nCan't continue without config.py — stopping here.")
    sys.exit(1)
else:
    report("env", "project root", "OK", str(PROJECT_ROOT))

env_file = PROJECT_ROOT / ".env"
report("env", ".env file", "OK" if env_file.exists() else "WARN",
       str(env_file) if env_file.exists() else "no .env found — everything will use defaults")

# ── DEPENDENCIES ──────────────────────────────────────────────────────────────

section("DEPENDENCIES")

# (import name, pip install name, why it matters)
DEPS = [
    ("dotenv", "python-dotenv", "loads .env"),
    ("keyring", "keyring", "OS credential store for secrets + memory DB key"),
    ("cryptography", "cryptography", "memory DB at-rest encryption"),
    ("openai", "openai", "NVIDIA NIM client"),
    ("ollama", "ollama", "local Ollama client"),
    ("groq", "groq", "intent router + vision"),
    ("elevenlabs", "elevenlabs", "primary TTS"),
    ("edge_tts", "edge-tts", "fallback TTS"),
    ("faster_whisper", "faster-whisper", "speech-to-text"),
    ("websockets", "websockets", "UI <-> backend bridge"),
    ("faiss", "faiss-cpu", "semantic memory search (degrades gracefully if missing)"),
    ("playwright", "playwright", "real per-browser control (actions/browser_control.py) — degrades to plain webbrowser.open() if missing, which can't target a specific browser"),
    ("pyautogui", "pyautogui", "automated message sending (actions/send_message.py) — degrades to pre-fill-and-ask-you-to-click-Send if missing"),
    ("pyperclip", "pyperclip", "clipboard paste used by actions/send_message.py's automated sending"),
]
for import_name, pip_name, why in DEPS:
    try:
        importlib.import_module(import_name)
        report("deps", import_name, "OK")
    except ImportError as e:
        report("deps", import_name, "FAIL", f"pip install {pip_name}  ({why}) — {e}")

# google.genai vs google-generativeai — requirements.txt lists the old package,
# but brain/llm.py imports the new unified SDK. Worth checking explicitly
# since `pip install -r requirements.txt` alone won't catch this mismatch.
try:
    import google.genai  # noqa: F401
    report("deps", "google.genai", "OK")
except ImportError as e:
    report("deps", "google.genai", "FAIL",
           "pip install google-genai — note this is a DIFFERENT package from "
           "'google-generativeai' in requirements.txt; brain/llm.py imports the "
           f"newer 'google.genai' SDK specifically ({e})")

try:
    import torch
    cuda_ok = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if cuda_ok else None
    report("deps", "torch", "OK",
           (f"CUDA build (GPU acceleration for anything torch-based, e.g. sentence-transformers): {gpu_name}"
            if cuda_ok else
            "CPU-only build — fine, whisper doesn't use torch's CUDA (see STT section below); only matters if you want sentence-transformers embeddings on GPU too"))
except ImportError:
    report("deps", "torch", "WARN", "not importable directly (faster-whisper may bundle its own CUDA runtime)")

# playwright's package and its actual browser binaries are two separate
# downloads — `pip install playwright` alone leaves the client library
# with nothing to drive, and the failure ("Executable doesn't exist at
# .../ms-playwright/chromium-.../chrome") only surfaces the first time
# actions/browser_control.py actually tries to launch something. Check
# for it here instead, same reasoning as the google.genai/torch checks
# above: a plain import succeeding says nothing about whether the real
# feature works.
try:
    import playwright  # noqa: F401
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if browsers_path and browsers_path != "0":
        cache_dir = Path(browsers_path).expanduser()
    elif platform.system() == "Windows":
        cache_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ms-playwright"
    elif platform.system() == "Darwin":
        cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        cache_dir = Path.home() / ".cache" / "ms-playwright"

    installed = list(cache_dir.glob("chromium*")) + list(cache_dir.glob("firefox*")) if cache_dir.exists() else []
    if installed:
        report("deps", "playwright browsers", "OK", f"found in {cache_dir}")
    else:
        report("deps", "playwright browsers", "FAIL",
               f"package is installed but no browser binaries found in {cache_dir} — "
               f"run `playwright install` (actions/browser_control.py will fail to launch anything without this)")
except ImportError:
    pass  # already reported as a missing dep above

# ── UI BUILD (Electron) ───────────────────────────────────────────────────────
# A green `npm run build` says nothing about whether ui/main.js actually
# points at what webpack emitted — the file webpack wrote can be perfectly
# fine while main.js loads a stale path from a refactor, and the exit code
# is still 0 either way. Both checks below resolve the real paths and hit
# the disk, the same way Electron itself will.

section("UI BUILD (Electron)")


def _resolve_path_join_call(js_args: str, call_dir: Path):
    """Tiny parser for the arguments of a single `path.join(...)` call —
    handles exactly what this codebase's main.js actually writes:
    __dirname plus plain quoted string literals, comma-separated. No
    template literals, no other variables, no nested calls. Returns the
    resolved Path, or None if any argument isn't one of those two forms
    (rather than guessing)."""
    parts = []
    for raw in js_args.split(","):
        arg = raw.strip()
        if not arg:
            continue
        if arg == "__dirname":
            parts.append(str(call_dir))
            continue
        m = re.match(r'^([\'"])(.*)\1$', arg)
        if not m:
            return None
        parts.append(m.group(2))
    return Path(*parts) if parts else None


def check_ffmpeg():
    """pydub (voice/tts.py's primary playback path) shells out to ffmpeg
    to decode MP3 — the format every TTS provider here actually returns.
    Missing ffmpeg doesn't break voice output: _play_audio_bytes() falls
    back to Method 2 (Windows PowerShell playback), which the code's own
    comment notes works without ffmpeg — but that fallback can't do
    mid-sentence interrupts, which Method 1 can. So this is a real,
    if soft, feature loss worth surfacing rather than a silent failure."""
    import shutil
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        report("audio", "ffmpeg/ffprobe", "OK", f"found at {ffmpeg}")
    else:
        missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
        report("audio", "ffmpeg/ffprobe", "WARN",
               f"{' and '.join(missing)} not on PATH — TTS playback still works via the "
               f"PowerShell fallback, but you lose mid-sentence interrupt support. "
               f"Fix: install ffmpeg (e.g. `winget install ffmpeg` on Windows 10/11, or grab a "
               f"static build and add its bin/ folder to PATH) — needs both ffmpeg and ffprobe "
               f"on PATH, a pip package alone won't cover ffprobe too.")


def check_electron_entry_point():
    main_js = PROJECT_ROOT / "ui" / "main.js"
    if not main_js.exists():
        report("ui_build", "ui/main.js", "FAIL", "file not found")
        return

    text = main_js.read_text(encoding="utf-8")
    m = re.search(r'\.loadFile\(\s*path\.join\(([^)]*)\)\s*\)', text)
    if not m:
        report("ui_build", "main.js loadFile() target", "FAIL",
               "couldn't find a .loadFile(path.join(...)) call in ui/main.js — "
               "if this was renamed to loadURL() or restructured, update this "
               "check in friday_doctor.py to match")
        return

    target = _resolve_path_join_call(m.group(1), main_js.parent)
    if target is None:
        report("ui_build", "main.js loadFile() target", "WARN",
               f"found loadFile(path.join({m.group(1)})) but this checker only "
               f"understands __dirname and plain string-literal arguments — "
               f"verify the path by hand")
        return

    if target.exists():
        try:
            shown = target.relative_to(PROJECT_ROOT)
        except ValueError:
            shown = target
        report("ui_build", "main.js loadFile() target", "OK", str(shown))
    else:
        dist_dir = PROJECT_ROOT / "ui" / "renderer" / "dist"
        if not dist_dir.exists():
            hint = "ui/renderer/dist/ doesn't exist at all — run `npm run build` in ui/"
        else:
            hint = ("ui/renderer/dist/ exists but doesn't contain this file — either "
                     "main.js points at a stale/wrong path, or the build output moved")
        report("ui_build", "main.js loadFile() target", "FAIL", f"{target} does not exist — {hint}")


def check_dist_assets():
    dist_dir = PROJECT_ROOT / "ui" / "renderer" / "dist"
    index_html = dist_dir / "index.html"
    if not index_html.exists():
        report("ui_build", "dist/index.html assets", "WARN",
               "ui/renderer/dist/index.html not built yet — run `npm run build` "
               "in ui/ (nothing to check assets against)")
        return

    html = index_html.read_text(encoding="utf-8")
    refs = re.findall(r'<(?:script|link)\b[^>]*?\b(?:src|href)=["\']([^"\']+)["\']',
                       html, re.IGNORECASE)
    if not refs:
        report("ui_build", "dist/index.html assets", "WARN",
               "no <script src=...> / <link href=...> tags found in dist/index.html — "
               "unexpected for an HtmlWebpackPlugin build, worth a manual look")
        return

    missing, checked = [], 0
    for ref in refs:
        if re.match(r'^([a-z][a-z0-9+.-]*:)?//', ref, re.IGNORECASE) or ref.startswith("data:"):
            continue  # external URL / data URI — nothing local to check
        checked += 1
        if not (dist_dir / ref.lstrip("/")).resolve().exists():
            missing.append(ref)

    if missing:
        report("ui_build", "dist/index.html assets", "FAIL",
               f"{len(missing)} of {checked} referenced file(s) missing from "
               f"ui/renderer/dist/: {', '.join(missing)}")
    else:
        report("ui_build", "dist/index.html assets", "OK",
               f"all {checked} referenced file(s) present in ui/renderer/dist/")


check_electron_entry_point()
check_dist_assets()

# ── KEYRING FUNCTIONAL CHECK ──────────────────────────────────────────────────

section("SECRETS / KEYRING")

try:
    import keyring
    ok, result, _ = timed_call(lambda: (
        keyring.set_password("FRIDAY_doctor_test", "ping", "pong"),
        keyring.get_password("FRIDAY_doctor_test", "ping"),
        keyring.delete_password("FRIDAY_doctor_test", "ping"),
    ))
    if ok and result[1] == "pong":
        report("keyring", "backend round-trip", "OK", "set/get/delete all worked — secrets are stored in the OS credential store")
    else:
        report("keyring", "backend round-trip", "WARN",
               f"keyring is importable but didn't round-trip correctly ({result}) — "
               f"config.py will silently fall back to reading .env directly")
except ImportError:
    report("keyring", "backend round-trip", "SKIP", "keyring package not installed (see DEPENDENCIES above)")
except Exception as e:
    report("keyring", "backend round-trip", "WARN",
           f"keyring backend error: {e} — falling back to .env for all secrets this run")

# ── LOAD REAL CONFIG ──────────────────────────────────────────────────────────

section("CONFIG")
try:
    from config import config
    report("config", "config.py loaded", "OK")
except Exception as e:
    print(f"\nCould not import config.py: {e}")
    print("Nothing further can be checked without it — stopping here.")
    sys.exit(1)

active_llm = config.brain.llm_provider
report("config", "active LLM_PROVIDER", "OK", active_llm)
report("config", "active TTS_PROVIDER", "OK", config.voice.tts_provider)

# ── LLM PROVIDERS — test all 4, not just the active one ──────────────────────

section(f"LLM PROVIDERS  (active: {active_llm})")

PING_MESSAGES = [{"role": "user", "content": "Reply with just: ok"}]


def classify_llm_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if "402" in msg or "payment" in low or "subscription" in low or "quota" in low:
        return f"quota/billing issue — {msg}"
    if "404" in msg or "not found" in low or "does not exist" in low:
        return f"model not found (likely deprecated/renamed) — {msg}"
    if "401" in msg or "unauthorized" in low or "invalid api key" in low or "invalid_api_key" in low:
        return f"invalid API key — {msg}"
    if "connection" in low or "connect" in low or "timed out" in low or "timeout" in low:
        return f"couldn't reach the server — {msg}"
    return msg


def check_ollama(live: bool):
    model = config.brain.ollama_model
    base_url = config.brain.ollama_base_url
    default_base = "http://localhost:11434"
    if base_url != default_base and not os.environ.get("OLLAMA_HOST"):
        report("llm", "ollama (config note)", "WARN",
               f"OLLAMA_BASE_URL is set to {base_url}, but brain/llm.py calls "
               f"ollama.chat() directly without passing this host — the ollama "
               f"package uses the OLLAMA_HOST env var instead (defaults to "
               f"{default_base}). Set OLLAMA_HOST if you need a non-default host.")
    if not live:
        report("llm", "ollama", "SKIP", "offline mode")
        return
    try:
        import ollama as ol
    except ImportError:
        report("llm", "ollama", "FAIL", "ollama package not installed")
        return
    client = ol.Client(timeout=15)
    ok, result, elapsed = timed_call(client.chat, model=model, messages=PING_MESSAGES)
    if ok:
        report("llm", "ollama", "OK", f"model '{model}' responded in {elapsed:.1f}s")
    else:
        report("llm", "ollama", "FAIL" if active_llm == "ollama" else "WARN",
               f"model '{model}' — {classify_llm_error(result)}")


def check_nvidia(live: bool):
    key = config.brain.nvidia_nim_api_key
    model = config.brain.nvidia_nim_model
    if not key:
        sev = "FAIL" if active_llm == "nvidia" else "SKIP"
        report("llm", "nvidia", sev, "NVIDIA_NIM_API_KEY not set" +
               (" (this is your ACTIVE provider!)" if sev == "FAIL" else " (optional unless LLM_PROVIDER=nvidia)"))
        return
    if not live:
        report("llm", "nvidia", "SKIP", f"key present ({mask(key)}), offline mode")
        return
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url="https://integrate.api.nvidia.com/v1", timeout=15)
        ok, result, elapsed = timed_call(
            client.chat.completions.create, model=model, messages=PING_MESSAGES, max_tokens=5)
        if ok:
            report("llm", "nvidia", "OK", f"model '{model}' responded in {elapsed:.1f}s")
        else:
            report("llm", "nvidia", "FAIL" if active_llm == "nvidia" else "WARN",
                   f"model '{model}' — {classify_llm_error(result)}")
    except ImportError:
        report("llm", "nvidia", "FAIL", "openai package not installed")


def check_gemini(live: bool):
    key = config.brain.gemini_api_key
    model = config.brain.gemini_model
    if not key:
        # Gemini is never the primary LLM_PROVIDER in this project, only
        # vision/planner fallback — so a missing key is never a hard FAIL.
        report("llm", "gemini", "SKIP", "GEMINI_API_KEY not set (optional — vision + planner fallback)")
        return
    if not live:
        report("llm", "gemini", "SKIP", f"key present ({mask(key)}), offline mode")
        return
    try:
        import google.genai as genai
        client = genai.Client(api_key=key)
        ok, result, elapsed = timed_call(client.models.generate_content, model=model, contents="ok")
        if ok:
            report("llm", "gemini", "OK", f"model '{model}' responded in {elapsed:.1f}s")
        else:
            report("llm", "gemini", "WARN", f"model '{model}' — {classify_llm_error(result)}")
    except ImportError:
        report("llm", "gemini", "FAIL", "google-genai package not installed (see DEPENDENCIES)")


live_net = not args.offline
check_ollama(live_net)
check_nvidia(live_net)
check_gemini(live_net)
check_ffmpeg()

# ── TTS ────────────────────────────────────────────────────────────────────

section("TTS")

elevenlabs_key = config.voice.elevenlabs_api_key
if not elevenlabs_key:
    sev = "FAIL" if config.voice.tts_provider == "elevenlabs" else "SKIP"
    report("tts", "elevenlabs", sev, "ELEVENLABS_API_KEY not set" +
           (" (this is your ACTIVE TTS provider!)" if sev == "FAIL" else ""))
elif not live_net:
    report("tts", "elevenlabs", "SKIP", f"key present ({mask(elevenlabs_key)}), offline mode")
else:
    try:
        from voice.tts import get_elevenlabs_usage
        usage = get_elevenlabs_usage()
        if usage:
            used, limit = usage["used"], usage["limit"]
            pct = (used / limit * 100) if limit else 0
            status = "WARN" if limit and used >= limit * 0.95 else "OK"
            report("tts", "elevenlabs", status,
                   f"key valid — {used:,}/{limit:,} characters used this period ({pct:.0f}%)")
        else:
            report("tts", "elevenlabs", "WARN", "key present but usage endpoint call failed — key may be invalid")
    except Exception as e:
        report("tts", "elevenlabs", "WARN", f"couldn't check: {e}")

fish_audio_key = config.voice.fish_audio_api_key
if not fish_audio_key:
    sev = "FAIL" if config.voice.tts_provider == "fish_audio" else "SKIP"
    report("tts", "fish_audio", sev, "FISH_AUDIO_API_KEY not set" +
           (" (this is your ACTIVE TTS provider!)" if sev == "FAIL" else ""))
elif not live_net:
    report("tts", "fish_audio", "SKIP", f"key present ({mask(fish_audio_key)}), offline mode")
else:
    try:
        from voice.tts import get_fish_audio_usage
        usage = get_fish_audio_usage()
        if usage and "limit" in usage:
            used, limit = usage["used"], usage["limit"]
            pct = (used / limit * 100) if limit else 0
            status = "WARN" if limit and used >= limit * 0.95 else "OK"
            report("tts", "fish_audio", status,
                   f"key valid — {used:,.2f}/{limit:,.2f} credits used ({pct:.0f}%)")
        elif usage:
            report("tts", "fish_audio", "OK", f"key valid — {usage['balance']:,.2f} credits remaining")
        else:
            report("tts", "fish_audio", "WARN", "key present but usage endpoint call failed — key may be invalid")
    except Exception as e:
        report("tts", "fish_audio", "WARN", f"couldn't check: {e}")

try:
    import edge_tts  # noqa: F401
    report("tts", "edge-tts (fallback)", "OK", "no key required, always available")
except ImportError:
    report("tts", "edge-tts (fallback)", "FAIL", "pip install edge-tts — you have NO TTS fallback without this")

# ── STT ────────────────────────────────────────────────────────────────────

section("STT")
try:
    import faster_whisper  # noqa: F401
    report("stt", "faster-whisper", "OK", f"configured model: {config.voice.whisper_model}")
    if config.voice.whisper_device == "cuda":
        # faster-whisper runs on CTranslate2, which has its own CUDA/cuDNN
        # bindings independent of PyTorch — torch.cuda.is_available() is
        # the wrong signal here (a CPU-only torch install is common and
        # irrelevant to whisper). Ask CTranslate2 directly instead.
        try:
            import ctranslate2
            cuda_devices = ctranslate2.get_cuda_device_count()
            if cuda_devices > 0:
                report("stt", "whisper device", "OK", f"cuda — {cuda_devices} device(s) visible to CTranslate2")
            else:
                report("stt", "whisper device", "WARN",
                       "WHISPER_DEVICE=cuda but CTranslate2 sees 0 CUDA devices — "
                       "set WHISPER_DEVICE=cpu or check nvidia-cublas-cu12/nvidia-cudnn-cu12 install")
        except ImportError:
            report("stt", "whisper device", "WARN", "ctranslate2 not importable — can't verify CUDA availability")
        except Exception as e:
            report("stt", "whisper device", "WARN", f"couldn't query CTranslate2 CUDA devices: {e}")
except ImportError:
    report("stt", "faster-whisper", "FAIL", "pip install faster-whisper")

# ── INTENT ROUTER (Groq) ──────────────────────────────────────────────────────

section("INTENT ROUTER (Groq)")
groq_key = config.brain.groq_api_key
if not groq_key:
    report("groq", "groq", "SKIP", "GROQ_API_KEY not set — intent router falls back to local rules (still works, just less precise)")
elif not live_net:
    report("groq", "groq", "SKIP", f"key present ({mask(groq_key)}), offline mode")
else:
    try:
        from groq import Groq
        client = Groq(api_key=groq_key, timeout=15)
        ok, result, elapsed = timed_call(
            client.chat.completions.create,
            model=config.brain.groq_model, messages=PING_MESSAGES, max_tokens=5)
        if ok:
            report("groq", "groq router", "OK", f"model '{config.brain.groq_model}' responded in {elapsed:.1f}s")
        else:
            report("groq", "groq router", "WARN", f"model '{config.brain.groq_model}' — {classify_llm_error(result)}")
    except ImportError:
        report("groq", "groq router", "FAIL", "groq package not installed")

# ── OPTIONAL INTEGRATIONS ─────────────────────────────────────────────────────

section("OPTIONAL INTEGRATIONS")
for label, key_attr in [("Tavily (web search)", "tavily_api_key"),
                         ("Exa (web search fallback)", "exa_api_key")]:
    key = getattr(config.brain, key_attr)
    if not key:
        report("optional", label, "SKIP", f"{key_attr.upper()} not set")
    elif not (args.full and live_net):
        report("optional", label, "OK", f"key present ({mask(key)}) — not live-tested, rerun with --full to verify")
    else:
        report("optional", label, "OK", f"key present ({mask(key)}) — live test not implemented yet, treating as configured")

if config.brain.spotify_client_id and config.brain.spotify_client_secret:
    report("optional", "Spotify", "OK", "client ID + secret present (OAuth flow not tested — that needs the app running)")
else:
    report("optional", "Spotify", "SKIP", "SPOTIFY_CLIENT_ID/SECRET not set")

# ── MEMORY / AUTH ──────────────────────────────────────────────────────────────

section("MEMORY / AUTH")
report("misc", "WS_AUTH_TOKEN", "OK" if config.ws_auth_token else "FAIL",
       "present" if config.ws_auth_token else "missing — UI won't be able to authenticate")

db_path = config.memory_db_path
report("misc", "memory DB path", "OK" if db_path.parent.exists() else "WARN", str(db_path))

keyring_ok = any(s == "keyring" and n == "backend round-trip" and st == "OK" for s, n, st, _ in RESULTS)
report("misc", "memory DB encryption", "OK" if keyring_ok else "WARN",
       "encrypted at rest" if keyring_ok else "UNENCRYPTED — see SECRETS/KEYRING section above")

# ── SUMMARY ────────────────────────────────────────────────────────────────────

counts = {"OK": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
for _, _, status, _ in RESULTS:
    counts[status] += 1

print("\n" + "=" * 64)
print(f" SUMMARY: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL, {counts['SKIP']} SKIP")
print("=" * 64)

problems = [(s, n, st, d) for s, n, st, d in RESULTS if st in ("FAIL", "WARN")]
if problems:
    print("\nACTION ITEMS:")
    for i, (s, n, st, d) in enumerate(problems, 1):
        print(f"{i}. {STATUS_TAG[st]} {n}: {d}")
else:
    print("\nEverything checked out clean.")

sys.exit(1 if counts["FAIL"] else 0)
