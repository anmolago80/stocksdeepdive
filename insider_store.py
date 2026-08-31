"""
insider_store.py

Services batch, Part 2: insider dealings & buybacks. Pure storage module
(no network) for director/insider transactions and buyback status pulled
by insider_engine.py from ASX announcements and SEC EDGAR Form 4s - same
_data_dir()/DB_PATH/_conn() convention as every other store module (see
follow_store.py's docstring).

  insider_filings   - one row per director/insider transaction notice
                       (ASX Appendix 3X/3Y/3Z, or a US Form 4). Deduplicated
                       on (ticker, source, link) so a re-fetch of the same
                       announcement/filing never doubles up.
  insider_fetch_log - last-refreshed-at per ticker, the same TTL-cache
                       shape portfolio_news_engine.py's news_fetch_log
                       uses (REFETCH_STALE_HOURS there = 6; this module's
                       own 24h constant lives in insider_engine.py per the
                       Part 2 spec).
  buyback_summary   - one factual status line per ticker (ASX: latest
                       buy-back notice figures; US: last-FY repurchases
                       from the cash-flow statement) - a single row, not a
                       history, since the Deep Dive panel only ever shows
                       the current status.

Every function does exactly one focused SQL statement; ticker is always
.strip().upper(), same convention as every other store module.
"""

import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS insider_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            filing_type TEXT NOT NULL,
            filed_at TEXT,
            person TEXT,
            action TEXT,
            quantity REAL,
            price REAL,
            link TEXT,
            raw_title TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(ticker, source, link)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS insider_fetch_log (
            ticker TEXT PRIMARY KEY,
            last_fetched_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS buyback_summary (
            ticker TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            amount REAL,
            currency TEXT,
            filed_at TEXT,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------
# Filings
# --------------------------------------------------------------------

def add_filing(ticker, source, filing_type, filed_at=None, person=None,
               action=None, quantity=None, price=None, link=None, raw_title=None):
    """INSERT OR IGNORE on the (ticker, source, link) uniqueness - a
    re-fetch of an announcement/filing already stored is a silent no-op,
    never a duplicate row. A filing whose parse failed still gets a row
    (person/action/quantity/price all None) - "a listed filing is valuable
    even unparsed" per the Part 2 spec."""
    if not ticker or not link:
        return
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO insider_filings
                 (ticker, source, filing_type, filed_at, person, action,
                  quantity, price, link, raw_title, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker.strip().upper(), source, filing_type, filed_at, person,
             action, quantity, price, link, raw_title, _now()),
        )


def filings_for(ticker, months=12):
    """Filings for `ticker` filed within the last `months`, newest first.
    A filing with no parseable filed_at date sorts last (still shown, just
    not date-orderable) rather than being dropped."""
    if not ticker:
        return []
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM insider_filings WHERE ticker = ?
                 ORDER BY (filed_at IS NULL), filed_at DESC""",
            (ticker.strip().upper(),),
        ).fetchall()
    out = [dict(r) for r in rows]
    if months is None:
        return out
    cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30.44)).strftime("%Y-%m-%d")
    return [r for r in out if r["filed_at"] is None or r["filed_at"][:10] >= cutoff]


def should_refetch(ticker, max_age_hours=24):
    if not ticker:
        return True
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_fetched_at FROM insider_fetch_log WHERE ticker = ?",
            (ticker.strip().upper(),),
        ).fetchone()
    if not row:
        return True
    try:
        last = datetime.fromisoformat(row[0])
    except ValueError:
        return True
    from datetime import timedelta
    return (datetime.now(timezone.utc) - last) > timedelta(hours=max_age_hours)


def mark_fetched(ticker):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO insider_fetch_log (ticker, last_fetched_at) VALUES (?, ?)
               ON CONFLICT(ticker) DO UPDATE SET last_fetched_at = excluded.last_fetched_at""",
            (ticker.strip().upper(), _now()),
        )


# --------------------------------------------------------------------
# Buyback summary - one row per ticker, overwritten on every refresh.
# --------------------------------------------------------------------

def set_buyback_summary(ticker, kind, text, amount=None, currency=None, filed_at=None):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO buyback_summary (ticker, kind, text, amount, currency, filed_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 kind = excluded.kind, text = excluded.text, amount = excluded.amount,
                 currency = excluded.currency, filed_at = excluded.filed_at,
                 updated_at = excluded.updated_at""",
            (ticker.strip().upper(), kind, text, amount, currency, filed_at, _now()),
        )


def get_buyback_summary(ticker):
    if not ticker:
        return None
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM buyback_summary WHERE ticker = ?", (ticker.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------
# Scanner roll-up: net insider buying/selling value over the last 12
# months, for the Scanner's "Insider net 12m" column - computed from
# whatever filings DO have quantity+price (an unparsed filing simply
# doesn't contribute a number, same red-flag-free "no data" convention
# every other Scanner column already uses for a missing value).
# --------------------------------------------------------------------

def net_insider_value_12m(ticker):
    rows = filings_for(ticker, months=12)
    total = 0.0
    have_any = False
    for r in rows:
        if r["action"] not in ("BUY", "SELL"):
            continue
        if r["quantity"] is None or r["price"] is None:
            continue
        have_any = True
        value = r["quantity"] * r["price"]
        total += value if r["action"] == "BUY" else -value
    return round(total, 2) if have_any else None


def net_insider_values_for(tickers):
    """Batched version of net_insider_value_12m for a Scanner-sized list
    of tickers (the "Insider net 12m" column) - ONE query + one in-Python
    pass instead of opening a fresh SQLite connection per row, which a
    500-ticker scan table would otherwise do. {ticker: value or None}."""
    if not tickers:
        return {}
    upper = [t.strip().upper() for t in tickers if t]
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=366)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" for _ in upper)
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT ticker, action, quantity, price FROM insider_filings
                  WHERE ticker IN ({placeholders}) AND action IN ('BUY', 'SELL')
                    AND quantity IS NOT NULL AND price IS NOT NULL
                    AND (filed_at IS NULL OR filed_at >= ?)""",
            (*upper, cutoff),
        ).fetchall()
    totals = {}
    for r in rows:
        v = r["quantity"] * r["price"]
        totals[r["ticker"]] = totals.get(r["ticker"], 0.0) + (v if r["action"] == "BUY" else -v)
    return {t: (round(totals[t], 2) if t in totals else None) for t in upper}
