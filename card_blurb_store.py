"""
card_blurb_store.py

Next-batch instruction, Part 4: the "card_blurb" field the owner edits per
covered company for the Rational Compounder page's new company shelf (the
one-line verdict shown on each card in State 1). Same SQLite file/
volume-resolution rule as positions_store.py and watchlist_store.py - see
positions_store.py's own module docstring for why SQLite over a hand-rolled
JSON file (concurrent Streamlit sessions writing at once).

One row per ticker - a card's blurb is a single current string, not a
history log. A missing row means "no admin override saved yet" - callers
(app.py's _rc_card_blurb) fall back to a draft built from the company's own
existing written verdict text (never invented), exactly as the instruction
asks: "PRE-FILL a draft for each from the existing written verdicts...
never invent a view the author hasn't written."

Mega-batch Part 12: a second, sibling table `research_status` - same file,
same SQLite connection/pattern, added here rather than a new module because
this IS "the Card blurbs area" the instruction names as where the new
per-ticker status control lives. One row per ticker holding a `status`
('in_progress' or 'terminated') and a `terminated_reason` (one line, in
the owner's own words - never generated, matching the same "never invent"
rule as the blurb above). A missing row means "still in progress" (the
pre-existing default), so only companies the owner has actively marked
Terminated need a row at all.

AUB.AX and RMD.AX are pre-set to Terminated with an EMPTY reason on first
use (owner context: research on both was deliberately stopped after risks
were found - not worth finalising - so the generic "research in progress"
fallback was actively misrepresenting them). The seed only inserts a row
when one doesn't already exist, so it never clobbers a reason the owner
has since filled in, and only runs once per process (module-level guard)
rather than on every call.

A ticker with a full written verdict (_rc_verdict_text() returns text) is
always treated as complete regardless of any stored status - "companies
with a full written verdict need no status" - so app.py's status lookup
checks that first and only falls back to this table when no verdict text
exists yet.
"""

import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

# Mega-batch Part 12: owner-stated Terminated reason pre-set for these two
# tickers (empty - the owner fills in his own words; never invented here).
# Seeded once per process (see _seed_research_status_presets below).
_PRESET_TERMINATED_TICKERS = ("AUB.AX", "RMD.AX")
_presets_seeded = False


def _conn():
    global _presets_seeded
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS card_blurbs (
            ticker TEXT PRIMARY KEY,
            blurb TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS research_status (
            ticker TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            terminated_reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )"""
    )
    if not _presets_seeded:
        _presets_seeded = True
        _seed_research_status_presets(conn)
    return conn


def _seed_research_status_presets(conn):
    """Insert AUB.AX/RMD.AX as Terminated (empty reason) the first time
    this process ever sees the table, and only if a row doesn't already
    exist for that ticker - so this never overwrites a reason the owner
    has since typed in, and never re-terminates a ticker the owner has
    deliberately switched back to In progress."""
    now = datetime.now(timezone.utc).isoformat()
    for ticker in _PRESET_TERMINATED_TICKERS:
        existing = conn.execute(
            "SELECT 1 FROM research_status WHERE ticker = ?", (ticker,)
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO research_status (ticker, status, terminated_reason, updated_at)
                   VALUES (?, 'terminated', '', ?)""",
                (ticker, now),
            )


def get_blurb(ticker):
    """The admin-saved blurb string for this ticker, or None if nothing has
    been saved yet (caller falls back to the derived draft)."""
    if not ticker:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT blurb FROM card_blurbs WHERE ticker = ?",
            (ticker.strip().upper(),),
        ).fetchone()
    return row[0] if row else None


def set_blurb(ticker, blurb):
    """UPSERT the saved blurb for one ticker. An empty/whitespace-only
    blurb DELETES the row instead of saving a blank string, so "Save" with
    an emptied text box reverts the card to the derived draft rather than
    showing nothing."""
    if not ticker:
        return
    ticker = ticker.strip().upper()
    blurb = (blurb or "").strip()
    with _conn() as conn:
        if not blurb:
            conn.execute("DELETE FROM card_blurbs WHERE ticker = ?", (ticker,))
            return
        conn.execute(
            """INSERT INTO card_blurbs (ticker, blurb, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 blurb = excluded.blurb,
                 updated_at = excluded.updated_at""",
            (ticker, blurb, datetime.now(timezone.utc).isoformat()),
        )


def all_blurbs():
    """{ticker: blurb} for every saved override."""
    with _conn() as conn:
        rows = conn.execute("SELECT ticker, blurb FROM card_blurbs").fetchall()
    return {r[0]: r[1] for r in rows}


def get_research_status(ticker):
    """(status, terminated_reason) for this ticker, or ('in_progress', '')
    if no row has ever been saved (the default). Does NOT know about "a
    full written verdict means complete" - that check lives in app.py
    (_rc_research_status), which is the one place that also has the
    workbook data needed to check for verdict text."""
    if not ticker:
        return "in_progress", ""
    with _conn() as conn:
        row = conn.execute(
            "SELECT status, terminated_reason FROM research_status WHERE ticker = ?",
            (ticker.strip().upper(),),
        ).fetchone()
    if not row:
        return "in_progress", ""
    return row[0], row[1] or ""


def set_research_status(ticker, status, terminated_reason=""):
    """UPSERT the status for one ticker. status must be 'in_progress' or
    'terminated'; terminated_reason is only meaningful (and only shown)
    for 'terminated', but is stored either way so switching back to
    Terminated later doesn't lose a previously-typed reason.

    Owner-review-round fix #1 hardening: a BLANK incoming reason never
    clobbers an existing non-blank one. Before this, calling this with
    terminated_reason="" (e.g. the admin "Save status" button clicked
    while the reason box happened to be empty - a plausible Streamlit
    widget-rerun timing slip, or simply re-saving the status radio
    without re-typing the reason) would silently overwrite a reason the
    owner had carefully typed in on an earlier save, with no
    confirmation and no way to tell afterward that anything was lost.
    Saving a genuinely blank reason still works the FIRST time (nothing
    to lose yet); once a real reason is on file, only a non-blank
    incoming value can replace it - matching set_blurb()'s own existing
    "never silently lose owner-typed text" convention elsewhere in this
    file, just applied at the write path rather than at boot-seed time."""
    if not ticker or status not in ("in_progress", "terminated"):
        return
    ticker = ticker.strip().upper()
    terminated_reason = (terminated_reason or "").strip()
    with _conn() as conn:
        if not terminated_reason:
            existing = conn.execute(
                "SELECT terminated_reason FROM research_status WHERE ticker = ?",
                (ticker,),
            ).fetchone()
            if existing and (existing[0] or "").strip():
                terminated_reason = existing[0]
        conn.execute(
            """INSERT INTO research_status (ticker, status, terminated_reason, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 status = excluded.status,
                 terminated_reason = excluded.terminated_reason,
                 updated_at = excluded.updated_at""",
            (ticker, status, terminated_reason, datetime.now(timezone.utc).isoformat()),
        )


def all_research_statuses():
    """{ticker: (status, terminated_reason)} for every ticker that has a
    saved row (i.e. every ticker the owner has touched via the admin
    control, including the AUB.AX/RMD.AX presets)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ticker, status, terminated_reason FROM research_status"
        ).fetchall()
    return {r[0]: (r[1], r[2] or "") for r in rows}
