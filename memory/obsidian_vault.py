"""
F.R.I.D.A.Y. — memory/obsidian_vault.py
Low-level read/write layer for FRIDAY's memory as a real Obsidian vault
(plain markdown files + YAML frontmatter + [[wikilinks]]) instead of an
opaque JSON blob. Open the vault folder in Obsidian and you're looking
at exactly what FRIDAY knows — browsable, and editable by hand.

Core design rule: FRIDAY must never destroy anything you add by hand.
Every note FRIDAY writes has a clearly marked block:

    <!-- friday:managed:start -->
    ...FRIDAY-owned content...
    <!-- friday:managed:end -->

FRIDAY only ever reads/replaces what's *inside* that block. Frontmatter
and anything below the end marker are yours — rewriting a note always
preserves them untouched. This module has no knowledge of "categories"
or "facts" — that mapping lives in memory/long_term.py, which is the
only other module that should import this one.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional

from utils.atomic_write import atomic_write_text

MANAGED_START = "<!-- friday:managed:start -->"
MANAGED_END = "<!-- friday:managed:end -->"
FREE_ZONE_HINT = "<!-- Anything below this line is yours — FRIDAY won't touch it. -->"

_lock = Lock()

VAULT_FOLDERS = ("People", "Preferences", "Wishes", "Notes")


# ---------------------------------------------------------------------------
# Vault bootstrap
# ---------------------------------------------------------------------------

def ensure_vault(vault_path: Path) -> None:
    """Creates the vault folder structure and a welcome note, if not already
    there. Safe to call on every startup — a no-op once the vault exists."""
    vault_path.mkdir(parents=True, exist_ok=True)
    for folder in VAULT_FOLDERS:
        (vault_path / folder).mkdir(exist_ok=True)

    readme = vault_path / "Start Here.md"
    if not readme.exists():
        atomic_write_text(readme, (
            "---\n"
            "type: friday-info\n"
            "---\n"
            "# FRIDAY's Brain\n\n"
            "This vault *is* FRIDAY's long-term memory — not a copy or an export, "
            "the actual thing it reads from and writes to. Everything under "
            "`People/`, `Preferences/`, `Wishes/`, and `Notes/` "
            "gets created and updated automatically as you talk to FRIDAY.\n\n"
            "You can edit any of these notes yourself. Each one has a clearly "
            f"marked section between `{MANAGED_START}` and `{MANAGED_END}` — "
            "that part is what FRIDAY actively manages and may rewrite. "
            "Anything you add below that block is permanently yours; FRIDAY "
            "will never touch it.\n\n"
            "Open the graph view (the icon in the left ribbon) to see how "
            "everything connects.\n"
        ))


# ---------------------------------------------------------------------------
# Filename <-> key mapping
# ---------------------------------------------------------------------------

def key_to_title(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title()


def title_to_key(title: str) -> str:
    return re.sub(r"\s+", "_", title.strip().lower())


def _safe_filename(title: str) -> str:
    # Strip characters that are awkward/illegal in filenames across OSes,
    # while keeping it readable — this is a display name, not a slug.
    cleaned = re.sub(r'[\\/:*?"<>|#^\[\]]', "", title).strip()
    return cleaned or "untitled"


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Returns (frontmatter_dict, rest_of_file). Minimal YAML — this vault
    only ever writes flat string/bool key: value pairs, so a full YAML
    parser is unnecessary; kept dependency-free and won't choke on
    frontmatter Obsidian plugins may add."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    rest = text[m.end():]
    fm = {}
    for line in raw.split("\n"):
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    return fm, rest


def _render_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Managed-block parsing
# ---------------------------------------------------------------------------

def _split_managed(body: str) -> tuple[str, str, str]:
    """Returns (before_managed, managed_content, after_managed) for the
    part of a note after its frontmatter. If no managed block exists yet,
    managed_content is "" and the whole body is treated as `before`
    (never destroyed — a note with no markers is entirely free content)."""
    start_i = body.find(MANAGED_START)
    end_i = body.find(MANAGED_END)
    if start_i == -1 or end_i == -1 or end_i < start_i:
        return body, "", ""
    before = body[:start_i]
    managed = body[start_i + len(MANAGED_START):end_i].strip("\n")
    after = body[end_i + len(MANAGED_END):]
    return before, managed, after


# ---------------------------------------------------------------------------
# Note read/write
# ---------------------------------------------------------------------------

def read_note(path: Path) -> tuple[dict, str, str, str]:
    """Returns (frontmatter, before_managed, managed_content, after_managed).
    All empty/defaults if the file doesn't exist."""
    if not path.exists():
        return {}, "", "", ""
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)
    before, managed, after = _split_managed(body)
    return fm, before, managed, after


def write_note(path: Path, frontmatter: dict, managed_content: str, title: Optional[str] = None) -> None:
    """Creates or updates a note, replacing ONLY its managed block.
    Existing frontmatter keys not in `frontmatter` are preserved (e.g. if
    you added your own). Free content below the managed block is always
    preserved byte-for-byte. `title` is only used the first time a note
    is created (as its `# Heading`) — never overwritten afterward, so
    renaming the heading yourself sticks.
    """
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_fm, before, _old_managed, after = read_note(path)

        merged_fm = dict(existing_fm)
        merged_fm.update(frontmatter)
        merged_fm["updated"] = datetime.now().strftime("%Y-%m-%d")

        if not before.strip():
            # Fresh note — write a heading and the free-zone hint.
            heading = f"# {title}\n\n" if title else ""
            before = heading
            after = f"\n\n{FREE_ZONE_HINT}\n" if not after.strip() else after

        new_text = (
            _render_frontmatter(merged_fm)
            + before.rstrip("\n") + "\n\n"
            + MANAGED_START + "\n"
            + managed_content.strip("\n") + "\n"
            + MANAGED_END
            + after
        )
        atomic_write_text(path, new_text)


def note_has_free_content(path: Path) -> bool:
    """True if the user has added anything outside the managed block —
    used to decide whether forgetting a fact should delete the file
    entirely or just clear its managed section."""
    if not path.exists():
        return False
    _, before, _, after = read_note(path)
    # Strip at most one auto-generated "# Heading" line from `before`
    # before checking — anything beyond that (including extra text the
    # user typed right after the heading, before the managed block) counts.
    before_lines = before.strip("\n").split("\n")
    if before_lines and before_lines[0].startswith("# "):
        before_lines = before_lines[1:]
    before_remainder = "\n".join(before_lines).strip()
    after_remainder = after.replace(FREE_ZONE_HINT, "").strip()
    return bool(before_remainder) or bool(after_remainder)


def clear_managed_block(path: Path) -> None:
    """Empties a note's managed block but keeps the file (and any free
    content) if it has one; deletes the file entirely if FRIDAY was the
    only thing that ever wrote to it."""
    if not path.exists():
        return
    if note_has_free_content(path):
        fm, before, _, after = read_note(path)
        with _lock:
            new_text = _render_frontmatter(fm) + before.rstrip("\n") + "\n\n" + after.lstrip("\n")
            atomic_write_text(path, new_text)
    else:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Aggregate notes (identity/preferences/wishes/notes) — H2-sectioned
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^## (.+?)\n(?:<!-- updated: (\d{4}-\d{2}-\d{2}) -->\n)?(.*?)(?=\n## |\Z)",
    re.DOTALL | re.MULTILINE,
)


def parse_sections(managed_content: str) -> dict:
    """Parses '## Title\\n<!-- updated: DATE -->\\nvalue' sections into
    {key: {"value": str, "updated": str}}."""
    out = {}
    for m in _SECTION_RE.finditer(managed_content.strip() + "\n"):
        title, updated, value = m.group(1).strip(), m.group(2), m.group(3).strip()
        key = title_to_key(title)
        out[key] = {"value": value, "updated": updated or datetime.now().strftime("%Y-%m-%d")}
    return out


def render_sections(entries: dict) -> str:
    """Inverse of parse_sections(). `entries`: {key: {"value","updated"}}."""
    parts = []
    for key, entry in entries.items():
        title = key_to_title(key)
        updated = entry.get("updated", datetime.now().strftime("%Y-%m-%d"))
        value = entry.get("value", "")
        parts.append(f"## {title}\n<!-- updated: {updated} -->\n{value}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def list_entity_notes(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.glob("*.md"))
