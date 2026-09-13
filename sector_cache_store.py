"""
sector_cache_store.py

Part 48: a learn-once GICS sector cache that self-heals the coverage gap
Part 47 left behind. scanner_engine.sector_for_ticker() renders nothing
for most Small Ordinaries / All Ordinaries names because that constituent
source carries no Sector column at all (unlike the ASX 200/S&P 500 pool
fetches nightly_scan.py already gets a live Sector column from), and the
static ASX_SECTOR_MAP fallback only covers roughly the ASX 200. Every
sector this app ever learns for a ticker - from a scan row that DID carry
one, a Deep Dive company-info fetch, the static map, or the nightly
top-up below - lands here once and is served locally forever after (no
network call, ever, to read it back).

Same _data_dir()/DB_PATH/_conn() convention as every other store module
(see insider_store.py's own docstring) - same shared stocksdeepdive.db
file on the same Railway volume mount every other store already relies
on, so this survives redeploys with zero deploy-config changes.

Pure storage, no network - every caller here (scanner_engine.py,
nightly_scan.py) does its own fetching (or has none to do, on a cache
hit) and passes the result in. Every function swallows its own errors -
a cache read/write must never break the scan or page render it rides on.

  sector_cache(ticker PRIMARY KEY, sector, source, learned_at)
    One row per ticker - the sector cache holds a single current answer,
    not a history. `source` is one of "scan" (learned in bulk from a
    universe's own scan rows - see nightly_scan.run_universe_scan()),
    "static_map"/"deep_dive" (written through by scanner_engine.
    sector_for_ticker() itself when the static ASX map or a caller's
    company_info supplies the answer), or "topup" (the nightly top-up
    job below, run_sector_topup() in nightly_scan.py). Purely informational
    - nothing here branches on it - kept only so a "where did this
    actually come from" question is answerable later.

WHY LAST-WRITE-WINS, NOT FIRST: a GICS sector reclassification is rare
but real (companies do get reassigned), and there's no reliable local
signal to tell "this cached value is now stale" from "this fresh fetch is
wrong" - trusting whichever fetch is most recent is the same tradeoff
every other *_fetch_log-style cache in this codebase already makes.
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
        """CREATE TABLE IF NOT EXISTS sector_cache (
            ticker TEXT PRIMARY KEY,
            sector TEXT NOT NULL,
            source TEXT NOT NULL,
            learned_at TEXT NOT NULL
        )"""
    )
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def get(ticker):
    """This ticker's cached sector, or None - no cache row, blank ticker,
    or any DB error (never raises; a read failure here must degrade to
    "unknown" exactly like a genuinely-uncached ticker, never crash a
    page)."""
    if not ticker:
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT sector FROM sector_cache WHERE ticker = ?",
                (str(ticker).strip().upper(),),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def get_many(tickers):
    """{ticker: sector} for every ticker in `tickers` that has a cache
    row - ONE query, not one per ticker, so a Scanner-sized table can call
    this once instead of opening a fresh connection per row (same reason
    insider_store.net_insider_values_for() is batched). A ticker with no
    cache row is simply absent from the result dict - "missing means
    unknown", never a placeholder entry. Never raises."""
    if not tickers:
        return {}
    upper = sorted({str(t).strip().upper() for t in tickers if t})
    if not upper:
        return {}
    try:
        placeholders = ",".join("?" for _ in upper)
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT ticker, sector FROM sector_cache WHERE ticker IN ({placeholders})",
                upper,
            ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def learn(ticker, sector, source):
    """UPSERT ticker -> sector. A no-op for a blank ticker or a blank/None
    sector - this cache must never learn "unknown" as if it were an
    answer, the same rule scanner_engine.sector_for_ticker() itself
    enforces at every call site. Last write wins on a re-learn (see this
    module's own docstring for why). Always swallows its own errors - a
    cache write is a side effect, never allowed to break the scan or page
    render that produced the value."""
    if not ticker:
        return
    if not isinstance(sector, str) or not sector.strip():
        return
    try:
        with _conn() as conn:
            conn.execute(
                """INSERT INTO sector_cache (ticker, sector, source, learned_at)
                     VALUES (?, ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     sector = excluded.sector,
                     source = excluded.source,
                     learned_at = excluded.learned_at""",
                (str(ticker).strip().upper(), sector.strip(), source, _now()),
            )
    except Exception:
        pass


def unknown_among(tickers, limit=None):
    """The subset of `tickers` with NO cache row yet, de-duplicated and in
    first-seen order, capped at `limit` (None = uncapped). This is what
    the nightly top-up (nightly_scan.run_sector_topup()) uses to pick its
    batch. Note this only reports what THIS cache lacks - a ticker
    returned here might still resolve via a live scan_row or the static
    ASX map at render time; this function has no visibility into either
    of those. Never raises (an error here just means an empty/best-effort
    result, same "degrade to unknown, never crash" rule as get/get_many)."""
    if not tickers:
        return []
    seen = set()
    ordered = []
    for t in tickers:
        if not t:
            continue
        u = str(t).strip().upper()
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    if not ordered:
        return []
    try:
        placeholders = ",".join("?" for _ in ordered)
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT ticker FROM sector_cache WHERE ticker IN ({placeholders})",
                ordered,
            ).fetchall()
        cached = {r[0] for r in rows}
    except Exception:
        cached = set()
    out = [t for t in ordered if t not in cached]
    if limit is not None:
        out = out[:limit]
    return out
