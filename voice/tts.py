"""
F.R.I.D.A.Y. — voice/tts.py

  - Speech interruption: stop_current() kills audio mid-playback via threading.Event
  - TTSPipeline drains queue on stop, doesn't finish queued sentences after interrupt
  - Mute check before each sentence so pipeline respects wake-word mute state
  - Three-tier provider chain: ElevenLabs -> Fish Audio -> edge-tts. Each
    engine falls straight to the next one inline (this sentence) on error;
    _note_tts_result() tracks consecutive failures per provider and, past
    a threshold, switches the session default too — same idea for both
    ElevenLabs and Fish Audio, not a one-off special case.
"""

import asyncio
import concurrent.futures
import io
import logging
import os
import queue
import re
import threading
import time
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

try:
    # Optional — only present once ui/ws_server.py is running.
    from ui.ws_server import send_audio_level_from_thread
except Exception:
    def send_audio_level_from_thread(_level: float):
        pass


def _pcm_rms_level(chunk: bytes, sample_width: int) -> float:
    """Normalized 0..1 RMS amplitude of a raw PCM chunk. Only handles the
    16-bit case (the common pydub decode output); anything else is
    skipped rather than guessed at."""
    if sample_width != 2 or len(chunk) < 2:
        return 0.0
    import numpy as np
    arr = np.frombuffer(chunk, dtype=np.int16)
    if arr.size == 0:
        return 0.0
    rms = np.sqrt(np.mean(arr.astype(np.float32) ** 2))
    return float(min(1.0, rms / 8000.0))


# ── Global interrupt event ────────────────────────────────────────────────────
# Set by stop_current() or handle_stop(). Cleared when new speech starts.
_interrupt_event = threading.Event()

def request_interrupt():
    """Called when user says stop/cancel — kills current + queued speech."""
    _interrupt_event.set()

def clear_interrupt():
    _interrupt_event.clear()


# ── Sentence splitter ─────────────────────────────────────────────────────────
#
# Each returned chunk becomes its own separate TTS call + playback (see
# accumulate_sentences/enqueue below) — so a bad split here isn't just a
# text-processing detail, it's FRIDAY's voice audibly fragmenting mid-
# sentence. The original version split on ANY [.!?] followed by
# whitespace, which treats "Mr. Smith" as two sentences — "Mr." gets
# synthesized and played as its own clipped utterance, then a beat, then
# "Smith is here" as a separate one. Same for "e.g.", "Dr.", "vs.",
# "etc.", "a.m./p.m." — anything with a period FRIDAY says fairly often.
# It also silently DROPPED any trailing fragment of 2 characters or
# less, which could eat real short replies. Both fixed below.

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "e.g", "i.e", "approx", "vol", "fig", "inc", "ltd", "co", "corp",
    "u.s", "u.k", "a.m", "p.m", "gen", "rep", "sen", "gov", "capt", "col", "lt",
}


def split_into_sentences(text: str) -> list[str]:
    candidates = list(re.finditer(r"[.!?]+\s+", text))
    sentences: list[str] = []
    start = 0
    for m in candidates:
        preceding = text[start:m.start()].strip()
        last_word = re.split(r"\s+", preceding)[-1].lower().strip(".") if preceding else ""
        if last_word in _ABBREVIATIONS:
            continue  # not a real sentence boundary — keep accumulating
        # A bare 1–2 digit number alone at the start of a line ("1. ", "12. ")
        # is a numbered-list marker, not the end of a sentence. Splitting
        # there chopped every numbered list into fragments ("...three: 1."
        # / "**Concord** — MIS Executive 2." / ...), which is both awkward
        # to speak and makes it impossible for _condense_list_for_speech()
        # to see a whole list at once.
        _ls = text.rfind("\n", 0, m.start()) + 1
        if re.fullmatch(r"\s*\d{1,2}", text[_ls:m.start()]):
            continue
        sentences.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    # Fold any very short fragment onto the previous sentence instead of
    # dropping it — it's real generated content, not noise.
    merged: list[str] = []
    for s in sentences:
        if merged and len(s) <= 2:
            merged[-1] = f"{merged[-1]} {s}"
        else:
            merged.append(s)
    return [s for s in merged if s]


def accumulate_sentences(stream: AsyncIterator[str]):
    async def _gen():
        buffer = ""
        async for token in stream:
            buffer += token
            sentences = split_into_sentences(buffer)
            if len(sentences) > 1:
                for sentence in sentences[:-1]:
                    yield sentence
                buffer = sentences[-1]
        if buffer.strip():
            yield buffer.strip()
    return _gen()


# ── Audio player with interrupt support ──────────────────────────────────────

# One long-lived PyAudio instance instead of a fresh one per sentence.
# pyaudio.PyAudio() enumerates every host API and device on the system —
# on Windows in particular this is measurably slow — and the old code
# paid that cost (plus p.terminate()'s matching teardown) on EVERY
# single sentence, back to back, with nothing overlapping it. That's
# dead air between every sentence boundary regardless of how good the
# splitting is. The stream itself (p.open()/.close()) is still opened
# fresh per sentence below — safe, since format can legitimately change
# sentence-to-sentence on an engine fallback — just not the whole
# device-enumeration step behind it.
_shared_pyaudio: Optional["pyaudio.PyAudio"] = None
_shared_pyaudio_lock = threading.Lock()


def _get_shared_pyaudio():
    global _shared_pyaudio
    import pyaudio
    with _shared_pyaudio_lock:
        if _shared_pyaudio is None:
            _shared_pyaudio = pyaudio.PyAudio()
        return _shared_pyaudio


def _play_audio_bytes(audio_bytes: bytes, interrupt_check: threading.Event = None):
    """
    Plays audio bytes. Checks interrupt_check between chunks.
    If interrupted, stops playback immediately.
    """
    if not audio_bytes:
        return

    # Method 1: pydub + pyaudio (chunked playback — supports mid-sentence interrupt)
    try:
        import pyaudio
        from pydub import AudioSegment
        from config import config as _cfg
        from voice import audio_devices

        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
        raw = audio.raw_data
        p = _get_shared_pyaudio()
        _spk = audio_devices.resolve(_cfg.voice.speaker_device_name, "output")
        stream = p.open(
            format=p.get_format_from_width(audio.sample_width),
            channels=audio.channels,
            rate=audio.frame_rate,
            output=True,
            output_device_index=_spk,
        )

        # Play in 50ms chunks so we can interrupt between chunks
        chunk_size = int(audio.frame_rate * audio.sample_width * audio.channels * 0.05)
        offset = 0
        while offset < len(raw):
            if interrupt_check and interrupt_check.is_set():
                logger.debug("TTS: playback interrupted mid-chunk")
                break
            chunk = raw[offset:offset + chunk_size]
            stream.write(chunk)
            # Real playback amplitude → orb reactivity while speaking
            # (replaces the old client-side Math.random() simulation).
            send_audio_level_from_thread(_pcm_rms_level(chunk, audio.sample_width))
            offset += chunk_size

        send_audio_level_from_thread(0.0)
        stream.stop_stream()
        stream.close()
        return
    except Exception as e:
        logger.debug(f"pydub playback failed: {e}")

    # Method 2: Windows PowerShell (no interrupt support, but works without ffmpeg)
    if interrupt_check and interrupt_check.is_set():
        return
    try:
        import tempfile, subprocess
        suffix = ".mp3" if audio_bytes[:3] == b"ID3" or audio_bytes[:2] == b"\xff\xfb" else ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        # FIX: Media.SoundPlayer only plays .WAV — not .MP3. Use WPF MediaPlayer for MP3.
        if suffix == ".wav":
            ps_cmd = f'(New-Object Media.SoundPlayer "{tmp_path}").PlaySync()'
        else:
            # WPF's MediaPlayer.Open() loads media asynchronously — reading
            # NaturalDuration immediately after Open()/Play() (the old
            # code) reads it before the async load has actually finished,
            # so HasTimeSpan is still false and the computed sleep is ~0,
            # cutting playback off almost instantly. Poll for HasTimeSpan
            # to become true first, THEN start playback and sleep for the
            # now-correctly-known duration.
            ps_cmd = (
                f"Add-Type -AssemblyName presentationCore; "
                f"$mp = New-Object System.Windows.Media.MediaPlayer; "
                f"$mp.Open([uri]'{tmp_path}'); "
                f"$deadline = [DateTime]::Now.AddSeconds(10); "
                f"while (-not $mp.NaturalDuration.HasTimeSpan -and [DateTime]::Now -lt $deadline) {{ Start-Sleep -Milliseconds 50 }}; "
                f"$mp.Play(); "
                f"if ($mp.NaturalDuration.HasTimeSpan) {{ Start-Sleep -Seconds ($mp.NaturalDuration.TimeSpan.TotalSeconds + 1) }} "
                f"else {{ Start-Sleep -Seconds 5 }}; "
                f"$mp.Stop(); $mp.Close()"
            )
        subprocess.run(["powershell", "-c", ps_cmd], timeout=45, capture_output=True)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return
    except Exception as e:
        logger.debug(f"Windows media player fallback failed: {e}")

    # Method 3: sounddevice
    try:
        import sounddevice as sd
        import soundfile as sf
        from config import config as _cfg
        from voice import audio_devices
        data, samplerate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        _spk = audio_devices.resolve(_cfg.voice.speaker_device_name, "output")
        sd.play(data, samplerate, device=_spk, blocking=True)
    except Exception as e:
        logger.error(f"All audio playback methods failed: {e}")


# ── ElevenLabs TTS ────────────────────────────────────────────────────────────

class ElevenLabsTTS:
    def __init__(self, api_key: str, voice_id: str, model: str):
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            from elevenlabs.client import ElevenLabs
            self._client = ElevenLabs(api_key=self.api_key)
        return self._client

    def synthesize(self, text: str, interrupt: threading.Event = None) -> Optional[bytes]:
        """Network call only, no playback — split out from speak() so
        _audio_worker can run this for the NEXT sentence in a background
        thread while the CURRENT sentence is still playing (see
        TTSPipeline._audio_worker). That overlap is what removes the
        network-round-trip gap between sentences; previously every
        sentence paid for its own full synth-then-play cycle serially.
        Raises on failure — same as before, speak() below still owns all
        fallback-engine handling, this has none of its own."""
        if not text.strip() or (interrupt and interrupt.is_set()):
            return None
        client = self._get_client()
        audio_bytes = client.text_to_speech.convert(
            text=text,
            voice_id=self.voice_id,
            model_id=self.model,
            output_format="mp3_44100_128",
            voice_settings={
                "stability": 0.5,
                "similarity_boost": 0.8,
                "style": 0.2,
                "use_speaker_boost": True,
            },
        )
        if hasattr(audio_bytes, "__iter__") and not isinstance(audio_bytes, bytes):
            audio_bytes = b"".join(audio_bytes)
        return audio_bytes

    def speak(self, text: str, interrupt: threading.Event = None):
        if not text.strip():
            return
        if interrupt and interrupt.is_set():
            return

        try:
            client = self._get_client()
            audio_bytes = client.text_to_speech.convert(
                text=text,
                voice_id=self.voice_id,
                model_id=self.model,
                output_format="mp3_44100_128",
                voice_settings={
                    "stability": 0.5,
                    "similarity_boost": 0.8,
                    "style": 0.2,
                    "use_speaker_boost": True,
                },
            )
            if hasattr(audio_bytes, "__iter__") and not isinstance(audio_bytes, bytes):
                audio_bytes = b"".join(audio_bytes)
            _play_audio_bytes(audio_bytes, interrupt_check=interrupt)
        except Exception as e:
            logger.error(f"ElevenLabs TTS error: {e}")
            _note_tts_result("elevenlabs", ok=False)
            _next_available_engine("elevenlabs").speak(_phonetic_fix(text), interrupt=interrupt)
            return
        _note_tts_result("elevenlabs", ok=True)


# ── Fish Audio TTS ────────────────────────────────────────────────────────────

class FishAudioTTS:
    """Direct REST call (api.fish.audio/v1/tts) rather than a bundled SDK —
    the docs support plain JSON just as well as their msgpack option, and
    this way there's no extra dependency to install. `model_id` is Fish
    Audio's own terminology for a voice/reference model (sent as
    `reference_id`), not the TTS engine version — that's `engine`, sent as
    the `model` header, and it's optional: omitted entirely if unset,
    letting Fish Audio's server pick its own current default."""

    def __init__(self, api_key: str, model_id: str = "", engine: str = ""):
        self.api_key = api_key
        self.model_id = model_id
        self.engine = engine

    def synthesize(self, text: str, interrupt: threading.Event = None) -> Optional[bytes]:
        """See ElevenLabsTTS.synthesize()'s docstring — same split, same reason."""
        if not text.strip() or (interrupt and interrupt.is_set()):
            return None
        import requests
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.engine:
            headers["model"] = self.engine
        payload = {"text": text, "format": "mp3"}
        if self.model_id:
            payload["reference_id"] = self.model_id
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers=headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.content

    def speak(self, text: str, interrupt: threading.Event = None):
        if not text.strip():
            return
        if interrupt and interrupt.is_set():
            return

        try:
            import requests
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            if self.engine:
                headers["model"] = self.engine
            payload = {"text": text, "format": "mp3"}
            if self.model_id:
                payload["reference_id"] = self.model_id

            resp = requests.post(
                "https://api.fish.audio/v1/tts",
                headers=headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            _play_audio_bytes(resp.content, interrupt_check=interrupt)
        except Exception as e:
            logger.error(f"Fish Audio TTS error: {e}")
            _note_tts_result("fish_audio", ok=False)
            _next_available_engine("fish_audio").speak(_phonetic_fix(text), interrupt=interrupt)
            return
        _note_tts_result("fish_audio", ok=True)


# ── edge-tts fallback ─────────────────────────────────────────────────────────

class EdgeTTS:
    def __init__(self, voice: str = ""):
        # edge-tts is the final, always-available fallback — it needs no
        # API key, so unlike the other two engines it can't just cascade
        # to "nothing configured, skip it." EDGE_TTS_VOICE should be set
        # in .env; this is a last-resort safety net so an empty/missing
        # value doesn't take down the one provider everything else falls
        # back to, rather than a model choice made here.
        self.voice = voice or "en-US-AriaNeural"

    def synthesize(self, text: str, interrupt: threading.Event = None) -> Optional[bytes]:
        """See ElevenLabsTTS.synthesize()'s docstring — same split, same reason."""
        if not text.strip() or (interrupt and interrupt.is_set()):
            return None
        import edge_tts, tempfile

        async def _synth(tmp_path: str):
            communicate = edge_tts.Communicate(text, self.voice)
            await communicate.save(tmp_path)

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
                tmp_path = f.name
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_synth(tmp_path))
            finally:
                loop.close()
            if interrupt and interrupt.is_set():
                return None
            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def speak(self, text: str, interrupt: threading.Event = None):
        if not text.strip():
            return
        if interrupt and interrupt.is_set():
            return

        try:
            import edge_tts, tempfile

            async def _synth(tmp_path: str):
                communicate = edge_tts.Communicate(text, self.voice)
                await communicate.save(tmp_path)

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
                    tmp_path = f.name

                # FIX: asyncio.run() raises if called from a thread that already has
                # a running event loop (e.g. the task_queue worker thread).
                # Use a dedicated new loop to be safe in all calling contexts.
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_synth(tmp_path))
                finally:
                    loop.close()

                if interrupt and interrupt.is_set():
                    return

                # Play through the same in-process player ElevenLabsTTS
                # already uses (pydub+pyaudio, falling back to
                # sounddevice) instead of shelling out to PowerShell.
                # That script called $mp.Open() (which loads media on a
                # background thread — non-blocking) and immediately read
                # $mp.NaturalDuration.TimeSpan right after, before the
                # async load had actually finished, so the computed sleep
                # duration was ~0 and playback got cut off almost
                # instantly. This also drops a whole powershell.exe spawn
                # (loading the presentationCore assembly) per sentence,
                # which was real, measurable lag on top of that.
                with open(tmp_path, "rb") as f:
                    audio_bytes = f.read()
                _play_audio_bytes(audio_bytes, interrupt_check=interrupt)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"edge-tts speak error: {e}")


# ── Provider chain (ElevenLabs -> Fish Audio -> edge-tts) ────────────────────
# Order matters: this *is* the fallback chain. edge-tts is always usable
# (no API key needed), so it's the guaranteed last stop.
_TTS_CHAIN = ("elevenlabs", "fish_audio", "edge")


def _build_engine(provider: str):
    """Builds an engine instance for `provider` if it's actually usable
    right now (has whatever it needs configured) — returns None otherwise.
    edge-tts always returns an instance; it's the one provider with
    nothing to be missing."""
    from config import config
    if provider == "elevenlabs" and config.voice.elevenlabs_api_key:
        return ElevenLabsTTS(
            api_key=config.voice.elevenlabs_api_key,
            voice_id=config.voice.elevenlabs_voice_id,
            model=config.voice.elevenlabs_model,
        )
    if provider == "fish_audio" and config.voice.fish_audio_api_key:
        return FishAudioTTS(
            api_key=config.voice.fish_audio_api_key,
            model_id=config.voice.fish_audio_model_id,
            engine=config.voice.fish_audio_engine,
        )
    if provider == "edge":
        return EdgeTTS(voice=config.voice.edge_tts_voice)
    return None


def _next_available_engine(after: str):
    """The engine to use for *this one sentence* when `after` just failed —
    the first usable provider strictly later in _TTS_CHAIN. Always
    returns something real: edge-tts needs no key, so the chain can never
    run out."""
    idx = _TTS_CHAIN.index(after) if after in _TTS_CHAIN else -1
    for candidate in _TTS_CHAIN[idx + 1:]:
        engine = _build_engine(candidate)
        if engine is not None:
            return engine
    return _build_engine("edge")


# ── Phonetic fixes ────────────────────────────────────────────────────────────

def _phonetic_fix(text: str) -> str:
    """
    Applies phonetic corrections so TTS pronounces names correctly.
    Add your own corrections here or via PHONETIC_FIXES env var.
    Format: "written=spoken,written2=spoken2"
    e.g. PHONETIC_FIXES="YourName=PhoneticSpelling,FRIDAY=Friday"
    """
    import os
    env_fixes = os.environ.get("PHONETIC_FIXES", "")
    fixes = {}
    if env_fixes:
        for pair in env_fixes.split(","):
            if "=" in pair:
                written, spoken = pair.split("=", 1)
                fixes[rf"\b{written.strip()}\b"] = spoken.strip()
    for pattern, replacement in fixes.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


_LIST_ITEM_RE = re.compile(r"^\s*\d+[\).]\s+", re.MULTILINE)
_NUM_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _condense_list_for_speech(text: str) -> str:
    """If `text` holds a numbered list (2+ lines starting '1.'/'1)' — the
    shape actions/gmail.py's results and the model's own re-formatting of
    them take), speak a short summary instead of reading every item aloud.
    The full list still reaches the UI: this only changes what's QUEUED
    FOR SPEECH, at the same enqueue() choke-point the markdown stripping
    uses, so 'detail on screen, key points spoken' needs no change to
    what the model writes. Lead-in and trailing question are kept as-is;
    each item is reduced to its name (text before the first dash/colon)."""
    lines = text.split("\n")
    idxs = [i for i, l in enumerate(lines) if _LIST_ITEM_RE.match(l)]
    if len(idxs) < 2:
        return text
    lead_in = "\n".join(lines[:idxs[0]]).strip()
    trail = "\n".join(lines[idxs[-1] + 1:]).strip()

    names = []
    for i in idxs:
        rest = _LIST_ITEM_RE.sub("", lines[i]).strip()
        rest = re.sub(r"\*\*(.+?)\*\*", r"\1", rest)
        parts = re.split(r"\s+[-—]\s+|:\s", rest, maxsplit=1)
        names.append(parts[0].strip() or " ".join(rest.split()[:4]))

    n = len(names)
    if n == 2:
        spoken = f"{names[0]} and {names[1]}"
    elif n == 3:
        spoken = f"{names[0]}, {names[1]}, and {names[2]}"
    else:
        spoken = f"{names[0]}, {names[1]}, and {n - 2} more"

    # Skip the count when the lead-in already states it right before the
    # list ("You've got three:") — otherwise it's said twice.
    tail_words = lead_in.lower()[-20:]
    stated = bool(re.search(r"\d", tail_words)) or any(w in tail_words for w in _NUM_WORDS)
    summary = f"{spoken}." if stated else f"{n} — {spoken}."
    return " ".join(p for p in (lead_in, summary, trail) if p)


def _strip_markdown_for_speech(text: str) -> str:
    """Strips markdown SYNTAX before text reaches TTS, keeping the
    actual words. Confirmed happening: '**Manager - Risk Analytics**'
    got read aloud as 'asterisk asterisk manager dash risk analytics
    asterisk asterisk' — TTS has no concept of markdown, it just speaks
    every character it's handed. The UI renders markdown properly
    (react-markdown is already a dependency) for DISPLAY, so this only
    touches the copy handed to the TTS engine; visual formatting in the
    UI is untouched, since that's a separate path from this one."""
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text)                    # ***bold italic***
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)                        # **bold**
    text = re.sub(r'__(.+?)__', r'\1', text)                            # __bold__
    text = re.sub(r'(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)', r'\1', text)    # *italic*
    text = re.sub(r'(?<!_)_(?!_)([^_\n]+?)_(?!_)', r'\1', text)         # _italic_
    text = re.sub(r'`([^`]+)`', r'\1', text)                            # `code`
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)          # # headers
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)                # [text](url) -> text
    text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)          # leading bullet markers
    return text


# ── Unified TTS interface ─────────────────────────────────────────────────────

_tts_engine = None

def get_elevenlabs_usage() -> Optional[dict]:
    """Real quota data from ElevenLabs' own account endpoint — this is
    the one service in the stack that actually exposes a queryable
    remaining-quota number, so the settings UI shows real figures for it
    instead of just a call count."""
    from config import config
    api_key = config.voice.elevenlabs_api_key
    if not api_key:
        return None
    try:
        import requests
        resp = requests.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": api_key},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        used = data.get("character_count")
        limit = data.get("character_limit")
        from memory import usage_tracker as ut
        ut.set_real_quota("elevenlabs", used=used, limit=limit, unit="characters")
        return {"used": used, "limit": limit, "unit": "characters"}
    except Exception as e:
        logger.debug(f"[TTS] Couldn't fetch ElevenLabs usage: {e}")
        return None


def get_fish_audio_usage() -> Optional[dict]:
    """Real quota data from Fish Audio's wallet endpoint
    (GET /wallet/{user_id}/api-credit) — same idea as get_elevenlabs_usage()
    above. Fish Audio is prepaid-credit rather than subscription-cap, so
    there's no natural 'used/limit' pair the way ElevenLabs has one; we
    derive an equivalent from credit (remaining) and cumulative_top_up
    (everything ever purchased): used = cumulative_top_up - credit,
    limit = cumulative_top_up. Both fields come back as strings per Fish
    Audio's schema, hence the float() calls.

    If the account has never topped up (cumulative_top_up == 0 — e.g.
    free-credit-only accounts), there's no meaningful limit to show a
    used/limit bar against — but the raw balance is still real data, so
    it's stored as `balance` instead of silently dropped (an earlier
    version of this function returned early here without calling
    set_real_quota at all, which meant a perfectly valid, configured
    account showed as 'not used yet' in Settings — same as an unset key.
    Fixed: every successful call now persists something)."""
    from config import config
    api_key = config.voice.fish_audio_api_key
    if not api_key:
        return None
    try:
        import requests
        resp = requests.get(
            "https://api.fish.audio/wallet/self/api-credit",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        credit = float(data.get("credit", 0) or 0)
        top_up = float(data.get("cumulative_top_up", 0) or 0)
        from memory import usage_tracker as ut
        if top_up > 0:
            used = max(0.0, top_up - credit)
            ut.set_real_quota("fish_audio", used=used, limit=top_up, unit="credits")
            return {"used": used, "limit": top_up, "unit": "credits"}
        # No top-up on record — still real data, just no natural cap.
        ut.set_real_quota("fish_audio", used=None, limit=None, unit="credits", balance=credit)
        return {"balance": credit, "unit": "credits"}
    except Exception as e:
        logger.debug(f"[TTS] Couldn't fetch Fish Audio usage: {e}")
        return None


def get_tts_engine():
    global _tts_engine
    if _tts_engine is not None:
        return _tts_engine
    from config import config
    provider = get_active_tts_provider()
    # Cascades forward through the chain from the active provider — not
    # just "active provider or straight to edge": if e.g. ElevenLabs has
    # no key configured at all but Fish Audio does, that's still tried
    # before falling all the way to edge-tts.
    start = _TTS_CHAIN.index(provider) if provider in _TTS_CHAIN else 0
    for candidate in _TTS_CHAIN[start:]:
        engine = _build_engine(candidate)
        if engine is not None:
            if candidate == "elevenlabs":
                logger.info(f"TTS: using ElevenLabs ({config.voice.elevenlabs_model or 'default model'})")
            elif candidate == "fish_audio":
                logger.info("TTS: using Fish Audio")
            else:
                logger.info("TTS: using edge-tts")
            _tts_engine = engine
            return _tts_engine
    # Should be unreachable — _build_engine("edge") always returns something.
    _tts_engine = EdgeTTS(voice=config.voice.edge_tts_voice)
    return _tts_engine


# ── Session-level TTS provider switch ─────────────────────────────────────────
# Mirrors brain/llm.py's LLM provider switching exactly: a session-only
# override, reset on process restart, so Settings (or the automatic
# failure fallback below) can pick a provider without touching .env.
KNOWN_TTS_PROVIDERS = ("elevenlabs", "fish_audio", "edge")
_session_tts_provider: Optional[str] = None


def get_active_tts_provider() -> str:
    from config import config
    return _session_tts_provider or config.voice.tts_provider


def set_active_tts_provider(name: str) -> bool:
    """Explicit (Settings) or automatic (failure fallback) TTS provider
    switch. Validated against the known set. Resets the cached engine so
    the change is picked up on the very next sentence — see the
    _audio_worker fix below, which re-fetches the engine per sentence
    instead of once when the pipeline starts, or this switch would sit
    there having no effect until the app restarted."""
    global _session_tts_provider, _tts_engine
    from config import config
    if name not in KNOWN_TTS_PROVIDERS:
        logger.warning(f"[TTS] Rejected unknown provider: {name!r}")
        return False
    _session_tts_provider = None if name == config.voice.tts_provider else name
    _tts_engine = None
    logger.info(f"[TTS] Active provider set to {name!r} (session override)")
    return True


# One failure could be a transient network blip, so the current sentence
# still falls back to the next provider alone (via _next_available_engine)
# without changing anything persistent. _AUTO_SWITCH_THRESHOLD in a row is
# a much stronger signal of something actually wrong — exhausted quota,
# revoked key — and without this, every future sentence would keep
# retrying the same broken call, failing, and eating that latency for
# nothing. At that point the session default itself moves to the next
# provider in _TTS_CHAIN, and the UI gets a toast so this isn't silent.
# Same logic for ElevenLabs and Fish Audio — tracked per provider, not a
# one-off special case for whichever one happens to be primary.
_consecutive_failures: dict[str, int] = {}
_AUTO_SWITCH_THRESHOLD = 2


def _note_tts_result(provider: str, ok: bool) -> None:
    if ok:
        _consecutive_failures[provider] = 0
        return
    _consecutive_failures[provider] = _consecutive_failures.get(provider, 0) + 1
    if _consecutive_failures[provider] < _AUTO_SWITCH_THRESHOLD:
        return
    if get_active_tts_provider() != provider:
        return  # already moved on — this was a stray inline-fallback failure
    try:
        next_provider = _TTS_CHAIN[_TTS_CHAIN.index(provider) + 1]
    except (ValueError, IndexError):
        return  # last resort in the chain failing has nowhere further to go
    set_active_tts_provider(next_provider)
    logger.warning(
        f"[TTS] {provider} failed {_consecutive_failures[provider]} times in a row — "
        f"switched to {next_provider} for the rest of this session."
    )
    try:
        from ui.ws_server import broadcast_from_thread
        broadcast_from_thread({
            "event": "toast",
            "message": f"{provider} unavailable — switched to {next_provider} for now. "
                       f"Change back in Settings once it's working again.",
            "kind": "warning",
        })
    except Exception:
        pass


def speak(text: str):
    text = _phonetic_fix(text)
    clear_interrupt()
    get_tts_engine().speak(text, interrupt=_interrupt_event)


async def stream_tts(text: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, speak, text)


# ── Pipelined TTS with interrupt support ─────────────────────────────────────

class TTSPipeline:
    """
    Producer/consumer TTS queue.
    stop_current() immediately stops playback AND discards all queued sentences.
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._speaking = False
        self._speaking_lock = threading.Lock()
        # One-ahead synthesis prefetch — see _audio_worker. Exactly one
        # background slot, not a general pool: only ever the single next
        # sentence already sitting in the queue is worth prefetching: go
        # further ahead and you're synthesizing sentences the LLM hasn't
        # even finished generating yet, which wouldn't exist to prefetch.
        self._prefetch_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tts-prefetch"
        )

    @property
    def is_speaking(self) -> bool:
        with self._speaking_lock:
            return self._speaking

    def _audio_worker(self):
        # A sentence already dequeued and handed to the prefetch pool for
        # synthesis while the PREVIOUS sentence was still playing:
        # (sentence, source_queue, engine, future). source_queue is the
        # exact Queue object it was taken from — see the q-snapshot
        # comment below; task_done() must always be called against the
        # same queue object a sentence was get()'d from, prefetched or
        # not, or you get the "task_done() called too many times" crash
        # that used to kill this whole thread permanently.
        pending = None

        while self._running:
            # Re-fetched every sentence, not once at thread start — a
            # runtime provider switch (Settings, or the automatic
            # failure fallback) resets the cached engine, and this is
            # what makes that actually take effect on the very next
            # sentence instead of silently doing nothing until the
            # whole pipeline restarts. get_tts_engine() just returns the
            # cached instance in the normal case, so this costs nothing.
            engine = get_tts_engine()
            # Snapshot the queue reference for this whole item — stop_current()
            # can reassign self._queue from another thread mid-flight, and
            # re-reading the attribute between get() and task_done() means
            # they could end up operating on two different Queue objects,
            # which raises "task_done() called too many times" and used to
            # kill this entire thread (permanently breaking TTS for the rest
            # of the session — exactly what happened before this fix).
            q = self._queue

            if pending is not None:
                sentence, item_q, prefetch_engine, future = pending
                pending = None
            else:
                try:
                    sentence = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                item_q, prefetch_engine, future = q, None, None

            try:
                if sentence is None:
                    break
                if _interrupt_event.is_set():
                    continue

                with self._speaking_lock:
                    self._speaking = True
                try:
                    # Use the prefetch only if it was started against the
                    # SAME engine we're about to speak with — a provider
                    # switch between the two sentences means the prefetch
                    # is in the wrong voice, so just fall through and
                    # resynthesize normally instead of playing it.
                    audio_bytes = None
                    if future is not None and prefetch_engine is engine:
                        try:
                            audio_bytes = future.result()
                        except Exception as e:
                            logger.debug(f"TTS prefetch failed, resynthesizing inline: {e}")

                    # Prefetch the sentence right after this one — already
                    # sitting in the queue, not one we're guessing at —
                    # before playing this one, so its network round-trip
                    # overlaps with THIS sentence's playback instead of
                    # happening afterward as a silent gap. Only when this
                    # engine actually supports it (the three bundled
                    # engines all do; anything that doesn't just falls
                    # back to the exact pre-prefetch behavior below).
                    if not _interrupt_event.is_set() and hasattr(engine, "synthesize"):
                        try:
                            next_sentence = item_q.get_nowait()
                        except queue.Empty:
                            next_sentence = None
                        if next_sentence is not None:
                            next_future = self._prefetch_pool.submit(
                                engine.synthesize, next_sentence, _interrupt_event
                            )
                            pending = (next_sentence, item_q, engine, next_future)

                    if audio_bytes is not None:
                        _play_audio_bytes(audio_bytes, interrupt_check=_interrupt_event)
                    else:
                        # No usable prefetch — the original synth+play(+
                        # fallback-engine-on-error) path, unchanged.
                        engine.speak(sentence, interrupt=_interrupt_event)
                finally:
                    with self._speaking_lock:
                        self._speaking = False
            except Exception as e:
                # Never let a single bad sentence/engine hiccup kill the
                # worker — that took down TTS for the whole session before.
                logger.error(f"TTS worker error on {sentence!r}: {e}")
            finally:
                try:
                    item_q.task_done()
                except ValueError:
                    # Belt and suspenders — shouldn't happen now that we
                    # snapshot q, but must never crash the thread either way.
                    logger.debug("TTS: task_done() mismatch, ignoring")

    def start(self):
        self._running = True
        self._worker = threading.Thread(
            target=self._audio_worker, daemon=True, name="friday-tts"
        )
        self._worker.start()

    def stop(self):
        """Graceful shutdown."""
        self.stop_current()
        if self._worker and self._worker.is_alive():
            self._queue.put(None)
            self._worker.join(timeout=5)
        self._running = False
        self._prefetch_pool.shutdown(wait=False, cancel_futures=True)

    def stop_current(self):
        """
        Interrupt current playback AND drain the queue.
        Called when user says 'stop' or interrupts.
        """
        request_interrupt()
        # FIX: Replace the queue entirely instead of draining it.
        # Draining with get_nowait()+task_done() can raise ValueError if the worker
        # thread concurrently calls task_done() on the same item it already got().
        # A fresh Queue resets the unfinished_tasks counter cleanly.
        self._queue = queue.Queue()
        logger.debug("TTS: stopped and queue replaced")

    def resume(self):
        """Clear interrupt so new speech can start."""
        clear_interrupt()

    def enqueue(self, sentence: str):
        """Add sentence to playback queue."""
        if sentence.strip() and not _interrupt_event.is_set():
            self._queue.put(_phonetic_fix(_strip_markdown_for_speech(_condense_list_for_speech(sentence))))

    def _restart_worker(self):
        """Self-heal: if the worker thread ever dies (shouldn't happen now,
        but the whole point of this is not trusting that), get a fresh
        queue + thread going instead of leaving TTS permanently dead for
        the rest of the session."""
        if self._worker and self._worker.is_alive():
            return
        logger.error("TTS worker thread is dead — restarting it.")
        self._queue = queue.Queue()
        self.start()

    def wait_until_done(self, timeout: float = 30.0):
        """Blocks until the queue drains — but never forever. A bare
        self._queue.join() would hang permanently if the worker thread
        ever died (nothing left to call task_done()), which is exactly
        what silently froze the UI on "RESPONDING" before this fix."""
        q = self._queue
        deadline = time.time() + timeout
        while q.unfinished_tasks > 0:
            if self._worker is None or not self._worker.is_alive():
                self._restart_worker()
                return
            if time.time() > deadline:
                logger.warning(f"TTS wait_until_done timed out after {timeout}s")
                return
            time.sleep(0.05)

    async def speak_stream(self, sentence_stream: AsyncIterator[str]):
        loop = asyncio.get_running_loop()
        async for sentence in sentence_stream:
            self.enqueue(sentence)
        await loop.run_in_executor(None, self.wait_until_done)