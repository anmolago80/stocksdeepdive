"""
watchlist_store.py

Per-user watchlists for signed-in visitors, stored in a small SQLite file.

WHERE THE FILE LIVES: the same resolution rule as every other persisted
file in this app (see build_compounder_data._cp_data_dir) - the attached
Railway Volume when one exists (RAILWAY_VOLUME_MOUNT_PATH is auto-set by
Railway the moment a volume is mounted), falling back to this directory
for local runs. Without a volume, watchlists still work but reset on each
redeploy - attach the volume before promoting this feature loudly.

WHY SQLITE, NOT A JSON FILE: multiple Streamlit sessions (visitors) write
concurrently; SQLite serialises writers safely out of the box, a hand-rolled
JSON read-modify-write cycle does not.

Identity is the signed-in Google email from paywall_engine - this module
never sees or stores anything else about the user.
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
        """CREATE TABLE IF NOT EXISTS watchlist (
            email TEXT NOT NULL,
            ticker TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (email, ticker)
        )"""
    )
    return conn


def get_watchlist(email):
    """Sorted ticker list for one user (empty list if none / no email)."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ticker FROM watchlist WHERE email = ? ORDER BY ticker", (email,)
        ).fetchall()
    return [r[0] for r in rows]


def add(email, ticker):
    if not email or not ticker:
        return
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (email, ticker, added_at) VALUES (?, ?, ?)",
            (email, ticker.upper(), datetime.now(timezone.utc).isoformat()),
        )


def remove(email, ticker):
    if not email or not ticker:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM watchlist WHERE email = ? AND ticker = ?", (email, ticker.upper())
        )


def contains(email, ticker):
    if not email or not ticker:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM watchlist WHERE email = ? AND ticker = ?", (email, ticker.upper())
        ).fetchone()
    return row is not None


def all_users():
    """[(email, [tickers...])] for every user with a non-empty watchlist -
    the weekly digest iterates this."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT email, ticker FROM watchlist ORDER BY email, ticker"
        ).fetchall()
    out = {}
    for email, ticker in rows:
        out.setdefault(email, []).append(ticker)
    return list(out.items())
