"""
test_injection_fixes.py — regression tests for the shell / PowerShell /
AppleScript injection fixes in:
    actions/computer_control.py  (_focus_window)
    brain/handlers.py            (handle_open_app, handle_reminder,
                                   handle_computer_settings)
    verifier/core.py             (_verify_reminder)

Run from the project root:
    python tests/test_injection_fixes.py

Each test tries a payload that WOULD have broken out of the old
f-string-built command. A PASS means no canary file was created — i.e.
the injected command never ran. Every test cleans up anything it
creates (temp files, scheduled tasks).

Windows-only tests are skipped automatically on other platforms.
"""

import asyncio
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OS = platform.system()
TEMP = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp")

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def test_focus_window():
    if OS != "Windows":
        print("[SKIP] _focus_window — not running on Windows")
        return
    from actions.computer_control import _focus_window

    canary = TEMP / "FRIDAY_PWNED_focus.txt"
    canary.unlink(missing_ok=True)
    payload = f'x"); New-Item -ItemType File -Path "{canary}" -Force; ("'
    _focus_window(payload)
    time.sleep(1)
    ok = not canary.exists()
    record("_focus_window (Windows)", ok, "canary file was created" if not ok else "no canary")
    canary.unlink(missing_ok=True)


def test_open_app():
    if OS != "Windows":
        print("[SKIP] handle_open_app — not running on Windows")
        return
    from brain.handlers import handle_open_app

    canary = TEMP / "FRIDAY_PWNED_openapp.txt"
    canary.unlink(missing_ok=True)
    # Forces Python's repr() into double-quote wrapping, which is what
    # made the old `Start-Process {repr(app_name)}` string vulnerable to
    # PowerShell's $(...) subexpression evaluation inside double quotes.
    payload = f"x' $(New-Item {canary} -Force)"
    asyncio.run(handle_open_app(payload))
    time.sleep(1)
    ok = not canary.exists()
    record("handle_open_app (Windows, repr()-PowerShell path)", ok,
           "canary file was created" if not ok else "no canary")
    canary.unlink(missing_ok=True)


def test_computer_settings_fallback():
    from brain.handlers import handle_computer_settings

    canary = TEMP / "FRIDAY_PWNED_settings.txt"
    canary.unlink(missing_ok=True)
    payload = f'not a real setting; New-Item -ItemType File -Path "{canary}" -Force'
    result = asyncio.run(handle_computer_settings({"description": payload}))
    time.sleep(1)
    ok = (not canary.exists()) and ("not sure how to handle" in result.lower())
    record("handle_computer_settings (raw-exec fallback removed)", ok, f"response: {result!r}")
    canary.unlink(missing_ok=True)


def test_verify_reminder_rejects_malformed():
    from verifier.core import Verifier

    v = Verifier()
    r = v._verify_reminder({"date": '2026-01-01"); calc; ("', "time": "09:00"}, "Reminder set")
    ok = r.status.name == "INCONCLUSIVE"
    record("_verify_reminder (malformed date rejected pre-subprocess)", ok, f"status={r.status.name}, reason={r.reason!r}")


def test_reminder_message_is_inert_data():
    if OS != "Windows":
        print("[SKIP] handle_reminder — not running on Windows")
        return
    from brain.handlers import handle_reminder

    canary = TEMP / "FRIDAY_PWNED_reminder.txt"
    canary.unlink(missing_ok=True)
    payload_msg = f'x"); New-Item -ItemType File -Path "{canary}" -Force; ("'
    date, time_str = "2099-01-01", "00:00"

    asyncio.run(handle_reminder({"date": date, "time": time_str, "message": payload_msg}))
    time.sleep(1)

    no_canary = not canary.exists()

    task_name = f"FRIDAY_Reminder_{date.replace('-', '')}_{time_str.replace(':', '')}"
    reminders_dir = Path.home() / ".friday" / "reminders"
    msg_file = reminders_dir / f"{task_name}.msg.txt"
    stored_verbatim = msg_file.exists() and msg_file.read_text(encoding="utf-8") == payload_msg

    ok = no_canary and stored_verbatim
    record("handle_reminder (message stored as inert data, not executed)", ok,
           f"no canary: {no_canary}, message stored verbatim on disk: {stored_verbatim}")

    # Cleanup: this test creates a REAL scheduled task — remove it.
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'Unregister-ScheduledTask -TaskName "{task_name}" -Confirm:$false -ErrorAction SilentlyContinue'],
        capture_output=True,
    )
    for f in reminders_dir.glob(f"{task_name}.*"):
        try:
            f.unlink()
        except Exception:
            pass
    canary.unlink(missing_ok=True)


if __name__ == "__main__":
    print(f"Running on: {OS}\n")
    test_focus_window()
    test_open_app()
    test_computer_settings_fallback()
    test_verify_reminder_rejects_malformed()
    test_reminder_message_is_inert_data()

    print("\n=== SUMMARY ===")
    if results:
        all_pass = all(ok for _, ok, _ in results)
        print("ALL PASS" if all_pass else "SOME FAILED — see [FAIL] lines above")
    else:
        print("No applicable tests ran on this platform.")
