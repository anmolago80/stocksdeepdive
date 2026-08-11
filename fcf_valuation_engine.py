"""
Discounted Free Cash Flow (DCF) intrinsic value.

Values a company on the cash it actually throws off rather than an assumed
multiple. Every input is *sourced* from the stock's own data - nothing here is
a hand-entered per-stock number.

Model (a standard two-stage DCF):
    1. Base = latest annual free cash flow per share (from the cash-flow
       statement, else info["freeCashflow"], else a per-stock manual override).
    2. Grow it for GROWTH_YEARS at an estimated growth rate, discounting each
       year back to today at the discount rate.
    3. Add a Gordon-growth terminal value for everything beyond the horizon.
    intrinsic = sum(discounted stage-1 cash flows) + discounted terminal value

GROWTH RATE, in priority order:
    1. ANALYST CONSENSUS - Yahoo's own "Next 5 Years (per annum)" analyst
       growth estimate for this specific stock, when Yahoo has coverage
       (capm_engine.get_growth_estimates_5y). This is the only input here
       that comes from outside the stock's own reported financials.
    2. HISTORICAL FCF CAGR   - compound annual growth of free cash flow across
       the available years of the cash-flow statement, but only when it's a
       CLEAN, POSITIVE signal (low year-to-year volatility).
    3. info["earningsGrowth"] / info["revenueGrowth"] - single-point fallback
       when there isn't enough clean FCF history and no analyst coverage.
    4. DEFAULT_GROWTH (a conservative sector-neutral average) - used only when
       nothing else is available. When this happens the value is flagged as a
       DEFAULT so the app can render it in red.

DISCOUNT RATE - CAPM cost of equity, computed per stock (capm_engine.py):
    discount_rate = risk_free_rate(currency) + beta * equity_risk_premium
This replaces the old flat 9% for every stock with a rate that reflects that
stock's own risk (its beta) and the bond yield of its own currency.

TERMINAL / PERPETUAL GROWTH RATE - tied to the stock's own CURRENCY
(capm_engine.PERPETUAL_GROWTH_BY_CCY), on the reasoning that no company can
out-grow its own economy forever in a Gordon-growth model. Replaces the old
flat 3% for every stock.

Defaults:
    growth_years     = 10
    DEFAULT_GROWTH   = 0.05   (5% - the "assume an average" fallback)

All three key inputs (growth, discount, perpetual) remain parameters - the
app's "Valuation & FCF inputs" panel passes an explicit value when Auto mode
is off, or when a per-stock override is set, and it's used as-is instead of
being auto-calculated. A per-stock manual FCF can also be supplied so the
model still does the maths, it just starts from a cash-flow figure you trust.

Returns (intrinsic_value_per_share, growth_rate_used, meta) where meta records
where each input came from and whether a default/average had to be assumed.
"""

import yfinance as yf

import capm_engine

# Kept only as an absolute last-resort fallback if capm_engine itself throws
# (e.g. both the live and fallback risk-free lookups somehow fail).
DEFAULT_DISCOUNT_RATE = 0.09
DEFAULT_PERPETUAL_RATE = 0.03
DEFAULT_GROWTH_YEARS = 10

# The "assume an average" fallback growth, used only when neither historical
# FCF nor info-based growth is available. Flagged as a default when used.
DEFAULT_GROWTH = 0.05

# Clamp estimated growth into a defensible band: no negative compounding in
# stage 1, and a ceiling so a hot trailing number can't produce a fantasy
# valuation. Terminal growth must stay below the discount rate or Gordon blows
# up.
GROWTH_FLOOR = 0.00
GROWTH_CEIL = 0.20

# Market-cap-tiered version of the ceiling above. A flat 20% ceiling let a
# mega-cap grow FCF at nearly the same clip as a micro-cap for a full
# 10-year stage-1 horizon - structurally implausible (compounding off a huge
# revenue base runs out of room well before a small company would). The
# ceiling tightens as market cap grows; small caps keep the original 20%
# headroom.
#
# Thresholds are USD-equivalent so they're consistent across currencies.
# FX_TO_USD_APPROX is a rough, static snapshot used only to BUCKET a company
# into a size tier - not a live rate - so being off by a few percent doesn't
# change which tier a company lands in except right at a boundary, which is
# an acceptable edge case for a sanity-backstop ceiling.
FX_TO_USD_APPROX = {
    "USD": 1.0,
    "AUD": 0.65,
}

# (min USD market cap, ceiling) pairs, largest threshold first - the first
# one a company's market cap clears wins.
MARKET_CAP_GROWTH_CEILINGS = [
    (200_000_000_000, 0.08),   # mega-cap
    (10_000_000_000, 0.12),    # large-cap
    (2_000_000_000, 0.16),     # mid-cap
    (0, 0.20),                 # small-cap / fallback bucket
]


def growth_ceiling_for(info, currency=None):
    """
    Market-cap-tiered growth ceiling - replaces the flat GROWTH_CEIL as the
    upper bound estimate_growth() (and a manual override) is clamped to.
    Falls back to the original flat GROWTH_CEIL whenever market cap isn't
    available - same fail-safe philosophy as everything else in this
    module: a missing data point should never block a valuation, just make
    it slightly less size-aware.
    """
    info = info or {}
    market_cap = info.get("marketCap")
    if not market_cap or market_cap <= 0:
        return GROWTH_CEIL

    ccy = (currency or info.get("currency") or "USD").upper()
    market_cap_usd = market_cap * FX_TO_USD_APPROX.get(ccy, 1.0)

    for threshold, ceiling in MARKET_CAP_GROWTH_CEILINGS:
        if market_cap_usd >= threshold:
            return ceiling
    return GROWTH_CEIL

# Cash-flow-statement row labels vary across yfinance versions / listings.
_FCF_LABELS = ("Free Cash Flow", "FreeCashFlow", "Free cash flow")
_OCF_LABELS = (
    "Operating Cash Flow", "Total Cash From Operating Activities",
    "OperatingCashFlow", "Cash Flow From Continuing Operating Activities",
)
_CAPEX_LABELS = ("Capital Expenditure", "CapitalExpenditures", "Capital Expenditures")


def _row(cashflow_df, labels):
    """Return the first matching row (as a list of floats, most-recent-first)
    from a yfinance cash-flow DataFrame, or None."""
    if cashflow_df is None or getattr(cashflow_df, "empty", True):
        return None
    for label in labels:
        if label in cashflow_df.index:
            try:
                vals = [float(v) for v in cashflow_df.loc[label].tolist()]
                return vals
            except Exception:
                continue
    return None


def extract_fcf_history(cashflow_df):
    """
    Pull a free-cash-flow series (most recent first) out of a yfinance
    cash-flow statement. Uses the explicit Free Cash Flow row if present,
    otherwise reconstructs it as Operating Cash Flow + Capital Expenditure
    (CapEx is reported negative, so addition nets it out).

    Returns a list of annual FCF values (most recent first), or [] if the
    statement isn't usable.
    """
    fcf = _row(cashflow_df, _FCF_LABELS)
    if fcf:
        cleaned = [v for v in fcf if v == v]  # drop NaN
        if len(cleaned) >= 2:
            return cleaned
        # else: FCF row present but too sparse - fall through to rebuild it.

    ocf = _row(cashflow_df, _OCF_LABELS)
    capex = _row(cashflow_df, _CAPEX_LABELS)
    if ocf and capex:
        n = min(len(ocf), len(capex))
        out = []
        for i in range(n):
            o, c = ocf[i], capex[i]
            if o == o and c == c:
                out.append(o + c)  # capex is negative in the statement
        if out:
            return out

    # Last resort: whatever single FCF point we could salvage (not enough for
    # a CAGR, but the caller can still use it as the base cash flow).
    if fcf:
        return [v for v in fcf if v == v]
    return []


def _mean(xs):
    xs = [x for x in xs if x == x]
    return sum(xs) / len(xs) if xs else 0.0


def _coeff_of_variation(series):
    """Volatility measure: stdev / mean-of-absolute-values. High = noisy, so
    a CAGR between two endpoints can't be trusted."""
    series = [x for x in series if x == x]
    if len(series) < 2:
        return 999.0
    scale = _mean([abs(x) for x in series])
    if scale == 0:
        return 999.0
    m = _mean(series)
    var = _mean([(x - m) ** 2 for x in series])
    return (var ** 0.5) / scale


def normalized_base_and_series(cashflow_df, info=None):
    """
    Produce a *normalised* current free cash flow and an FCF series for growth.

    The single most recent year is a bad base when a company had a one-off
    capex spike (e.g. COH building a new plant): that year's FCF craters and,
    if used raw, both collapses the DCF level AND turns the growth CAGR
    negative. To avoid that we NORMALISE capex - subtract the AVERAGE capex
    (over the available years) from the latest operating cash flow - so a
    single heavy investment year doesn't define the whole valuation.

    Returns (base_fcf, fcf_series_recent_first, source) where source is one of
    "ocf-normcapex" | "fcf-median" | "info" | "none".
    """
    info = info or {}

    ocf = _row(cashflow_df, _OCF_LABELS)
    capex = _row(cashflow_df, _CAPEX_LABELS)
    ocf = [v for v in (ocf or []) if v == v]
    capex = [v for v in (capex or []) if v == v]

    if len(ocf) >= 2 and len(capex) >= 1:
        avg_capex = _mean(capex)                 # capex is negative in statements
        series = [o + avg_capex for o in ocf]    # OCF - normalised capex
        base = ocf[0] + avg_capex                # latest OCF, normalised capex
        return base, series, "ocf-normcapex"

    # Fall back to the reported FCF line. Use the MEDIAN of the last few years
    # as the base so one outlier year doesn't dominate; keep the raw series for
    # the growth estimate.
    fcf = extract_fcf_history(cashflow_df)
    if fcf:
        recent = sorted(fcf[:3])
        base = recent[len(recent) // 2]          # median of up to 3 latest
        return base, fcf, "fcf-median"

    info_fcf = info.get("freeCashflow", 0) or 0
    if info_fcf > 0:
        return info_fcf, [], "info"

    return None, [], "none"


def growth_from_history(fcf_history):
    """
    Compound annual growth rate of free cash flow across the available years.

    fcf_history is most-recent-first (yfinance order). Needs at least two
    positive endpoints to be meaningful. Returns a decimal growth rate, or
    None if history can't support an estimate.
    """
    if not fcf_history or len(fcf_history) < 2:
        return None

    # Reorder oldest -> newest for a clean CAGR.
    series = list(reversed(fcf_history))
    oldest, newest = series[0], series[-1]
    years = len(series) - 1

    # CAGR only makes sense between two positive endpoints.
    if oldest is None or newest is None or oldest <= 0 or newest <= 0:
        return None

    try:
        cagr = (newest / oldest) ** (1.0 / years) - 1.0
    except Exception:
        return None
    return cagr


def estimate_growth(info, fcf_series=None, analyst_growth=None, ceiling=None):
    """
    Estimate a stage-1 growth rate and report where it came from.

    When BOTH a Yahoo analyst 5-year growth estimate AND a clean historical
    FCF CAGR are available, the growth rate used is the MIN of the two - i.e.
    whichever of "what Yahoo's analysts expect" and "what the company has
    actually delivered over the last 5 years" is more conservative wins. This
    targets a real problem: Yahoo's 5-year analyst estimate is a thin,
    sometimes single-analyst-driven field that regularly comes in
    unrealistically high (e.g. 20%+ per year for a mature, mega-cap
    business) - taking the min against actual historical FCF growth stops
    that from silently driving the number on its own.

    `ceiling` overrides the module-level GROWTH_CEIL - pass the result of
    growth_ceiling_for(info, currency) to apply the market-cap-tiered
    ceiling (defaults to the flat GROWTH_CEIL if not given, e.g. for
    callers/tests that don't need size-awareness).

    The returned `governor` tells you what actually determined the FINAL
    (post-ceiling) number:
        "Yahoo"   - the analyst estimate was the smaller/only signal, and it
                    wasn't capped.
        "History" - the historical FCF CAGR was the smaller/only signal, and
                    it wasn't capped.
        "Info"    - fell back to reported earningsGrowth/revenueGrowth.
        "Default" - fell back to DEFAULT_GROWTH (flagged red in the UI).
        "Cap"     - whichever signal would otherwise have been used was
                    ABOVE the ceiling for this company's market-cap tier, so
                    the ceiling itself is what's actually driving the
                    number, not Yahoo or History.

    Priority when only one signal is available:
        1. "analyst" - Yahoo's estimate alone, if history isn't clean/usable.
        2. "history" - historical FCF CAGR alone, if Yahoo has no coverage,
           but ONLY when it's a CLEAN, POSITIVE signal (low year-to-year
           volatility). A noisy or negative CAGR (often a capex-spike
           artifact) is not trusted.
        3. "info"    - reported earningsGrowth / revenueGrowth.
        4. "history" (non-negative) as a last numeric resort, else
        5. "default" - DEFAULT_GROWTH average (flagged red in the UI).

    Returns (growth_rate, source, governor), clamped to
    [GROWTH_FLOOR, ceiling].
    """
    info = info or {}
    ceiling = GROWTH_CEIL if ceiling is None else ceiling

    def _finalize(raw_rate, source, natural_governor):
        governor = "Cap" if raw_rate > ceiling else natural_governor
        return max(GROWTH_FLOOR, min(raw_rate, ceiling)), source, governor

    g = growth_from_history(fcf_series)
    clean = (
        g is not None
        and g > 0
        and fcf_series
        and _coeff_of_variation(fcf_series) <= 0.60
    )

    if analyst_growth is not None and clean:
        combined = min(analyst_growth, g)
        natural_governor = "Yahoo" if analyst_growth <= g else "History"
        return _finalize(combined, "analyst+history", natural_governor)

    if analyst_growth is not None:
        return _finalize(analyst_growth, "analyst", "Yahoo")

    if clean:
        return _finalize(g, "history", "History")

    for key in ("earningsGrowth", "revenueGrowth"):
        val = info.get(key)
        if val is not None:
            return _finalize(val, "info", "Info")

    if g is not None and g >= 0:
        return _finalize(g, "history", "History")

    return DEFAULT_GROWTH, "default", "Default"


def dcf_intrinsic_value(
    ticker,
    info=None,
    cashflow_df=None,
    currency=None,
    discount_rate=None,
    perpetual_rate=None,
    growth_rate=None,
    growth_years=DEFAULT_GROWTH_YEARS,
    manual_fcf=None,
):
    """
    Returns (intrinsic_value_per_share, growth_rate_used, meta).

    intrinsic_value is 0 when FCF or shares are unavailable/non-positive, so
    callers can fall back to another method.

    discount_rate / perpetual_rate / growth_rate each default to None, which
    means "auto-calculate" (CAPM / currency-based / analyst-or-history). Pass
    an explicit number for any of the three (from the app's Valuation & FCF
    panel, global or per-stock) and it's used as-is instead.

    meta = {
        "growth_source":    "analyst" | "history" | "analyst+history" | "info" | "default" | "manual",
        "growth_governor":  "Yahoo" | "History" | "Info" | "Default" | "Manual" | "Cap" | None,
        "growth_ceiling_used": float,  # the market-cap-tiered ceiling actually applied
        "fcf_source":       "history" | "info" | "manual" | "none",
        "discount_source":  "capm" | "capm-default" | "manual" | "fallback",
        "perpetual_source": "currency" | "manual" | "fallback",
        "growth_default": bool,   # True when the average growth had to be used
        "defaulted":      bool,   # True if any core input was an assumption
    }
    """
    meta = {
        "growth_source": None,
        "growth_governor": None,
        "growth_ceiling_used": None,
        "fcf_source": "none",
        "discount_source": None,
        "perpetual_source": None,
        "growth_default": False,
        "defaulted": False,
        "discount_rate_used": None,
        "perpetual_rate_used": None,
    }

    try:
        if info is None:
            info = yf.Ticker(ticker).info or {}
        currency = currency or info.get("currency") or "USD"

        shares = info.get("sharesOutstanding", 0) or 0
        if shares <= 0:
            return 0, None, meta

        # --- Base free cash flow -------------------------------------------
        # 1) user-supplied manual override, else 2) a capex-NORMALISED base
        # (latest operating cash flow minus AVERAGE capex) so a one-off capex
        # spike doesn't collapse the valuation.
        norm_base, fcf_series, base_src = normalized_base_and_series(
            cashflow_df, info=info
        )

        if manual_fcf is not None and manual_fcf > 0:
            fcf = float(manual_fcf)
            meta["fcf_source"] = "manual"
        elif norm_base is not None and norm_base > 0:
            fcf = norm_base
            meta["fcf_source"] = base_src
        else:
            fcf = info.get("freeCashflow", 0) or 0
            meta["fcf_source"] = "info" if fcf > 0 else "none"

        if fcf <= 0:
            return 0, None, meta

        # --- Discount rate (CAPM, per stock) --------------------------------
        if discount_rate is not None:
            meta["discount_source"] = "manual"
        else:
            try:
                discount_rate, capm_meta = capm_engine.resolve_discount_rate(info, currency)
                meta["discount_source"] = "capm-default" if capm_meta["defaulted"] else "capm"
            except Exception:
                discount_rate = DEFAULT_DISCOUNT_RATE
                meta["discount_source"] = "fallback"

        # --- Terminal / perpetual growth rate (currency-based) --------------
        if perpetual_rate is not None:
            meta["perpetual_source"] = "manual"
        else:
            try:
                perpetual_rate = capm_engine.resolve_perpetual_rate(currency, discount_rate)
                meta["perpetual_source"] = "currency"
            except Exception:
                perpetual_rate = DEFAULT_PERPETUAL_RATE
                meta["perpetual_source"] = "fallback"

        # Guard: terminal growth must be strictly below the discount rate.
        if perpetual_rate >= discount_rate:
            perpetual_rate = max(0.0, discount_rate - 0.01)

        meta["discount_rate_used"] = round(discount_rate, 4)
        meta["perpetual_rate_used"] = round(perpetual_rate, 4)

        # --- Growth rate ----------------------------------------------------
        growth_ceiling = growth_ceiling_for(info, currency)
        meta["growth_ceiling_used"] = growth_ceiling
        if growth_rate is not None:
            capped = growth_rate > growth_ceiling
            growth_rate = max(GROWTH_FLOOR, min(growth_rate, growth_ceiling))
            meta["growth_source"] = "manual"
            meta["growth_governor"] = "Cap" if capped else "Manual"
        else:
            analyst_growth = None
            try:
                analyst_growth = capm_engine.get_growth_estimates_5y(ticker)
            except Exception:
                analyst_growth = None
            growth_rate, gsrc, governor = estimate_growth(
                info, fcf_series=fcf_series, analyst_growth=analyst_growth,
                ceiling=growth_ceiling)
            meta["growth_source"] = gsrc
            meta["growth_governor"] = governor
            if gsrc == "default":
                meta["growth_default"] = True
                meta["defaulted"] = True

        fcf_per_share = fcf / shares

        # Stage 1: discount each year's grown cash flow back to today.
        intrinsic = 0.0
        cash_flow = fcf_per_share
        for year in range(1, growth_years + 1):
            cash_flow = cash_flow * (1 + growth_rate)
            intrinsic += cash_flow / ((1 + discount_rate) ** year)

        # Stage 2: Gordon terminal value on the final year's cash flow.
        terminal_cf = cash_flow * (1 + perpetual_rate)
        terminal_value = terminal_cf / (discount_rate - perpetual_rate)
        intrinsic += terminal_value / ((1 + discount_rate) ** growth_years)

        return round(intrinsic, 2), round(growth_rate, 4), meta

    except Exception:
        return 0, None, meta
