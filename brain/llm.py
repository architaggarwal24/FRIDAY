"""
F.R.I.D.A.Y. — brain/llm.py
Multi-provider LLM router (NVIDIA NIM -> Ollama -> Gemini), the system
prompt builder, and tool-call dispatch.

  - Session lock: when all cloud models fail, FRIDAY switches to Ollama
    for the entire session instead of re-trying cloud on every message.
  - The system prompt is cached (see build_system_prompt()) rather than
    rebuilt every message — except long-term memory retrieval, which is
    always recomputed fresh from the current turn's question.
  - Tools list is built once at module load, not every request.
  - Smart 429 backoff: 404 models are skipped, 429 models get one retry
    after a short wait once every other provider is exhausted.
  - Provider dispatch is one simple function, no nesting.
"""

import asyncio
import json
import logging
import random
import re
import secrets
import time
from typing import AsyncIterator, Callable, Optional

from config import config

logger = logging.getLogger(__name__)


def _record_groq_rate_limit_headers(headers, service: str) -> None:
    """Groq puts real RPM/TPM remaining-vs-limit data on every response
    header, success or not — no separate usage call needed, just read
    what's already there. RPD (daily count) isn't exposed this way per
    Groq's own docs, so the local call counter still covers that part."""
    try:
        remaining = headers.get("x-ratelimit-remaining-requests")
        limit = headers.get("x-ratelimit-limit-requests")
        if remaining is not None and limit is not None:
            from memory import usage_tracker as ut
            used = int(limit) - int(remaining)
            ut.set_real_quota(service, used=used, limit=int(limit), unit="requests/min window")
    except Exception as e:
        logger.debug(f"[Groq] Couldn't parse rate-limit headers: {e}")


# ── Session-level provider lock ───────────────────────────────────────────────
# Once set to "ollama", ALL requests this session go to Ollama.
# Resets on process restart. User can also say "switch back to cloud".
_session_provider: Optional[str] = None  # None = use config.brain.llm_provider

# Mirrors voice/tts.py's ElevenLabs->edge auto-switch tracking — see
# _cloud_fallback() below for why this exists.
_consecutive_ollama_failures = 0
_LLM_AUTO_SWITCH_THRESHOLD = 2


def get_active_provider() -> str:
    return _session_provider or config.brain.llm_provider


KNOWN_PROVIDERS = ("ollama", "nvidia", "gemini", "auto", "fast", "strong", "local", "smart")

# One line, spoken once, whenever the active provider actually changes —
# regardless of which of the three places triggered it (see
# set_active_provider() below, which is the only thing that ever speaks
# one). Written from what FRIDAY actually gets used for in this project
# (the coding agent, screen vision, memory, focus sessions), not model
# benchmarks — a person doesn't care about tokens/sec, they care what
# just got easier or harder to ask for. 5 per provider, FRIDAY's actual
# voice per core/prompt.txt: dry, contractions, "boss" occasional, not
# on every line.
_PROVIDER_SWITCH_LINES: dict[str, list[str]] = {
    "ollama": [
        "Going local — this one won't leave the machine.",
        "Switched to Ollama. Free, private, and fine for most of what we do.",
        "On Ollama now, boss. If this gets gnarly, say so and I'll go back to cloud.",
        "Local model's up. No bill for this one.",
        "Running Ollama. Slower on the hard stuff, but it's all yours.",
    ],
    "nvidia": [
        "Back on NIM — the one behind most of what we do.",
        "Switched to NVIDIA, boss. Let's get back to it.",
        "NIM's up. This is what runs the coding agent, too.",
        "On NVIDIA now — good for anything that needs to move.",
        "Running NIM. Send it.",
    ],
    "gemini": [
        "Switched to Gemini — the one with actual eyes.",
        "On Gemini now, boss. Good pick if you want me looking at your screen.",
        "Gemini's up. Show me something.",
        "Running Gemini — handles a picture better than the others.",
        "Switched over to Gemini.",
    ],
    "auto": [
        "On Auto — I'll pick fast, strong, or local depending on what's up.",
        "Auto routing's on. I'll hop tiers myself if one's down.",
        "Switched to Auto — fastest thing that's actually working, boss.",
    ],
    "fast": [
        "Fast tier — quick answers, less muscle behind them.",
        "Switched to Fast. Good for anything that doesn't need deep thought.",
        "On the fast lane now, boss.",
    ],
    "strong": [
        "Strong tier — NIM or Gemini, whichever's up. Full reasoning.",
        "Switched to Strong. This is the one for the hard stuff.",
        "On Strong now — bring on the complicated version of the question.",
    ],
    "local": [
        "Local tier — Ollama only, nothing leaves this machine.",
        "Switched to Local. If Ollama's down, I'll say so rather than sneak off to the cloud.",
        "On Local now, boss. Private, and it stays that way.",
    ],
}
_last_provider_line = ""


def _pick_provider_line(name: str) -> str:
    global _last_provider_line
    pool = _PROVIDER_SWITCH_LINES.get(name, [f"Switched to {name}."])
    options = [line for line in pool if line != _last_provider_line] or pool
    line = random.choice(options)
    _last_provider_line = line
    return line


def _announce_provider_switch(name: str, reason: str = "") -> None:
    """Speaks the switch — direct TTS, not routed through the provider
    being switched to. Asking a model to introduce itself is unreliable
    (this project's own earlier experience with it is exactly why the
    drift callouts and posture nudges in actions/focus_session.py are
    also canned lines, not LLM output — same reasoning here).

    Goes straight to the already-running TTS pipeline via
    ui.ws_server's registered instance rather than needing a speak_fn
    threaded through every caller — set_active_provider() is called
    from contexts (the WS handler, mid-stream inside _cloud_fallback)
    that don't all have one in scope, and this is the one thing in the
    whole app that's guaranteed to exist once anything is running."""
    try:
        from ui.ws_server import _tts_instance
        if _tts_instance is not None:
            _tts_instance.enqueue(_pick_provider_line(name))
    except Exception as e:
        logger.debug(f"[LLM] Couldn't announce provider switch: {e}")

    try:
        from ui.ws_server import broadcast_from_thread
        broadcast_from_thread({
            "event": "toast",
            # Reason included when there is one (an automatic fallback
            # always has one — see _cloud_fallback() below) so the toast
            # stays as informative as the switch-specific wording each
            # call site used to write inline, without every call site
            # needing to write its own toast anymore.
            "message": f"Switched to {name}." if not reason else f"Switched to {name} ({reason}).",
            "kind": "info",
        })
    except Exception:
        pass


def set_active_provider(name: str, reason: str = "") -> bool:
    """THE single place a provider swap actually happens — the manual
    Settings toggle, the voice-phrase quick-switch, and
    _cloud_fallback()'s automatic Ollama-down fallback all call this
    (directly, or via lock_to_ollama()/unlock_provider() below, which
    are now thin wrappers around it) rather than touching
    _session_provider themselves. One function means the announcement
    can never be skipped by one caller or duplicated by another.

    Validated against the known set — returns False rather than
    silently accepting a typo that would only surface as a confusing
    failure three calls later. Only announces when the EFFECTIVE
    provider actually changes — asking to switch to what's already
    active is a no-op, not a new line.
    """
    global _session_provider
    if name not in KNOWN_PROVIDERS:
        logger.warning(f"[LLM] Rejected unknown provider: {name!r}")
        return False

    before = get_active_provider()
    _session_provider = None if name == config.brain.llm_provider else name
    after = get_active_provider()

    logger.info(f"[LLM] Active provider set to {name!r} (session override)"
                + (f" — {reason}" if reason else ""))
    if after != before:
        _announce_provider_switch(after, reason=reason)
    return True


def lock_to_ollama(reason: str = ""):
    set_active_provider("ollama", reason=reason or "cloud unavailable")


def unlock_provider():
    """Call this if user says 'switch back to cloud'."""
    set_active_provider(config.brain.llm_provider, reason="user requested")


# ── Conversation memory ──────────────────────────────────────────────────────

class ConversationMemory:
    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self.messages: list[dict] = []

    def add_user(self, text: str):
        # Guard against double-add during provider rotation
        if self.already_has_user(text):
            return
        self.messages.append({"role": "user", "content": text})
        self._trim()
        self._log("user", text)

    def add_assistant(self, text: str):
        self.messages.append({"role": "assistant", "content": text})
        self._trim()
        self._log("assistant", text)

    def _trim(self):
        if len(self.messages) > self.max_turns * 2:
            self.messages = self.messages[-(self.max_turns * 2):]

    def _log(self, role: str, text: str):
        try:
            from memory.memory_store import log_message
            log_message(role, text)
        except Exception:
            pass

    def already_has_user(self, text: str) -> bool:
        if self.messages and self.messages[-1]["role"] == "user":
            return self.messages[-1]["content"] == text
        return False

    def get_messages(self) -> list[dict]:
        return list(self.messages)

    def get_all(self) -> list[dict]:
        return list(self.messages)

    def clear(self):
        self.messages.clear()


memory = ConversationMemory(max_turns=config.brain.max_history_turns)


# ── Tool definitions (built once at load) ────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application, website, or program on the computer. "
            "ALWAYS call this tool when user asks to open something — "
            "NEVER just say 'Opened X' without calling this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "Exact app name e.g. 'Chrome', 'Spotify', 'VS Code', 'WhatsApp'"}
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web for current information or facts. "
            "Call this for: current events, prices, people, news, "
            "song/music recommendations, movie suggestions, sports scores, recipes — "
            "anything that benefits from live data. "
            "Also use for 'suggest songs', 'recommend music', 'what are good X' questions. "
            "NEVER answer from memory if the info could be outdated. "
            "NEVER narrate that you will search — just call this immediately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer OS: volume, brightness, mute, WiFi, "
            "keyboard shortcuts, lock screen, dark mode, clipboard. "
            "NOT for reminders, events, or calendar — use calendar (or reminder for a plain local "
            "alarm) for those, even if the request doesn't sound like a 'setting'. "
            "ALWAYS call this for system control — never just describe what you would do."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Natural language: what to do"},
                "value": {"type": "string", "description": "Optional value e.g. '50' for volume level"}
            },
            "required": ["description"]
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls a web browser: open URLs, search, click, type, scroll. "
            "Use for web navigation tasks. Call this — do not just say you navigated somewhere. "
            "If the user names a specific browser ('in edge', 'using chrome', 'open opera and "
            "search...'), ALWAYS pass it as `browser` — omitting it means whatever the OS "
            "default browser happens to be, not the one asked for."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "go_to | search | click | type | scroll | close"},
                "browser": {"type": "string", "description": "Only if the user named one: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Leave out entirely if they didn't say which browser — do not guess one."},
                "url": {"type": "string", "description": "URL for go_to"},
                "query": {"type": "string", "description": "Search query — leave blank/omit if the user didn't actually give one yet ('search something' is not a query); the tool will ask for it."},
                "text": {"type": "string", "description": "Text for click or type"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": (
            "Manages files and folders: list, create, read, write, delete, move, copy, find, open, "
            "undo. Deletes and overwriting writes are automatically backed up to a local trash first "
            "— 'undo' restores the most recent one. "
            "ALWAYS call this for file operations — never describe them without calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "create_file (creates a file with content) | create_folder (creates a directory) | read | write | list | delete | move | copy | rename | find | open | disk_usage | undo (restores the most recently deleted/overwritten file — no path needed). Use create_file when asked to make a file; use create_folder only when making a directory."},
                "path": {"type": "string", "description": "Path. Use 'desktop', 'downloads', 'documents' as shortcuts."},
                "name": {"type": "string", "description": "File name"},
                "content": {"type": "string", "description": "File content — REQUIRED for create_file and write. Include the full code or text to write into the file."},
                "destination": {"type": "string", "description": "Destination for move/copy"},
                "new_name": {"type": "string", "description": "New name for rename"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "send_message",
        "description": (
            "Sends a message via WhatsApp or Telegram. "
            "Call this to actually send — never say 'Message sent' without calling it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "receiver": {"type": "string", "description": "Contact name"},
                "message_text": {"type": "string", "description": "Message to send"},
                "platform": {"type": "string", "description": "WhatsApp | Telegram"}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "spotify_control",
        "description": (
            "Controls Spotify: play, pause, resume, skip, volume, shuffle, what's playing, one-time "
            "account login. "
            "ALWAYS call this for music requests — NEVER say 'Playing X' without calling this first. "
            "Even if Spotify is not connected via API, this tool handles URI-based playback."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "play | pause | resume | next | previous | volume | current | shuffle | login"},
                "query": {"type": "string", "description": "EXACT song/artist name from user's current message ONLY. NEVER infer from previous messages or context. If user says 'play alone part 2', query is 'alone part 2'. Do not add artist names unless the user explicitly said them."},
                "value": {"type": "string", "description": "Volume level 0-100 for volume action"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "google_status",
        "description": (
            "Checks whether a Google account is connected (for Calendar + Gmail). Call this for "
            "questions like 'are you connected to Google', 'is my calendar set up', 'can you see "
            "my Gmail'. Read-only — does not create, send, or delete anything. Calendar/Gmail "
            "actions themselves aren't available yet; this only reports connection status."
        ),
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "weather_report",
        "description": (
            "Gets current weather for a city. Call this — never make up weather data. "
            "For 'my home city' / 'weather at home' — just call this with city='home'; it resolves "
            "the configured home city internally. Do NOT use recall_memory or web_search to look up "
            "the user's home city first, that's not where it's stored."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name, or 'home' for the configured home city"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Plays a YouTube video or shows trending. "
            "Call this for YouTube requests — never say 'Playing on YouTube' without calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "play | trending"},
                "query": {"type": "string", "description": "Search query for play"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "screenshot",
        "description": "Takes a screenshot and saves it. Call this — never say you took one without calling.",
        "parameters": {
            "type": "object",
            "properties": {
                "save_path": {"type": "string", "description": "Save path (optional, defaults to Desktop)"}
            }
        }
    },
    {
        "name": "screen_process",
        "description": (
            "CRITICAL: You have NO visual ability without this tool. "
            "You CANNOT see the screen, webcam, or any image unless you call this. "
            "Call when user asks what is on screen, what something looks like, or to analyze a screenshot. "
            "This returns real analysis text — respond to the user based on it normally, same as any other tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to analyze or ask about the screen"},
                "angle": {"type": "string", "description": "'screen' to capture display, 'camera' for webcam. Default: screen"},
                "app_name": {"type": "string", "description": "Optional — name of a SPECIFIC app/window to look at (e.g. 'opera', 'vscode', 'discord'), even if it isn't the frontmost window. Omit to just capture whatever's currently visible on screen."}
            },
            "required": ["text"]
        }
    },
    {
        "name": "reminder",
        "description": (
            "Sets a plain, LOCAL reminder — an OS notification popup at a specific time. "
            "NOT connected to any calendar app; use this only for a one-off 'notify me at X' alarm "
            "with no need to see it on a calendar (e.g. 'remind me to take the laundry out at 5pm'). "
            "For anything the user calls 'my calendar', 'schedule', or an event they'd expect to see "
            "on their phone's calendar — use the calendar tool instead, not this one. "
            "Do NOT use for 'remind me in X minutes' — that is timer. "
            "Also handles listing pending reminders and cancelling one. "
            "ALWAYS call this — never say 'Reminder set' without calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "set (default) | list | remove"},
                "date": {"type": "string", "description": "Date YYYY-MM-DD, for action=set"},
                "time": {"type": "string", "description": "Time HH:MM (24h), for action=set"},
                "message": {"type": "string", "description": "Reminder message, for action=set"},
                "query": {"type": "string", "description": "Text to match against for action=remove, e.g. 'dentist'"}
            },
            "required": []
        }
    },
    {
        "name": "calendar",
        "description": (
            "The user's REAL Google Calendar — use this for anything they call 'my calendar', "
            "'schedule', or a meeting/event/appointment they'd expect to see on their phone's "
            "calendar app. 'What's on my calendar' / 'how's my schedule looking' / 'anything today' "
            "→ action=list (NOT screen_process — never guess at a calendar from a screenshot when "
            "this tool gives a real answer). 'Add/schedule X to my calendar' → action=create. "
            "Requires being connected (see google_status) — if not connected, this tool will say so "
            "plainly; relay that honestly, don't pretend it worked. "
            "Do NOT use computer_settings or reminder for calendar-flavored requests — this is the "
            "one real calendar; reminder is a separate, unrelated local-only notification. "
            "This CAN create events directly — never say 'I can't add events directly' or similar; "
            "that is false. If the user's request is vague ('add an event', no date/time/title given), "
            "call this anyway with your best guess or an empty/placeholder value for what's missing — "
            "the tool validates and tells you exactly what's missing so you can ask a real, specific "
            "follow-up question, instead of you deciding upfront not to try."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list (default) | create | delete | next"},
                "date": {"type": "string", "description": "Date YYYY-MM-DD — for list (which day) or create/delete"},
                "days": {"type": "integer", "description": "For action=list — span of days from `date` (default 1)"},
                "summary": {"type": "string", "description": "Event title, for action=create"},
                "time": {"type": "string", "description": "Time HH:MM (24h), for action=create (omit only if all_day)"},
                "duration_minutes": {"type": "integer", "description": "For action=create, default 60"},
                "all_day": {"type": "boolean", "description": "For action=create — all-day event, no time needed"},
                "location": {"type": "string", "description": "Optional, for action=create"},
                "description": {"type": "string", "description": "Optional notes, for action=create"},
                "query": {"type": "string", "description": "Text to match the event title against, for action=delete"}
            },
            "required": []
        }
    },
    {
        "name": "gmail",
        "description": (
            "The user's REAL Gmail — use for anything about 'my email', 'my inbox', 'unread mail', "
            "or checking for a message from someone. 'Any mail from X' / 'check my inbox' / "
            "'unread emails' → action=list or action=search (NOT screen_process — never guess at "
            "email content from a screenshot when this tool gives a real answer). "
            "'Open/click/read the X one' after a list/search → action=open (opens it in the browser) "
            "or action=read (reads the body back right here) — query can be an ordinal ('the second "
            "one', 'the last one') or a few words from the sender/subject; this resolves against the "
            "MOST RECENT list/search shown, so don't re-search first, just reference it. "
            "For 'any mail from X' / 'is there an email from X' / 'the latest email from X' — these "
            "want ONE result, not an inbox dump: pass max_results=1 (Gmail already returns newest "
            "first). Only use a higher max_results when the user actually asks for a list, a count, "
            "or 'all the emails about X'. When relaying results, don't just recite every line the "
            "tool returned — summarise naturally, the way a person would answer 'any mail from X'. "
            "action=draft creates a Gmail DRAFT only (never sent, safe, reviewed by the user first). "
            "action=send ACTUALLY SENDS — this is irreversible, so confirm the recipient/subject/body "
            "with the user in plain conversation before calling this with action=send (the system "
            "will also hold it for explicit confirmation, but don't rely on that alone — check first). "
            "Requires being connected (see google_status) — if not connected, this tool says so "
            "plainly; relay that honestly, don't pretend it worked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list (default) | search | read | open | draft | send"},
                "query": {"type": "string", "description": "Search text for action=search; an ordinal or a few words for action=read/open/draft; which email to reply to for action=send"},
                "max_results": {"type": "integer", "description": "For list/search, default 10"},
                "to": {"type": "string", "description": "Recipient email address, for action=send"},
                "subject": {"type": "string", "description": "Subject line, for action=send"},
                "body": {"type": "string", "description": "Message body, for action=draft/send"}
            },
            "required": []
        }
    },
    {
        "name": "automation",
        "description": (
            "Create, list, enable/disable, or delete a recurring BACKGROUND rule — things FRIDAY "
            "does on her own without being asked each time, e.g. 'every morning at 8, tell me the "
            "weather', '10 minutes before any meeting, remind me', 'let me know when I get an email "
            "from my boss'. Rules run continuously in the background, checked roughly every minute, "
            "even when the user isn't actively talking to FRIDAY — this does NOT do the thing right "
            "now, it schedules it to happen automatically going forward. For 'do X now', use the "
            "relevant tool directly instead (calendar/gmail/reminder/etc.), not this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "create (default) | list | delete | enable | disable"},
                "trigger_type": {"type": "string", "description": "daily_time | interval_minutes | before_event | new_email_from — for action=create"},
                "trigger_time": {"type": "string", "description": "HH:MM (24h), for trigger_type=daily_time"},
                "trigger_minutes": {"type": "integer", "description": "For trigger_type=interval_minutes (every N minutes) or before_event (N minutes before the event)"},
                "trigger_query": {"type": "string", "description": "Event title to watch for (before_event), or a Gmail search query like 'from:x@y.com' (new_email_from)"},
                "action_type": {"type": "string", "description": "speak | tool, for action=create"},
                "speak_message": {"type": "string", "description": "What to say, for action_type=speak"},
                "tool_name": {"type": "string", "description": "calendar | reminder | gmail | open_app | computer_settings, for action_type=tool"},
                "tool_args": {"type": "object", "description": "Args matching that tool's own parameters, for action_type=tool"},
                "description": {"type": "string", "description": "Short human-readable label for this automation"},
                "query": {"type": "string", "description": "Which automation to delete/enable/disable — matched against its id or description"}
            },
            "required": []
        }
    },
    {
        "name": "morning_briefing",
        "description": (
            "A single combined briefing: weather (if a home city is set), today's real calendar, "
            "local reminders, unread inbox digest, and any watched-topic news — all in one go. "
            "Use when the user asks for their 'morning briefing', 'what's my day look like', 'catch "
            "me up', 'brief me', or similar. Don't use this for a single specific thing (just the "
            "weather, just the calendar) — use that tool directly instead, this is for 'everything at once'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Override the configured home city for weather, optional"}
            },
            "required": []
        }
    },
    {
        "name": "open_loop",
        "description": (
            "Tracks something to follow up on LATER with no specific time attached — "
            "'I should think about switching jobs', 'let me know if you hear anything about X', "
            "something YOU (FRIDAY) said you'd check on. "
            "Do NOT use if the user gave a clock time, date, or 'in N minutes' — that's reminder or timer. "
            "Only call action=add when there's a genuine open commitment, not for every passing remark — "
            "over-tracking is as bad as under-tracking. "
            "FRIDAY resurfaces an open loop on its own after roughly a day, then at most about once a "
            "week after that — call action=resolve once it's actually been dealt with, so it stops "
            "coming back up. "
            "ALWAYS call this — never say 'I'll keep track of that' without calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "add (default) | resolve | list"},
                "text": {"type": "string", "description": "What to follow up on, for action=add"},
                "query": {"type": "string", "description": "Id or words matching the loop, for action=resolve"}
            },
            "required": []
        }
    },
    {
        "name": "code",
        "description": (
            "All coding tasks. FRIDAY decides the engine automatically based on task complexity — "
            "you never need to specify which agent to use. "
            "\n"
            "Use this for: write code, edit code, fix bugs, explain code, run code, "
            "refactor, add a feature, build a project, change this file, update that function — anything code-related. "
            "\n"
            "FRIDAY will infer scope from the request: "
            "single file / quick write → handled directly; "
            "multi-file / repo-level / complex refactor → aider agent; "
            "deep reasoning / architecture / full project build → claude_code agent. "
            "\n"
            "ALWAYS call this tool for coding. Never output code as plain text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task":         {"type": "string", "description": "Exactly what to do, in plain English. Be specific — include file paths, function names, what to change."},
                "path":         {"type": "string", "description": "File path (for single-file tasks) or project folder path (for multi-file tasks). Infer from context if mentioned."},
                "language":     {"type": "string", "description": "Programming language — infer from file extension or context if not stated. Default: python."},
                "action":       {"type": "string", "description": "write | edit | explain | run | fix | refactor | build — infer from the user's intent."},
            },
            "required": ["task", "action"]
        }
    },
    {
        "name": "desktop_control",
        "description": (
            "Controls the desktop: set wallpaper, organize files, clean desktop, get system stats. "
            "Call this for desktop management tasks. This tool only performs the fixed actions listed "
            "below — it cannot run arbitrary code or perform tasks outside this list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "wallpaper | wallpaper_url | current_wallpaper | organize | clean | list | stats"},
                "path": {"type": "string", "description": "Local image file path, for action=wallpaper"},
                "url": {"type": "string", "description": "Image URL, for action=wallpaper_url"},
                "mode": {"type": "string", "description": "by_type | by_date for action=organize (default by_type)"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "computer_control",
        "description": (
            "Direct computer control: click, type, drag, scroll, hotkeys, clipboard, focus windows, "
            "find UI elements on screen via AI, generate fake form data. Use for GUI automation tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "type | smart_type | click | double_click | right_click | move | drag | hotkey | "
                        "press | scroll | copy | paste | screenshot | wait | clear_field | focus_window | "
                        "screen_find | screen_click | random_data | user_data"
                    ),
                },
                "text": {"type": "string", "description": "Text to type/smart_type/paste"},
                "x": {"type": "string", "description": "X coordinate for click/move"},
                "y": {"type": "string", "description": "Y coordinate for click/move"},
                "x1": {"type": "string", "description": "Drag start X coordinate"},
                "y1": {"type": "string", "description": "Drag start Y coordinate"},
                "x2": {"type": "string", "description": "Drag end X coordinate"},
                "y2": {"type": "string", "description": "Drag end Y coordinate"},
                "keys": {"type": "string", "description": "Hotkey combo e.g. 'ctrl+c'"},
                "key": {"type": "string", "description": "Single key name for 'press', e.g. 'enter'"},
                "direction": {"type": "string", "description": "'up' | 'down' | 'left' | 'right' for scroll"},
                "amount": {"type": "string", "description": "Scroll amount (default 3)"},
                "seconds": {"type": "string", "description": "Duration for 'wait' (max 30s)"},
                "title": {"type": "string", "description": "Window title fragment for focus_window"},
                "description": {"type": "string", "description": "Natural-language UI element description for screen_find/screen_click"},
                "type": {"type": "string", "description": "Data type for random_data, e.g. 'email', 'name', 'password'"},
                "field": {"type": "string", "description": "Memory field name for user_data, e.g. 'name', 'email'"},
                "clear_first": {"type": "string", "description": "'true'/'false' — clear field before smart_type (default true)"},
                "path": {"type": "string", "description": "Save path for screenshot (must be inside home dir)"},
            },
            "required": ["action"]
        }
    },

    {
        "name": "file_processor",
        "description": (
            "Processes and analyzes files: PDF, Word, Excel, video, audio, images, code, CSV. "
            "Can summarize, extract text, transcribe audio/video, analyze data. "
            "Call when user asks about a file's contents or wants it processed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Full path to the file to process"},
                "task": {"type": "string", "description": "What to do: summarize | extract_text | transcribe | analyze | convert"}
            },
            "required": ["file_path", "task"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "Updates or installs Steam and Epic Games games. "
            "Can schedule updates, check download status, shutdown PC when done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "update | install | status | schedule"},
                "game_name": {"type": "string", "description": "Game name"},
                "platform": {"type": "string", "description": "steam | epic"},
                "schedule_time": {"type": "string", "description": "Time to run e.g. '3:00 AM' for schedule action"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "flight_finder",
        "description": (
            "Searches for flights on Google Flights. "
            "Call when user asks about flights, prices, or travel options."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Departure city or airport code"},
                "destination": {"type": "string", "description": "Arrival city or airport code"},
                "date": {"type": "string", "description": "Departure date YYYY-MM-DD"},
                "return_date": {"type": "string", "description": "Return date for round trip (optional)"},
                "cabin": {"type": "string", "description": "economy | business | first (default: economy)"}
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Executes multi-step goals requiring 2+ different tools in sequence. "
            "Use for goals like 'research X and save to a file', 'search flights and send results'. "
            "Also use when user says 'and then', 'after that', or chains two distinct actions. "
            "Do NOT use for single-tool tasks — call that tool directly instead. "
            "If the plan turns out to have 2+ steps, this returns a CONFIRMATION REQUIRED prompt "
            "with the plan instead of running it — relay the plan and wait for a yes/no, same as "
            "any other held action. A 1-step plan just runs immediately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Complete description of what to accomplish"},
                "priority": {"type": "string", "description": "low | normal | high"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Recalls facts about the user from long-term memory. "
            "Call this IMMEDIATELY and SILENTLY whenever the user asks about their preferences, "
            "projects, habits, settings, history, or anything personal. "
            "Examples: 'what is my favorite X', 'what do I usually use', 'what was that project', "
            "'do you remember my X', 'what did I tell you about Y'. "
            "Do NOT say 'let me check' — just call this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up — in plain English e.g. 'favorite project' or 'preferred editor'"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Silently saves an important fact about the user to long-term memory. "
            "Call automatically when user reveals their name, preferences, projects, habits, or relationships. "
            "Do not announce that you are saving — just call it quietly. "
            "ALL THREE fields are required every time — category, key, AND value. "
            "Never call this with only a 'value' or 'fact' field; always decide a category and a short key too."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "identity | preferences | relationships | wishes | notes"},
                "key": {"type": "string", "description": "REQUIRED. Short snake_case key, e.g. 'favorite_music_genre' or 'currently_watching' — never omit this."},
                "value": {"type": "string", "description": "Concise value in English"}
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "focus_session",
        "description": (
            "Starts/controls a focus session — FRIDAY watches which window is frontmost "
            "and calls the user out when they drift off what they started on. "
            "Call for 'lock me in', 'focus session', 'keep me on task', 'stop me getting distracted', "
            "for pausing/resuming/extending/cancelling a running one, and for 'lock on this'/"
            "'keep me here'/'this is the tab'/'change what I'm locked on' (action=retarget) — "
            "changes the lock target without ending the session. Note: retarget phrases are "
            "normally caught earlier by the intent router for speed; only call this tool for "
            "retarget if that didn't already happen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "start | pause | resume | extend | retarget | abort | set_nag_interval"},
                "minutes": {"type": "integer", "description": "Session length for 'start' (default 25), minutes to add for 'extend' (default 10), or seconds between reminders for 'set_nag_interval'"},
                "label": {"type": "string", "description": "Optional short name for what they're working on, e.g. 'thesis'"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "power_control",
        "description": (
            "Controls system power state: shutdown, restart, sleep, hibernate, lock screen. "
            "ALWAYS call this — never say 'Shutting down' without calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "shutdown | restart | sleep | hibernate | lock"}
            },
            "required": ["action"]
        }
    },
]

# Build OpenAI-format tools list once at module load — reused every request
_OAI_TOOLS = [{"type": "function", "function": {
    "name": t["name"], "description": t["description"], "parameters": t["parameters"],
}} for t in TOOL_DEFINITIONS]

# Ollama tool hint injected into system prompt (built once)
_OLLAMA_TOOL_HINT = (
    "\n\nCRITICAL RULES:\n"
    "1. NEVER say you did something unless you emitted the JSON tool call.\n"
    "2. NEVER confirm playing music, opening apps, or any action without the JSON.\n"
    "3. To take ANY action, emit a single JSON object on its own line (nothing else on that line):\n"
    '{"tool":"open_app","app_name":"<app name>"}\n'
    '{"tool":"spotify_control","action":"play","query":"<song or artist>"}\n'
    '{"tool":"spotify_control","action":"pause"}\n'
    '{"tool":"spotify_control","action":"next"}\n'
    '{"tool":"web_search","query":"<search query>"}\n'
    '{"tool":"screen_process","text":"<what to look for/describe>","angle":"screen"}\n'
    '{"tool":"screen_process","text":"<what to look for/describe>","angle":"screen","app_name":"<app the user named, e.g. opera>"}\n'
    '{"tool":"send_message","receiver":"<contact name as saved>","message_text":"<text>","platform":"WhatsApp"}\n'
    '{"tool":"computer_settings","description":"<what to do>"}\n'
    '{"tool":"weather_report","city":"<city name>"}\n'
    '{"tool":"youtube_video","action":"play","query":"<query>"}\n'
    '{"tool":"file_controller","action":"list","path":"desktop"}\n'
    '{"tool":"file_controller","action":"read","path":"C:\\\\Archit\\\\Projects\\\\JARVIS\\\\main.py"}\n'
    '{"tool":"file_controller","action":"create_folder","path":"C:\\\\Archit\\\\Projects","name":"JARVIS"}\n'
    '{"tool":"code","action":"write","task":"create a Python digital assistant","path":"C:\\\\Archit\\\\Projects\\\\JARVIS","language":"python"}\n'
    '{"tool":"code","action":"write","task":"write a snake game","language":"python"}\n'
    '{"tool":"code","action":"fix","task":"fix the bug in main.py","path":"C:\\\\project\\\\main.py","language":"python"}\n'
    '{"tool":"power_control","action":"lock"}\n'
    '{"tool":"topic_monitor","action":"add","topic":"<topic>"}\n'
    '{"tool":"topic_monitor","action":"remove","topic":"<topic>"}\n'
    '{"tool":"topic_monitor","action":"list"}\n'
    '{"tool":"open_loop","action":"add","text":"<what to follow up on later>"}\n'
    '{"tool":"open_loop","action":"resolve","query":"<id or words matching it>"}\n'
    '{"tool":"open_loop","action":"list"}\n'
    "4. After emitting JSON, add ONE short sentence confirming what you did (max 10 words).\n"
    "5. Normal replies: short, plain sentences, no markdown, no lists. Exception: if the honest "
    "answer is a list of several items (search results, multiple files, several options), use "
    "short numbered pointers instead — one line per item, just the name/label plus at most one "
    "distinguishing detail, not full specs or descriptions. Never recite a source's full text "
    "back — name the items, then ask if they want more on any of them.\n"
    "6. Use the code tool ONLY when the CONTENT being written is actual source code in a "
    "programming language (a script, a program, a function). For plain text or data — "
    "creating/editing a .txt file, writing a note, saving text someone dictated to you — "
    "use file_controller with action=write or create_file instead, even if the request uses "
    "words like \"write\" or \"create\". A file already correctly written does NOT need a "
    "second tool call to \"update\" it — don't re-write something that just succeeded.\n"
    "7. To read a file, use file_controller with action=read and the full path.\n"
    "8. code tool auto-creates folders — pass the full destination path in the path field.\n"
    "9. You have NO visual ability without screen_process — if asked what's on screen, "
    "to look at something, or to describe an image/webcam, you MUST call screen_process. "
    "Never claim you can't see the screen without calling it first. "
    "If the user names a SPECIFIC app (\"what's on opera\", \"check discord\", \"look at my code editor\"), "
    "pass that app's name in the app_name field — this can see that app even if it's not the frontmost "
    "window, so you do NOT need to ask the user to bring it to the front first.\n"
    "10. Never call the same tool twice in the same turn with a near-identical argument "
    "(e.g. calling spotify_control(query=\"chill vibes\") then again with \"Chill Vibes\"). "
    "Trust the tool result you already got and respond to it — don't re-call \"just to be sure.\""
)


# ── System prompt (cached, rebuilt every ~60s for time freshness) ─────────────

_prompt_cache: dict = {"runtime": "", "base": "", "base_snippet": "", "failures": "", "session": "", "built_at": 0.0}
_PROMPT_TTL = 60  # seconds


def _load_base_prompt() -> str:
    try:
        return config.prompt_path.read_text(encoding="utf-8")
    except Exception:
        return "You are F.R.I.D.A.Y., a sharp and capable AI assistant. Be concise and call tools to complete tasks."


def _build_runtime_context() -> str:
    """
    Auto-detects OS, username, and common paths at runtime.
    No hardcoded personal info — discovers everything dynamically.
    Inspired by Mark XXXIX's config_manager pattern.
    """
    import platform, os, datetime
    from pathlib import Path

    lines = []

    # OS detection
    system = platform.system()
    os_name = {"Windows": "Windows 11", "Darwin": "macOS", "Linux": "Linux"}.get(system, system)
    lines.append(f"OS: {os_name}")

    # Username + home dir — auto-detected, never hardcoded
    home = Path.home()
    username = home.name
    lines.append(f"Username: {username}")
    lines.append(f"Home: {home}")

    # Common paths — built from home, not hardcoded
    lines.append(f"Desktop: {home / 'Desktop'}")
    lines.append(f"Downloads: {home / 'Downloads'}")
    lines.append(f"Documents: {home / 'Documents'}")

    # Windows-specific extras
    if system == "Windows":
        # Check for common project folders the user might have
        for candidate in ["Projects", "Dev", "Code", "Work"]:
            p = home.parent / candidate  # e.g. C:\Projects
            if p.exists():
                lines.append(f"Projects: {p}")
                break
        for candidate in [home.parent / candidate for candidate in ["Projects", "Dev"]]:
            if candidate.exists():
                break

    lines.append(f"Current time: {datetime.datetime.now().strftime('%A, %B %d, %Y — %I:%M %p')}")

    return "[RUNTIME CONTEXT — use naturally when relevant]\n" + "\n".join(lines)


def build_system_prompt(force: bool = False, query: str = "") -> str:
    """Builds the full system prompt. Everything except long-term memory
    is expensive-ish and rarely changes turn to turn, so it's cached for
    _PROMPT_TTL seconds. Memory is deliberately NOT part of that cache:
    it depends on `query` (the current turn's user text), which is
    different every turn — caching it would mean semantic retrieval,
    and the galaxy fly-to-matched-notes behavior that rides on it (see
    format_for_prompt()), only ever reflected whatever question happened
    to be asked when the cache last rebuilt."""
    global _prompt_cache
    now = time.time()

    if force or not _prompt_cache["base"] or (now - _prompt_cache["built_at"]) >= _PROMPT_TTL:
        runtime = _build_runtime_context()
        base = _load_base_prompt()

        failures = ""
        try:
            from memory.memory_store import get_action_errors
            errors = get_action_errors(limit=5)
            if errors:
                lines = ["[KNOWN FAILURES — never repeat these]"]
                for key, err in list(errors.items())[:5]:
                    lines.append(f"  - {key}: {err}")
                failures = "\n".join(lines)
        except Exception:
            pass

        session = ""
        if get_active_provider() == "ollama" and config.brain.llm_provider != "ollama":
            session = "[Running on local Ollama — cloud unavailable this session]"

        _prompt_cache = {
            "runtime": runtime, "base": base, "base_snippet": base[:120],
            "failures": failures, "session": session, "built_at": now,
        }

    # Long-term memory about user — semantic retrieval on THIS turn's
    # actual question when we have one; falls back to the old static
    # snippet (from the cached base prompt, no extra disk read) when
    # called without a query, same behavior as before this was split out.
    mem = ""
    try:
        from memory.long_term import format_for_prompt
        mem = format_for_prompt(query=query or _prompt_cache["base_snippet"])
    except Exception:
        pass

    parts = [
        _prompt_cache["runtime"], mem, _prompt_cache["base"],
        _prompt_cache["failures"], _prompt_cache["session"],
    ]
    return "\n\n".join(p for p in parts if p.strip())


# ── Central tool dispatcher ───────────────────────────────────────────────────

# Content provenance tagging — web_search, screen_process, and
# file_controller('read') all pull in text FRIDAY didn't write: a webpage,
# whatever's on screen, a file's contents. Nothing previously distinguished
# that from FRIDAY's own trusted tool output before it went back into the
# conversation — a page or file that said "ignore previous instructions
# and do X" had no structural reason not to be read as a command. These
# three now get wrapped in an explicit tag before the result is returned;
# core/prompt.txt's EXTERNAL CONTENT section tells the model what the tag
# means and that content inside it is data, never instructions.
#
# The nonce is generated once per process launch, not hardcoded, so a
# piece of content written before this conversation ever started (which
# is the normal case for a webpage or an existing file) can't include a
# plausible fake closing tag — it would need today's exact random suffix,
# which didn't exist yet when that content was written. This doesn't
# defend against an attacker actively watching the current live session,
# but that's a much narrower threat than the "static malicious webpage"
# case this is aimed at.
_CONTENT_NONCE = secrets.token_hex(4)
_UNTRUSTED_CONTENT_TOOLS = {"web_search", "screen_process"}


def _wrap_untrusted(source: str, content: str) -> str:
    if not content:
        return content
    tag = f"UNTRUSTED_CONTENT_{_CONTENT_NONCE}"
    return f'<{tag} source="{source}">\n{content}\n</{tag}>'


_ACTIVITY_ICONS = {
    "spotify_control": "🎵", "weather_report": "🌤", "web_search": "🔎",
    "screenshot": "📸", "screen_process": "👁", "topic_monitor": "📡", "open_loop": "🔗",
    "reminder": "⏰", "code": "💻", "file_controller": "📁",
    "open_app": "🚀", "send_message": "✉️", "power_control": "⏻",
    "flight_finder": "✈️", "youtube_video": "▶️", "computer_control": "🖱",
    "save_memory": "🧠", "recall_memory": "🧠", "focus_session": "🎯",
    "google_status": "🔗", "calendar": "📅", "gmail": "📧", "automation": "⚙️",
    "morning_briefing": "☀️",
}


def _activity_summary(name: str, args: dict) -> str:
    """Short human-readable line for the Activity Feed — not the full
    tool result, just enough to show what happened at a glance."""
    if name == "spotify_control":
        return f"Spotify: {args.get('action', '?')} {args.get('query', '')}".strip()
    if name == "web_search":
        return f"Searched: {args.get('query', '')}"
    if name == "topic_monitor":
        return f"Monitor: {args.get('action', '?')} {args.get('topic', '')}".strip()
    if name == "weather_report":
        return f"Weather: {args.get('city', 'current location')}"
    if name == "open_app":
        return f"Opened {args.get('name', args.get('app', '?'))}"
    if name == "focus_session":
        act = args.get("action", "?")
        mins = args.get("minutes")
        return f"Focus: {act}" + (f" ({mins}m)" if act in ("start", "extend") and mins else "")
    if args:
        first_val = next(iter(args.values()), "")
        return f"{name}: {str(first_val)[:50]}"
    return name.replace("_", " ")


_verifier = None


def _get_verifier():
    global _verifier
    if _verifier is None:
        from verifier import Verifier
        _verifier = Verifier()
    return _verifier


async def _dispatch_tool(name: str, args: dict,
                          speak_fn: Optional[Callable] = None,
                          _confirmed: bool = False) -> str:
    # Multi-step plan preview — checked before the general risk gate below,
    # since this is a different kind of hold: not "this one action is
    # dangerous", but "this request implies several actions and the user
    # hasn't seen the whole batch yet". Reuses the exact same pending-
    # confirmation mechanism (set_pending/confirmation_prompt/is_confirmation)
    # rather than a second one — the model gets a directive prompt back
    # either way, and start.py's confirmation handling doesn't need to know
    # which kind of hold it's looking at.
    if name == "agent_task":
        plan = args.get("_plan")
        if plan is None and not _confirmed:
            from agent.planner import plan_task
            goal = args.get("goal", "")
            plan = await plan_task(goal)
            if not plan:
                return "Couldn't come up with a plan for that, boss. Try rephrasing?"

            if len(plan) >= 2:
                from sentinel import set_pending
                preview_args = dict(args)
                preview_args["_plan"] = plan
                set_pending("agent_task", preview_args)
                logger.warning(f"[Sentinel] Held {len(plan)}-step plan pending confirmation: {goal!r}")
                try:
                    from ui.ws_server import send_activity
                    await send_activity(f"Plan ready ({len(plan)} steps) — awaiting confirmation", "📋")
                except Exception:
                    pass
                plan_lines = "\n".join(
                    f"{i+1}. {s.get('description') or s.get('tool', '?')}"
                    for i, s in enumerate(plan)
                )
                return (
                    f"[CONFIRMATION REQUIRED — do NOT say this is done, it has NOT run yet. "
                    f"Show/speak this exact plan to the user, then ask them to say 'yes' to run "
                    f"all {len(plan)} steps or 'no' to cancel — it will only run if they confirm:\n"
                    f"{plan_lines}]"
                )
            # A single-step plan doesn't need a batch confirmation — attach
            # it to args so the dispatch below uses it directly instead of
            # planning a second time.
            args = dict(args)
            args["_plan"] = plan

    # Confirmation gate — checked before anything else runs. A HIGH-risk
    # action never executes on the first ask; it's held and the model is
    # handed a directive prompt to relay instead. _confirmed=True is only
    # ever passed by the direct re-dispatch in _handle_user_text after the
    # user has actually said yes on a later turn — never by the model's
    # own tool-calling loop, so an LLM can't talk its way past this by
    # claiming confirmation on its own.
    if not _confirmed:
        from sentinel import classify_risk, RiskLevel, set_pending, confirmation_prompt
        if classify_risk(name, args) == RiskLevel.HIGH:
            set_pending(name, args)
            logger.warning(f"[Sentinel] Held high-risk action pending confirmation: {name}({args})")
            try:
                from ui.ws_server import send_activity
                await send_activity(f"Held for confirmation: {_activity_summary(name, args)}", "🛑")
            except Exception:
                pass
            return confirmation_prompt(name, args)

    result = await _dispatch_tool_impl(name, args, speak_fn)

    if name in _UNTRUSTED_CONTENT_TOOLS or (
        name == "file_controller" and args.get("action") in ("read", "read_file")
    ):
        result = _wrap_untrusted(name, result)

    activity_text = _activity_summary(name, args)
    activity_icon = _ACTIVITY_ICONS.get(name, "◆")

    # Independent verification — a handler's own "success" string is not
    # the final word. This re-checks real state (process list, actual
    # volume, actual file, actual monitor list, actual scheduled task)
    # and overrides a false-positive claim rather than trusting it.
    try:
        from verifier.models import VerificationStatus
        loop = asyncio.get_running_loop()
        verification = await loop.run_in_executor(
            None, _get_verifier().verify, name, args, str(result)
        )
        if verification.status == VerificationStatus.FAILED:
            logger.warning(f"[Verifier] {name} FAILED: {verification.reason}")
            result = (
                f"{result}\n\n"
                f"[VERIFICATION: independently checked and this did NOT actually happen — "
                f"{verification.reason} Tell the user honestly, don't just repeat the "
                f"success message above.]"
            )
            activity_icon = "⚠️"
            activity_text = f"{activity_text} — verification failed"
        elif verification.status == VerificationStatus.VERIFIED:
            logger.debug(f"[Verifier] {name} verified: {verification.reason}")
    except Exception as e:
        logger.debug(f"[Verifier] {name} unavailable: {e}")

    try:
        from ui.ws_server import send_activity
        await send_activity(activity_text, activity_icon)
    except Exception:
        pass  # Activity Feed is a nice-to-have — never let it break a tool call

    # Last-mentioned-thing tracking, for bare-pronoun follow-ups like
    # "kill it" / "stop it" / "close it" / "pause it". Only tracks on an
    # apparent success: the relevant arg must be non-empty (filters out
    # e.g. "please specify a topic to monitor") and the result can't
    # contain common failure phrasing or an explicit Verifier FAILED
    # marker — a wrongly-tracked target could mean "kill it" later acts
    # on the wrong thing, so this stays conservative.
    _failure_markers = ("couldn't", "can't", "cannot", "failed", "not found",
                         "unknown", "error", "sorry", "did not actually happen")
    _looks_ok = not any(m in str(result).lower() for m in _failure_markers)

    if _looks_ok and name == "open_app":
        app_name = (args.get("app_name") or "").strip()
        if app_name:
            set_last_mentioned("app", app_name, app_name)
    elif _looks_ok and name == "topic_monitor" and args.get("action") in ("add", "remove"):
        topic = (args.get("topic") or "").strip()
        if topic:
            set_last_mentioned("topic", topic, topic)
    elif _looks_ok and name == "open_loop" and args.get("action") == "add":
        loop_text = (args.get("text") or "").strip()
        if loop_text:
            set_last_mentioned("loop", loop_text, loop_text)
    elif _looks_ok and name == "spotify_control" and args.get("action") == "play":
        query = (args.get("query") or "").strip()
        if query:
            set_last_mentioned("spotify", query, query)

    return result


async def _dispatch_tool_impl(name: str, args: dict,
                               speak_fn: Optional[Callable] = None) -> str:
    logger.info(f"[Tool] {name}({args})")
    try:
        if name == "recall_memory":
            query = args.get("query", "")
            from memory import long_term as lt
            # Semantic search first
            hits = lt._search_index(query)
            if hits:
                # FIX: _search_index meta stores {cat, key}, NOT {category, value}.
                # Look up actual values from the live long_term memory data.
                mem_data = lt.get_all()
                lines = []
                for h in hits:
                    cat = h.get("cat", "")
                    key = h.get("key", "")
                    entry = mem_data.get(cat, {}).get(key)
                    val = lt._entry_value(entry) if entry else ""
                    if val:
                        lines.append(f"{cat}/{key}: {val}")
                return "\n".join(lines) if lines else "Nothing found."
            # Fallback: scan all memory for keyword match
            mem = lt.get_all()
            q = query.lower()
            matches = []
            for cat, items in mem.items():
                if not isinstance(items, dict): continue
                for key, entry in items.items():
                    val = lt._entry_value(entry)
                    if q in key.lower() or q in val.lower():
                        matches.append(f"{cat}/{key}: {val}")
            return "\n".join(matches) if matches else "I don't have that in memory yet, boss."

        elif name == "save_memory":
            from memory.long_term import remember
            key = str(args.get("key", "")).strip()
            value = str(args.get("value", "")).strip()
            category = str(args.get("category", "notes")).strip() or "notes"

            if not key or not value:
                # The model didn't follow the declared schema (category/key/
                # value, all required) — seen in practice with local models
                # sending a single free-text field instead, e.g.
                # {"fact": "...", "value": "..."}. Don't just dump the raw
                # sentence into "notes" with an ugly auto-generated key —
                # that's what this used to do, and it produced entries
                # like "Archit Loves The Show House 2004: Archit loves the
                # show House (2004)", a category-less restatement of
                # itself. Re-run it through the same LLM extraction
                # extract_and_save_facts() already uses for the automatic
                # background path, so it gets a real category and a short,
                # sensible key — falling back to the raw-note dump only if
                # that extraction itself doesn't work out, so nothing is
                # ever silently lost either way.
                fallback_text = next(
                    (str(v).strip() for v in args.values() if str(v).strip()), ""
                )
                if not fallback_text:
                    logger.warning(f"[save_memory] No usable content in args: {args}")
                    return "failed — the tool call had no usable content to save"

                try:
                    from memory.memory_store import _llm_call
                    prompt = (
                        f'Extract ONE saveable fact from: "{fallback_text}"\n'
                        'Return a single JSON object {"category":"identity|preferences|relationships|wishes|notes",'
                        '"key":"short_snake_case","value":"concise"}. No markdown, no explanation.'
                    )
                    raw = await _llm_call(prompt)
                    text = re.sub(r"```json|```", "", raw).strip()
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    parsed = json.loads(match.group(0) if match else text)
                    extracted_key = str(parsed.get("key", "")).strip()
                    extracted_value = str(parsed.get("value", "")).strip()
                    extracted_category = str(parsed.get("category", "")).strip()
                    if not extracted_key or not extracted_value:
                        raise ValueError("extraction returned an empty key or value")
                    from memory.long_term import VALID_CATEGORIES
                    if extracted_category not in VALID_CATEGORIES:
                        extracted_category = "notes"
                    key, value, category = extracted_key, extracted_value, extracted_category
                    logger.warning(
                        f"[save_memory] Model sent non-schema args {args} — "
                        f"re-extracted as {category}/{key} instead of a raw note"
                    )
                except Exception as e:
                    logger.warning(
                        f"[save_memory] Re-extraction failed ({e}) — falling back to a raw note for: {args}"
                    )
                    words = re.sub(r"[^\w\s]", "", fallback_text).split()[:8]
                    if words:
                        key = "_".join(words).lower()
                    else:
                        from datetime import datetime as _dt
                        key = _dt.now().strftime("note_%Y%m%d_%H%M%S")
                    value, category = fallback_text, "notes"

            saved = remember(key=key, value=value, category=category)
            return "ok" if saved else "failed — nothing was actually saved (duplicate value or invalid input)"

        elif name == "open_app":
            from brain.handlers import handle_open_app
            return await handle_open_app(args.get("app_name", ""))

        elif name == "web_search":
            from brain.handlers import handle_web_search
            return await handle_web_search(args.get("query", ""))

        elif name == "computer_settings":
            from brain.handlers import handle_computer_settings
            return await handle_computer_settings(args)

        elif name == "browser_control":
            from brain.handlers import handle_browser_control
            return await handle_browser_control(args)

        elif name == "file_controller":
            from brain.handlers import handle_file_controller
            return await handle_file_controller(args)

        elif name == "send_message":
            from brain.handlers import handle_send_message
            return await handle_send_message(args)

        elif name == "spotify_control":
            from brain.handlers import handle_spotify
            return await handle_spotify(args)

        elif name == "google_status":
            from brain.handlers import handle_google_status
            return await handle_google_status(args)

        elif name == "weather_report":
            from brain.handlers import handle_weather
            return await handle_weather(args.get("city", ""))

        elif name == "youtube_video":
            from brain.handlers import handle_youtube
            return await handle_youtube(args)

        elif name == "screenshot":
            from brain.handlers import handle_screenshot
            return await handle_screenshot(args.get("save_path"))

        elif name == "screen_process":
            from brain.handlers import handle_screen_process
            return await handle_screen_process(args, speak_fn)

        elif name == "reminder":
            from brain.handlers import handle_reminder
            return await handle_reminder(args)

        elif name == "calendar":
            from brain.handlers import handle_calendar
            return await handle_calendar(args)

        elif name == "gmail":
            from brain.handlers import handle_gmail
            return await handle_gmail(args)

        elif name == "automation":
            from brain.handlers import handle_automation
            return await handle_automation(args)

        elif name == "morning_briefing":
            from brain.handlers import handle_morning_briefing
            return await handle_morning_briefing(args)

        elif name == "wait":
            # Only meaningful inside a planned multi-step task (executed via
            # agent/executor.py's execute_plan, which already wraps every
            # step in asyncio.wait_for(..., timeout=config.agent.step_timeout_seconds)).
            # Capped safely under that per-step timeout so a wait step can
            # never itself trigger the step-timeout/skip path.
            try:
                requested = float(args.get("seconds", 0))
            except (TypeError, ValueError):
                requested = 0
            cap = max(1, config.agent.step_timeout_seconds - 10)
            seconds = max(0.0, min(requested, cap))
            await asyncio.sleep(seconds)
            return f"Waited {seconds:g}s."

        elif name == "agent_task":
            from agent.task_queue import get_queue, TaskPriority
            pmap = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL, "high": TaskPriority.HIGH}
            priority = pmap.get(args.get("priority", "normal"), TaskPriority.NORMAL)
            # Pass on_chunk so executor can stream summary to UI word-by-word
            task_id = get_queue().submit(
                goal=args.get("goal", ""),
                priority=priority,
                speak=speak_fn,
                on_chunk=speak_fn,  # reuse speak for streaming in voice mode
                plan=args.get("_plan"),  # already planned (and possibly confirmed) above
            )
            return f"Task queued (ID: {task_id}). I'm on it, boss."

        elif name == "focus_session":
            from actions.focus_session import focus_session
            return await focus_session(args)

        elif name == "power_control":
            from brain.handlers import handle_power
            return await handle_power(args.get("action", ""))

        elif name == "desktop_control":
            from actions.desktop import desktop_control
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: desktop_control(args))

        elif name == "computer_control":
            from actions.computer_control import computer_control
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: computer_control(args))

        elif name == "code":
            from actions.coding_agent import handle_code
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: handle_code(args))

        elif name == "file_processor":
            from actions.file_processor import file_processor
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: file_processor(
                file_path=args.get("file_path", ""),
                task=args.get("task", "summarize")
            ))

        elif name == "game_updater":
            from actions.game_updater import game_updater
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: game_updater(args))

        elif name == "flight_finder":
            from actions.flight_finder import flight_finder
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: flight_finder(args))

        elif name == "topic_monitor":
            from brain.handlers import handle_topic_monitor
            return await handle_topic_monitor(args)

        elif name == "open_loop":
            from brain.handlers import handle_open_loop
            return await handle_open_loop(args)

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        logger.error(f"Tool '{name}' failed: {e}")
        try:
            from memory.memory_store import save_action_error
            save_action_error(f"{name}:{str(args)[:60]}", str(e)[:200])
        except Exception:
            pass
        return f"Tool failed: {e}"


# ── Shared streaming helpers ──────────────────────────────────────────────────

def _make_openai_stream_handler():
    """Returns per-request state containers for streaming."""
    return {
        "full_response": "",
        "buffer": "",
        "tool_calls_acc": {},
    }


async def _yield_buffer(state: dict):
    """
    Flush remaining text in buffer.
    NOTE: This is a helper async generator kept for potential future use.
    It is NOT called by the streaming pipeline — buffer flushing is done
    inline (`yield state["buffer"].strip()`) to avoid extra generator overhead.
    Do not call this and await it — iterate it with `async for`.
    """
    if state["buffer"].strip():
        yield state["buffer"].strip()
        state["buffer"] = ""



def _sanitize_token(token: str) -> str:
    """
    Strips stray script characters and markdown noise some models emit
    mid-stream (used on every streamed token, regardless of provider).
    Only removes: CJK/Arabic script chars, markdown table rows.
    Does NOT strip spaces — that caused concatenation bugs.
    """
    import unicodedata, re

    result = []
    for ch in token:
        # Keep spaces, newlines, tabs always
        if ch in (" ", "\n", "\t"):
            result.append(ch)
            continue
        # Keep all printable non-script chars
        if not ch.isprintable():
            continue
        n = unicodedata.name(ch, "")
        # Only block known non-Latin script blocks
        if any(n.startswith(s) for s in (
            "CJK", "HANGUL", "ARABIC", "HIRAGANA", "KATAKANA",
            "DEVANAGARI", "THAI", "HEBREW"
        )):
            continue
        result.append(ch)

    c = "".join(result)
    # Strip markdown table rows only (not regular pipes)
    c = re.sub(r"^\s*\|[-: |]+\|\s*$", "", c, flags=re.MULTILINE)
    c = re.sub(r"^\s*\|.{3,}\|\s*$", "", c, flags=re.MULTILINE)
    # Strip **bold** and ## headers
    c = re.sub(r"\*{2,}(.*?)\*{2,}", r"\1", c)
    c = re.sub(r"^#{1,6}\s+", "", c, flags=re.MULTILINE)
    return c


async def _process_token(state: dict, token: str):
    """Accumulate token, split on sentence boundaries, yield complete sentences."""
    token = _sanitize_token(token)
    if not token:
        return []
    state["full_response"] += token
    state["buffer"] += token
    from voice.tts import split_into_sentences
    sentences = split_into_sentences(state["buffer"])
    result = []
    if len(sentences) > 1:
        for s in sentences[:-1]:
            if s.strip():
                result.append(s.strip())
        state["buffer"] = sentences[-1]
    return result


async def _execute_tool_calls(state: dict, speak_fn: Optional[Callable]) -> list[dict]:
    """Execute accumulated tool calls. Returns list of (tool_call, result) for agentic loop."""
    results = []
    for tc_id, tc_data in state["tool_calls_acc"].items():
        name = tc_data["name"]
        call_id = tc_data["id"] or f"call_{tc_id}"
        try:
            args = json.loads(tc_data["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        if name:
            result = await _dispatch_tool(name, args, speak_fn)
            logger.info(f"[Tool result] {name}: {str(result)[:120]}")
            results.append({"call_id": call_id, "name": name, "args": args, "result": str(result)})
    return results


# ── Session file context ──────────────────────────────────────────────────────
# Track the last file path mentioned in conversation so follow-up commands
# like "update it" / "fix it" / "now run it" resolve to the correct file.
import re as _re
_session_last_file: str = ""

def _update_session_file(text: str) -> None:
    """Remember the last file path mentioned so follow-ups like 'update it' resolve correctly."""
    global _session_last_file
    import re as _refile
    # Match Windows absolute paths, e.g. C:\Archit\Projects\JARVIS\main.py
    m = _refile.search(r"[A-Za-z]:[/\\][^\s\"']+", text)
    if m:
        _session_last_file = m.group(0).rstrip(".'")

def get_session_last_file() -> str:
    return _session_last_file


# ── Last mentioned thing (app / topic monitor / spotify track) ────────────────
# Small, specific tracker for bare-pronoun follow-ups like "kill it" / "stop
# it" / "close it" / "pause it" — NOT full session memory, just the single
# most recent instance of three action-appropriate categories. Same pattern
# as _session_last_file above, generalized to more than just file paths.
_last_mentioned: dict = {}  # {"kind": "app"|"topic"|"spotify", "label": str, "ref": str}


def set_last_mentioned(kind: str, label: str, ref: str) -> None:
    global _last_mentioned
    _last_mentioned = {"kind": kind, "label": label, "ref": ref}


def get_last_mentioned() -> dict:
    return _last_mentioned


def clear_last_mentioned() -> None:
    global _last_mentioned
    _last_mentioned = {}


# ── Provider: Ollama ──────────────────────────────────────────────────────────

_TOOL_CALL_START = re.compile(r'\{\s*"tool"\s*:')

# Belt-and-suspenders: catches a broken tool-call fragment that lost its
# opening brace somewhere (round boundaries reset the buffer, so a JSON
# call split across two rounds can leave an orphaned tail like
# 'tool":"spotify_control","action":"play"}' with nothing recognizing it
# as JSON anymore). Real spoken sentences don't start with 'word":' —
# this is cheap enough to run on everything we're about to yield.
_JSON_FRAGMENT_RE = re.compile(r'^"?\w+"\s*:\s*[\{"\[]')


def _looks_like_json_fragment(s: str) -> bool:
    return bool(_JSON_FRAGMENT_RE.match(s.strip()))


def _extract_tool_call(buffer: str):
    """Find a {"tool": ...} object anywhere in buffer (not just at the
    very start) and return (obj, buffer_with_json_removed) — or
    (None, buffer) if there isn't one (yet, or at all).

    The old check only fired when the WHOLE buffer, stripped, was exactly
    one JSON object. Real model output is usually
    "Got it, I'll remember that. {"tool":"save_memory",...}" — text THEN
    JSON — which that check never matched, so the tool never actually ran
    and the raw JSON leaked into what got shown/spoken instead.
    """
    m = _TOOL_CALL_START.search(buffer)
    if not m:
        return None, buffer

    start = m.start()
    depth = 0
    in_string = False
    escape = False
    end = None
    for i in range(start, len(buffer)):
        c = buffer[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        # Opening brace seen but not closed yet — still streaming in,
        # not an error. Caller should wait for more tokens.
        return None, buffer

    candidate = buffer[start:end]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None, buffer

    if "tool" not in obj:
        return None, buffer

    remaining = (buffer[:start] + buffer[end:]).strip()
    return obj, remaining


async def _stream_ollama(user_text: str, system_prompt: str,
                          speak_fn: Optional[Callable] = None) -> AsyncIterator[str]:
    """
    Ollama streaming with an agentic follow-up loop.

    Previously, after dispatching a tool (e.g. file read), the result was
    silently discarded and Ollama never saw it — so it never followed up.
    Now we feed the tool result back into the conversation and call Ollama
    again so it can respond with the actual content (like NIM does).
    """
    try:
        import ollama as ol
    except ImportError:
        raise RuntimeError("pip install ollama")

    memory.add_user(user_text)
    _update_session_file(user_text)  # track last mentioned file path

    messages = [
        {"role": "system", "content": system_prompt + _OLLAMA_TOOL_HINT},
        *memory.get_messages(),
    ]

    full_response = ""
    MAX_ROUNDS = 4  # tool → response → tool → response …
    _calls_this_turn: set = set()  # (tool_name, normalized_args) — dedupe backstop
    # Set right after a round where _reminder_failed/_generic_failed fired
    # (below) — see the top of the next round's body and the post-stream
    # check after the token loop for how this is used.
    _check_next_round_for_false_success = False
    _last_failed_tool_result = None

    # Separate, narrower gate: a calendar/reminder-flavored request answered
    # with NO tool call at all and a false "I can't do that" — confirmed
    # happening even after the calendar tool's schema was made more
    # explicit about this being false (twice now: "I can't do that
    # directly" for screen_process-vs-calendar confusion, then "I can't
    # add calendar events directly" for calendar itself, both times with
    # zero tool call attempted). A fourth wording pass isn't the fix —
    # this checks the FIRST round's own output before it's spoken, the
    # same way _check_next_round_for_false_success checks a post-failure
    # round, and forces one real attempt instead of accepting the denial.
    # Capped at one retry so a model that's genuinely still going to
    # decline doesn't loop forever — see MAX_ROUNDS.
    _CALENDAR_TRIGGER_WORDS = ("calendar", "schedule", "event", "appointment", "reminder", "meeting")
    _check_round0_for_denial = any(w in user_text.lower() for w in _CALENDAR_TRIGGER_WORDS)
    _denial_retry_used = False
    _FALSE_DENIAL_MARKERS = (
        "i can't add", "i can't create", "i cannot add", "i cannot create",
        "i can't directly", "i cannot directly", "i don't have access to your calendar",
        "i'm not able to add", "i'm not able to create", "i can't do that directly",
        "i can't add events", "i can't add calendar", "i don't have the ability",
        "i'm unable to add", "i'm unable to create",
    )

    try:
        for round_n in range(MAX_ROUNDS):
            buffer = ""
            round_response = ""
            tool_name_called = None
            tool_action_called = None
            tool_result = None
            # This round's own copy — the flag above gets reused by the
            # NEXT round-that-follows-a-failure, so it has to be read into
            # a local and cleared before this round can set it again.
            _this_round_checks_failure = _check_next_round_for_false_success
            _check_next_round_for_false_success = False
            _this_round_checks_denial = (round_n == 0 and _check_round0_for_denial and not _denial_retry_used)

            # num_predict: 300 for fresh turns, 500 for follow-up rounds.
            _np = 500 if round_n > 0 else 300
            try:
                stream = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda msgs=messages, np=_np: ol.chat(
                            model=config.brain.ollama_model,
                            messages=msgs,
                            stream=True,
                            # Lowered from 0.75/0.92 — this round both PICKS
                            # tools and writes the final reply, and high
                            # temperature is exactly the kind of thing that
                            # makes a model less likely to reliably call the
                            # right tool, follow the JSON format, or stick to
                            # what a tool result actually said instead of
                            # embellishing it. 0.2 trades a little variety in
                            # phrasing for a lot more reliability on both.
                            options={"temperature": 0.2, "top_p": 0.85,
                                     "num_predict": np, "num_ctx": 8192},
                        ),
                    ),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[Ollama] Request timed out after 30s — breaking round")
                yield "Taking too long, boss. Try rephrasing or use 'switch to NIM' when it's back."
                break

            _chunk_deadline = time.time() + 20
            _stream_iter = iter(stream)
            _stalled = False
            while True:
                if time.time() > _chunk_deadline:
                    logger.warning("[Ollama] Per-chunk timeout — model stalled")
                    _stalled = True
                    break
                try:
                    chunk = next(_stream_iter)
                except StopIteration:
                    break
                _chunk_deadline = time.time() + 20
                try:
                    from ui.ws_server import get_stop_flag
                    if get_stop_flag():
                        break
                except Exception:
                    pass

                token = (chunk["message"]["content"] if isinstance(chunk, dict)
                         else chunk.message.content)
                if not token:
                    continue

                round_response += token
                buffer += token

                # Detect a tool call anywhere in the buffer (see
                # _extract_tool_call's docstring for why this replaced a
                # much narrower whole-buffer-is-JSON check).
                obj, remaining = _extract_tool_call(buffer)
                if obj is not None:
                    t_name = obj.pop("tool", None)
                    if t_name:
                        # Dedupe backstop — the prompt tells the model not
                        # to re-call the same tool with a near-identical
                        # argument in one turn, but models don't always
                        # listen. Normalize (lowercase, sorted keys) so
                        # "chill vibes" vs "Chill Vibes" still counts as
                        # the same call.
                        call_key = (t_name, json.dumps(obj, sort_keys=True).lower())
                        if call_key in _calls_this_turn:
                            logger.info(f"[Ollama Tool] skipped duplicate call: {t_name}({obj})")
                            tool_name_called = t_name
                            tool_action_called = obj.get("action", "")
                            tool_result = "(already did this — don't repeat it, just respond)"
                            buffer = ""
                            continue

                        _calls_this_turn.add(call_key)
                        tool_name_called = t_name
                        tool_action_called = obj.get("action", "")
                        tool_result = await _dispatch_tool(t_name, obj, speak_fn)
                        logger.info(f"[Ollama Tool] {t_name}: {str(tool_result)[:120]}")
                        # Discard `remaining` rather than carrying it into
                        # the next round. It's the lead-in text before the
                        # tool call ("On it.", "Sure thing.") — reliably
                        # just filler, and on a multi-tool-call turn each
                        # round adds its own, which is how "On it. On it,
                        # boss." happened. The real answer is the FINAL
                        # round's response after all tools are done, which
                        # still gets spoken normally below.
                        buffer = ""
                        continue

                if _TOOL_CALL_START.search(buffer) or "{" in buffer:
                    # A tool call looks like it's starting but hasn't
                    # closed yet — don't sentence-split this buffer. It
                    # doesn't know anything about JSON structure, so it can
                    # (and did) chop the JSON mid-stream, e.g. splitting
                    # right after the opening "{" and losing it for good —
                    # the rest of the JSON then has no opening brace left
                    # to be recognized by, and leaks through as plain text
                    # once it closes. Just keep accumulating tokens until
                    # _extract_tool_call above can find the closing brace.
                    #
                    # The bare "{" check (not just _TOOL_CALL_START, which
                    # needs the fuller '{"tool":' to have streamed in yet)
                    # matters on its own: a lead-in like 'Set it for you,
                    # boss. {"tool":...' sentence-splits as "boss." being a
                    # complete sentence the MOMENT the trailing "{" shows up
                    # (a period followed by any new character reads as "next
                    # sentence starting" to a naive splitter) — which yields
                    # "Set it for you, boss." before _TOOL_CALL_START's
                    # stricter pattern has even matched yet. That's how each
                    # retry round's lead-in text ("Set it for you, boss." /
                    # "Fixed that for you." / ...) was leaking through and
                    # stacking up across a multi-round tool-retry turn even
                    # though the code already discards buffer once a tool
                    # call is recognized — by then it was already too late,
                    # the lead-in had already been yielded (and, once
                    # yielded here, already spoken/displayed — there's no
                    # taking it back after the fact). Pausing on ANY brace,
                    # not just a confirmed tool-call prefix, closes that gap.
                    continue

                from voice.tts import split_into_sentences
                sentences = split_into_sentences(buffer)
                if len(sentences) > 1:
                    for s in sentences[:-1]:
                        clean = s.strip()
                        if clean and not clean.startswith("{") and not _looks_like_json_fragment(clean):
                            # Holding this round's text back entirely — see
                            # the post-loop check below for why: this is
                            # the one round where the model is reacting to
                            # a tool call that JUST failed, and the known,
                            # observed failure mode is a confident false
                            # "Done"/"Added"/"All set" opener that a LATER
                            # sentence in the SAME response then
                            # contradicts. Once a sentence is yielded
                            # here it's already been spoken — there's no
                            # unsaying it — so for this one round only,
                            # nothing streams out until the full response
                            # can be checked as a whole.
                            if not _this_round_checks_failure and not _this_round_checks_denial:
                                yield clean
                    buffer = sentences[-1]

            if _this_round_checks_denial and tool_result is None:
                # Nothing was streamed during the token loop above for
                # this round (same gate as the false-success check below)
                # — round_response holds the model's complete, still-
                # unspoken reply, and no tool was called at all. Check
                # for a false capability-denial before any of it reaches
                # the user.
                _denial_check_text = round_response.strip().lower()[:120]
                if any(m in _denial_check_text for m in _FALSE_DENIAL_MARKERS):
                    logger.warning(
                        f"[Ollama] Discarded false capability-denial {round_response[:60]!r} "
                        f"for a calendar/reminder-flavored request — forcing a real attempt."
                    )
                    _denial_retry_used = True
                    messages.append({"role": "assistant", "content": round_response})
                    messages.append({"role": "user", "content": (
                        "That was false — you DO have calendar and reminder tools that can create "
                        "events/reminders directly. Call one of them now. If something wasn't "
                        "specified (a title, an exact date), use your best guess or a placeholder — "
                        "the tool will tell you exactly what's missing so you can ask a real "
                        "follow-up question, instead of deciding upfront not to try."
                    )})
                    continue  # one more round, forced — do NOT yield the denial
                # Not actually a denial — genuinely honest text (e.g. "not
                # connected yet" is fine, that's real). The early per-
                # sentence yields above were suppressed for this whole
                # round though (same gate), so the full text has to be
                # yielded here explicitly — `buffer` alone only holds the
                # trailing fragment by this point, not everything that
                # was already split off sentence-by-sentence earlier.
                _text = round_response.strip()
                if _text and not _text.startswith("{") and not _looks_like_json_fragment(_text):
                    yield _text
                full_response += round_response
                break

            _FALSE_SUCCESS_OPENERS = (
                "done", "added", "all set", "you're all set", "set for",
                "scheduled", "sorted", "fixed", "consider it done",
                "got it done", "there you go", "taken care of", "no problem, it's done",
                "removed", "deleted", "cancelled", "canceled", "cleared", "gone",
            )

            if _this_round_checks_failure and tool_result is None:
                # Nothing was streamed during the token loop above for this
                # round (see the `if not _this_round_checks_failure` guard
                # a few lines up) — round_response now holds the model's
                # COMPLETE, still-unspoken reaction to a tool call that
                # just failed. Check it before ANY of it reaches the user:
                # the observed failure here is a confident opening claim
                # ("Done.", "Added to your calendar.") that a LATER
                # sentence in that SAME response then contradicts — a
                # prompt instruction telling the model not to do this
                # (above) measurably helped but did not reliably stop a
                # small local model from doing it anyway. This is the
                # backstop: catch it before it's spoken, not hope it
                # doesn't happen.
                _first = split_into_sentences(round_response.strip())
                _opener = _first[0].strip().lower() if _first else ""
                if any(_opener.startswith(m) for m in _FALSE_SUCCESS_OPENERS):
                    logger.warning(
                        f"[Ollama] Discarded false-success opener {_opener[:40]!r} "
                        f"after a failed tool call — substituting the real result."
                    )
                    _honest = _last_failed_tool_result or "That didn't actually work, boss."
                    yield _honest
                    full_response += _honest
                else:
                    _text = round_response.strip()
                    if _text and not _text.startswith("{") and not _looks_like_json_fragment(_text):
                        yield _text
                        full_response += round_response
                    elif _stalled:
                        # Same stall-watchdog fallback as the normal path
                        # below — without it, a stall on this specific
                        # held-back round would yield literally nothing.
                        yield "Model stalled out on that one, boss. Try again?"
                    else:
                        full_response += round_response
                break

            if not _this_round_checks_failure:
                if buffer.strip() and not buffer.strip().startswith("{") and not _looks_like_json_fragment(buffer.strip()):
                    yield buffer.strip()
                elif _stalled and not round_response.strip():
                    # Model produced nothing this round and the stall watchdog
                    # fired — without this, the generator just ends here with
                    # zero yields and FRIDAY says literally nothing.
                    yield "Model stalled out on that one, boss. Try again?"
            # else: _this_round_checks_failure but a NEW tool call WAS made
            # this round (tool_result is not None) — the model self-
            # corrected (e.g. switched from computer_settings to reminder).
            # Nothing to check or yield here: any lead-in text before that
            # tool call is discarded exactly the same way any tool-call
            # lead-in always is (see the comment further up on `remaining`)
            # — the follow_up-building code below runs normally on this
            # round's new tool_result, same as any other round.

            full_response += round_response

            # No tool was called this round — Ollama gave a text response, done.
            if tool_result is None:
                break

            # Tool was called — feed the result back and let Ollama respond.
            # The follow-up prompt is task-aware:
            # - If the user wanted to READ  → summarise the content
            # - If the user wanted to EDIT/UPDATE/FIX → must emit a code/write tool call next
            #   (Ollama's small model hallucinates "I updated it" without actually writing)
            _edit_signals = (
                "update", "edit", "modify", "change", "fix", "rewrite",
                "improve", "refactor", "add", "implement", "upgrade",
            )
            _user_wants_edit = any(w in user_text.lower() for w in _edit_signals)
            _file_was_read = tool_action_called in ("read", "read_file")

            # - If the user wanted to SEND A MESSAGE → must emit send_message next
            #   (open_app/screen_process only look at the screen — same
            #   hallucination shape as the file-read case above: the model
            #   sees a WhatsApp chat on screen via screen_process and, since
            #   screen_process is read-only, has nothing left to do but
            #   *say* "sending it now" instead of actually sending it)
            _message_signals = ("send", "message", "text him", "text her", "whatsapp", "telegram", " dm ")
            _user_wants_send = any(w in user_text.lower() for w in _message_signals)
            _send_already_called = any(k[0] == "send_message" for k in _calls_this_turn)

            # Reminder tool returns a small, exact set of English strings —
            # enumerated in actions handle_reminder — so failures are
            # detectable precisely, not guessed at. Without this, a failed
            # "set" call (e.g. missing the required separate 'date' field)
            # falls into the generic branch below, which just says
            # "respond based on this result" — and the small local model
            # reliably free-associates a plausible-sounding success instead
            # of relaying the plain-English failure it was just given. Same
            # hallucination shape as the file-edit/send-message cases above,
            # just not yet covered for this tool.
            _reminder_failed = (
                tool_name_called == "reminder"
                and tool_result.strip().startswith((
                    "I need", "Couldn't", "Reminder failed:", "Found more than one match",
                ))
            )

            if _reminder_failed:
                _check_next_round_for_false_success = True
                _last_failed_tool_result = tool_result
                from datetime import datetime as _dt
                follow_up = (
                    f"[Tool result for reminder]\n{tool_result}\n\n"
                    f"This reminder was NOT set — the call above FAILED, it did not succeed. "
                    f"Do not tell the user it was set, added, fixed, or scheduled; that would "
                    f"be false. Tell them plainly that it failed and why, using the error "
                    f"above. If the fix is simple — e.g. you're missing the separate 'date' "
                    f"field — work out the correct value yourself (today's date is "
                    f"{_dt.now().strftime('%Y-%m-%d')}) and emit exactly ONE corrected JSON "
                    f"tool call with 'date' (YYYY-MM-DD), 'time' (HH:MM), and 'message' (the "
                    f"reminder text — NOT 'text', the tool doesn't read that key) all set. "
                    f"Only say it succeeded once a tool result actually starts with "
                    f"'Reminder set' or 'Reminder noted'."
                )
            elif _user_wants_send and not _send_already_called and tool_name_called in (
                "open_app", "screen_process", "agent_task",
            ):
                follow_up = (
                    f"[Tool result for {tool_name_called}]\n{tool_result}\n\n"
                    f"The user asked you to SEND a message. {tool_name_called} does NOT send "
                    f"messages — it only looks at or opens the screen. The message has NOT been "
                    f"sent yet. You MUST emit a JSON tool call on its own line now:\n"
                    f'{{"tool":"send_message","receiver":"<contact>","message_text":"<text>","platform":"WhatsApp"}}\n'
                    f"Do NOT say the message was sent without emitting that JSON. Emit the tool call now."
                )
            elif _user_wants_edit and tool_name_called == "file_controller" and _file_was_read:
                # Read was step 1 — now demand a write tool call. Gated on
                # the action actually having been a READ: previously this
                # fired on file_controller being called at all, including
                # when it had just WRITTEN the file successfully — which
                # then force-instructed the model to redundantly "update"
                # an already-correct file through the code tool instead,
                # overwriting good content with a generated script. That's
                # what corrupted temp.txt: a write followed immediately by
                # an unnecessary forced second write through the wrong tool.
                follow_up = (
                    f"[File contents of {tool_name_called} read successfully]\n"
                    f"{tool_result}\n\n"
                    f"The user wants you to UPDATE this file. "
                    f"You MUST emit a JSON tool call on its own line to write the updated code. "
                    f"Use the code tool like this:\n"
                    f'{{"tool":"code","action":"write","task":"{user_text}","path":"<same path as the file you just read>","language":"python"}}\n'
                    f"Do NOT say you updated it without emitting that JSON. Emit the tool call now."
                )
            else:
                # Generic failure detector: the codebase's own convention
                # (checked across handlers.py) is that a failed/unhandled
                # tool call returns a plain-English string starting with
                # "Couldn't", "I need", "I can't", or "I'm not sure how
                # to" — enumerable, not guessed at. Without this, a tool
                # that fails or gets called wrongly (e.g. computer_settings
                # invoked for a calendar request it has no way to handle)
                # falls into the same generic "respond based on this
                # result" instruction as any successful read — and the
                # model free-associates a plausible opener ("Added to
                # your calendar.") before it's even looked at whether the
                # result says it worked, then contradicts itself once it
                # does. Same hallucination shape the _reminder_failed
                # block above exists for, just not scoped to one tool —
                # this is the catch-all for every other one, present and
                # future (Calendar/Gmail will have plenty of their own
                # failure strings once built).
                _generic_failed = tool_result.strip().lower().startswith((
                    "i need", "i can't", "i couldn't", "couldn't",
                    "i'm not sure how to", "i'm not sure",
                ))
                if _generic_failed:
                    _check_next_round_for_false_success = True
                    _last_failed_tool_result = tool_result
                    follow_up = (
                        f"[Tool result for {tool_name_called}]\n{tool_result}\n\n"
                        f"This did NOT succeed — the result above is a failure or "
                        f"'don't know how' response, not a confirmation. Do not say it "
                        f"was done, added, set, changed, or fixed — not even as an opening "
                        f"sentence you then correct. Tell the user plainly, in one "
                        f"consistent message, what went wrong and — only if there's an "
                        f"obviously better tool for this — you may emit that JSON tool "
                        f"call instead of just apologizing."
                    )
                else:
                    # Read-only — just summarise
                    follow_up = (
                        f"[Tool result for {tool_name_called}]\n{tool_result}\n\n"
                        f"Respond to the user based on this result. "
                        f"Be concise and natural. "
                        f"If you need to take a follow-up action, emit the appropriate JSON tool call."
                    )

            messages.append({"role": "assistant", "content": round_response.strip()})
            messages.append({"role": "user", "content": follow_up})
            # Reset for next round
            tool_result = None
            tool_name_called = None

        if full_response.strip():
            memory.add_assistant(full_response)
        asyncio.create_task(_background_memory(user_text, memory.get_all()))

    except Exception as e:
        logger.error(f"Ollama error: {e}")
        # Re-raise instead of swallowing: this used to yield a friendly
        # "Hit a snag" message here and stop, which meant a failed Ollama
        # call (not running, a :cloud model needing a subscription, a
        # network blip) was a dead end even with cloud providers fully
        # configured and working — the caller never got a chance to fall
        # back, unlike every other provider here which already falls
        # back to Ollama on failure. Letting this propagate lets the
        # dispatcher's _cloud_fallback() actually try something else.
        raise


# ── Provider: NVIDIA NIM ──────────────────────────────────────────────────────

async def _stream_nvidia_nim(user_text: str, system_prompt: str,
                              speak_fn: Optional[Callable] = None) -> AsyncIterator[str]:
    from openai import AsyncOpenAI, NOT_GIVEN

    MAX_TOOL_ROUNDS = 6   # max read→write→confirm cycles before stopping

    client = AsyncOpenAI(
        api_key=config.brain.nvidia_nim_api_key,
        base_url="https://integrate.api.nvidia.com/v1",
        max_retries=0,
        timeout=8.0,
    )

    memory.add_user(user_text)
    _update_session_file(user_text)

    # Build mutable message list for agentic loop
    messages = [{"role": "system", "content": system_prompt}, *memory.get_messages()]

    try:
        for round_n in range(MAX_TOOL_ROUNDS):
            state = _make_openai_stream_handler()

            stream = await client.chat.completions.create(
                model=config.brain.nvidia_nim_model,
                messages=messages,
                tools=_OAI_TOOLS,
                tool_choice="auto",
                # Same reasoning as _stream_ollama's temperature drop —
                # this is a tool-calling round, not free creative writing;
                # 0.85 was too high for reliable tool selection and
                # faithful use of tool results.
                temperature=0.2,
                max_tokens=2048 if round_n == 0 else 4096,
                stream=True,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )

            # Accumulate the full assistant message for history
            assistant_tool_calls = []

            async for chunk in stream:
                try:
                    from ui.ws_server import get_stop_flag
                    if get_stop_flag():
                        return
                except Exception:
                    pass

                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                if not delta:
                    continue

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in state["tool_calls_acc"]:
                            state["tool_calls_acc"][idx] = {"id": "", "name": "", "args": ""}
                        if tc.id:
                            state["tool_calls_acc"][idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                state["tool_calls_acc"][idx]["name"] += tc.function.name
                            if tc.function.arguments:
                                state["tool_calls_acc"][idx]["args"] += tc.function.arguments

                token = getattr(delta, "content", None) or ""
                if token:
                    for s in await _process_token(state, token):
                        yield s

            if state["buffer"].strip():
                yield state["buffer"].strip()

            # No tool calls this round — LLM gave a text response, we're done
            if not state["tool_calls_acc"]:
                if state["full_response"].strip():
                    memory.add_assistant(state["full_response"])
                asyncio.create_task(_background_memory(user_text, memory.get_all()))
                return

            # Execute tool calls and collect results
            tool_results = await _execute_tool_calls(state, speak_fn)

            if not tool_results:
                return

            # Append assistant's tool-call message to history
            oai_tool_calls = [
                {
                    "id": tr["call_id"],
                    "type": "function",
                    "function": {"name": tr["name"], "arguments": json.dumps(tr["args"])},
                }
                for tr in tool_results
            ]
            messages.append({
                "role": "assistant",
                "content": state["full_response"] or None,
                "tool_calls": oai_tool_calls,
            })

            # Append tool results so LLM sees what happened
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["call_id"],
                    "content": tr["result"],
                })

            # If this was purely a read (no text yielded), continue silently
            # Otherwise the LLM already spoke — let it continue naturally

        # Exhausted rounds — something went wrong
        logger.warning("[NIM] Agentic loop hit max rounds without finishing")

    except Exception as e:
        logger.error(f"NVIDIA NIM error: {type(e).__name__}: {e or '(no message)'}")
        raise


# ── Provider: Gemini ──────────────────────────────────────────────────────────

async def _stream_gemini(user_text: str, system_prompt: str,
                          speak_fn: Optional[Callable] = None,
                          _is_retry: bool = False) -> AsyncIterator[str]:
    """Gemini via new google.genai SDK (google-genai package).

    This used to be a single generate_content() call with no round-trip
    at all: if Gemini's response was a pure function_call part (the
    normal shape for a tool-calling turn — text and a function call
    don't usually come back together), the tool executed, but its
    result was only logged, never sent back to Gemini for an actual
    reply. full_response/buffer stayed empty the whole time, and the
    generator yielded nothing — "stream_response produced zero output"
    for literally any successful tool call. _stream_ollama has had a
    proper multi-round tool→result→response loop all along; this now
    does too, same MAX_ROUNDS shape."""
    import google.genai as genai
    from google.genai import types as gtypes

    client = genai.Client(api_key=config.brain.gemini_api_key)
    model_name = config.brain.gemini_model

    # On a retry (see the 503 handling in stream_response) this exact
    # user message is already in memory from the first attempt —
    # adding it again would put a duplicate in the history.
    if not _is_retry:
        memory.add_user(user_text)
    _tools_ran = False  # set once any tool executes — see the except below
    contents = []
    for msg in memory.get_messages():
        role = "user" if msg["role"] == "user" else "model"
        contents.append(gtypes.Content(role=role, parts=[gtypes.Part(text=msg["content"])]))

    # Tool declarations for new SDK
    #
    # Every parameter used to be forced to type="STRING" here regardless
    # of what its own schema actually declared — meaning every integer
    # (calendar's days/duration_minutes, gmail's max_results,
    # automation's trigger_minutes), every boolean (calendar's all_day),
    # and the one object param (automation's tool_args) all got the
    # wrong type sent to Gemini's function-calling schema. That's a
    # genuine, likely source of "something's off with Gemini" — a schema
    # type mismatch on tool calls is exactly the kind of failure that's
    # inconsistent and hard to pin down from the outside. Fixed: map each
    # parameter's own declared JSON-schema type to Gemini's equivalent
    # instead of hardcoding one for all of them.
    _JSON_TO_GEMINI_TYPE = {
        "string": "STRING", "integer": "INTEGER", "number": "NUMBER",
        "boolean": "BOOLEAN", "object": "OBJECT", "array": "ARRAY",
    }
    tool_fns = []
    for t in TOOL_DEFINITIONS:
        props = {}
        for k, v in t["parameters"].get("properties", {}).items():
            gemini_type = _JSON_TO_GEMINI_TYPE.get(v.get("type", "string"), "STRING")
            schema_kwargs = {"type": gemini_type, "description": v.get("description", "")}
            if gemini_type == "OBJECT":
                # A bare OBJECT type still needs a (possibly empty)
                # properties dict to be valid in Gemini's schema format —
                # free-form here since tool_args' actual shape depends on
                # which tool it's being passed to.
                schema_kwargs["properties"] = {}
            props[k] = gtypes.Schema(**schema_kwargs)
        tool_fns.append(gtypes.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters=gtypes.Schema(
                type="OBJECT",
                properties=props,
                required=t["parameters"].get("required", []),
            )
        ))

    gen_config = gtypes.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[gtypes.Tool(function_declarations=tool_fns)] if tool_fns else None,
        # Left unset before, which means Gemini's own default (around 1.0
        # for flash models) — same tool-calling-reliability reasoning as
        # the Ollama/NIM temperature drops above; this was the one path
        # that hadn't gotten it.
        temperature=0.2,
    )

    full_response = ""
    MAX_ROUNDS = 4  # same shape/budget as _stream_ollama's tool→response loop
    loop = asyncio.get_running_loop()

    try:
        for round_n in range(MAX_ROUNDS):
            buffer = ""
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=gen_config,
                )
            )

            try:
                from ui.ws_server import get_stop_flag
                if get_stop_flag():
                    return
            except Exception:
                pass

            parts = response.candidates[0].content.parts
            function_call_part = None
            round_text = ""

            for part in parts:
                if hasattr(part, "function_call") and part.function_call and part.function_call.name:
                    function_call_part = part
                elif hasattr(part, "text") and part.text:
                    round_text += part.text

            if function_call_part:
                fc = function_call_part.function_call
                _tools_ran = True
                result = await _dispatch_tool(fc.name, dict(fc.args), speak_fn)
                logger.info(f"[Gemini Tool] {fc.name}: {str(result)[:80]}")

                # Echo the model's own function-call turn back verbatim
                # (the whole returned content, not just the one part —
                # preserves anything else that came back alongside it),
                # then the function's result as a role="user" turn.
                # That's what Google's own function-calling docs use —
                # NOT role="tool": the python-genai README shows 'tool',
                # but the live API rejects it outright ("Role 'tool' is
                # not supported", 400 INVALID_ARGUMENT — confirmed in a
                # real run), which killed every Gemini tool call until
                # this was corrected.
                contents.append(response.candidates[0].content)
                _fr_kwargs = {"name": fc.name, "response": {"result": str(result)}}
                if getattr(fc, "id", None):
                    _fr_kwargs["id"] = fc.id  # newer models return call IDs; docs pass them back
                try:
                    _fr_part = gtypes.Part.from_function_response(**_fr_kwargs)
                except TypeError:
                    # Older google-genai without the id kwarg — same call without it
                    _fr_kwargs.pop("id", None)
                    _fr_part = gtypes.Part.from_function_response(**_fr_kwargs)
                contents.append(gtypes.Content(role="user", parts=[_fr_part]))
                # Any text that came back alongside the function call is
                # typically just filler ("Let me check...") — same
                # discard-the-lead-in convention _stream_ollama already
                # uses, for the same reason: it's not grounded in the
                # result yet, since the result didn't exist when this
                # text was generated.
                continue  # next round: let Gemini react to the real result

            # No function call this round — round_text is the real,
            # final reply. Stream it the same way _stream_ollama does.
            if round_text:
                full_response += round_text
                buffer += round_text
                from voice.tts import split_into_sentences
                sentences = split_into_sentences(buffer)
                if len(sentences) > 1:
                    for s in sentences[:-1]:
                        if s.strip():
                            yield s.strip()
                    buffer = sentences[-1]

            if buffer.strip():
                yield buffer.strip()
            break
        else:
            # MAX_ROUNDS exhausted without ever getting a text-only
            # response — surface something rather than silently
            # producing zero output again.
            yield "That took more back-and-forth than expected, boss — can you try rephrasing?"

        if full_response.strip():
            memory.add_assistant(full_response)
        asyncio.create_task(_background_memory(user_text, memory.get_all()))

    except Exception as e:
        logger.error(f"Gemini error: {e}")
        # If a tool already ran before this failed, a blind retry of the
        # whole function would run it a second time (a duplicate calendar
        # event, say) — tag the exception so the retry logic upstream
        # knows not to.
        try:
            e._friday_tools_ran = _tools_ran
        except AttributeError:
            pass  # exotic exception type without a __dict__ — retry logic then just treats it as "unknown"
        raise


# ── Background memory ─────────────────────────────────────────────────────────

async def _background_memory(user_text: str, messages: list[dict]):
    try:
        from memory.memory_store import extract_and_save_facts
        await asyncio.wait_for(extract_and_save_facts(user_text), timeout=10)
    except Exception:
        pass

    try:
        # FIX: len(messages) % 20 == 0 is True for empty lists (0 % 20 == 0).
        # Added len(messages) >= 20 to require at least a full batch before summarising.
        if len(messages) >= 20 and len(messages) % 20 == 0:
            from memory.memory_store import summarize_and_save
            await asyncio.wait_for(summarize_and_save(messages), timeout=15)
    except Exception:
        pass

# ── Quick single-shot LLM call (for code_helper, handlers, etc.) ──────────────

async def _quick_llm(prompt: str, system: str = "You are a helpful coding assistant. Output only what is asked — no preamble.") -> str:
    """
    Non-streaming single-turn LLM call. Routes through the active provider.
    Used by code_helper, planner, and other internal callers.
    """
    provider = get_active_provider()

    # NIM / Gemini all use OpenAI-compatible SDK (NIM does; Gemini's branch is separate below)
    if provider == "nvidia":
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key=config.brain.nvidia_nim_api_key,
            )
            model = config.brain.nvidia_nim_model

            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.2,
                max_tokens=4096,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"[_quick_llm] {provider} failed: {e} — falling back to Ollama")

    # Gemini
    if provider == "gemini" and config.brain.gemini_api_key:
        try:
            import google.genai as genai
            from google.genai import types as gtypes
            client = genai.Client(api_key=config.brain.gemini_api_key)
            resp = client.models.generate_content(
                model=config.brain.gemini_model,
                contents=prompt,
                # Matches the NIM branch above and _stream_gemini's main
                # loop — _quick_llm backs the planner and code_helper,
                # both of which want faithful, reliable output over
                # creative variety.
                config=gtypes.GenerateContentConfig(system_instruction=system, temperature=0.2),
            )
            return resp.text or ""
        except Exception as e:
            logger.warning(f"[_quick_llm] Gemini failed: {e} — falling back to Ollama")

    # Ollama fallback
    try:
        import ollama as ol
        resp = ol.chat(
            model=config.brain.ollama_model,
            messages=[
                {"role": "system",  "content": system},
                {"role": "user",    "content": prompt},
            ],
            # Same reasoning as the other three branches — this was
            # unset, meaning Ollama's own default (~0.8), for a function
            # whose whole job is reliable, literal output.
            options={"temperature": 0.2},
        )
        # FIX: Handle both dict (older ollama SDK) and object (newer SDK) responses
        return (resp["message"]["content"] if isinstance(resp, dict) else resp.message.content) or ""
    except Exception as e:
        raise RuntimeError(f"All LLM providers failed in _quick_llm: {e}") from e



# ── Main entry point ──────────────────────────────────────────────────────────

async def stream_response(user_text: str,
                           speak_fn: Optional[Callable] = None) -> AsyncIterator[str]:
    """
    Routes to the active provider: nvidia | gemini → Ollama fallback.
    """
    low = user_text.lower()
    if any(p in low for p in ["use local", "use ollama", "go local", "switch to ollama"]):
        # lock_to_ollama() (via set_active_provider) speaks the line
        # directly and is the only thing that does — what's yielded here
        # is \x00-prefixed (transcript-only, never spoken a second time,
        # same idiom _cloud_fallback() uses below) purely so start.py's
        # loop has SOMETHING to put in the transcript and doesn't fall
        # into its "produced zero output" fallback, which would otherwise
        # speak an unrelated "didn't get anything back there" right after
        # the switch line.
        before = get_active_provider()
        lock_to_ollama("user requested")
        after = get_active_provider()
        yield f"\x00{_last_provider_line}" if after != before else f"\x00Already on {after}, boss."
        return
    if any(p in low for p in ["use cloud", "use nim", "switch to nim", "unlock provider"]):
        before = get_active_provider()
        unlock_provider()
        after = get_active_provider()
        yield f"\x00{_last_provider_line}" if after != before else f"\x00Already on {after}, boss."
        return

    system_prompt = build_system_prompt(query=user_text)
    provider = get_active_provider()

    from sentinel import start_turn
    start_turn()

    try:
        from memory import usage_tracker as ut
        ut.record_call(f"llm_{provider}")
    except Exception:
        pass

    async def _ollama_fallback(original_err: str):
        # \x00 marks this as transcript-only — _handle_user_text (start.py)
        # shows it but doesn't speak it. This fires on every primary-provider
        # failure, so speaking it every time is just noise; the actual
        # answer (or the final failure message below, which IS spoken)
        # is what matters.
        yield f"\x00Provider down ({original_err[:40]}), switching to local, boss."
        try:
            async for s in _stream_ollama(user_text, system_prompt, speak_fn):
                yield s
        except Exception as e2:
            logger.error(f"[LLM] Ollama fallback also failed: {e2}")
            yield "Both cloud and Ollama are down, boss. Check your connections."

    async def _cloud_fallback(original_err: str):
        # Mirror of _ollama_fallback, other direction: Ollama is the
        # active provider and just failed (not running, a :cloud model
        # needing an ollama.com subscription, a network blip — Ollama's
        # own error handling above no longer swallows this). Try
        # whichever cloud provider is actually configured, in order,
        # before giving up.
        #
        # Same idea as voice/tts.py's ElevenLabs->edge auto-switch: after
        # this happens _LLM_AUTO_SWITCH_THRESHOLD times in a row, the
        # session default itself switches to whichever provider just
        # worked, so future turns don't pay the latency of trying a down
        # Ollama first every single time — and the UI gets a toast so
        # it's not a silent change. Below that threshold, next turn
        # tries Ollama again first, since one failure isn't yet a
        # pattern (a single dropped request shouldn't permanently move
        # you off your configured local default).
        global _consecutive_ollama_failures
        _consecutive_ollama_failures += 1

        candidates = []
        if config.brain.nvidia_nim_api_key:
            candidates.append(("nvidia", _stream_nvidia_nim(user_text, system_prompt, speak_fn)))
        if config.brain.gemini_api_key:
            candidates.append(("gemini", _stream_gemini(user_text, system_prompt, speak_fn)))

        if not candidates:
            yield (f"\x00Ollama down ({original_err[:40]}) and no cloud provider is "
                   f"configured, boss.")
            yield "Ollama's not responding, boss. Make sure it's running, or add a cloud API key in .env."
            return

        for name, stream in candidates:
            yield f"\x00Ollama down ({original_err[:40]}), trying {name}, boss."
            try:
                async for s in stream:
                    yield s
                if _consecutive_ollama_failures >= _LLM_AUTO_SWITCH_THRESHOLD:
                    # set_active_provider() is the one function every swap
                    # goes through — it logs, speaks the curated line, and
                    # broadcasts the toast itself, so nothing extra happens
                    # here beyond noting WHY (the failure count) in this
                    # call site's own log line.
                    set_active_provider(
                        name, reason=f"Ollama failed {_consecutive_ollama_failures}x in a row"
                    )
                    _consecutive_ollama_failures = 0
                return
            except Exception as e2:
                logger.error(f"[LLM] Ollama->{name} fallback also failed: {e2}")
                continue

        yield "Ollama and every configured cloud provider failed, boss. Check your connections."

    from brain import model_router as _router

    try:
        if provider == "ollama":
            try:
                async for s in _stream_ollama(user_text, system_prompt, speak_fn):
                    yield s
            except Exception as e:
                logger.error(f"[LLM] Ollama failed: {e} — trying a cloud provider")
                async for s in _cloud_fallback(type(e).__name__):
                    yield s
            else:
                _consecutive_ollama_failures = 0

        elif provider == "nvidia":
            try:
                async for s in _stream_nvidia_nim(user_text, system_prompt, speak_fn):
                    yield s
            except Exception as e:
                logger.error(f"[LLM] NIM failed: {type(e).__name__}: {e or '(no message)'} — falling back to Ollama")
                async for s in _ollama_fallback(type(e).__name__):
                    yield s

        elif provider == "gemini":
            try:
                async for s in _stream_gemini(user_text, system_prompt, speak_fn):
                    yield s
            except Exception as e:
                # Google's own 503 message says demand spikes are
                # "usually temporary" — a brief single retry catches
                # most of those without forcing a full provider fallback
                # over what's often just a several-second blip, rather
                # than treating a transient capacity spike the same as a
                # genuine outage.
                if ("503" in str(e) or "UNAVAILABLE" in str(e)) and not getattr(e, "_friday_tools_ran", False):
                    logger.warning(f"[LLM] Gemini 503 (transient) — retrying once shortly: {e}")
                    await asyncio.sleep(3)
                    try:
                        async for s in _stream_gemini(user_text, system_prompt, speak_fn, _is_retry=True):
                            yield s
                        return
                    except Exception as e2:
                        e = e2
                logger.error(f"[LLM] Gemini failed: {e} — falling back to Ollama")
                async for s in _ollama_fallback(type(e).__name__):
                    yield s

        elif provider in _router.ROUTER_MODES:
            # FAST/STRONG/LOCAL/AUTO — see brain/model_router.py. This
            # dispatch is the ONLY thing that changed in this function for
            # the router modes; nvidia/gemini/ollama above are completely
            # untouched, and picking one of them directly still bypasses
            # the router exactly as it always has.
            async for s in _router.stream(provider, user_text, system_prompt, speak_fn):
                yield s

        else:
            # Unknown provider — default to NIM, fall back to Ollama
            try:
                async for s in _stream_nvidia_nim(user_text, system_prompt, speak_fn):
                    yield s
            except Exception:
                async for s in _stream_ollama(user_text, system_prompt, speak_fn):
                    yield s

    except Exception as e:
        logger.error(f"stream_response error: {e}")
        yield "Something went wrong, boss. Try again."


def clear_memory():
    memory.clear()
    logger.info("Conversation memory cleared.")


async def generate_session_summary() -> Optional[str]:
    """1-2 sentence recap of this session's conversation, for the next
    boot's morning briefing (see start.py). Returns None if there was
    nothing worth summarizing or generation fails — non-critical feature,
    never worth crashing shutdown over."""
    turns = memory.get_all()
    if len(turns) < 2:
        return None

    transcript = "\n".join(
        f"{t['role']}: {t['content']}" for t in turns[-20:]
    )
    try:
        summary = await _quick_llm(
            f"Conversation:\n{transcript}\n\n"
            "Summarize in ONE short sentence what the user was working on "
            "or asked about, for a future greeting like "
            "'Yesterday you were working on X.' Output only that sentence, "
            "no preamble, no quotes.",
            system="You write extremely short, factual session recaps. One sentence only.",
        )
        summary = (summary or "").strip().strip('"')
        return summary or None
    except Exception as e:
        logger.debug(f"generate_session_summary failed: {e}")
        return None