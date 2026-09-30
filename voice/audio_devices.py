"""
voice/audio_devices.py — F.R.I.D.A.Y.
Pick which microphone and which speakers FRIDAY uses — and show a clean,
deduplicated list in Settings instead of every (device × host API)
permutation Windows actually exposes.

WHY THIS FILE EXISTS
    The first version of the Settings audio picker called
    sd.query_devices() and split the results into "has input channels" /
    "has output channels" with no further filtering. On a normal Windows
    machine that's not a device list, it's a quiz: the same physical
    headset shows up 3-4 times (once per host API — MME, DirectSound,
    WASAPI, WDM-KS), plus pseudo-devices like "Microsoft Sound Mapper"
    and "Primary Sound Driver" that aren't hardware at all. This file
    reduces that down to what Windows' own sound settings panel shows:
    one real device per name, on whichever host API actually moves audio
    — "opens successfully" is not the same thing as "works" (DirectSound
    output can report success while silently discarding everything
    written to it).

Ported and adapted from Mark-LIV's core/audio_devices.py (FatihMakes, CC
BY-NC 4.0 — https://github.com/FatihMakes/Mark-LIV). One real
architectural difference drove the adaptation, not just a find-replace:
Mark-LIV's whole audio pipeline runs at two FIXED rates (16kHz in, 24kHz
out) with no resampling, so it probes "can this open at OUR rate".
FRIDAY's voice/vad.py already queries each device's own native rate and
resamples afterward if needed (see its needs_resample logic) — so this
version probes "can this open at ITS OWN native rate", which is the test
that actually matches how FRIDAY opens streams. Same reasoning applies
to TTS playback, which already plays back at whatever rate the decoded
audio itself is, not a fixed pipeline rate.

WHY NAMES, NOT INDICES
    sounddevice identifies devices by integer index, and those indices
    shift whenever a device appears or disappears — unplug a headset and
    a saved index can silently point at something else entirely on the
    next launch. Names are stored in config instead, resolved to an
    index at stream-open time.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

DEFAULT_LABEL = "System default"
DEFAULT_VALUE = ""

_cache: Optional[dict[str, list[str]]] = None
_cache_lock = threading.Lock()
_chosen_api: dict = {"input": None, "output": None}

# Measured-not-reasoned preference order per Mark-LIV's own hard-won notes:
# an earlier version picked WASAPI for being cleanly-named and every open
# failed on non-native sample rates; the next picked DirectSound and it
# turned out to be a silent sink on output. Real probing (_transport_works,
# below) is what actually decides — this list is only where probing starts.
_PREFERRED_APIS = {
    "Windows": ("directsound", "mme", "wasapi"),
    "Darwin":  ("core audio",),
    "Linux":   ("pulse", "pipewire", "jack", "alsa"),
}

_PSEUDO_DEVICES = (
    "sound mapper", "primary sound", "sysdefault", "default",
    "dmix", "dsnoop", "surround", "samplerate", "speexrate", "upmix",
    "vdownmix", "null",
)

_probe_results: dict = {}


def _is_pseudo(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in _PSEUDO_DEVICES)


def _native_rate(dev: dict) -> int:
    try:
        return int(dev.get("default_samplerate", 0)) or 44100
    except Exception:
        return 44100


def _transport_works(idx: int, kind: str, rate: int, api_key) -> bool:
    """Does this host API actually move audio, or only pretend to?
    Output writes silence (inaudible); input reads and discards."""
    if api_key in _probe_results:
        return _probe_results[api_key]
    ok = False
    try:
        import sounddevice as sd
        secs = 0.5 if kind == "output" else 0.3
        if kind == "output":
            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                     blocksize=1024, device=idx)
            st.start()
            t0 = time.monotonic()
            st.write(bytes(int(rate * secs) * 2))
            elapsed = time.monotonic() - t0
            st.stop(); st.close()
            ok = elapsed > secs * 0.5
        else:
            frames = [0]

            def _cb(indata, n, *_a):
                frames[0] += n

            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                 blocksize=1024, device=idx, callback=_cb)
            st.start()
            time.sleep(secs)
            st.stop(); st.close()
            ok = frames[0] > rate * secs * 0.3
    except Exception as e:
        print(f"[Audio] {kind} transport probe failed: {e}")
        ok = False
    _probe_results[api_key] = ok
    return ok


def _usable(idx: int, kind: str, rate: int) -> bool:
    """Opens (and immediately closes) a real stream — check_output_settings()
    lies (passes for endpoints that then fail to actually open), this doesn't."""
    st = None
    try:
        import sounddevice as sd
        if kind == "input":
            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                 blocksize=1024, device=idx, callback=lambda *_a: None)
        else:
            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                     blocksize=1024, device=idx)
        st.start()
        return True
    except Exception:
        return False
    finally:
        if st is not None:
            try:
                st.stop(); st.close()
            except Exception:
                pass


def _display_name(name: str, devices) -> str:
    """MME truncates names to 31 chars — if another host API has a longer
    name starting with this one, show that instead."""
    if len(name) < 30:
        return name
    best = name
    for dev in devices:
        other = (dev.get("name") or "").strip()
        if len(other) > len(best) and other.startswith(name):
            best = other
    return best


def _query() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"input": [], "output": []}
    try:
        import platform
        import sounddevice as sd

        devices = list(sd.query_devices())
        try:
            apis = [a.get("name", "") for a in sd.query_hostapis()]
        except Exception:
            apis = []
        preferred = _PREFERRED_APIS.get(platform.system(), ())

        def _collect(api_filter, kind) -> list[tuple[int, str]]:
            chan = "max_input_channels" if kind == "input" else "max_output_channels"
            found, seen = [], set()
            for idx, dev in enumerate(devices):
                name = (dev.get("name") or "").strip()
                if not name or _is_pseudo(name) or name in seen:
                    continue
                if dev.get(chan, 0) <= 0:
                    continue
                if api_filter is not None:
                    api = apis[dev["hostapi"]].lower() if dev.get("hostapi", -1) < len(apis) else ""
                    if api_filter not in api:
                        continue
                if not _usable(idx, kind, _native_rate(dev)):
                    continue
                seen.add(name)
                found.append((idx, name))
            return found

        for kind in ("input", "output"):
            for api_filter in list(preferred) + [None]:
                found = _collect(api_filter, kind)
                if not found:
                    continue
                probe_rate = _native_rate(devices[found[0][0]])
                if not _transport_works(found[0][0], kind, probe_rate, (api_filter, kind)):
                    continue
                _chosen_api[kind] = api_filter
                out[kind] = [_display_name(n, devices) for _i, n in found]
                break
            if out[kind]:
                print(f"[Audio] {kind}: using {_chosen_api[kind] or 'any host API'} "
                      f"({len(out[kind])} devices)")
        return out
    except Exception as e:
        print(f"[Audio] Device enumeration failed: {e}")
    return out


def prefetch() -> None:
    """Warm the cache on a background thread so Settings never pays for
    enumeration (each probe opens a real stream) on first open."""
    def _work():
        global _cache
        result = _query()
        with _cache_lock:
            _cache = result
    threading.Thread(target=_work, daemon=True, name="audio-devices").start()


def list_devices(kind: str, refresh: bool = False) -> list[str]:
    global _cache
    with _cache_lock:
        cached = None if refresh else _cache
    if cached is None:
        cached = _query()
        with _cache_lock:
            _cache = cached
    return list(cached.get(kind, []))


def resolve(name: str, kind: str) -> Optional[int]:
    """Saved device name -> sounddevice index, or None for system default
    (also returned if the saved device is gone — a missing headset should
    degrade to the built-in speakers, not crash on startup)."""
    wanted = (name or "").strip()
    if not wanted or wanted == DEFAULT_LABEL:
        return None
    try:
        import platform
        import sounddevice as sd

        devices = list(sd.query_devices())
        try:
            apis = [a.get("name", "") for a in sd.query_hostapis()]
        except Exception:
            apis = []
        chan_key = "max_input_channels" if kind == "input" else "max_output_channels"

        def _candidates(api_filter):
            for idx, dev in enumerate(devices):
                if dev.get(chan_key, 0) <= 0:
                    continue
                if api_filter is not None:
                    api = apis[dev["hostapi"]].lower() if dev.get("hostapi", -1) < len(apis) else ""
                    if api_filter not in api:
                        continue
                yield idx, (dev.get("name") or "").strip()

        list_devices(kind)  # warms _chosen_api if cache is cold
        chosen = _chosen_api.get(kind)
        orders = ([chosen] if chosen is not None else []) \
            + [a for a in _PREFERRED_APIS.get(platform.system(), ()) if a != chosen] + [None]

        for api_filter in orders:
            partial = None
            for idx, dev_name in _candidates(api_filter):
                rate = _native_rate(devices[idx])
                if dev_name == wanted:
                    if _usable(idx, kind, rate):
                        return idx
                    continue
                if partial is None and (dev_name.startswith(wanted[:24]) or wanted.startswith(dev_name[:24])):
                    if _usable(idx, kind, rate):
                        partial = idx
            if partial is not None:
                return partial

        print(f"[Audio] Saved {kind} device '{wanted}' can't be opened on any host API — using system default")
        return None
    except Exception as e:
        print(f"[Audio] resolve({kind}) failed: {e} — using system default")
        return None
