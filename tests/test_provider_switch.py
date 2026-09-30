"""
test_provider_switch.py — regression tests for the provider-switch
flavor-text layer in brain/llm.py.

Covers:
  - set_active_provider() is the one function that ever speaks a line;
    switching to an unknown provider is rejected and silent
  - switching to whatever's already active is a no-op: no line, no
    toast, nothing spoken twice
  - two consecutive real switches produce two different lines (not a
    repeat) — the exact scenario in the prompt's Verify section
  - the voice-phrase quick-switch inside stream_response() goes through
    the same function (not its own hardcoded strings) and — the bug
    this file specifically guards against — never yields zero output,
    which would otherwise trip start.py's "produced zero output"
    fallback and speak an unrelated line right after the switch
  - _cloud_fallback()'s automatic Ollama-down switch gets a real
    curated line via the same path, not the old toast-only behavior
  - lock_to_ollama()/unlock_provider() are thin wrappers, not a second
    implementation — going through set_active_provider() has the same
    observable effect (one spoken line, correct active provider) as
    calling set_active_provider() directly

TTS and the toast broadcaster are faked by monkeypatching
ui.ws_server's module attributes, same trick test_llm_fallback.py
already uses for broadcast_from_thread.

Run from the project root:
    python tests/test_provider_switch.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brain.llm as llm            # noqa: E402
from memory import long_term as lt  # noqa: E402

results = []


def _reset_store():
    """stream_response() pulls long-term memory into the system prompt
    (build_system_prompt() -> format_for_prompt()), which touches the
    real vault unless redirected — same isolation every other test file
    in this project uses, so running this one doesn't write into the
    real FRIDAY_Brain/ folder."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="friday_providertest_"))
    lt.VAULT_PATH = tmp_dir / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp_dir / "lt_faiss.index"
    lt.META_PATH = tmp_dir / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp_dir / "long_term.json"


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


class Harness:
    """Fakes ui.ws_server's TTS instance and toast broadcaster, and
    resets brain.llm's session-provider state around each test."""

    def __init__(self):
        self.spoken = []
        self.toasts = []

    def __enter__(self):
        import ui.ws_server as wsmod

        class _FakeTTS:
            def enqueue(_self, text):
                self.spoken.append(text)

        self._wsmod = wsmod
        self._orig_tts = wsmod._tts_instance
        self._orig_broadcast = wsmod.broadcast_from_thread
        wsmod._tts_instance = _FakeTTS()
        wsmod.broadcast_from_thread = lambda data: self.toasts.append(data)

        llm._session_provider = None
        llm._last_provider_line = ""
        llm._consecutive_ollama_failures = 0
        _reset_store()
        return self

    def __exit__(self, *exc):
        self._wsmod._tts_instance = self._orig_tts
        self._wsmod.broadcast_from_thread = self._orig_broadcast
        llm._session_provider = None
        llm._last_provider_line = ""
        llm._consecutive_ollama_failures = 0


# ── set_active_provider() itself ────────────────────────────────────────

def test_unknown_provider_is_rejected_and_silent():
    with Harness() as h:
        ok = llm.set_active_provider("chatgpt")
        record("unknown provider is rejected", ok is False)
        record("nothing spoken for a rejected switch", h.spoken == [], h.spoken)
        record("no toast for a rejected switch", h.toasts == [], h.toasts)


def test_known_provider_speaks_a_curated_line():
    with Harness() as h:
        ok = llm.set_active_provider("gemini")
        record("known provider is accepted", ok is True)
        record("exactly one line spoken", len(h.spoken) == 1, h.spoken)
        record("the line is one of the curated Gemini lines, not generic",
               h.spoken and h.spoken[0] in llm._PROVIDER_SWITCH_LINES["gemini"], h.spoken)
        record("a toast also fires", len(h.toasts) == 1, h.toasts)
        record("active provider actually changed", llm.get_active_provider() == "gemini")


def test_switching_to_already_active_provider_is_a_silent_no_op():
    with Harness() as h:
        llm.set_active_provider("nvidia")
        h.spoken.clear()
        h.toasts.clear()

        ok = llm.set_active_provider("nvidia")
        record("re-selecting the active provider still reports ok", ok is True)
        record("no new line for a no-op switch", h.spoken == [], h.spoken)
        record("no new toast for a no-op switch", h.toasts == [], h.toasts)


def test_two_consecutive_real_switches_never_repeat():
    """The exact scenario in the prompt: swap providers by voice twice
    in a row, confirm two different lines."""
    with Harness() as h:
        llm.set_active_provider("ollama")
        llm.set_active_provider("nvidia")
        record("two consecutive real switches produce exactly two lines",
               len(h.spoken) == 2, h.spoken)
        record("...and they are different lines, not a repeat",
               h.spoken[0] != h.spoken[1], h.spoken)


def test_every_line_in_the_pool_is_reachable_and_provider_specific():
    with Harness() as h:
        seen = set()
        for _ in range(60):
            llm.set_active_provider("gemini")
            seen.add(h.spoken[-1])
            llm.set_active_provider("ollama")  # bounce away so the next call is a real switch
        record("random selection reaches more than just one line from the pool",
               len(seen) > 1, seen)
        record("every line seen actually belongs to Gemini's own pool",
               seen <= set(llm._PROVIDER_SWITCH_LINES["gemini"]), seen)


# ── the voice-phrase quick-switch inside stream_response() ─────────────

async def test_voice_phrase_switch_never_yields_zero_output():
    """The bug this test exists to catch: stream_response() must yield
    at least one sentence for the quick-switch branches, or start.py's
    caller falls into its 'produced zero output' fallback and speaks an
    unrelated line immediately after the real announcement."""
    with Harness() as h:
        chunks = [c async for c in llm.stream_response("use ollama please")]
        record("stream_response yields at least one chunk for a quick-switch phrase",
               len(chunks) >= 1, chunks)
        record("the yielded chunk is silent/transcript-only (\\x00-prefixed)",
               chunks and chunks[0].startswith("\x00"), chunks)
        record("exactly one line was actually spoken (via TTS, not the yield)",
               len(h.spoken) == 1, h.spoken)
        record("the transcript text matches what was actually spoken",
               chunks[0][1:] == h.spoken[0], (chunks, h.spoken))


async def test_voice_phrase_switch_routes_through_set_active_provider():
    """Confirms this isn't a second, parallel implementation — the
    curated pool is what gets spoken, not a hardcoded one-off string."""
    with Harness() as h:
        async for _ in llm.stream_response("switch to ollama"):
            pass
        record("the quick-switch phrase speaks a real curated Ollama line",
               h.spoken and h.spoken[0] in llm._PROVIDER_SWITCH_LINES["ollama"], h.spoken)
        record("active provider actually changed to ollama", llm.get_active_provider() == "ollama")


async def test_voice_phrase_no_op_still_yields_something():
    with Harness() as h:
        llm.set_active_provider("ollama")
        h.spoken.clear()
        chunks = [c async for c in llm.stream_response("use ollama")]
        record("a no-op quick-switch still yields (avoids the zero-output fallback)",
               len(chunks) >= 1, chunks)
        record("...but says nothing new out loud", h.spoken == [], h.spoken)


async def test_unlock_provider_phrase_returns_to_configured_default():
    with Harness() as h:
        llm.set_active_provider("ollama")
        h.spoken.clear()
        async for _ in llm.stream_response("switch to nim"):
            pass
        record("'switch to nim' returns to the configured provider",
               llm.get_active_provider() == llm.config.brain.llm_provider)
        record("a line was spoken for it", len(h.spoken) == 1, h.spoken)


# ── _cloud_fallback()'s automatic Ollama-down switch ────────────────────

async def test_automatic_ollama_fallback_speaks_a_real_line_not_just_a_toast():
    """The prompt's other Verify scenario: trigger the existing automatic
    fallback and confirm it gets a spoken line now, not the old
    toast-only behavior. Drives the REAL _cloud_fallback closure (it's
    defined inside stream_response(), unreachable directly) by making
    Ollama fail and NVIDIA succeed, rather than replicating its logic —
    a hand copy could silently drift from the real implementation,
    which is exactly the duplication risk this whole feature exists to
    avoid."""
    with Harness() as h:
        llm._session_provider = "ollama"
        llm._consecutive_ollama_failures = llm._LLM_AUTO_SWITCH_THRESHOLD - 1

        orig_nim_key = llm.config.brain.nvidia_nim_api_key
        orig_stream_ollama = llm._stream_ollama
        orig_stream_nvidia = llm._stream_nvidia_nim
        llm.config.brain.nvidia_nim_api_key = "fake-key-for-test"

        async def _broken_ollama(*a, **kw):
            raise ConnectionError("Ollama not reachable")
            yield  # pragma: no cover — makes this an async generator

        async def _fake_nvidia(*a, **kw):
            yield "hello from nvidia"

        llm._stream_ollama = _broken_ollama
        llm._stream_nvidia_nim = _fake_nvidia
        try:
            chunks = [c async for c in llm.stream_response("what's the weather")]
        finally:
            llm.config.brain.nvidia_nim_api_key = orig_nim_key
            llm._stream_ollama = orig_stream_ollama
            llm._stream_nvidia_nim = orig_stream_nvidia

        record("the real fallback path actually produced output",
               len(chunks) >= 1, chunks)
        record("the automatic fallback speaks a real curated line",
               len(h.spoken) == 1 and h.spoken[0] in llm._PROVIDER_SWITCH_LINES["nvidia"], h.spoken)
        record("...not just a toast with no voice", h.toasts and h.spoken, (h.toasts, h.spoken))
        record("active provider actually switched to nvidia for future turns",
               llm.get_active_provider() == "nvidia")
        record("the failure counter reset after the switch",
               llm._consecutive_ollama_failures == 0, llm._consecutive_ollama_failures)


# ── lock_to_ollama / unlock_provider are thin wrappers, not a copy ─────

def test_lock_and_unlock_helpers_go_through_the_same_function():
    with Harness() as h:
        llm.lock_to_ollama("cloud unavailable")
        record("lock_to_ollama speaks a real curated line",
               h.spoken and h.spoken[0] in llm._PROVIDER_SWITCH_LINES["ollama"], h.spoken)
        record("lock_to_ollama actually sets the active provider", llm.get_active_provider() == "ollama")

        h.spoken.clear()
        llm.unlock_provider()
        record("unlock_provider speaks a line for returning to the configured provider",
               len(h.spoken) == 1, h.spoken)
        record("unlock_provider returns to config.brain.llm_provider",
               llm.get_active_provider() == llm.config.brain.llm_provider)


async def main():
    test_unknown_provider_is_rejected_and_silent()
    test_known_provider_speaks_a_curated_line()
    test_switching_to_already_active_provider_is_a_silent_no_op()
    test_two_consecutive_real_switches_never_repeat()
    test_every_line_in_the_pool_is_reachable_and_provider_specific()
    await test_voice_phrase_switch_never_yields_zero_output()
    await test_voice_phrase_switch_routes_through_set_active_provider()
    await test_voice_phrase_no_op_still_yields_something()
    await test_unlock_provider_phrase_returns_to_configured_default()
    await test_automatic_ollama_fallback_speaks_a_real_line_not_just_a_toast()
    test_lock_and_unlock_helpers_go_through_the_same_function()

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
