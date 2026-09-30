"""
actions/reminder_store.py — structured index of pending reminders.

handle_reminder() in brain/handlers.py creates the actual Windows
Scheduled Task plus a .msg.txt/.ps1 file pair under ~/.friday/reminders/
— that's what actually fires the reminder. This module is a small,
easily-queryable index alongside them (~/.friday/reminders/index.json),
mirroring actions/topic_monitor.py's monitors.json pattern, so "what
reminders do I have" doesn't need to shell out to schtasks every time,
and removal can clean up the scheduled task + files + index entry
together in one place.
"""

import json
import os
import platform
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from utils.atomic_write import atomic_write_json

REMINDERS_DIR = Path.home() / ".friday" / "reminders"
_INDEX_PATH = REMINDERS_DIR / "index.json"
_lock = threading.Lock()


def _load() -> list:
    try:
        if _INDEX_PATH.exists():
            return json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save(entries: list) -> None:
    with _lock:
        REMINDERS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_INDEX_PATH, entries, indent=2, ensure_ascii=False)


def _cleanup_files(task_name: str) -> None:
    if not task_name:
        return
    for suffix in (".msg.txt", ".ps1"):
        (REMINDERS_DIR / f"{task_name}{suffix}").unlink(missing_ok=True)


def add_reminder(task_name: str, date: str, time_str: str, message: str) -> None:
    entries = _load()
    entries = [e for e in entries if e.get("task_name") != task_name]  # replace if re-set for the same slot
    entries.append({
        "task_name": task_name,
        "date": date,
        "time": time_str,
        "message": message,
        "created": datetime.now().isoformat(),
    })
    _save(entries)


def list_reminders() -> list:
    """Returns pending (not-yet-due) reminders, soonest first. Expired
    ones are pruned — index entry and backing files removed — as a side
    effect of listing, so the returned list is always honest without
    needing a separate cleanup job."""
    entries = _load()
    now = datetime.now()
    pending, expired = [], []
    for e in entries:
        try:
            due = datetime.strptime(f"{e['date']} {e['time']}", "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            expired.append(e)  # malformed entry — drop it
            continue
        (pending if due > now else expired).append(e)

    if expired:
        for e in expired:
            _cleanup_files(e.get("task_name", ""))
        _save(pending)

    pending.sort(key=lambda e: f"{e['date']} {e['time']}")
    return pending


def remove_reminder(task_name: str) -> bool:
    """Unregisters the Windows Scheduled Task, removes the backing
    files, and drops the index entry. Returns True if it found and
    removed something."""
    entries = _load()
    match = next((e for e in entries if e.get("task_name") == task_name), None)
    if not match:
        return False

    if platform.system() == "Windows":
        # Same env-var-indirection pattern as handle_reminder itself —
        # task_name is passed as data, never spliced into the script text.
        env = os.environ.copy()
        env["FRIDAY_R_TASK"] = task_name
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 'Unregister-ScheduledTask -TaskName $env:FRIDAY_R_TASK -Confirm:$false -ErrorAction SilentlyContinue'],
                capture_output=True, timeout=8, env=env,
            )
        except Exception:
            pass

    _cleanup_files(task_name)
    entries = [e for e in entries if e.get("task_name") != task_name]
    _save(entries)
    return True


def find_by_query(query: str) -> list:
    """Loose match against message text or date/time — lets voice-driven
    removal ('cancel my dentist reminder') work without needing the
    exact task_name."""
    q = (query or "").strip().lower()
    if not q:
        return []
    return [
        e for e in list_reminders()
        if q in e.get("message", "").lower() or q in e.get("date", "") or q in e.get("time", "")
    ]
