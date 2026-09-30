"""
F.R.I.D.A.Y. — memory/open_loops.py
Explicit "follow up on this later" tracking — the piece "notices things
on its own" was actually missing. reminder.py already handles anything
with a real date/time (fires via the OS scheduler, works even if FRIDAY
isn't running). topic_monitor.py already handles "watch this external
thing." Neither covers "I said I'd think about X" / "FRIDAY said it'd
check on Y" with no specific time attached — vague, but still something
worth resurfacing eventually rather than silently forgetting.

Deliberately explicit, not inferred: a loop is only created when the
model calls the open_loop tool (see core/prompt.txt for when it should),
never guessed from conversation history after the fact. Silently
inferring "unresolved threads" from a transcript is a false-positive
machine — confidently resurfacing something already handled elsewhere,
or misreading a passing remark as a commitment, does more harm than the
occasional thing that goes untracked. An explicit tag is slower to build
up but never wrong about what it claims to be tracking.

Storage: memory/open_loops.json — same shape convention as
topic_monitor.py's monitors.json, kept separate from long_term.json
(fact-shaped) since this is list-shaped and has its own lifecycle
(resolved / resurfaced timestamps) that doesn't belong in the vault.
"""
import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from config import config
from utils.atomic_write import atomic_write_json

LOOPS_PATH = Path(config.memory_db_path).parent / "open_loops.json"
_lock = threading.Lock()

# A loop is eligible for its first resurface once it's been open this long...
_MIN_AGE_BEFORE_FIRST_SURFACE = timedelta(hours=20)
# ...and, if still unresolved after being mentioned, isn't brought up
# again until this much time has passed — the point is an occasional
# nudge, not a repeating nag every single check cycle.
_MIN_GAP_BETWEEN_SURFACES = timedelta(days=6)


def _load() -> list:
    try:
        if LOOPS_PATH.exists():
            return json.loads(LOOPS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save(loops: list) -> None:
    with _lock:
        LOOPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(LOOPS_PATH, loops, indent=2, ensure_ascii=False)


def add_loop(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Nothing to track — what should I follow up on?"
    loops = _load()
    loops.append({
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "created": datetime.now().isoformat(),
        "last_resurfaced": "",
        "resolved": False,
    })
    _save(loops)
    return f"Got it — I'll follow up on that: {text}"


def resolve_loop(query: str) -> str:
    """Matches by id, or by substring of the text if no id matches —
    same forgiving lookup style as topic_monitor.remove_monitor."""
    query = (query or "").strip()
    loops = _load()
    for l in loops:
        if l["id"] == query and not l["resolved"]:
            l["resolved"] = True
            _save(loops)
            return f'Marked as resolved: "{l["text"]}"'
    ql = query.lower()
    for l in loops:
        if not l["resolved"] and ql in l["text"].lower():
            l["resolved"] = True
            _save(loops)
            return f'Marked as resolved: "{l["text"]}"'
    return f"Couldn't find an open loop matching: {query}"


def list_open() -> list:
    return [l for l in _load() if not l["resolved"]]


def due_for_resurface() -> dict | None:
    """Picks at most ONE loop to bring up this cycle — never floods
    several at once. Marks it resurfaced (persisted) before returning,
    so the next hourly check doesn't immediately re-pick the same one."""
    loops = _load()
    now = datetime.now()
    for l in loops:
        if l["resolved"]:
            continue
        try:
            created = datetime.fromisoformat(l["created"])
        except Exception:
            continue
        if now - created < _MIN_AGE_BEFORE_FIRST_SURFACE:
            continue
        if l["last_resurfaced"]:
            try:
                last = datetime.fromisoformat(l["last_resurfaced"])
                if now - last < _MIN_GAP_BETWEEN_SURFACES:
                    continue
            except Exception:
                pass
        l["last_resurfaced"] = now.isoformat()
        _save(loops)
        return l
    return None
