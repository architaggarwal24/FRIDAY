"""
F.R.I.D.A.Y. — actions/focus_session.py

Focus sessions: you say "lock me in for 25 minutes", FRIDAY notes which
window you started on, and calls you out when you wander off it.

Locking granularity — window TITLE + process name, not URL host. The
original macOS design hashed the URL host via an AppleScript "front
window" call; Windows has no equivalent one-liner for reading Chrome's
current URL without either UI Automation (pywinauto reading the address
bar's accessible text — works, but adds a dependency and is slower per
tick) or a companion browser extension. Chrome does put the page title
in its window title, so "you're on Instagram" is still catchable by
name. That's coarser than true host-hashing but real and shippable;
upgrade to UI-Automation URL reading only if title-level proves too
noisy in practice, not speculatively.

PRIVACY — the point of this module's design:
The window title and process name are read fresh every tick, compared
against the session's baseline, and discarded inside the reader. They
are never logged, never broadcast, never persisted. Everything that
leaves this module — the WS payload, the end-of-session record — is
booleans, counters, and minutes only. focus_state() is the single
public accessor for that, and it builds its dict from a fixed
whitelist of keys rather than filtering a larger one, so a new internal
field can't leak by being forgotten about later.

The one deliberate exception is the drift callout itself: it names
what you drifted into ("That's Instagram, not what we're working on")
so the nag is actually useful. That name is derived fresh in
_read_drift_label(), passed straight into a single _speak() call by
_pick_drift_line(), and never returned to any caller, stored on
FocusSession, or included in focus_state()/the session ledger — it
exists for exactly one spoken line and then falls out of scope, same
as the fingerprint. See test_focus_session_privacy.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time
from typing import Callable, Optional

logger = logging.getLogger("friday.focus")

# How long a different window has to stay frontmost before it counts as
# a drift. Alt-tabbing to check something for half a second is not
# drifting; this is what stops the callouts from being unbearable.
DRIFT_GRACE_MS = 800

# Consecutive ticks a candidate window has to hold frontmost, after a
# deferred start, before it becomes the lock target.
DEFER_LOCK_TICKS = 2

# If FRIDAY's own window never leaves frontmost this long after a
# deferred start, give up rather than keep guessing — a wrong lock is
# worse than a late one, same principle as drift detection itself.
DEFER_TIMEOUT_S = 45

# The Electron shell's process name in dev mode (see start.py's
# _start_electron — there's no packaged/renamed build yet). Matched by
# process name, not window title: the title changes constantly (it's
# the page/panel name), the process name doesn't. If this project ever
# ships a branded build via electron-builder, the packaged exe gets its
# own name and this list needs a matching entry.
_FRIDAY_PROCESS_NAMES = ("electron.exe", "electron")

# The focus card (ui/card.html) is a second Electron BrowserWindow in
# the SAME process, so it shares these names — which is exactly why the
# card can't be identified by process name alone and instead tags its
# requests with source="card". See retarget().

# ── Screen watch ──────────────────────────────────────────────────────
# Ambient "you've been staring at the same thing" detection. The
# renderer (ui/renderer/components/ScreenWatch.jsx) does the capture and
# the frame-diffing locally; only when the screen has genuinely not
# changed for STUCK_THRESHOLD_S does ONE frame come here to be looked
# at. Distinct from memory/screen_awareness.py, which is the on-demand
# "what's on my screen" path and stays exactly as it is.
#
# NOTE, and this is a real difference from the posture watch: this
# feature DOES send image data, once, when it fires. That's unavoidable
# for a nudge that says something useful about what's actually on
# screen. What's bounded is how often: never during normal work, once
# per stuck episode, then silence for the cooldown.
SCREEN_STUCK_THRESHOLD_S = 60
SCREEN_STUCK_COOLDOWN_S = 180

# How long to wait after a callout before nagging again about a drift
# that's still ongoing. Configurable per session via set_nag_interval.
DEFAULT_NAG_INTERVAL_S = 60

# A session is "clean" (and extends the streak) at this much time
# on-target or better.
CLEAN_SESSION_RATIO = 0.85

# Vault key prefix for session records. Deliberately distinct and
# un-sentence-like so these ledger entries don't pollute the semantic
# search relevance of real notes (see memory/long_term.py) — a query
# like "what am I working on" should surface actual notes, not a pile
# of "focus_log_..." rows.
_LEDGER_KEY_PREFIX = "focus_log_"
_STREAK_KEY = "focus_streak"

# Canned callouts. Deliberately NOT routed through the LLM: a scripted
# line is instant and free, and round-tripping "hey, get back to work"
# through a model is slow and costs money for something that should
# land the moment you drift.
#
# Three escalation tiers for repeated nags during the SAME continuous
# drift — not different content per distraction (the {label} fills
# that in), just a firmer tone the longer it drags on. Four lines per
# tier, not three: a long drift means several nags at the same tier,
# and three lines loops noticeably over that many repeats; four holds
# up better. Written to match core/prompt.txt's actual voice — "boss"
# used occasionally, not attached to every line; dry, has opinions,
# contractions, short.
_DRIFT_LINES_TIER1 = [
    "That's {label}, not what we're working on.",
    "{label}? Come on, back to it.",
    "Drifted into {label}. Let's go.",
    "That's {label} — not the plan.",
]
_DRIFT_LINES_TIER2 = [
    "Still on {label}, boss. Back to it.",
    "{label} again — you asked me to catch this.",
    "That's twice now. {label} isn't the task.",
    "Come on. {label} can wait.",
]
_DRIFT_LINES_TIER3 = [
    "{label}. Seriously, back to work.",
    "You're still on {label}. I'm not letting this go.",
    "{label}, boss. This is the one thing you asked me to stop.",
    "Still {label}. Whatever it is, it's not this.",
]
_DRIFT_TIERS = [_DRIFT_LINES_TIER1, _DRIFT_LINES_TIER2, _DRIFT_LINES_TIER3]

_RETURN_LINES = [
    "Back on target.",
    "There we go. Back to it.",
    "Good. Carry on, boss.",
]

# ── Posture watch ─────────────────────────────────────────────────────
# The renderer (ui/renderer/components/PostureWatch.jsx) does all the
# camera work and sends ONLY booleans here via the posture_state WS
# command. No image data ever crosses that boundary — see that file's
# header for the full contract.

# How long posture has to stay bad continuously before it's worth
# saying anything. Short enough to feel responsive, long enough that
# leaning over to pick up a pen doesn't trigger it.
POSTURE_BAD_SUSTAIN_MS = 700

# After a nudge, say nothing about posture for this long regardless of
# how bad it stays — one dry nudge, then leave them alone. Without this
# it'd nag every 700ms, which is unusable.
POSTURE_NUDGE_COOLDOWN_S = 30

# Absence gets a much longer grace than bad posture: stepping away for
# a moment is normal, and the camera also briefly loses you when you
# turn or stretch.
POSTURE_ABSENCE_GRACE_S = 12

# "Give me a minute" / "I need to focus on something important" —
# silences posture nudges (NOT drift callouts) for this long.
RELIEF_VALVE_S = 180

_POSTURE_LINES = [
    "Sit up.",
    "Posture, boss.",
    "You're folding in on yourself.",
    "Straighten up a second.",
    "Shoulders back.",
    "That slouch is going to cost you later.",
]

_ABSENCE_LINES = [
    "Still there?",
    "You've been gone a bit, boss.",
    "Chair's empty, clock's still running.",
]

# Known sites, matched by substring against the window title
# (case-insensitive) — checked in order, first match wins. Kept short
# and specific on purpose: a bare "x" would match half of everything
# ("Excel", "Xbox", "Notepad++"...), so X is matched by its title
# suffix pattern ("Post title / X") and by the legacy "twitter" name,
# never by the bare letter.
_SITE_LABELS = [
    ("instagram", "Instagram"),
    ("youtube", "YouTube"),
    ("reddit", "Reddit"),
    ("tiktok", "TikTok"),
    ("netflix", "Netflix"),
    ("gmail", "Gmail"),
    ("twitter", "X"),
    (" / x", "X"),
]

# Process name -> speakable app name, for anything not in the site map
# above. Best-effort and short by construction — this (or the title-case
# fallback below it) is what stands in for "the raw title verbatim" that
# this feature is explicitly not allowed to speak.
_APP_DISPLAY_NAMES = {
    "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint",
    "outlook": "Outlook", "code": "VS Code", "discord": "Discord",
    "slack": "Slack", "spotify": "Spotify", "explorer": "File Explorer",
    "notepad": "Notepad", "acrord32": "Acrobat Reader", "steam": "Steam",
}


def _is_friday_process(proc: str) -> bool:
    return (proc or "").strip().lower() in _FRIDAY_PROCESS_NAMES


def _process_display_name(proc: str) -> str:
    """Raw process name (e.g. "chrome.exe", "WINWORD.EXE") -> something
    speakable ("Chrome", "Word"). Falls back to a cleaned, title-cased
    version of the process stem for anything not in the known-app map —
    still short, still never the raw window title."""
    proc = (proc or "").strip()
    if not proc:
        return "something else"
    stem = re.sub(r'\.(exe|app)$', '', proc, flags=re.IGNORECASE)
    known = _APP_DISPLAY_NAMES.get(stem.lower())
    if known:
        return known
    cleaned = re.sub(r'[_\-]+', ' ', stem).strip()
    return cleaned.title() if cleaned else "something else"


def _derive_drift_label(title: str, proc: str) -> str:
    """The one place this whole feature exists for: turns a raw window
    title/process into a short, speakable name for exactly one drift
    callout line. Site names win by substring match against the title;
    everything else falls back to the app's own display name. Never
    the raw title verbatim — see module docstring for the privacy
    contract this return value is bound by (one _speak() call, then
    gone; never stored, broadcast, or logged)."""
    low_title = (title or "").lower()
    for needle, label in _SITE_LABELS:
        if needle in low_title:
            return label
    return _process_display_name(proc)


def _read_drift_label() -> str:
    """Reads the frontmost window fresh, specifically to derive a short
    spoken label for a single drift callout line — kept separate from
    _read_frontmost() (used for fingerprinting/is_friday, called every
    tick) so this one only runs at the moment a callout is actually
    about to be spoken. Same privacy boundary as _read_frontmost(): the
    raw title/process exist as locals here and nowhere else."""
    try:
        from brain.handlers import _get_frontmost_window
        title, proc = _get_frontmost_window()
    except Exception as e:
        logger.debug(f"[Focus] drift-label read failed: {e}")
        return "something else"
    return _derive_drift_label(title, proc)


def _pick(pool: list, last: str = "") -> str:
    """Random line, avoiding an immediate repeat of the previous one."""
    options = [line for line in pool if line != last] or list(pool)
    return random.choice(options)


def _pick_drift_line(nag_count: int, last: str = "") -> str:
    """Picks a line from the escalation tier matching how many nags this
    SAME continuous drift has already had (1 -> tier 1, 2 -> tier 2, 3+
    -> tier 3, clamped — it doesn't keep escalating forever), fills in
    a freshly-derived label, and returns the finished, speakable line.
    The label is derived and used right here, in this one call, and
    never returned separately."""
    tier = _DRIFT_TIERS[min(max(nag_count, 1), len(_DRIFT_TIERS)) - 1]
    template = _pick(tier, last)
    return template.format(label=_read_drift_label())


def _fingerprint(title: str, proc: str) -> str:
    """One-way fingerprint of a window identity. Even though this never
    leaves the module, hashing means a heap dump or an accidental log of
    session internals still can't reveal which app or page it was."""
    raw = f"{(proc or '').strip().lower()}\x00{(title or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


class FocusSession:
    """Server-side session state. One at a time — a second start()
    replaces the first rather than running two overlapping sessions."""

    def __init__(self, minutes: int, label: str = ""):
        self.planned_seconds = max(1, int(minutes)) * 60
        # `label` is user-supplied ("thesis", "tax stuff") — it's their
        # own word for the session, not an observed app identity, so
        # it's safe to echo back. Never derived from a window title.
        self.label = (label or "").strip()[:60]

        self.started_at = time.time()
        self.baseline: Optional[str] = None   # fingerprint, set on first tick
        self.active = True
        self.paused = False

        # Deferred-lock state (see module docstring / start()) — set at
        # construction time if FRIDAY's own window was frontmost when
        # the session was requested. deferred_seconds is excluded from
        # elapsed_seconds(), same as paused_seconds: time spent waiting
        # for the user to switch away doesn't count against the planned
        # session length, since nothing is "on target" yet to measure.
        self.is_deferred = False
        self.awaiting_label_reply = False
        self.deferred_seconds = 0.0
        self._defer_started_at: Optional[float] = None
        self._defer_candidate_fp: Optional[str] = None
        self._defer_candidate_ticks = 0

        self.on_target_seconds = 0.0
        self.paused_seconds = 0.0
        self.drift_count = 0

        self.drifting = False
        self._pending_since: Optional[float] = None   # when the off-target window first appeared
        self._pending_fp: Optional[str] = None
        self._drift_started_at: Optional[float] = None
        self._drift_nag_count = 0   # resets each time a NEW drift begins; drives escalation tier
        self._last_nag_at = 0.0
        self._last_line = ""
        self.nag_interval_s = DEFAULT_NAG_INTERVAL_S

        self._last_tick = time.time()

    # ── derived values ────────────────────────────────────────────────

    def elapsed_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at - self.paused_seconds - self.deferred_seconds)

    def remaining_seconds(self) -> float:
        return max(0.0, self.planned_seconds - self.elapsed_seconds())

    def on_target_ratio(self) -> float:
        elapsed = self.elapsed_seconds()
        if elapsed <= 0:
            return 1.0
        return max(0.0, min(1.0, self.on_target_seconds / elapsed))

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0


# Module-level singleton + the callbacks the runtime wires in at startup.
class PostureWatch:
    """Posture state, fed entirely by booleans from the renderer.

    Deliberately module-level rather than a field on FocusSession: the
    camera can be on with no session running (you might just want the
    nudges), and a session can run with the camera off. Keeping it here
    means neither has to exist for the other to work — but it lives in
    THIS module, surfaces through the same focus_state() whitelist, and
    the relief valve is one more flag on it rather than a parallel
    system somewhere else, per the brief.

    Nothing here is ever anything but a boolean or a timestamp. There
    is no field that could hold a frame, a landmark, or a measurement,
    because none is ever sent.
    """

    def __init__(self):
        self.monitoring = False      # renderer says the camera is running
        self.present = True          # someone's in frame
        self.head_down = False
        self.slouched = False

        self._bad_since: Optional[float] = None
        self._last_nudge_at = 0.0
        self._absent_since: Optional[float] = None
        self._absence_called = False
        self._last_line = ""
        self._relief_until = 0.0

        # Screen watch — same object because it's the same "ambient
        # watcher" concern and shares the relief valve.
        self.screen_stuck = False
        self._last_screen_nudge_at = 0.0

    def bad_posture(self) -> bool:
        return bool(self.present and (self.head_down or self.slouched))

    def relief_active(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) < self._relief_until

    def silence(self, seconds: float = RELIEF_VALVE_S, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self._relief_until = now + seconds
        # Clear in-progress timers too — coming back from a relief
        # window shouldn't instantly fire a nudge for a slouch that
        # started before it.
        self._bad_since = None
        self._absent_since = None
        self._absence_called = False


_session: Optional[FocusSession] = None
_posture = PostureWatch()
_speak_fn: Optional[Callable] = None
_broadcast_fn: Optional[Callable] = None
_last_broadcast_state: Optional[dict] = None


def configure(speak_fn: Optional[Callable] = None,
              broadcast_fn: Optional[Callable] = None) -> None:
    """Wires in how to speak and how to push state to the UI. Called
    once at startup (start.py), same as the proactive monitor's
    speak_fn. Both are optional — the session still runs and records
    correctly with neither, which is what makes it testable."""
    global _speak_fn, _broadcast_fn
    _speak_fn = speak_fn
    _broadcast_fn = broadcast_fn


def focus_state() -> dict:
    """THE single client-visible view of session state.

    Built from an explicit whitelist — booleans, counters, minutes, and
    the user's own label. Never the window title, never the process
    name, never the fingerprint. If you add internal state to
    FocusSession, it does not appear here unless you deliberately add it
    to this dict, which is the point.
    """
    p = _posture
    posture_keys = {
        # All booleans, all derived in the renderer from frames that
        # never left it. There is no "how slouched" number here and no
        # way to add one — the WS command only accepts booleans.
        "posture_monitoring": bool(p.monitoring),
        "posture_present": bool(p.present),
        "posture_head_down": bool(p.head_down),
        "posture_slouched": bool(p.slouched),
        "posture_silenced": bool(p.relief_active()),
        "screen_stuck": bool(p.screen_stuck),
    }

    s = _session
    if s is None or not s.active:
        return {
            "active": False,
            "paused": False,
            "drifting": False,
            "is_deferred": False,
            "label": "",
            "planned_minutes": 0,
            "elapsed_minutes": 0,
            "remaining_minutes": 0,
            "on_target_minutes": 0,
            "on_target_percent": 0,
            "drift_count": 0,
            "nag_interval_seconds": DEFAULT_NAG_INTERVAL_S,
            **posture_keys,
        }
    return {
        "active": True,
        "paused": bool(s.paused),
        "drifting": bool(s.drifting),
        "is_deferred": bool(s.is_deferred),
        "label": s.label,
        "planned_minutes": int(round(s.planned_seconds / 60)),
        "elapsed_minutes": int(s.elapsed_seconds() // 60),
        "remaining_minutes": int(round(s.remaining_seconds() / 60)),
        "on_target_minutes": int(s.on_target_seconds // 60),
        "on_target_percent": int(round(s.on_target_ratio() * 100)),
        "drift_count": int(s.drift_count),
        "nag_interval_seconds": int(s.nag_interval_s),
        **posture_keys,
    }


def _speak(line: str) -> None:
    if not line or _speak_fn is None:
        return
    try:
        _speak_fn(line)
    except Exception as e:
        logger.debug(f"[Focus] speak failed: {e}")


async def _broadcast_if_changed(force: bool = False) -> None:
    """Pushes focus_status to the UI on every state change (not just on
    request). Compares the whitelisted dict itself, so this is also a
    second guarantee that nothing outside the whitelist is in flight."""
    global _last_broadcast_state
    state = focus_state()
    if not force and state == _last_broadcast_state:
        return
    _last_broadcast_state = state
    if _broadcast_fn is None:
        return
    try:
        result = _broadcast_fn({"event": "focus_status", **state})
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        logger.debug(f"[Focus] broadcast failed: {e}")


def _read_frontmost() -> tuple:
    """Reads the frontmost window fresh and returns ONLY a fingerprint
    plus whether it's FRIDAY's own window — never the raw title/process
    name themselves.

    The raw title and process name exist as locals here and nowhere
    else; they are compared and discarded inside this function. This is
    the privacy boundary for the whole module. Returns (None, False) if
    the platform can't report a window at all (Linux, or a failed
    call), so callers can tell "unknown" apart from "different window"
    and not invent a drift — or a deferred lock — out of a failed read.

    is_friday is only consulted by the deferred-lock flow (see start()
    and _tick_deferred()); ordinary drift tracking only needs the
    fingerprint and ignores the second value.
    """
    try:
        from brain.handlers import _get_frontmost_window
        title, proc = _get_frontmost_window()
    except Exception as e:
        logger.debug(f"[Focus] window read failed: {e}")
        return None, False
    if not title and not proc:
        return None, False
    return _fingerprint(title, proc), _is_friday_process(proc)


def _read_current_fingerprint() -> Optional[str]:
    """Fingerprint-only convenience wrapper around _read_frontmost(),
    for the ordinary (non-deferred) drift-tracking path."""
    return _read_frontmost()[0]


def is_awaiting_voice_reply() -> bool:
    """True while a deferred session is waiting on the user's spoken
    answer to "what are we focusing on" — checked by start.py's voice
    loop to skip the wake-word wait for exactly this one reply, the
    same "mic open for one reply" shape sentinel's pending confirmations
    already use (see _handle_user_text's pending-confirmation check)."""
    s = _session
    return bool(s is not None and s.active and s.is_deferred and s.awaiting_label_reply)


def consume_voice_reply(text: str) -> Optional[str]:
    """Called from start.py's _handle_user_text, in the same early,
    before-normal-classification position as sentinel's pending-
    confirmation check. If a deferred session is actively waiting on
    its "what are we focusing on" answer, treats `text` as the session
    label, consumes the turn, and returns a spoken acknowledgment.
    Returns None otherwise so the caller falls through to normal
    handling — same contract as sentinel's is_confirmation()."""
    if not is_awaiting_voice_reply():
        return None
    s = _session
    text = (text or "").strip()
    s.awaiting_label_reply = False
    if text:
        s.label = text[:60]
    ack = f"Got it — {s.label}. " if s.label else ""
    return f"{ack}Switch over whenever you're ready — I'll lock on once you're there."


async def update_posture(present: bool = True, head_down: bool = False,
                          slouched: bool = False, monitoring: bool = True,
                          now: Optional[float] = None) -> None:
    """Entry point for the posture_state WS command (ui/ws_server.py).

    Takes three booleans and nothing else — by signature, there is no
    way for a frame, a landmark array, or a numeric measurement to get
    in here even by accident. The renderer computes these from video it
    never transmits.

    Evaluated on arrival rather than in tick(): the renderer sends
    several times a second, so POSTURE_BAD_SUSTAIN_MS (700ms) is
    actually measurable here. The 1s tick loop is too coarse to
    resolve it.
    """
    p = _posture
    now = now if now is not None else time.time()

    p.monitoring = bool(monitoring)
    p.present = bool(present)
    p.head_down = bool(head_down)
    p.slouched = bool(slouched)

    if not p.monitoring:
        p._bad_since = None
        p._absent_since = None
        p._absence_called = False
        await _broadcast_if_changed()
        return

    # Paused session = deliberately stepped away from the work; nothing
    # to nudge about. Relief valve = explicitly asked to be left alone.
    muted = p.relief_active(now) or (_session is not None and _session.active and _session.paused)

    if not p.present:
        # Absence: much longer grace, and it's said at most once per
        # continuous absence rather than every cooldown window.
        p._bad_since = None
        if p._absent_since is None:
            p._absent_since = now
        elif (not muted and not p._absence_called
                and (now - p._absent_since) >= POSTURE_ABSENCE_GRACE_S):
            p._absence_called = True
            line = _pick(_ABSENCE_LINES, p._last_line)
            p._last_line = line
            _speak(line)
        await _broadcast_if_changed()
        return

    p._absent_since = None
    p._absence_called = False

    if p.bad_posture():
        if p._bad_since is None:
            p._bad_since = now
        sustained_ms = (now - p._bad_since) * 1000
        cooled_down = (now - p._last_nudge_at) >= POSTURE_NUDGE_COOLDOWN_S
        if not muted and sustained_ms >= POSTURE_BAD_SUSTAIN_MS and cooled_down:
            p._last_nudge_at = now
            line = _pick(_POSTURE_LINES, p._last_line)
            p._last_line = line
            _speak(line)
    else:
        p._bad_since = None

    await _broadcast_if_changed()


async def report_screen_stuck(image_b64: str = "", image_format: str = "jpeg",
                               now: Optional[float] = None) -> None:
    """Called when the renderer reports the screen hasn't changed for
    SCREEN_STUCK_THRESHOLD_S. Sends that ONE frame to the vision
    provider for a nudge that actually says something about what's on
    screen, then goes quiet for SCREEN_STUCK_COOLDOWN_S.

    Silent no-op when the relief valve is up or a session is paused —
    same mute rules as the posture nudges, since it's the same kind of
    interruption. The frame is used for one request and not retained.
    """
    p = _posture
    now = now if now is not None else time.time()

    if (now - p._last_screen_nudge_at) < SCREEN_STUCK_COOLDOWN_S:
        return
    if p.relief_active(now) or (_session is not None and _session.active and _session.paused):
        return

    p._last_screen_nudge_at = now
    p.screen_stuck = True
    await _broadcast_if_changed(force=True)

    line = ""
    if image_b64:
        try:
            from brain.handlers import _vision_capability_note, _describe_image_b64
            note = _vision_capability_note()
            if note:
                # No vision configured — say something honest and generic
                # rather than pretending to have looked.
                line = "You've been on the same screen a while, boss."
            else:
                line = await _describe_image_b64(
                    image_b64, image_format,
                    "The user has been staring at this exact screen without it changing for a "
                    "minute. In ONE short spoken sentence, say something specific and genuinely "
                    "useful about what's on it — name what they're stuck on if you can tell. "
                    "Dry, no preamble, no pleasantries.",
                )
        except Exception as e:
            logger.debug(f"[Focus] screen-stuck vision call failed: {e}")

    _speak((line or "").strip() or "You've been on the same screen a while, boss.")


async def clear_screen_stuck() -> None:
    """Renderer reports the screen changed again."""
    if _posture.screen_stuck:
        _posture.screen_stuck = False
        await _broadcast_if_changed()


async def silence_posture(seconds: float = RELIEF_VALVE_S) -> str:
    """The relief valve — "give me a minute", "I need to focus on
    something important". Silences POSTURE nudges only; drift callouts
    are a different thing the user explicitly asked for and keep
    running. Works whether or not a session is active."""
    _posture.silence(seconds)
    await _broadcast_if_changed(force=True)
    mins = int(round(seconds / 60))
    return f"Alright — quiet on the posture for {mins} minute{'s' if mins != 1 else ''}."


async def tick(now: Optional[float] = None) -> None:
    """One second of session logic. Separated from the loop below so
    tests can drive it directly without waiting in real time."""
    s = _session
    if s is None or not s.active:
        return

    now = now if now is not None else time.time()
    delta = max(0.0, now - s._last_tick)
    s._last_tick = now

    if s.paused:
        s.paused_seconds += delta
        await _broadcast_if_changed()
        return

    if s.is_deferred:
        await _tick_deferred(s, now, delta)
        return

    current = _read_current_fingerprint()

    # First readable tick establishes what "on target" means.
    if s.baseline is None:
        if current is not None:
            s.baseline = current
            s.on_target_seconds += delta
        await _broadcast_if_changed()
        return

    # Unreadable window (platform can't say) — credit the time rather
    # than manufacturing a drift from a failed read.
    if current is None:
        s.on_target_seconds += delta
        await _broadcast_if_changed()
        return

    on_target = (current == s.baseline)

    if on_target:
        s.on_target_seconds += delta
        s._pending_since = None
        s._pending_fp = None
        if s.drifting:
            # Came back.
            s.drifting = False
            s._drift_started_at = None
            s._drift_nag_count = 0
            line = _pick(_RETURN_LINES, s._last_line)
            s._last_line = line
            _speak(line)
    else:
        # Off-target: only counts once it's outlasted the grace period.
        if s._pending_fp != current:
            s._pending_fp = current
            s._pending_since = now
        elapsed_ms = (now - (s._pending_since or now)) * 1000
        if not s.drifting and elapsed_ms >= DRIFT_GRACE_MS:
            s.drifting = True
            s.drift_count += 1
            s._drift_started_at = now
            s._last_nag_at = now
            s._drift_nag_count = 1
            line = _pick_drift_line(s._drift_nag_count, s._last_line)
            s._last_line = line
            _speak(line)
        elif s.drifting and (now - s._last_nag_at) >= s.nag_interval_s:
            s._last_nag_at = now
            s._drift_nag_count += 1
            line = _pick_drift_line(s._drift_nag_count, s._last_line)
            s._last_line = line
            _speak(line)

    if s.expired():
        await _finish("completed")
        return

    await _broadcast_if_changed()


async def _tick_deferred(s: "FocusSession", now: float, delta: float) -> None:
    """One tick of the deferred-lock flow: FRIDAY's own window was
    frontmost when the session was requested, so nothing has been
    locked yet. Watches for the first window that stays frontmost for
    DEFER_LOCK_TICKS consecutive ticks and locks onto that; gives up
    with no lock at all if FRIDAY's window is still frontmost after
    DEFER_TIMEOUT_S — a wrong lock is worse than a late one."""
    s.deferred_seconds += delta

    # Lazily established on the first deferred tick (same pattern as
    # `baseline` for the normal path) rather than stamped in start() —
    # keeps the timeout anchored to the same clock every tick uses.
    if s._defer_started_at is None:
        s._defer_started_at = now

    fp, is_friday = _read_frontmost()

    if is_friday or fp is None:
        # Still looking at FRIDAY (or an unreadable window) — no
        # candidate to build a streak on yet.
        s._defer_candidate_fp = None
        s._defer_candidate_ticks = 0
    else:
        if s._defer_candidate_fp == fp:
            s._defer_candidate_ticks += 1
        else:
            s._defer_candidate_fp = fp
            s._defer_candidate_ticks = 1

        if s._defer_candidate_ticks >= DEFER_LOCK_TICKS:
            s.is_deferred = False
            s.awaiting_label_reply = False
            s.baseline = fp
            s.on_target_seconds += delta  # this tick is itself on-target
            s._defer_started_at = None
            s._defer_candidate_fp = None
            s._defer_candidate_ticks = 0
            line = f"Locked on. Focusing on {s.label}." if s.label else "Locked on."
            _speak(line)
            await _broadcast_if_changed(force=True)
            return

    if s._defer_started_at is not None and (now - s._defer_started_at) >= DEFER_TIMEOUT_S:
        await _finish("deferred_timeout")
        return

    await _broadcast_if_changed()


async def _focus_loop() -> None:
    """Persistent background task, one-second tick. Registered at
    startup and runs independent of any conversational turn — same
    shape as start.py's _proactive_monitor, just watching windows
    instead of resource usage."""
    while True:
        try:
            await asyncio.sleep(1)
            await tick()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[Focus] tick error: {e}")


def start_focus_loop() -> asyncio.Task:
    """Called once from start.py, next to _proactive_monitor."""
    return asyncio.create_task(_focus_loop())


# ── session record / streak ───────────────────────────────────────────

def _record_session(s: FocusSession, outcome: str) -> dict:
    """Writes the end-of-session ledger entry + updates the streak.

    Stores counters only — minutes, drift count, outcome. No window
    identity of any kind reaches this. Best-effort: a memory failure
    must not break finishing a session, so the summary is still
    returned either way.
    """
    elapsed_min = int(round(s.elapsed_seconds() / 60))
    on_target_min = int(round(s.on_target_seconds / 60))
    planned_min = int(round(s.planned_seconds / 60))
    ratio = s.on_target_ratio()
    clean = ratio >= CLEAN_SESSION_RATIO and outcome == "completed"

    streak = 0
    try:
        from memory.long_term import get_all, remember
        notes = (get_all() or {}).get("notes", {}) or {}

        prev_raw = notes.get(_STREAK_KEY)
        prev = 0
        if isinstance(prev_raw, dict):
            prev_raw = prev_raw.get("value", "")
        try:
            prev = int(str(prev_raw).strip() or 0)
        except (TypeError, ValueError):
            prev = 0

        streak = prev + 1 if clean else 0
        remember(_STREAK_KEY, str(streak), category="notes")

        stamp = time.strftime("%Y%m%d_%H%M", time.localtime(s.started_at))
        remember(
            f"{_LEDGER_KEY_PREFIX}{stamp}",
            f"{on_target_min}/{planned_min} min on target, "
            f"{s.drift_count} drifts, {outcome}",
            category="notes",
        )
    except Exception as e:
        logger.debug(f"[Focus] could not record session: {e}")

    return {
        "outcome": outcome,
        "planned_minutes": planned_min,
        "elapsed_minutes": elapsed_min,
        "on_target_minutes": on_target_min,
        "on_target_percent": int(round(ratio * 100)),
        "drift_count": s.drift_count,
        "clean": clean,
        "streak": streak,
    }


def _summary_line(r: dict) -> str:
    bits = [
        f"Session done, boss. {r['on_target_minutes']} of {r['planned_minutes']} minutes on target "
        f"({r['on_target_percent']} percent)."
    ]
    if r["drift_count"] == 0:
        bits.append("No drifts at all.")
    elif r["drift_count"] == 1:
        bits.append("One drift.")
    else:
        bits.append(f"{r['drift_count']} drifts.")
    if r["clean"] and r["streak"] > 1:
        bits.append(f"That's {r['streak']} clean sessions in a row.")
    elif r["clean"]:
        bits.append("Clean session.")
    return " ".join(bits)


async def _finish(outcome: str) -> dict:
    global _session
    s = _session
    if s is None:
        return {}
    s.active = False
    if outcome == "deferred_timeout":
        # Nothing was ever locked on — this is a session that never
        # actually started, not one that went badly. No ledger entry,
        # no streak effect either way.
        report = {
            "outcome": outcome,
            "planned_minutes": int(round(s.planned_seconds / 60)),
            "elapsed_minutes": 0, "on_target_minutes": 0,
            "on_target_percent": 0, "drift_count": 0,
            "clean": False, "streak": 0,
        }
    else:
        report = _record_session(s, outcome)
    _session = None
    await _broadcast_if_changed(force=True)
    if outcome == "completed":
        _speak(_summary_line(report))
    elif outcome == "deferred_timeout":
        _speak("Didn't see you switch away, boss — I've let that focus session go. "
               "Just say the word when you're ready to try again.")
    return report


# ── public control surface (used by the focus_session tool) ───────────

async def start(minutes: int = 25, label: str = "") -> str:
    global _session, _last_broadcast_state
    if _session is not None and _session.active:
        await _finish("replaced")
    _session = FocusSession(minutes=minutes, label=label)
    _last_broadcast_state = None

    _, is_friday = _read_frontmost()
    if is_friday:
        # The command came from FRIDAY's own UI — locking "wherever the
        # frontmost window is" right now would lock onto FRIDAY herself.
        # Defer instead of guessing: wait for the user to actually go to
        # their work (see _tick_deferred).
        s = _session
        s.is_deferred = True
        s.awaiting_label_reply = True
        await _broadcast_if_changed(force=True)
        return "Go to what you're working on and I'll lock on there. What are we focusing on?"

    await _broadcast_if_changed(force=True)
    what = f" on {_session.label}" if _session.label else ""
    return (f"Locked in for {int(round(_session.planned_seconds / 60))} minutes{what}. "
            f"I'll call it out if you wander off.")


async def pause() -> str:
    s = _session
    if s is None or not s.active:
        return "No focus session running, boss."
    if s.paused:
        return "Already paused."
    s.paused = True
    await _broadcast_if_changed()
    return "Focus session paused."


async def resume() -> str:
    s = _session
    if s is None or not s.active:
        return "No focus session running, boss."
    if not s.paused:
        return "Already running."
    s.paused = False
    # Re-baseline on resume: wherever you are when you come back is the
    # new "on target", since you may well have deliberately moved.
    s.baseline = None
    await _broadcast_if_changed()
    return "Back on. Focus session resumed."


async def extend(minutes: int = 10) -> str:
    s = _session
    if s is None or not s.active:
        return "No focus session running, boss."
    add = max(1, int(minutes))
    s.planned_seconds += add * 60
    await _broadcast_if_changed()
    return f"Extended by {add} minutes. {int(round(s.remaining_seconds() / 60))} to go."


async def abort() -> str:
    s = _session
    if s is None or not s.active:
        return "No focus session running, boss."
    report = await _finish("aborted")
    return (f"Focus session cancelled. {report['on_target_minutes']} of "
            f"{report['planned_minutes']} minutes on target, {report['drift_count']} drifts.")


async def set_nag_interval(seconds: int) -> str:
    s = _session
    if s is None or not s.active:
        return "No focus session running, boss."
    s.nag_interval_s = max(5, int(seconds))
    await _broadcast_if_changed()
    return f"I'll remind you every {s.nag_interval_s} seconds while you're off target."


def _read_frontmost_behind_card() -> tuple:
    """(fingerprint, is_friday) for the topmost window that ISN'T one of
    FRIDAY's own — used when a retarget is tagged as coming from the
    card. Same privacy boundary as _read_frontmost(): title and process
    exist as locals and are discarded here."""
    try:
        from brain.handlers import _get_frontmost_excluding
        title, proc = _get_frontmost_excluding(_FRIDAY_PROCESS_NAMES)
    except Exception as e:
        logger.debug(f"[Focus] behind-card window read failed: {e}")
        return None, False
    if not title and not proc:
        return None, False
    return _fingerprint(title, proc), _is_friday_process(proc)


async def retarget(source: str = "voice") -> str:
    """Changes the lock target to whatever's frontmost right now,
    without aborting the session — for "lock on this" / "keep me here"
    / "this is the tab" and natural variants (see brain/router.py's
    focus_retarget intent and start.py's dispatch of it).

    Locks silently: unlike a drift callout, this doesn't go through
    _speak() — the confirmation is just the normal tool-response speech
    every other focus_session action already uses. And it forgives
    whatever drift was already accumulating toward the OLD target: the
    user is deliberately choosing a new one, so a pending or already-
    counted drift against the old target no longer applies.
    """
    s = _session
    if s is None or not s.active:
        return "No focus session running, boss."

    # Forgive in-progress drift against whatever the target used to be —
    # applies whether we're about to lock immediately or re-defer.
    if s.drifting:
        s.drift_count = max(0, s.drift_count - 1)
    s.drifting = False
    s._pending_since = None
    s._pending_fp = None
    s._drift_started_at = None
    s._drift_nag_count = 0

    # THE CARD TRAP: clicking a button on the always-on-top card makes
    # the card's own window frontmost, so "lock whatever's frontmost"
    # would lock the card. The card can't be told apart by process name
    # (it's a second BrowserWindow in the same Electron process), so it
    # tags its own requests instead, and those read past FRIDAY's
    # windows to the real work window behind them. Same principle as
    # the deferred-lock flow: don't lock the thing that's asking.
    if source == "card":
        fp, is_friday = _read_frontmost_behind_card()
        if fp is not None and not is_friday:
            s.baseline = fp
            s.is_deferred = False
            s.awaiting_label_reply = False
            await _broadcast_if_changed(force=True)
            return f"Locked on. Focusing on {s.label}." if s.label else "Locked on."
        # Couldn't find a real window behind the card — fall through to
        # the deferred flow rather than locking something wrong.
        s.is_deferred = True
        s.awaiting_label_reply = True
        s.baseline = None
        s._defer_started_at = None
        s._defer_candidate_fp = None
        s._defer_candidate_ticks = 0
        await _broadcast_if_changed(force=True)
        return "Go to what you're working on and I'll lock on there. What are we focusing on?"

    _, is_friday = _read_frontmost()
    if is_friday:
        # Re-targeting from FRIDAY's own window doesn't mean "lock onto
        # FRIDAY" — re-arm the same deferred-lock flow start() uses, for
        # the same reason: locking "wherever the frontmost window is"
        # right now would lock FRIDAY herself.
        s.is_deferred = True
        s.awaiting_label_reply = True
        s.baseline = None
        s._defer_started_at = None
        s._defer_candidate_fp = None
        s._defer_candidate_ticks = 0
        await _broadcast_if_changed(force=True)
        return "Go to what you're working on and I'll lock on there. What are we focusing on?"

    fp = _read_current_fingerprint()
    if fp is None:
        return "Couldn't read the current window, boss — try again in a second."

    s.baseline = fp
    s.is_deferred = False
    s.awaiting_label_reply = False
    await _broadcast_if_changed(force=True)
    return f"Locked on. Focusing on {s.label}." if s.label else "Locked on."


async def focus_session(args: dict) -> str:
    """Tool entry point — dispatched from brain/llm.py. Also the target
    of start.py's router-level focus_retarget fast path (see
    brain/router.py) for retarget specifically, so both routes run the
    exact same code, not a parallel implementation."""
    action = str(args.get("action", "")).strip().lower()
    try:
        minutes = int(args.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    label = str(args.get("label", "") or "")

    if action == "start":
        return await start(minutes=minutes or 25, label=label)
    if action == "pause":
        return await pause()
    if action == "resume":
        return await resume()
    if action == "extend":
        return await extend(minutes=minutes or 10)
    if action == "abort":
        return await abort()
    if action == "retarget":
        return await retarget(source=str(args.get("source", "voice") or "voice"))
    if action == "set_nag_interval":
        return await set_nag_interval(seconds=minutes or DEFAULT_NAG_INTERVAL_S)
    return ("Unknown focus action, boss — try start, pause, resume, extend, "
            "retarget, abort, or set_nag_interval.")
