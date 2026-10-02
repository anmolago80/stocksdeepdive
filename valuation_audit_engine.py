"""
valuation_audit_engine.py

Valuation Change Audit (owner-directed, 2 Oct 2026, Commit 2 of the
23:00 UTC incident response): a read-only, owner-only tool to inspect
what a night's valuation-engine changes actually did to the pool - the
owner's own framing: "no sandbox can reach the volume" to check this
any other way.

Two data sources, neither mutated here:
  - score_history.py's own per-day (intrinsic_value, mos_pct, quality,
    price) rows - date A (the latest recorded day) vs date B (an
    earlier day, 1-7 days back, selectable by the caller).
  - The LATEST saved scan row per ticker (scan_store.load_scan_raw(),
    via top100_engine._eligible_scan_payloads() - the SAME orphaned/
    derived/degraded universe filtering select_top100_pool() itself
    uses, reused here rather than re-derived) - the valuation-
    provenance fields (growth/FCF/discount/share-count/FX source and
    the raw/used numbers behind them) nightly_scan.analyze_ticker_lite()
    now also writes onto every row (see that function's own comment
    at its "Growth Source"/... keys - added alongside this commit,
    pure passthrough of resolve_intrinsic_value()'s own iv_meta, no
    change to any existing field or to scoring/selection).

Unlike top100_engine.select_top100_pool()'s own freshest-row-per-
ticker pass, _latest_scan_rows_by_ticker() below never excludes a
stale or DCF-unreliable row - the whole point of this audit is to
also show those. Nothing in this module feeds Value Score/Quality/
Long Score/select_top100_pool() or any other ranking/selection path;
it is a read-only report over data those paths already produced.
"""

import sqlite3
import statistics
from datetime import datetime, timezone

import resolver_engine
import score_history
import top100_engine

# How many distinct score_history days to pull back when hunting for
# date A/date B - generously above the UI's own 1-7 "days back" range
# so a gap (a universe not scanned every single night) still leaves
# enough distinct days to pick date B from.
_DAYS_LOOKBACK_BUFFER = 10


def _available_score_history_days(limit=_DAYS_LOOKBACK_BUFFER):
    """Most recent distinct score_history days, newest first."""
    with score_history._conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT day FROM score_history ORDER BY day DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r[0] for r in rows]


def _score_history_rows_for_day(day):
    """{ticker: {"intrinsic_value","mos_pct","quality","price"}} for every
    score_history row recorded on `day`."""
    if not day:
        return {}
    with score_history._conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT ticker, intrinsic_value, mos_pct, quality, price
                 FROM score_history WHERE day = ?""",
            (day,),
        ).fetchall()
    return {r["ticker"]: dict(r) for r in rows}


def _latest_scan_rows_by_ticker(log=print):
    """{ticker: (row_dict, universe)} - the single freshest saved scan
    row per ticker across every ELIGIBLE universe (top100_engine.
    _eligible_scan_payloads(), reused rather than re-derived - the
    orphaned/derived-parent/degraded exclusion select_top100_pool()
    itself applies before it ever looks at an individual row). Tie-
    break identical to select_top100_pool()'s own best_candidate pass
    (newest generated_at, then higher Long Score) - but unlike that
    pass, this NEVER excludes a stale or DCF-unreliable row; the audit
    page's whole purpose is to also show those."""
    eligible = top100_engine._eligible_scan_payloads(log=log)
    best = {}
    for universe, payload in eligible.items():
        gen_raw = payload.get("generated_at")
        try:
            gen_dt = datetime.fromisoformat(gen_raw) if gen_raw else None
        except ValueError:
            gen_dt = None
        if gen_dt is None:
            gen_dt = datetime.min.replace(tzinfo=timezone.utc)
        for row in payload.get("rows") or []:
            ticker = (row.get("Ticker") or "").strip().upper()
            if not ticker:
                continue
            row_long_score = row.get("Long Score")
            score_key = row_long_score if row_long_score is not None else -1
            key = (gen_dt, score_key)
            prev = best.get(ticker)
            if prev is None or key > (prev[2], prev[3]):
                best[ticker] = (row, universe, gen_dt, score_key)
    return {t: (row, universe) for t, (row, universe, _gd, _ls) in best.items()}


def _pct_change(a, b):
    """(a - b) / |b| * 100, or None when either side is missing or b is
    zero (nothing meaningful to divide by)."""
    if a is None or b in (None, 0):
        return None
    return (a - b) / abs(b) * 100.0


def _bucket_counts(rows, key_fn):
    out = {}
    for r in rows:
        k = key_fn(r) or "(none)"
        out[k] = out.get(k, 0) + 1
    return out


def _summarize(rows):
    n = len(rows)
    d_ivs = [r["d_iv_pct"] for r in rows if r["d_iv_pct"] is not None]
    median_d_iv = statistics.median(d_ivs) if d_ivs else None
    over_50 = [r for r in rows if r["d_iv_pct"] is not None and r["d_iv_pct"] > 50]
    over_100 = [r for r in rows if r["d_iv_pct"] is not None and r["d_iv_pct"] > 100]
    dcf_unreliable_count = sum(1 for r in rows if r["dcf_unreliable"])
    return {
        "ticker_count": n,
        "median_d_iv_pct": round(median_d_iv, 1) if median_d_iv is not None else None,
        "over_50_count": len(over_50),
        "over_100_count": len(over_100),
        "dcf_unreliable_count": dcf_unreliable_count,
        # "the line that says whether Step 4, the growth blend or
        # something else did it" - a breakdown of the >+50% group only,
        # by the three provenance dimensions most likely to explain a
        # jump: fcf_base_source, whether Step 4's distorted-year
        # mechanism fired at all (non-empty fcf_distorted_years), and
        # growth_source.
        "over_50_by_fcf_base_source": _bucket_counts(over_50, lambda r: r["fcf_base_source"]),
        "over_50_by_distorted_years": _bucket_counts(
            over_50, lambda r: "non-empty" if r["fcf_distorted_years"] else "empty"
        ),
        "over_50_by_growth_source": _bucket_counts(over_50, lambda r: r["growth_source"]),
    }


def build_change_audit(days_back=1, log=print):
    """Builds the full Valuation Change Audit: date A (the latest
    score_history day) vs date B (the `days_back`-th distinct day
    before it, clamped to 1-7), one row per ticker present on date A.

    Returns {"date_a", "date_b", "rows", "summary"}. `rows` is sorted
    by |dIV %| descending by default (a ticker with no computable dIV %
    - either side missing, or IV(B) is 0 - sorts last). Each row's
    growth/FCF/discount provenance columns come from the ticker's
    LATEST saved scan row (see _latest_scan_rows_by_ticker()'s own
    docstring) - that reflects the most recent scan regardless of
    whether it happened to run on date A itself, since score_history
    has never stored these fields and nightly_scan.py only started
    writing them in this same commit.

    Never raises on a data-shape surprise for one ticker - score_history
    only ever has plain numeric columns and the latest-scan-row lookup
    already tolerates a missing/partial row (plain dict.get()), so there
    is nothing here that depends on live network or external state."""
    days_back = max(1, min(7, int(days_back or 1)))
    available_days = _available_score_history_days()
    if not available_days:
        return {"date_a": None, "date_b": None, "rows": [], "summary": _summarize([])}

    date_a = available_days[0]
    date_b = available_days[days_back] if len(available_days) > days_back else None

    rows_a = _score_history_rows_for_day(date_a)
    rows_b = _score_history_rows_for_day(date_b)
    latest_scan = _latest_scan_rows_by_ticker(log=log)

    out_rows = []
    for ticker, a in rows_a.items():
        b = rows_b.get(ticker) or {}
        iv_a, iv_b = a.get("intrinsic_value"), b.get("intrinsic_value")
        price_a, price_b = a.get("price"), b.get("price")
        mos_a, mos_b = a.get("mos_pct"), b.get("mos_pct")

        scan_row, universe = latest_scan.get(ticker, (None, None))
        scan_row = scan_row or {}
        stored_flag = bool(scan_row.get("DCF Unreliable"))
        current_flag = resolver_engine.dcf_looks_unreliable(iv_a, price_a)

        out_rows.append({
            "ticker": ticker,
            "universe": universe,
            "price_b": price_b, "price_a": price_a,
            "iv_b": iv_b, "iv_a": iv_a,
            "d_iv_pct": _pct_change(iv_a, iv_b),
            "mos_b": mos_b, "mos_a": mos_a,
            "dcf_unreliable": stored_flag or current_flag,
            "growth_source": scan_row.get("Growth Source"),
            "growth_used": scan_row.get("Growth Used"),
            "growth_ceiling_used": scan_row.get("Growth Ceiling Used"),
            "growth_end_rate_used": scan_row.get("Growth End Rate Used"),
            "fcf_source": scan_row.get("FCF Source"),
            "fcf_base_source": scan_row.get("FCF Base Source"),
            "fcf_distorted_years": scan_row.get("FCF Distorted Years") or [],
            "fcf_base_raw": scan_row.get("FCF Base Raw"),
            "fcf_base_used": scan_row.get("FCF Base Used"),
            "capex_basis": scan_row.get("Capex Basis"),
            "share_count_flagged": bool(scan_row.get("Share Count Flagged", False)),
            "fx_converted": scan_row.get("FX Converted"),
        })

    out_rows.sort(
        key=lambda r: abs(r["d_iv_pct"]) if r["d_iv_pct"] is not None else -1,
        reverse=True,
    )

    summary = _summarize(out_rows)
    if log:
        log(
            f"[valuation_audit] {date_a} vs {date_b or 'n/a'}: "
            f"{summary['ticker_count']} ticker(s), median dIV "
            f"{summary['median_d_iv_pct']}%, >+50%: {summary['over_50_count']}, "
            f">+100%: {summary['over_100_count']}, "
            f"DCF-unreliable: {summary['dcf_unreliable_count']}"
        )
    return {"date_a": date_a, "date_b": date_b, "rows": out_rows, "summary": summary}
