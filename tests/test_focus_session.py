"""
test_focus_session.py — regression tests for actions/focus_session.py.

Covers:
  - starting a session and establishing a baseline window
  - a window change past DRIFT_GRACE_MS records a drift AND fires a
    spoken callout
  - a window change that does NOT outlast the grace period is ignored
    (alt-tabbing for half a second isn't drifting)
  - returning to the original window clears the drifting flag
  - PRIVACY: nothing the client ever sees — focus_state() or any
    broadcast payload — contains a raw window title or process name.
    This is the paranoid one: it greps every broadcast payload for the
    actual app/title strings used in the test.
  - end-of-session report counts, and the streak only extends on a
    clean (85%+) session
  - starting a session from FRIDAY's own window defers instead of
    locking, says the go-to-your-work line, and exposes is_deferred
  - the deferred lock only fires after DEFER_LOCK_TICKS (2) consecutive
    ticks on the SAME new window — not the instant you switch, and not
    for a window that only held one tick before changing again
  - the deferred timeout gives up (no lock) if FRIDAY's window never
    leaves frontmost
  - the "what are we focusing on" voice reply is consumed exactly once
    as the session label
  - retarget(): from a work window it locks silently (no _speak()
    callout) and forgives whatever drift was already accumulating
    against the old target; from FRIDAY's own window it re-arms the
    deferred flow instead of locking FRIDAY

Window reading is faked by monkeypatching _read_frontmost, so these run
identically on any OS with no real windows involved.

Run from the project root:
    python tests/test_focus_session.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import focus_session as fs   # noqa: E402
from memory import long_term as lt        # noqa: E402

results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


# Realistic-looking identities. The privacy test greps broadcast
# payloads for these exact strings, so they must be distinctive.
WORK_TITLE, WORK_PROC = "thesis_chapter_3.docx - Word", "WINWORD.EXE"
DISTRACT_TITLE, DISTRACT_PROC = "Instagram - Google Chrome", "chrome.exe"
FRIDAY_TITLE, FRIDAY_PROC = "F.R.I.D.A.Y.", "electron.exe"

SECRET_STRINGS = [
    WORK_TITLE, WORK_PROC, DISTRACT_TITLE, DISTRACT_PROC,
    "Instagram", "Chrome", "chrome", "WINWORD", "Word", "thesis",
    FRIDAY_PROC, "electron",
]


class Harness:
    """Swaps out the window reader, speak, and broadcast so a session can
    be driven deterministically with no real windows, audio, or sockets.

    Patches brain.handlers._get_frontmost_window directly — the actual
    source both fs._read_frontmost() (fingerprint/is_friday, called
    every tick) AND fs._read_drift_label() (the callout label, called
    only at speak-time) read from. Patching at the source means both
    stay consistent automatically; patching fs._read_frontmost alone
    would leave the label path reading real, unfaked windows.
    """

    def __init__(self):
        self.spoken = []
        self.broadcasts = []
        self.current = (WORK_TITLE, WORK_PROC)
        import brain.handlers as handlers_mod
        self._handlers_mod = handlers_mod
        self._orig_get_frontmost = handlers_mod._get_frontmost_window

    def __enter__(self):
        self._handlers_mod._get_frontmost_window = lambda: self.current
        fs.configure(speak_fn=self.spoken.append, broadcast_fn=self._broadcast)
        fs._session = None
        fs._last_broadcast_state = None
        return self

    def __exit__(self, *exc):
        self._handlers_mod._get_frontmost_window = self._orig_get_frontmost
        fs.configure(speak_fn=None, broadcast_fn=None)
        fs._session = None
        fs._last_broadcast_state = None

    def _broadcast(self, payload):
        self.broadcasts.append(payload)

    def switch_to(self, title, proc):
        self.current = (title, proc)

    def switch_to_friday(self):
        self.current = (FRIDAY_TITLE, FRIDAY_PROC)


def _is_drift_tier_line(text: str, tier_idx: int = None) -> bool:
    """True if `text` matches one of the {label}-templated tier lines
    with SOME label filled in — used to confirm a callout is a real
    canned line (not LLM output) without hardcoding which label it
    used. Checked against a specific tier (0/1/2) if given, else any."""
    import re as _re
    tiers = fs._DRIFT_TIERS if tier_idx is None else [fs._DRIFT_TIERS[tier_idx]]
    for tier in tiers:
        for template in tier:
            pattern = "^" + _re.escape(template).replace(r"\{label\}", ".+") + "$"
            if _re.match(pattern, text):
                return True
    return False


def _reset_store():
    """Point long-term memory at a throwaway vault so session ledger
    writes don't touch the real one."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="friday_focustest_"))
    lt.VAULT_PATH = tmp_dir / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp_dir / "lt_faiss.index"
    lt.META_PATH = tmp_dir / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp_dir / "long_term.json"
    return tmp_dir


# ── tests ─────────────────────────────────────────────────────────────

async def test_start_establishes_baseline():
    _reset_store()
    with Harness() as h:
        await fs.start(minutes=25, label="thesis")
        state = fs.focus_state()
        record("start() marks the session active", state["active"] is True, state)
        record("planned minutes recorded", state["planned_minutes"] == 25, state)

        t = 1000.0
        await fs.tick(now=t)
        record("first tick establishes a baseline", fs._session.baseline is not None)
        record("no drift on the starting window", fs.focus_state()["drift_count"] == 0)


async def test_drift_past_grace_records_and_calls_out():
    _reset_store()
    with Harness() as h:
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)                      # baseline = work window
        h.spoken.clear()

        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)
        t += 1
        await fs.tick(now=t)                      # first sight of new window
        drift_during_grace = fs.focus_state()["drift_count"]
        record("no drift recorded during the grace period",
               drift_during_grace == 0 and not h.spoken, (drift_during_grace, h.spoken))

        # Past DRIFT_GRACE_MS (800ms) since the new window first appeared.
        t += 1
        await fs.tick(now=t)
        state = fs.focus_state()
        record("drift recorded once past the grace period", state["drift_count"] == 1, state)
        record("drifting flag set", state["drifting"] is True, state)
        record("a spoken callout fired", len(h.spoken) == 1, h.spoken)
        record("the callout is a canned tier-1 line with a label filled in, not LLM output",
               h.spoken and _is_drift_tier_line(h.spoken[0], tier_idx=0), h.spoken)


async def test_brief_switch_within_grace_is_not_a_drift():
    _reset_store()
    with Harness() as h:
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)
        h.spoken.clear()

        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)
        t += 0.3
        await fs.tick(now=t)
        h.switch_to(WORK_TITLE, WORK_PROC)        # back before grace elapsed
        t += 0.3
        await fs.tick(now=t)

        state = fs.focus_state()
        record("a sub-grace-period switch never counts as a drift",
               state["drift_count"] == 0 and state["drifting"] is False, state)
        record("...and stays silent", not h.spoken, h.spoken)


async def test_returning_clears_the_drift():
    _reset_store()
    with Harness() as h:
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)

        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)
        record("drifting before returning", fs.focus_state()["drifting"] is True)

        h.spoken.clear()
        h.switch_to(WORK_TITLE, WORK_PROC)
        t += 1
        await fs.tick(now=t)

        state = fs.focus_state()
        record("returning clears the drifting flag", state["drifting"] is False, state)
        record("the drift still counts in the tally (it happened)",
               state["drift_count"] == 1, state)
        record("a return line is spoken",
               h.spoken and h.spoken[0] in fs._RETURN_LINES, h.spoken)


async def test_client_state_never_contains_window_identity():
    """The paranoid one. Drives a session through several window changes,
    then greps BOTH focus_state() and every broadcast payload for any
    trace of the real app/title strings."""
    _reset_store()
    with Harness() as h:
        await fs.start(minutes=25, label="thesis")
        t = 1000.0
        await fs.tick(now=t)
        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)
        for _ in range(3):
            t += 1
            await fs.tick(now=t)
        h.switch_to(WORK_TITLE, WORK_PROC)
        t += 1
        await fs.tick(now=t)

        state = fs.focus_state()
        payloads = h.broadcasts + [state]
        blob = repr(payloads)

        # "thesis" is the user's own label for the session, which they
        # supplied and which IS meant to come back — it is not an
        # observed window identity. Everything else must be absent.
        leaked = [s for s in SECRET_STRINGS
                  if s.lower() != "thesis" and s.lower() in blob.lower()]
        record("no window title or process name in any client-visible payload",
               not leaked, f"leaked: {leaked}")

        record("broadcasts actually happened (test would be vacuous otherwise)",
               len(h.broadcasts) > 0, len(h.broadcasts))

        # And the whitelist itself is exactly what we expect — a new
        # internal field can't quietly ride along into the payload.
        expected_keys = {
            "active", "paused", "drifting", "is_deferred", "label", "planned_minutes",
            "elapsed_minutes", "remaining_minutes", "on_target_minutes",
            "on_target_percent", "drift_count", "nag_interval_seconds",
            # Posture watch — booleans only, derived in the renderer from
            # frames that never left it. See test_posture_watch.py.
            "posture_monitoring", "posture_present", "posture_head_down",
            "posture_slouched", "posture_silenced",
            # Ambient screen watch — one more boolean, same shape.
            "screen_stuck",
        }
        record("focus_state() exposes exactly the whitelisted keys",
               set(state.keys()) == expected_keys,
               set(state.keys()) ^ expected_keys)

        # Every value must be a bool/int/str-label — no nested objects
        # that could smuggle something through.
        bad = {k: v for k, v in state.items()
               if not isinstance(v, (bool, int, str))}
        record("all client-visible values are plain booleans/counters/label",
               not bad, bad)

        # The fingerprint is one-way even internally.
        fp = fs._fingerprint(DISTRACT_TITLE, DISTRACT_PROC)
        record("fingerprint doesn't contain the raw identity",
               "instagram" not in fp.lower() and "chrome" not in fp.lower(), fp)


async def test_abort_reports_counters_and_clears_session():
    _reset_store()
    with Harness() as h:
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)
        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)

        msg = await fs.abort()
        record("abort() returns a summary mentioning the drift", "1 drift" in msg, msg)
        record("session is cleared after abort", fs.focus_state()["active"] is False)


async def test_streak_only_extends_on_a_clean_session():
    _reset_store()
    with Harness() as h:
        # Clean: never leaves the baseline window.
        await fs.start(minutes=1)
        s = fs._session
        s.on_target_seconds = 60.0
        s.started_at -= 60
        s._last_tick -= 60
        report = await fs._finish("completed")
        record("a 100%-on-target session is clean", report["clean"] is True, report)
        record("streak starts at 1", report["streak"] == 1, report)

        # Dirty: mostly off-target.
        await fs.start(minutes=1)
        s = fs._session
        s.on_target_seconds = 10.0
        s.drift_count = 4
        s.started_at -= 60
        s._last_tick -= 60
        report2 = await fs._finish("completed")
        record("a mostly-off-target session is not clean", report2["clean"] is False, report2)
        record("streak resets to 0 after a dirty session", report2["streak"] == 0, report2)


# ── deferred-lock tests (starting from FRIDAY's own window) ────────────

async def test_starting_from_fridays_window_defers_instead_of_locking():
    _reset_store()
    with Harness() as h:
        h.switch_to_friday()
        msg = await fs.start(minutes=25, label="thesis")

        record("start() returns the go-to-your-work line instead of locking",
               "go to what you're working on" in msg.lower(), msg)
        record("...and asks what they're focusing on", "focusing on" in msg.lower(), msg)

        state = fs.focus_state()
        record("is_deferred is True immediately after a from-FRIDAY start",
               state["is_deferred"] is True, state)
        record("session is active but has no baseline yet",
               state["active"] is True and fs._session.baseline is None, state)

        t = 1000.0
        await fs.tick(now=t)
        record("still deferred while FRIDAY's window stays frontmost",
               fs.focus_state()["is_deferred"] is True)
        record("no drift/callout while merely deferred", not h.spoken and fs.focus_state()["drift_count"] == 0)


async def test_locks_on_only_after_two_consecutive_ticks_on_the_new_window():
    """The exact scenario in the prompt: start deferred, switch away,
    confirm it does NOT lock the instant the switch happens — only
    after the second consecutive tick on that window — and confirm the
    'Locked on' callout fires then, not before."""
    _reset_store()
    with Harness() as h:
        h.switch_to_friday()
        await fs.start(minutes=25, label="thesis")
        t = 1000.0
        await fs.tick(now=t)                       # still on FRIDAY
        h.spoken.clear()

        h.switch_to(WORK_TITLE, WORK_PROC)          # user switches to their work
        t += 1
        await fs.tick(now=t)                        # tick #1 on the new window
        state_after_first_tick = fs.focus_state()
        record("NOT locked after only one tick on the new window",
               state_after_first_tick["is_deferred"] is True, state_after_first_tick)
        record("no 'locked on' callout yet", not h.spoken, h.spoken)

        t += 1
        await fs.tick(now=t)                        # tick #2 — this is DEFER_LOCK_TICKS
        state_after_second_tick = fs.focus_state()
        record("locked after the second consecutive tick on the new window",
               state_after_second_tick["is_deferred"] is False, state_after_second_tick)
        record("a 'Locked on' callout fires now",
               len(h.spoken) == 1 and "locked on" in h.spoken[0].lower(), h.spoken)
        record("label is preserved through the lock", state_after_second_tick["label"] == "thesis")

        # And it's now tracking that window as the real baseline —
        # drift detection works normally from here on.
        h.spoken.clear()
        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)
        record("normal drift detection works after locking on",
               fs.focus_state()["drift_count"] == 1, fs.focus_state())


async def test_flickering_candidates_dont_lock_early():
    """Two DIFFERENT non-FRIDAY windows, one tick each, should not lock —
    only the SAME window held for two consecutive ticks counts."""
    _reset_store()
    with Harness() as h:
        h.switch_to_friday()
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)

        h.switch_to(WORK_TITLE, WORK_PROC)
        t += 1
        await fs.tick(now=t)
        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)   # different window — candidate streak resets
        t += 1
        await fs.tick(now=t)

        record("a different candidate window resets the streak, doesn't lock",
               fs.focus_state()["is_deferred"] is True, fs.focus_state())


async def test_deferred_timeout_gives_up_without_locking():
    _reset_store()
    with Harness() as h:
        h.switch_to_friday()
        await fs.start(minutes=25)
        t = 1000.0
        # FRIDAY's window never leaves frontmost for DEFER_TIMEOUT_S.
        await fs.tick(now=t)
        t += fs.DEFER_TIMEOUT_S + 1
        await fs.tick(now=t)

        state = fs.focus_state()
        record("session is no longer active after the deferred timeout",
               state["active"] is False, state)
        record("timeout speaks a distinct line (not the 'Locked on' one)",
               h.spoken and "locked on" not in h.spoken[-1].lower(), h.spoken)


async def test_voice_reply_is_consumed_as_the_label_and_unblocks_wake_word():
    _reset_store()
    with Harness() as h:
        h.switch_to_friday()
        await fs.start(minutes=25)

        record("awaiting a voice reply right after a deferred start",
               fs.is_awaiting_voice_reply() is True)

        ack = fs.consume_voice_reply("my thesis chapter")
        record("consume_voice_reply returns an acknowledgment, not None",
               ack is not None, ack)
        record("the reply becomes the session label",
               fs._session.label == "my thesis chapter", fs._session.label)
        record("no longer awaiting a reply after it's consumed",
               fs.is_awaiting_voice_reply() is False)

        # A second, unrelated utterance should NOT be swallowed as
        # another label answer.
        second = fs.consume_voice_reply("something unrelated")
        record("a second utterance is not intercepted (falls through to normal handling)",
               second is None, second)


async def test_no_deferral_when_not_starting_from_fridays_window():
    """Baseline check: starting from an ordinary window behaves exactly
    like Prompt 04 — no deferral, immediate lock."""
    _reset_store()
    with Harness() as h:
        h.switch_to(WORK_TITLE, WORK_PROC)
        msg = await fs.start(minutes=25)
        record("normal start() still locks immediately (unchanged Prompt 04 behavior)",
               "locked in" in msg.lower(), msg)
        record("is_deferred is False", fs.focus_state()["is_deferred"] is False)


# ── retarget (Prompt 06: change the lock target mid-session) ──────────

async def test_retarget_from_a_work_window_locks_silently_and_forgives_drift():
    """The exact scenario in the prompt: say 'lock on this' from a work
    window. Confirms it locks onto the new window, does NOT go through
    the _speak() callout path (locks silently), and forgives whatever
    drift was already accumulating against the old target."""
    _reset_store()
    with Harness() as h:
        h.switch_to(WORK_TITLE, WORK_PROC)
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)                        # baseline = work window

        # Drift toward a third window, past the grace period, so it's
        # already been counted and announced by the time retarget() is
        # called.
        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)
        state_before = fs.focus_state()
        record("sanity: a drift is in progress before retargeting",
               state_before["drifting"] is True and state_before["drift_count"] == 1, state_before)
        h.spoken.clear()

        # Now say "lock on this" — from wherever the user actually is
        # right now (the distraction window, in this case — doesn't
        # matter which; retarget locks onto whatever's frontmost).
        msg = await fs.retarget()

        record("retarget() returns a 'Locked on' confirmation", "locked on" in msg.lower(), msg)
        record("retarget() does NOT go through the _speak() callout path (locks silently)",
               h.spoken == [], h.spoken)

        state_after = fs.focus_state()
        record("the drift that was in progress is forgiven — drifting clears",
               state_after["drifting"] is False, state_after)
        record("the already-counted drift is un-counted too",
               state_after["drift_count"] == 0, state_after)
        record("is_deferred stays False (immediate lock, not deferred)",
               state_after["is_deferred"] is False, state_after)

        # And the new target is now genuinely tracked as "on target" —
        # not just cosmetically cleared.
        h.spoken.clear()
        t += 1
        await fs.tick(now=t)
        record("the new window is now the tracked baseline (no drift on it)",
               fs.focus_state()["drift_count"] == 0 and not h.spoken, fs.focus_state())


async def test_retarget_from_fridays_window_rearms_deferred_flow():
    """The other half of the prompt's scenario: say 'lock on this' while
    looking at FRIDAY's own window. Must NOT lock onto FRIDAY — it
    should re-arm the same deferred flow start() uses."""
    _reset_store()
    with Harness() as h:
        h.switch_to(WORK_TITLE, WORK_PROC)
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)

        h.switch_to_friday()
        msg = await fs.retarget()

        record("retargeting from FRIDAY's own window re-arms deferral instead of locking",
               "go to what you're working on" in msg.lower(), msg)
        state = fs.focus_state()
        record("is_deferred is True after retargeting from FRIDAY's window",
               state["is_deferred"] is True, state)
        record("baseline is cleared, not set to FRIDAY's own window",
               fs._session.baseline is None, fs._session.baseline)
        record("awaiting the voice reply again, same as a fresh deferred start",
               fs.is_awaiting_voice_reply() is True)

        # And it locks on for real once the user actually switches away,
        # same two-tick rule as a fresh deferred start.
        h.switch_to(WORK_TITLE, WORK_PROC)
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)
        record("locks on again once the user switches away, same as a fresh deferred start",
               fs.focus_state()["is_deferred"] is False, fs.focus_state())


async def test_retarget_with_no_active_session():
    _reset_store()
    with Harness() as h:
        msg = await fs.retarget()
        record("retarget() with no session running says so rather than erroring",
               "no focus session" in msg.lower(), msg)


# ── drift label derivation (mapped site / unmapped app) ────────────────

async def test_drift_callout_names_a_mapped_site():
    _reset_store()
    with Harness() as h:
        h.switch_to(WORK_TITLE, WORK_PROC)
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)

        h.switch_to("Instagram - Google Chrome", "chrome.exe")
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)

        record("a mapped site produces a callout naming it by its short label",
               h.spoken and "Instagram" in h.spoken[0], h.spoken)
        record("the callout is a real tier-1 canned line, not LLM output",
               h.spoken and _is_drift_tier_line(h.spoken[0], tier_idx=0), h.spoken)
        record("the raw title never appears verbatim in the callout",
               h.spoken and "Google Chrome" not in h.spoken[0], h.spoken)


async def test_drift_callout_names_an_unmapped_apps_process_name():
    """No site-map entry matches this title, so it should fall back to
    the app's own (short, cleaned-up) display name — never the raw
    title, never the raw .exe string."""
    _reset_store()
    with Harness() as h:
        h.switch_to(WORK_TITLE, WORK_PROC)
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)

        h.switch_to("General - Company Workspace - Slack", "slack.exe")
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)

        record("an unmapped app falls back to its cleaned display name",
               h.spoken and "Slack" in h.spoken[0], h.spoken)
        record("still a real tier-1 canned line", h.spoken and _is_drift_tier_line(h.spoken[0], tier_idx=0), h.spoken)
        record("never the raw .exe process name",
               h.spoken and "slack.exe" not in h.spoken[0].lower(), h.spoken)
        record("never the raw window title",
               h.spoken and "Company Workspace" not in h.spoken[0], h.spoken)


async def test_drift_escalates_across_four_line_tiers():
    """Confirms the escalation actually escalates: tier 1 on first
    callout, tier 2 after one nag interval, tier 3 after two — and
    stays at tier 3 (doesn't index out of range) for further nags."""
    _reset_store()
    with Harness() as h:
        h.switch_to(WORK_TITLE, WORK_PROC)
        await fs.start(minutes=25)
        fs._session.nag_interval_s = 5  # fast-forward nags for the test
        t = 1000.0
        await fs.tick(now=t)

        h.switch_to(DISTRACT_TITLE, DISTRACT_PROC)
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)   # 1st callout -> tier 1
        record("each tier has exactly 4 lines (not 3)",
               all(len(tier) == 4 for tier in fs._DRIFT_TIERS), [len(t) for t in fs._DRIFT_TIERS])
        record("first callout is tier 1", _is_drift_tier_line(h.spoken[-1], tier_idx=0), h.spoken)

        t += 6
        await fs.tick(now=t)   # 2nd callout -> tier 2
        record("second callout (after one nag interval) escalates to tier 2",
               _is_drift_tier_line(h.spoken[-1], tier_idx=1), h.spoken)

        t += 6
        await fs.tick(now=t)   # 3rd callout -> tier 3
        record("third callout escalates to tier 3",
               _is_drift_tier_line(h.spoken[-1], tier_idx=2), h.spoken)

        t += 6
        await fs.tick(now=t)   # 4th callout -> stays at tier 3, no crash
        record("further callouts stay at tier 3 rather than erroring",
               _is_drift_tier_line(h.spoken[-1], tier_idx=2), h.spoken)


async def main():
    await test_start_establishes_baseline()
    await test_drift_past_grace_records_and_calls_out()
    await test_brief_switch_within_grace_is_not_a_drift()
    await test_returning_clears_the_drift()
    await test_client_state_never_contains_window_identity()
    await test_abort_reports_counters_and_clears_session()
    await test_streak_only_extends_on_a_clean_session()
    await test_starting_from_fridays_window_defers_instead_of_locking()
    await test_locks_on_only_after_two_consecutive_ticks_on_the_new_window()
    await test_flickering_candidates_dont_lock_early()
    await test_deferred_timeout_gives_up_without_locking()
    await test_voice_reply_is_consumed_as_the_label_and_unblocks_wake_word()
    await test_no_deferral_when_not_starting_from_fridays_window()
    await test_retarget_from_a_work_window_locks_silently_and_forgives_drift()
    await test_retarget_from_fridays_window_rearms_deferred_flow()
    await test_retarget_with_no_active_session()
    await test_drift_callout_names_a_mapped_site()
    await test_drift_callout_names_an_unmapped_apps_process_name()
    await test_drift_escalates_across_four_line_tiers()

    print()
    print("=== SUMMARY ===")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
