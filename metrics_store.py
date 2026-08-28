"""
metrics_store.py

First-party, aggregate-only page-view counting - no third-party trackers,
no cookies of its own, no per-visitor identity stored. One row per
(day, page, ticker, src) with a running view count; app.py calls bump()
once per page render and the admin Stats popover reads stats() back.

Same SQLite file / volume-resolution rule as watchlist_store.py and
email_auth.py. Callers never touch SQL directly.

WHY (day, page, ticker, src) AND NOT A ROW PER VISIT: a UPSERT-and-
increment keeps the table tiny (one row per distinct combination per day,
not one row per pageview forever) while still answering "how many views
did this page/ticker/src get" - exactly the aggregate questions the admin
Stats popover and privacy policy promise, and nothing more.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # Audit fix 2.9: default SQLite rollback-journal mode takes a whole-
    # file write lock, so heavy concurrent write load (e.g. during the
    # nightly scan writing score_history alongside a visitor's page-view
    # bump) can surface as an uncaught "database is locked" error instead
    # of degrading silently like everything else in this app. WAL lets
    # readers and a writer proceed concurrently and is a property of the
    # DB file itself (persists once set) - set on every connect since it's
    # a cheap no-op once already enabled, and any of the several modules
    # sharing this same stocksdeepdive.db file could be the first to open
    # it in a fresh process.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS page_views (
            day TEXT NOT NULL,
            page TEXT NOT NULL,
            ticker TEXT NOT NULL,
            src TEXT NOT NULL,
            views INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, page, ticker, src)
        )"""
    )
    return conn


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def bump(page, ticker=None, src=None):
    """UPSERT views+1 for today (UTC) for this (page, ticker, src). ticker
    and src are stored as '' when absent, so grouping/joins never have to
    deal with NULL. Callers wrap this in try/except - analytics must never
    break a page render."""
    if not page:
        return
    day = _today()
    ticker = (ticker or "").strip().upper()
    src = (src or "").strip()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO page_views (day, page, ticker, src, views)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(day, page, ticker, src) DO UPDATE SET
                 views = views + 1""",
            (day, page, ticker, src),
        )


def stats(days=30):
    """AGGREGATE view counts only - no identities are ever stored here in
    the first place. Returns:
      {'total_7d', 'total_30d', 'by_page': [(page, views), ...] desc,
       'by_src': [(src, views), ...] desc (excludes ''),
       'by_ticker': [(ticker, views), ...] desc (research page only,
       excludes ''), 'daily': [(day, views), ...] asc}
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    since7 = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    with _conn() as conn:
        total_30d = conn.execute(
            "SELECT COALESCE(SUM(views), 0) FROM page_views WHERE day >= ?",
            (since,),
        ).fetchone()[0]
        total_7d = conn.execute(
            "SELECT COALESCE(SUM(views), 0) FROM page_views WHERE day >= ?",
            (since7,),
        ).fetchone()[0]
        by_page = conn.execute(
            """SELECT page, SUM(views) v FROM page_views WHERE day >= ?
               GROUP BY page ORDER BY v DESC""",
            (since,),
        ).fetchall()
        by_src = conn.execute(
            """SELECT src, SUM(views) v FROM page_views
               WHERE day >= ? AND src != '' GROUP BY src ORDER BY v DESC""",
            (since,),
        ).fetchall()
        by_ticker = conn.execute(
            """SELECT ticker, SUM(views) v FROM page_views
               WHERE day >= ? AND page = 'research' AND ticker != ''
               GROUP BY ticker ORDER BY v DESC""",
            (since,),
        ).fetchall()
        daily = conn.execute(
            """SELECT day, SUM(views) v FROM page_views WHERE day >= ?
               GROUP BY day ORDER BY day ASC""",
            (since,),
        ).fetchall()
    return {
        "total_7d": total_7d,
        "total_30d": total_30d,
        "by_page": by_page,
        "by_src": by_src,
        "by_ticker": by_ticker,
        "daily": daily,
    }
