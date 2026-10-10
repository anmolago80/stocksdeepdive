"""
market_readiness_engine.py

PART 3 STEP 3.1 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): the owner-only "Market readiness"
panel's own logic - pure Python, no Streamlit dependency, same
"*_engine.py owns the logic, app.py owns the markup" split every other
owner-only Admin panel in this codebase already follows.

Reads ONLY already-stored data: scan_store's saved overnight-scan rows
(scan_store.load_scan_raw(..., allow_private=True) - the SAME read the
existing "Private universes" Admin panel uses) and score_history's
per-ticker daily history (already recorded for every universe,
private ones included - nightly_scan.py's own score_history.record()
call has no privacy gate of its own). NO network call anywhere in this
module - confirmed by construction: nothing here imports yfinance, a
fundamentals_data/deep_dive_engine/capm_engine fetch function, or
anything else that could reach a live data provider. NOTHING is
written - every function here is a pure read/compute.

"Never estimate; show 'not stored' and say which field is missing" is
enforced throughout: a count or median returns None (never a guessed
number) when the input rows don't carry the field it needs.
"""
import re

import fcf_valuation_engine
import scan_store
import score_history

# A28 cent-per-company Top 200 scoring cost estimate (PART 3 STEP 3.1's
# own dry-run figure, Andrew's own 2-cents-per-company rate quoted
# throughout this instruction - never a scoring call itself).
TOP200_SCORE_COST_PER_COMPANY_USD = 0.02

RUN_TO_RUN_FLAG_THRESHOLD_PCT = 25.0
HIGH_DIVIDEND_YIELD_THRESHOLD_PCT = 12.0
HIGH_MOS_THRESHOLD_PCT = 80.0
LOW_MOS_THRESHOLD_PCT = -80.0

_TRAILING_NUMBER_RE = re.compile(r"(\d+)\s*$")


def expected_constituent_count(universe_name):
    """The index's own published nominal size, read off its OWN name
    (e.g. "FTSE 100" -> 100, "TOPIX 500" -> 500, "Nikkei 225" -> 225) -
    these indices are literally named by their constituent count, so
    this is a stable, dated-by-definition fact, never a guessed or
    memorised list (the "never build a constituent list from memory"
    rule is about TICKER lists, not an index's own well-known size).
    None for a name with no trailing number (e.g. "TSX Composite",
    "All Ordinaries") - "not stored", never a guess, for those."""
    m = _TRAILING_NUMBER_RE.search(universe_name or "")
    return int(m.group(1)) if m else None


def list_readiness_universes():
    """Every universe with a saved scan right now, public AND private -
    scan_store.list_saved_universes(include_private=True), sorted. The
    SAME call the existing "Private universes" Admin panel already
    uses (app.py's _render_private_universes_panel())."""
    return sorted(scan_store.list_saved_universes(include_private=True))


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _is_at_growth_cap(r):
    """True iff this ONE row's growth rate was capped - the real
    "Growth Governor" field (Proposal 1 of the Director's numbered
    fix-proposal round, 8 Oct 2026) when the KEY is present on the row
    at all (checked by key presence, not by its value being non-None -
    a genuinely uncapped row's own Growth Governor is legitimately
    None/"Manual"/"Default", and that must still count as "the real
    field answered no", never fall through to the proxy below). For a
    row saved by an OLDER nightly_scan.py (before this field existed
    at all), the KEY itself is simply absent, and this falls back to
    the ORIGINAL proxy - "Growth Used" == "Growth Ceiling Used" (PART 3
    STEP 3.0's own Q4 answer, which named this proxy's own false-
    positive/false-negative risk) - so an old stored scan still gets a
    best-effort count rather than silently reading as zero."""
    if "Growth Governor" in r:
        return r["Growth Governor"] == "Cap"
    return (
        r.get("Growth Used") is not None and r.get("Growth Ceiling Used") is not None
        and abs(r["Growth Used"] - r["Growth Ceiling Used"]) < 1e-6
    )


def valuation_breakdown(rows):
    """{"dcf_unreliable_count", "at_growth_cap_count",
    "analyst_estimate_count", "fallback_path_counts" ({source: count}),
    "median_mos_pct", "above_80_count", "below_neg80_count"}.

    "At growth cap" - see _is_at_growth_cap()'s own docstring (Proposal
    1 of the Director's numbered fix-proposal round, 8 Oct 2026): reads
    the row's real stored "Growth Governor" when present, falling back
    to the original Growth-Used/Growth-Ceiling-Used proxy for a row
    saved before that field existed. "Fallback path" groups by the
    row's own "Growth Source" field (None rows excluded - "not stored"
    for an individual row never pollutes this count)."""
    dcf_unreliable = sum(1 for r in rows if r.get("DCF Unreliable"))
    at_cap = sum(1 for r in rows if _is_at_growth_cap(r))
    analyst = sum(1 for r in rows if (r.get("Growth Source") or "").startswith("analyst"))
    fallback_counts = {}
    for r in rows:
        src = r.get("Growth Source")
        if src is None:
            continue
        fallback_counts[src] = fallback_counts.get(src, 0) + 1
    mos_values = [r.get("MOS %") for r in rows if r.get("MOS %") is not None]
    above_80 = sum(1 for v in mos_values if v > HIGH_MOS_THRESHOLD_PCT)
    below_neg80 = sum(1 for v in mos_values if v < LOW_MOS_THRESHOLD_PCT)
    return {
        "dcf_unreliable_count": dcf_unreliable,
        "at_growth_cap_count": at_cap,
        "analyst_estimate_count": analyst,
        "fallback_path_counts": fallback_counts,
        "median_mos_pct": _median(mos_values),
        "above_80_count": above_80,
        "below_neg80_count": below_neg80,
    }


def dividend_breakdown(rows):
    """{"paying_count", "median_yield_pct", "high_yield_tickers"} -
    "pays one" = a stored "Dividend TTM" that is a positive number;
    high_yield_tickers lists every ticker whose own "Dividend Yield %"
    exceeds HIGH_DIVIDEND_YIELD_THRESHOLD_PCT (12%), for Andrew to spot-
    check by hand (a yield this high is exactly where a pence/pounds or
    other scale mismatch would show up loudest)."""
    paying = sum(1 for r in rows if (r.get("Dividend TTM") or 0) > 0)
    yields = [r.get("Dividend Yield %") for r in rows if r.get("Dividend Yield %") is not None]
    high_yield = sorted(
        r["Ticker"] for r in rows
        if r.get("Dividend Yield %") is not None and r["Dividend Yield %"] > HIGH_DIVIDEND_YIELD_THRESHOLD_PCT
    )
    return {
        "paying_count": paying,
        "median_yield_pct": _median(yields),
        "high_yield_tickers": high_yield,
    }


def price_guard_flags(rows):
    """Every ticker the price-unit guard (fundamentals_data.
    _check_price_unit_guard()) flagged, with its own stored reason.
    The guard's own two compared figures (price x shares vs marketCap)
    are NOT persisted on the stored row - only the boolean flag and a
    text reason are - so this reports "not stored" for those two
    figures explicitly, rather than re-deriving or guessing them."""
    return [
        {
            "ticker": r.get("Ticker"),
            "reason": r.get("price_unit_suspect_reason") or "not stored",
            "price_times_shares_vs_marketcap": "not stored (only the boolean flag and reason "
                                                "are persisted on the row)",
        }
        for r in rows if r.get("price_unit_suspect")
    ]


def run_to_run_stability(rows, threshold_pct=RUN_TO_RUN_FLAG_THRESHOLD_PCT):
    """{"flagged": [...], "single_scan_tickers": [...]}. For every
    ticker in `rows`, reads score_history.series(ticker) (already
    recorded for every universe nightly_scan.py scans, private ones
    included - see this module's own docstring) and compares its last
    TWO stored days' own "intrinsic_value". A ticker with fewer than
    two stored days goes to single_scan_tickers (explicit "only one
    scan is stored", never silently skipped). "data path" for the
    OLDER of the two days is "not stored" - score_history's own row
    shape (see score_history.series()'s own docstring) never persisted
    FCF/growth source per day, only long_score/price/quality/moat/
    mos_pct/intrinsic_value/valuation_label/moat_state - so only
    TODAY's data path (read off `rows` itself) is ever known for the
    older comparison point too; this is reported as such, never
    guessed."""
    flagged = []
    single_scan = []
    for r in rows:
        ticker = r.get("Ticker")
        if not ticker:
            continue
        hist = score_history.series(ticker)
        with_iv = [h for h in hist if h.get("intrinsic_value") is not None]
        if len(with_iv) < 2:
            single_scan.append(ticker)
            continue
        prev, latest = with_iv[-2], with_iv[-1]
        prev_iv, latest_iv = prev["intrinsic_value"], latest["intrinsic_value"]
        if not prev_iv:
            continue
        pct_move = abs(latest_iv - prev_iv) / abs(prev_iv) * 100.0
        if pct_move > threshold_pct:
            flagged.append({
                "ticker": ticker,
                "previous_day": prev["day"], "previous_intrinsic_value": prev_iv,
                "latest_day": latest["day"], "latest_intrinsic_value": latest_iv,
                "pct_move": round(pct_move, 1),
                "latest_data_path": r.get("FCF Source") or "not stored",
                "previous_data_path": "not stored (score_history does not persist a per-day "
                                       "data path)",
            })
    flagged.sort(key=lambda f: f["pct_move"], reverse=True)
    return {"flagged": flagged, "single_scan_tickers": sorted(single_scan)}


def cross_ticker_pairs(universe_name, rows):
    """Every ticker in `rows` that shares an exact, normalized (stripped/
    lowercased) "Company Name" with a ticker in any OTHER stored
    universe (public or private) - the same company under another
    ticker, exact-name match only, deliberately never fuzzy (same
    "never merge on name similarity alone" rule top100_engine.
    _report_undocumented_same_name_duplicates() already follows for
    the Top 100 pool). Report-only, never merges or removes anything -
    returns [{"name", "this_ticker", "other_ticker", "other_universe"}]."""
    by_name = {}
    for other_universe in list_readiness_universes():
        if other_universe == universe_name:
            continue
        payload = scan_store.load_scan_raw(other_universe, allow_private=True)
        if not payload:
            continue
        for r in payload.get("rows") or []:
            name = (r.get("Company Name") or "").strip().lower()
            ticker = r.get("Ticker")
            if not name or not ticker:
                continue
            by_name.setdefault(name, []).append((ticker, other_universe))

    pairs = []
    for r in rows:
        name = (r.get("Company Name") or "").strip().lower()
        ticker = r.get("Ticker")
        if not name or not ticker:
            continue
        for other_ticker, other_universe in by_name.get(name, []):
            if other_ticker != ticker:
                pairs.append({
                    "name": r.get("Company Name"), "this_ticker": ticker,
                    "other_ticker": other_ticker, "other_universe": other_universe,
                })
    pairs.sort(key=lambda p: (p["this_ticker"], p["other_ticker"]))
    return pairs


def top20_by_value_score(rows):
    """The top 20 rows by "Long Score" (Value Score) descending - ticker,
    name, currency (fcf_valuation_engine.trading_currency_for(ticker,
    info=None) - ticker-suffix-only, no network call, no live `info`),
    price, intrinsic value, margin of safety, growth used, data path
    (FCF Source), dividend yield. Rows with no Long Score at all sort
    last (never dropped - "never show nothing" extends to not silently
    excluding an unscored row from this very table)."""
    ranked = sorted(rows, key=lambda r: (r.get("Long Score") is None, -(r.get("Long Score") or 0)))
    out = []
    for r in ranked[:20]:
        ticker = r.get("Ticker")
        out.append({
            "ticker": ticker, "name": r.get("Company Name"),
            "currency": fcf_valuation_engine.trading_currency_for(ticker, info=None),
            "price": r.get("Price"), "intrinsic_value": r.get("Intrinsic Value"),
            "mos_pct": r.get("MOS %"), "growth_used": r.get("Growth Used"),
            "data_path": r.get("FCF Source"), "dividend_yield_pct": r.get("Dividend Yield %"),
            "value_score": r.get("Long Score"),
        })
    return out


def top200_dry_run(rows):
    """{"candidate_count", "no_stored_score_count", "estimated_cost_usd"}
    - a COUNT only, per the instruction's own words ("nothing is
    selected or scored"). candidate_count = every NON-FUND row (every
    stored ticker in this universe is a Top 200 candidate if the
    universe were public); no_stored_score_count = candidate rows with
    no "Long Score" at all (would need a fresh score before entering
    the pool for real); cost = no_stored_score_count x TOP200_SCORE_
    COST_PER_COMPANY_USD.

    TASK 2 of instruction_scanner_chips_trusts_japan_sectors.md (9 Oct
    2026, Director-directed): a row nightly_scan.run_universe_scan()
    marked "Is Fund" (a UK/European-style investment trust - see that
    module's own _is_sector_fund() docstring) is never a real Top 200
    candidate - company valuation doesn't apply to it, so it's
    excluded here exactly like it's excluded from the Scanner's own
    UNDERVALUED count/median MOS/Value Map (which exclude it for free,
    since its MOS %/Valuation are already None/"N/A" - this function
    needed its own explicit exclusion since it counts every row
    regardless of valuation fields)."""
    _candidates = [r for r in rows if not r.get("Is Fund")]
    candidate_count = len(_candidates)
    no_score = sum(1 for r in _candidates if r.get("Long Score") is None)
    return {
        "candidate_count": candidate_count,
        "no_stored_score_count": no_score,
        "estimated_cost_usd": round(no_score * TOP200_SCORE_COST_PER_COMPANY_USD, 2),
    }


def pence_quoted_count(rows):
    """How many rows were originally pence-quoted (GBp/GBX) before
    fundamentals_data.normalize_pence_quote() converted them to pounds
    - "price_quote_unit" is stored on the row precisely so this can be
    counted without re-deriving it; the stored "Price" itself is
    ALREADY in pounds by the time it reaches this row (never pence),
    so this counts "needed conversion", not "is currently in pence"."""
    return sum(1 for r in rows if r.get("price_quote_unit") == "GBp")


def trading_currencies_found(rows):
    """{currency: count} across every row, via fcf_valuation_engine.
    trading_currency_for(ticker, info=None) - ticker-suffix only, no
    live `info`, no network call."""
    counts = {}
    for r in rows:
        ccy = fcf_valuation_engine.trading_currency_for(r.get("Ticker"), info=None)
        counts[ccy] = counts.get(ccy, 0) + 1
    return counts


def universe_readiness(universe_name):
    """The full Market readiness panel's own per-universe dict, or None
    if there's no saved scan for this universe at all. Every sub-field
    computed purely from the one stored payload (scan_store.
    load_scan_raw(universe_name, allow_private=True)) plus score_
    history / fcf_valuation_engine.trading_currency_for() - no network
    call, nothing written, anywhere in this function or anything it
    calls."""
    payload = scan_store.load_scan_raw(universe_name, allow_private=True)
    if not payload:
        return None
    rows = payload.get("rows") or []
    tickers_stored = sorted({r.get("Ticker") for r in rows if r.get("Ticker")})
    expected = expected_constituent_count(universe_name)
    return {
        "universe": universe_name,
        "is_private": scan_store.is_private_universe(universe_name),
        "rows_stored": len(rows),
        "generated_at": payload.get("generated_at"),
        "constituents_expected": expected,
        "constituents_missing": (
            (expected - len(tickers_stored)) if expected is not None else None
        ),
        "trading_currencies_found": trading_currencies_found(rows),
        "pence_quoted_count": pence_quoted_count(rows),
        "valuation": valuation_breakdown(rows),
        "dividends": dividend_breakdown(rows),
        "price_guard_flags": price_guard_flags(rows),
        "run_to_run": run_to_run_stability(rows),
        "cross_ticker_pairs": cross_ticker_pairs(universe_name, rows),
        "top20": top20_by_value_score(rows),
        "top200_dry_run": top200_dry_run(rows),
        "rows": rows,
    }
