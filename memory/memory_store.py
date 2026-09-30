"""
F.R.I.D.A.Y. — memory/memory_store.py
SQLite-backed persistent memory: facts, notes, summaries, conversation log, action errors.
"""

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from config import config

logger = logging.getLogger(__name__)
DB_PATH = str(config.memory_db_path)
_conn: Optional[sqlite3.Connection] = None
_conn_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is None:
            Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def checkpoint_wal() -> None:
    """Flushes the WAL file into the main DB file without closing the
    connection, so a plain byte-copy of DB_PATH alone (no separate -wal
    file) reflects the true current state. Used by db_crypto before each
    periodic re-encryption checkpoint."""
    global _conn
    if _conn is None:
        return
    try:
        _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        logger.warning(f"[Memory] WAL checkpoint failed: {e}")


def close_db() -> None:
    """Checkpoints and closes the live connection. Used by db_crypto on
    shutdown, before the final re-encryption — encrypting while the
    connection (and its -wal/-shm files) is still open would capture an
    inconsistent snapshot."""
    global _conn
    if _conn is None:
        return
    try:
        _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _conn.close()
    except Exception as e:
        logger.warning(f"[Memory] close_db failed: {e}")
    finally:
        _conn = None


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                key      TEXT NOT NULL UNIQUE,
                value    TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'notes',
                updated  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notes (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summaries (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                summary TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                role    TEXT NOT NULL,
                content TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_errors (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                action_key TEXT NOT NULL,
                error      TEXT NOT NULL,
                created    REAL NOT NULL
            );
        """)
    logger.info("Memory DB initialized.")


# ── Facts ─────────────────────────────────────────────────────────────────────

def upsert_fact(key: str, value: str, category: str = "notes"):
    if not key or not value:
        return
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO facts (key, value, category, updated) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "category=excluded.category, updated=excluded.updated",
            (key.strip(), value.strip(), category, time.time())
        )
        conn.commit()
    except Exception as e:
        logger.error(f"[Memory] upsert_fact: {e}")


def get_facts(category: Optional[str] = None, limit: int = 50) -> dict:
    try:
        conn = _connect()
        if category:
            rows = conn.execute(
                "SELECT key, value FROM facts WHERE category=? ORDER BY updated DESC LIMIT ?",
                (category, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, value FROM facts ORDER BY updated DESC LIMIT ?", (limit,)
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


# ── Notes ─────────────────────────────────────────────────────────────────────

def save_note(content: str):
    try:
        conn = _connect()
        conn.execute("INSERT INTO notes (content, created) VALUES (?, ?)", (content, time.time()))
        conn.commit()
    except Exception as e:
        logger.error(f"[Memory] save_note: {e}")


def get_notes(limit: int = 10) -> list[str]:
    try:
        rows = _connect().execute(
            "SELECT content FROM notes ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["content"] for r in rows]
    except Exception:
        return []


# ── Summaries ──────────────────────────────────────────────────────────────────

def save_summary(text: str):
    try:
        conn = _connect()
        conn.execute(
            "DELETE FROM summaries WHERE id NOT IN "
            "(SELECT id FROM summaries ORDER BY created DESC LIMIT 4)"
        )
        conn.execute("INSERT INTO summaries (summary, created) VALUES (?, ?)", (text, time.time()))
        conn.commit()
    except Exception as e:
        logger.error(f"[Memory] save_summary: {e}")


def get_summaries(limit: int = 3) -> list[str]:
    try:
        rows = _connect().execute(
            "SELECT summary FROM summaries ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["summary"] for r in rows]
    except Exception:
        return []


def consume_latest_summary() -> Optional[str]:
    """Fetch AND delete the most recent summary in one go, so the morning
    briefing mentions it once and doesn't repeat it on the next boot."""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT id, summary FROM summaries ORDER BY created DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM summaries WHERE id = ?", (row["id"],))
        conn.commit()
        return row["summary"]
    except Exception:
        return None


# ── Conversations ──────────────────────────────────────────────────────────────

def log_message(role: str, content: str):
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO conversations (role, content, created) VALUES (?, ?, ?)",
            (role, content, time.time())
        )
        conn.execute(
            "DELETE FROM conversations WHERE id NOT IN "
            "(SELECT id FROM conversations ORDER BY created DESC LIMIT 1000)"
        )
        conn.commit()
    except Exception:
        pass


# ── Action Errors ──────────────────────────────────────────────────────────────

def save_action_error(action_key: str, error: str):
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO action_errors (action_key, error, created) VALUES (?, ?, ?)",
            (action_key, error, time.time())
        )
        conn.commit()
    except Exception:
        pass


def get_action_errors(limit: int = 10) -> dict:
    try:
        rows = _connect().execute(
            "SELECT action_key, error FROM action_errors ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()
        return {r["action_key"]: r["error"] for r in rows}
    except Exception:
        return {}


# ── Context Builder ────────────────────────────────────────────────────────────

def _faiss_available() -> bool:
    try:
        import faiss                                          # noqa: F401
        from sentence_transformers import SentenceTransformer  # noqa: F401
        return True
    except ImportError:
        return False


_encoder = None
_faiss_index = None
_faiss_meta: list[dict] = []


def _get_encoder():
    global _encoder
    if _encoder is None:
        from sentence_transformers import SentenceTransformer
        _encoder = SentenceTransformer("all-MiniLM-L6-v2")
    return _encoder


def _rebuild_faiss_index():
    """Rebuilds the in-memory FAISS index from the current facts table."""
    global _faiss_index, _faiss_meta
    if not _faiss_available():
        return
    try:
        import faiss, numpy as np
        rows = _connect().execute(
            "SELECT key, value, category FROM facts ORDER BY updated DESC LIMIT 500"
        ).fetchall()
        if not rows:
            return
        texts = [f"{r['category']} {r['key']}: {r['value']}" for r in rows]
        meta  = [{"key": r["key"], "category": r["category"]} for r in rows]

        enc   = _get_encoder()
        vecs  = enc.encode(texts, convert_to_numpy=True, normalize_embeddings=True).astype("float32")
        index = faiss.IndexFlatIP(vecs.shape[1])
        index.add(vecs)

        _faiss_index = index
        _faiss_meta  = meta
        logger.debug(f"[Memory] FAISS index: {len(rows)} facts")
    except Exception as e:
        logger.warning(f"[Memory] FAISS rebuild failed: {e}")


def _semantic_search(query: str, k: int = 20) -> list[dict]:
    """Returns top-k {key, category} hits. Empty on failure."""
    global _faiss_index, _faiss_meta
    if _faiss_index is None or not _faiss_meta:
        return []
    try:
        import numpy as np
        enc  = _get_encoder()
        vec  = enc.encode([query], convert_to_numpy=True, normalize_embeddings=True).astype("float32")
        _, idxs = _faiss_index.search(vec, min(k, _faiss_index.ntotal))
        return [_faiss_meta[i] for i in idxs[0] if 0 <= i < len(_faiss_meta)]
    except Exception as e:
        logger.warning(f"[Memory] Semantic search failed: {e}")
        return []


# Patch upsert_fact to rebuild index after writes
_orig_upsert = upsert_fact


def upsert_fact(key: str, value: str, category: str = "notes"):  # type: ignore[misc]
    _orig_upsert(key, value, category)
    _rebuild_faiss_index()


def build_memory_context(query: str = "") -> str:
    """
    Formats memory for system prompt injection.

    If sentence-transformers + FAISS are available AND a query is given,
    only the top-K semantically relevant facts are included.
    Falls back to full structured dump if FAISS is unavailable.

    The 2200-char cap is removed — semantic retrieval keeps size bounded naturally.
    """
    use_semantic = _faiss_available() and bool(query)

    if use_semantic:
        hits    = _semantic_search(query, k=25)
        hit_set = {(h["category"], h["key"]) for h in hits}
        # Always include identity regardless of relevance
        id_rows = _connect().execute(
            "SELECT key, value, category FROM facts WHERE category='identity' ORDER BY updated DESC"
        ).fetchall()
        selected_keys = {(r["category"], r["key"]) for r in id_rows} | hit_set
    else:
        selected_keys = None  # means "all"

    lines = []

    CAT_ORDER = ["identity", "preferences", "projects", "relationships", "wishes", "notes"]
    CAT_LABELS = {
        "identity":      "About you",
        "preferences":   "Preferences",
        "projects":      "Active projects",
        "relationships": "People",
        "wishes":        "Wishes / plans",
        "notes":         "Notes",
    }

    for cat in CAT_ORDER:
        limit = 20 if cat == "identity" else 15
        rows = _connect().execute(
            "SELECT key, value FROM facts WHERE category=? ORDER BY updated DESC LIMIT ?",
            (cat, limit)
        ).fetchall()

        cat_lines = []
        for r in rows:
            if selected_keys and (cat, r["key"]) not in selected_keys:
                continue
            cat_lines.append(f"  - {r['key'].replace('_', ' ').title()}: {r['value']}")

        if cat_lines:
            lines.append(f"{CAT_LABELS.get(cat, cat)}:")
            lines.extend(cat_lines)
            lines.append("")

    # Raw notes
    raw_notes = get_notes(limit=5)
    if raw_notes:
        if "Notes:" not in "\n".join(lines):
            lines.append("Notes:")
        for n in raw_notes:
            lines.append(f"  - {n}")
        lines.append("")

    # Past session summaries
    summaries = get_summaries(limit=2)
    if summaries:
        lines.append("Past sessions:")
        for s in summaries:
            lines.append(f"  {s}")
        lines.append("")

    if not lines:
        return ""

    mode = " [semantic]" if use_semantic else ""
    header = f"[MEMORY{mode} — use naturally, never recite like a list]\n"
    return header + "\n".join(lines) + "\n"


# ── Background: fact extraction ────────────────────────────────────────────────

async def extract_and_save_facts(user_text: str):
    TRIGGERS = [
        "my name", "i am", "i'm", "i work", "i live", "i like",
        "i love", "i hate", "i prefer", "my job", "my project",
        "i use", "remember", "don't forget", "save this", "note that"
    ]
    if not any(t in user_text.lower() for t in TRIGGERS):
        return

    prompt = (
        f'Extract saveable facts from: "{user_text}"\n'
        'Return a JSON array of {"category":"identity|preferences|relationships|wishes|notes",'
        '"key":"snake_case","value":"concise"} objects. Return [] if nothing worth saving. No markdown.'
    )

    try:
        raw   = await _llm_call(prompt)
        text  = re.sub(r"```json|```", "", raw).strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        facts = json.loads(match.group(0) if match else text)
        # Save to long_term (structured JSON), not SQLite facts table
        from memory.long_term import remember_many
        remember_many(facts)
    except Exception as e:
        logger.debug(f"[Memory] Fact extraction skipped: {e}")


async def summarize_and_save(messages: list[dict]):
    try:
        conv = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages[-20:])
        summary = await _llm_call(
            f"Summarize this conversation in 2-3 sentences for future reference:\n{conv}"
        )
        save_summary(summary.strip())
        logger.info("[Memory] Conversation summarized.")
    except Exception as e:
        logger.debug(f"[Memory] Summarization failed: {e}")


async def _llm_call(prompt: str) -> str:
    """Shared LLM helper for memory ops — Gemini first, NIM fallback, Ollama last."""
    if config.brain.gemini_api_key:
        import google.genai as genai
        _genai_client = genai.Client(api_key=config.brain.gemini_api_key)
        _genai_model = config.brain.gemini_model
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: _genai_client.models.generate_content(model=_genai_model, contents=prompt))
        return resp.text

    if config.brain.nvidia_nim_api_key:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=config.brain.nvidia_nim_api_key,
            base_url="https://integrate.api.nvidia.com/v1",
        )
        resp = await client.chat.completions.create(
            model=config.brain.nvidia_nim_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=300,
        )
        return resp.choices[0].message.content or ""

    # Ollama fallback
    import ollama as ol
    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(
        None,
        lambda: ol.chat(
            model=config.brain.ollama_model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.1, "num_predict": 300},
        )
    )
    # FIX: Handle both dict (older ollama SDK) and object (newer SDK) responses
    return (resp["message"]["content"] if isinstance(resp, dict) else resp.message.content)


# Initialize on import — encryption setup must run first, since it needs
# to decrypt (or migrate) the on-disk file into a normal working copy
# before anything opens it as a live SQLite connection.
from memory.db_crypto import MemoryDBEncryption
_db_encryption = MemoryDBEncryption(Path(DB_PATH))
_db_encryption.setup()

init_db()