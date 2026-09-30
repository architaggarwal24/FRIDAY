"""
brain/model_router.py — F.R.I.D.A.Y. Model Router

FRIDAY's tool-calling loop, memory, and streaming pipeline all live in
brain/llm.py and are untouched by this file — this module is purely a
DISPATCH layer in front of them. stream_response() in brain/llm.py now
has four extra selectable provider values — "auto", "fast", "strong",
"local" — and when one of those is active, it calls stream() below
instead of going straight to one provider. Picking a specific provider
("nvidia", "gemini", "ollama") still works exactly as it always did;
this is purely additive.

    FRIDAY (brain/llm.py stream_response)
              │
              ▼
        model_router.stream(mode, ...)
              │
      ┌───────┼───────┐
      ▼       ▼       ▼
    FAST   STRONG   LOCAL
      │       │       │
      ▼       ▼       ▼
  LiteLLM   existing  existing
  (new)     NIM/Gemini Ollama
            functions  function
            (brain.llm, (brain.llm,
             unchanged)  unchanged)

TIERS, NOT PROVIDERS
    FAST/STRONG/LOCAL are tiers, each backed by 1-2 candidate models tried
    in order — a tier "fails" only once every candidate in it has failed.
    STRONG and LOCAL are the EXISTING nvidia/gemini/ollama code in
    brain/llm.py, called as-is (_stream_nvidia_nim/_stream_gemini/
    _stream_ollama) — nothing about how those actually talk to their
    providers changed. FAST is the only genuinely new code path, added
    because there was nothing to reuse for it — see the naming note below
    before wiring in real credentials.

ABOUT THE FAST-TIER MODEL NAMES
    The brief named "Qwen3.8 Flash Free" via "Token Harbor" and
    "DeepSeek-V4.1-Flash" via "TokenJuice". Neither Token Harbor nor
    TokenJuice turned up as an actual LLM API service in a web search —
    "tokenjuice" is an unrelated CLI output-compaction tool, and "harbor"
    results are all a different unrelated containerized-LLM toolkit.
    Rather than fabricate connection details for services that don't
    appear to exist as described, this file makes NO assumption about
    which real service backs FAST — both slots are entirely config-driven
    (FAST_MODEL_1_*/FAST_MODEL_2_* in .env: a model name, an OpenAI-
    compatible base_url, and an api_key). Point them at whatever the
    real intended service turns out to be — Qwen/DeepSeek specifically,
    or anything else with an OpenAI-compatible /chat/completions endpoint
    — and FAST works with zero code changes. Until then FAST tier has
    nothing configured, which is a normal, handled state (see "every tier
    can be absent" below), not an error.

WHY LiteLLM FOR FAST BUT NOT STRONG/LOCAL
    "Use the existing LiteLLM setup where appropriate" — there isn't one;
    grep for "litellm" across this project before this change returns
    nothing. It's a new dependency (see requirements.txt), used ONLY for
    FAST, because FAST is the only tier with no existing, working
    integration to reuse. STRONG (NIM + Gemini) and LOCAL (Ollama)
    already have solid, tested code in brain/llm.py and voice-adjacent
    modules; routing those through LiteLLM too would mean re-implementing
    working integrations for no functional gain, which the brief
    explicitly asked not to do. LiteLLM's OpenAI-compatible response
    shape (same .choices[0].delta.content/.tool_calls shape the NIM
    integration already streams) is what makes _stream_litellm() below
    able to reuse brain.llm's existing token-processing/tool-execution
    helpers almost verbatim instead of duplicating that pipeline again.

FALLBACK CHAINS
    Each of the four selectable modes has its own chain, configured in
    config.py (ROUTER_CHAIN_FAST/STRONG/LOCAL/AUTO in .env, comma-
    separated tier names) — centralized there, not scattered across this
    file. Defaults:
        fast:   fast,strong,local    (try fast, escalate on failure)
        strong: strong,fast,local    (try strong, degrade gracefully)
        local:  local                (deliberately no cloud escape — see
                                       "why LOCAL doesn't fall back" below)
        auto:   fast,strong,local    (full chain — let the router decide)
    A tier with nothing configured (e.g. FAST with both model slots
    blank) is skipped, not an error — the chain just moves to the next
    tier. Every tier can be absent; the chain only actually fails if
    EVERY tier in it is either unconfigured or errors out, in which case
    stream() yields one clear message instead of raising — this is the
    same shape of guarantee brain/llm.py's existing _ollama_fallback/
    _cloud_fallback already give the person: a down provider degrades,
    it doesn't crash the app.

WHY LOCAL DOESN'T FALL BACK BY DEFAULT
    Picking LOCAL is usually a deliberate choice — privacy, offline use,
    no cloud spend — so ROUTER_CHAIN_LOCAL defaults to just "local" with
    nothing after it. If Ollama isn't reachable, LOCAL mode says so
    plainly rather than silently sending the conversation to a cloud
    provider the person specifically didn't pick. Set
    ROUTER_CHAIN_LOCAL=local,strong,fast in .env to opt into escalation
    instead, if that's what's actually wanted.

NOT YET WIRED IN: agent_task planning (agent/planner.py) keeps its own
existing Gemini-ladder/NIM/Ollama waterfall (see core/gemini_ladder.py)
rather than going through this router too — that waterfall already does
its own multi-provider fallback and isn't broken, so leaving it alone
follows the same "don't rewrite working code" principle this whole
module does. Worth revisiting later if planning should also respect
FAST/STRONG/LOCAL mode, but that's a separate, smaller follow-up, not
something this pass needed to touch to deliver what was asked for.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Callable, Optional

from config import config

logger = logging.getLogger(__name__)

# The four values stream_response() in brain/llm.py now recognizes in
# addition to the existing "nvidia"/"gemini"/"ollama". Exported so
# brain/llm.py and the frontend-facing provider list can both import this
# one list rather than the set of router modes living in two places.
ROUTER_MODES = ("auto", "fast", "strong", "local", "smart")


def _parse_chain(raw: str) -> list[str]:
    return [t.strip().lower() for t in (raw or "").split(",") if t.strip()]


def _chain_for_mode(mode: str) -> list[str]:
    chains = {
        "fast":   config.brain.router_chain_fast,
        "strong": config.brain.router_chain_strong,
        "local":  config.brain.router_chain_local,
        "auto":   config.brain.router_chain_auto,
    }
    parsed = _parse_chain(chains.get(mode, ""))
    return parsed or ["fast", "strong", "local"]


# Deliberately broad, not precise — a message that mentions any of these
# is likely to need one of FRIDAY's ~20 tools (calendar, gmail, reminder,
# automation, weather, screen/window/app control, memory, code, files,
# system settings…), and that's exactly the class of turn where the
# local model's tool-selection has repeatedly proven unreliable across
# this whole project (wrong tool picked, no tool call attempted at all,
# false success/failure claims) — the reason "smart" mode exists.
# Over-inclusive is the safe direction to err in: a chit-chat message
# routed to STRONG by mistake just costs a little cloud quota; a genuine
# tool request routed to LOCAL by mistake is the actual failure mode
# this is meant to avoid. Getting this perfectly precise isn't the goal.
_TOOL_FLAVORED_HINTS = (
    "calendar", "schedule", "event", "appointment", "meeting",
    "email", "mail", "inbox", "gmail",
    "remind", "reminder", "timer",
    "weather",
    "automation", "automate",
    "briefing",
    "screen", "window", "app ", "application",
    "volume", "brightness", "wifi", "bluetooth", "setting",
    "search", "look up", "google",
    "remember", "recall", "forget", "memory",
    "code", "script", "function", "bug", "debug",
    "file", "folder", "document",
    "open ", "close ",
)


def _looks_tool_flavored(user_text: str) -> bool:
    low = (user_text or "").lower()
    return any(h in low for h in _TOOL_FLAVORED_HINTS)


# Follow-ups in the middle of a tool conversation ("the second one",
# "yes, do it", "open it") carry no tool keywords of their own, but they
# depend entirely on the tool turn before them — handing them to the
# local model mid-conversation is exactly the handoff that broke "open
# the Concord one". So once a turn routes to STRONG, the next couple of
# minutes of turns stay there too.
_STICKY_SECONDS = 120.0
_last_strong_at = 0.0


def _smart_pick(user_text: str) -> str:
    global _last_strong_at
    import time
    now = time.time()
    if _looks_tool_flavored(user_text):
        _last_strong_at = now
        return "strong"
    if now - _last_strong_at < _STICKY_SECONDS:
        return "strong"
    return "local"


def _fast_candidates() -> list[dict]:
    """Each configured FAST slot as {name, base_url, api_key}. A slot
    needs both a name and a base_url to count as configured — an api_key
    is optional (some OpenAI-compatible endpoints don't require one)."""
    cands = []
    b = config.brain
    if b.fast_model_1_name and b.fast_model_1_base_url:
        cands.append({"name": b.fast_model_1_name, "base_url": b.fast_model_1_base_url, "api_key": b.fast_model_1_api_key})
    if b.fast_model_2_name and b.fast_model_2_base_url:
        cands.append({"name": b.fast_model_2_name, "base_url": b.fast_model_2_base_url, "api_key": b.fast_model_2_api_key})
    return cands


def tier_status() -> dict:
    """What's actually configured right now, per tier — used by the
    Settings panel (see ui/ws_server.py's settings payload) so the
    frontend can show which of FAST/STRONG/LOCAL are actually reachable
    rather than just listing all four modes as if they're equally ready."""
    return {
        "fast":   bool(_fast_candidates()),
        "strong": bool(config.brain.nvidia_nim_api_key or config.brain.gemini_api_key),
        "local":  bool(config.brain.ollama_model),
    }


async def _run_candidates(labeled_gens) -> AsyncIterator[str]:
    """Shared fallback core: try each (label, async_generator) in order.
    Stops trying further candidates the moment ANY token has been yielded
    by the current one — falling back after a partial response would mean
    showing the person two different, unrelated answers stitched
    together, which is worse than just stopping. Raises RuntimeError only
    if every candidate failed before yielding anything at all."""
    last_err: Optional[Exception] = None
    for label, gen in labeled_gens:
        yielded_any = False
        try:
            async for chunk in gen:
                yielded_any = True
                yield chunk
            return
        except Exception as e:
            last_err = e
            logger.warning(f"[Router] '{label}' failed: {e}")
            if yielded_any:
                return  # partial output already shown — stop, don't compound it
            continue    # failed cleanly before producing anything — try the next one
    raise RuntimeError(str(last_err) if last_err else "no candidates configured")


async def _try_tier(tier: str, user_text: str, system_prompt: str,
                     speak_fn: Optional[Callable]) -> AsyncIterator[str]:
    from brain import llm as base  # lazy import: brain.llm lazily imports THIS
                                     # module too when dispatching to a router
                                     # mode, so importing it at module load time
                                     # in either direction would be circular

    if tier == "strong":
        attempts = []
        if config.brain.nvidia_nim_api_key:
            attempts.append(("strong/nvidia", base._stream_nvidia_nim(user_text, system_prompt, speak_fn)))
        if config.brain.gemini_api_key:
            attempts.append(("strong/gemini", base._stream_gemini(user_text, system_prompt, speak_fn)))
        if not attempts:
            raise RuntimeError("STRONG: neither NVIDIA_NIM_API_KEY nor GEMINI_API_KEY is set")
        async for chunk in _run_candidates(attempts):
            yield chunk

    elif tier == "local":
        if not config.brain.ollama_model:
            raise RuntimeError("LOCAL: OLLAMA_MODEL is not set")
        # A single candidate still goes through _run_candidates so an
        # Ollama connection error surfaces as a clean "this tier failed"
        # RuntimeError instead of an unhandled exception — this is the
        # actual mechanism behind "Ollama being offline must not crash
        # FRIDAY": the error is caught right here, same as every other
        # tier, and stream() below turns a fully-exhausted chain into one
        # spoken message instead of letting anything raise past it.
        async for chunk in _run_candidates([("local/ollama", base._stream_ollama(user_text, system_prompt, speak_fn))]):
            yield chunk

    elif tier == "fast":
        candidates = _fast_candidates()
        if not candidates:
            raise RuntimeError("FAST: neither FAST_MODEL_1_* nor FAST_MODEL_2_* is set")
        attempts = [(f"fast/{c['name']}", _stream_litellm(user_text, system_prompt, speak_fn, c)) for c in candidates]
        async for chunk in _run_candidates(attempts):
            yield chunk

    else:
        raise RuntimeError(f"Unknown tier: {tier}")


async def stream(mode: str, user_text: str, system_prompt: str,
                  speak_fn: Optional[Callable] = None) -> AsyncIterator[str]:
    """Entry point brain/llm.py's stream_response() calls for the five
    router modes. "smart" is the odd one out: rather than walking its
    own configured chain, it makes ONE content-based decision — does
    this message look like it'll need a tool? — and then walks STRONG's
    chain or LOCAL's chain accordingly, reusing those two tiers'
    existing fallback behavior wholesale rather than duplicating it. The
    other four modes are unchanged: walks the configured chain for
    `mode` (config.brain.router_chain_*), tier by tier, with the same
    stop-after-partial-output rule _run_candidates uses within a tier —
    applied here across tiers too, so a STRONG response that starts
    streaming and then dies mid-way doesn't get topped up with LOCAL's
    unrelated answer directly after it."""
    if mode == "smart":
        effective = _smart_pick(user_text)
        logger.debug(f"[Router] smart: routing to '{effective}' for {user_text[:50]!r}")
        mode = effective

    chain = _chain_for_mode(mode)
    last_err: Optional[Exception] = None

    for tier in chain:
        yielded_any = False
        try:
            async for chunk in _try_tier(tier, user_text, system_prompt, speak_fn):
                yielded_any = True
                yield chunk
            return
        except Exception as e:
            last_err = e
            logger.warning(f"[Router] tier '{tier}' unavailable: {e}")
            if yielded_any:
                return
            continue

    msg = "Every provider in that chain is unavailable right now, boss."
    logger.error(f"[Router] mode='{mode}' chain={chain} exhausted — last_error={last_err}")
    yield msg


# ── FAST tier: LiteLLM ────────────────────────────────────────────────
# Structurally this mirrors brain.llm._stream_nvidia_nim almost line for
# line — same token-processing pipeline, same tool-call accumulation and
# execution, same memory/session-file bookkeeping, same agentic follow-up
# pass after a tool result comes back. Only the client construction and
# the actual completions call differ (litellm.acompletion against a
# configurable base_url instead of AsyncOpenAI against NIM's fixed one).
# That mirroring is deliberate: it's the same pattern every existing
# provider function here already follows, not a new one introduced for
# this tier.
async def _stream_litellm(user_text: str, system_prompt: str,
                           speak_fn: Optional[Callable], candidate: dict) -> AsyncIterator[str]:
    import litellm
    from brain import llm as base

    model = candidate["name"]
    base_url = candidate["base_url"]
    api_key = candidate["api_key"] or "not-needed"  # some OpenAI-compatible
                                                       # endpoints accept any
                                                       # non-empty string when
                                                       # they don't actually
                                                       # require auth

    base.memory.add_user(user_text)
    state = base._make_openai_stream_handler()

    # "openai/<model>" + api_base is LiteLLM's documented way to treat an
    # arbitrary endpoint as OpenAI-compatible rather than looking `model`
    # up against LiteLLM's own named-provider registry — the right choice
    # here since these are unverified third-party endpoints, not one of
    # LiteLLM's built-in providers.
    stream_resp = await litellm.acompletion(
        model=f"openai/{model}",
        api_base=base_url,
        api_key=api_key,
        messages=[{"role": "system", "content": system_prompt}, *base.memory.get_messages()],
        tools=base._OAI_TOOLS,
        tool_choice="auto",
        temperature=0.85,
        max_tokens=350,
        stream=True,
        timeout=20,
    )

    async for chunk in stream_resp:
        try:
            from ui.ws_server import get_stop_flag
            if get_stop_flag():
                break
        except Exception:
            pass

        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        if not delta:
            continue

        if getattr(delta, "tool_calls", None):
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
            for s in await base._process_token(state, token):
                yield s

    if state["buffer"].strip():
        yield state["buffer"].strip()

    tool_results = await base._execute_tool_calls(state, speak_fn)

    if tool_results:
        import json
        messages = [{"role": "system", "content": system_prompt}, *base.memory.get_messages()]
        oai_tool_calls = [
            {"id": tr["call_id"], "type": "function",
             "function": {"name": tr["name"], "arguments": json.dumps(tr["args"])}}
            for tr in tool_results
        ]
        messages.append({"role": "assistant", "content": state["full_response"] or None, "tool_calls": oai_tool_calls})
        for tr in tool_results:
            messages.append({"role": "tool", "tool_call_id": tr["call_id"], "content": tr["result"]})

        state2 = base._make_openai_stream_handler()
        try:
            stream2 = await litellm.acompletion(
                model=f"openai/{model}", api_base=base_url, api_key=api_key,
                messages=messages, tools=base._OAI_TOOLS, tool_choice="auto",
                temperature=0.85, max_tokens=4096, stream=True, timeout=30,
            )
            async for chunk in stream2:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                if not delta:
                    continue
                token = getattr(delta, "content", None) or ""
                if token:
                    for s in await base._process_token(state2, token):
                        yield s
            if state2["buffer"].strip():
                yield state2["buffer"].strip()
            await base._execute_tool_calls(state2, speak_fn)
        except Exception as e:
            logger.warning(f"[Router] FAST/{model} follow-up pass failed: {e}")

        combined = (state["full_response"] + " " + state2["full_response"]).strip()
        if combined:
            base.memory.add_assistant(combined)
    else:
        if state["full_response"].strip():
            base.memory.add_assistant(state["full_response"])

    import asyncio
    asyncio.create_task(base._background_memory(user_text, base.memory.get_all()))
