"""
moat_engine.py

Moat Score (Phase 1 - display only, NOT part of Value Score - see
MOAT_IN_VALUE_SCORE below for the Phase-2 switch, built but left off).

One-line distinction from Quality: Quality measures how good the
business is RIGHT NOW - today's levels (net margin, ROIC, ROE - see
quality_engine.py / auto_compounder_engine.py's own ROIC/ROE metrics).
Moat measures how likely the business is to STAY that way - durability
through TIME. No ratio is shared between the two: Quality owns today's
snapshot; Moat owns the multi-year trend behind it.

Four pillars, 0-100 total:
  1. Excess-return spread (30 pts) - TTM ROIC (ROE for financials) minus
     the cost of capital.
  2. Persistence (25 pts) - fraction of available fiscal years the
     business cleared a 12% return threshold. Capped at 20/25 while
     fewer than 8 years of statement history are on file.
  3. Pricing power (25 pts) - the gross-margin trend (falls back to
     operating margin, flagged, when no Gross Profit row exists):
     held/expanded, stability, growth-without-discounting.
  4. Reinvestment (20 pts) - incremental return on newly-deployed
     capital, newest available year vs oldest.

Plus an erosion overlay (see _erosion_overlay): when ROIC and operating
margin have BOTH fallen meaningfully below their own multi-year average
in the latest window, the read becomes "moat watch" (a caption, no
score penalty); if that's true in the two most recent windows in a row,
"eroding" (caps the final score at 50).

A pillar that cannot be computed (missing rows, no usable years) is
DROPPED, never defaulted to a neutral/zero value, and the remaining
pillars are reweighted to 100 - a flag records what and why. Defaulting
a missing pillar to a neutral score is exactly the mistake that once let
ETFs outscore CSL in the portfolio health score; this module does not
repeat it. ETFs/funds and tickers with under 2 usable statement years
get score=None, mode="na" (displayed as "N/A (fund)"/"N/A").

This module is NEW and only CALLS the existing engines - it does not
modify auto_compounder_engine.py, capm_engine.py, or fundamentals_data.py.
Every statement-row lookup (_series/_find_row), the CAPM cost-of-equity
formula (capm_engine.resolve_discount_rate), the plausibility-checked
TTM interest expense (_interest_expense_ttm), and the "average invested
capital over the period" convention (_avg_invested_capital_for_year) are
all reused via that module's own already-public helper functions
(underscore-prefixed by this codebase's convention, not by access
control) - so Moat's numbers are computed exactly the same way the
site's existing ROIC/WACC figures already are, never a second, slightly
different reimplementation living in this file. The one exception is
WACC itself: auto_compounder_engine.py's own WACC math
(_build_cost_of_capital's nested `_wacc_for` closure) is not a
module-level function, so it can't be imported directly - _ttm_wacc()
below reproduces that exact formula at the TTM point only, built from
the same importable helper calls, rather than editing that engine to
export it.

Caching: same JSON-on-persistent-Railway-Volume pattern
auto_compounder_engine.py's own auto_cv_sections cache uses (24h TTL,
invalidated on either this module's own MOAT_ENGINE_VERSION or
fundamentals_data.BUNDLE_VERSION changing) - reuses that module's
_data_dir() helper for the volume-path resolution rather than
duplicating the RAILWAY_VOLUME_MOUNT_PATH lookup a third time.
"""

import json
import os
import tempfile
import time

import auto_compounder_engine as _ace
import capm_engine
import fundamentals_data

# Bumped whenever a pillar formula/band in this module changes, so a
# stale cached score doesn't silently outlive the code that produced it -
# same discipline as auto_compounder_engine.ENGINE_VERSION, and for the
# identical reason (see that constant's own comment for the cautionary
# tale of a change shipping without a version bump).
MOAT_ENGINE_VERSION = 1

_CACHE_DIR_NAME = "moat_cache"
_CACHE_TTL_SECONDS = 24 * 3600

TAX_RATE_DEFAULT = 0.25
PERSISTENCE_ROIC_THRESHOLD = 0.12
MIN_YEARS_FOR_FULL_PERSISTENCE = 8
PERSISTENCE_CAP_BELOW_MIN_YEARS = 20  # out of the pillar's 25

# Phase-2 switch: OFF by default. When enabled, Value Score's weights
# change to fold Moat in (see the callers in app.py/deep_dive_engine.py
# that check this flag) - built now, left off, per the owner's explicit
# instruction not to enable it yet.
MOAT_IN_VALUE_SCORE = os.environ.get("MOAT_IN_VALUE_SCORE") == "1"


# -----------------------------------
# Persistence (mirrors auto_compounder_engine.py's own _data_dir/_cache_*
# pattern, one JSON file per ticker rather than per-ticker+overrides -
# Moat has no override inputs to key on)
# -----------------------------------

def _cache_dir():
    path = os.path.join(_ace._data_dir(), _CACHE_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _cache_path(ticker):
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in ticker.upper())
    return os.path.join(_cache_dir(), f"{safe}.json")


def _read_cache(ticker):
    path = _cache_path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        meta = obj.get("_meta") or {}
        if meta.get("moat_engine_version") != MOAT_ENGINE_VERSION:
            return None
        if meta.get("bundle_version") != fundamentals_data.BUNDLE_VERSION:
            return None
        generated_at = meta.get("generated_at")
        if not generated_at:
            return None
        age = time.time() - generated_at
        if age > _CACHE_TTL_SECONDS or age < 0:
            return None
        return obj.get("result")
    except Exception:
        return None


def _write_cache(ticker, result):
    path = _cache_path(ticker)
    payload = {
        "_meta": {
            "moat_engine_version": MOAT_ENGINE_VERSION,
            "bundle_version": fundamentals_data.BUNDLE_VERSION,
            "generated_at": time.time(),
        },
        "result": result,
    }
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except OSError:
        pass


# -----------------------------------
# Classification helpers
# -----------------------------------

def _is_fund(info):
    """ETFs/funds get score=None, mode='na' - a moat is a property of an
    operating business, not a basket of them. quoteType is the reliable
    signal; a name-based "ETF" fallback catches the rare case where
    yfinance's quoteType comes back wrong/missing for a known fund."""
    quote_type = (info.get("quoteType") or "").upper()
    if quote_type in ("ETF", "MUTUALFUND"):
        return True
    name = (info.get("longName") or info.get("shortName") or "")
    return "ETF" in name.upper()


def _is_financials(info):
    """Financial Services (or bank/insurance industry) - ROIC is
    meaningless for a balance sheet that IS the business (a bank's
    "invested capital" is deposits and loans, not plant/equipment), so
    this mode substitutes ROE - cost of equity for ROIC - WACC
    throughout. AUB.AX (an insurance broker) must take this path."""
    sector = (info.get("sector") or "").strip()
    if sector == "Financial Services":
        return True
    industry = (info.get("industry") or "").lower()
    return ("bank" in industry) or ("insurance" in industry)


def moat_band(score):
    """(color, verdict_label) for the site-wide green/amber/red
    convention, matching the bands specified for the Comparison table's
    Moat column: green > 70, amber 40-70, red <= 40. Returns None for a
    None score (fund/insufficient-data case - callers render "N/A")."""
    if score is None:
        return None
    if score > 70:
        return "green", "Strong moat"
    if score <= 40:
        return "red", "Weak/no moat"
    return "amber", "Moderate moat"


# -----------------------------------
# Per-year return series (ROIC for a standard company, ROE for financials)
# -----------------------------------

def _year_return_series(bundle, info, is_financials):
    """[(year_label, return_value_or_None, extra), ...] newest-first,
    keyed off the balance sheet's own stockholders'-equity year list (the
    same anchor _avg_invested_capital_for_year already uses).

    Standard mode: return_value is ROIC = NOPAT / average invested
    capital, using ONE TTM tax rate applied to every period - the same
    convention _build_cost_of_capital's own _roic_for()/_wacc_for() use
    (see that function's comment: cost of equity, and by the same logic
    the tax rate captured alongside it, is "ONE constant across every
    year", not recomputed per year). `extra` carries the raw NOPAT/
    invested-capital pair (needed by the Reinvestment pillar) so it is
    not recomputed a second time; {} in financials mode, where
    Reinvestment does not apply (see _pillar_reinvestment).

    Financials mode: return_value is plain ROE = net income / that
    year's own equity (no averaging - the ordinary ROE convention)."""
    income, balance = bundle["income"], bundle["balance"]
    equity_s = dict(_ace._series(balance, "stockholders_equity"))
    years_desc = [y for y, _ in _ace._series(balance, "stockholders_equity")]
    if not years_desc:
        return []

    if is_financials:
        net_income_s = dict(_ace._series(income, "net_income"))
        out = []
        for y in years_desc:
            eq, ni = equity_s.get(y), net_income_s.get(y)
            out.append((y, (ni / eq) if (eq and ni is not None) else None, {}))
        return out

    debt_s = dict(_ace._series(balance, "total_debt"))
    cash_s = dict(_ace._series(balance, "cash"))
    op_income_s = dict(_ace._series(income, "operating_income"))
    pretax_s = dict(_ace._series(income, "pretax_income"))
    tax_s = dict(_ace._series(income, "tax_provision"))
    ttm_pretax, ttm_tax = pretax_s.get(years_desc[0]), tax_s.get(years_desc[0])
    tax_rate = (ttm_tax / ttm_pretax) if (ttm_tax is not None and ttm_pretax) else TAX_RATE_DEFAULT

    out = []
    for y in years_desc:
        op_inc = op_income_s.get(y)
        ic = _ace._avg_invested_capital_for_year(equity_s, debt_s, cash_s, years_desc, y)
        if op_inc is None or not ic:
            out.append((y, None, {}))
            continue
        nopat = op_inc * (1 - tax_rate)
        out.append((y, nopat / ic, {"nopat": nopat, "invested_capital": ic}))
    return out


def _operating_margin_series(bundle, years_desc):
    """[operating_margin_or_None, ...] in the SAME order as years_desc
    (the return series' own year list) - looked up independently from
    the income statement's own year labels rather than assumed to line
    up 1:1, since the balance sheet and income statement can carry
    slightly different fiscal year-end labels for the same company (seen
    on FID.AX - see _build_cost_of_capital's own comment on that exact
    mismatch). A year with no match on either side is None here, and
    _erosion_overlay's caller filters those out positionally before
    windowing, so ROIC and operating margin never end up misaligned by
    one year against each other."""
    income = bundle["income"]
    revenue_s = dict(_ace._series(income, "revenue"))
    op_s = dict(_ace._series(income, "operating_income"))
    out = []
    for y in years_desc:
        rev, op = revenue_s.get(y), op_s.get(y)
        out.append((op / rev) if (rev and op is not None) else None)
    return out


# -----------------------------------
# Pillar 1 - Excess-return spread (30 pts)
# -----------------------------------

def _ttm_wacc(bundle, basics):
    """Reproduces _build_cost_of_capital's own _wacc_for() at the TTM
    point only - this module never needs WACC for any other year (see
    module docstring for why this can't just import that closure).
    Returns (wacc_or_None, flagged)."""
    info, mcap, ccy = basics["info"], basics["market_cap"], basics["currency"]
    balance, income = bundle["balance"], bundle["income"]

    total_debt = _ace._latest(balance, "total_debt")
    long_term_debt = _ace._latest(balance, "long_term_debt")
    ltd = long_term_debt if long_term_debt is not None else total_debt
    ltd_flagged = long_term_debt is None

    interest_expense, interest_flagged, interest_estimated = _ace._interest_expense_ttm(bundle)

    pretax_income = _ace._latest(income, "pretax_income")
    tax_provision = _ace._latest(income, "tax_provision")
    tax_rate = (tax_provision / pretax_income) if (tax_provision is not None and pretax_income) else TAX_RATE_DEFAULT

    ce_result = _ace._safe(capm_engine.resolve_discount_rate, info, ccy)
    cost_of_equity, ce_meta = ce_result if ce_result else (None, {})
    if cost_of_equity is None or mcap is None:
        return None, False
    ce_flagged = bool((ce_meta or {}).get("defaulted") or (ce_meta or {}).get("floored"))

    if not ltd:
        return cost_of_equity, True  # 100% equity weight - same convention as _wacc_for

    weight_e = mcap / (mcap + ltd)
    weight_d = ltd / (mcap + ltd)
    cost_of_debt = (abs(interest_expense) / ltd) * (1 - tax_rate) if interest_expense is not None else 0.0
    wacc = weight_e * cost_of_equity + weight_d * cost_of_debt
    flagged = bool(ltd_flagged or interest_expense is None or interest_flagged or interest_estimated or ce_flagged)
    return wacc, flagged


def _spread_points(spread):
    if spread <= 0:
        return 0
    if spread <= 0.05:
        return 10
    if spread <= 0.15:
        return 20
    return 30


def _pillar_spread(bundle, basics, is_financials, roic_list, flags):
    if not roic_list or roic_list[0] is None:
        flags.append("excess-return spread: TTM return could not be computed - pillar dropped")
        return None

    ttm_return = roic_list[0]
    if is_financials:
        ce_result = _ace._safe(capm_engine.resolve_discount_rate, basics["info"], basics["currency"])
        cost_of_capital, ce_meta = ce_result if ce_result else (None, {})
        if cost_of_capital is None:
            flags.append("excess-return spread: cost of equity unavailable - pillar dropped")
            return None
        if (ce_meta or {}).get("defaulted") or (ce_meta or {}).get("floored"):
            flags.append("excess-return spread: cost of equity rests on a defaulted/floored CAPM input")
    else:
        cost_of_capital, wacc_flagged = _ttm_wacc(bundle, basics)
        if cost_of_capital is None:
            flags.append("excess-return spread: WACC unavailable - pillar dropped")
            return None
        if wacc_flagged:
            flags.append("excess-return spread: WACC rests on a defaulted/estimated input")

    spread = ttm_return - cost_of_capital
    metric_name = "ROE" if is_financials else "ROIC"
    flags.append(f"excess-return spread: TTM {metric_name} {ttm_return:.1%} minus cost of capital {cost_of_capital:.1%} = {spread:+.1%}")
    return _spread_points(spread)


# -----------------------------------
# Pillar 2 - Persistence (25 pts)
# -----------------------------------

def _pillar_persistence(roic_list, is_financials, flags):
    usable = [v for v in roic_list if v is not None]
    n = len(usable)
    if n == 0:
        flags.append("persistence: no usable years - pillar dropped")
        return None

    hits = sum(1 for v in usable if v > PERSISTENCE_ROIC_THRESHOLD)
    points = (hits / n) * 25
    metric_name = "ROE" if is_financials else "ROIC"
    flags.append(f"persistence: {hits}/{n} year(s) with {metric_name} > 12% ({n} year(s) of statement data available)")
    if n < MIN_YEARS_FOR_FULL_PERSISTENCE:
        points = min(points, PERSISTENCE_CAP_BELOW_MIN_YEARS)
        flags.append(
            f"persistence: capped at {PERSISTENCE_CAP_BELOW_MIN_YEARS}/25 - fewer than "
            f"{MIN_YEARS_FOR_FULL_PERSISTENCE} years of statement history available "
            "(the 10-year test is the real one; it unlocks automatically with deeper "
            "statement depth, e.g. an EODHD_API_KEY)"
        )
    return points


# -----------------------------------
# Pillar 3 - Pricing power (25 pts), on the gross-margin series
# -----------------------------------

def _pillar_pricing_power(bundle, flags):
    income = bundle["income"]
    revenue_s = dict(_ace._series(income, "revenue"))
    years_desc = [y for y, _ in _ace._series(income, "revenue")]
    if not years_desc:
        flags.append("pricing power: no revenue history - pillar dropped")
        return None

    gp_row = _ace._find_row(income, ["Gross Profit"])
    used_fallback = gp_row is None
    numerator_s = dict(_ace._series(income, "Gross Profit" if not used_fallback else "operating_income"))
    margin_label = "operating margin" if used_fallback else "gross margin"

    gm_series = [(y, numerator_s.get(y) / revenue_s.get(y))
                 for y in years_desc if revenue_s.get(y) and numerator_s.get(y) is not None]
    if len(gm_series) < 2:
        flags.append(f"pricing power: fewer than 2 years of {margin_label} data - pillar dropped")
        return None
    if used_fallback:
        flags.append("pricing power: no Gross Profit row available - used operating margin instead")

    values = [v for _, v in gm_series]  # newest-first
    n = len(values)
    half = max(1, n // 2)
    recent_half, prior_half = values[:half], (values[half:] or values[half - 1:half])
    recent_avg, prior_avg = sum(recent_half) / len(recent_half), sum(prior_half) / len(prior_half)
    change_half_pts = (recent_avg - prior_avg) * 100

    if change_half_pts >= -1:
        held_points = 10
        held_desc = "held/expanded"
    elif change_half_pts >= -3:
        held_points = 5
        held_desc = "down 1-3pts"
    else:
        held_points = 0
        held_desc = "down >3pts"

    mean_v = sum(values) / n
    stdev_pts = ((sum((v - mean_v) ** 2 for v in values) / n) ** 0.5) * 100
    if stdev_pts < 2:
        stability_points = 10
    elif stdev_pts <= 5:
        stability_points = 5
    else:
        stability_points = 0

    # Full-period (newest vs oldest available year) margin change and
    # revenue CAGR - deliberately the whole span, not the half-over-half
    # window above, since "growth without discounting margins" is asking
    # whether margin held up over the company's actual growth run, not
    # just its most recent half.
    newest_rev, oldest_rev = revenue_s.get(years_desc[0]), revenue_s.get(years_desc[-1])
    years_span = max(1, len(years_desc) - 1)
    revenue_cagr = None
    if newest_rev and oldest_rev and newest_rev > 0 and oldest_rev > 0:
        revenue_cagr = (newest_rev / oldest_rev) ** (1 / years_span) - 1
    gm_change_full_pts = (values[0] - values[-1]) * 100

    if gm_change_full_pts >= -1:
        growth_points = 5 if (revenue_cagr is not None and revenue_cagr >= 0.05) else 2
    else:
        growth_points = 0

    flags.append(
        f"pricing power: {margin_label} {held_desc} ({change_half_pts:+.1f}pt half-over-half), "
        f"stdev {stdev_pts:.1f}pt across {n} year(s)"
    )
    return held_points + stability_points + growth_points


# -----------------------------------
# Pillar 4 - Reinvestment (20 pts) - standard mode only (financials mode
# has no NOPAT/invested-capital extra data - see _year_return_series -
# so this pillar is naturally dropped there, reweighted like any other
# missing pillar)
# -----------------------------------

def _pillar_reinvestment(year_series, flags):
    usable = [(y, e) for y, v, e in year_series if v is not None and e.get("nopat") is not None]
    if len(usable) < 2:
        flags.append("reinvestment: fewer than 2 years of NOPAT/invested-capital data - pillar dropped (not evaluated in financials mode)")
        return None

    newest_y, newest_e = usable[0]
    oldest_y, oldest_e = usable[-1]
    d_nopat = newest_e["nopat"] - oldest_e["nopat"]
    d_ic = newest_e["invested_capital"] - oldest_e["invested_capital"]

    if d_ic <= 0 and d_nopat >= 0:
        flags.append(
            f"reinvestment: capital-light/buyback pattern - invested capital shrank "
            f"{oldest_y}->{newest_y} while NOPAT held or grew"
        )
        return 16

    if d_ic == 0:
        flags.append(f"reinvestment: invested capital unchanged {oldest_y}->{newest_y} - pillar dropped")
        return None

    incremental_roic = d_nopat / d_ic
    flags.append(f"reinvestment: incremental ROIC {incremental_roic:.1%} ({oldest_y}->{newest_y})")
    if incremental_roic > 0.15:
        return 20
    if incremental_roic >= 0.08:
        return 12
    if incremental_roic >= 0.0:
        return 6
    return 0


# -----------------------------------
# Erosion overlay
# -----------------------------------

def _windows(values_newest_first):
    """(recent, prior) per the erosion-overlay spec: with >=8 periods
    available, last 3 vs the 3 immediately before (symmetric); otherwise,
    last 3 (or fewer) vs the 2 immediately before. None, None if there
    isn't enough on either side to form both windows."""
    n = len(values_newest_first)
    if n < 4:
        return None, None
    if n >= 8:
        recent, prior = values_newest_first[:3], values_newest_first[3:6]
    else:
        recent, prior = values_newest_first[:3], values_newest_first[3:5]
    if not recent or not prior:
        return None, None
    return recent, prior


def _trigger_at(roic_vals, opm_vals, offset):
    """Evaluate the erosion trigger using values[offset:] as "current" -
    offset=0 is the latest window, offset=1 shifts one period back, so
    calling this at offset=0 and offset=1 tells the caller whether the
    same trigger held in the two most recent windows in a row. Returns
    None (not evaluable - too little history) or a bool."""
    r_recent, r_prior = _windows(roic_vals[offset:])
    o_recent, o_prior = _windows(opm_vals[offset:])
    if r_recent is None or o_recent is None:
        return None
    r_recent_avg, r_prior_avg = sum(r_recent) / len(r_recent), sum(r_prior) / len(r_prior)
    o_recent_avg, o_prior_avg = sum(o_recent) / len(o_recent), sum(o_prior) / len(o_prior)
    # Relative >20% ROIC decline only makes sense against a positive
    # baseline - a prior-period ROIC at/below zero has no meaningful
    # "20% worse" reading, so the trigger simply doesn't fire from that
    # side (the operating-margin leg alone can never trigger erosion -
    # both legs are required, per spec).
    roic_down = (r_prior_avg > 0) and (r_recent_avg < r_prior_avg * 0.8)
    opm_down = (o_prior_avg - o_recent_avg) * 100 > 3
    return bool(roic_down and opm_down)


def _erosion_overlay(roic_vals, opm_vals, flags):
    now = _trigger_at(roic_vals, opm_vals, 0)
    if not now:
        return "none"
    prev = _trigger_at(roic_vals, opm_vals, 1)
    if prev:
        flags.append(
            "moat watch: ROIC and operating margin both sit meaningfully below their "
            "preceding multi-year average, in the two most recent periods"
        )
        return "eroding"
    flags.append(
        "moat watch: latest period's ROIC and operating margin sit below the "
        "preceding multi-year average"
    )
    return "watch"


# -----------------------------------
# Orchestration
# -----------------------------------

def _na_result(flags=None):
    return {"score": None, "components": [], "erosion": "none", "flags": flags or [], "years": 0, "mode": "na"}


def _compute_moat_from_bundle(ticker, bundle, info):
    flags = []

    if _is_fund(info):
        return _na_result(["ETF/fund - Moat is not applicable to a basket of businesses"])

    income, balance = bundle.get("income"), bundle.get("balance")
    if income is None or balance is None or getattr(income, "empty", True) or getattr(balance, "empty", True):
        return _na_result(["insufficient statement data"])

    is_financials = _is_financials(info)
    mode = "financials" if is_financials else "standard"
    basics = _ace._basics(bundle)

    return_series = _year_return_series(bundle, info, is_financials)
    years_desc = [y for y, _, _ in return_series]
    roic_list = [v for _, v, _ in return_series]
    usable_years = sum(1 for v in roic_list if v is not None)
    if usable_years < 2:
        return {
            **_na_result([f"fewer than 2 usable statement years ({usable_years} available)"]),
            "years": usable_years, "mode": mode,
        }

    components = []

    spread_pts = _pillar_spread(bundle, basics, is_financials, roic_list, flags)
    if spread_pts is not None:
        components.append({"pillar": "Excess-return spread", "points": round(spread_pts, 1), "max": 30})

    persistence_pts = _pillar_persistence(roic_list, is_financials, flags)
    if persistence_pts is not None:
        components.append({"pillar": "Persistence", "points": round(persistence_pts, 1), "max": 25})

    pricing_pts = _pillar_pricing_power(bundle, flags)
    if pricing_pts is not None:
        components.append({"pillar": "Pricing power", "points": round(pricing_pts, 1), "max": 25})

    reinvest_pts = _pillar_reinvestment(return_series, flags)
    if reinvest_pts is not None:
        components.append({"pillar": "Reinvestment", "points": round(reinvest_pts, 1), "max": 20})

    if not components:
        return {
            "score": None, "components": [], "erosion": "none",
            "flags": flags + ["every pillar dropped - Moat not computable"],
            "years": usable_years, "mode": mode,
        }

    total_max = sum(c["max"] for c in components)
    raw_total = sum(c["points"] for c in components)
    score = (raw_total / total_max) * 100 if total_max else None
    if total_max < 100:
        flags.append(
            f"{len(components)}/4 pillar(s) computed - the remaining {100 - total_max} "
            "points were reweighted proportionally across those, not defaulted to a "
            "neutral score"
        )

    opm_list = _operating_margin_series(bundle, years_desc)
    pairs = [(r, o) for r, o in zip(roic_list, opm_list) if r is not None and o is not None]
    erosion = _erosion_overlay([r for r, _ in pairs], [o for _, o in pairs], flags)

    if erosion == "eroding" and score is not None and score > 50:
        score = 50.0
        flags.append("score capped at 50 - moat shows sustained erosion across the two most recent periods")

    return {
        "score": round(score, 1) if score is not None else None,
        "components": components,
        "erosion": erosion,
        "flags": flags,
        "years": usable_years,
        "mode": mode,
    }


def get_cached_moat(ticker):
    """Read-only peek at this ticker's cached Moat result - never fetches
    or computes. Returns the cached result dict, or None when nothing
    (fresh) is cached yet. For the live Comparison/Scanner scan loop
    (_render_scan_results in app.py): computing a full multi-year
    fundamentals bundle live for every ticker in a 10+-name comparison is
    too slow for a page load, so that table shows whatever the nightly
    scan (or an earlier Deep Dive view of the same ticker) already
    computed and cached, and "-" otherwise - the same "attention-lite,
    labelled" convention Discovery already uses for a large live scan."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None
    return _read_cache(ticker)


def compute_moat(ticker, force_refresh=False):
    """Public API. -> {"score": 0-100 or None, "components": [...],
    "erosion": "none"|"watch"|"eroding", "flags": [...], "years": n,
    "mode": "standard"|"financials"|"na"}. Cached 24h on the volume (see
    module docstring); pass force_refresh=True to bypass a stale/wrong
    cached read (e.g. from the nightly scan after a real data update)."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _na_result(["no ticker given"])

    if not force_refresh:
        cached = _read_cache(ticker)
        if cached is not None:
            return cached

    try:
        bundle = fundamentals_data.get_bundle(ticker)
    except Exception:
        return _na_result(["fundamentals bundle fetch failed"])
    if not bundle:
        return _na_result(["fundamentals bundle unavailable"])

    result = _compute_moat_from_bundle(ticker, bundle, bundle.get("info") or {})
    _write_cache(ticker, result)
    return result


def moat_contributions(components):
    """Ordered {pillar_label: points} for the "What's driving Moat" bar
    chart (_dd_contrib_chart's own input shape) - only pillars that were
    actually computed appear, since a dropped pillar showing as a 0-point
    bar would misleadingly read as "scored zero" rather than "couldn't be
    computed"."""
    return {c["pillar"]: c["points"] for c in components}
