"""
test_memory_recall.py — regression tests for memory/long_term.py.

Covers two real bugs found while investigating why recall felt weak:

1. remember_many() called a function, _remember_many_locked(), that was
   never actually defined — the real implementation sat as unreachable
   code after a `return` statement inside remember_many() itself. Every
   call raised NameError. This is the function extract_and_save_facts()
   (automatic, triggered on nearly every turn containing "i am", "i
   work", "i live", "i like", etc.) and handle_memory_save() (the
   explicit "remember that..." command) both call to actually persist
   facts — so long-term memory was silently, 100% failing to save
   anything, the whole time, with the failure swallowed at debug-log
   level or masked behind a generic "Couldn't save that, boss" message.

2. format_for_prompt() trusted _faiss_available() (are the packages
   importable?) as license to filter down to identity-only + semantic
   hits — but _search_index() returned an empty list both when search
   genuinely found nothing AND when search couldn't run at all (index
   not built yet, embedding call failed for any reason — e.g. no
   network to fetch the model on first use). Those two cases are
   indistinguishable to a caller checking `if hits:` — so any transient
   embedding failure made every non-identity fact silently invisible to
   the system prompt, which is a worse failure mode than not having
   FAISS installed at all (that path falls back to showing everything).

Run from the project root:
    python tests/test_memory_recall.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import long_term as lt  # noqa: E402

results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def _reset_store():
    """Point long_term at a throwaway vault for this test run, and start empty."""
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="friday_memtest_"))
    lt.VAULT_PATH = tmp_dir / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp_dir / "lt_faiss.index"
    lt.META_PATH = tmp_dir / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp_dir / "long_term.json"  # nonexistent — skip migration


def test_remember_many_does_not_crash():
    _reset_store()
    n = lt.remember_many([
        {"category": "identity", "key": "name", "value": "Archit"},
        {"category": "preferences", "key": "favorite_language", "value": "Python"},
    ])
    record("remember_many() saves facts without raising NameError", n == 2, f"saved={n}")

    record(
        "facts are actually persisted with correct values",
        lt.get_category("identity").get("name") == "Archit"
        and lt.get_category("preferences").get("favorite_language") == "Python",
    )

    n2 = lt.remember_many([{"category": "identity", "key": "name", "value": "Archit"}])
    record("re-saving an identical fact reports 0 changed (dedup works)", n2 == 0, f"n2={n2}")

    n3 = lt.remember_many([{"category": "identity", "key": "name", "value": "Archit S"}])
    record("an actual value change is detected and saved", n3 == 1, f"n3={n3}")


def test_format_for_prompt_falls_back_when_search_cannot_run():
    _reset_store()
    lt.remember_many([
        {"category": "identity", "key": "name", "value": "Archit"},
        {"category": "preferences", "key": "favorite_language", "value": "Python"},
        {"category": "relationships", "key": "sister", "value": "younger sister named Priya"},
    ])

    # Simulate: packages are importable, but search genuinely can't run
    # this time (index not built, embedding call failed, whatever).
    original_search = lt._search_index
    original_available = lt._faiss_available
    lt._faiss_available = lambda: True
    lt._search_index = lambda query, k=lt.TOP_K_RETRIEVE: None
    try:
        result = lt.format_for_prompt("tell me about the user")
    finally:
        lt._search_index = original_search
        lt._faiss_available = original_available

    record(
        "when search can't run, non-identity facts still show (not silently hidden)",
        "Python" in result and "Priya" in result,
        repr(result),
    )
    record(
        "does not claim [semantic] mode when search didn't actually run",
        "[semantic]" not in result,
    )


def test_format_for_prompt_still_filters_when_search_actually_works():
    _reset_store()
    lt.remember_many([
        {"category": "identity", "key": "name", "value": "Archit"},
        {"category": "preferences", "key": "favorite_language", "value": "Python"},
        {"category": "relationships", "key": "sister", "value": "younger sister named Priya"},
    ])

    # Simulate: search actually ran and only matched one entry — the fix
    # must not have broken real filtering by always showing everything.
    original_search = lt._search_index
    original_available = lt._faiss_available
    lt._faiss_available = lambda: True
    lt._search_index = lambda query, k=lt.TOP_K_RETRIEVE: [{"cat": "preferences", "key": "favorite_language"}]
    try:
        result = lt.format_for_prompt("what language does the user like")
    finally:
        lt._search_index = original_search
        lt._faiss_available = original_available

    record(
        "real filtering still works: identity + matched hit included, unmatched fact excluded",
        "Python" in result and "Archit" in result and "Priya" not in result,
        repr(result),
    )
    record("claims [semantic] mode when search actually ran", "[semantic]" in result)


def _with_fake_broadcast():
    """Swaps in a capturing fake for ui.ws_server.broadcast_from_thread and
    returns (list_of_broadcast_calls, restore_fn). format_for_prompt()
    imports the real function fresh at call time (`from ui.ws_server import
    broadcast_from_thread`), so patching the module attribute beforehand is
    picked up correctly — same trick test_llm_fallback.py uses."""
    import ui.ws_server as wsmod
    calls = []
    original = wsmod.broadcast_from_thread
    wsmod.broadcast_from_thread = lambda data: calls.append(data)
    return calls, (lambda: setattr(wsmod, "broadcast_from_thread", original))


def test_semantic_match_broadcasts_matched_node_ids_for_the_galaxy():
    _reset_store()
    lt.remember_many([
        {"category": "identity", "key": "name", "value": "Archit"},
        {"category": "preferences", "key": "favorite_language", "value": "Python"},
        {"category": "relationships", "key": "sister", "value": "younger sister named Priya"},
    ])

    original_search = lt._search_index
    original_available = lt._faiss_available
    lt._faiss_available = lambda: True
    lt._search_index = lambda query, k=lt.TOP_K_RETRIEVE: [
        {"cat": "preferences", "key": "favorite_language"},
        {"cat": "relationships", "key": "sister"},
    ]
    calls, restore = _with_fake_broadcast()
    try:
        lt.format_for_prompt("what language does the user like")
    finally:
        lt._search_index = original_search
        lt._faiss_available = original_available
        restore()

    record("semantic mode broadcasts exactly one memory_match event", len(calls) == 1, calls)
    if calls:
        record("event name is memory_match", calls[0].get("event") == "memory_match", calls[0])
        record(
            "node ids use the real graph node id shape (aggregate collapsed, relationship as-is)",
            set(calls[0].get("node_ids", [])) == {"preferences/preferences", "relationships/sister"},
            calls[0].get("node_ids"),
        )


def test_fallback_mode_does_not_broadcast():
    _reset_store()
    lt.remember_many([
        {"category": "preferences", "key": "favorite_language", "value": "Python"},
    ])

    original_search = lt._search_index
    original_available = lt._faiss_available
    lt._faiss_available = lambda: True
    lt._search_index = lambda query, k=lt.TOP_K_RETRIEVE: None  # search couldn't run
    calls, restore = _with_fake_broadcast()
    try:
        lt.format_for_prompt("tell me about the user")
    finally:
        lt._search_index = original_search
        lt._faiss_available = original_available
        restore()

    record("fallback-to-everything path never broadcasts a galaxy match", calls == [], calls)


def test_no_query_does_not_broadcast():
    _reset_store()
    lt.remember_many([
        {"category": "preferences", "key": "favorite_language", "value": "Python"},
    ])

    calls, restore = _with_fake_broadcast()
    try:
        lt.format_for_prompt("")  # e.g. small talk / no query supplied
    finally:
        restore()

    record("no query -> no semantic search -> no broadcast", calls == [], calls)


if __name__ == "__main__":
    test_remember_many_does_not_crash()
    test_format_for_prompt_falls_back_when_search_cannot_run()
    test_format_for_prompt_still_filters_when_search_actually_works()
    test_semantic_match_broadcasts_matched_node_ids_for_the_galaxy()
    test_fallback_mode_does_not_broadcast()
    test_no_query_does_not_broadcast()

    print()
    print("=== SUMMARY ===")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total")
        sys.exit(1)
    print("ALL PASS")
