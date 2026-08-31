"""
results_store.py

Services batch, Part 4: results-day re-analysis - pure storage. Two
tables:

  earnings_watch  - one row per ticker this site tracks an earnings
                     calendar for (every ticker that's ever been scanned
                     overnight, unioned with every followed ticker - see
                     results_engine.py's own docstring for the weekly
                     refresh that fills this table). Holds the most
                     recently known REPORTED date, the next EXPECTED
                     date, and the last several quarters' "Reported EPS"
                     values (used to compute a genuine before/after EPS
                     TTM without a second network call at re-analysis
                     time) - the raw scraped earnings-calendar table
                     itself is never stored, only this small summary.

  results_events   - one row per (ticker, report_date) - the Deep Dive
                     "before/after" card's one data source. Created on
                     the first re-analysis pass (the day after a report),
                     updated IN PLACE by the second pass three days later
                     (to catch statements Yahoo ingested late) rather
                     than creating a second row, so a ticker always has
                     at most one open card per report.

Same SQLite file / volume-resolution rule as every other *_store module in
this codebase (see follow_store.py's own docstring for why SQLite over a
hand-rolled file - concurrent Streamlit sessions/cron jobs writing at
once). Callers never touch SQL directly.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS earnings_watch (
            ticker TEXT PRIMARY KEY,
            last_report_date TEXT,
            next_report_date TEXT,
            eps_history_json TEXT,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS results_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            report_date TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            what_moved_json TEXT,
            stale INTEGER NOT NULL DEFAULT 0,
            notified INTEGER NOT NULL DEFAULT 0,
            pass_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(ticker, report_date)
        )"""
    )
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------
# earnings_watch
# --------------------------------------------------------------------

def upsert_earnings_dates(ticker, last_report_date, next_report_date, eps_history=None):
    """last_report_date/next_report_date: 'YYYY-MM-DD' strings or None.
    A field left None this call keeps whatever value the row already had
    (rather than being blanked out) - a scrape that only returned future
    dates this time shouldn't erase a last_report_date learned earlier,
    and vice versa. eps_history (a list of {"date","reported_eps"},
    already sorted newest-first - see results_engine._parse_earnings_
    dates_df) always overwrites, since it's re-derived fresh from the
    same scrape every time and there's nothing to preserve."""
    if not ticker:
        return
    ticker = ticker.strip().upper()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT last_report_date, next_report_date FROM earnings_watch WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        if last_report_date is None and existing:
            last_report_date = existing[0]
        if next_report_date is None and existing:
            next_report_date = existing[1]
        conn.execute(
            "INSERT INTO earnings_watch "
            "(ticker, last_report_date, next_report_date, eps_history_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET "
            "last_report_date = excluded.last_report_date, "
            "next_report_date = excluded.next_report_date, "
            "eps_history_json = excluded.eps_history_json, "
            "updated_at = excluded.updated_at",
            (ticker, last_report_date, next_report_date,
             json.dumps(eps_history or []), _now()),
        )


def get_earnings_watch(ticker):
    if not ticker:
        return None
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ticker, last_report_date, next_report_date, eps_history_json, updated_at "
            "FROM earnings_watch WHERE ticker = ?",
            (ticker.strip().upper(),),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["eps_history"] = json.loads(d.pop("eps_history_json") or "[]")
    return d


def all_watched():
    """Every watched ticker's row (same shape as get_earnings_watch) -
    results_engine.check_results_day()'s nightly scan of "who might have
    reported 1 or 3 days ago"."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, last_report_date, next_report_date, eps_history_json, updated_at "
            "FROM earnings_watch"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["eps_history"] = json.loads(d.pop("eps_history_json") or "[]")
        out.append(d)
    return out


def stale_watch_tickers(tickers, max_age_days=6):
    """Subset of `tickers` whose earnings_watch row is missing or older
    than max_age_days - the weekly refresh's own "due" filter, same
    age-based-staleness idea as insider_store.should_refetch (Part 2),
    just against a longer TTL since an earnings calendar changes at most
    a few times a year per ticker. Order is not guaranteed to match
    `tickers`' own order (a never-watched ticker sorts first via the set
    difference below) - callers that want a stable/prioritised order
    should sort the result themselves."""
    if not tickers:
        return []
    wanted = {t.strip().upper() for t in tickers if t and t.strip()}
    if not wanted:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ticker, updated_at FROM earnings_watch WHERE ticker IN ({})".format(
                ",".join("?" for _ in wanted)
            ),
            list(wanted),
        ).fetchall()
    fresh = {r[0] for r in rows if r[1] and r[1] > cutoff}
    return sorted(wanted - fresh)


# --------------------------------------------------------------------
# results_events
# --------------------------------------------------------------------

def _row_to_dict(row):
    d = dict(row)
    d["before"] = json.loads(d.pop("before_json") or "null")
    d["after"] = json.loads(d.pop("after_json") or "null")
    d["what_moved"] = json.loads(d.pop("what_moved_json") or "[]")
    d["stale"] = bool(d["stale"])
    d["notified"] = bool(d["notified"])
    return d


def get_event(ticker, report_date):
    if not ticker or not report_date:
        return None
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM results_events WHERE ticker = ? AND report_date = ?",
            (ticker.strip().upper(), report_date),
        ).fetchone()
    return _row_to_dict(row) if row else None


def upsert_event(ticker, report_date, before, after, what_moved, stale=False):
    """Insert on the first (day+1) pass; update the SAME row's "after"/
    "what_moved"/"stale" in place on the second (day+3) pass - "before"
    is never touched again once set, since it's a fact about the world
    (the last score BEFORE the report) that a later pass can't change.
    Returns (event_id, is_new)."""
    if not ticker or not report_date:
        return None, False
    ticker = ticker.strip().upper()
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM results_events WHERE ticker = ? AND report_date = ?",
            (ticker, report_date),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE results_events SET after_json = ?, what_moved_json = ?, "
                "stale = ?, pass_count = pass_count + 1, updated_at = ? WHERE id = ?",
                (json.dumps(after), json.dumps(what_moved), int(bool(stale)), now, existing[0]),
            )
            return existing[0], False
        cur = conn.execute(
            "INSERT INTO results_events (ticker, report_date, before_json, after_json, "
            "what_moved_json, stale, notified, pass_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?)",
            (ticker, report_date, json.dumps(before), json.dumps(after),
             json.dumps(what_moved), int(bool(stale)), now, now),
        )
        return cur.lastrowid, True


def mark_notified(event_id):
    if not event_id:
        return
    with _conn() as conn:
        conn.execute("UPDATE results_events SET notified = 1 WHERE id = ?", (event_id,))


def recent_event_for_ticker(ticker, within_days=14):
    """Most recent results event for `ticker` reported within the last
    `within_days` days - the Deep Dive before/after card's one data
    source. None if nothing recent enough (or nothing at all)."""
    if not ticker:
        return None
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=within_days)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM results_events WHERE ticker = ? AND report_date >= ? "
            "ORDER BY report_date DESC LIMIT 1",
            (ticker.strip().upper(), cutoff),
        ).fetchone()
    return _row_to_dict(row) if row else None


def events_this_week():
    """One row per distinct ticker with a results event reported in the
    last 7 days, most-recently-reported first - the home page's optional
    "Reported this week" strip (skipped entirely by the caller when this
    returns fewer than 2 entries, per Part 4's own spec)."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM results_events WHERE report_date >= ? "
            "ORDER BY report_date DESC, ticker ASC",
            (cutoff,),
        ).fetchall()
    seen = set()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if d["ticker"] in seen:
            continue
        seen.add(d["ticker"])
        out.append(d)
    return out
