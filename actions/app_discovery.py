"""
actions/app_discovery.py — real installed-app discovery, replacing the
"hardcoded alias map + try five shell strategies and hope" approach that
used to be the whole of handle_open_app.

Two sources, matching what Windows itself considers "installed apps":
  1. Start Menu .lnk shortcuts (per-user + all-users) — covers traditional
     desktop apps (Spotify, VS Code, Chrome, ...).
  2. Packaged/UWP apps via PowerShell `Get-StartApps` — covers Store apps
     and some modern built-ins that don't have a normal .lnk (Calculator,
     Settings, some newer apps) that would otherwise be invisible to a
     shortcut scan.

Cached with a TTL so normal use is an in-memory lookup — a cache miss
costs the Get-StartApps + shortcut-walk cost (a second or two), but that
only happens once per TTL window.

Fuzzy matching is exact -> substring -> best-word difflib, stdlib only —
no new dependency for something SequenceMatcher already handles well
enough for typo/spacing/partial-name differences. If this proves too weak
in practice (word-order differences, longer natural queries), a dedicated
library like rapidfuzz would be a reasonable upgrade — not adding it
preemptively.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_OS = "Windows" if os.name == "nt" else ("Darwin" if os.uname().sysname == "Darwin" else "Linux")

REFRESH_TTL_SECONDS = 300
FUZZY_CUTOFF = 0.55


@dataclass
class AppEntry:
    name: str
    target: str          # .lnk resolved exe path, OR "shell:AppsFolder\\<AppUserModelID>" for packaged apps
    source: str           # "shortcut" | "packaged"


_cache: List[AppEntry] = []
_cache_built_at: float = 0.0


def _resolve_lnk(lnk_path: Path) -> Optional[str]:
    """Resolve a .lnk shortcut's target exe path via the WScript.Shell COM
    object — the standard way to read shortcut targets on Windows, and
    pywin32 (which provides this) is already a dependency here for
    Spotify window focus."""
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(lnk_path))
        target = shortcut.Targetpath
        return target if target else None
    except Exception:
        return None


def _scan_shortcuts() -> List[AppEntry]:
    entries: List[AppEntry] = []
    search_dirs = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    seen_names = set()
    for base in search_dirs:
        if not base.exists():
            continue
        try:
            for lnk in base.rglob("*.lnk"):
                name = lnk.stem
                key = name.lower()
                if key in seen_names:
                    continue
                target = _resolve_lnk(lnk)
                if not target:
                    continue
                entries.append(AppEntry(name=name, target=target, source="shortcut"))
                seen_names.add(key)
        except Exception as e:
            logger.debug(f"[AppDiscovery] Shortcut scan failed for {base}: {e}")
    return entries


def _scan_packaged_apps() -> List[AppEntry]:
    """Get-StartApps covers packaged/UWP apps (Store apps, some modern
    built-ins) that don't show up as .lnk files at all. Launched via
    `explorer.exe shell:AppsFolder\\<AppUserModelID>`, Windows' own
    documented way to launch a packaged app by ID."""
    try:
        script = (
            "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            data = [data]
        entries = []
        for item in data:
            name = item.get("Name", "").strip()
            app_id = item.get("AppID", "").strip()
            if name and app_id:
                entries.append(AppEntry(name=name, target=f"shell:AppsFolder\\{app_id}", source="packaged"))
        return entries
    except Exception as e:
        logger.debug(f"[AppDiscovery] Get-StartApps failed: {e}")
        return []


def _rebuild_cache() -> List[AppEntry]:
    if _OS != "Windows":
        return []  # shortcut/AppsFolder scanning is Windows-specific; other OSes keep using the old strategies
    t0 = time.time()
    entries = _scan_shortcuts() + _scan_packaged_apps()
    logger.info(f"[AppDiscovery] Indexed {len(entries)} apps in {time.time() - t0:.1f}s")
    return entries


def get_app_registry(force_refresh: bool = False) -> List[AppEntry]:
    global _cache, _cache_built_at
    now = time.time()
    if force_refresh or not _cache or (now - _cache_built_at) > REFRESH_TTL_SECONDS:
        _cache = _rebuild_cache()
        _cache_built_at = now
    return _cache


def fuzzy_resolve(query: str, entries: Optional[List[AppEntry]] = None, cutoff: float = FUZZY_CUTOFF) -> Optional[AppEntry]:
    """Exact match -> substring match -> best-word fuzzy match. Returns
    None if nothing clears the cutoff, which callers should treat as
    'not found' — not a reason to guess."""
    entries = entries if entries is not None else get_app_registry()
    if not entries or not query.strip():
        return None

    query_norm = query.strip().lower()

    # 1. Exact match (case-insensitive).
    for entry in entries:
        if entry.name.lower() == query_norm:
            return entry

    # 2. Substring match — handles partial names like "spot" -> "Spotify".
    # If several entries contain the query, prefer the shortest name (the
    # closest thing to an exact match) rather than guessing which one.
    substring_matches = [e for e in entries if query_norm in e.name.lower()]
    if len(substring_matches) == 1:
        return substring_matches[0]
    if len(substring_matches) > 1:
        return min(substring_matches, key=lambda e: len(e.name))

    # 3. Best fuzzy match — compares the whole query against each
    # candidate name. difflib's ratio is length-normalized, so this
    # naturally favors closer matches over merely-plausible ones.
    best_entry, best_ratio = None, 0.0
    for entry in entries:
        ratio = difflib.SequenceMatcher(None, query_norm, entry.name.lower()).ratio()
        if ratio > best_ratio:
            best_entry, best_ratio = entry, ratio
    if best_entry and best_ratio >= cutoff:
        return best_entry

    return None
