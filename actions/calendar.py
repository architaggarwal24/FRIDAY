"""
actions/calendar.py — real Google Calendar integration: list, create, and
delete events, built on the shared OAuth in actions/google_auth.py.

This is what "add this to my calendar" should have been calling all
along. actions/reminder.py is a LOCAL scheduler — it fires an OS
notification via Task Scheduler, with zero connection to Google
Calendar or any calendar app — which is exactly why "she added it to
reminders instead of my calendar" kept happening: reminder was being
asked to do a job it was never built for. This is that job. reminder.py
is untouched and still exists for its own purpose (a plain "notify me
at this time" alarm with no account needed); brain/llm.py's tool
schemas now point calendar-flavored requests here instead.

Every failure string below follows the same convention already
established in reminder.py/handlers.py — starts with "Couldn't",
"I need", or "I'm not connected" — because brain/llm.py's generic
failure detector already keys off exactly those prefixes to stop the
model from claiming success over a failure (see the _generic_failed
check in _stream_ollama). No changes needed there; writing errors the
same way means this tool is covered by that guard for free.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("friday.calendar")

_NOT_CONNECTED = (
    "Couldn't reach your Google Calendar, boss — you're not connected yet. Run "
    "`python google_auth_setup.py` from the repo root to set that up."
)


def _get_service():
    """Returns an authenticated Calendar API service, or None if nothing
    is connected — callers turn that into _NOT_CONNECTED rather than
    raising, matching how every other failure here is a plain string."""
    from actions.google_auth import get_credentials
    creds = get_credentials()
    if creds is None:
        return None
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=creds)


def _fmt_time(iso: str, all_day: bool) -> str:
    if all_day:
        return "all day"
    try:
        # Google returns offset-aware ISO timestamps (...+05:30 etc.) —
        # fromisoformat handles that natively on 3.11+; the replace
        # covers the older 'Z' suffix some client libs still emit.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return iso


def _event_line(ev: dict) -> str:
    summary = ev.get("summary", "(no title)")
    start = ev.get("start", {})
    all_day = "date" in start  # all-day events use 'date', timed ones use 'dateTime'
    when = _fmt_time(start.get("dateTime", start.get("date", "")), all_day)
    loc = f" @ {ev['location']}" if ev.get("location") else ""
    return f"{when} — {summary}{loc}"


def list_events(date_str: Optional[str] = None, days: int = 1) -> str:
    """Lists events on a given date (default: today) or a span of `days`
    starting there. date_str must be YYYY-MM-DD if given — same strict
    format reminder.py already trained the model to supply."""
    service = _get_service()
    if service is None:
        return _NOT_CONNECTED

    if date_str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            return "I need a date in YYYY-MM-DD format to check the calendar."
        try:
            start_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return f"Couldn't check the calendar — '{date_str}' isn't a real date, boss."
    else:
        start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    end_date = start_date + timedelta(days=max(1, days))

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=start_date.isoformat() + "Z",
            timeMax=end_date.isoformat() + "Z",
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()
    except Exception as e:
        return f"Couldn't reach Google Calendar: {e}"

    events = result.get("items", [])
    if not events:
        return "Nothing on the calendar" + (f" for {date_str}" if date_str else " today") + "."

    lines = [_event_line(ev) for ev in events]
    header = f"Calendar for {date_str}:" if date_str else "Today's calendar:"
    return header + "\n" + "\n".join(lines)


def get_next_event() -> str:
    service = _get_service()
    if service is None:
        return _NOT_CONNECTED
    try:
        now = datetime.utcnow().isoformat() + "Z"
        result = service.events().list(
            calendarId="primary", timeMin=now, singleEvents=True,
            orderBy="startTime", maxResults=1,
        ).execute()
    except Exception as e:
        return f"Couldn't reach Google Calendar: {e}"

    events = result.get("items", [])
    if not events:
        return "Nothing else on your calendar, boss."
    return "Next up: " + _event_line(events[0])


def create_event(summary: str, date: str, time_str: Optional[str] = None,
                  duration_minutes: int = 60, all_day: bool = False,
                  location: str = "", description: str = "") -> str:
    """Same strict validation shape as handle_reminder: date is required
    and must be YYYY-MM-DD; time is required and must be HH:MM unless
    all_day is set. Rejecting anything else outright (rather than trying
    to be lenient and guess) is what made reminder.py's self-correction
    loop reliable — an ambiguous format here would just move the same
    failure-then-retry cycle into a NEW tool instead of fixing it."""
    service = _get_service()
    if service is None:
        return _NOT_CONNECTED

    if not summary or not summary.strip():
        return "I need to know what to call the event."
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        return "I need a date in YYYY-MM-DD format to create an event."

    if all_day:
        try:
            end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            return f"Couldn't create that event — '{date}' isn't a real date, boss."
        body = {
            "summary": summary,
            "start": {"date": date},
            "end": {"date": end},
        }
    else:
        if not re.fullmatch(r"\d{2}:\d{2}", time_str or ""):
            return "I need a time in HH:MM format to create an event (or say it's an all-day event)."
        try:
            start_dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            return f"Couldn't create that event — '{date} {time_str}' isn't a real date/time, boss."
        end_dt = start_dt + timedelta(minutes=max(5, duration_minutes))
        tz = _local_tz_name()
        body = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
        }

    if location:
        body["location"] = location
    if description:
        body["description"] = description

    try:
        created = service.events().insert(calendarId="primary", body=body).execute()
    except Exception as e:
        return f"Couldn't create that event: {e}"

    when = date if all_day else f"{date} at {time_str}"
    return f"Event created for {when}: {summary}."


def _local_tz_name() -> str:
    """Best-effort local IANA timezone name — falls back to UTC (still a
    valid, unambiguous event, just not shifted to the user's local
    clock) rather than failing the whole create_event call over this."""
    try:
        import tzlocal
        return tzlocal.get_localzone_name()
    except Exception:
        try:
            local = datetime.now().astimezone()
            return local.tzname() or "UTC"
        except Exception:
            return "UTC"


def find_events(query: str, date_str: Optional[str] = None, days_ahead: int = 30) -> list[dict]:
    """Helper for delete_event — searches upcoming events (default: next
    30 days) for a summary match. Uses Calendar's own text search (q=)
    rather than fetching everything and matching client-side."""
    service = _get_service()
    if service is None:
        return []
    if date_str:
        try:
            start = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return []
        end = start + timedelta(days=1)
    else:
        start = datetime.utcnow()
        end = start + timedelta(days=days_ahead)
    try:
        result = service.events().list(
            calendarId="primary", timeMin=start.isoformat() + "Z", timeMax=end.isoformat() + "Z",
            q=query, singleEvents=True, orderBy="startTime", maxResults=10,
        ).execute()
    except Exception:
        return []
    return result.get("items", [])


def delete_event(query: str, date_str: Optional[str] = None) -> str:
    service = _get_service()
    if service is None:
        return _NOT_CONNECTED
    if not query or not query.strip():
        return "I need to know which event to delete."

    matches = find_events(query, date_str)
    if not matches:
        return f"Couldn't find an event matching '{query}', boss."
    if len(matches) > 1:
        options = "; ".join(_event_line(m) for m in matches)
        return f"Couldn't tell which event you meant — found more than one match: {options}"

    ev = matches[0]
    try:
        service.events().delete(calendarId="primary", eventId=ev["id"]).execute()
    except Exception as e:
        return f"Couldn't delete that event: {e}"
    return f"Deleted: {ev.get('summary', '(no title)')}."
