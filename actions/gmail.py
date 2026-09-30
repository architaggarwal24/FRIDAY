"""
actions/gmail.py — real Gmail integration: list unread, search, read, open,
draft replies, and send, built on the shared OAuth in actions/google_auth.py.

Same conventions as actions/calendar.py: every failure string starts
with "Couldn't" or "I need" (the prefixes brain/llm.py's generic
failure detector already watches for), and this module stays
deterministic — no AI summarisation in here; the conversational layer
does that with what these functions return.

What changed and why (from real logs):
  * Results used to be plain text with the message IDs thrown away, so a
    follow-up like "open the Concord one" had nothing to point at and the
    model fell back to guessing at a screenshot (and invented an email).
    Every list/search now REMEMBERS its results (id, sender, subject…)
    for 30 minutes, and read/open resolve "the second one" / "the
    Concord one" against them deterministically.
  * There was no way to read or open an email at all. read_email() and
    open_email() exist now.
  * Search terms are extracted by extract_terms() — a real filler-word
    filter — instead of crude string stripping that once turned "click on
    the email from Concord Logistics" into a garbage query whose top hit
    was an unrelated (but real!) email, which looked grounded and wasn't.
  * If a multi-word query finds nothing, it's relaxed to its individual
    words and the header says so — never silently answering a different
    question than the one asked.

send_email is the one irreversible action here — sentinel/core.py
classifies it as HIGH risk (held for explicit confirmation).
"""

from __future__ import annotations

import base64
import html
import logging
import re
import time
import webbrowser
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import Optional

logger = logging.getLogger("friday.gmail")

_NOT_CONNECTED = (
    "Couldn't reach Gmail, boss — you're not connected yet. Run "
    "`python google_auth_setup.py` from the repo root to set that up."
)

# ── Remembered results (what "the second one" refers to) ─────────────────────
_LAST_RESULTS: list[dict] = []
_LAST_RESULTS_AT: float = 0.0
_RESULTS_TTL_S = 30 * 60
_PROFILE_EMAIL: Optional[str] = None


def last_results() -> list[dict]:
    """Copy of the most recent list/search results (empty if stale)."""
    if time.time() - _LAST_RESULTS_AT > _RESULTS_TTL_S:
        return []
    return list(_LAST_RESULTS)


def _remember(items: list[dict]) -> None:
    global _LAST_RESULTS, _LAST_RESULTS_AT
    _LAST_RESULTS = list(items)
    _LAST_RESULTS_AT = time.time()


def _get_service():
    from actions.google_auth import get_credentials
    creds = get_credentials()
    if creds is None:
        return None
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds)


# ── Small parsing helpers ─────────────────────────────────────────────────────

def _header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _clean_sender(from_raw: str) -> str:
    # 'Name <email@x.com>' — the name alone is what a spoken answer wants.
    name = re.sub(r"\s*<[^>]*>", "", from_raw).strip().strip('"')
    return name or from_raw


def _pretty_date(raw: str) -> str:
    try:
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%d %b, %I:%M %p").lstrip("0")
    except Exception:
        return raw


def _to_item(msg: dict) -> dict:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    from_raw = _header(headers, "From")
    date_raw = _header(headers, "Date")
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "sender": _clean_sender(from_raw),
        "from_raw": from_raw,
        "subject": _header(headers, "Subject") or "(no subject)",
        "snippet": html.unescape((msg.get("snippet") or "").strip()),
        "date": _pretty_date(date_raw) if date_raw else "",
    }


def _fetch_items(service, ids: list[dict]) -> list[dict]:
    items = []
    for m in ids:
        full = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        items.append(_to_item(full))
    return items


def _line(i: int, it: dict) -> str:
    snippet = it["snippet"]
    if len(snippet) > 80:
        snippet = snippet[:80].rsplit(" ", 1)[0] + "…"
    return f"{i}) {it['sender']} — {it['subject']}" + (f": {snippet}" if snippet else "")


# ── Query extraction: pull the actual search terms out of a request ──────────
_FILLER = set("""
click open find check look looking search searching show read tell see view get give pull fetch go take
need want please can could would you me my mine the a an on in at for of to any all latest recent newest
last new one ones mail mails email emails message messages inbox gmail google mailbox from about regarding
concerning that this these those it screen browser tab window up is there are was were do does did i im if
whether received receive got have has had content contents unread whats what who which whose and or with by
into out so then just also again first second third fourth fifth top sent subject titled called named
sender related open opened opening whatever thing stuff
""".split())

_ORDINALS = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "last": -1,
    "latest": 0, "newest": 0, "top": 0, "recent": 0,
}


def extract_terms(text: str) -> str:
    """The real search terms in a request, filler and action words removed,
    original casing/order kept. 'click on the email from Concord Logistics'
    -> 'Concord Logistics'; 'the second one' -> ''. A quoted phrase wins
    outright."""
    if not text:
        return ""
    q = re.search(r'["“]([^"”]+)["”]', text)
    if q:
        return q.group(1).strip()
    kept = []
    for tok in re.findall(r"[A-Za-z0-9&@.'\-]+", text):
        clean = tok.strip(".'-")
        if not clean:
            continue
        low = clean.lower()
        if low in _FILLER or low.rstrip("s") in ("email", "mail"):
            continue
        kept.append(clean)
    return " ".join(kept)


def _ordinal_index(text: str) -> Optional[int]:
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in _ORDINALS:
            return _ORDINALS[w]
    return None


def _haystack(it: dict) -> str:
    return f"{it['sender']} {it['from_raw']} {it['subject']} {it['snippet']}".lower()


def _term_score(terms: str, it: dict) -> float:
    toks = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", terms) if len(t) > 1]
    if not toks:
        return 0.0
    hay = _haystack(it)
    return sum(1 for t in toks if t in hay) / len(toks)


# ── Search / list (data layer + text layer) ──────────────────────────────────

def search_data(query: str, max_results: int = 10):
    """-> (items, error_text, used_query). Newest first (Gmail's order),
    with results that actually contain every term ranked ahead of ones
    that merely matched somewhere in the body. A multi-word query that
    finds nothing is relaxed to its single words — and used_query says
    so, so the answer never quietly addresses a different question."""
    service = _get_service()
    if service is None:
        return [], _NOT_CONNECTED, query
    if not query or not query.strip():
        return [], "I need something to search for.", query
    n = max(1, min(max_results, 25))

    def _run(q: str):
        res = service.users().messages().list(userId="me", q=q, maxResults=n).execute()
        return res.get("messages", [])

    try:
        used = query.strip()
        ids = _run(used)
        if not ids:
            words = sorted({w for w in re.findall(r"[A-Za-z0-9]+", used) if len(w) >= 3},
                           key=len, reverse=True)
            if len(words) > 1:
                for w in words[:2]:
                    ids = _run(w)
                    if ids:
                        used = w
                        break
        if not ids:
            return [], None, used
        items = _fetch_items(service, ids)
        items.sort(key=lambda it: -_term_score(used, it))  # stable: keeps newest-first within ties
        _remember(items)
        return items, None, used
    except Exception as e:
        return [], f"Couldn't search Gmail: {e}", query


def list_unread_data(max_results: int = 10):
    service = _get_service()
    if service is None:
        return [], _NOT_CONNECTED
    try:
        res = service.users().messages().list(
            userId="me", labelIds=["UNREAD", "INBOX"], maxResults=max(1, min(max_results, 25)),
        ).execute()
        ids = res.get("messages", [])
        if not ids:
            return [], None
        items = _fetch_items(service, ids)
        _remember(items)
        return items, None
    except Exception as e:
        return [], f"Couldn't reach Gmail: {e}"


def list_unread(max_results: int = 10) -> str:
    items, err = list_unread_data(max_results)
    if err:
        return err
    if not items:
        return "No unread emails, boss."
    return f"{len(items)} unread:\n" + "\n".join(_line(i, it) for i, it in enumerate(items, 1))


def search(query: str, max_results: int = 10) -> str:
    items, err, used = search_data(query, max_results)
    if err:
        return err
    if not items:
        return f"Nothing matching '{used}' in Gmail."
    note = f" (broadened from '{query.strip()}')" if used != query.strip() else ""
    return (f"Found {len(items)} matching '{used}'{note}:\n"
            + "\n".join(_line(i, it) for i, it in enumerate(items, 1)))


# ── Resolving "the second one" / "the Concord one" ───────────────────────────

def resolve(ref: str) -> Optional[dict]:
    """Turn a spoken reference into ONE message dict: an ordinal or
    matching words against the remembered results first, else a fresh
    search. None if nothing plausible."""
    terms = extract_terms(ref or "")
    idx = _ordinal_index(ref or "")
    recent = last_results()

    if recent:
        if terms:
            scored = sorted(((_term_score(terms, it), -n, it) for n, it in enumerate(recent)),
                            key=lambda s: (s[0], s[1]), reverse=True)
            if scored and scored[0][0] >= 0.6:
                return scored[0][2]
        elif idx is not None:
            i = idx if idx >= 0 else len(recent) + idx
            if 0 <= i < len(recent):
                return recent[i]
        else:
            return recent[0]  # "open it" / "that one" — the top/only result

    if terms:
        items, err, _ = search_data(terms, 3)
        if items and not err:
            return items[0]
    return None


def _not_found(ref: str) -> str:
    terms = extract_terms(ref or "")
    what = f" matching '{terms}'" if terms else " to use"
    return f"Couldn't find an email{what}, boss — try searching first."


# ── Read / open ──────────────────────────────────────────────────────────────

def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    plain, htmls = [], []

    def walk(p: dict):
        mime = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode_part(data))
        elif data and mime == "text/html":
            htmls.append(_decode_part(data))
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload or {})
    text = "\n".join(plain).strip()
    if not text and htmls:
        h = "\n".join(htmls)
        h = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", h)
        h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", h)
        h = re.sub(r"<[^>]+>", " ", h)
        text = html.unescape(h)
    text = re.sub(r"https?://\S+", "[link]", text)
    text = re.sub(r"[ \t\r\f\v\u200b\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text


def read_data(ref: str, max_chars: int = 1800):
    """-> (item, body, error). item is the resolved message dict."""
    service = _get_service()
    if service is None:
        return None, "", _NOT_CONNECTED
    item = resolve(ref)
    if not item:
        return None, "", _not_found(ref)
    try:
        full = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
    except Exception as e:
        return item, "", f"Couldn't read that email: {e}"
    body = _extract_body(full.get("payload", {}))
    if len(body) > max_chars:
        body = body[:max_chars].rsplit(" ", 1)[0] + " […]"
    return item, body or "(no readable text in this email)", None


def read_email(ref: str, max_chars: int = 1800) -> str:
    item, body, err = read_data(ref, max_chars)
    if err:
        return err
    return (f"From: {item['sender']}\nSubject: {item['subject']}\n"
            f"Date: {item['date']}\n\n{body}")


def _profile_email(service) -> Optional[str]:
    global _PROFILE_EMAIL
    if _PROFILE_EMAIL is None:
        try:
            _PROFILE_EMAIL = service.users().getProfile(userId="me").execute().get("emailAddress")
        except Exception:
            return None
    return _PROFILE_EMAIL


def open_email(ref: str) -> str:
    """Opens the message in Gmail in the browser. authuser pins it to the
    connected account — plain /u/0/ opens whichever account happens to be
    first in the browser, which may not be this one."""
    service = _get_service()
    if service is None:
        return _NOT_CONNECTED
    item = resolve(ref)
    if not item:
        return _not_found(ref)
    email = _profile_email(service)
    url = (f"https://mail.google.com/mail/?authuser={email}#all/{item['id']}" if email
           else f"https://mail.google.com/mail/u/0/#all/{item['id']}")
    try:
        webbrowser.open(url)
    except Exception as e:
        return f"Couldn't open the browser: {e}"
    return f"Opened '{item['subject']}' from {item['sender']} in Gmail."


# ── Draft / send ─────────────────────────────────────────────────────────────

def draft_reply(query: str, body: str) -> str:
    """Creates a DRAFT only — never sends. A draft is fully reversible
    (the user reviews it in Gmail before anything goes out), which is
    exactly why this isn't sentinel-gated the way send_email is."""
    service = _get_service()
    if service is None:
        return _NOT_CONNECTED
    if not body or not body.strip():
        return "I need a message body to draft a reply."
    original = resolve(query) if query else None
    if not original:
        return f"Couldn't find an email matching '{query}' to reply to."
    try:
        subject = original["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        to = original["from_raw"]
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().drafts().create(
            userId="me", body={"message": {"raw": raw, "threadId": original["thread_id"]}}
        ).execute()
        return f"Draft saved: reply to {to} — '{subject}'. Review it in Gmail before sending."
    except Exception as e:
        return f"Couldn't create that draft: {e}"


def send_email(to: str, subject: str, body: str, in_reply_to_query: Optional[str] = None) -> str:
    """The one irreversible action in this file — see sentinel/core.py,
    this is HIGH risk and held for explicit confirmation before ever
    reaching this function."""
    service = _get_service()
    if service is None:
        return _NOT_CONNECTED
    if not to or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
        return "I need a valid recipient email address to send this."
    if not subject or not subject.strip():
        return "I need a subject line to send this."
    if not body or not body.strip():
        return "I need a message body to send this."

    thread_id = None
    if in_reply_to_query:
        original = resolve(in_reply_to_query)
        if original:
            thread_id = original["thread_id"]
            if original["subject"] and not subject.lower().startswith("re:"):
                subject = f"Re: {original['subject']}"

    try:
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body_payload = {"raw": raw}
        if thread_id:
            body_payload["threadId"] = thread_id
        service.users().messages().send(userId="me", body=body_payload).execute()
        return f"Sent to {to}: '{subject}'."
    except Exception as e:
        return f"Couldn't send that email: {e}"
