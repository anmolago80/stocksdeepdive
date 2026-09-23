"""
quote_snapshot_store.py

Trading Cost tab, Commit 1: one real bid/ask quote per ticker per day,
captured while the market is genuinely open (see quote_recorder.py for
the job that calls record_snapshot() below) - never Yahoo's after-hours
snapshot, which can be junk (OCL.AX after the close: bid A$5.75 / ask
A$6.45, a 12% gap - the owner-reported example this whole feature exists
to fix). Commit 2's Corwin-Schultz estimator fills every OTHER day from
daily high/low; this table is only ever the small number of real,
verified points that estimator is checked against.

Same SQLite file / volume-resolution rule as score_history.py/
snapshot_store.py - see watchlist_store.py's module docstring for why
SQLite over a hand-rolled JSON file. Callers never touch SQL directly.

Two tables:
  quote_snapshots      - one row per (ticker, snap_date) - the actual
                          captured quote. PRIMARY KEY (ticker, snap_date)
                          doubles as the unique index the spec asks for,
                          same convention as score_history.py's own
                          PRIMARY KEY (day, ticker).
  quote_snapshot_runs  - one row per (run_date, market) - a run's own
                          summary counts (captured + each rejection
                          reason). Needed because "reject junk rather
                          than storing it" (quote_recorder.py's own
                          rule) means a rejected quote leaves NO row in
                          quote_snapshots - without this second table
                          the Admin Dashboard's "rejection counts by
                          reason" panel would have nothing to read.

Retention: quote_snapshots keeps QUOTE_RETENTION_DAYS (400) days -
long enough for Commit 2's 30-day window plus real headroom, short
enough that a small daily table never becomes a Volume problem.
quote_snapshot_runs is pruned on the same cadence, kept a little
longer (RUN_SUMMARY_RETENTION_DAYS, 400 as well - it's one tiny row
per market per day, not worth a shorter/separate window).

Holiday-gap follow-up (before Commit 1's first push): three more
rejection reasons (rejected_market_not_regular/rejected_no_volume/
rejected_frozen_quote - see quote_recorder.py's own module docstring)
close the one gap the original four rules didn't cover - a market
holiday's Yahoo response can look superficially like a valid quote.
latest_snapshot() below is the one new read this needed: the frozen-
quote check has to know the PREVIOUS stored snapshot to compare
against.
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

QUOTE_RETENTION_DAYS = 400
RUN_SUMMARY_RETENTION_DAYS = 400

# Exact match to quote_recorder.py's REJECT_* reason constants - kept as
# plain column names (this codebase's convention, e.g. admin_metrics_
# store.py's typed counter columns) rather than a JSON blob, since the
# reason set is small and fixed. First four are the spec's original
# rejection rules; the last three are the holiday-gap follow-up
# (market-state-not-REGULAR, no-volume fallback, frozen-vs-previous
# quote - see quote_recorder.py's own module docstring).
_REJECTION_COLUMNS = (
    "rejected_missing_or_nonpositive",
    "rejected_ask_le_bid",
    "rejected_wide_spread",
    "rejected_price_off_mid",
    "rejected_market_not_regular",
    "rejected_no_volume",
    "rejected_frozen_quote",
)


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS quote_snapshots (
            ticker TEXT NOT NULL,
            snap_date TEXT NOT NULL,
            snap_at_utc TEXT NOT NULL,
            bid REAL,
            ask REAL,
            bid_size REAL,
            ask_size REAL,
            last_price REAL,
            currency TEXT,
            source TEXT,
            PRIMARY KEY (ticker, snap_date)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS quote_snapshot_runs (
            run_date TEXT NOT NULL,
            market TEXT NOT NULL,
            captured_count INTEGER,
            rejected_missing_or_nonpositive INTEGER,
            rejected_ask_le_bid INTEGER,
            rejected_wide_spread INTEGER,
            rejected_price_off_mid INTEGER,
            rejected_market_not_regular INTEGER,
            rejected_no_volume INTEGER,
            rejected_frozen_quote INTEGER,
            ran_at_utc TEXT,
            PRIMARY KEY (run_date, market)
        )"""
    )
    # Holiday-gap follow-up: guarded ALTER TABLE (score_history.py's own
    # precedent) so an on-disk DB created by Commit 1's original (four-
    # column) CREATE TABLE upgrades in place - a pre-existing row simply
    # reads back NULL/0 for these three columns, same as any other "no
    # data yet" case this module already handles.
    for _col in ("rejected_market_not_regular", "rejected_no_volume", "rejected_frozen_quote"):
        try:
            conn.execute(f"ALTER TABLE quote_snapshot_runs ADD COLUMN {_col} INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def record_snapshot(ticker, snap_date, snap_at_utc, bid, ask, bid_size,
                     ask_size, last_price, currency, source):
    """Upsert one (ticker, snap_date) row - same ON CONFLICT DO UPDATE
    shape as score_history.record()/snapshot_store's own upserts. Called
    only for a quote that already passed quote_recorder.py's junk
    rejection - this function itself does no validation, it just stores
    what it's given."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO quote_snapshots
                 (ticker, snap_date, snap_at_utc, bid, ask, bid_size,
                  ask_size, last_price, currency, source)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker, snap_date) DO UPDATE SET
                 snap_at_utc = excluded.snap_at_utc,
                 bid = excluded.bid,
                 ask = excluded.ask,
                 bid_size = excluded.bid_size,
                 ask_size = excluded.ask_size,
                 last_price = excluded.last_price,
                 currency = excluded.currency,
                 source = excluded.source""",
            (ticker, snap_date, snap_at_utc, bid, ask, bid_size, ask_size,
             last_price, currency, source),
        )


def record_run_summary(run_date, market, captured_count, rejection_counts, ran_at_utc):
    """Upsert one (run_date, market) summary row. `rejection_counts` is a
    dict keyed by the same reason strings as _REJECTION_COLUMNS (missing
    keys default to 0) - quote_recorder.py's REJECT_* constants are
    exactly these column names, so callers never need a translation
    table. Retried runs (the scheduler's own _DAILY_JOB_RETRY_CAP) simply
    overwrite the same row via ON CONFLICT, so this always reflects the
    LAST attempt for that day/market, not a sum across attempts."""
    cols = {c: int(rejection_counts.get(c) or 0) for c in _REJECTION_COLUMNS}
    with _conn() as conn:
        conn.execute(
            """INSERT INTO quote_snapshot_runs
                 (run_date, market, captured_count,
                  rejected_missing_or_nonpositive, rejected_ask_le_bid,
                  rejected_wide_spread, rejected_price_off_mid,
                  rejected_market_not_regular, rejected_no_volume,
                  rejected_frozen_quote, ran_at_utc)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_date, market) DO UPDATE SET
                 captured_count = excluded.captured_count,
                 rejected_missing_or_nonpositive = excluded.rejected_missing_or_nonpositive,
                 rejected_ask_le_bid = excluded.rejected_ask_le_bid,
                 rejected_wide_spread = excluded.rejected_wide_spread,
                 rejected_price_off_mid = excluded.rejected_price_off_mid,
                 rejected_market_not_regular = excluded.rejected_market_not_regular,
                 rejected_no_volume = excluded.rejected_no_volume,
                 rejected_frozen_quote = excluded.rejected_frozen_quote,
                 ran_at_utc = excluded.ran_at_utc""",
            (run_date, market, int(captured_count or 0),
             cols["rejected_missing_or_nonpositive"], cols["rejected_ask_le_bid"],
             cols["rejected_wide_spread"], cols["rejected_price_off_mid"],
             cols["rejected_market_not_regular"], cols["rejected_no_volume"],
             cols["rejected_frozen_quote"], ran_at_utc),
        )


def _day_n_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def rows_per_day(days=14):
    """[{"snap_date", "count"}, ...] for the last `days` calendar days,
    most recent first - read straight off quote_snapshots itself (not
    quote_snapshot_runs' cached captured_count) so this is always the
    literal row count on disk, never a summary value that could drift
    from it. Never raises - returns [] on any read error."""
    cutoff = _day_n_ago(days - 1)
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT snap_date, COUNT(*) FROM quote_snapshots "
                "WHERE snap_date >= ? GROUP BY snap_date ORDER BY snap_date DESC",
                (cutoff,),
            ).fetchall()
        return [{"snap_date": r[0], "count": r[1]} for r in rows]
    except Exception:
        return []


def rejection_counts(days=14):
    """{"rejected_missing_or_nonpositive": N, ...} summed across every
    market's run over the last `days` days - the Admin Dashboard's
    "rejection counts by reason" panel. Never raises - returns all-zero
    dict on any read error."""
    zero = {c: 0 for c in _REJECTION_COLUMNS}
    cutoff = _day_n_ago(days - 1)
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT SUM(rejected_missing_or_nonpositive), SUM(rejected_ask_le_bid), "
                "SUM(rejected_wide_spread), SUM(rejected_price_off_mid), "
                "SUM(rejected_market_not_regular), SUM(rejected_no_volume), "
                "SUM(rejected_frozen_quote) "
                "FROM quote_snapshot_runs WHERE run_date >= ?",
                (cutoff,),
            ).fetchone()
        if row is None:
            return zero
        return {c: int(v or 0) for c, v in zip(_REJECTION_COLUMNS, row)}
    except Exception:
        return zero


def latest_snapshot(ticker):
    """{"bid","ask","last_price"} for the most recently stored snapshot
    of `ticker` (any date - not necessarily "yesterday", if a day was
    skipped), or None if none exists yet. Holiday-gap follow-up: quote_
    recorder.py's belt-and-braces frozen-quote check compares today's
    raw fetch against this before deciding to store it. Never raises -
    returns None on any read error."""
    try:
        with _conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT bid, ask, last_price FROM quote_snapshots "
                "WHERE ticker = ? ORDER BY snap_date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def recent_rows(limit=20):
    """[{"ticker","snap_date","snap_at_utc","bid","ask","bid_size",
    "ask_size","last_price","currency","source"}, ...] - the Admin
    Dashboard's "20 most recent rows" panel, newest first. Never raises
    - returns [] on any read error."""
    try:
        with _conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ticker, snap_date, snap_at_utc, bid, ask, bid_size, "
                "ask_size, last_price, currency, source FROM quote_snapshots "
                "ORDER BY snap_at_utc DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def prune_old(log=print):
    """Deletes quote_snapshots/quote_snapshot_runs rows older than their
    own retention window - same shape as admin_metrics_store.py's
    prune_old_* functions (cutoff + DELETE ... WHERE date < ?, own try/
    except, logs the count, never raises). Called from scheduler_engine.
    _run_volume_check on the existing nightly retention-prune slot."""
    n_snapshots = 0
    try:
        cutoff = _day_n_ago(QUOTE_RETENTION_DAYS - 1)
        with _conn() as conn:
            cur = conn.execute("DELETE FROM quote_snapshots WHERE snap_date < ?", (cutoff,))
            n_snapshots = max(cur.rowcount, 0)
        if n_snapshots:
            log(f"[quote_snapshot_store] pruned {n_snapshots} old quote snapshot row(s)")
    except Exception as e:
        log(f"[quote_snapshot_store] quote snapshot prune failed: {e}")
    try:
        cutoff = _day_n_ago(RUN_SUMMARY_RETENTION_DAYS - 1)
        with _conn() as conn:
            cur = conn.execute("DELETE FROM quote_snapshot_runs WHERE run_date < ?", (cutoff,))
            n_runs = max(cur.rowcount, 0)
        if n_runs:
            log(f"[quote_snapshot_store] pruned {n_runs} old run-summary row(s)")
    except Exception as e:
        log(f"[quote_snapshot_store] run-summary prune failed: {e}")
    return n_snapshots
