"""
test_posture_watch.py — regression tests for the posture watch.

Covers the backend half (actions/focus_session.py's update_posture /
silence_posture) plus a static audit of the renderer half that no
Python test could otherwise reach.

  - sustained bad posture fires exactly one nudge, after
    POSTURE_BAD_SUSTAIN_MS, not before
  - after a nudge it stays quiet for POSTURE_NUDGE_COOLDOWN_S no matter
    how bad posture stays, then may nudge again
  - a brief slouch that resolves before the sustain threshold says
    nothing at all
  - absence gets the much longer POSTURE_ABSENCE_GRACE_S, and is called
    at most once per continuous absence
  - the relief valve silences posture nudges for RELIEF_VALVE_S, and
    does NOT silence drift callouts (a different thing the user asked
    for)
  - PRIVACY: update_posture's signature accepts booleans only, and
    nothing it produces in focus_state() is anything but a boolean
  - THE EAR LAW: a static audit of the renderer component + the WS
    command confirming no code path in this feature can enable the
    microphone

Run from the project root:
    python tests/test_posture_watch.py
"""

import asyncio
import inspect
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import focus_session as fs   # noqa: E402
from memory import long_term as lt        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


class Harness:
    def __init__(self):
        self.spoken = []
        self.broadcasts = []
        self.current = ("thesis.docx - Word", "WINWORD.EXE")
        import brain.handlers as handlers_mod
        self._handlers_mod = handlers_mod
        self._orig = handlers_mod._get_frontmost_window

    def __enter__(self):
        self._handlers_mod._get_frontmost_window = lambda: self.current
        fs.configure(speak_fn=self.spoken.append, broadcast_fn=self.broadcasts.append)
        fs._session = None
        fs._posture = fs.PostureWatch()
        fs._last_broadcast_state = None
        return self

    def __exit__(self, *exc):
        self._handlers_mod._get_frontmost_window = self._orig
        fs.configure(speak_fn=None, broadcast_fn=None)
        fs._session = None
        fs._posture = fs.PostureWatch()
        fs._last_broadcast_state = None

    def switch_to(self, title, proc):
        self.current = (title, proc)


def _reset_store():
    tmp = Path(tempfile.mkdtemp(prefix="friday_posturetest_"))
    lt.VAULT_PATH = tmp / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp / "lt_faiss.index"
    lt.META_PATH = tmp / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp / "long_term.json"


# ── nudge timing ──────────────────────────────────────────────────────

async def test_sustained_slouch_nudges_after_the_threshold():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        await fs.update_posture(present=True, slouched=True, now=t)
        record("nothing said on the first bad-posture sample", h.spoken == [], h.spoken)

        # Still inside the 700ms window.
        await fs.update_posture(present=True, slouched=True, now=t + 0.4)
        record("still silent before POSTURE_BAD_SUSTAIN_MS elapses", h.spoken == [], h.spoken)

        # Past it.
        await fs.update_posture(present=True, slouched=True, now=t + 0.8)
        record("nudges once past POSTURE_BAD_SUSTAIN_MS (~0.8s)", len(h.spoken) == 1, h.spoken)
        record("the nudge is a canned line, not LLM output",
               h.spoken and h.spoken[0] in fs._POSTURE_LINES, h.spoken)


async def test_nudge_then_cooldown_then_nudge_again():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        await fs.update_posture(present=True, slouched=True, now=t)
        await fs.update_posture(present=True, slouched=True, now=t + 0.8)
        record("first nudge fired", len(h.spoken) == 1, h.spoken)

        # Hammer it throughout the cooldown — must stay silent.
        for i in range(1, int(fs.POSTURE_NUDGE_COOLDOWN_S)):
            await fs.update_posture(present=True, slouched=True, now=t + 0.8 + i)
        record(f"silent for the whole {int(fs.POSTURE_NUDGE_COOLDOWN_S)}s cooldown despite constant bad posture",
               len(h.spoken) == 1, h.spoken)

        await fs.update_posture(present=True, slouched=True,
                                 now=t + 0.8 + fs.POSTURE_NUDGE_COOLDOWN_S + 0.1)
        record("may nudge again once the cooldown expires", len(h.spoken) == 2, h.spoken)


async def test_brief_slouch_says_nothing():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        await fs.update_posture(present=True, slouched=True, now=t)
        await fs.update_posture(present=True, slouched=True, now=t + 0.3)
        await fs.update_posture(present=True, slouched=False, now=t + 0.5)
        await fs.update_posture(present=True, slouched=True, now=t + 0.9)
        record("a slouch that resolves before the threshold is never mentioned",
               h.spoken == [], h.spoken)


async def test_head_down_also_counts_as_bad_posture():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        await fs.update_posture(present=True, head_down=True, now=t)
        await fs.update_posture(present=True, head_down=True, now=t + 0.8)
        record("head_down alone is enough to nudge", len(h.spoken) == 1, h.spoken)


# ── absence ───────────────────────────────────────────────────────────

async def test_absence_gets_the_longer_grace():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        await fs.update_posture(present=False, now=t)
        await fs.update_posture(present=False, now=t + 5)
        record("silent 5s into an absence (posture threshold would have fired long ago)",
               h.spoken == [], h.spoken)

        await fs.update_posture(present=False, now=t + fs.POSTURE_ABSENCE_GRACE_S + 0.5)
        record(f"speaks once past POSTURE_ABSENCE_GRACE_S ({int(fs.POSTURE_ABSENCE_GRACE_S)}s)",
               len(h.spoken) == 1, h.spoken)
        record("the absence line is canned", h.spoken and h.spoken[0] in fs._ABSENCE_LINES, h.spoken)

        await fs.update_posture(present=False, now=t + fs.POSTURE_ABSENCE_GRACE_S + 30)
        record("absence is mentioned at most once per continuous absence",
               len(h.spoken) == 1, h.spoken)


# ── relief valve ──────────────────────────────────────────────────────

async def test_relief_valve_silences_posture_nudges():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        await fs.silence_posture()
        record("relief valve reports back in minutes", fs.focus_state()["posture_silenced"] is True)

        # Hammer bad posture through the whole relief window.
        for i in range(0, int(fs.RELIEF_VALVE_S), 10):
            await fs.update_posture(present=True, slouched=True, head_down=True,
                                     now=t + i)
        record("no posture nudge at any point during the relief window",
               h.spoken == [], h.spoken)


async def test_relief_valve_expires():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        fs._posture.silence(seconds=fs.RELIEF_VALVE_S, now=t)
        await fs.update_posture(present=True, slouched=True, now=t + 10)
        record("silent inside the window", h.spoken == [], h.spoken)

        after = t + fs.RELIEF_VALVE_S + 1
        await fs.update_posture(present=True, slouched=True, now=after)
        await fs.update_posture(present=True, slouched=True, now=after + 0.8)
        record("nudges resume once the relief window expires", len(h.spoken) == 1, h.spoken)


async def test_relief_valve_does_not_silence_drift_callouts():
    """The relief valve is about posture nagging. Drift callouts are the
    thing the user explicitly asked FRIDAY to do; silencing those too
    would quietly gut the feature they turned on."""
    _reset_store()
    with Harness() as h:
        h.switch_to("thesis.docx - Word", "WINWORD.EXE")
        await fs.start(minutes=25)
        t = 1000.0
        await fs.tick(now=t)
        await fs.silence_posture()
        h.spoken.clear()

        h.switch_to("Instagram - Google Chrome", "chrome.exe")
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)
        record("drift callouts still fire while posture is silenced",
               len(h.spoken) == 1, h.spoken)
        record("...and it's a drift line, not a posture line",
               h.spoken and h.spoken[0] not in fs._POSTURE_LINES, h.spoken)


async def test_paused_session_suppresses_posture_nudges():
    _reset_store()
    with Harness() as h:
        await fs.start(minutes=25)
        await fs.pause()
        t = 1000.0
        await fs.update_posture(present=True, slouched=True, now=t)
        await fs.update_posture(present=True, slouched=True, now=t + 0.8)
        record("a paused session means no posture nudges either", h.spoken == [], h.spoken)


# ── privacy ───────────────────────────────────────────────────────────

async def test_posture_state_is_booleans_only():
    _reset_store()
    with Harness() as h:
        await fs.update_posture(present=True, head_down=True, slouched=True, now=1000.0)
        state = fs.focus_state()
        posture_keys = {k: v for k, v in state.items() if k.startswith("posture_")}
        record("focus_state exposes posture as booleans only",
               posture_keys and all(isinstance(v, bool) for v in posture_keys.values()),
               posture_keys)

        sig = inspect.signature(fs.update_posture)
        non_bool = [n for n, p in sig.parameters.items()
                    if n != "now" and not isinstance(p.default, bool)]
        record("update_posture's signature accepts nothing but booleans (plus the test clock)",
               not non_bool, non_bool)


# ── THE EAR LAW (static audit) ────────────────────────────────────────

def _strip_js_comments(src: str) -> str:
    """Removes // and /* */ comments. The audit below must check CODE,
    not prose: PostureWatch.jsx's header deliberately discusses
    start_listen and audioCapture to explain the ear law, and a naive
    substring search would flag that documentation as a violation —
    which would push whoever hits it toward deleting the explanation
    instead of keeping the rule."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    src = re.sub(r"(?m)(?<![:'\"])//.*$", "", src)
    return src


def test_ear_law_no_code_path_enables_the_microphone():
    """No Python test can observe the renderer, so this audits the
    source directly: the posture feature's files must contain nothing
    that starts, enables, or requests audio capture."""
    component = ROOT / "ui" / "renderer" / "components" / "PostureWatch.jsx"
    record("PostureWatch.jsx exists to audit", component.exists())
    if not component.exists():
        return
    src = _strip_js_comments(component.read_text(encoding="utf-8"))

    # The one command that sets _mic_event on the backend.
    record("PostureWatch.jsx never sends start_listen",
           "start_listen" not in src)

    # getUserMedia must explicitly refuse audio.
    calls = re.findall(r"getUserMedia\s*\(([^;]*?)\)\s*;", src, re.S)
    record("PostureWatch.jsx calls getUserMedia exactly once", len(calls) == 1, len(calls))
    if calls:
        record("...and that call passes audio: false",
               re.search(r"audio\s*:\s*false", calls[0]) is not None, calls[0][:200])
        record("...and never audio: true",
               re.search(r"audio\s*:\s*true", calls[0]) is None, calls[0][:200])

    for forbidden in ("webkitSpeechRecognition", "SpeechRecognition",
                      "MediaRecorder", "audioCapture", "getAudioTracks"):
        record(f"PostureWatch.jsx never references {forbidden}", forbidden not in src)

    # The backend command must not touch the mic event either.
    ws = (ROOT / "ui" / "ws_server.py").read_text(encoding="utf-8")
    block = ws.split('elif cmd == "posture_state":', 1)
    record("posture_state command exists in ws_server.py", len(block) == 2)
    if len(block) == 2:
        handler = block[1].split("elif cmd ==", 1)[0]
        record("the posture_state handler never touches _mic_event",
               "_mic_event" not in handler, handler[:200])

    # And the module that consumes it.
    fsrc = (ROOT / "actions" / "focus_session.py").read_text(encoding="utf-8")
    fsrc = re.sub(r"(?m)^\s*#.*$", "", fsrc)   # code, not the comments explaining the rule
    for forbidden in ("_mic_event", "start_listen", "get_mic_event"):
        record(f"focus_session.py never references {forbidden}", forbidden not in fsrc)

    # Electron main must deny audio at the permission layer.
    main_js = (ROOT / "ui" / "main.js").read_text(encoding="utf-8")
    record("main.js installs a permission handler",
           "setPermissionRequestHandler" in main_js)
    record("main.js denies audio capture explicitly",
           'includes("audio")' in main_js and "callback(false)" in main_js)



# ── screen watch (ambient stuck detection) ────────────────────────────

async def test_screen_stuck_nudges_once_then_cools_down():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        # No vision configured in the test env -> falls back to the
        # generic line, which is the behaviour under test here (timing),
        # not the wording.
        await fs.report_screen_stuck(image_b64="", now=t)
        record("a stuck screen produces exactly one nudge", len(h.spoken) == 1, h.spoken)
        record("screen_stuck surfaces in the whitelisted state",
               fs.focus_state()["screen_stuck"] is True)

        # Repeated reports throughout the cooldown must stay silent.
        for i in range(1, int(fs.SCREEN_STUCK_COOLDOWN_S), 20):
            await fs.report_screen_stuck(image_b64="", now=t + i)
        record(f"silent for the whole {int(fs.SCREEN_STUCK_COOLDOWN_S)}s cooldown",
               len(h.spoken) == 1, h.spoken)

        await fs.report_screen_stuck(image_b64="", now=t + fs.SCREEN_STUCK_COOLDOWN_S + 1)
        record("may nudge again after the cooldown", len(h.spoken) == 2, h.spoken)


async def test_screen_stuck_respects_the_relief_valve():
    _reset_store()
    with Harness() as h:
        t = 1000.0
        fs._posture.silence(seconds=fs.RELIEF_VALVE_S, now=t)
        await fs.report_screen_stuck(image_b64="", now=t + 5)
        record("no stuck-screen nudge while the relief valve is up", h.spoken == [], h.spoken)


async def test_screen_changed_clears_the_flag():
    _reset_store()
    with Harness() as h:
        await fs.report_screen_stuck(image_b64="", now=1000.0)
        record("flag set while stuck", fs.focus_state()["screen_stuck"] is True)
        await fs.clear_screen_stuck()
        record("flag clears when the screen changes again",
               fs.focus_state()["screen_stuck"] is False)


# ── the card trap ─────────────────────────────────────────────────────

async def test_card_retarget_locks_the_window_behind_the_card():
    """Clicking LOCK on the always-on-top card makes the CARD frontmost.
    A naive retarget would lock the card itself. source="card" must make
    it read past FRIDAY's own windows to the real work window."""
    _reset_store()
    with Harness() as h:
        h.switch_to("thesis.docx - Word", "WINWORD.EXE")
        await fs.start(minutes=25)
        await fs.tick(now=1000.0)

        # Simulate the click: FRIDAY's own window is now frontmost...
        h.switch_to("Focus", "electron.exe")
        # ...but the real work window behind it is a browser.
        import brain.handlers as handlers_mod
        orig_excl = handlers_mod._get_frontmost_excluding
        handlers_mod._get_frontmost_excluding = lambda excl=(): ("Docs - Google Chrome", "chrome.exe")
        try:
            msg = await fs.retarget(source="card")
        finally:
            handlers_mod._get_frontmost_excluding = orig_excl

        record("a card-sourced retarget locks on rather than deferring",
               "locked on" in msg.lower(), msg)
        record("...and it locked the window BEHIND the card, not the card",
               fs._session.baseline == fs._fingerprint("Docs - Google Chrome", "chrome.exe"))
        record("...so it is NOT the card's own fingerprint",
               fs._session.baseline != fs._fingerprint("Focus", "electron.exe"))
        record("not left deferred", fs.focus_state()["is_deferred"] is False)


async def test_card_retarget_defers_when_nothing_is_behind_it():
    _reset_store()
    with Harness() as h:
        h.switch_to("thesis.docx - Word", "WINWORD.EXE")
        await fs.start(minutes=25)
        await fs.tick(now=1000.0)

        import brain.handlers as handlers_mod
        orig_excl = handlers_mod._get_frontmost_excluding
        handlers_mod._get_frontmost_excluding = lambda excl=(): ("", "")
        try:
            msg = await fs.retarget(source="card")
        finally:
            handlers_mod._get_frontmost_excluding = orig_excl

        record("no readable window behind the card -> defers instead of guessing",
               "go to what you're working on" in msg.lower(), msg)
        record("...and is marked deferred", fs.focus_state()["is_deferred"] is True)


async def test_voice_retarget_is_unchanged_by_the_card_path():
    _reset_store()
    with Harness() as h:
        h.switch_to("thesis.docx - Word", "WINWORD.EXE")
        await fs.start(minutes=25)
        await fs.tick(now=1000.0)
        h.switch_to("Docs - Google Chrome", "chrome.exe")
        msg = await fs.retarget()  # default source="voice"
        record("voice retarget still locks whatever is actually frontmost",
               "locked on" in msg.lower() and
               fs._session.baseline == fs._fingerprint("Docs - Google Chrome", "chrome.exe"), msg)


async def main():
    await test_sustained_slouch_nudges_after_the_threshold()
    await test_nudge_then_cooldown_then_nudge_again()
    await test_brief_slouch_says_nothing()
    await test_head_down_also_counts_as_bad_posture()
    await test_absence_gets_the_longer_grace()
    await test_relief_valve_silences_posture_nudges()
    await test_relief_valve_expires()
    await test_relief_valve_does_not_silence_drift_callouts()
    await test_paused_session_suppresses_posture_nudges()
    await test_posture_state_is_booleans_only()
    await test_screen_stuck_nudges_once_then_cools_down()
    await test_screen_stuck_respects_the_relief_valve()
    await test_screen_changed_clears_the_flag()
    await test_card_retarget_locks_the_window_behind_the_card()
    await test_card_retarget_defers_when_nothing_is_behind_it()
    await test_voice_retarget_is_unchanged_by_the_card_path()
    test_ear_law_no_code_path_enables_the_microphone()

    print()
    print("=== SUMMARY ===")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total")
        for n, _ in failed:
            print(f"  - {n}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
