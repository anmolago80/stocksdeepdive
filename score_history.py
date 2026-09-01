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

Nothing here is ever deleted in ordinary operation - a ticker scanned
every night for years accumulates one very small row per day, and
digest/Deep-Dive "days ago" lookups depend on that history actually being
there to look back at. The one exception is delete_bad_price_rows()
below, a Fix 9 (2026-09-01) one-off cleanup helper for rows that were
never valid data to begin with (a NaN price from a broken scan run) -
see that function's own docstring.
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
    # Services batch, Part 1 (metric alerts) / Part 5 (score history chart):
    # widened from long_score/price only to every metric an alert or the
    # history chart can reference - quality/moat/mos_pct/intrinsic_value as
    # numbers, valuation_label/moat_state as their categorical strings.
    # Guarded ALTER TABLE (portfolio_store.py's own precedent) so an
    # existing on-disk DB upgrades in place; rows recorded before this
    # migration simply read back NULL for the new columns, same as any
    # other "no data yet" case this module already handles.
    for _col, _type in (
        ("quality", "REAL"), ("moat", "REAL"), ("mos_pct", "REAL"),
        ("intrinsic_value", "REAL"), ("valuation_label", "TEXT"),
        ("moat_state", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE score_history ADD COLUMN {_col} {_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record(rows, day=None):
    """
    Upsert one row per ticker for `day` (defaults to today, UTC). `rows` is
    an iterable of dicts shaped like nightly_scan's row output - "Ticker",
    "Long Score", "Price", and (Services batch) "Quality", "Moat", "MOS %",
    "Intrinsic Value", "Valuation", "Moat Erosion" are read when present, so
    a caller can pass the exact same row list the overnight scan already
    built without reshaping it first; a caller (e.g. digest_engine's
    lighter-weight row shape) that omits the newer keys just gets NULLs for
    those columns, exactly as before this widening. Re-running the same
    day's scan twice just overwrites that day's rows, so this is safe to
    call more than once for the same day.
    """
    day = day or _today()
    with _conn() as conn:
        for r in rows:
            ticker = (r.get("Ticker") or "").strip().upper()
            if not ticker:
                continue
            conn.execute(
                """INSERT INTO score_history
                     (day, ticker, long_score, price, quality, moat, mos_pct,
                      intrinsic_value, valuation_label, moat_state)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, ticker) DO UPDATE SET
                     long_score = excluded.long_score,
                     price = excluded.price,
                     quality = excluded.quality,
                     moat = excluded.moat,
                     mos_pct = excluded.mos_pct,
                     intrinsic_value = excluded.intrinsic_value,
                     valuation_label = excluded.valuation_label,
                     moat_state = excluded.moat_state""",
                (day, ticker, r.get("Long Score"), r.get("Price"), r.get("Quality"),
                 r.get("Moat"), r.get("MOS %"), r.get("Intrinsic Value"),
                 r.get("Valuation"), r.get("Moat Erosion")),
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
            """SELECT day, long_score, price, quality, moat, mos_pct,
                      intrinsic_value, valuation_label, moat_state
                 FROM score_history
                 WHERE ticker = ? AND day <= ?
                 ORDER BY day DESC LIMIT 1""",
            (ticker.strip().upper(), cutoff),
        ).fetchone()
    return dict(row) if row else None


def latest(ticker):
    """Most recent stored row for `ticker`, or None. Used by alert_engine.py
    as the "previous value" side of crossing-detection (read BEFORE
    tonight's record() call for that ticker lands, so it genuinely reflects
    the last night this ticker was scanned - see alert_engine.
    snapshot_previous_values())."""
    if not ticker:
        return None
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT day, long_score, price, quality, moat, mos_pct,
                      intrinsic_value, valuation_label, moat_state
                 FROM score_history
                 WHERE ticker = ? ORDER BY day DESC LIMIT 1""",
            (ticker.strip().upper(),),
        ).fetchone()
    return dict(row) if row else None


def series(ticker, limit_days=730):
    """Ascending-by-day list of every stored row for `ticker` within the
    last `limit_days` - the Deep Dive score-history chart's (Services batch
    Part 5) one data source. Each item is {"day","long_score","price",
    "quality","moat","mos_pct","intrinsic_value","valuation_label",
    "moat_state"}. No interpolation - a night the ticker wasn't scanned is
    simply a gap in the returned list, and the chart's own caption says so.

    Default widened from 365 to 730 days (~24 months), 2026-09-01, per
    Andrew's request - this module never deletes rows (see the module
    docstring), so the only thing that was ever cutting the chart off at
    a year was this query window, not the data. Raising it doesn't
    retroactively create history that was never recorded - a ticker only
    has as many days on the chart as nights it's actually been scanned
    since score_history.record() started being called (Services batch
    Part 1/5, 2026-08-31) - it just means the window stops being the
    limiting factor once two years of real nightly data exists."""
    if not ticker:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=limit_days)).strftime("%Y-%m-%d")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT day, long_score, price, quality, moat, mos_pct,
                      intrinsic_value, valuation_label, moat_state
                 FROM score_history
                 WHERE ticker = ? AND day >= ?
                 ORDER BY day ASC""",
            (ticker.strip().upper(), cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def before_date(ticker, target_day):
    """The closest stored row for `ticker` strictly BEFORE `target_day`
    ('YYYY-MM-DD') - Services batch Part 4's "before" half of a results-
    day before/after comparison, where the caller already knows the exact
    calendar day (a report date) rather than a relative "days ago" offset
    (see get() above for that case). Strictly before (not at-or-before)
    since the report date itself is when "after" starts."""
    if not ticker or not target_day:
        return None
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT day, long_score, price, quality, moat, mos_pct,
                      intrinsic_value, valuation_label, moat_state
                 FROM score_history
                 WHERE ticker = ? AND day < ?
                 ORDER BY day DESC LIMIT 1""",
            (ticker.strip().upper(), target_day),
        ).fetchone()
    return dict(row) if row else None


def delete_bad_price_rows(day):
    """Deletes score_history rows for `day` ('YYYY-MM-DD') whose price is
    NULL or NaN. Fix 9 (2026-09-01): used by nightly_scan.
    cleanup_fix9_nan_data(), the one-off boot-time cleanup step, to
    remove the ~433 garbage points the 31 Aug 20:00 UTC run recorded for
    every ASX 200/ASX 300 ticker that got a NaN placeholder price that
    night (see that function's docstring, and nightly_scan.
    analyze_ticker_lite()'s, for the root cause).

    This table has no universe column, but scoping the delete to "this
    day AND a NaN/NULL price" already isolates exactly the bad rows on
    its own: every other universe scanned that same day (S&P 500) got a
    real price, so nothing but the actual broken rows can match both
    conditions at once - no universe lookup needed.

    Uses the standard SQLite idiom for "is NaN" - `price != price` -
    since NaN is the only float value that never equals itself; Python's
    math.isnan() can't run inside a SQL WHERE clause. Returns the number
    of rows deleted. Not exposed anywhere else - see the module
    docstring for why this is a deliberate, narrow exception to "nothing
    here is ever deleted"."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM score_history WHERE day = ? AND (price IS NULL OR price != price)",
            (day,),
        )
        return cur.rowcount


def all_tracked_tickers():
    """Every distinct ticker with at least one recorded score_history row
    - i.e. "every ticker ever scanned overnight". Services batch Part 4's
    weekly earnings-calendar refresh watches this list unioned with
    follow_store.all_followed_tickers()."""
    with _conn() as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM score_history").fetchall()
    return [r[0] for r in rows]


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
