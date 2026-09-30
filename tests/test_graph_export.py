"""
test_graph_export.py — regression tests for memory/graph_export.py, the
vault -> {"nodes": [...], "links": [...]} flattener behind the `galaxy`
panel's 3D force-directed graph.

Covers:
  - an empty vault produces an empty graph, not an error
  - a note with no links of its own still produces an isolated node
    (never silently dropped just because nothing points at/from it)
  - the auto-generated Me -> relationship hub links (long_term.py's
    _rebuild_hub_links) show up as graph edges
  - a hand-added [[wikilink]] in a note's free-content zone (below
    obsidian_vault.FREE_ZONE_HINT) shows up as a graph edge too — the
    whole point of a real vault graph over a JSON export
  - a [[wikilink]] to a note that doesn't exist is dropped, not a crash
  - node shape (id/label/group/excerpt) matches what Galaxy.jsx expects,
    for both an aggregate note and a per-person relationship note

Run from the project root:
    python tests/test_graph_export.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import graph_export as ge          # noqa: E402
from memory import long_term as lt             # noqa: E402
from memory import obsidian_vault as ov        # noqa: E402

results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def _reset_store():
    tmp_dir = Path(tempfile.mkdtemp(prefix="friday_graphtest_"))
    lt.VAULT_PATH = tmp_dir / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp_dir / "lt_faiss.index"
    lt.META_PATH = tmp_dir / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp_dir / "long_term.json"  # nonexistent — skip migration
    return tmp_dir


def _add_free_zone_link(path: Path, wikilink_line: str) -> None:
    """Hand-adds a line below FREE_ZONE_HINT in an existing note, the
    way a user editing the note in Obsidian would."""
    text = path.read_text(encoding="utf-8")
    assert ov.FREE_ZONE_HINT in text, "note has no free-zone hint to anchor the edit"
    text = text.replace(ov.FREE_ZONE_HINT, ov.FREE_ZONE_HINT + f"\n{wikilink_line}\n")
    path.write_text(text, encoding="utf-8")


def test_empty_vault_produces_empty_graph_not_an_error():
    _reset_store()
    graph = ge.build_graph()
    record("empty vault -> empty nodes/links, no crash", graph == {"nodes": [], "links": []}, graph)


def test_note_with_no_links_is_an_isolated_node():
    _reset_store()
    lt.remember_many([{"category": "notes", "key": "grocery_list", "value": "eggs, milk, bread"}])

    graph = ge.build_graph()
    ids = {n["id"] for n in graph["nodes"]}
    record("a note nothing links to/from still produces a node", "notes/notes" in ids, ids)

    touches_it = [l for l in graph["links"] if "notes/notes" in (l["source"], l["target"])]
    record("...and it has no edges (isolated, not an error)", touches_it == [], touches_it)


def test_hub_link_from_me_to_relationship_is_included():
    _reset_store()
    lt.remember_many([
        {"category": "identity", "key": "name", "value": "Archit"},
        {"category": "relationships", "key": "sister", "value": "Priya"},
    ])

    graph = ge.build_graph()
    found = any({l["source"], l["target"]} == {"identity/me", "relationships/sister"} for l in graph["links"])
    record("auto-generated Me -> relationship hub link becomes a graph edge", found, graph["links"])


def test_hand_added_wikilink_in_free_zone_is_picked_up():
    _reset_store()
    lt.remember_many([
        {"category": "relationships", "key": "sister", "value": "Priya"},
        {"category": "notes", "key": "trip_planning", "value": "Thinking about a trip in December"},
    ])
    _add_free_zone_link(lt._aggregate_path("notes"), "Need to ask [[Sister]] about dates.")

    graph = ge.build_graph()
    found = any({l["source"], l["target"]} == {"notes/notes", "relationships/sister"} for l in graph["links"])
    record("hand-added [[wikilink]] in a note's free zone becomes a graph edge", found, graph["links"])


def test_wikilink_to_nonexistent_note_does_not_crash():
    _reset_store()
    lt.remember_many([{"category": "notes", "key": "trip_planning", "value": "Thinking about a trip"}])
    _add_free_zone_link(lt._aggregate_path("notes"), "See [[Nonexistent Person]] for details.")

    try:
        graph = ge.build_graph()
        crashed = False
    except Exception as e:
        crashed = True
        graph = None
        record("a link to a note that doesn't exist raised", False, repr(e))
    if not crashed:
        record("a [[wikilink]] to a nonexistent note doesn't crash the export", True)
        phantom_node = any("nonexistent" in n["id"] for n in graph["nodes"])
        phantom_edge = any("nonexistent" in l["source"] or "nonexistent" in l["target"] for l in graph["links"])
        record("...and doesn't produce a phantom node or edge either", not phantom_node and not phantom_edge)


def test_aggregate_node_shape():
    _reset_store()
    lt.remember_many([{"category": "preferences", "key": "favorite_language", "value": "Python"}])

    graph = ge.build_graph()
    node = next((n for n in graph["nodes"] if n["id"] == "preferences/preferences"), None)
    record("aggregate note produces exactly one node", node is not None)
    if node:
        record("label is title-cased via key_to_title", node["label"] == "Preferences", node["label"])
        record("group is the category (for color-coding)", node["group"] == "preferences", node["group"])
        record("excerpt is drawn from the note's managed content", "Python" in node["excerpt"], node["excerpt"])


def test_relationship_node_shape():
    _reset_store()
    lt.remember_many([{"category": "relationships", "key": "sister", "value": "Priya, lives in Mumbai"}])

    graph = ge.build_graph()
    node = next((n for n in graph["nodes"] if n["id"] == "relationships/sister"), None)
    record("relationship note produces exactly one node", node is not None)
    if node:
        record("label is title-cased via key_to_title", node["label"] == "Sister", node["label"])
        record("group is 'relationships' (for color-coding)", node["group"] == "relationships", node["group"])
        record(
            "excerpt is the note's raw managed content (no H2 sectioning for entity notes)",
            node["excerpt"] == "Priya, lives in Mumbai", node["excerpt"],
        )


def test_excerpt_is_truncated_to_expected_length():
    _reset_store()
    long_value = "x" * 400
    lt.remember_many([{"category": "relationships", "key": "friend", "value": long_value}])

    graph = ge.build_graph()
    node = next((n for n in graph["nodes"] if n["id"] == "relationships/friend"), None)
    record(
        "excerpt is capped at graph_export.EXCERPT_LENGTH chars",
        node is not None and len(node["excerpt"]) == ge.EXCERPT_LENGTH,
        len(node["excerpt"]) if node else None,
    )


if __name__ == "__main__":
    test_empty_vault_produces_empty_graph_not_an_error()
    test_note_with_no_links_is_an_isolated_node()
    test_hub_link_from_me_to_relationship_is_included()
    test_hand_added_wikilink_in_free_zone_is_picked_up()
    test_wikilink_to_nonexistent_note_does_not_crash()
    test_aggregate_node_shape()
    test_relationship_node_shape()
    test_excerpt_is_truncated_to_expected_length()

    print()
    print("=== SUMMARY ===")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total")
        sys.exit(1)
    print("ALL PASS")
