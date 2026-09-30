"""
F.R.I.D.A.Y. — actions/topic_monitor.py
User-configured topic watching, ported from Mark-L's
actions/background_monitor.py. Checks DDG news once per day per topic;
returns an alert string when a new headline appears.

Storage: memory/monitors.json, its own small file — deliberately not
mixed into memory/long_term.json, which is fact-shaped, not list-shaped.
"""
import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from config import config
from utils.atomic_write import atomic_write_json

MONITORS_PATH = Path(config.memory_db_path).parent / "monitors.json"
_lock = threading.Lock()

# Blocked categories — never monitor regardless of what's asked.
_BLOCKED = {
    "bitcoin", "ethereum", "dogecoin", "solana", "binance",
    "nft", "blockchain", "defi", "altcoin", "memecoin", "coin", "token",
    "crypto", "cryptocurrency",
}


def _is_blocked(topic: str) -> bool:
    t = topic.lower()
    return any(word in t for word in _BLOCKED)


def _slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.lower().strip())[:40].strip("_")


def _title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _load() -> dict:
    try:
        if MONITORS_PATH.exists():
            return json.loads(MONITORS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(monitors: dict) -> None:
    with _lock:
        MONITORS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(MONITORS_PATH, monitors, indent=2, ensure_ascii=False)


def add_monitor(topic: str) -> str:
    topic = (topic or "").strip()
    if not topic:
        return "Please specify a topic to monitor."
    if _is_blocked(topic):
        return "I don't monitor crypto or financial topics, boss."
    monitors = _load()
    slug = _slug(topic)
    if slug in monitors:
        return f"Already monitoring: {monitors[slug]['topic']}"
    monitors[slug] = {
        "topic": topic,
        "added": datetime.now().strftime("%Y-%m-%d"),
        "last_check": "",
        "last_hash": "",
    }
    _save(monitors)
    return f"Now monitoring: {topic}"


def remove_monitor(topic: str) -> str:
    topic = (topic or "").strip().lower()
    monitors = _load()
    slug = _slug(topic)
    if slug in monitors:
        label = monitors.pop(slug)["topic"]
        _save(monitors)
        return f"Stopped monitoring: {label}"
    for key, val in list(monitors.items()):
        if topic in val.get("topic", "").lower():
            label = monitors.pop(key)["topic"]
            _save(monitors)
            return f"Stopped monitoring: {label}"
    return f"Not currently monitoring: {topic}"


def list_monitors() -> list[str]:
    return [v.get("topic", k) for k, v in _load().items()]


def list_monitors_full() -> list[dict]:
    """Same data, richer shape — used by the UI watchlist panel, which
    wants the added date too, not just the topic name."""
    return [
        {"slug": k, "topic": v.get("topic", k), "added": v.get("added", ""), "last_check": v.get("last_check", "")}
        for k, v in _load().items()
    ]


def check_all() -> list[str]:
    """Run all pending topic checks (once per day per topic). Returns a
    list of short natural-language alert strings — empty if nothing new."""
    from actions.web_search import _ddg_news

    monitors = _load()
    if not monitors:
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    alerts: list[str] = []
    changed = False

    for slug, data in monitors.items():
        if data.get("last_check") == today:
            continue

        topic = data.get("topic", slug)
        try:
            results = _ddg_news(topic, max_results=5)
            monitors[slug]["last_check"] = today
            changed = True
            if not results:
                continue

            top = results[0]
            title = top.get("title", "").strip()
            if not title:
                continue

            h = _title_hash(title)
            if h == data.get("last_hash"):
                continue  # same headline as last check

            monitors[slug]["last_hash"] = h
            alerts.append(f"On {topic}: {title}.")
        except Exception:
            continue

    if changed:
        _save(monitors)

    return alerts
