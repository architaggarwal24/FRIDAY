"""
F.R.I.D.A.Y. — voice/stt.py
Speech-to-Text using faster-whisper on CUDA.

RTX 4070 benchmarks:
  - large-v3  fp16: ~150ms for a 5-second clip
  - medium    fp16: ~70ms
  - small     fp16: ~35ms

Set WHISPER_MODEL in .env to pick one — large-v3 for best accuracy, smaller if VRAM is tight.
"""

import logging
import time
from typing import Optional

import numpy as np

from config import config

logger = logging.getLogger(__name__)

# Lazy-loaded model — only initialized on first transcription call
_model = None
_model_device = None   # track what device the loaded model is on


def _load_model():
    """Loads the Whisper model. Reloads if device changed (e.g. CUDA->CPU fallback)."""
    global _model, _model_device
    current_device = config.voice.whisper_device
    if _model is not None and _model_device == current_device:
        return _model
    if _model is not None:
        _model = None  # Device changed — force reload

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "faster-whisper not installed. Run:\n"
            "  pip install faster-whisper"
        )

    logger.info(
        f"Loading Whisper {config.voice.whisper_model} on "
        f"{config.voice.whisper_device} ({config.voice.whisper_compute_type})..."
    )
    t0 = time.time()
    _model = WhisperModel(
        config.voice.whisper_model,
        device=config.voice.whisper_device,
        compute_type=config.voice.whisper_compute_type,
        # Download models to a local cache (avoids re-downloading)
        download_root="./.models/whisper",
        # CPU threads for non-GPU parts of the pipeline
        cpu_threads=4,
        num_workers=1,
    )
    elapsed = time.time() - t0
    logger.info(f"Whisper model loaded in {elapsed:.1f}s")
    _model_device = config.voice.whisper_device
    return _model


def transcribe(audio: np.ndarray) -> Optional[str]:
    """
    Transcribes a float32 numpy array (16kHz, mono) to text.

    Args:
        audio: float32 numpy array from VAD recorder

    Returns:
        Transcribed text string, or None if nothing was detected.
    """
    if audio is None or len(audio) == 0:
        return None

    model = _load_model()

    t0 = time.time()
    segments, info = model.transcribe(
        audio,
        # None means auto-detect; guard against env var setting it to string "None"
        language=(None if str(config.voice.whisper_language).lower() in ("none", "", "auto")
                  else config.voice.whisper_language),
        beam_size=config.voice.whisper_beam_size,  # 5 = much better for proper nouns/song names
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=300,
            speech_pad_ms=100,
        ),
        suppress_blank=True,
        without_timestamps=True,
        # temperature=0 forces greedy decode — bad for ambiguous proper nouns.
        # (0.0, 0.2) means: try greedy first, fall back to sampling if confidence is low.
        temperature=(0.0, 0.2),
        # Reject segments where Whisper is guessing — reduces false positives on noise
        no_speech_threshold=0.6,
        # Primes Whisper with domain vocabulary for better recognition
        initial_prompt=config.voice.whisper_initial_prompt,
        # Boost log probability for likely words — helps with short utterances
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
    )

    # Materialize the lazy segment generator
    texts = [seg.text.strip() for seg in segments if seg.text.strip()]
    result = " ".join(texts) if texts else None

    elapsed_ms = (time.time() - t0) * 1000
    if config.show_latency:
        logger.info(f"STT: {elapsed_ms:.0f}ms → {result!r}")

    if result:
        result = _clean_stt_output(result)

    return result


def _clean_stt_output(text: str) -> Optional[str]:
    """
    Cleans Whisper output:
    1. Filters known hallucinations on silence
    2. Detects and removes word repetition loops (e.g. "ARCHIT ARCHIT ARCHIT...")
    3. Deduplicates repeated sentences
    """
    # Filter known silence hallucinations
    hallucinations = {
        "thank you.", "thanks.", "you", ".", " ", "...",
        "thank you for watching.", "bye.", "bye bye.",
        "you you you", "the the the",
    }
    if text.lower().strip() in hallucinations:
        logger.debug(f"STT: filtered hallucination: {text!r}")
        return None

    # Detect word repetition loop — "ARCHIT ARCHIT ARCHIT..."
    # Split into words and check if one word dominates
    words = text.split()
    if len(words) >= 6:
        from collections import Counter
        counts = Counter(w.lower() for w in words)
        most_common_word, most_common_count = counts.most_common(1)[0]
        # If one word makes up >60% of all words, it's a loop
        if most_common_count / len(words) > 0.6:
            logger.warning(f"STT: repetition loop detected — '{most_common_word}' x{most_common_count}, collapsing")
            # Return just the word once — user probably said it once clearly
            return most_common_word.capitalize()

    # Detect sentence repetition — same sentence repeated multiple times
    sentences = [s.strip() for s in text.replace(".", ". ").split(". ") if s.strip()]
    if len(sentences) >= 3:
        from collections import Counter
        counts = Counter(s.lower() for s in sentences)
        most_common_sent, count = counts.most_common(1)[0]
        if count >= 3 and count / len(sentences) > 0.5:
            logger.warning(f"STT: sentence repetition detected, deduplicating")
            # Return unique sentences only
            seen = set()
            unique = []
            for s in sentences:
                if s.lower() not in seen:
                    seen.add(s.lower())
                    unique.append(s)
            text = ". ".join(unique)

    return text if text.strip() else None


async def transcribe_async(audio: np.ndarray) -> Optional[str]:
    """
    Async wrapper — runs transcription in a thread pool so the event loop
    stays responsive during the ~150ms GPU inference.
    """
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, transcribe, audio)


def warm_up():
    """
    Pre-loads the Whisper model at startup so the first real transcription
    isn't slow. Call this during initialization.
    """
    logger.info("Warming up Whisper model...")
    dummy = np.zeros(16000, dtype=np.float32)  # 1 second of silence
    transcribe(dummy)
    logger.info("Whisper warm-up complete.")