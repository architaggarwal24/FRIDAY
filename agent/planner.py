"""
F.R.I.D.A.Y. — agent/planner.py
Multi-step task planner with step-result chaining: the planner prompt
teaches {{step_N_result}} placeholders so later steps can reference
earlier ones. synthesize_results() builds a content-aware summary from
actual step outputs. should_use_agent_task() triggers agent mode at 2+
steps. Recent action errors are injected into planner context for
known-failure awareness, and the planner routes through the same
multi-provider chain as the main LLM.
"""

import asyncio
import json
import logging
import re
from typing import Callable, Optional

from config import config

logger = logging.getLogger(__name__)

# Every provider on the plan_task() waterfall needs a hard ceiling, or one
# slow/wedged rung blocks the whole turn with nothing upstream to un-stick
# it. NIM is a cloud API and should never legitimately take this long.
# Ollama's default client has NO built-in timeout at all — a cold model
# load, GPU contention, or a wedged server hangs `ol.chat()` indefinitely,
# and since it's dispatched via run_in_executor() there's nothing that
# times it out on its own either. 45s is generous enough to cover a cold
# local model load on modest hardware while still giving up eventually
# instead of leaving the whole assistant "stuck" for the rest of the turn.
_NIM_PLAN_TIMEOUT_S = 20
_OLLAMA_PLAN_TIMEOUT_S = 45

PLANNER_SYSTEM = """You are a task planner for F.R.I.D.A.Y., an AI assistant on Windows 11.
Given a goal, break it into concrete executable steps using the available tools.

STEP CHAINING:
Steps CAN reference prior results. Use {{step_N_result}} in any arg value to inject
the output of step N. Example:
  Step 1: web_search — query: "cheapest flight Delhi to Mumbai"
  Step 2: file_controller — action: write, path: desktop, name: flights.txt, content: "{{step_1_result}}"

Available tools:
- open_app(app_name)
- web_search(query)
- computer_settings(description, value?)
- browser_control(action, url?, query?, text?)
- file_controller(action, path?, name?, content?, destination?)
- spotify_control(action, query?, value?)
- weather_report(city)
- google_status()  # read-only: is a Google account connected (Calendar + Gmail foundation)
- calendar(action, date, days, summary, time, duration_minutes, all_day, location, description, query)  # real Google Calendar — list/create/delete/next
- gmail(action, query, max_results, to, subject, body)  # real Gmail — list/search/read/open/draft/send (send is HIGH risk, confirmed)
- automation(action, trigger_type, trigger_time, trigger_minutes, trigger_query, action_type, speak_message, tool_name, tool_args, description, query)  # background recurring rules — create/list/delete/enable/disable
- morning_briefing(city)  # weather + calendar + reminders + inbox + watched topics, all at once
- youtube_video(action, query?)
- screenshot()
- send_message(receiver, message_text, platform)
- reminder(date, time, message)
- code_helper(action, description, language?, output_path?)
- desktop_control(action, path?, url?, mode?)  # action: wallpaper | wallpaper_url | current_wallpaper | organize | clean | list | stats
- computer_control(action, x?, y?, text?, keys?, window?)
- dev_agent(goal, language?, output_dir?)
- file_processor(file_path, task)
- game_updater(action, game_name?, platform?, schedule_time?)
- flight_finder(origin, destination, date, return_date?, cabin?)
- power_control(action)
- wait(seconds)  # pause between steps — REQUIRED for "every N seconds/minutes",
  "wait N before doing X", or any spaced-out repeated action. There is no other
  way to represent a delay; a request with a timing requirement and no wait
  step between the repeated actions is not a valid plan for it.

Return ONLY a JSON array of steps with no markdown fences:
[
  {"step": 1, "tool": "tool_name", "args": {"key": "value"}, "description": "what this does", "critical": true},
  ...
]

Rules:
- Use fewest steps possible. Each step uses exactly one tool.
- Steps run in order. Max 8 steps. Return valid JSON ONLY.
- critical: true for steps where failure should trigger replan (default true).
- agent_task threshold: 2+ steps spanning different tool categories.
- A goal that needs more repeats than the 8-step budget allows (each wait
  step also counts toward it) still gets a real plan: do as many
  search/wait pairs as fit and say so in the last step's description,
  rather than returning an empty plan because the exact count doesn't fit."""


SYNTHESIZER_SYSTEM = """You are the results synthesizer for F.R.I.D.A.Y., a sharp AI assistant.
Given a completed task and its step outputs, write 1-2 sentences summarizing what was accomplished.
- Address the user as "boss"
- Be specific — mention actual results (prices, filenames, content found)
- Do NOT list steps or mention tool names
- Match FRIDAY's tone: confident, direct, no filler words"""


async def plan_task(goal: str, known_failures: Optional[dict] = None) -> Optional[list[dict]]:
    logger.info(f"[Planner] Planning: {goal}")

    system = PLANNER_SYSTEM
    if known_failures:
        failure_lines = "\n".join(f"  - {k}: {v}" for k, v in list(known_failures.items())[:5])
        system += f"\n\nKNOWN FAILURES — do not retry these approaches:\n{failure_lines}"

    prompt = f"Goal: {goal}"

    # A real waterfall across every configured provider, not "whichever is
    # configured first" — the old if/elif/else meant that once
    # GEMINI_API_KEY was set, NIM/Ollama were never tried even if Gemini
    # failed outright (a 429, a timeout, anything). Ollama has no key
    # requirement, so it's always the last resort.
    attempts: list[tuple[str, Callable]] = []
    if config.brain.gemini_api_key:
        attempts.append(("gemini", _plan_gemini))
    if config.brain.nvidia_nim_api_key:
        attempts.append(("nim", _plan_nim))
    attempts.append(("ollama", _plan_ollama))

    last_error: Optional[Exception] = None
    for name, fn in attempts:
        try:
            plan = await fn(prompt, system)
            if plan:
                return plan
            logger.warning(f"[Planner] {name} returned no usable plan — trying next provider")
        except Exception as e:
            last_error = e
            logger.error(f"[Planner] {name} failed: {e}")

    if last_error:
        logger.error(f"[Planner] All providers exhausted; last error: {last_error}")
    return None


async def _plan_gemini(prompt: str, system: str) -> Optional[list[dict]]:
    # Walks a two-model ladder (gemini_model / gemini_fast_model) with a
    # hard timeout and a per-model quota cooldown, instead of one
    # hardcoded model with an unbounded wait. See core/gemini_ladder.py.
    from core.gemini_ladder import call as gemini_call, SMART
    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(
        None,
        lambda: gemini_call(prompt, tier=SMART, system_instruction=system, timeout_ms=15_000),
    )
    if resp is None:
        raise RuntimeError("Gemini ladder exhausted (quota cooldown or every rung failed)")
    return _parse_plan(resp.text)


async def _plan_nim(prompt: str, system: str) -> Optional[list[dict]]:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=config.brain.nvidia_nim_api_key,
        base_url="https://integrate.api.nvidia.com/v1",
        timeout=_NIM_PLAN_TIMEOUT_S,
    )
    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=config.brain.nvidia_nim_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.2, max_tokens=800,
        ),
        timeout=_NIM_PLAN_TIMEOUT_S,
    )
    return _parse_plan(resp.choices[0].message.content)


async def _plan_ollama(prompt: str, system: str) -> Optional[list[dict]]:
    import ollama as ol
    loop = asyncio.get_running_loop()
    # wait_for bounds how long WE wait — it can't reach into the executor
    # thread and actually kill a hung ol.chat() call, but it stops this
    # coroutine (and the turn) from blocking on it forever. The orphaned
    # background call just finishes (or errors) on its own later and gets
    # discarded.
    resp = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: ol.chat(
                model=config.brain.ollama_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                options={"temperature": 0.2, "num_predict": 600},
            )
        ),
        timeout=_OLLAMA_PLAN_TIMEOUT_S,
    )
    # FIX: handle both dict (old ollama SDK) and object (new SDK) responses
    content = (resp["message"]["content"] if isinstance(resp, dict)
               else resp.message.content)
    return _parse_plan(content)


def _parse_plan(text: str) -> Optional[list[dict]]:
    text = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        logger.error(f"[Planner] No JSON array in: {text[:200]}")
        return None
    try:
        plan = json.loads(match.group(0))
        if not isinstance(plan, list):
            return None
        for step in plan:
            if not isinstance(step, dict) or "tool" not in step:
                return None
        logger.info(f"[Planner] {len(plan)} steps")
        return plan
    except json.JSONDecodeError as e:
        logger.error(f"[Planner] JSON error: {e}")
        return None


# ── synthesize_results  (ported + improved from Mark XXXIX) ──────────────────

async def synthesize_results(goal: str, step_results: dict, completed_steps: list) -> str:
    """
    Content-aware summary: uses actual step outputs, not just "X/Y steps done".
    Falls back to a simple count string if the LLM call fails.
    """
    fallback = (
        f"Done, boss. Completed {len(completed_steps)} steps for: {goal[:60]}"
        + ("..." if len(goal) > 60 else ".")
    )

    steps_block = []
    for step in completed_steps:
        n      = step.get("step", "?")
        tool   = step.get("tool", "")
        desc   = step.get("description", "")
        result = step_results.get(n, "")
        preview = str(result)[:500] if result else "(no output)"
        steps_block.append(f"Step {n} [{tool}] — {desc}\nOutput: {preview}")

    prompt = (
        f'Goal: "{goal}"\n\n'
        f"Completed steps:\n" + "\n\n".join(steps_block) + "\n\n"
        "Summarize in 1-2 sentences. Be specific about what was found or done. "
        "Address user as 'boss'."
    )

    # Same waterfall as plan_task: try every configured provider, not just
    # the first one, and let the Gemini ladder (core/gemini_ladder.py)
    # absorb a single model's quota outage instead of that being the whole
    # story. `fallback` still catches the case where every provider is
    # either unconfigured or actually down.
    if config.brain.gemini_api_key:
        try:
            from core.gemini_ladder import call as gemini_call, FAST
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: gemini_call(prompt, tier=FAST, system_instruction=SYNTHESIZER_SYSTEM, timeout_ms=10_000),
            )
            if resp is not None and (resp.text or "").strip():
                return resp.text.strip()
            logger.warning("[Planner] Gemini synthesis returned nothing — trying next provider")
        except Exception as e:
            logger.warning(f"[Planner] Gemini synthesis failed: {e} — trying next provider")

    if config.brain.nvidia_nim_api_key:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=config.brain.nvidia_nim_api_key,
                base_url="https://integrate.api.nvidia.com/v1",
            )
            resp = await client.chat.completions.create(
                model=config.brain.nvidia_nim_model,
                messages=[
                    {"role": "system", "content": SYNTHESIZER_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.3, max_tokens=120,
            )
            return (resp.choices[0].message.content or "").strip() or fallback
        except Exception as e:
            logger.warning(f"[Planner] NIM synthesis failed: {e}")

    return fallback


# ── should_use_agent_task  (ported from Mark XXXIX, threshold 2+) ─────────────

def should_use_agent_task(goal: str) -> bool:
    """
    Returns True if the goal likely requires 2+ steps spanning different tool categories.
    """
    SIGNALS = [
        " and save", " and write", " and open", " and send",
        " then ", " after that", "research", "find and", "look up and",
        "search and", "summarize and", "translate and", "compare and",
        "download and", "install and", "check and",
    ]
    return any(sig in goal.lower() for sig in SIGNALS)
