"""
core/gemini_ladder.py — F.R.I.D.A.Y.

Centralizes one-shot Gemini calls behind a small per-tier model ladder
with a per-model quota cooldown and a hard request timeout, instead of
every call site building its own genai.Client() against one hardcoded
model with no timeout and nothing to fall back to.

WHY THIS EXISTS
    agent/planner.py used to build a fresh genai.Client() against a single
    hardcoded model (config.agent.planner_model) with no timeout. The first
    429 (quota exhausted) killed every agent_task call for the rest of the
    session — and plan_task()'s if/elif/else meant NVIDIA NIM / Ollama were
    never tried either, even when both were configured, because the
    provider choice was "whichever is configured first", not "whichever
    actually answers".

    This module doesn't fix that by itself — agent/planner.py still has to
    call it and still has to fall through to NIM/Ollama when the whole
    Gemini ladder is exhausted — but it gives every call site a single,
    reusable way to (a) never hang forever, (b) survive one model on the
    ladder being out of quota, and (c) stop retrying a model that just told
    it "out of quota" a second ago.

Ported and trimmed from Mark-LIV's core/gemini.py (FatihMakes, CC BY-NC
4.0 — https://github.com/FatihMakes/Mark-LIV). Trimmed: no Live-API rung
(that needs an active voice session kept open for the whole call, which
this project doesn't do for one-shot text/vision calls), no
config/api_keys.json (uses FRIDAY's own config.py instead).
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from config import config

FAST = "fast"    # short classification/extraction, coordinate-finding, one-liners
SMART = "smart"  # planning, synthesis, anything that needs real reasoning

# Milliseconds. The API itself rejects anything under ~10s with "Minimum
# allowed deadline is 10s", so that's the floor regardless of what's passed.
DEFAULT_TIMEOUT_MS = 15_000
_MIN_TIMEOUT_MS = 10_000

# A model that just answered 429 is out of quota and will likely stay that
# way for a while on the free tier. Retrying it on every call is a wasted
# round-trip in front of every request — remembering it for a few minutes
# turns the ladder from a cost into a saving.
_COOLDOWN_SECONDS = 300

_cooldown: dict[str, float] = {}
_cool_lock = threading.Lock()


def _ladders() -> dict[str, tuple[str, ...]]:
    # Built per-call (not cached at import time) so picking up a changed
    # .env / rebuilt config object doesn't need a process restart.
    fast = config.brain.gemini_fast_model
    smart = config.brain.gemini_model
    return {
        # Fast-first for FAST, smart-first for SMART, but each ladder
        # falls back to the other model rather than having nothing behind
        # it — one configured model is still better than zero.
        FAST: tuple(m for m in (fast, smart) if m),
        SMART: tuple(m for m in (smart, fast) if m),
    }


def _cool(model: str) -> None:
    with _cool_lock:
        _cooldown[model] = time.monotonic() + _COOLDOWN_SECONDS


def _cooling(model: str) -> bool:
    with _cool_lock:
        until = _cooldown.get(model, 0.0)
        if until and time.monotonic() < until:
            return True
        _cooldown.pop(model, None)
        return False


def _client(timeout_ms: int):
    import google.genai as genai
    from google.genai import types as gtypes

    key = config.brain.gemini_api_key
    if not key:
        raise RuntimeError("no Gemini API key configured")
    return genai.Client(
        api_key=key,
        http_options=gtypes.HttpOptions(timeout=max(_MIN_TIMEOUT_MS, int(timeout_ms))),
    )


def call(contents, tier: str = FAST, system_instruction: Optional[str] = None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS):
    """Walk the model ladder for `tier`, skipping models currently cooling
    down from a recent 429. Returns the SDK's own response object (so
    callers needing more than `.text` — grounding metadata, candidates —
    still get it), or None if every rung failed. Never raises — a caller
    that gets None decides what "no answer" means for it (fall back to
    another provider, return a canned string, etc).
    """
    from google.genai import types as gtypes

    ladder = _ladders().get(tier, ())
    if not ladder:
        print(f"[GeminiLadder] no models configured for tier '{tier}'")
        return None

    # Prefer rungs not currently cooling down; if EVERY rung is cooling
    # down, try them anyway rather than failing without even attempting —
    # the cooldown is a heuristic, not a guarantee the quota is still dead.
    tried = [m for m in ladder if not _cooling(m)] or list(ladder)

    cl = None
    for model in tried:
        try:
            if cl is None:
                cl = _client(timeout_ms)
            kwargs = {"model": model, "contents": contents}
            if system_instruction:
                kwargs["config"] = gtypes.GenerateContentConfig(system_instruction=system_instruction)
            return cl.models.generate_content(**kwargs)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                _cool(model)
                print(f"[GeminiLadder] {model}: out of quota — cooling down {_COOLDOWN_SECONDS // 60} min")
            else:
                print(f"[GeminiLadder] {model}: {type(e).__name__}: {msg[:140]}")
    return None


def text(contents, tier: str = FAST, system_instruction: Optional[str] = None,
          timeout_ms: int = DEFAULT_TIMEOUT_MS, default: str = "") -> str:
    """`call`, reduced to the reply text. `default` when nothing answered."""
    resp = call(contents, tier=tier, system_instruction=system_instruction, timeout_ms=timeout_ms)
    if resp is None:
        return default
    return (getattr(resp, "text", None) or "").strip() or default
