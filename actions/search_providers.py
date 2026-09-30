"""
actions/search_providers.py — Tavily (primary) + Exa (fallback) web
search, replacing the DDG/Bing scraping approach that kept silently
returning zero results.

Failover behavior, as specced:
- Tavily is tried first on every call while it isn't marked exhausted.
- A single call failing (network error, timeout, non-quota error) falls
  back to Exa for THAT call only — Tavily stays primary next time.
- A quota-exceeded response (HTTP 429, or a 4xx whose body mentions
  usage/credit limits) marks Tavily exhausted for real: Exa becomes
  primary for every call after that until the retry window passes
  (memory/usage_tracker's 24h default) and Tavily gets tried again.
- If Tavily recovers (a retry succeeds), it's promoted back to primary
  automatically — mark_ok() clears the exhausted flag.

Both are real APIs with actual response bodies, not scrapers — a 200
response reliably means real results, unlike the DDG path this replaces.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from memory import usage_tracker as ut

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
EXA_URL = "https://api.exa.ai/search"
_TIMEOUT = 15


class QuotaExceeded(Exception):
    """Raised specifically for a quota/rate-limit response — distinct
    from a generic request failure so the caller knows whether to mark
    the provider exhausted or just try the other one this once."""


def _tavily_search(query: str, max_results: int = 5) -> list[dict]:
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not set")

    resp = requests.post(
        TAVILY_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code == 429:
        raise QuotaExceeded(f"Tavily rate/quota limit: {resp.text[:200]}")
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "snippet": r.get("content", ""), "url": r.get("url", "")}
        for r in data.get("results", [])
    ]


def _exa_search(query: str, max_results: int = 5) -> list[dict]:
    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key:
        raise RuntimeError("EXA_API_KEY not set")

    resp = requests.post(
        EXA_URL,
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={
            "query": query,
            "numResults": max_results,
            "contents": {"highlights": True},
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code == 429:
        raise QuotaExceeded(f"Exa rate/quota limit: {resp.text[:200]}")
    resp.raise_for_status()
    data = resp.json()
    results = []
    for r in data.get("results", []):
        highlights = r.get("highlights") or []
        snippet = " ".join(highlights) if highlights else (r.get("text", "") or "")[:500]
        results.append({"title": r.get("title", ""), "snippet": snippet, "url": r.get("url", "")})
    return results


def fetch_tavily_usage() -> Optional[dict]:
    """GET /usage — a real endpoint Tavily exposes, unlike Exa (which
    needs a separate team-admin key) or Gemini (which needs full GCP
    monitoring setup). Returns account-level credit usage/limit."""
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return None
    try:
        resp = requests.get(
            "https://api.tavily.com/usage",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response shape has a key-level and account-level usage object;
        # account-level is what actually matters for "how close to the
        # free tier's 1,000/month am I."
        account = data.get("account", data)
        used = account.get("usage") or account.get("used")
        limit = account.get("limit")
        if used is not None and limit is not None:
            ut.set_real_quota("tavily", used=used, limit=limit, unit="credits")
            return {"used": used, "limit": limit, "unit": "credits"}
    except Exception as e:
        logger.debug(f"[Search] Couldn't fetch Tavily usage: {e}")
    return None


def web_search_with_fallback(query: str, max_results: int = 5) -> tuple[list[dict], str]:
    """Returns (results, provider_used). provider_used is "tavily",
    "exa", or "none" — the caller (handle_web_search) uses this to fall
    through to the old DDG scraper as a final, zero-cost last resort if
    both real APIs are unavailable (no keys set, or both genuinely down),
    and to be honest with the user about where an answer came from."""

    tavily_available = bool(os.environ.get("TAVILY_API_KEY"))
    exa_available = bool(os.environ.get("EXA_API_KEY"))
    tavily_exhausted = ut.is_exhausted("tavily")

    order = []
    if tavily_available and not tavily_exhausted:
        order.append("tavily")
    if exa_available:
        order.append("exa")
    # If tavily is exhausted, it's simply excluded here — not retried
    # every call. is_exhausted() already handles "try again after the
    # cooldown": once that window passes, tavily_exhausted becomes False
    # and it's added back above, first in line. Retrying it on every
    # single call during the cooldown would just re-trigger the same
    # rate limit repeatedly, which defeats the point of tracking it.

    for provider in order:
        try:
            ut.record_call(provider)
            if provider == "tavily":
                results = _tavily_search(query, max_results)
            else:
                results = _exa_search(query, max_results)
            ut.mark_ok(provider)
            if results:
                return results, provider
            # Empty-but-successful response — not an error, just no
            # hits for this query. Don't fall through to the other
            # provider on an honest "nothing found."
            return [], provider
        except QuotaExceeded as e:
            logger.warning(f"[Search] {provider} quota exceeded: {e}")
            ut.mark_exhausted(provider)
            continue  # try the next provider in order
        except Exception as e:
            logger.warning(f"[Search] {provider} request failed: {e}")
            continue  # transient failure — try the next provider, don't mark exhausted

    return [], "none"
