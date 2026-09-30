"""
test_browser_and_messaging.py — regression tests for:

  1. The wiring fix: brain/handlers.py's handle_browser_control() and
     handle_send_message() used to always use webbrowser.open() (OS
     default browser, no way to target a specific one) and a
     pre-fill-and-ask-the-human-to-click-Send WhatsApp flow — even
     though a real, working, Playwright/pyautogui-based implementation
     already existed in actions/browser_control.py and
     actions/send_message.py and was never called from anywhere. These
     tests confirm the real implementations are actually reached now.

  2. Two concrete bugs found while wiring this up:
       - actions/browser_control.py referenced an undefined
         `jarvis_profile` in its real-profile-launch-failed fallback
         path (only `friday_profile` was ever defined) — a guaranteed
         NameError exactly when that fallback was needed most (e.g.
         the target browser is already open with that profile).
       - actions/send_message.py's _get_os() built its result from an
         empty dict, so it returned "windows" unconditionally on every
         platform.

  3. The actual reported bug: "search X in edge" opened the OS default
     browser (Opera) instead, because the browser_control tool schema
     never had a `browser` parameter at all — nothing the LLM could
     have passed even if it tried. Covered by inspecting the live
     schema in brain/llm.py.

  4. The other reported bug: asking "what do you want to search for"
     and then answering it went through NORMAL intent classification
     instead of completing the search — "weather in bangalore" got
     answered as a fresh weather query. Fixed with a pending-query
     mechanic mirroring actions/focus_session.py's
     is_awaiting_voice_reply()/consume_voice_reply() shape.

playwright is actually installed in this sandbox, so
actions/browser_control.py imports for real; pyautogui is not, so
actions/send_message.py exercises its own ImportError-guarded
_PYAUTOGUI = False path, same as it would on a machine that hasn't
installed it. Both are fine — no real browser gets launched or
message sent in any of these; the actual browser_control()/
send_message() entry points are monkeypatched out everywhere that
would otherwise do real I/O.

Run from the project root:
    python tests/test_browser_and_messaging.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brain.handlers as handlers  # noqa: E402

results = []
ROOT = Path(__file__).resolve().parents[1]


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def _reset_pending():
    handlers._pending_browser_search = None


# ── bug 1: the jarvis_profile NameError ─────────────────────────────────

def test_no_jarvis_profile_reference_remains():
    src = (ROOT / "actions" / "browser_control.py").read_text(encoding="utf-8")
    record("actions/browser_control.py no longer references the undefined jarvis_profile",
           "jarvis_profile" not in src)
    record("...friday_profile is what's actually used in the fallback",
           "friday_profile" in src)


def test_browser_control_module_actually_imports():
    """The NameError above would only ever surface at CALL time (Python
    doesn't check names inside a function body until it runs), so
    importing cleanly doesn't prove the bug is fixed — that's what the
    source-grep above is for. This just confirms nothing else about the
    real module is broken."""
    import actions.browser_control as bc
    record("actions/browser_control.py imports cleanly", bc is not None)
    record("browser_control() entry point exists", callable(getattr(bc, "browser_control", None)))


# ── bug 2: send_message.py's _get_os() ──────────────────────────────────

def test_get_os_detects_the_real_platform():
    import actions.send_message as sm
    import platform as _platform

    orig = _platform.system
    try:
        _platform.system = lambda: "Windows"
        record("_get_os() detects Windows for real now", sm._get_os() == "windows")
        _platform.system = lambda: "Darwin"
        record("_get_os() detects mac for real now", sm._get_os() == "mac")
        _platform.system = lambda: "Linux"
        record("_get_os() detects linux for real now", sm._get_os() == "linux")
    finally:
        _platform.system = orig


def test_get_os_is_not_hardcoded_from_an_empty_dict():
    import inspect
    import actions.send_message as sm
    body = inspect.getsource(sm._get_os)
    # Positive assertion of the actual fix (calls platform.system()),
    # not a search for the old code's absence — the comment
    # documenting what the bug used to be legitimately still contains
    # the string "cfg = {}", which a naive absence-check would trip on.
    record("_get_os() now actually calls platform.system()",
           "platform.system()" in body, body)


# ── bug 3: the browser_control tool schema now has a browser param ─────

def test_browser_control_schema_has_a_browser_parameter():
    import brain.llm as llm
    schema = next(t for t in llm.TOOL_DEFINITIONS if t["name"] == "browser_control")
    props = schema["parameters"]["properties"]
    record("browser_control's schema now has a 'browser' property",
           "browser" in props, props.keys())
    record("...with a description mentioning at least edge and chrome",
           "edge" in props["browser"]["description"].lower()
           and "chrome" in props["browser"]["description"].lower(),
           props["browser"]["description"])
    record("the tool description tells the model to always pass it when named",
           "browser" in schema["description"].lower())


# ── bug 4: the pending-search follow-up ─────────────────────────────────

async def test_search_with_no_query_asks_and_remembers_instead_of_launching():
    _reset_pending()
    called = {"n": 0}

    def _fake_browser_control(parameters):
        called["n"] += 1
        return "should not have been called"

    import actions.browser_control as bc
    orig = bc.browser_control
    bc.browser_control = _fake_browser_control
    try:
        result = await handlers.handle_browser_control({"action": "search", "browser": "edge"})
    finally:
        bc.browser_control = orig

    record("asking to search with no query asks a clean clarifying question",
           "search" in result.lower(), result)
    record("...and does NOT launch a browser for nothing to search",
           called["n"] == 0, called)
    record("is_awaiting_browser_query() is now True", handlers.is_awaiting_browser_query() is True)


async def test_pending_query_is_consumed_as_the_search_term_not_reclassified():
    """The exact bug reported: answering the clarifying question must
    complete the search, not be treated as a brand new, unrelated
    message — even when the answer looks exactly like something that
    would otherwise classify as its own intent (a weather query)."""
    _reset_pending()
    captured = {}

    def _fake_browser_control(parameters):
        captured.update(parameters)
        return f"Searching for: {parameters.get('query')}"

    import actions.browser_control as bc
    orig = bc.browser_control
    bc.browser_control = _fake_browser_control
    try:
        await handlers.handle_browser_control({"action": "search", "browser": "edge"})
        ack = await handlers.consume_browser_query("weather in bangalore")
    finally:
        bc.browser_control = orig

    record("consume_browser_query returns a real result, not None",
           ack is not None, ack)
    record("the query that actually got searched is the follow-up text itself",
           captured.get("query") == "weather in bangalore", captured)
    record("the browser named in the ORIGINAL request is preserved through the follow-up",
           captured.get("browser") == "edge", captured)
    record("is_awaiting_browser_query() clears after being consumed",
           handlers.is_awaiting_browser_query() is False)


async def test_unrelated_utterance_after_consuming_is_not_intercepted_again():
    _reset_pending()

    import actions.browser_control as bc
    orig = bc.browser_control
    bc.browser_control = lambda parameters: "ok"
    try:
        await handlers.handle_browser_control({"action": "search"})
        await handlers.consume_browser_query("first query")
        second = await handlers.consume_browser_query("something completely unrelated")
    finally:
        bc.browser_control = orig

    record("a second utterance after the pending query is already consumed is not intercepted",
           second is None, second)


async def test_search_with_a_query_already_given_never_becomes_pending():
    """Baseline: the normal, already-working case (a full request in one
    go) must not trip the new clarification path at all."""
    _reset_pending()
    captured = {}

    import actions.browser_control as bc
    orig = bc.browser_control
    bc.browser_control = lambda parameters: captured.update(parameters) or "Searching for: x"
    try:
        await handlers.handle_browser_control({"action": "search", "query": "weather in bangalore", "browser": "edge"})
    finally:
        bc.browser_control = orig

    record("a search with a real query never sets a pending state",
           handlers.is_awaiting_browser_query() is False)
    record("...and the browser actually gets called with the right args",
           captured.get("query") == "weather in bangalore" and captured.get("browser") == "edge",
           captured)


async def test_empty_followup_gives_up_cleanly_rather_than_erroring():
    _reset_pending()
    await handlers.handle_browser_control({"action": "search"})
    ack = await handlers.consume_browser_query("   ")
    record("an empty follow-up doesn't crash, and gives an honest response",
           ack is not None and "boss" in ack.lower(), ack)
    record("pending state clears either way (doesn't loop forever waiting)",
           handlers.is_awaiting_browser_query() is False)


# ── the fallback path (Playwright/pyautogui genuinely unavailable) ─────

async def test_browser_control_falls_back_gracefully_without_playwright():
    _reset_pending()
    import builtins
    real_import = builtins.__import__

    def _blocking_import(name, *a, **kw):
        if name == "actions.browser_control" or name.startswith("actions.browser_control"):
            raise ImportError("simulated: playwright not installed")
        return real_import(name, *a, **kw)

    builtins.__import__ = _blocking_import
    try:
        result = await handlers.handle_browser_control({"action": "go_to", "url": "example.com"})
    finally:
        builtins.__import__ = real_import

    record("falls back to the plain webbrowser path when the real module can't import",
           "example.com" in result, result)


async def test_send_message_wires_through_to_the_real_module():
    captured = {}
    import actions.send_message as sm
    orig = sm.send_message
    sm.send_message = lambda parameters: captured.update(parameters) or "Message sent to Dad on WhatsApp."
    try:
        result = await handlers.handle_send_message({
            "receiver": "Dad", "message_text": "hi", "platform": "WhatsApp",
        })
    finally:
        sm.send_message = orig

    record("handle_send_message reaches the real send_message() now, not just a pre-fill-and-ask",
           captured.get("receiver") == "Dad" and captured.get("message_text") == "hi", captured)
    record("the result reports an actual send, not 'select and hit Send yourself'",
           "hit send" not in result.lower(), result)


async def main():
    test_no_jarvis_profile_reference_remains()
    test_browser_control_module_actually_imports()
    test_get_os_detects_the_real_platform()
    test_get_os_is_not_hardcoded_from_an_empty_dict()
    test_browser_control_schema_has_a_browser_parameter()
    await test_search_with_no_query_asks_and_remembers_instead_of_launching()
    await test_pending_query_is_consumed_as_the_search_term_not_reclassified()
    await test_unrelated_utterance_after_consuming_is_not_intercepted_again()
    await test_search_with_a_query_already_given_never_becomes_pending()
    await test_empty_followup_gives_up_cleanly_rather_than_erroring()
    await test_browser_control_falls_back_gracefully_without_playwright()
    await test_send_message_wires_through_to_the_real_module()

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
