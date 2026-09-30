"""
test_obsidian_brain.py — regression tests for the Obsidian-vault-backed
long-term memory (memory/obsidian_vault.py + memory/long_term.py).

Covers the behaviors specific to this feature, on top of the general
save/recall correctness already covered by test_memory_recall.py:

  - the vault gets the right folder/file structure for each category
  - People/Me.md accumulates auto-generated [[wikilink]] sections
    pointing at every relationship/project note, and those links stay
    correct as relationships/projects are added and removed
  - the core promise of the feature: content you add by hand in a note
    (outside FRIDAY's managed block) survives every future FRIDAY
    rewrite of that note, byte for byte
  - legacy long_term.json gets migrated into the vault exactly once
  - the reserved section keys used for hub-links never leak into
    get_category("identity") as if they were real facts

Run from the project root:
    python tests/test_obsidian_brain.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import long_term as lt          # noqa: E402
from memory import obsidian_vault as ov     # noqa: E402

results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def _reset_store():
    tmp_dir = Path(tempfile.mkdtemp(prefix="friday_obsidiantest_"))
    lt.VAULT_PATH = tmp_dir / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp_dir / "lt_faiss.index"
    lt.META_PATH = tmp_dir / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp_dir / "long_term.json"
    return tmp_dir


def test_vault_structure_and_file_placement():
    _reset_store()
    lt.remember_many([
        {"category": "identity", "key": "name", "value": "Archit"},
        {"category": "preferences", "key": "favorite_language", "value": "Python"},
        {"category": "relationships", "key": "sister", "value": "younger sister named Priya"},
        {"category": "wishes", "key": "travel", "value": "wants to visit Japan"},
    ])
    record("identity aggregates into People/Me.md", (lt.VAULT_PATH / "People" / "Me.md").exists())
    record("preferences aggregate into Preferences.md", (lt.VAULT_PATH / "Preferences.md").exists())
    record("wishes aggregate into Wishes.md", (lt.VAULT_PATH / "Wishes.md").exists())
    record("relationships get their own note under People/", (lt.VAULT_PATH / "People" / "Sister.md").exists())
    record("'projects' is no longer a valid category", "projects" not in lt.VALID_CATEGORIES)


def test_projects_category_redirects_to_notes():
    """'projects' was removed as its own category (folded into notes) —
    anything that still tries to save under it must land safely in
    notes instead of silently going nowhere."""
    _reset_store()
    n = lt.remember_many([{"category": "projects", "key": "current_project", "value": "FRIDAY_v6"}])
    record("saving with category='projects' still saves something", n == 1, f"n={n}")
    notes = lt.get_category("notes")
    record(
        "a 'projects' save redirects safely into notes instead of vanishing",
        notes.get("current_project") == "FRIDAY_v6",
        f"got {notes}",
    )
    record("no Projects/ folder gets created for it", not (lt.VAULT_PATH / "Projects").exists())


def test_reserved_link_keys_round_trip_and_never_leak():
    for k in lt._LINKS_KEY.values():
        record(f"reserved key {k!r} round-trips through title_to_key/key_to_title",
               ov.title_to_key(ov.key_to_title(k)) == k)

    _reset_store()
    lt.remember_many([
        {"category": "identity", "key": "name", "value": "Archit"},
        {"category": "relationships", "key": "sister", "value": "Priya"},
    ])
    identity = lt.get_category("identity")
    record(
        "hub-link sections never appear as identity facts",
        set(identity.keys()) == {"name"},
        f"got {list(identity.keys())}",
    )


def test_hub_links_track_relationships():
    _reset_store()
    lt.remember_many([
        {"category": "identity", "key": "name", "value": "Archit"},
        {"category": "relationships", "key": "sister", "value": "Priya"},
    ])
    _, _, managed, _ = ov.read_note(lt._aggregate_path("identity"))
    record("Me.md links to the new relationship note", "[[Sister]]" in managed, managed)

    lt.forget("sister", "relationships")
    _, _, managed2, _ = ov.read_note(lt._aggregate_path("identity"))
    record("removing a relationship removes its link from Me.md", "[[Sister]]" not in managed2, managed2)
    record("no leftover project-links section", "project_links" not in managed2.lower().replace(" ", "_"))


def test_existing_projects_folder_migrates_to_notes():
    """Simulates a vault created before this change: a real Projects/*.md
    note already on disk. The migration should fold it into Notes.md and
    remove the folder, not leave it orphaned and unreadable."""
    tmp_dir = _reset_store()
    projects_dir = lt.VAULT_PATH / "Projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    ov.write_note(
        projects_dir / "Favorite Project.md",
        {"type": "project"}, "not specified yet", title="Favorite Project",
    )

    mem = lt.get_all()  # triggers the one-time migration
    record(
        "pre-existing Projects/*.md content is folded into Notes.md",
        mem["notes"].get("favorite_project", {}).get("value") == "not specified yet",
        f"got notes={mem['notes']}",
    )
    record("the old Projects/ folder is removed after migrating", not projects_dir.exists())
    record("a migration marker prevents it from running again",
           (lt.VAULT_PATH / "_friday" / ".projects_migrated").exists())


def test_hand_edits_survive_a_friday_rewrite():
    """The actual promise of this feature: opening a note in Obsidian and
    adding your own content must never be at risk of FRIDAY silently
    wiping it out on a later update."""
    _reset_store()
    lt.remember_many([{"category": "relationships", "key": "sister", "value": "Priya, lives in Mumbai"}])

    note_path = lt._entity_path("relationships", "sister")
    text = note_path.read_text()
    text = text.replace("# Sister", "# Sister (Priya)")
    text += "\nShe's getting married in December — plan the trip.\n"
    note_path.write_text(text)

    # FRIDAY learns something new about her and updates the fact.
    lt.remember_many([{"category": "relationships", "key": "sister", "value": "Priya, works as a doctor in Mumbai"}])

    final = note_path.read_text()
    record("user's renamed heading survives a later FRIDAY update", "# Sister (Priya)" in final)
    record("user's own paragraph survives a later FRIDAY update", "getting married in December" in final)
    record("the actual new fact was saved correctly", "works as a doctor" in final)


def test_forget_deletes_clean_notes_but_preserves_hand_edited_ones():
    _reset_store()
    lt.remember_many([
        {"category": "relationships", "key": "colleague", "value": "works with the user on FRIDAY"},
        {"category": "relationships", "key": "friend", "value": "college friend"},
    ])
    friend_path = lt._entity_path("relationships", "friend")
    friend_path.write_text(friend_path.read_text() + "\nKnown since 2015, met at a hackathon.\n")

    lt.forget("colleague", "relationships")
    lt.forget("friend", "relationships")

    record("a note with no user edits is deleted entirely on forget",
           not lt._entity_path("relationships", "colleague").exists())
    record("a note WITH user edits survives forget (just loses the managed fact)",
           friend_path.exists() and "Known since 2015" in friend_path.read_text())


def test_legacy_json_migrates_exactly_once():
    tmp_dir = _reset_store()
    legacy = tmp_dir / "long_term.json"
    legacy.write_text(
        '{"identity": {"name": {"value": "TestUser", "updated": "2026-01-01"}}, '
        '"relationships": {}, "preferences": {}, "projects": {}, "wishes": {}, "notes": {}}'
    )
    lt._LEGACY_MEMORY_PATH = legacy

    mem = lt.get_all()
    record("legacy JSON facts are migrated into the vault on first load",
           mem["identity"].get("name", {}).get("value") == "TestUser")
    record("a migration marker is written so it won't re-run",
           (lt.VAULT_PATH / "_friday" / ".migrated").exists())


if __name__ == "__main__":
    test_vault_structure_and_file_placement()
    test_projects_category_redirects_to_notes()
    test_reserved_link_keys_round_trip_and_never_leak()
    test_hub_links_track_relationships()
    test_hand_edits_survive_a_friday_rewrite()
    test_forget_deletes_clean_notes_but_preserves_hand_edited_ones()
    test_legacy_json_migrates_exactly_once()
    test_existing_projects_folder_migrates_to_notes()

    print()
    print("=== SUMMARY ===")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total")
        sys.exit(1)
    print("ALL PASS")
