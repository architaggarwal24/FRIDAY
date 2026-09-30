"""
F.R.I.D.A.Y. — brain/router.py
Ultra-fast intent classification.

Priority order:
  1. Local keyword rules (instant, no network)
  2. Groq API (fast, ~100ms) — only if GROQ_API_KEY is set in .env
  3. Falls back to "chat" — LLM handles it naturally

This means FRIDAY works perfectly even without a Groq key.
"""

import asyncio
import json
import logging
import re
import time
from typing import Literal, Optional

import pydantic
from pydantic import BaseModel, Field

from config import config

logger = logging.getLogger(__name__)

IntentType = Literal[
    # FIX: Literal must include ALL values that VALID_INTENTS and classify_intent can return.
    # Original was missing: reminder, memory_save, memory_recall, news, open_app, file, window, search
    "chat", "coding", "timer", "reminder",
    "memory", "memory_save", "memory_recall",
    "stop", "clear",
    "system", "volume", "power", "screenshot", "clipboard",
    "open_app", "file", "window",
    "search", "weather", "news",
    "spotify", "pronoun_stop", "focus_retarget", "posture_relief",
]


class IntentClassification(BaseModel):
    """The ONLY thing a Groq classification call is allowed to produce.
    This is a plain chat-completion call — no function/tool-calling
    parameter is passed to Groq at all, so there's no code path by which
    the model could invoke anything directly regardless of what its
    output says. All it can do is produce text, which must validate
    against this schema before anything downstream trusts it. Any
    mismatch — wrong type, an intent string outside the known set,
    confidence out of range — fails validation and falls back to "chat",
    never a best-effort guess at what was meant.
    """
    intent: IntentType
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


# Below this, treat a technically-valid classification as too unsure to
# act on and fall back to "chat" — a low-confidence guess isn't much
# better than an invalid one.
_MIN_CONFIDENCE = 0.35

INTENT_SYSTEM_PROMPT = """You are an intent classifier for a voice AI assistant called F.R.I.D.A.Y.
Given a user utterance, respond with ONLY a single JSON object — no prose, no markdown fences, nothing else — matching exactly this shape:

{"intent": "<one of the categories below>", "confidence": <float 0.0-1.0>}

Categories:
chat, coding, timer, reminder, memory, stop, clear, system, volume, power, screenshot, clipboard, open_app, file, window, search, weather, news, spotify

Rules:
- coding: write code, create a project, edit/update/fix/refactor a file, "create a ... in python/js/c++", "read this file and update it", "build X from scratch", "fix the bug in", "add feature to"
- chat: general conversation, questions, anything not listed below
- reminder: "set a reminder for 5pm", "remind me at 9am", "schedule reminder" — specific clock time/date. Also "remove/cancel/delete a reminder or calendar event" ("remove that event", "cancel my 5pm reminder", "delete the meeting from my calendar") — this is deleting a SCHEDULED ITEM, a reminder/calendar action, NOT the "stop" category below even though it shares the word "cancel".
- timer: "set a timer", "remind me in X minutes" — countdown duration
- memory_save: "remember that X", "save this", "note that", "don't forget X" — user giving info to store
- memory_recall: "what do you remember", "what do you know about me", "do you remember", "what is my name" — user asking what FRIDAY knows
- stop: interrupting FRIDAY's CURRENT speech or action — "stop", "be quiet", "shut up", "never mind", "cancel" said alone with nothing else specified. NOT "cancel/remove/delete THE [reminder/event/meeting]" — that's reminder (above), since there's a specific scheduled item being acted on, not a request to interrupt.
- clear: "clear history", "forget everything", "start over", "reset"
- system: "what time", "what date", "cpu usage", "ram", "gpu", "battery", "disk space"
- volume: "set volume", "mute", "unmute", "volume up", "volume down", "louder", "quieter"
- power: "shutdown", "restart", "reboot", "sleep", "hibernate", "lock screen", "turn off"
- screenshot: "take a screenshot", "capture screen", "screenshot"
- clipboard: "clipboard", "copy this", "what's in clipboard"
- open_app: "open", "launch", "start" followed by an app name
- file: "find file", "open file", "open folder", "open downloads"
- window: "minimize", "maximize", "close window", "switch to", "show desktop"
- search: "search google", "look up", "google", "youtube", "go to", "open website"
- weather: "weather", "temperature", "forecast", "is it raining"
- news: "news", "headlines", "what's happening", "latest news"
- spotify: "play", "pause", "skip", "next song", "previous song", "what's playing", "shuffle", "music"

confidence: how sure you are, 0.0-1.0. If genuinely unclear between two categories, use a lower value rather than guessing high — a low-confidence intent falls back to plain chat instead of being acted on.

Respond with ONLY the JSON object, nothing else."""


def _extract_json(text: str) -> Optional[str]:
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        return brace_match.group(0)
    return None


def _parse_classification(raw_text: str) -> "IntentClassification | None":
    """Returns None (not a guess) if the model's output doesn't validate —
    schema mismatch and low confidence are both treated as 'not usable',
    same handling either way at the call site."""
    candidate = _extract_json(raw_text)
    if candidate is None:
        return None
    try:
        parsed = IntentClassification.model_validate_json(candidate)
    except pydantic.ValidationError as e:
        logger.debug(f"[Router] Groq output failed schema validation: {e}")
        return None
    if parsed.confidence < _MIN_CONFIDENCE:
        logger.debug(f"[Router] Groq confidence too low ({parsed.confidence:.2f}) for intent={parsed.intent!r}")
        return None
    return parsed


# ── Groq client (lazy, optional) ─────────────────────────────────────────────

_groq_client = None
_groq_unavailable = False   # stop retrying after first failure


def _get_groq_client():
    global _groq_client, _groq_unavailable
    if _groq_unavailable:
        return None
    if _groq_client is not None:
        return _groq_client

    api_key = config.brain.groq_api_key
    if not api_key:
        logger.info(
            "GROQ_API_KEY not set in .env — intent router using local rules only. "
            "Add GROQ_API_KEY to .env for smarter routing."
        )
        _groq_unavailable = True
        return None

    try:
        from groq import Groq
        _groq_client = Groq(api_key=api_key)
        logger.info(f"Groq intent router ready (model: {config.brain.groq_model})")
        return _groq_client
    except ImportError:
        logger.warning("groq package not installed — run: pip install groq")
        _groq_unavailable = True
        return None
    except Exception as e:
        logger.warning(f"Groq init failed: {e} — using local rules only")
        _groq_unavailable = True
        return None


# ── Local keyword rules (instant, no network) ────────────────────────────────

_LOCAL_RULES = [
    # Checked BEFORE the generic "stop" rule below — "cancel" as a bare
    # substring match against "cancel my reminder"/"cancel that event"
    # would otherwise misfire as stop (an emergency interrupt) instead of
    # reminder (deleting a scheduled item), same gap just confirmed on
    # the Groq path for "remove it now" — this closes it here too, since
    # a local-rule match bypasses Groq (and its now-updated prompt)
    # entirely.
    (["cancel my reminder", "cancel the reminder", "cancel that reminder", "cancel this reminder",
      "remove that reminder", "remove this reminder", "remove my reminder", "delete that reminder",
      "delete this reminder", "delete my reminder", "cancel my event", "cancel that event",
      "cancel this event", "remove that event", "remove this event", "remove my event",
      "delete that event", "delete this event", "delete my event", "cancel my meeting",
      "remove my meeting", "delete my meeting", "remove it from my calendar",
      "delete it from my calendar", "cancel it from my calendar"], "reminder"),
    (["stop", "cancel", "shut up", "be quiet", "nevermind", "never mind"], "stop"),
    (["clear history", "forget everything", "start over", "reset memory"], "clear"),
    (["what time", "what's the time", "current time", "what day", "today's date", "what date"], "system"),
    (["cpu", "ram usage", "gpu usage", "battery", "disk space", "how much ram", "memory usage"], "system"),
    (["mute", "unmute", "volume up", "volume down", "louder", "quieter", "set volume", "turn up", "turn down"], "volume"),
    (["shutdown", "shut down", "restart", "reboot", "sleep", "hibernate", "lock screen", "lock my pc"], "power"),
    (["screenshot", "take a screenshot", "capture screen", "screen capture"], "screenshot"),
    (["clipboard", "copy this", "what's in clipboard", "whats in clipboard"], "clipboard"),
    (["minimize", "maximize", "close window", "switch to", "show desktop", "list windows"], "window"),
    (["weather", "temperature", "forecast", "is it raining", "how hot", "how cold"], "weather"),
    (["headlines", "top news", "latest news", "what's in the news", "news today"], "news"),
    (["skip", "next song", "next track", "previous song", "what's playing", "now playing",
      "pause music", "stop music", "resume music", "shuffle", "put on some music",
      "play some music", "spotify"], "spotify"),
    (["set a reminder", "reminder for", "remind me at", "schedule a reminder", "add a reminder"], "reminder"),
    (["set a timer", "timer for", "remind me in", "alarm for"], "timer"),
    # Focus-session retarget — changes what a RUNNING session is locked
    # onto without ending it (see actions/focus_session.py's retarget()).
    # Deliberately routed here at the local-rule level rather than left
    # to the LLM's own tool-calling: this needs to register the instant
    # it's said, same reasoning as "stop" above. Starting/pausing/
    # resuming/extending/aborting a session are NOT here — those already
    # work fine through normal LLM tool-calling and don't need the speed.
    (["lock on this", "lock onto this", "keep me here", "stay here",
      "this is the tab", "this is the window", "relock here", "re-lock here",
      "lock here instead", "switch the lock", "change what i'm locked on",
      "change what im locked on"], "focus_retarget"),
    # Posture relief valve — "leave me alone for a bit". Local-rule
    # routed for the same reason as the retarget phrases: it needs to
    # take effect the moment it's said, and it's the one thing a person
    # says when they're already irritated at being nudged.
    # Note: "stop nagging"/"quit nagging" are deliberately NOT here.
    # They'd be caught by the "stop" rule above first (it fires on any
    # "stop"), and that rule is the emergency interrupt — reordering it
    # to win a posture phrase would be a bad trade. Those phrases still
    # do something reasonable (they shut her up immediately); they just
    # don't start the 3-minute relief window.
    (["give me a minute", "give me a sec", "leave me alone",
      "need to focus on something important", "focus on something important",
      "lay off", "not now", "quit it", "enough"], "posture_relief"),
    # Save: user is giving FRIDAY something to store
    (["remember that", "remember this", "save this", "note that",
      "don't forget", "keep in mind", "make a note"], "memory_save"),
    # Recall: user is asking what FRIDAY knows
    (["what do you remember", "what do you know about me", "what did i say",
      "what have i told you", "recall", "do you remember", "tell me about me",
      "what is my name", "how old am i", "where do i live"], "memory_recall"),
    # Generic "remember" without context — needs clarification, route to recall
    (["remember about me", "remember me"], "memory_recall"),
    (["find file", "search for file", "locate file", "open folder", "open downloads",
      "open documents", "open desktop"], "file"),
    (["undo that", "undo it", "undo the last", "undo last file", "undo my last",
      "restore that file", "restore the last file", "bring that file back",
      "bring back that file", "get that file back"], "file"),
    # Coding intent — genuinely code-specific phrases, safe to match
    # immediately with no further context needed.
    (["code a", "implement a", "program a",
      "update the code", "edit the code",
      "fix the bug", "fix this bug", "fix the code", "debug this",
      "refactor", "add feature", "add a feature",
      "digital assistant", "voice assistant", "ai assistant",
      "snake game", "todo app", "calculator", "web scraper", "chatbot",
      "in python", "in javascript", "in c++", "in typescript",
      ], "coding"),
]

# Coding intent, part 2 — phrases like "write a ___"/"create a ___"/
# "make a ___" are far too generic to match alone: "make a playlist",
# "create an event", "build a case for buying X" aren't coding requests,
# but would trigger this rule (and _route()'s handle_code bypass, which
# skips normal conversation/tool use entirely) just as readily as "make a
# snake game" would. These only count as coding when the message ALSO
# names an actual coding artifact — otherwise they fall through to the
# smarter Groq classifier (or default chat) like any ordinary request.
_CODING_GENERIC_VERBS = [
    "write a", "write me a", "create a", "create an", "build a", "build an",
    "make a", "make me a", "from scratch", "build from scratch", "create from scratch",
    "update this file", "edit this file",
    "read this file and", "look at this file and", "open this file and",
]
_CODING_NOUNS = [
    "script", "program", "app", "application", "website", "web app", "webpage",
    "function", "class", "api", "endpoint", "database", "algorithm", "module",
    "bot", "game", "code", "codebase", "repo", "repository",
    "python", "javascript", "typescript", "c++", "rust", "golang", "java", "html", "css", "sql",
]

VALID_INTENTS = {
    "chat", "coding", "timer", "reminder", "memory", "memory_save", "memory_recall", "stop", "clear",
    "system", "volume", "power", "screenshot", "clipboard",
    "open_app", "file", "window", "search", "weather", "news", "spotify",
    "pronoun_stop", "focus_retarget", "posture_relief",
}

# Bare-pronoun follow-up ("kill it", "stop that", "pause it", "close this")
# resolving against whatever brain.llm's last-mentioned-thing tracker holds
# (last app opened / last topic added-or-removed / last song played).
# Anchored to the WHOLE message (plus a little politeness padding) rather
# than a loose substring check — "close the door" or "what's the closest
# restaurant" must never match this, only something that's essentially
# just this command and nothing else.
_PRONOUN_STOP_RE = re.compile(
    r"^(?:can you\s+|could you\s+|please\s+|go ahead and\s+|just\s+)*"
    r"(?:stop|kill|close|pause)\s+(?:it|that|this)"
    r"(?:\s+please)?[\.\!\?]*$"
)


def _match_pronoun_stop(text_lower: str) -> bool:
    return bool(_PRONOUN_STOP_RE.match(text_lower.strip()))


# ── Main classifier ───────────────────────────────────────────────────────────

def classify_intent(user_text: str) -> IntentType:
    text_lower = user_text.lower().strip()

    # 0 — Bare-pronoun stop/kill/close/pause ("kill it", "stop that") —
    # only claimed here if there's actually something to resolve it
    # against (brain.llm's last-mentioned-thing tracker). Otherwise falls
    # through to the general local rules right below unchanged — "stop"
    # still hits the existing stop/cancel rule exactly as it did before
    # this feature existed, so plain "stop" with nothing tracked behaves
    # no differently than before.
    if _match_pronoun_stop(text_lower):
        from brain.llm import get_last_mentioned
        if get_last_mentioned():
            logger.debug("Intent (local): pronoun_stop")
            return "pronoun_stop"

    # 1 — Local keyword rules (instant)
    for keywords, intent in _LOCAL_RULES:
        if any(kw in text_lower for kw in keywords):
            logger.debug(f"Intent (local): {intent}")
            return intent

    # 1b — Coding intent, generic-verb phrases: only count as coding when
    # paired with an actual coding-artifact noun (see _CODING_GENERIC_VERBS
    # comment above) — otherwise "make a playlist"/"create an event" would
    # incorrectly bypass normal conversation entirely.
    if (any(kw in text_lower for kw in _CODING_GENERIC_VERBS)
            and any(noun in text_lower for noun in _CODING_NOUNS)):
        logger.debug("Intent (local): coding (generic verb + coding noun)")
        return "coding"

    # 2 — Open app detection
    is_open = any(text_lower.startswith(w) for w in ["open ", "launch ", "start ", "run "])
    is_url  = any(w in text_lower for w in ["visit", "go to", "navigate", "website", ".com", ".org", ".io", "http"])
    is_file = any(w in text_lower for w in ["folder", "file", "directory", "downloads", "documents", "desktop"])
    if is_open and not is_url and not is_file:
        logger.debug("Intent (local): open_app")
        return "open_app"

    # 3 — URL / search
    if any(text_lower.startswith(w) for w in ["google ", "search ", "youtube ", "look up", "go to ", "visit ", "open http"]):
        logger.debug("Intent (local): search")
        return "search"

    # 4 — Play music
    if text_lower.startswith("play ") and not any(w in text_lower for w in ["game", "video", "movie", "on youtube"]):
        logger.debug("Intent (local): spotify")
        return "spotify"

    # 5 — Groq (optional, fast ~100ms)
    client = _get_groq_client()
    if client is None:
        logger.debug("Intent (fallback): chat")
        return "chat"

    t0 = time.time()
    try:
        from memory import usage_tracker as ut
        ut.record_call("groq_router")
        raw = client.chat.completions.with_raw_response.create(
            model=config.brain.groq_model,
            messages=[
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {"role": "user",   "content": user_text},
            ],
            max_tokens=60,
            temperature=0,
        )
        try:
            from brain.llm import _record_groq_rate_limit_headers
            _record_groq_rate_limit_headers(raw.headers, "groq_router")
        except Exception:
            pass
        response = raw.parse()
        raw_text = (response.choices[0].message.content or "").strip()
        elapsed_ms = (time.time() - t0) * 1000

        parsed = _parse_classification(raw_text)
        if parsed is None:
            # Invalid JSON, schema mismatch, or too-low confidence — all
            # treated the same way: not usable, fall back to chat rather
            # than force a guess. Distinct from the raw response so a bad
            # pattern in Groq's output is diagnosable from logs alone.
            logger.debug(f"[Router] Groq ({elapsed_ms:.0f}ms) unusable output: {raw_text!r} — defaulting to chat")
            return "chat"

        logger.debug(f"[Router] Groq ({elapsed_ms:.0f}ms): intent={parsed.intent!r} confidence={parsed.confidence:.2f}")
        return parsed.intent

    except Exception as e:
        logger.warning(f"Groq intent failed: {e} — defaulting to chat")
        return "chat"


async def classify_intent_async(user_text: str) -> IntentType:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, classify_intent, user_text)