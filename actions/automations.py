"""
actions/automations.py — generalized trigger/action rules, modeled
directly on actions/topic_monitor.py's own pattern (JSON persistence,
atomic writes, a pure "what's due right now" check with no side effects
of its own). Delivery reuses the EXACT mechanism topic_monitor and
open_loops already use — start.py's _proactive_monitor background loop,
which already runs every 60s and already has speak_fn + a toast channel
wired in. This module only answers "what's due" and executes each due
rule's ACTION; the loop that calls it is the same one everything else
that "notices things on its own" already runs on.

Trigger types:
  - daily_time        {"time": "HH:MM"} — once per calendar day
  - interval_minutes  {"minutes": N} — every N minutes since last fire
  - before_event      {"minutes_before": N, "query": "..."} — N minutes
                       before a calendar event whose title matches query
  - new_email_from     {"query": "..."} — a Gmail search query (e.g.
                       "from:boss@x.com") with unseen results since last check

Action types:
  - speak   {"message": "..."} — spoken directly via speak_fn, no tool call
  - tool    {"tool": "calendar"|"reminder"|"gmail"|"open_app"|"computer_settings",
             "args": {...}} — dispatched through the same handlers the
             conversational tool-calling path uses

Deliberately NOT included yet: a "goal" action type handing a free-text
goal to agent/planner.py's multi-step executor. That's a real next step,
just one that needs its own careful wiring rather than a guess at an
interface — everything here is scoped to what's concretely testable now.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday.automations")

_VALID_TRIGGERS = {"daily_time", "interval_minutes", "before_event", "new_email_from"}
_VALID_ACTIONS = {"speak", "tool"}
_VALID_TOOLS = {"calendar", "reminder", "gmail", "open_app", "computer_settings", "morning_briefing"}


def _path() -> Path:
    from config import config
    return config.base_dir / "memory" / "automations.json"


def _load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Couldn't read automations.json ({e}) — treating as empty rather than crashing.")
        return []


def _save(rules: list[dict]) -> None:
    from utils.atomic_write import atomic_write_text
    atomic_write_text(_path(), json.dumps(rules, indent=2))


def add_rule(trigger: dict, action: dict, description: str = "") -> str:
    if not isinstance(trigger, dict) or trigger.get("type") not in _VALID_TRIGGERS:
        return f"I need a valid trigger type — one of {', '.join(sorted(_VALID_TRIGGERS))}."
    if not isinstance(action, dict) or action.get("type") not in _VALID_ACTIONS:
        return f"I need a valid action type — one of {', '.join(sorted(_VALID_ACTIONS))}."
    if action["type"] == "tool" and action.get("tool") not in _VALID_TOOLS:
        return f"I need a valid tool for the action — one of {', '.join(sorted(_VALID_TOOLS))}."
    if trigger["type"] == "daily_time" and not re.fullmatch(r"\d{2}:\d{2}", trigger.get("time", "")):
        return "I need a daily_time trigger's time in HH:MM format."
    if trigger["type"] == "interval_minutes" and not isinstance(trigger.get("minutes"), int):
        return "I need interval_minutes' minutes as a whole number."
    if trigger["type"] == "before_event" and not trigger.get("query"):
        return "I need before_event's query — which event title to watch for."
    if trigger["type"] == "new_email_from" and not trigger.get("query"):
        return "I need new_email_from's query — a Gmail search like 'from:someone@x.com'."

    rules = _load()
    rule = {
        "id": uuid.uuid4().hex[:8],
        "description": description or f"{trigger['type']} → {action['type']}",
        "trigger": trigger,
        "action": action,
        "enabled": True,
        "last_fired": None,
        "state": {},  # trigger-specific dedupe data (e.g. last-seen email ID, last-fired event start)
    }
    rules.append(rule)
    _save(rules)
    return f"Automation created: {rule['description']} (id {rule['id']})."


def list_rules() -> str:
    rules = _load()
    if not rules:
        return "No automations set up."
    lines = []
    for r in rules:
        status = "on" if r.get("enabled", True) else "off"
        last = r.get("last_fired") or "never"
        lines.append(f"[{r['id']}] ({status}) {r['description']} — last fired: {last}")
    return "Automations:\n" + "\n".join(lines)


def remove_rule(rule_id_or_query: str) -> str:
    rules = _load()
    query = (rule_id_or_query or "").strip().lower()
    matches = [r for r in rules if r["id"] == query or query in r["description"].lower()]
    if not matches:
        return f"Couldn't find an automation matching '{rule_id_or_query}'."
    if len(matches) > 1:
        options = "; ".join(f"[{m['id']}] {m['description']}" for m in matches)
        return f"Couldn't tell which automation you meant — found more than one match: {options}"
    rules = [r for r in rules if r["id"] != matches[0]["id"]]
    _save(rules)
    return f"Removed: {matches[0]['description']}."


def set_enabled(rule_id_or_query: str, enabled: bool) -> str:
    rules = _load()
    query = (rule_id_or_query or "").strip().lower()
    matches = [r for r in rules if r["id"] == query or query in r["description"].lower()]
    if not matches:
        return f"Couldn't find an automation matching '{rule_id_or_query}'."
    if len(matches) > 1:
        options = "; ".join(f"[{m['id']}] {m['description']}" for m in matches)
        return f"Couldn't tell which automation you meant — found more than one match: {options}"
    for r in rules:
        if r["id"] == matches[0]["id"]:
            r["enabled"] = enabled
    _save(rules)
    return f"{'Enabled' if enabled else 'Disabled'}: {matches[0]['description']}."


def mark_fired(rule_id: str, state: Optional[dict] = None) -> None:
    rules = _load()
    for r in rules:
        if r["id"] == rule_id:
            r["last_fired"] = datetime.now().isoformat(timespec="seconds")
            if state is not None:
                r["state"] = state
    _save(rules)


def get_due_rules() -> list[dict]:
    """Pure evaluation, no side effects — same principle as
    topic_monitor.check_all() not itself deciding what to DO with a
    headline it finds. The caller (start.py's proactive-monitor loop)
    executes each returned rule's action and then calls _mark_fired();
    this function never mutates last_fired/state itself, so a rule
    that's returned as due but whose action then fails to execute isn't
    silently marked as having fired. The caller calls mark_fired(rule['id'],
    rule.get('_dedupe_state')) once the action has actually run — every
    branch below that needs dedupe state attaches it under that one
    uniform key rather than a different ad-hoc key per trigger type."""
    due = []
    now = datetime.now()
    for rule in _load():
        if not rule.get("enabled", True):
            continue
        trig = rule["trigger"]
        ttype = trig["type"]

        if ttype == "daily_time":
            last = rule.get("last_fired")
            already_today = last and last[:10] == now.strftime("%Y-%m-%d")
            target_h, target_m = map(int, trig["time"].split(":"))
            target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if not already_today and target <= now < target + _timedelta_minutes(5):
                due.append(rule)

        elif ttype == "interval_minutes":
            last = rule.get("last_fired")
            if last is None:
                due.append(rule)
            else:
                elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
                if elapsed >= trig["minutes"]:
                    due.append(rule)

        elif ttype == "before_event":
            try:
                from actions.calendar import find_events
                matches = find_events(trig["query"], days_ahead=1)
            except Exception as e:
                logger.debug(f"before_event check failed for rule {rule['id']}: {e}")
                continue
            for ev in matches:
                start = ev.get("start", {}).get("dateTime")
                if not start:
                    continue
                try:
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    continue
                minutes_until = (start_dt - now).total_seconds() / 60
                already_fired_this_one = rule.get("state", {}).get("last_event_start") == start
                if 0 <= minutes_until <= trig["minutes_before"] and not already_fired_this_one:
                    rule = {**rule, "_dedupe_state": {"last_event_start": start}}
                    due.append(rule)
                    break

        elif ttype == "new_email_from":
            try:
                from actions import gmail as gm
                result = gm.search(trig["query"], max_results=5)
            except Exception as e:
                logger.debug(f"new_email_from check failed for rule {rule['id']}: {e}")
                continue
            if result.lower().startswith("found"):
                # First line after the header is the newest match — used
                # as the dedupe key so re-checking doesn't refire on the
                # same email every interval.
                first_line = result.split("\n", 1)[1].split("\n", 1)[0] if "\n" in result else ""
                if rule.get("state", {}).get("last_seen") != first_line:
                    rule = {**rule, "_dedupe_state": {"last_seen": first_line}}
                    due.append(rule)

    return due


def _timedelta_minutes(n):
    from datetime import timedelta
    return timedelta(minutes=n)
