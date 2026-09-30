"""
test_save_memory_tool.py — regression test for the save_memory LLM tool
handler in brain/llm.py.

Found live: a local model (via Ollama tool-calling) called save_memory
with {"fact": "..."} / {"value": "..."} instead of the declared schema
({"category", "key", "value"}, all required). The old handler did:

    remember(key=args.get("key", ""), value=args.get("value", ""), ...)
    return "ok"

— which silently passed empty strings into remember() (a no-op there
by design), then returned "ok" unconditionally regardless of what
remember() actually did. Confirmed live: FRIDAY said "Got it. Added to
the records," logged nothing, and the fact was permanently absent.

First fix made remember() return True/False and added a fallback that
saved the raw text as a "notes" entry instead of dropping it — but that
still produced entries like "Archit Loves The Show House 2004: Archit
loves the show House (2004)", a category-less restatement of itself
with an ugly auto-generated key. Second fix (tested here): the fallback
now re-runs the text through the same LLM extraction
extract_and_save_facts() already uses for the automatic background
path, landing it in a real category with a sensible key — the raw-note
dump is now the last resort only if that extraction itself fails.

Run from the project root:
    python tests/test_save_memory_tool.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brain.llm as llm                              # noqa: E402
import memory.memory_store as memory_store           # noqa: E402
import memory.long_term as lt                        # noqa: E402
from memory.long_term import get_category            # noqa: E402

results = []


def _reset_store():
    """Points long-term memory at a throwaway vault — same isolation
    every other test file in this project uses. Without this, this
    file writes real fact entries into the real FRIDAY_Brain/ folder
    (confirmed: this was the one file in the whole suite still doing
    that)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="friday_savememorytest_"))
    lt.VAULT_PATH = tmp_dir / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp_dir / "lt_faiss.index"
    lt.META_PATH = tmp_dir / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp_dir / "long_term.json"


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


async def run():
    _reset_store()
    real_llm_call = memory_store._llm_call

    # --- The exact live failure, WITH the extraction succeeding ---
    async def fake_llm_call_good(prompt):
        return '{"category": "preferences", "key": "currently_watching", "value": "House (2004)"}'

    memory_store._llm_call = fake_llm_call_good
    result = await llm._dispatch_tool_impl(
        "save_memory",
        {"value": "Archit is currently watching and loves the TV series House (2004)"},
        None,
    )
    prefs = get_category("preferences")
    record(
        "malformed call gets properly re-categorized, not dumped as a generic note",
        result == "ok" and prefs.get("currently_watching") == "House (2004)",
        f"result={result!r}, prefs={prefs}",
    )
    notes_after_good = get_category("notes")
    record(
        "the raw-sentence-as-a-note fallback is NOT used when re-extraction succeeds",
        not any("currently watching and loves" in v for v in notes_after_good.values()),
        f"notes={notes_after_good}",
    )

    # --- Re-extraction itself fails (e.g. no LLM provider reachable) —
    #     must still fall back to the raw note, never lose the fact ---
    async def fake_llm_call_broken(prompt):
        raise ConnectionError("simulated: no LLM provider reachable")

    memory_store._llm_call = fake_llm_call_broken
    result2 = await llm._dispatch_tool_impl(
        "save_memory", {"fact": "Archit's favorite project is FRIDAY"}, None
    )
    notes = get_category("notes")
    record(
        "if re-extraction itself fails, still falls back to a raw note instead of losing the fact",
        result2 == "ok" and any("favorite project is FRIDAY" in v for v in notes.values()),
        f"result={result2!r}, notes={notes}",
    )
    key = next((k for k, v in notes.items() if "favorite project is FRIDAY" in v), "")
    record(
        "raw-note fallback key is still clean (alnum + underscores only)",
        bool(key) and key.replace("_", "").isalnum(),
        repr(key),
    )

    memory_store._llm_call = real_llm_call

    # --- Well-formed schema call must still work exactly as before ---
    result3 = await llm._dispatch_tool_impl(
        "save_memory", {"category": "preferences", "key": "favorite_language", "value": "Python"}, None
    )
    record(
        "a correctly-shaped call still works normally",
        result3 == "ok" and get_category("preferences").get("favorite_language") == "Python",
        f"result={result3!r}",
    )

    # --- A call with genuinely nothing usable must report failure honestly ---
    result4 = await llm._dispatch_tool_impl("save_memory", {}, None)
    record(
        "a call with no usable content reports failure instead of a false 'ok'",
        result4 != "ok" and "failed" in result4.lower(),
        f"result={result4!r}",
    )

    # --- 'projects' as a category must redirect safely, not error ---
    result5 = await llm._dispatch_tool_impl(
        "save_memory", {"category": "projects", "key": "current_project", "value": "FRIDAY_v6"}, None
    )
    record(
        "category='projects' (no longer valid) redirects into notes instead of failing",
        result5 == "ok" and get_category("notes").get("current_project") == "FRIDAY_v6",
        f"result={result5!r}, notes={get_category('notes')}",
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
