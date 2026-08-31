"""
score_history.py

Daily snapshot of each ticker's Long Score and price - one row per
(day, ticker) - written once per night by the overnight scan job
(nightly_scan.py) and read back by the Deep Dive page (a "vs 30 days ago"
caption) and the weekly digest (a movement-vs-last-week column).

Same SQLite file / volume-resolution rule as positions_store.py,
watchlist_store.py, email_auth.py - see watchlist_store.py's module
docstring for why SQLite over a hand-rolled JSON file (concurrent Streamlit
sessions/cron jobs writing at once). Callers never touch SQL directly.

Nothing here is ever deleted - a ticker scanned every night for years
accumulates one very small row per day, and digest/Deep-Dive "days ago"
lookups depend on that history actually being there to look back at.
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # Audit fix 2.9: see metrics_store.py's identical comment - same
    # shared stocksdeepdive.db file, same WAL rationale.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS score_history (
            day TEXT NOT NULL,
            ticker TEXT NOT NULL,
            long_score REAL,
            price REAL,
            PRIMARY KEY (day, ticker)
        )"""
    )
    return conn


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record(rows, day=None):
    """
    Upsert one row per ticker for `day` (defaults to today, UTC). `rows` is
    an iterable of dicts shaped like nightly_scan's row output - only
    "Ticker", "Long Score" and "Price" are read, so a caller can pass the
    exact same row list the overnight scan already built without reshaping
    it first. Re-running the same day's scan twice just overwrites that
    day's rows, so this is safe to call more than once for the same day.
    """
    day = day or _today()
    with _conn() as conn:
        for r in rows:
            ticker = (r.get("Ticker") or "").strip().upper()
            if not ticker:
                continue
            conn.execute(
                """INSERT INTO score_history (day, ticker, long_score, price)
                     VALUES (?, ?, ?, ?)
                   ON CONFLICT(day, ticker) DO UPDATE SET
                     long_score = excluded.long_score,
                     price = excluded.price""",
                (day, ticker, r.get("Long Score"), r.get("Price")),
            )


def get(ticker, days_ago):
    """
    The closest stored row for `ticker` at or before `days_ago` days back
    from today (UTC) - not an exact-day match, since the overnight scan
    doesn't necessarily run every single night for every universe, and a
    "closest available" comparison is far more useful than silently
    returning nothing just because that exact calendar day has no row.
    Returns {"day", "long_score", "price"} or None if this ticker has no
    stored history at or before that point at all.
    """
    if not ticker:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT day, long_score, price FROM score_history
                 WHERE ticker = ? AND day <= ?
                 ORDER BY day DESC LIMIT 1""",
            (ticker.strip().upper(), cutoff),
        ).fetchone()
    return dict(row) if row else None


def latest(ticker):
    """Most recent stored row for `ticker`, or None."""
    if not ticker:
        return None
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT day, long_score, price FROM score_history
                 WHERE ticker = ? ORDER BY day DESC LIMIT 1""",
            (ticker.strip().upper(),),
        ).fetchone()
    return dict(row) if row else None


def tracked_summary(min_days=60, limit=500):
    """AI-readiness roadmap Phase 4 (citation helpers): one row per ticker
    with enough recorded history to be worth publishing - its FIRST and
    most recent recorded (day, long_score, price), spanning at least
    min_days. Purely descriptive of what the nightly scan actually wrote
    down, on the actual dates it wrote it - see track_record_render.py's
    own docstring for why this is deliberately not framed as investment
    performance.

    Returns a list of {"ticker", "first_day", "first_score", "first_price",
    "last_day", "last_score", "last_price", "days_span"} sorted by ticker.
    One query (SQLite window functions - available since 3.25, comfortably
    older than anything this app runs on) rather than an N+1 loop issuing
    a separate earliest/latest lookup per ticker across what is, in
    production, several hundred tracked tickers.
    """
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ticker, day, long_score, price, rn_asc, rn_desc FROM (
                SELECT ticker, day, long_score, price,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY day ASC) AS rn_asc,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY day DESC) AS rn_desc
                FROM score_history
            ) WHERE rn_asc = 1 OR rn_desc = 1
            """
        ).fetchall()

    by_ticker = {}
    for r in rows:
        d = by_ticker.setdefault(r["ticker"], {})
        if r["rn_asc"] == 1:
            d["first_day"] = r["day"]
            d["first_score"] = r["long_score"]
            d["first_price"] = r["price"]
        if r["rn_desc"] == 1:
            d["last_day"] = r["day"]
            d["last_score"] = r["long_score"]
            d["last_price"] = r["price"]

    out = []
    for ticker, d in by_ticker.items():
        if "first_day" not in d or "last_day" not in d:
            continue
        if d["first_day"] == d["last_day"]:
            continue  # only one day on record - nothing "over time" to show
        try:
            span = (datetime.strptime(d["last_day"], "%Y-%m-%d")
                    - datetime.strptime(d["first_day"], "%Y-%m-%d")).days
        except ValueError:
            continue
        if span < min_days:
            continue
        out.append({"ticker": ticker, "days_span": span, **d})

    out.sort(key=lambda x: x["ticker"])
    return out[:limit]
