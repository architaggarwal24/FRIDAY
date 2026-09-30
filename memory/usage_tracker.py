"""
memory/usage_tracker.py — shared call-count and exhaustion tracking for
every external service FRIDAY talks to.

Two things live here that are otherwise scattered:
1. The failover logic needs to know "is Tavily currently marked
   exhausted, and has it been long enough to retry it" — that's state,
   not a one-off variable, so it has to survive restarts.
2. The settings UI wants to show usage across every service in one
   table. Building a separate ad-hoc counter per service would mean the
   UI has to know about each one individually; a shared tracker means
   adding a new service to the settings display is just calling
   record_call() from wherever that service is invoked.

Real remaining-quota numbers (character count, tokens left) are fetched
live from each provider's own API where one exists (ElevenLabs has
GET /v1/user/subscription; Fish Audio has GET /wallet/self/api-credit;
Tavily/Exa currently don't expose a remaining-credits endpoint on the
plans checked). Everything else is a LOCAL call count — honestly
labeled as "calls FRIDAY has made", not "quota remaining", since
that's what it actually is.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from utils.atomic_write import atomic_write_json

_STATE_PATH = Path(__file__).parent / "usage_state.json"
_lock = threading.Lock()

# How long a service stays "exhausted" before the failover logic will
# try it again. 24h is a reasonable default for a monthly-quota service —
# short enough to recover same-day if the quota reset happens to land
# sooner, long enough not to hammer a service that just told us no.
DEFAULT_RETRY_SECONDS = 24 * 60 * 60


def _load() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(state: dict) -> None:
    try:
        atomic_write_json(_STATE_PATH, state, indent=2)
    except OSError:
        pass


def record_call(service: str) -> None:
    """Call once per actual API call, success or failure — this is a
    usage counter, not a success counter."""
    with _lock:
        state = _load()
        entry = state.setdefault(service, {"calls": 0, "status": "ok", "last_exhausted_at": None})
        entry["calls"] = entry.get("calls", 0) + 1
        entry["last_call_at"] = time.time()
        _save(state)


def mark_exhausted(service: str) -> None:
    with _lock:
        state = _load()
        entry = state.setdefault(service, {"calls": 0, "status": "ok", "last_exhausted_at": None})
        entry["status"] = "exhausted"
        entry["last_exhausted_at"] = time.time()
        _save(state)


def mark_ok(service: str) -> None:
    """Call on a successful response — clears a stale exhausted flag if
    the service turns out to be working again (quota reset, etc)."""
    with _lock:
        state = _load()
        entry = state.setdefault(service, {"calls": 0, "status": "ok", "last_exhausted_at": None})
        if entry.get("status") != "ok":
            entry["status"] = "ok"
            entry["last_exhausted_at"] = None
        _save(state)


def is_exhausted(service: str, retry_after_seconds: int = DEFAULT_RETRY_SECONDS) -> bool:
    """True if this service should be SKIPPED right now. Automatically
    becomes False again once retry_after_seconds has passed since it was
    marked exhausted — treated as 'worth trying again', not 'confirmed
    working', so a real failure will just mark it exhausted again."""
    with _lock:
        state = _load()
    entry = state.get(service)
    if not entry or entry.get("status") != "exhausted":
        return False
    last = entry.get("last_exhausted_at")
    if last is None:
        return False
    return (time.time() - last) < retry_after_seconds


def set_real_quota(service: str, used: Optional[float], limit: Optional[float],
                    unit: str = "", balance: Optional[float] = None) -> None:
    """For services with an actual queryable quota endpoint (ElevenLabs,
    Fish Audio). Stored separately from the call counter so the settings
    UI can show both 'calls FRIDAY made' and 'actual remaining quota'
    without conflating the two.

    `balance` is for prepaid-credit services (Fish Audio) that have no
    natural used/limit cap the way a subscription does — e.g. an account
    that's only ever spent free credit, never topped up, so there's no
    honest 'limit' to show a bar against. Set used/limit when you have a
    real cap, balance when you only have a raw remaining amount; the
    settings UI renders whichever one is present rather than silently
    dropping the data just because it doesn't fit the used/limit shape."""
    with _lock:
        state = _load()
        entry = state.setdefault(service, {"calls": 0, "status": "ok", "last_exhausted_at": None})
        entry["real_quota"] = {"used": used, "limit": limit, "unit": unit,
                                "balance": balance, "checked_at": time.time()}
        _save(state)


def get_usage_snapshot() -> dict:
    """Full current state — what the settings UI renders directly."""
    with _lock:
        return _load()
