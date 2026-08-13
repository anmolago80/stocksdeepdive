"""
follow_store.py

"Follow this company" email capture for the Research and Deep Dive pages -
a lighter commitment than a full watchlist: a reader leaves an email to be
notified when the hand-built research changes. Ticker "*" means "email me
about ANY new/updated research", not just one company.

Same SQLite file / volume-resolution rule as watchlist_store.py and
email_auth.py - see watchlist_store.py's module docstring for why SQLite
over a hand-rolled JSON file (concurrent Streamlit sessions writing at
once). Callers never touch SQL directly.

Identity is the signed-in email from paywall_engine / email_auth - this
module never sees or stores anything else about a follower.
"""

import os
import sqlite3
from datetime import datetime, timezone

# Sentinel ticker meaning "every new/updated company", used by the follow
# UI's "all research" option (if/when one is added) and by announce_engine.
ALL_TICKERS = "*"


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS followers (
            email TEXT NOT NULL,
            ticker TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (email, ticker)
        )"""
    )
    return conn


def follow(email, ticker):
    if not email or not ticker:
        return
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO followers (email, ticker, created_at) "
            "VALUES (?, ?, ?)",
            (email.strip().lower(), ticker.strip().upper(),
             datetime.now(timezone.utc).isoformat()),
        )


def unfollow(email, ticker):
    if not email or not ticker:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM followers WHERE email = ? AND ticker = ?",
            (email.strip().lower(), ticker.strip().upper()),
        )


def is_following(email, ticker):
    if not email or not ticker:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM followers WHERE email = ? AND ticker = ?",
            (email.strip().lower(), ticker.strip().upper()),
        ).fetchone()
    return row is not None


def followers_of(ticker):
    """Emails following this exact ticker (does NOT expand the "*"
    wildcard - callers that need "everyone" combine this with the ALL_TICKERS
    list themselves, e.g. announce_engine.announce_rebuild)."""
    if not ticker:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT email FROM followers WHERE ticker = ? ORDER BY email",
            (ticker.strip().upper(),),
        ).fetchall()
    return [r[0] for r in rows]


def all_followers():
    """Distinct emails following anything at all (any ticker, including
    the "*" wildcard)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT email FROM followers ORDER BY email"
        ).fetchall()
    return [r[0] for r in rows]


def follow_count():
    """Aggregate stats only: {'emails': distinct follower count,
    'rows': total follow rows (a follower of several tickers counts once
    per ticker here)}."""
    with _conn() as conn:
        emails = conn.execute(
            "SELECT COUNT(DISTINCT email) FROM followers"
        ).fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM followers").fetchone()[0]
    return {"emails": emails, "rows": rows}
