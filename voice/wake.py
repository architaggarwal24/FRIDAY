"""
F.R.I.D.A.Y. — voice/wake.py
Wake word detection using OpenWakeWord.

IMPORTANT — There is no "Hey FRIDAY" pre-trained model.
Two options:

Option A (default): Use "hey_mycroft" — say "Hey Mycroft" to activate.
  This is the closest-sounding free model to "Hey FRIDAY".
  It works fine once you get used to it.

Option B (custom model — say "Hey FRIDAY" for real):
  1. Record 100+ samples of yourself saying "Hey FRIDAY"
  2. Train at: https://github.com/dscripka/openWakeWord/blob/main/docs/custom_models.md
  3. Put the .onnx file in models/hey_friday.onnx (in your project folder)
  4. Set WAKE_WORD_MODEL=hey_friday in your .env

Option C: Disable wake word entirely with --no-wake flag.

Install:
    pip install openwakeword
    python -m openwakeword.utils.download_models

Available bundled models (confirmed working):
    hey_jarvis, alexa, hey_mycroft, timer, weather
"""

import asyncio
import logging
import time
from pathlib import Path
from threading import Event, Thread
from typing import Optional

import sounddevice as sd
import numpy as np

from config import config

logger = logging.getLogger(__name__)

try:
    from openwakeword.model import Model
    OWW_AVAILABLE = True
except ImportError:
    OWW_AVAILABLE = False
    logger.warning("openwakeword not installed. Run: pip install openwakeword")


# ── Model selection ───────────────────────────────────────────────────────────

def _resolve_model() -> tuple[list, str]:
    """
    Returns (model_paths_or_names, display_name).
    Priority:
      1. Custom model file in models/ (user-trained "Hey FRIDAY")
      2. WAKE_WORD_MODEL env var
      3. hey_mycroft (default — closest to "hey friday" phonetically)
    """
    import os

    # Check for custom model file
    custom_path = Path(__file__).parent.parent / "models" / "hey_friday.onnx"
    if custom_path.exists():
        logger.info(f"Custom wake word model found: {custom_path}")
        return [str(custom_path)], "Hey FRIDAY (custom)"

    # Check env var
    env_model = os.environ.get("WAKE_WORD_MODEL", "").strip()
    if env_model:
        logger.info(f"Wake word model from env: {env_model}")
        return [env_model], env_model

    # Default: hey_mycroft — most phonetically similar to "hey friday"
    # Falls back to hey_jarvis if mycroft isn't downloaded
    return ["hey_mycroft", "hey_jarvis"], "Hey Mycroft (say this to activate FRIDAY)"


class WakeWordDetector:
    def __init__(self, sensitivity: float = None):
        if not OWW_AVAILABLE:
            raise RuntimeError(
                "openwakeword not installed.\n"
                "Run: pip install openwakeword\n"
                "Then: python -m openwakeword.utils.download_models"
            )

        self.sensitivity = sensitivity or config.voice.wake_sensitivity

        model_names, display_name = _resolve_model()
        self._model_names = model_names
        self._display_name = display_name

        self._detected_event = Event()
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        self._model: Optional[Model] = None

    def _load_model(self):
        """Try each model name until one loads successfully."""
        last_error = None
        for name in self._model_names:
            try:
                logger.info(f"Loading OpenWakeWord model: {name}")
                model = Model(
                    wakeword_models=[name],
                    inference_framework="onnx",
                )
                logger.info(f"Wake word active — {self._display_name}")
                logger.info(f"Say '{self._display_name.split('(')[0].strip()}' to activate FRIDAY")
                return model
            except Exception as e:
                logger.warning(f"Model '{name}' failed to load: {e}")
                last_error = e

        raise RuntimeError(
            f"No wake word model loaded. Last error: {last_error}\n"
            "Run: python -m openwakeword.utils.download_models"
        )

    def _get_score_key(self) -> str:
        """Returns the key to look up in OWW prediction dict."""
        for name in self._model_names:
            # Custom .onnx files use stem as key
            if name.endswith(".onnx"):
                return Path(name).stem
            return name
        return self._model_names[0]

    def _listen_loop(self):
        """Background thread. Streams 80ms audio chunks through OWW."""
        if self._model is None:
            try:
                self._model = self._load_model()
            except Exception as e:
                logger.error(f"Wake word model load failed: {e}")
                return

        chunk_samples = 1280   # 80ms at 16kHz — OWW required frame size
        sample_rate = 16000
        score_key = self._get_score_key()

        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_samples,
                device=None,
            ) as stream:
                while not self._stop_event.is_set():
                    audio_chunk, _ = stream.read(chunk_samples)
                    audio_flat = audio_chunk.flatten()

                    prediction = self._model.predict(audio_flat)

                    # Try exact key first, then any key above threshold
                    score = prediction.get(score_key, 0.0)
                    if score < self.sensitivity:
                        # Try all keys — handles case where model key differs from name
                        score = max(prediction.values()) if prediction else 0.0

                    if score >= self.sensitivity:
                        logger.info(f"Wake word detected! score={score:.2f}")
                        self._detected_event.set()
                        self._model.reset()

                        while self._detected_event.is_set() and not self._stop_event.is_set():
                            time.sleep(0.05)

        except Exception as e:
            logger.error(f"Wake word listener error: {e}", exc_info=True)

    def start(self):
        self._stop_event.clear()
        self._thread = Thread(target=self._listen_loop, daemon=True, name="friday-wake")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def wait_for_detection(self, timeout: float = None) -> bool:
        detected = self._detected_event.wait(timeout=timeout)
        self._detected_event.clear()
        return detected

    def reset(self):
        self._detected_event.clear()
        if self._model:
            self._model.reset()


# ── Module-level singleton ────────────────────────────────────────────────────

_detector: Optional[WakeWordDetector] = None


def get_detector() -> WakeWordDetector:
    global _detector
    if _detector is None:
        _detector = WakeWordDetector()
        _detector.start()
    return _detector


async def wait_for_wake_word():
    """
    Async. Blocks until wake word is detected, then returns.
    If OWW is not available, logs warning and returns immediately.
    """
    if not OWW_AVAILABLE:
        logger.warning("Wake word not available — activating immediately")
        return

    loop = asyncio.get_running_loop()
    try:
        detector = get_detector()
        # Poll in background thread — doesn't block event loop
        await loop.run_in_executor(None, detector.wait_for_detection)
    except Exception as e:
        logger.error(f"wait_for_wake_word error: {e}")