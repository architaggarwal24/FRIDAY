"""
F.R.I.D.A.Y. — utils/atomic_write.py
Crash-safe text/JSON writes for small local state files.

The problem this fixes: `path.write_text(json.dumps(data))` is two steps
(truncate the file, then write the new bytes) with no atomicity between
them. If FRIDAY is killed — crash, forced close, power loss — in that
window, the file is left truncated: invalid JSON that fails to parse on
the next load, silently resetting whatever state lived there (usage
tracking, the file-trash index, reminders, topic watchlist, ...).

The fix is the standard one: write the new content to a temp file in the
*same directory* as the target, then atomically replace the target with
it via os.replace(). Same-directory matters — os.replace()/os.rename()
are only atomic within a single filesystem/volume; a temp file in a
different directory (e.g. the OS temp dir) could be on a different
volume on some setups, silently losing the atomicity guarantee this
whole module exists to provide.

At every point before the final os.replace(), the original file is
completely untouched — a crash mid-write leaves the old (valid) content
in place, never a partial new one. If anything goes wrong before that
point, the temp file is cleaned up and the exception propagates instead
of being swallowed here, so callers keep their existing error handling
(most already wrap these calls in their own try/except).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # ensure bytes are actually on disk, not just buffered
        os.replace(tmp_path, path)  # atomic on both Windows and POSIX
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, encoding: str = "utf-8", **json_kwargs) -> None:
    """Same as atomic_write_text, but json.dumps()s data first. Pass
    indent=, ensure_ascii=, etc. through json_kwargs exactly as you
    would to json.dumps() directly."""
    atomic_write_text(path, json.dumps(data, **json_kwargs), encoding=encoding)
