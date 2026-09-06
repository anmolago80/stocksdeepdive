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
"""

import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS card_blurbs (
            ticker TEXT PRIMARY KEY,
            blurb TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


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
