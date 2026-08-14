"""
positions_store.py

Author position disclosure - whether the author personally holds, has never
held, or previously held (and exited) each covered company. Rendered on the
Research page so readers can see the author's own skin-in-the-game status
next to the research, independent of the site's data verdicts.

Same SQLite file / volume-resolution rule as watchlist_store.py and
email_auth.py - see watchlist_store.py's module docstring for why SQLite
over a hand-rolled JSON file (concurrent Streamlit sessions writing at
once). Callers never touch SQL directly.

One row per ticker - a company's disclosure is a single current state, not
a history log. status is one of 'holds', 'never', 'closed'.
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
        """CREATE TABLE IF NOT EXISTS author_positions (
            ticker TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            first_purchase TEXT,
            exit_month TEXT,
            entry_approach TEXT,
            avg_price REAL,
            currency TEXT,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


def get_position(ticker):
    """dict for this ticker's stored disclosure, or None if no row exists
    yet (callers treat a missing row as status 'never')."""
    if not ticker:
        return None
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM author_positions WHERE ticker = ?",
            (ticker.strip().upper(),),
        ).fetchone()
    return dict(row) if row else None


def set_position(ticker, status, first_purchase=None, exit_month=None,
                  entry_approach=None, avg_price=None, currency=None):
    """UPSERT the disclosure row for one ticker, stamping updated_at (UTC)."""
    if not ticker or not status:
        return
    with _conn() as conn:
        conn.execute(
            """INSERT INTO author_positions
                 (ticker, status, first_purchase, exit_month, entry_approach,
                  avg_price, currency, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 status = excluded.status,
                 first_purchase = excluded.first_purchase,
                 exit_month = excluded.exit_month,
                 entry_approach = excluded.entry_approach,
                 avg_price = excluded.avg_price,
                 currency = excluded.currency,
                 updated_at = excluded.updated_at""",
            (
                ticker.strip().upper(), status, first_purchase, exit_month,
                entry_approach, avg_price, currency,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def all_positions():
    """{ticker: dict} for every stored disclosure row."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM author_positions").fetchall()
    return {r["ticker"]: dict(r) for r in rows}
