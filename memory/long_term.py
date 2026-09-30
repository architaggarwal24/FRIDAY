"""
memory/long_term.py  —  F.R.I.D.A.Y. Long-Term Memory
Backed by a real Obsidian vault (memory/obsidian_vault.py) — plain
markdown notes with frontmatter and [[wikilinks]], not an opaque JSON
blob. Open config.obsidian_vault_path in Obsidian to browse, search,
graph, and hand-edit exactly what FRIDAY knows.

Storage mapping:
  - identity                 -> People/Me.md             (aggregate, H2 sections)
  - preferences/wishes/notes -> Preferences.md/Wishes.md/Notes.md (aggregate, H2 sections)
  - relationships             -> People/<Name>.md         (one note per key)

People/Me.md also carries auto-generated links to every relationship
and project note, so the vault's graph view actually shows a connected
picture rather than isolated notes — that's the point of using Obsidian
here rather than just prettier JSON.

FAISS + sentence-transformers — semantic retrieval (optional, degrades
gracefully; see format_for_prompt()).

This replaces the upsert_fact/build_memory_context path for long-term
facts. SQLite (memory_store.py) is kept for: conversation log, session
summaries, action errors — unrelated to this module.
"""

import json
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys

from memory import obsidian_vault as ov


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()


def _get_vault_path() -> Path:
    try:
        from config import config
        return config.obsidian_vault_path
    except Exception:
        return BASE_DIR / "FRIDAY_Brain"


VAULT_PATH = _get_vault_path()

# Legacy JSON store — read once for migration, then left alone.
_LEGACY_MEMORY_PATH = BASE_DIR / "memory" / "long_term.json"

INDEX_PATH = BASE_DIR / "memory" / "lt_faiss.index"
META_PATH = BASE_DIR / "memory" / "lt_faiss_meta.json"

_lock = Lock()
MAX_VALUE_LENGTH = 600
TOP_K_RETRIEVE = 20

VALID_CATEGORIES = {"identity", "preferences", "relationships", "wishes", "notes"}

# Aggregate categories all live in one H2-sectioned note; entity
# categories get one note per key, in the given folder.
_AGGREGATE_NOTE = {
    "identity": "People/Me.md",
    "preferences": "Preferences.md",
    "wishes": "Wishes.md",
    "notes": "Notes.md",
}
_ENTITY_FOLDER = {
    "relationships": "People",
}
_ENTITY_TYPE = {"relationships": "person"}

# Reserved section keys inside People/Me.md used for auto-generated
# links — filtered out of get_category("identity") so they never show
# up mixed in with real facts. Must be values that key_to_title() then
# title_to_key() round-trips back to unchanged (no double underscores —
# those collapse to single spaces and don't come back the same way).
_LINKS_KEY = {"relationships": "people_links"}


# ---------------------------------------------------------------------------
# FAISS (optional)
# ---------------------------------------------------------------------------

def _faiss_available() -> bool:
    try:
        import faiss                                             # noqa
        from sentence_transformers import SentenceTransformer    # noqa
        return True
    except ImportError:
        return False


_encoder = None


def _get_encoder():
    global _encoder
    if _encoder is None:
        from sentence_transformers import SentenceTransformer
        _encoder = SentenceTransformer("all-MiniLM-L6-v2")
    return _encoder


def _embed(texts: list[str]):
    enc = _get_encoder()
    return enc.encode(texts, convert_to_numpy=True, normalize_embeddings=True).astype("float32")


def _build_index(entries: list[tuple]) -> None:
    if not entries:
        return
    try:
        import faiss
        texts = [f"{cat} {key}: {_entry_value(e)}" for cat, key, e in entries]
        meta  = [{"cat": cat, "key": key} for cat, key, _ in entries]
        vecs  = _embed(texts)
        index = faiss.IndexFlatIP(vecs.shape[1])
        index.add(vecs)
        faiss.write_index(index, str(INDEX_PATH))
        META_PATH.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[LongTerm] FAISS build failed: {e}")


def _search_index(query: str, k: int = TOP_K_RETRIEVE):
    """Returns a list of hits, or None if semantic search couldn't run at
    all this time (index missing/not yet built, embedding call failed —
    e.g. no network to fetch the model on first use, a corrupted index
    file, anything). None is a distinct signal from an empty list: an
    empty list means "searched successfully, nothing indexed yet",
    which callers can trust; None means "couldn't search", which
    callers must NOT treat as "nothing relevant exists" — see
    format_for_prompt(), which falls back to showing everything rather
    than silently hiding every non-identity fact because search failed.
    """
    try:
        import faiss
        if not INDEX_PATH.exists():
            return None
        index = faiss.read_index(str(INDEX_PATH))
        meta  = json.loads(META_PATH.read_text(encoding="utf-8"))
        _, idxs = index.search(_embed([query]), min(k, index.ntotal))
        return [meta[i] for i in idxs[0] if 0 <= i < len(meta)]
    except Exception as e:
        print(f"[LongTerm] Semantic search unavailable this time ({e}) — showing all facts instead")
        return None


# ---------------------------------------------------------------------------
# Vault-backed storage
# ---------------------------------------------------------------------------

def _entry_value(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("value", ""))
    return str(entry)


def _truncate(val: str) -> str:
    if len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _entity_path(category: str, key: str) -> Path:
    folder = VAULT_PATH / _ENTITY_FOLDER[category]
    return folder / (ov._safe_filename(ov.key_to_title(key)) + ".md")


def _aggregate_path(category: str) -> Path:
    return VAULT_PATH / _AGGREGATE_NOTE[category]


def _load_unlocked() -> dict:
    """Reads the entire vault into the same {category: {key: {"value",
    "updated"}}} shape the rest of this module (and every caller) has
    always worked with — the storage format changed, this shape didn't."""
    ov.ensure_vault(VAULT_PATH)
    memory = {c: {} for c in VALID_CATEGORIES}

    for cat in ("identity", "preferences", "wishes", "notes"):
        _, _, managed, _ = ov.read_note(_aggregate_path(cat))
        sections = ov.parse_sections(managed)
        for reserved in _LINKS_KEY.values():
            sections.pop(reserved, None)
        memory[cat] = sections

    for cat, folder_name in _ENTITY_FOLDER.items():
        folder = VAULT_PATH / folder_name
        for path in ov.list_entity_notes(folder):
            if cat == "relationships" and path.name == "Me.md":
                continue
            fm, _, managed, _ = ov.read_note(path)
            if not managed.strip():
                continue
            key = ov.title_to_key(path.stem)
            memory[cat][key] = {"value": managed.strip(), "updated": fm.get("updated", "")}

    return memory


def _all_entries(memory: dict) -> list[tuple]:
    out = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                out.append((cat, key, entry))
    return out


def _rebuild_hub_links(memory: dict) -> None:
    """Regenerates the auto-generated wikilink section in People/Me.md
    pointing at every relationship note — this is what makes the
    vault's graph view show a connected picture instead of isolated
    notes. Cheap enough to just rerun on every write."""
    people_links = "\n".join(
        f"- [[{ov.key_to_title(k)}]]" for k in memory.get("relationships", {})
    ) or "(none yet)"

    me_path = _aggregate_path("identity")
    _, _, managed, _ = ov.read_note(me_path)
    sections = ov.parse_sections(managed)
    sections[_LINKS_KEY["relationships"]] = {"value": people_links, "updated": datetime.now().strftime("%Y-%m-%d")}
    ov.write_note(me_path, {"type": "person", "relation": "self"}, ov.render_sections(sections), title="Me")


def _write_entry(category: str, key: str, value: str) -> None:
    if category in _AGGREGATE_NOTE:
        path = _aggregate_path(category)
        fm_type = "identity" if category == "identity" else category
        _, _, managed, _ = ov.read_note(path)
        sections = ov.parse_sections(managed)
        reserved = {k: v for k, v in sections.items() if k in _LINKS_KEY.values()}
        sections[key] = {"value": value, "updated": datetime.now().strftime("%Y-%m-%d")}
        sections.update(reserved)  # keep link sections intact, not treated as real facts
        title = "Me" if category == "identity" else category.title()
        ov.write_note(path, {"type": fm_type}, ov.render_sections(sections), title=title)
    else:
        path = _entity_path(category, key)
        extra_fm = {"relation": key} if category == "relationships" else {"category": key}
        ov.write_note(
            path,
            {"type": _ENTITY_TYPE[category], **extra_fm},
            value,
            title=ov.key_to_title(key),
        )


def _delete_entry(category: str, key: str) -> None:
    if category in _AGGREGATE_NOTE:
        path = _aggregate_path(category)
        _, _, managed, _ = ov.read_note(path)
        sections = ov.parse_sections(managed)
        if key in sections:
            del sections[key]
            title = "Me" if category == "identity" else category.title()
            fm_type = "identity" if category == "identity" else category
            ov.write_note(path, {"type": fm_type}, ov.render_sections(sections), title=title)
    else:
        ov.clear_managed_block(_entity_path(category, key))


# ---------------------------------------------------------------------------
# One-time migration from the old JSON store
# ---------------------------------------------------------------------------

def _migrate_legacy_json_if_needed() -> None:
    marker = VAULT_PATH / "_friday" / ".migrated"
    if marker.exists() or not _LEGACY_MEMORY_PATH.exists():
        return
    try:
        data = json.loads(_LEGACY_MEMORY_PATH.read_text(encoding="utf-8"))
        facts = []
        for cat, items in data.items():
            if cat not in VALID_CATEGORIES or not isinstance(items, dict):
                continue
            for key, entry in items.items():
                val = _entry_value(entry)
                if val:
                    facts.append({"category": cat, "key": key, "value": val})
        if facts:
            count, snapshot = _remember_many_locked(facts)
            if snapshot is not None and _faiss_available():
                _build_index(snapshot)
            print(f"[LongTerm] Migrated {count} facts from long_term.json into the Obsidian vault.")
    except Exception as e:
        print(f"[LongTerm] Legacy JSON migration skipped: {e}")
    finally:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat(), encoding="utf-8")


def _migrate_projects_folder_if_needed() -> None:
    """'projects' stopped being its own category (folded into notes) —
    one-time move of any existing Projects/*.md notes into Notes.md,
    then remove the now-unused folder. Guarded by a marker so this only
    ever runs once, same pattern as the legacy JSON migration above."""
    marker = VAULT_PATH / "_friday" / ".projects_migrated"
    old_folder = VAULT_PATH / "Projects"
    if marker.exists():
        return
    if old_folder.exists():
        try:
            moved = 0
            for path in ov.list_entity_notes(old_folder):
                _, _, managed, _ = ov.read_note(path)
                value = managed.strip()
                if value:
                    key = ov.title_to_key(path.stem)
                    _write_entry("notes", key, value)
                    moved += 1
            import shutil
            shutil.rmtree(old_folder, ignore_errors=True)
            if moved:
                print(f"[LongTerm] Moved {moved} note(s) from Projects/ into Notes.md (projects is no longer a separate category).")
        except Exception as e:
            print(f"[LongTerm] Projects folder migration skipped: {e}")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now().isoformat(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def remember(key: str, value: str, category: str = "notes") -> bool:
    """Save a single fact. Category defaults to 'notes'. Thread-safe.
    Returns True if something was actually written, False if the input
    was invalid (empty key/value) or identical to what's already stored
    (nothing to change) — callers should not report success on False."""
    if not key or not value:
        return False
    if category not in VALID_CATEGORIES:
        category = "notes"
    snapshot = None
    saved = False
    with _lock:
        memory   = _load_unlocked()
        new_val  = _truncate(str(value).strip())
        existing = memory[category].get(key, {})
        if not isinstance(existing, dict) or existing.get("value") != new_val:
            _write_entry(category, key, new_val)
            if category in _ENTITY_FOLDER:
                memory[category][key] = {"value": new_val}
                _rebuild_hub_links(memory)
            memory = _load_unlocked()
            snapshot = _all_entries(memory)
            saved = True
            print(f"[LongTerm] 💾 {category}/{key} = {new_val}")
    if snapshot is not None and _faiss_available():
        _build_index(snapshot)
    return saved


def remember_many(facts: list[dict]) -> int:
    """
    Save multiple facts at once. Thread-safe.
    Each dict: {"category": str, "key": str, "value": str}
    Returns count of facts saved.
    """
    if not facts:
        return 0
    with _lock:
        count, snapshot = _remember_many_locked(facts)
    if snapshot is not None and _faiss_available():
        _build_index(snapshot)
    return count


def _remember_many_locked(facts: list[dict]):
    """Inner implementation — caller must hold _lock. Returns (count, snapshot_or_None)."""
    memory  = _load_unlocked()
    changed = 0
    touched_entity_cats = set()
    for f in facts:
        cat = f.get("category", "notes")
        key = str(f.get("key", "")).strip()
        val = str(f.get("value", "")).strip()
        if not key or not val:
            continue
        if cat not in VALID_CATEGORIES:
            cat = "notes"
        new_val  = _truncate(val)
        existing = memory[cat].get(key, {})
        if not isinstance(existing, dict) or existing.get("value") != new_val:
            _write_entry(cat, key, new_val)
            memory[cat][key] = {"value": new_val}
            changed += 1
            if cat in _ENTITY_FOLDER:
                touched_entity_cats.add(cat)
            print(f"[LongTerm] 💾 {cat}/{key} = {new_val}")
    if changed:
        if touched_entity_cats:
            _rebuild_hub_links(_load_unlocked())
        return changed, _all_entries(_load_unlocked())
    return changed, None


def forget(key: str, category: str = "notes") -> bool:
    with _lock:
        memory = _load_unlocked()
        if key in memory.get(category, {}):
            _delete_entry(category, key)
            if category in _ENTITY_FOLDER:
                _rebuild_hub_links(_load_unlocked())
            return True
    return False


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

def load() -> dict:
    with _lock:
        _migrate_legacy_json_if_needed()
        _migrate_projects_folder_if_needed()
        return _load_unlocked()


def get_all() -> dict:
    return load()


def get_category(category: str) -> dict:
    """Returns {key: value_str} for a category."""
    memory = load()
    return {
        k: _entry_value(v)
        for k, v in memory.get(category, {}).items()
        if isinstance(v, dict) and v.get("value")
    }


def _notify_galaxy_of_semantic_match(hit_set: set) -> None:
    """Tells the galaxy panel (if it happens to be open) which real
    graph notes this turn's semantic search actually used, so it can
    fly the camera to them the same way clicking them by hand would.
    Same broadcast_from_thread() pattern used everywhere else for
    cross-thread UI events (voice/tts.py, agent/task_queue.py, ...) —
    this runs from whatever thread is building the prompt, not
    necessarily the asyncio loop the WS server lives on.
    Best-effort and silent: no UI server running, no WS clients
    connected, nothing actually matched, or the import failing outright
    (e.g. called from a test with no ui/ package around) are all fine,
    not errors — this is a nice-to-have visualization hook, never
    something the memory system itself should fail over."""
    if not hit_set:
        return
    try:
        from memory.graph_export import node_id_for
        from ui.ws_server import broadcast_from_thread
    except Exception:
        return
    node_ids = sorted({node_id_for(cat, key) for cat, key in hit_set})
    if not node_ids:
        return
    broadcast_from_thread({"event": "memory_match", "node_ids": node_ids})


def format_for_prompt(query: str = "") -> str:
    """
    Returns structured memory block for system prompt injection.
    Uses semantic retrieval if FAISS available and query is provided.
    Always includes all identity entries regardless.
    """
    memory = load()
    entries = _all_entries(memory)

    if not entries:
        return ""

    use_semantic = _faiss_available() and bool(query)

    selected = entries  # default: show everything (also the correct
                         # behavior when semantic search can't run)
    mode = ""
    if use_semantic:
        hits = _search_index(query, k=TOP_K_RETRIEVE)
        if hits is not None:
            hit_set = {(h["cat"], h["key"]) for h in hits}
            selected = [
                (c, k, e) for c, k, e in entries
                if c == "identity" or (c, k) in hit_set
            ]
            mode = " [semantic]"
            _notify_galaxy_of_semantic_match(hit_set)

    if not selected:
        return ""

    grouped: dict[str, list[str]] = {}
    for cat, key, entry in selected:
        val = _entry_value(entry)
        if val:
            grouped.setdefault(cat, []).append(
                f"{key.replace('_', ' ').title()}: {val}"
            )

    CAT_LABELS = {
        "identity":      "About the user",
        "preferences":   "Preferences",
        "relationships": "People in their life",
        "wishes":        "Wishes / plans",
        "notes":         "Other notes",
    }

    lines = []
    for cat in ["identity", "preferences", "relationships", "wishes", "notes"]:
        items = grouped.get(cat, [])
        if not items:
            continue
        lines.append(f"{CAT_LABELS[cat]}:")
        for item in items:
            lines.append(f"  - {item}")
        lines.append("")

    if not lines:
        return ""

    return f"[MEMORY{mode} — use naturally, never recite like a list]\n" + "\n".join(lines) + "\n"


def as_natural_summary() -> list:
    """
    Returns a plain readable summary of everything known.
    Used for recall responses — no category headers, just facts.
    """
    memory = load()
    parts  = []

    identity = get_category("identity")
    if identity.get("name"):     parts.append(f"name is {identity['name']}")
    if identity.get("age"):      parts.append(f"{identity['age']} years old")
    if identity.get("city"):     parts.append(f"based in {identity['city']}")
    if identity.get("job"):      parts.append(f"works as {identity['job']}")
    if identity.get("language"): parts.append(f"speaks {identity['language']}")
    for k, v in identity.items():
        if k not in ("name", "age", "city", "job", "language") and v:
            parts.append(f"{k.replace('_', ' ')} is {v}")

    prefs = get_category("preferences")
    for k, v in list(prefs.items())[:5]:
        parts.append(f"favorite {k.replace('_', ' ')} is {v}")

    rels = get_category("relationships")
    for k, v in list(rels.items())[:3]:
        parts.append(f"{k} is {v}")

    wishes = get_category("wishes")
    for k, v in list(wishes.items())[:3]:
        parts.append(f"wants to {v}")

    notes = get_category("notes")
    for k, v in list(notes.items())[:3]:
        parts.append(v)

    return parts  # caller assembles into sentence
