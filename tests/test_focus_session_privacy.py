"""
test_focus_session_privacy.py — dedicated privacy regression test for
the drift-callout label feature in actions/focus_session.py.

The feature: a drift callout names what you drifted into ("That's
Instagram, not what we're working on") instead of just saying you
drifted. The label is derived fresh, used for exactly one spoken line,
and must never appear anywhere else — not in focus_state(), not in any
broadcast payload, not in the end-of-session report or ledger.

This is deliberately a SEPARATE file from test_focus_session.py (which
covers the feature's correctness) so the privacy guarantee has its own
standalone, unambiguous pass/fail, per the prompt's instructions.

Method: drift into a made-up site name that would never appear in the
canned line pools by coincidence, capture every session artifact
(state snapshots, broadcast payloads, the end-of-session report), and
grep all of it for that made-up name. It must appear ONLY in the one
spoken line captured at the moment of the callout — nowhere else.

Run from the project root:
    python tests/test_focus_session_privacy.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import focus_session as fs   # noqa: E402
from memory import long_term as lt        # noqa: E402

results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


# A made-up site name. Distinctive, not a real service, and specifically
# NOT one of the words used anywhere in _DRIFT_TIERS/_RETURN_LINES/
# _SITE_LABELS/_APP_DISPLAY_NAMES — so if it turns up anywhere outside
# the one captured spoken line, it can only have gotten there by
# actually carrying the raw window identity along, not by coincidence.
MADE_UP_TITLE = "Zorblaxxian Dashboard - Quibberflarp Industries"
MADE_UP_PROC = "quibberflarp.exe"
MADE_UP_NEEDLE = "quibberflarp"   # case-insensitive search target

WORK_TITLE, WORK_PROC = "thesis_chapter_3.docx - Word", "WINWORD.EXE"


class Harness:
    """Same shape as test_focus_session.py's — patches
    brain.handlers._get_frontmost_window directly so both
    fs._read_frontmost() and fs._read_drift_label() see the same fake
    window consistently."""

    def __init__(self):
        self.spoken = []
        self.broadcasts = []
        self.current = (WORK_TITLE, WORK_PROC)
        import brain.handlers as handlers_mod
        self._handlers_mod = handlers_mod
        self._orig_get_frontmost = handlers_mod._get_frontmost_window

    def __enter__(self):
        self._handlers_mod._get_frontmost_window = lambda: self.current
        fs.configure(speak_fn=self.spoken.append, broadcast_fn=self._broadcast)
        fs._session = None
        fs._last_broadcast_state = None
        return self

    def __exit__(self, *exc):
        self._handlers_mod._get_frontmost_window = self._orig_get_frontmost
        fs.configure(speak_fn=None, broadcast_fn=None)
        fs._session = None
        fs._last_broadcast_state = None

    def _broadcast(self, payload):
        self.broadcasts.append(payload)

    def switch_to(self, title, proc):
        self.current = (title, proc)


def _reset_store():
    tmp_dir = Path(tempfile.mkdtemp(prefix="friday_focusprivacytest_"))
    lt.VAULT_PATH = tmp_dir / "FRIDAY_Brain"
    lt.INDEX_PATH = tmp_dir / "lt_faiss.index"
    lt.META_PATH = tmp_dir / "lt_faiss_meta.json"
    lt._LEGACY_MEMORY_PATH = tmp_dir / "long_term.json"
    return tmp_dir


async def main():
    _reset_store()
    with Harness() as h:
        # Sanity check on the test's own premise: the made-up name must
        # not already appear in any canned pool, or this test would be
        # meaningless (it'd "leak" by pure coincidence).
        pools_blob = repr(fs._DRIFT_TIERS) + repr(fs._RETURN_LINES) + \
            repr(fs._SITE_LABELS) + repr(fs._APP_DISPLAY_NAMES)
        record("sanity: the made-up name isn't already in any canned pool",
               MADE_UP_NEEDLE not in pools_blob.lower())

        await fs.start(minutes=25, label="thesis")
        t = 1000.0
        await fs.tick(now=t)   # baseline = work window

        # Drift into the made-up site, past the grace period, and let
        # it escalate through a couple of nags so multiple callouts
        # (each deriving the label fresh) get a chance to leak if
        # they're going to.
        h.switch_to(MADE_UP_TITLE, MADE_UP_PROC)
        fs._session.nag_interval_s = 2
        t += 1
        await fs.tick(now=t)
        t += 1
        await fs.tick(now=t)      # 1st callout
        t += 3
        await fs.tick(now=t)      # 2nd callout (tier 2)

        # Confirm the feature actually fired and actually named the
        # made-up site — otherwise this test would trivially "pass" by
        # never exercising the label derivation at all.
        callouts_with_label = [s for s in h.spoken if MADE_UP_NEEDLE in s.lower()]
        record("the callout actually names the made-up site (test isn't vacuous)",
               len(callouts_with_label) >= 1, h.spoken)

        # Capture the state and every broadcast BEFORE ending the
        # session, then abort to also capture the end-of-session report
        # and whatever gets written to the ledger.
        mid_session_state = fs.focus_state()
        mid_session_broadcasts = list(h.broadcasts)

        report = await fs.abort()

        notes = (lt.get_all() or {}).get("notes", {}) or {}
        ledger_entries = {k: v for k, v in notes.items() if k.startswith(fs._LEDGER_KEY_PREFIX)}

        # ── THE check: everything a client / the vault could ever see,
        # searched for the made-up name. Only h.spoken (captured at the
        # exact moment of each callout) is allowed to contain it.
        client_visible_blob = repr({
            "mid_session_state": mid_session_state,
            "mid_session_broadcasts": mid_session_broadcasts,
            "post_abort_broadcasts": h.broadcasts,
            "end_of_session_report": report,
            "ledger_entries": ledger_entries,
        })

        record("made-up site name does not appear in focus_state()",
               MADE_UP_NEEDLE not in repr(mid_session_state).lower(), mid_session_state)
        record("made-up site name does not appear in any broadcast payload",
               MADE_UP_NEEDLE not in repr(h.broadcasts).lower(), h.broadcasts)
        record("made-up site name does not appear in the end-of-session report",
               MADE_UP_NEEDLE not in repr(report).lower(), report)
        record("made-up site name does not appear in the vault ledger",
               MADE_UP_NEEDLE not in repr(ledger_entries).lower(), ledger_entries)
        record("made-up site name does not appear ANYWHERE in the combined client-visible blob",
               MADE_UP_NEEDLE not in client_visible_blob.lower())

        # And the reverse sanity check: it DID appear in the spoken
        # lines, confirming the search itself is capable of finding it
        # (a search that can't find anything proves nothing).
        record("...while confirming the needle-search itself actually works (found it in speech)",
               MADE_UP_NEEDLE in repr(h.spoken).lower(), h.spoken)

        # Same check, but for the raw process name too (belt and
        # braces — the site-label path and the app-fallback path are
        # different code paths; both need to hold).
        record("raw .exe process name doesn't leak into client-visible state either",
               "quibberflarp.exe" not in client_visible_blob.lower())

    print()
    print("=== SUMMARY ===")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
