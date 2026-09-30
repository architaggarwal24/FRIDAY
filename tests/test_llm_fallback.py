"""
test_llm_fallback.py — regression test for brain/llm.py's Ollama-to-cloud
fallback chain.

Background: every OTHER provider (nvidia/gemini) already fell
back to Ollama on failure — but the reverse direction didn't exist.
_stream_ollama() caught its own errors internally and yielded a generic
"Hit a snag, boss" message instead of raising, so stream_response()'s
try/except around the ollama branch never actually caught anything to
fall back from. With LLM_PROVIDER=ollama set as the permanent default
(the normal setup for a fully-local install), any Ollama failure —  not
running, a `:cloud` model needing an ollama.com subscription, a dropped
connection — was a dead end for that whole turn even with working cloud
provider keys configured, until the user manually switched providers in
Settings. This is exactly what happened live: an ollama.com 402 on a
`:cloud` model produced "Hit a snag, boss" with no fallback attempted.

Fixed by: (1) letting _stream_ollama's error propagate instead of
swallowing it, (2) adding _cloud_fallback() — mirrors the existing
_ollama_fallback() in reverse, trying nvidia -> gemini in
order, (3) after 2 consecutive Ollama failures, permanently switching
the session's active provider (same threshold/pattern as voice/tts.py's
ElevenLabs->edge auto-switch) instead of retrying a down Ollama first on
every single turn.

Run from the project root:
    python tests/test_llm_fallback.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brain.llm as llm         # noqa: E402
from config import config       # noqa: E402
from memory import long_term as lt  # noqa: E402

results = []


def _reset_store():
    """stream_response() pulls long-term memory into the system prompt,
    which touches the real vault unless redirected — same isolation
    every other test file in this project uses. Without this, running
    this file writes into the real FRIDAY_Brain/ folder."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="friday_llmfallbacktest_"))
    lt.VAULT_PATH = tmp_dir / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp_dir / "lt_faiss.index"
    lt.META_PATH = tmp_dir / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp_dir / "long_term.json"


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


async def _fake_fail(*a, **kw):
    raise ConnectionError("simulated: ollama not reachable")
    yield  # pragma: no cover — makes this an async generator


async def _fake_ollama_ok(*a, **kw):
    yield "ollama says hi"


async def _fake_nvidia_ok(*a, **kw):
    yield "nvidia says hi"


async def _drain(gen):
    return [x async for x in gen]


async def run():
    _reset_store()
    toasts = []

    def fake_broadcast(data):
        toasts.append(data)

    import ui.ws_server as wsmod
    wsmod.broadcast_from_thread = fake_broadcast

    # --- below threshold: falls back, doesn't switch yet ---
    llm._stream_ollama = _fake_fail
    llm._stream_nvidia_nim = _fake_nvidia_ok
    config.brain.nvidia_nim_api_key = "fake-key-for-test"
    config.brain.gemini_api_key = ""
    llm._session_provider = "ollama"
    llm._consecutive_ollama_failures = 0

    out = "".join(await _drain(llm.stream_response("hello", None)))
    record(
        "1st ollama failure falls back to nvidia, provider not yet switched",
        "nvidia says hi" in out and llm.get_active_provider() == "ollama" and not toasts,
    )

    # --- at threshold: falls back AND switches permanently + toasts ---
    out = "".join(await _drain(llm.stream_response("hello again", None)))
    record(
        "2nd consecutive failure switches active provider to nvidia + toasts",
        "nvidia says hi" in out
        and llm.get_active_provider() == "nvidia"
        and len(toasts) == 1
        # Toast wording is now built centrally by
        # brain.llm._announce_provider_switch() (see test_provider_switch.py
        # for the dedicated coverage of that function) — it includes the
        # reason passed through from _cloud_fallback() rather than each
        # call site writing its own toast text.
        and "Ollama failed" in toasts[0]["message"],
    )

    # --- ollama succeeding directly keeps the counter at 0 ---
    llm._session_provider = "ollama"
    llm._consecutive_ollama_failures = 0
    llm._stream_ollama = _fake_ollama_ok
    out = "".join(await _drain(llm.stream_response("hi", None)))
    record(
        "ollama succeeding directly keeps the failure counter at 0",
        "ollama says hi" in out and llm._consecutive_ollama_failures == 0,
    )

    # --- no cloud providers configured: clear message, no crash ---
    llm._stream_ollama = _fake_fail
    config.brain.nvidia_nim_api_key = ""
    llm._consecutive_ollama_failures = 0
    out = "".join(await _drain(llm.stream_response("hello", None)))
    record(
        "no cloud provider configured -> clear message, no crash",
        "running" in out.lower() or "make sure" in out.lower(),
    )


if __name__ == "__main__":
    asyncio.run(run())
    print()
    print("=== SUMMARY ===")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total")
        sys.exit(1)
    print("ALL PASS")
