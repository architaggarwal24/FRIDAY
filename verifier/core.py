"""
verifier/core.py — independent outcome check.

Runs after a tool handler reports success, before that success is spoken
to the user. A handler's own return string is NOT sufficient on its own —
this re-checks real-world state independently (actual running process,
actual volume level, actual file on disk, actual monitor list, actual
scheduled task) rather than trusting what the handler believes happened.

Every verify_* method returns VERIFIED, FAILED, or INCONCLUSIVE — never
forced into a false positive or an unfair negative when there's no
reliable way to check. If a verifier itself throws, that's a bug in this
code, not confirmation of a failure — it's surfaced as INCONCLUSIVE, not
FAILED, so a broken check can never masquerade as "Forge lied."
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Optional

from verifier.models import VerificationResult, VerificationStatus

logger = logging.getLogger(__name__)

VERIFIED = VerificationStatus.VERIFIED
FAILED = VerificationStatus.FAILED
INCONCLUSIVE = VerificationStatus.INCONCLUSIVE


class Verifier:
    def __init__(self) -> None:
        self._registry: dict[str, Callable[[dict, str], VerificationResult]] = {
            "open_app": self._verify_open_app,
            "computer_settings": self._verify_computer_settings,
            "topic_monitor": self._verify_topic_monitor,
            "open_loop": self._verify_open_loop,
            "reminder": self._verify_reminder,
            "calendar": self._verify_calendar,
            "gmail": self._verify_gmail,
            "screenshot": self._verify_screenshot,
            "spotify_control": self._verify_spotify,
            "save_memory": self._verify_save_memory,
            "power_control": self._verify_power_control,
            # Read-only / no independently-checkable side effect — nothing
            # to contradict, so trivially verified rather than skipped
            # silently (keeps the Activity Feed / logs honest about what
            # WAS and wasn't checked).
            "weather_report": self._verify_readonly,
            "google_status": self._verify_readonly,
            "web_search": self._verify_readonly,
            "youtube_video": self._verify_readonly,
            "flight_finder": self._verify_readonly,
            "screen_process": self._verify_readonly,
            "wait": self._verify_readonly,
        }

    def verify(self, action_name: str, args: dict, result_text: str) -> VerificationResult:
        # A handler reporting its OWN failure isn't a false-positive risk —
        # nothing to independently contradict. Simple heuristic on the
        # handler's own wording; a genuine false claim of success won't
        # match this, which is exactly the case verification exists for.
        if re.search(r"\b(failed|error|couldn't|could not|not installed|unavailable)\b", result_text, re.I):
            return VerificationResult(VERIFIED, "Handler already reported failure — nothing to independently contradict.")

        method = self._registry.get(action_name)
        if method is None:
            return VerificationResult(INCONCLUSIVE, f"No verifier registered for action '{action_name}'.")

        try:
            return method(args, result_text)
        except Exception as e:
            logger.warning(f"[Verifier] {action_name} verification itself raised: {e}")
            return VerificationResult(INCONCLUSIVE, f"Verification itself raised an error: {e}")

    # ── Individual verifiers ──────────────────────────────────────────────

    def _verify_readonly(self, args: dict, result_text: str) -> VerificationResult:
        return VerificationResult(VERIFIED, "Read-only action — no side effect to verify.")

    def _verify_open_app(self, args: dict, result_text: str) -> VerificationResult:
        import psutil
        from actions.app_discovery import fuzzy_resolve

        app_name = args.get("app_name") or args.get("description") or ""
        match = fuzzy_resolve(app_name)
        if not match or match.source != "shortcut":
            # Packaged/UWP apps can't be reliably matched to a running
            # process by exe name the way a shortcut's resolved path can —
            # same limitation the source project flagged, not pretending
            # otherwise here either.
            return VerificationResult(
                INCONCLUSIVE,
                "No shortcut-resolved exe path to check against the process list for this app."
            )

        exe_name = match.target.replace("/", "\\").split("\\")[-1].lower()
        running = {p.name().lower() for p in psutil.process_iter(["name"])}
        if exe_name in running:
            return VerificationResult(VERIFIED, f"Confirmed '{exe_name}' is running.")
        return VerificationResult(
            FAILED, f"Reported opening '{exe_name}', but no matching process is running."
        )

    def _verify_computer_settings(self, args: dict, result_text: str) -> VerificationResult:
        desc = (args.get("description") or "").lower()
        value = args.get("value", "")
        if "volume" not in desc and "mute" not in desc:
            # Brightness/wifi/clipboard/darkmode don't have an easy
            # independent read in this codebase yet — honest inconclusive
            # rather than a fabricated check.
            return VerificationResult(INCONCLUSIVE, "No independent check implemented for this settings category yet.")
        try:
            from pycaw.pycaw import AudioUtilities
            vol = AudioUtilities.GetSpeakers().EndpointVolume
            current_pct = round(vol.GetMasterVolumeLevelScalar() * 100)
        except Exception as e:
            return VerificationResult(INCONCLUSIVE, f"Couldn't read back system volume: {e}")

        m = re.search(r"(\d+)", value or desc)
        if m:
            target = min(100, max(0, int(m.group(1))))
            if abs(current_pct - target) <= 2:  # small tolerance for rounding
                return VerificationResult(VERIFIED, f"Volume confirmed at {current_pct}%.")
            return VerificationResult(
                FAILED, f"Asked for {target}%, but system volume actually reads {current_pct}%."
            )
        # Relative change ("up"/"down") or mute/unmute — no absolute
        # target to compare against, just confirm we can read a sane value.
        return VerificationResult(VERIFIED, f"System volume reads {current_pct}% after the change.")

    def _verify_topic_monitor(self, args: dict, result_text: str) -> VerificationResult:
        from actions import topic_monitor
        action = (args.get("action") or "").lower()
        topic = (args.get("topic") or "").strip().lower()
        current = [t.lower() for t in topic_monitor.list_monitors()]

        if action == "add":
            if not topic:
                return VerificationResult(INCONCLUSIVE, "No topic argument to check against the monitor list.")
            if any(topic in t or t in topic for t in current):
                return VerificationResult(VERIFIED, f"'{topic}' is present in the monitor list.")
            return VerificationResult(FAILED, f"Reported adding '{topic}', but it's not in the monitor list.")

        if action in ("remove", "stop"):
            if not topic:
                return VerificationResult(INCONCLUSIVE, "No topic argument to check against the monitor list.")
            if any(topic in t or t in topic for t in current):
                return VerificationResult(FAILED, f"Reported removing '{topic}', but it's still in the monitor list.")
            return VerificationResult(VERIFIED, f"'{topic}' is no longer in the monitor list.")

        return VerificationResult(VERIFIED, "Read-only monitor action (list/check) — no side effect to verify.")

    def _verify_open_loop(self, args: dict, result_text: str) -> VerificationResult:
        from memory import open_loops
        action = (args.get("action") or "").lower()
        text = (args.get("text") or args.get("query") or "").strip().lower()
        current = [l["text"].lower() for l in open_loops.list_open()]

        if action == "add":
            if not text:
                return VerificationResult(INCONCLUSIVE, "No text argument to check against the open-loop list.")
            if any(text in t or t in text for t in current):
                return VerificationResult(VERIFIED, "The loop is present in the open list.")
            return VerificationResult(FAILED, "Reported adding a loop, but it's not in the open list.")

        if action == "resolve":
            if not text:
                return VerificationResult(INCONCLUSIVE, "No text/id argument to check against the open-loop list.")
            if any(text in t or t in text for t in current):
                return VerificationResult(FAILED, "Reported resolving a loop, but a match is still open.")
            return VerificationResult(VERIFIED, "No matching loop remains open.")

        return VerificationResult(VERIFIED, "Read-only open-loop action (list) — no side effect to verify.")

    def _verify_reminder(self, args: dict, result_text: str) -> VerificationResult:
        # handle_reminder redirects "add" to a real Google Calendar event
        # instead of a local Task Scheduler entry whenever an account is
        # connected (see the comment there) — create_event's success
        # string ("Event created for...") is worded distinctly from this
        # tool's own ("Reminder set for..." / "Reminder noted: ...")
        # specifically so this check can tell which system actually got
        # written to. Without this, a successful calendar redirect would
        # get checked against Task Scheduler — where it was deliberately
        # never written — and get reported back as FAILED even though it
        # worked, which is the false-failure mirror of the exact
        # false-success problem this whole verifier exists to catch.
        if result_text.strip().startswith("Event created for"):
            return self._verify_calendar({**args, "action": "create"}, result_text)
        if result_text.strip().startswith("Deleted:"):
            return self._verify_calendar({**args, "action": "delete"}, result_text)

        import os
        import re as _re
        import subprocess
        date = args.get("date", "")
        time_str = args.get("time", "")

        # Same strict validation as the handler — checked independently
        # here too, since this runs on the raw tool-call args regardless
        # of what the handler's result text said.
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not _re.fullmatch(r"\d{2}:\d{2}", time_str):
            return VerificationResult(INCONCLUSIVE, "date/time args aren't in the expected format to check against Task Scheduler.")

        task_name = f"FRIDAY_Reminder_{date.replace('-', '')}_{time_str.replace(':', '')}"
        try:
            # task_name is passed via an environment variable, never spliced
            # into the script text, so it can't be parsed as PowerShell
            # syntax — same fix as _focus_window / handle_reminder.
            env = os.environ.copy()
            env["FRIDAY_R_TASK"] = task_name
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 '(Get-ScheduledTask -TaskName $env:FRIDAY_R_TASK -ErrorAction SilentlyContinue) -ne $null'],
                capture_output=True, text=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW, env=env,
            )
            found = proc.stdout.strip().lower() == "true"
        except Exception as e:
            return VerificationResult(INCONCLUSIVE, f"Couldn't query Task Scheduler: {e}")

        if found:
            return VerificationResult(VERIFIED, f"Confirmed scheduled task '{task_name}' exists.")
        return VerificationResult(FAILED, f"Reported scheduling '{task_name}', but no such task exists.")

    def _verify_gmail(self, args: dict, result_text: str) -> VerificationResult:
        """Same independent-recheck principle as _verify_calendar — for
        send, actually search the Sent folder rather than trusting the
        API call's own reported success; for draft, check the drafts
        list the same way."""
        action = (args.get("action") or "list").strip().lower()
        try:
            from actions import gmail as gm
        except Exception as e:
            return VerificationResult(INCONCLUSIVE, f"Couldn't import gmail module to check: {e}")

        if action in ("send", "reply"):
            to = args.get("to") or ""
            subject = args.get("subject") or ""
            if not to:
                return VerificationResult(INCONCLUSIVE, "No recipient in the tool-call args to check against.")
            found = gm.search(f"in:sent to:{to} subject:{subject}", max_results=1)
            found_lower = found.strip().lower()
            if found_lower.startswith("found"):
                return VerificationResult(VERIFIED, f"Confirmed a sent message to {to} exists in Sent.")
            if found_lower.startswith("couldn't"):
                # The verification search itself failed (network blip,
                # etc.) — that's a problem with checking, not evidence the
                # original send failed. Don't report FAILED on a flake.
                return VerificationResult(INCONCLUSIVE, f"Couldn't verify — the check itself failed: {found}")
            return VerificationResult(FAILED, f"Reported sending to {to}, but no matching sent message exists.")

        if action in ("draft", "draft_reply"):
            # Drafts aren't searchable via the same messages().list(q=...)
            # call sent uses, and a false positive here (silently
            # reporting FAILED for a draft that actually saved) is worse
            # than not checking at all — draft_reply's own return string
            # already comes straight from the Gmail API's own create call
            # succeeding or raising, so there's little independent value
            # in re-deriving that. Left INCONCLUSIVE rather than guessing.
            return VerificationResult(INCONCLUSIVE, "Drafts aren't independently re-checked — trusting the API call's own result.")

        return VerificationResult(INCONCLUSIVE, f"'{action}' has no independent check defined (read-only action).")

    def _verify_calendar(self, args: dict, result_text: str) -> VerificationResult:
        """Independent re-check against the live Calendar API — same
        principle as _verify_reminder above, just against Google instead
        of Task Scheduler. Only checks create/delete; list/next are
        read-only and registered separately as no-op checks."""
        action = (args.get("action") or "list").strip().lower()
        try:
            from actions import calendar as gcal
        except Exception as e:
            return VerificationResult(INCONCLUSIVE, f"Couldn't import calendar module to check: {e}")

        if action in ("create", "add", "set", "schedule"):
            summary = args.get("summary") or args.get("message") or args.get("title") or ""
            date = args.get("date") or ""
            if not summary or not date:
                return VerificationResult(INCONCLUSIVE, "No summary/date in the tool-call args to check against.")
            matches = gcal.find_events(summary, date)
            if matches:
                return VerificationResult(VERIFIED, f"Confirmed '{summary}' exists on the calendar for {date}.")
            return VerificationResult(FAILED, f"Reported creating '{summary}' on {date}, but no such event exists.")

        if action in ("delete", "remove", "cancel"):
            query = args.get("query") or args.get("summary") or args.get("message") or ""
            date = args.get("date") or None
            if not query:
                return VerificationResult(INCONCLUSIVE, "No query in the tool-call args to check against.")
            matches = gcal.find_events(query, date)
            if not matches:
                return VerificationResult(VERIFIED, f"Confirmed no event matching '{query}' remains on the calendar.")
            return VerificationResult(FAILED, f"Reported deleting '{query}', but a matching event still exists.")

        return VerificationResult(INCONCLUSIVE, f"'{action}' has no independent check defined (read-only action).")

    def _verify_screenshot(self, args: dict, result_text: str) -> VerificationResult:
        custom_path = args.get("save_path")
        if custom_path:
            return VerificationResult(
                VERIFIED if Path(custom_path).exists() else FAILED,
                f"Custom save path {custom_path!r} " + ("exists." if Path(custom_path).exists() else "does not exist."),
            )
        m = re.search(r"saved to (\S+\.png)", result_text, re.I)
        if not m:
            return VerificationResult(INCONCLUSIVE, "Couldn't parse a filename out of the result to check.")
        dest = Path.home() / "Desktop" / m.group(1)
        if dest.exists():
            return VerificationResult(VERIFIED, f"Confirmed {dest.name} exists on disk.")
        return VerificationResult(FAILED, f"Reported saving {dest.name}, but it doesn't exist on disk.")

    def _verify_spotify(self, args: dict, result_text: str) -> VerificationResult:
        # Honest limitation: confirming actual playback needs the FULL
        # Spotify Web API (user OAuth, not just client-credentials), which
        # isn't set up here. Claiming VERIFIED without that would just be
        # a second unverified guess, not a real check — inconclusive is
        # the honest answer, same as the source project's own stance on
        # UWP process matching.
        return VerificationResult(
            INCONCLUSIVE,
            "Can't independently confirm actual Spotify playback without full user OAuth — not set up.",
        )

    def _verify_save_memory(self, args: dict, result_text: str) -> VerificationResult:
        from memory.long_term import get_category
        key = args.get("key", "")
        category = args.get("category", "notes")
        if not key:
            return VerificationResult(INCONCLUSIVE, "No key argument to check against memory.")
        stored = get_category(category)
        if key in stored:
            return VerificationResult(VERIFIED, f"Confirmed '{key}' is present in stored memory.")
        return VerificationResult(FAILED, f"Reported saving '{key}', but it's not in stored memory.")

    def _verify_power_control(self, args: dict, result_text: str) -> VerificationResult:
        # Shutdown/restart/sleep/lock can terminate this very process
        # mid-check — there's no safe window to verify a shutdown actually
        # happened from inside the process being shut down. Lock is the
        # only sub-action that's theoretically checkable (session state),
        # but not worth the complexity for one case — inconclusive across
        # the board is the honest, simple answer.
        return VerificationResult(INCONCLUSIVE, "Can't safely verify power actions from within the process being affected.")
