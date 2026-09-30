"""
F.R.I.D.A.Y. — memory/graph_export.py
Flattens the Obsidian vault into a {"nodes": [...], "links": [...]}
shape for the `galaxy` panel's 3D force-directed graph (ui/renderer/
components/Galaxy.jsx). Reuses memory/long_term.py's own file-location
knowledge (_aggregate_path, _ENTITY_FOLDER, _LINKS_KEY, VAULT_PATH) and
memory/obsidian_vault.py's own note reader (read_note) — this module
has no vault-parsing logic of its own beyond finding [[wikilinks]] in
already-extracted text, same way obsidian_vault.py has no notion of
"categories" and long_term.py has no notion of "the graph".

One node per *note* (physical file), not per fact:
  - identity/preferences/wishes/notes are each a single aggregate file
    (People/Me.md, Preferences.md, Wishes.md, Notes.md) -> one node each.
  - relationships get one node per person note under People/, same as
    everywhere else in long_term.py.

Two link sources, both required for this to be a real vault graph and
not just a re-skinned JSON export:
  (a) the auto-generated hub links long_term._rebuild_hub_links() writes
      into People/Me.md's reserved "People Links" section — read straight
      off that section (via the same [[wikilink]] extraction used below)
      so this always matches whatever was actually written, rather than
      re-deriving it from the relationships list independently.
  (b) [[wikilinks]] the user hand-added in any note's free-content zone
      (the part below obsidian_vault.FREE_ZONE_HINT) — this is the whole
      point of a real vault instead of a JSON export: the user's own
      links must show up, not just the auto-generated ones.

A wikilink to a note that doesn't exist (typo, not-yet-created note,
a link into the managed zone that got hand-edited weirdly) is silently
dropped rather than raising — a broken link in a personal notes vault
is normal, not an error condition.
"""

from __future__ import annotations

import re

from memory import long_term as lt
from memory import obsidian_vault as ov

# Matches the target of [[Title]], [[Title|Alias]], and [[Title#Heading]] —
# only the part before a pipe or heading anchor identifies the note.
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")

EXCERPT_LENGTH = 200

# Aggregate categories each live in exactly one file; this is the title
# long_term._write_entry()/_delete_entry() already use for that file's
# `# Heading`, so it's what a hand-written [[wikilink]] to it would say.
_AGGREGATE_TITLE = {
    "identity": "Me",
    "preferences": "Preferences",
    "wishes": "Wishes",
    "notes": "Notes",
}


def node_id_for(cat: str, key: str) -> str:
    """Maps a (category, key) fact reference — the shape long_term.py's
    _all_entries()/_search_index() already use internally — to this
    module's graph node id. Aggregate categories collapse every fact
    into that category's single whole-note id (there's one node for
    all of "preferences", not one per preference); relationships map
    straight through, same key, since each already gets its own note.
    Used by long_term.format_for_prompt() to tell the galaxy panel
    which real graph nodes a semantic-search hit actually corresponds
    to."""
    if cat in _AGGREGATE_TITLE:
        return f"{cat}/{ov.title_to_key(_AGGREGATE_TITLE[cat])}"
    return f"{cat}/{key}"


def _extract_links(text: str) -> list[str]:
    """Returns the note titles referenced via [[wikilink]] in `text`."""
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text or "") if m.group(1).strip()]


def _free_zone(after: str) -> str:
    """The user's own part of a note's free content — below FRIDAY's
    hint line, if it's still there; the whole `after` blob otherwise
    (e.g. someone deleted the hint line by hand but kept writing below
    where it used to be)."""
    after = after or ""
    if ov.FREE_ZONE_HINT in after:
        return after.split(ov.FREE_ZONE_HINT, 1)[1]
    return after


def build_graph() -> dict:
    """Reads the whole vault and returns {"nodes": [...], "links": [...]}.
    Safe to call on an empty/partial vault — missing files just produce
    fewer nodes, never an error."""
    nodes: list[dict] = []
    key_to_id: dict[str, str] = {}    # title_to_key(note title) -> node id
    free_zones: dict[str, str] = {}   # node id -> that note's free content

    # --- Aggregate notes: identity/preferences/wishes/notes, one node each.
    for category, title in _AGGREGATE_TITLE.items():
        path = lt._aggregate_path(category)
        if not path.exists():
            continue
        _, _, managed, after = ov.read_note(path)
        key = ov.title_to_key(title)
        node_id = node_id_for(category, key)
        nodes.append({
            "id": node_id,
            "label": ov.key_to_title(key),
            "group": category,
            "excerpt": managed.strip()[:EXCERPT_LENGTH],
        })
        key_to_id[key] = node_id
        free_zones[node_id] = _free_zone(after)

    # --- Relationships: one node per person note under People/ — same
    # folder + skip-Me.md pattern as long_term._load_unlocked().
    folder = lt.VAULT_PATH / lt._ENTITY_FOLDER["relationships"]
    for path in ov.list_entity_notes(folder):
        if path.name == "Me.md":
            continue
        _, _, managed, after = ov.read_note(path)
        if not managed.strip():
            continue
        key = ov.title_to_key(path.stem)
        node_id = node_id_for("relationships", key)
        nodes.append({
            "id": node_id,
            "label": ov.key_to_title(key),
            "group": "relationships",
            "excerpt": managed.strip()[:EXCERPT_LENGTH],
        })
        key_to_id[key] = node_id
        free_zones[node_id] = _free_zone(after)

    links: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()

    def _add_link(source: str, target: str) -> None:
        if source == target:
            return
        edge = tuple(sorted((source, target)))
        if edge in seen_edges:
            return
        seen_edges.add(edge)
        links.append({"source": source, "target": target})

    # (a) Auto-generated hub links: Me -> every relationship note. Read
    # straight from Me.md's own rendered "People Links" section so this
    # always matches what long_term._rebuild_hub_links() actually wrote.
    me_id = key_to_id.get(ov.title_to_key(_AGGREGATE_TITLE["identity"]))
    if me_id:
        _, _, me_managed, _ = ov.read_note(lt._aggregate_path("identity"))
        sections = ov.parse_sections(me_managed)
        links_section = sections.get(lt._LINKS_KEY["relationships"], {})
        for title in _extract_links(links_section.get("value", "")):
            target = key_to_id.get(ov.title_to_key(title))
            if target:
                _add_link(me_id, target)

    # (b) Hand-added [[wikilinks]] in each note's free zone — the reason
    # this is a real vault graph and not just a re-skinned JSON export.
    for node_id, free_text in free_zones.items():
        for title in _extract_links(free_text):
            target = key_to_id.get(ov.title_to_key(title))
            if target:
                _add_link(node_id, target)

    return {"nodes": nodes, "links": links}
