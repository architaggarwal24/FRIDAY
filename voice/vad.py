"""
F.R.I.D.A.Y. — voice/vad.py
Voice Activity Detection using WebRTC VAD.
Records from mic and returns a clean audio segment when the user stops speaking.

Fix v2: Auto-detects mic channel count. If mic is stereo, records 2ch and
        downmixes to mono before passing to VAD/Whisper.
"""

import asyncio
import collections
import logging
import time
from typing import Optional

import numpy as np
import sounddevice as sd
import webrtcvad

from config import config

try:
    # Optional — only present once ui/ws_server.py is running. Level push
    # is best-effort: if the UI isn't up (e.g. CLI-only mode), this no-ops.
    from ui.ws_server import send_audio_level_from_thread
except Exception:
    def send_audio_level_from_thread(_level: float):
        pass


def _rms_level(frame: bytes) -> float:
    """Normalized 0..1 RMS amplitude of a mono int16 PCM frame."""
    arr = np.frombuffer(frame, dtype=np.int16)
    if arr.size == 0:
        return 0.0
    rms = np.sqrt(np.mean(arr.astype(np.float32) ** 2))
    # int16 full-scale is 32768; real speech rarely nears that, so scale
    # against a lower ceiling for a more visually responsive range.
    return float(min(1.0, rms / 8000.0))

logger = logging.getLogger(__name__)


def _get_mic_channels(device_index: int) -> int:
    """Returns the input channel count for the device. Never returns 0."""
    try:
        info = sd.query_devices(device_index if device_index >= 0 else None, kind="input")
        ch = int(info.get("max_input_channels", 0))
        if ch < 1:
            # Device has 0 input channels — it's an output-only or broken device.
            # Fall back to system default input instead.
            logger.warning(
                f"Device {device_index} has 0 input channels — "
                "falling back to system default mic."
            )
            info = sd.query_devices(kind="input")
            ch = int(info.get("max_input_channels", 1))
        return max(1, ch)
    except Exception as e:
        logger.warning(f"Could not query device {device_index}: {e} — assuming 1 channel")
        return 1


def _to_mono(raw: bytes, channels: int) -> bytes:
    """Downmix multi-channel int16 PCM to mono by averaging channels."""
    if channels == 1:
        return raw
    arr = np.frombuffer(raw, dtype=np.int16)
    # reshape to (samples, channels) then average across channels
    arr = arr.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return arr.tobytes()


class VADRecorder:
    """
    Streams audio from the microphone and uses WebRTC VAD to detect
    when the user has finished speaking. Returns the captured audio
    as a numpy float32 array at 16kHz (ready for Whisper).
    """

    def __init__(self):
        self.vad = webrtcvad.Vad(config.voice.vad_aggressiveness)

        # Use the device's actual native sample rate if it supports 16kHz,
        # otherwise resample after capture. WebRTC VAD requires 8/16/32/48kHz.
        # Resolve the saved device NAME to whatever index sounddevice
        # currently has it at (indices shift on reconnect — see
        # voice/audio_devices.py). Falls back to the legacy raw index,
        # then to system default, if name resolution comes back empty.
        from voice import audio_devices
        dev_idx = audio_devices.resolve(config.voice.mic_device_name, "input")
        if dev_idx is None and config.voice.mic_device_name == "" and config.voice.device_index >= 0:
            dev_idx = config.voice.device_index
        try:
            dev_info = sd.query_devices(dev_idx if dev_idx is not None else None, kind="input")
            native_rate = int(dev_info.get("default_samplerate", 16000))
        except Exception:
            native_rate = 16000

        # Prefer 16kHz (no resampling for Whisper). If device can't do 16kHz
        # we record at native rate and resample to 16kHz afterwards.
        SUPPORTED_VAD_RATES = {8000, 16000, 32000, 48000}
        if native_rate in SUPPORTED_VAD_RATES:
            self.sample_rate = native_rate
            self.needs_resample = (native_rate != 16000)
        else:
            self.sample_rate = 16000
            self.needs_resample = False

        self.target_rate = 16000   # Whisper always wants 16kHz
        self.chunk_ms = 30
        self.chunk_samples = int(self.sample_rate * self.chunk_ms / 1000)
        self.silence_chunks = int(config.voice.silence_threshold_ms / self.chunk_ms)
        self.min_speech_chunks = int(config.voice.min_speech_ms / self.chunk_ms)

        # Auto-detect channels — fixes "Invalid number of channels" on stereo mics
        self.channels = _get_mic_channels(dev_idx)
        self.device_idx = dev_idx  # resolved once here; record_utterance() reuses it
                                    # rather than re-reading config (which holds a NAME,
                                    # not something sd.RawInputStream(device=...) accepts)
        logger.info(
            f"VADRecorder: device={dev_idx}, channels={self.channels}, "
            f"rate={self.sample_rate}, resample={self.needs_resample}"
        )

    def _is_speech(self, frame: bytes) -> bool:
        """Returns True if the 30ms PCM frame contains speech."""
        try:
            return self.vad.is_speech(frame, self.sample_rate)
        except Exception:
            return False

    def record_utterance(self) -> Optional[np.ndarray]:
        """
        Blocking call. Records until:
          - Speech is detected, then
          - Silence_threshold_ms of silence follows
        Returns float32 numpy array at 16kHz, or None if nothing was captured.
        """
        logger.debug("VAD: listening for speech...")

        # Ring buffer holds recent frames (captures leading syllables pre-trigger)
        ring_buffer = collections.deque(maxlen=10)
        triggered = False
        voiced_frames = []
        silence_count = 0
        start_time = time.time()

        with sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=self.channels,   # use actual mic channels, not hardcoded 1
            dtype="int16",
            blocksize=self.chunk_samples * self.channels,
            device=self.device_idx,
        ) as stream:
            while True:
                # Safety cap — don't record forever
                if time.time() - start_time > config.voice.max_record_seconds:
                    logger.warning("VAD: max recording time reached")
                    break

                raw, _ = stream.read(self.chunk_samples * self.channels)
                # Downmix to mono — VAD and Whisper both require mono PCM
                frame = _to_mono(bytes(raw), self.channels)
                is_speech = self._is_speech(frame)

                # Real mic amplitude → orb reactivity in the UI (replaces
                # the old client-side Math.random() simulation).
                send_audio_level_from_thread(_rms_level(frame))

                if not triggered:
                    ring_buffer.append((frame, is_speech))
                    # Trigger when >50% of ring buffer is speech (was 80%, too strict)
                    num_voiced = sum(1 for _, s in ring_buffer if s)
                    if num_voiced > 0.5 * ring_buffer.maxlen:
                        triggered = True
                        logger.debug("VAD: speech started")
                        # Include the pre-trigger frames (catches leading syllables)
                        voiced_frames.extend(f for f, _ in ring_buffer)
                        ring_buffer.clear()
                else:
                    voiced_frames.append(frame)
                    if is_speech:
                        silence_count = 0
                    else:
                        silence_count += 1
                        if silence_count >= self.silence_chunks:
                            logger.debug("VAD: speech ended (silence detected)")
                            break

        # Recording stopped — zero out the level so the orb settles instead
        # of freezing on the last captured amplitude.
        send_audio_level_from_thread(0.0)

        if not voiced_frames:
            return None

        # Reject clips that are too short (noise, clicks)
        if len(voiced_frames) < self.min_speech_chunks:
            logger.debug("VAD: audio too short, ignoring")
            return None

        # Concatenate PCM frames and convert to float32 for Whisper
        audio_bytes = b"".join(voiced_frames)
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_float32 = audio_int16.astype(np.float32) / 32768.0

        # Resample to 16kHz if we recorded at a different rate
        if self.needs_resample and self.sample_rate != self.target_rate:
            try:
                import scipy.signal as sps
                num_samples = int(len(audio_float32) * self.target_rate / self.sample_rate)
                audio_float32 = sps.resample(audio_float32, num_samples).astype(np.float32)
            except ImportError:
                # scipy not available — use a simple decimation approximation
                ratio = self.target_rate / self.sample_rate
                indices = np.round(np.arange(0, len(audio_float32), 1 / ratio)).astype(int)
                indices = indices[indices < len(audio_float32)]
                audio_float32 = audio_float32[indices]

        return audio_float32


# Module-level singleton — reset whenever config changes
_recorder: Optional[VADRecorder] = None


def reset_recorder():
    """Call this after changing config.voice.mic_device_name at runtime."""
    global _recorder
    _recorder = None


def get_recorder() -> VADRecorder:
    global _recorder
    if _recorder is None:
        _recorder = VADRecorder()
        logger.info(
            f"VADRecorder ready — device={_recorder.device_idx}, "
            f"channels={_recorder.channels}, rate={_recorder.sample_rate}"
        )
    return _recorder


async def record_until_silence() -> Optional[np.ndarray]:
    """
    Async wrapper around VADRecorder.record_utterance().
    Runs the blocking IO in a thread so the event loop stays free.
    """
    loop = asyncio.get_running_loop()
    recorder = get_recorder()
    audio = await loop.run_in_executor(None, recorder.record_utterance)
    return audio