"""
quality_v2_engine.py

TASK 32 (Director-directed, owner request, 5 Nov 2026): a "through-the-
cycle" Quality score - SHADOW MODE / TEST ONLY, owner approval required
before any go-live (see nightly_scan.py's own call site and top100_
engine.py's simulate_pool_v2() for exactly how this is kept out of every
live surface).

EVIDENCE/ROOT CAUSE (the Director's own case, verbatim): CSL.AX's locked
baseline (16 Jul 2026) stored Quality 51, driven by auto_quality_engine.
get_quality_score()'s POINT-IN-TIME Yahoo `.info` fields (earnings
growth -80.1%, ROE 6.7%, profit margin 9.1%) - a single impairment/
restructuring year read as near-permanent business failure, when the
SAME period's one-off-excluded figures (margin 19.5%, ROIC 16.5%) show a
high-quality business with a temporary, non-cash charge. get_quality_
score() has no way to tell "one bad year" apart from "a genuinely
deteriorating business" - it only ever sees today's single `.info`
snapshot.

get_quality_score_v2() below is the fix: SAME structure, weights, clamps
and profitability gate as auto_quality_engine.get_quality_score() (see
that function's own docstring for the formula this mirrors term-for-
term), but every input is read from annual financial statements over up
to the latest 3 fiscal years, with one-off items excluded via auto_
compounder_engine.normalized_income_for_fy() - the SAME Normalized-
Income-with-fallback formula portfolio_health_engine.py's Evolution
engine already uses for its own Margins/ROIC/Growth Δ components (TASK
16c, 22 Oct 2026). Per the Director's own instruction ("no second
normalisation method"), that formula was PROMOTED out of portfolio_
health_engine.py into auto_compounder_engine.py so both callers share
the one real implementation - see that function's own docstring.

MEDIAN, not average, across however many of the latest 3 fiscal years
are actually available for a given input (1, 2, or 3) - statistics.
median() already handles all three counts correctly (median of 2 is
their mean), and a median is more robust than a straight average to
exactly the kind of single anomalous year this task exists to smooth
out: a plain 3-year average of (19.5%, a good year, a good year) still
gets pulled toward "one bad number" the way a straight average always
does, whereas the median of 3 picks the middle (normal) year outright
and only falls back to averaging when there are just 2 data points to
begin with.

PER-INPUT FALLBACK (the Director's own instruction): each of the 7
inputs below (ROIC, ROE, net margin, revenue growth, earnings growth,
FCF sign, debt/equity) is computed independently - if a particular
input has NO usable fiscal-year data at all (a missing row, an ETF/fund
with no income statement, a ticker with only 1 year of history and that
one year's own row missing), THAT INPUT ALONE falls back to today's
live `.info` figure (the exact same single-point read get_quality_
score() itself already uses for it), flagged in `meta["fallback_fields"]`
- never a blanket "give up on the whole ticker" default. A ticker with
no usable statements at all (every input falls back) behaves exactly
like get_quality_score()'s own `defaulted=True` case - quality_v2 ends
up identical to quality_v1's own score, which is the correct, honest
answer when there is nothing better to compute from.

FINANCIALS-MODE TICKERS (banks/insurers): excluded from the statement-
based computation entirely, same as portfolio_health_engine.py's
Evolution engine already excludes them from Growth/Margins/FCF/Debt Δ
(TASK 16c) - "Total Revenue"/"Operating Cash Flow"/"Capital Expenditure"
don't mean the same thing for a bank's balance-sheet-driven business as
they do for an operating company (see financials_classifier.py's own
module docstring). Every input falls back to today's `.info` value for
these tickers, by the same per-field mechanism above - not a special
case, just every field landing in the "no usable statement data for
this ticker" branch on purpose.

SHADOW ONLY: this module is never called from analyze_ticker_lite()
itself (the shared row-builder also used by digest_engine's weekly
watchlist email) - a full multi-year statement fetch is new, real cost
this task's own instruction never asked the digest email to pay for.
nightly_scan.py's two real scan paths call get_quality_score_v2() as an
ADDITIVE, in-place row attachment (_attach_quality_v2(), the same
pattern _attach_moat()/_attach_dividend_payout() already use), reusing
the SAME fundamentals_data.get_bundle() call those two functions already
made moments earlier for this ticker (cached - zero extra network cost).
The live "Quality"/"Quality Default" fields, Value Score, Top 200
selection and every public page are completely untouched by this module
- see nightly_scan.py's own call-site comment and top100_engine.py's
simulate_pool_v2().
"""

import statistics

import auto_compounder_engine
import financials_classifier

# Must track auto_quality_engine.get_quality_score()'s own (locally-
# scoped, not importable) PROFIT_GATE_CAP/GROWTH_CLAMP constants - "same
# structure, weights, clamps and profitability gate" per the task's own
# instruction. Duplicated here, not re-derived differently, and these
# two numbers are the only thing that would ever need to change if that
# function's own clamps ever did.
PROFIT_GATE_CAP = 55
GROWTH_CLAMP = 0.50

# How many of the most recent fiscal years (by real calendar date, not
# column position) feed the median for every input below.
FISCAL_YEARS_WINDOW = 3

# The 7 inputs get_quality_score_v2() computes from statements, in a
# fixed order - meta["fallback_fields"] names a subset of these exact
# strings.
_INPUT_FIELDS = (
    "roic", "roe", "net_margin", "revenue_growth", "earnings_growth",
    "fcf_sign", "debt_to_equity",
)

# TASK 45 item 1/2 (Director-directed, 12 Nov 2026, 12 Nov live preview
# review): ROE had no equity-sign check and no plausibility clamp at
# all - unlike ROIC just below it, which already clamps to +-100%
# (max(-1.0, min(ni / invested, 1.0))). A company with negative or
# near-zero book equity (heavy buybacks - AAL/HCA/MTCH/SMG/CCI/INSE/
# KSPI; or a highly-levered mortgage REIT whose Yahoo-reported .info
# ROE can be JUST as distorted - ORC/NREF/REFI) produces a huge or
# sign-flipped ROE (a loss divided by negative equity reads POSITIVE),
# inflating the score by up to +20 points (roe * 100 * 0.20). 100%
# (1.0) is the same ceiling ROIC already uses, chosen for consistency,
# not independently re-derived.
ROE_PLAUSIBLE_MAX = 1.0


def _median_of_available(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return statistics.median(vals)


def _plausible_roe(value):
    """None if `value` is missing, non-numeric, or its magnitude
    exceeds ROE_PLAUSIBLE_MAX (>100%) - applied to BOTH the per-fiscal-
    year statement-derived figure AND the .info["returnOnEquity"]
    fallback, since a negative/near-zero book equity distorts either
    source the same way (Yahoo's own reported ROE is computed from the
    SAME balance sheet, so it is no more trustworthy than the
    statement-derived figure once equity is broken). Treating it as
    unavailable here means it flows through this module's own existing
    per-input fallback/median machinery exactly like any other missing
    value - never a separate, parallel "sanity override" path."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if abs(value) > ROE_PLAUSIBLE_MAX:
        return None
    return value


def _growth_rate_v2(dated_values):
    """CAGR across up to FISCAL_YEARS_WINDOW fiscal years of
    {date: value_or_None} when the newest and oldest usable values are
    both positive (true CAGR, annualised by the real calendar gap
    between them); otherwise the mean of each consecutive year-over-
    year rate (cur - prior) / abs(prior) - the same tolerant formula
    portfolio_health_engine._evolution_growth() already uses for a
    value that CAN be negative (earnings/normalized income, unlike
    revenue). Clamped to +-GROWTH_CLAMP, exactly like today's single-
    point growth inputs. None if fewer than 2 usable (date, value)
    pairs exist at all - that specific input then falls back to
    today's live growth figure, per this module's own per-input
    fallback rule."""
    pairs = sorted(
        ((d, v) for d, v in dated_values.items() if v is not None),
        reverse=True,
    )
    if len(pairs) < 2:
        return None
    newest_date, newest_val = pairs[0]
    oldest_date, oldest_val = pairs[-1]
    if newest_val > 0 and oldest_val > 0 and newest_date != oldest_date:
        years = max(1.0, (newest_date - oldest_date).days / 365.25)
        rate = (newest_val / oldest_val) ** (1.0 / years) - 1.0
    else:
        yoy = []
        for (d_cur, v_cur), (d_prior, v_prior) in zip(pairs, pairs[1:]):
            if v_prior:
                yoy.append((v_cur - v_prior) / abs(v_prior))
        if not yoy:
            return None
        rate = sum(yoy) / len(yoy)
    return max(-GROWTH_CLAMP, min(rate, GROWTH_CLAMP))


def _fy_dates(income):
    """Up to the latest FISCAL_YEARS_WINDOW annual-statement dates,
    newest first - anchored on the income statement's own "revenue" row
    (the most universally present row across every provider/sector),
    same "pick the anchor row, look everything else up by date"
    convention portfolio_health_engine.py's Evolution engine already
    uses (fy_dates = sorted(ni_by_date.keys(), ...))."""
    if income is None:
        return []
    dates = sorted(auto_compounder_engine.series_by_date(income, "revenue").keys(), reverse=True)
    return dates[:FISCAL_YEARS_WINDOW]


def get_quality_score_v2(ticker, bundle, info=None):
    """(score, meta) - see this module's own docstring for the full
    design. `bundle` is fundamentals_data.get_bundle(ticker)'s own
    return shape ({"income","balance","cashflow","info",...}) - the
    caller passes in whatever it already fetched; this function makes
    no network call of its own. `info` is the live yfinance `.info`
    dict (optional - read from `bundle["info"]` when not given
    separately), used ONLY for this module's own per-input fallback
    values, never blended into the statement-based figures themselves.

    meta = {
        "fiscal_years_used": [...],      # ISO date strings actually
                                          # read from the income
                                          # statement, newest first
        "fallback_fields": [...],        # subset of _INPUT_FIELDS that
                                          # had no usable statement data
                                          # and fell back to today's
                                          # live .info value instead
        "defaulted": bool,               # every single input fell back
                                          # (no usable statements at
                                          # all for this ticker) - same
                                          # meaning as get_quality_
                                          # score()'s own `defaulted`
        "financials_mode": bool,         # excluded from the statement
                                          # read on purpose (bank/
                                          # insurer) - every field is
                                          # necessarily a fallback
        "roic": float, "roe": float, "net_margin": float,
        "revenue_growth": float, "earnings_growth": float,
        "fcf_positive": bool or None, "debt_to_equity": float,
    }
    Never raises - (today's v1 score via the fallback-info path, an
    all-fallback meta) on any unexpected error, so one bad ticker's
    bundle can never break a nightly scan run."""
    try:
        return _get_quality_score_v2(ticker, bundle, info)
    except Exception:
        return _fallback_only_score(info or {})


def _fallback_only_score(info):
    from auto_quality_engine import get_quality_score as _v1
    score, defaulted = _v1(None, info=info)
    return score, {
        "fiscal_years_used": [], "fallback_fields": list(_INPUT_FIELDS),
        "defaulted": True, "financials_mode": False,
        "roic": None, "roe": None, "net_margin": None,
        "revenue_growth": None, "earnings_growth": None,
        "fcf_positive": None, "debt_to_equity": None,
    }


def _is_mortgage_reit(info):
    """TASK 40 (Director-directed, 8 Nov 2026, review finding): True
    for Yahoo's "REIT - Mortgage" industry (ORC/NLY/AGNC/DX/NREF/REFI
    and similar) - a balance-sheet LENDER (borrows short, holds a
    leveraged book of mortgage-backed securities/loans), economically
    a bank, not an operating real-estate owner. financials_classifier.
    is_financials()'s own REIT/real-estate exclusion (2 Oct 2026,
    owner-directed) is unconditional on "reit"/"real estate" appearing
    anywhere in the industry string - correct for an equity/property
    REIT (its FCF/ROIC are still meaningful), wrong for a mortgage
    REIT (net income/ROE is the only meaningful read, exactly like a
    bank - see financials_classifier.py's own module docstring on
    Fable's principle). Scoped to Quality v2 ONLY, per the Director's
    own explicit instruction - financials_classifier.is_financials()
    itself (moat_engine.py's ROIC/ROE pillar choice, fcf_valuation_
    engine.py's DCF path) is deliberately NOT touched here; widening
    this to those callers is a separate decision this task doesn't
    make. Matches on "reit" AND "mortgage" both present (case-
    insensitive substring), robust to Yahoo's own minor punctuation
    variance ("REIT - Mortgage", "REIT—Mortgage") rather than an exact
    string match."""
    industry = (info.get("industry") or "").lower()
    return "reit" in industry and "mortgage" in industry


def _get_quality_score_v2(ticker, bundle, info):
    bundle = bundle or {}
    info = info if info is not None else (bundle.get("info") or {})
    income, balance, cashflow = bundle.get("income"), bundle.get("balance"), bundle.get("cashflow")

    financials_mode = financials_classifier.is_financials(info, ticker=ticker) or _is_mortgage_reit(info)
    fy_dates = [] if financials_mode else _fy_dates(income)

    fallback_fields = []

    # Per-fiscal-year normalized income (the one-off exclusion every
    # other statement-based input below is built on).
    ni_by_fy = {fy: auto_compounder_engine.normalized_income_for_fy(income, fy)[0] for fy in fy_dates}
    revenue_by_fy = auto_compounder_engine.series_by_date(income, "revenue") if fy_dates else {}
    equity_by_fy = auto_compounder_engine.series_by_date(balance, "stockholders_equity") if fy_dates else {}
    debt_by_fy = auto_compounder_engine.series_by_date(balance, "total_debt") if fy_dates else {}
    ocf_by_fy = auto_compounder_engine.series_by_date(cashflow, "operating_cash_flow") if fy_dates else {}
    capex_by_fy = auto_compounder_engine.series_by_date(cashflow, "capex") if fy_dates else {}
    fcf_row_by_fy = auto_compounder_engine.series_by_date(cashflow, "free_cash_flow") if fy_dates else {}

    def _fcf_for(fy):
        o, c = ocf_by_fy.get(fy), capex_by_fy.get(fy)
        if o is not None and c is not None:
            return o + c
        return fcf_row_by_fy.get(fy)

    # ROE: normalized income / stockholders equity, per FY, medianed.
    # TASK 45 item 1 (Director-directed, 12 Nov 2026): a FY with
    # non-positive equity is skipped entirely (equity_by_fy[fy] > 0,
    # not merely truthy - a negative value is truthy in Python and
    # would otherwise sign-flip the ratio), and any individual FY's
    # ratio that's still implausible (> +-100%, e.g. from tiny-but-
    # positive equity) is dropped via _plausible_roe() rather than
    # feeding the median - exactly the "treat as unavailable" rule the
    # Director asked for, never a clamp that would keep a distorted
    # number in the mix.
    roe_vals = []
    for fy in fy_dates:
        ni = ni_by_fy.get(fy)
        eq = equity_by_fy.get(fy)
        if ni is None or eq is None or eq <= 0:
            continue
        fy_roe = _plausible_roe(ni / eq)
        if fy_roe is not None:
            roe_vals.append(fy_roe)
    roe = _median_of_available(roe_vals)
    if roe is None:
        # TASK 45 item 2 (Director-directed, 12 Nov 2026): the SAME
        # plausibility guard applies to the .info fallback value too -
        # this is the path a financials-mode/mortgage-REIT ticker
        # (ORC/NREF/REFI) actually takes (fy_dates is forced empty for
        # them), and Yahoo's own reported returnOnEquity can be just as
        # distorted by negative/near-zero book equity as a statement-
        # derived figure - never assumed sane just because it came from
        # .info rather than a statement. An implausible fallback value
        # becomes 0 (neutral), the SAME "contributes nothing" outcome
        # v1 already has for a ticker Yahoo reports no ROE for at all.
        roe = _plausible_roe(info.get("returnOnEquity")) or 0
        fallback_fields.append("roe")

    # ROIC: normalized income / (stockholders equity + total debt),
    # per FY, medianed - REAL book equity from the balance sheet, not
    # v1's marketCap/priceToBook proxy (a market-price-based estimate
    # that this statement-based score has no need for once the real
    # figure is available; the fallback path below still uses v1's
    # exact proxy, since that's what "today's value" means for ROIC).
    roic_vals = []
    for fy in fy_dates:
        ni = ni_by_fy.get(fy)
        eq = equity_by_fy.get(fy)
        if ni is None or eq is None:
            continue
        invested = eq + (debt_by_fy.get(fy) or 0.0)
        if invested > 0:
            roic_vals.append(max(-1.0, min(ni / invested, 1.0)))
    roic = _median_of_available(roic_vals)
    if roic is None:
        roic = 0.0
        market_cap = info.get("marketCap", 0) or 0
        price_to_book = info.get("priceToBook", 0) or 0
        total_debt_info = info.get("totalDebt", 0) or 0
        net_income_info = info.get("netIncomeToCommon", 0) or 0
        if market_cap > 0 and price_to_book > 0:
            book_equity = market_cap / price_to_book
            invested_capital = book_equity + total_debt_info
            if invested_capital > 0:
                roic = max(-1.0, min(net_income_info / invested_capital, 1.0))
        fallback_fields.append("roic")

    # Net margin: normalized income / revenue, per FY, medianed.
    margin_vals = [
        (ni_by_fy.get(fy) / revenue_by_fy[fy])
        for fy in fy_dates
        if ni_by_fy.get(fy) is not None and revenue_by_fy.get(fy)
    ]
    net_margin = _median_of_available(margin_vals)
    if net_margin is None:
        net_margin = info.get("profitMargins", 0) or 0
        fallback_fields.append("net_margin")

    # Revenue growth / earnings growth: CAGR (fallback to mean YoY) over
    # the fiscal-year window, clamped.
    revenue_growth = _growth_rate_v2(revenue_by_fy) if fy_dates else None
    if revenue_growth is None:
        revenue_growth = max(-GROWTH_CLAMP, min(info.get("revenueGrowth", 0) or 0, GROWTH_CLAMP))
        fallback_fields.append("revenue_growth")

    earnings_growth = _growth_rate_v2(ni_by_fy) if fy_dates else None
    if earnings_growth is None:
        earnings_growth = max(-GROWTH_CLAMP, min(info.get("earningsGrowth", 0) or 0, GROWTH_CLAMP))
        fallback_fields.append("earnings_growth")

    # FCF sign: median FCF across the fiscal-year window, sign only -
    # mirrors get_quality_score()'s own binary (not continuous) FCF
    # treatment.
    fcf_vals = [_fcf_for(fy) for fy in fy_dates]
    fcf_median = _median_of_available(fcf_vals)
    if fcf_median is None:
        fcf_median = info.get("freeCashflow", 0) or 0
        fallback_fields.append("fcf_sign")
    if fcf_median > 0:
        fcf_positive = True
    elif fcf_median < 0:
        fcf_positive = False
    else:
        fcf_positive = None

    # Debt/equity: total debt / stockholders equity * 100, per FY,
    # medianed - same percent-like scale yfinance's own debtToEquity
    # field already uses (0 missing debt treated as zero debt, not
    # missing - the common case for a debt-free compounder).
    debt_eq_vals = [
        ((debt_by_fy.get(fy) or 0.0) / equity_by_fy[fy]) * 100
        for fy in fy_dates
        if equity_by_fy.get(fy)
    ]
    debt_to_equity = _median_of_available(debt_eq_vals)
    if debt_to_equity is None:
        debt_to_equity = info.get("debtToEquity", 0) or 0
        fallback_fields.append("debt_to_equity")

    if financials_mode:
        fallback_fields = list(_INPUT_FIELDS)

    score = 50
    score += roic * 100 * 0.25
    score += roe * 100 * 0.20
    score += net_margin * 100 * 0.15
    score += revenue_growth * 100 * 0.15
    score += earnings_growth * 100 * 0.15
    if fcf_positive is True:
        score += 10
    elif fcf_positive is False:
        score -= 5
    debt_penalty = min(debt_to_equity * 0.05, 15)
    score -= debt_penalty
    score = max(min(round(score), 100), 0)

    ni_median = _median_of_available([v for v in ni_by_fy.values() if v is not None]) if fy_dates else None
    loss_making = (
        net_margin < 0
        or (ni_median if ni_median is not None else (info.get("netIncomeToCommon", 0) or 0)) < 0
        or (fcf_median or 0) < 0
    )
    if loss_making:
        score = min(score, PROFIT_GATE_CAP)

    return score, {
        "fiscal_years_used": [str(d) for d in fy_dates],
        "fallback_fields": fallback_fields,
        "defaulted": len(fallback_fields) >= len(_INPUT_FIELDS),
        "financials_mode": financials_mode,
        "roic": roic, "roe": roe, "net_margin": net_margin,
        "revenue_growth": revenue_growth, "earnings_growth": earnings_growth,
        "fcf_positive": fcf_positive, "debt_to_equity": debt_to_equity,
    }
