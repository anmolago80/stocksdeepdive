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
import math
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
    `moat` is moat_engine's result dict, or None.

    Fix (2026-09-02, live/nightly merge): MERGES `row` onto whatever's
    already stored for this ticker, key by key, instead of replacing the
    stored row outright. Root cause, observed live: app.py's
    _save_live_snapshot() (the "refresh this ticker's public snapshot
    from a visitor's own Deep Dive view" hook) builds its row from
    deep_dive_engine.analyze()'s own fields only - it has no way to
    compute nightly-only enrichments like peer_context.
    attach_percentiles()'s "Percentiles" or batch 3's "Dividend TTM"/
    "Dividend Yield %"/"Payout Ratio %"/"Next Ex-Div Date" (the dividend
    fields need a fundamentals bundle + payout ratio only the real
    nightly scan attaches - see nightly_scan._attach_dividend_payout).
    Saving that necessarily-partial row outright used to null those
    fields out on /s/<ticker>, the API and MCP the moment anyone viewed
    the ticker's Deep Dive during the day, until the next nightly scan
    overwrote the row again. The rule is generic and symmetric: for
    every key in the new `row`, keep the new value when it's not None,
    otherwise keep whatever was already stored - so a key the new row
    doesn't even set (like the dividend fields above, simply absent from
    a live row) is left completely untouched. A real nightly scan
    computes every field itself, so every value it saves is non-None
    (or genuinely None, e.g. no dividend history) and this merge is a
    no-op on that path - only the live hook's partial row is ever
    missing keys. `universe` gets the SAME kind of treatment, but only
    in the "live" direction: a ticker that already has a real scanned
    universe keeps it instead of being reclassified "live" on every
    daytime view; a genuinely never-scanned ticker still gets "live".
    This is deliberately one-way - only ever triggered when THIS call's
    own `universe` is "live" (the live hook's own marker; a real nightly
    scan always passes its real universe name) - so a ticker's first
    real nightly scan still overwrites a previously-live-only "live"
    label with the real universe, exactly as before this fix."""
    if not ticker or not row:
        return
    ticker = ticker.strip().upper()
    existing = get_snapshot(ticker)
    if existing:
        merged_row = dict(existing.get("data") or {})
        for key, value in row.items():
            if value is not None:
                merged_row[key] = value
        row = merged_row
        if universe == "live" and existing.get("universe"):
            universe = existing["universe"]
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


def delete_snapshot(ticker):
    """Permanently removes one ticker's stored snapshot. Fix 9 (2026-09-01):
    used by nightly_scan.cleanup_fix9_nan_data(), the one-off boot-time
    cleanup step, to remove ASX 200/ASX 300 snapshots that were saved with
    a NaN/None Price by the 31 Aug 20:00 UTC run (see that function's
    docstring for the root cause). Not exposed anywhere else - a
    snapshot's normal lifecycle is upsert-only (save_snapshot); it's
    simply overwritten by the next scan, never deleted in ordinary
    operation."""
    if not ticker:
        return
    ticker = ticker.strip().upper()
    with _conn() as conn:
        conn.execute("DELETE FROM snapshots WHERE ticker = ?", (ticker,))


# -----------------------------------------------------------------
# Fix 6, AI fixes round 2 (2026-08-31): a single public field mapping,
# applied to every public-facing surface that reads a stored snapshot
# row (or a raw scan_store row of the same shape - both are
# nightly_scan.analyze_ticker_lite()'s field names, or app.py's
# live-hook equivalent): snapshot_render.py (page/copy-as-text/meta/
# JSON-LD), api_v1.py (deep-dive/compare/scan) and mcp_server.py (all
# five tools). Whitelist, not blacklist - only keys listed in
# _PUBLIC_FIELD_MAP are ever copied through, so a new internal column
# added to the scan row later never leaks onto a public surface by
# default. "Signal"/"Trade Setup"/"Trend" are deliberately dropped
# everywhere this touches: on an indexable page "LONG"/"AVOID" read as
# recommendations, which conflicts with the site's factual framing
# (descriptions of calculations, never advice) and its own disclaimer.
# The Deep Dive page's own factual view already calls this same number
# "Value Score", not "Long Score" - this brings the new public
# surfaces into line with that. The Scanner/Deep Dive pages themselves
# are NOT touched by this - that naming is a separate, existing
# decision (see the round 2 instruction doc's own scope note).
# -----------------------------------------------------------------

_PUBLIC_FIELD_MAP = {
    "Price": "price",
    "Intrinsic Value": "intrinsic_value",
    "MOS %": "mos_pct",
    "Quality": "quality",
    "Psychology": "psychology",
    "Discovery (lite)": "discovery",
    "Long Score": "value_score",
    "Valuation": "valuation_label",
    "Type": "company_type",
    "Quality Default": "quality_estimated",
    "Intrinsic Default": "intrinsic_estimated",
    "Company Name": "company_name",
    # Services batch 2, Part 1 (2026-09-01): "What the price implies" -
    # reverse DCF, computed by nightly_scan.analyze_ticker_lite() via
    # reverse_dcf_engine.compute(). None/None for any ticker with no
    # positive FCF base (same set Intrinsic Value/MOS are already None
    # for, plus P/E-blend names) - see nightly_scan.py's own comment.
    "Implied Growth %": "implied_growth_pct",
    "Model Growth %": "model_growth_pct",
    # Services batch 2, Part 2 (2026-09-01): peer context - percentile
    # ranks (universe + sector objects, one per metric), computed once
    # per nightly scan by peer_context.attach_percentiles(). Absent
    # entirely on a row from a scan saved before this shipped (an old
    # cached scan file simply has no "Percentiles" key yet) - public_view
    # already omits any key not present on `row`, so that's a clean
    # omission, not a crash or a fabricated value.
    "Percentiles": "percentiles",
    # Services batch 3, Part A1 (2026-09-02): dividend headline numbers -
    # "Dividend TTM"/"Dividend Yield %"/"Next Ex-Div Date" are computed
    # for every scanned ticker inside nightly_scan.analyze_ticker_lite();
    # "Payout Ratio %" only for the two real nightly-scan paths (needs a
    # fundamentals bundle - see nightly_scan._attach_dividend_payout's own
    # docstring). Absent entirely on a ticker with no dividend history at
    # all, or one scanned before this shipped - public_view already omits
    # any key not present on `row`, never a fabricated 0. Franking is
    # deliberately NOT here: Yahoo has no franking data, and the
    # per-holding Franking % (Part A2) is private, never public.
    "Dividend TTM": "dividend_ttm",
    "Dividend Yield %": "dividend_yield_pct",
    "Payout Ratio %": "payout_ratio_pct",
    "Next Ex-Div Date": "next_ex_date",
}


def public_view(row):
    """Maps one stored/scan row's internal field names to the neutral
    public names above. Returns a NEW dict (never mutates `row`) holding
    only the whitelisted keys that are actually present on `row` - a
    ticker whose snapshot predates this fix (no "Company Name" cached
    yet) simply omits company_name rather than inventing one; callers
    that need a display name should fall back to the ticker itself,
    same as deep_dive_engine.analyze()'s own `info.get("longName") or
    info.get("shortName") or ticker` pattern. Deliberately does not
    include "Ticker" itself - every caller already has the ticker from
    the outer snapshot/scan-row shape, so this stays purely about
    field *names*, not a full row reshape.

    Post-fix, 2026-08-31 (live-caught while verifying this same round's
    deploy): a ticker whose price/intrinsic-value fetch failed can have
    a literal NaN float sitting in the stored row (e.g. yfinance
    returning no price that day). NaN silently rendered as the text
    "nan" on /s/<ticker>, and crashed /api/v1/deep-dive/* and every
    other JSON surface outright - Starlette's JSONResponse calls
    json.dumps(..., allow_nan=False), so a NaN in the payload is a 500,
    not just a cosmetic glitch. Every value that reaches a public
    surface goes through this one function, so sanitizing NaN/Infinity
    to None here (rather than patching each of the call sites) fixes the
    JSON crash and, for free, makes the HTML/copy-text paths fall back
    to their existing "-" / omit-this-fact handling for a missing
    number, instead of ever having a NaN to format in the first place."""
    row = row or {}
    out = {
        public_key: row[internal_key]
        for internal_key, public_key in _PUBLIC_FIELD_MAP.items()
        if internal_key in row
    }
    for key, value in out.items():
        if isinstance(value, float) and not math.isfinite(value):
            out[key] = None
    return out


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
