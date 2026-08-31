"""
snapshot_store.py

Per-ticker "snapshot" - the nightly-computed public numbers behind
/s/<TICKER> and the read-only /api/v1/* JSON API (Phase 1 of the
AI-readiness roadmap, AI_ROADMAP_stocksdeepdive.md). Built ONLY from data
engines already compute for other reasons - the overnight universe scan
(nightly_scan.analyze_ticker_lite, run via scheduler_engine) and the Moat
Score cache (moat_engine) - so this store never triggers a fresh
yfinance/network call of its own, and nothing here is user data: every
value already appears in the public Scanner/Deep Dive pages.

A ticker whose Moat Score hasn't been computed yet (nobody has opened its
Deep Dive page, and the nightly scan itself never computes Moat - see
build_snapshots_from_scan below) simply shows moat: None until it has
been; the snapshot still exists and is still useful without it.

Same SQLite file / volume-resolution rule as every other store here - see
positions_store.py's docstring for why SQLite over a hand-rolled JSON
file (concurrent readers/writers). One row per ticker: a snapshot is a
single current state, overwritten each night it's rescanned, not a
history log (nightly_scan already writes its own history via
score_history.py if a time series is ever needed).
"""

import json
import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            ticker TEXT PRIMARY KEY,
            universe TEXT NOT NULL,
            data_json TEXT NOT NULL,
            moat_json TEXT,
            generated_at TEXT NOT NULL
        )"""
    )
    return conn


def save_snapshot(ticker, universe, row, moat=None):
    """UPSERT one ticker's snapshot. `row` is the plain dict
    nightly_scan.analyze_ticker_lite() returns (or an equivalent shape);
    `moat` is moat_engine's result dict, or None."""
    if not ticker or not row:
        return
    ticker = ticker.strip().upper()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO snapshots
                 (ticker, universe, data_json, moat_json, generated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 universe = excluded.universe,
                 data_json = excluded.data_json,
                 moat_json = excluded.moat_json,
                 generated_at = excluded.generated_at""",
            (ticker, universe, json.dumps(row),
             json.dumps(moat) if moat else None,
             datetime.now(timezone.utc).isoformat()),
        )


def get_snapshot(ticker):
    """{ticker, universe, data, moat, generated_at} for one ticker, or
    None if it has never been scanned. `data` and `moat` are already
    parsed back into dicts (moat is None when nothing was cached)."""
    if not ticker:
        return None
    ticker = ticker.strip().upper()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM snapshots WHERE ticker = ?", (ticker,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["data"] = json.loads(d.pop("data_json"))
    except (TypeError, ValueError):
        d.pop("data_json", None)
        d["data"] = {}
    moat_raw = d.pop("moat_json", None)
    try:
        d["moat"] = json.loads(moat_raw) if moat_raw else None
    except (TypeError, ValueError):
        d["moat"] = None
    return d


def all_snapshots(universe=None, limit=None):
    """[{ticker, universe, generated_at}, ...], ticker order. Deliberately
    does NOT parse each row's full data/moat JSON - callers that only need
    the ticker list (the /s/ index, the sitemap) shouldn't pay to parse
    every row's whole payload; call get_snapshot(ticker) for the full
    picture on one ticker."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        q = "SELECT ticker, universe, generated_at FROM snapshots"
        params = ()
        if universe:
            q += " WHERE universe = ?"
            params = (universe,)
        q += " ORDER BY ticker"
        if limit:
            q += " LIMIT ?"
            params = params + (limit,)
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def snapshot_count():
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]


def build_snapshots_from_scan(universe, rows, log=print):
    """Called after a nightly universe scan saves its rows
    (scheduler_engine._run_nightly -> nightly_scan.run_universe_scan) -
    builds/refreshes one snapshot per scanned ticker.

    Never calls yfinance itself, and never calls moat_engine itself
    either: nightly_scan.run_universe_scan() already attaches Moat
    (row["Moat"] / row["Moat Erosion"] / row["Moat Mode"]) to each row via
    its own _attach_moat() step before rows ever reach here, using
    moat_engine's cache exactly once per ticker per scan. Re-deriving it
    here would just be a second, independent cache lookup that could
    disagree with the value the scan already recorded - so this simply
    reads it back off the row. A ticker with no cached moat yet just has
    Moat=None on the row, which becomes moat=None below - the snapshot
    still exists and is still useful without it. This step is therefore
    free on top of a scan that already ran - no extra network calls, no
    AI key, nothing beyond re-shaping data engines already produced."""
    n = 0
    for row in (rows or []):
        ticker = (row.get("Ticker") or "").strip().upper()
        if not ticker:
            continue
        moat = None
        if row.get("Moat") is not None or row.get("Moat Erosion") or row.get("Moat Mode"):
            moat = {
                "score": row.get("Moat"),
                "erosion": row.get("Moat Erosion"),
                "mode": row.get("Moat Mode"),
            }
        try:
            save_snapshot(ticker, universe, row, moat=moat)
            n += 1
        except Exception as e:
            log(f"[snapshot_store] {ticker}: failed to save snapshot: {e}")
    log(f"[snapshot_store] {universe}: {n} snapshot(s) saved")
    return n
