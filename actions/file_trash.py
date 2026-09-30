"""
actions/file_trash.py — lightweight local trash for file_controller.

Sentinel gates *asking* before a delete happens, but a confirmed delete
still permanently destroyed the file — no recovery path if the wrong
file got confirmed, or if an overwriting write clobbered something that
mattered. This module is the recovery path: before a delete or an
overwriting write actually happens, the previous content is moved/copied
into a local trash directory and recorded in an index. The last N
entries (configurable, config.file_trash_max_entries) are kept; older
ones are pruned automatically.

Every operation here is self-consistent: even restoring a trashed file
never destroys anything either — if something now occupies the restore
target, that gets trashed first too, so nothing is ever silently lost.

Layout:
    <project_root>/.file_trash/
        index.json          — ordered list of trash entries, oldest first
        <uuid>_<name>        — the actual backed-up file/folder content

Index entry shape:
    {
        "id": "<uuid4 hex>",
        "original_path": "C:\\Users\\...\\notes.txt",
        "trash_path": "<project_root>/.file_trash/<uuid>_notes.txt",
        "operation": "delete" | "overwrite",
        "is_dir": false,
        "timestamp": "2026-08-16T12:34:56.789012"
    }
"""

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.atomic_write import atomic_write_json

logger = logging.getLogger("friday.file_trash")


def _trash_dir() -> Path:
    from config import config
    d = config.base_dir / ".file_trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return _trash_dir() / "index.json"


def _max_entries() -> int:
    from config import config
    return max(1, config.file_trash_max_entries)


def _load_index() -> list:
    path = _index_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Trash index unreadable ({e}) — starting fresh. "
                        f"Existing backup files under {_trash_dir()} are untouched, just untracked.")
        return []


def _save_index(entries: list) -> None:
    atomic_write_json(_index_path(), entries, indent=2)


def _prune(entries: list) -> list:
    """Keeps only the most recent max_entries, deleting the backup files
    (not just the index rows) for anything older."""
    limit = _max_entries()
    if len(entries) <= limit:
        return entries
    overflow = entries[:-limit] if limit > 0 else entries
    keep = entries[-limit:] if limit > 0 else []
    for entry in overflow:
        tp = Path(entry.get("trash_path", ""))
        try:
            if tp.is_dir():
                shutil.rmtree(tp, ignore_errors=True)
            elif tp.exists():
                tp.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Could not remove pruned trash entry {tp}: {e}")
    return keep


def _add_entry(original_path: Path, trash_target: Path, operation: str, is_dir: bool) -> dict:
    entries = _load_index()
    entry = {
        "id": uuid.uuid4().hex,
        "original_path": str(original_path),
        "trash_path": str(trash_target),
        "operation": operation,
        "is_dir": is_dir,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    entries.append(entry)
    entries = _prune(entries)
    _save_index(entries)
    return entry


def _unique_trash_target(original_path: Path) -> Path:
    return _trash_dir() / f"{uuid.uuid4().hex}_{original_path.name}"


def trash_before_delete(path: Path) -> Optional[dict]:
    """Moves `path` into trash instead of letting the caller destroy it
    outright. Returns the index entry on success, None if trashing
    itself failed (caller should fall back to send2trash / a direct
    delete rather than silently treating a failed backup as success)."""
    if not path.exists():
        return None
    target = _unique_trash_target(path)
    try:
        shutil.move(str(path), str(target))
        entry = _add_entry(path, target, "delete", path.is_dir())
        logger.info(f"[FileTrash] Trashed (delete) {path} -> {target.name}")
        return entry
    except Exception as e:
        logger.warning(f"[FileTrash] Could not trash {path} before delete: {e}")
        return None


def trash_before_overwrite(path: Path) -> Optional[dict]:
    """Copies the CURRENT content of `path` into trash before it gets
    overwritten. A copy, not a move — the caller is about to write new
    content to this same path right after. Best-effort: a failure here
    is logged but shouldn't block an ordinary write, so this returns
    None on failure rather than raising."""
    if not path.exists() or not path.is_file():
        return None  # nothing to back up — this is a genuinely new file
    target = _unique_trash_target(path)
    try:
        shutil.copy2(str(path), str(target))
        entry = _add_entry(path, target, "overwrite", False)
        logger.info(f"[FileTrash] Trashed (pre-overwrite) {path} -> {target.name}")
        return entry
    except Exception as e:
        logger.warning(f"[FileTrash] Could not back up {path} before overwrite: {e}")
        return None


def undo_last() -> str:
    """Restores the most recently trashed file/folder to its original
    location. If something now occupies that location, it gets trashed
    first too — undo never destroys anything either."""
    entries = _load_index()
    if not entries:
        return "Nothing in the trash to undo, boss."

    entry = entries[-1]
    original = Path(entry["original_path"])
    trash_path = Path(entry["trash_path"])

    if not trash_path.exists():
        # Backup file itself is gone (manually removed, pruned by another
        # process, disk cleanup, etc.) — drop the now-dangling index entry
        # rather than repeatedly failing on it.
        entries.pop()
        _save_index(entries)
        return f"Couldn't undo — the backup for {original.name} is missing. Removed that entry from the trash log."

    # Remove this entry from the index now, before doing anything else —
    # its backup file becomes untracked, so pruning (which only touches
    # indexed entries) can't evict it out from under us. Without this,
    # a very small max_entries could let the "trash whatever's currently
    # occupying the restore path" step below prune away the very backup
    # we're about to restore.
    entries = entries[:-1]
    _save_index(entries)

    if original.exists():
        # Don't clobber whatever's there now — trash it first (works for
        # both files and directories).
        trash_before_delete(original)

    try:
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash_path), str(original))
    except Exception as e:
        return f"Couldn't restore {original.name}: {e}"

    verb = "deletion" if entry["operation"] == "delete" else "overwrite"
    return f"Undone — restored {original.name} (last {verb}), boss."


def last_entry_summary() -> Optional[str]:
    entries = _load_index()
    if not entries:
        return None
    e = entries[-1]
    return f"{Path(e['original_path']).name} ({e['operation']}, {e['timestamp']})"
