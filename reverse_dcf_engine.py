"""
reverse_dcf_engine.py

Services batch 2, Part 1 (2026-09-01): "What the price implies" - a reverse
DCF. Instead of the site's normal DCF (assume a growth rate, solve for a
fair value), this asks the opposite question: given the CURRENT PRICE, what
FCF growth rate would the site's own DCF model need to assume to justify
that price? That number is a description of what the market is pricing in,
not a forecast - it never gets its own red/green judgement, and it is
deliberately never labelled "cheap"/"expensive"/"buy"/"sell".

MECHANICS - reuses fcf_valuation_engine.dcf_intrinsic_value() for every
input (base FCF, shares -> FCF per share, the CAPM discount rate, the
currency-based perpetual rate, the 10-year growth horizon) rather than
re-deriving any of them: one call with growth_rate=<the same override the
Deep Dive/scan is already using, or None for auto> resolves ALL of those
inputs exactly the same way the on-screen Intrinsic Value figure does, so
the two numbers are always internally consistent (same base FCF, same
discount rate, same admin overrides via _dcf_overrides_for in app.py).

The ONE piece of real work this module does that dcf_intrinsic_value can't
do for us is the actual reverse solve. dcf_intrinsic_value's own
growth_rate parameter is clamped to [GROWTH_FLOOR (0.0), a market-cap-tiered
ceiling well under 60%] - reasonable for a forward-looking valuation (the
site should never assume a fantasy growth rate), but exactly wrong for a
reverse DCF, which must be able to report an implied growth rate ANYWHERE
in a wide band, including negative (a price below the zero-growth DCF
value implies negative growth is priced in - a real, useful fact, not an
error) and above the site's own forward-looking ceiling (the market can and
does price in unrealistic growth; that's the point of showing this card).
So _dcf_value_for_growth() below is a small, deliberately UNCLAMPED mirror
of dcf_intrinsic_value()'s stage-1/stage-2 discounting arithmetic (the
"Stage 1"/"Stage 2" block in fcf_valuation_engine.dcf_intrinsic_value,
lines ~709-719) - not a re-implementation of the MODEL (base FCF, discount
rate, perpetual rate, growth-rate estimation all still come from that one
dcf_intrinsic_value() call above, never recomputed here), just the bare
compounding formula evaluated at an arbitrary candidate growth rate so it
can be bisected. The formula is monotonically increasing in growth_rate
(higher assumed growth -> strictly higher discounted value, since FCF per
share is positive), so bisection over [GROWTH_MIN, GROWTH_MAX] is
well-posed and converges in well under 100 iterations for a 0.01% tolerance.
"""

from fcf_valuation_engine import dcf_intrinsic_value, DEFAULT_GROWTH_YEARS

# Bisection bounds and tolerance, per the instruction doc - deliberately far
# wider than fcf_valuation_engine's own forward-looking growth ceiling (this
# is describing what the market is pricing in, not what the site's model
# would ever assume).
GROWTH_MIN = -0.30
GROWTH_MAX = 0.60
TOLERANCE = 0.0001   # 0.01%, as an absolute decimal width on the bisection interval
_MAX_ITERATIONS = 100


def _dcf_value_for_growth(fcf_per_share, growth_rate, discount_rate, perpetual_rate, growth_years):
    """Bare, unclamped mirror of dcf_intrinsic_value()'s own stage-1/
    stage-2 discounting loop - see this module's docstring for why this
    one piece of arithmetic is duplicated rather than reused (the real
    function clamps growth_rate to a forward-looking band that a reverse
    solve must be able to search outside of)."""
    intrinsic = 0.0
    cash_flow = fcf_per_share
    for year in range(1, growth_years + 1):
        cash_flow = cash_flow * (1 + growth_rate)
        intrinsic += cash_flow / ((1 + discount_rate) ** year)
    terminal_cf = cash_flow * (1 + perpetual_rate)
    terminal_value = terminal_cf / (discount_rate - perpetual_rate)
    intrinsic += terminal_value / ((1 + discount_rate) ** growth_years)
    return intrinsic


def _solve_growth(fcf_per_share, price, discount_rate, perpetual_rate, growth_years):
    """Bisection for the growth rate g in [GROWTH_MIN, GROWTH_MAX] at which
    _dcf_value_for_growth(...) == price. Returns (g, capped) where capped
    is "low" if the price implies growth below GROWTH_MIN, "high" if it
    implies growth above GROWTH_MAX, else None (a real interior solution)."""
    v_lo = _dcf_value_for_growth(fcf_per_share, GROWTH_MIN, discount_rate, perpetual_rate, growth_years)
    v_hi = _dcf_value_for_growth(fcf_per_share, GROWTH_MAX, discount_rate, perpetual_rate, growth_years)

    if price <= v_lo:
        return GROWTH_MIN, "low"
    if price >= v_hi:
        return GROWTH_MAX, "high"

    lo, hi = GROWTH_MIN, GROWTH_MAX
    for _ in range(_MAX_ITERATIONS):
        if (hi - lo) < TOLERANCE:
            break
        mid = (lo + hi) / 2.0
        v_mid = _dcf_value_for_growth(fcf_per_share, mid, discount_rate, perpetual_rate, growth_years)
        if v_mid < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0, None


def compute(
    ticker,
    current_price,
    info=None,
    cashflow_df=None,
    currency=None,
    discount_rate=None,
    perpetual_rate=None,
    growth_rate=None,
    manual_fcf=None,
    growth_years=DEFAULT_GROWTH_YEARS,
):
    """
    Reverse-DCF "what the price implies" for one ticker.

    discount_rate/perpetual_rate/growth_rate/manual_fcf are the SAME
    optional overrides fcf_valuation_engine.dcf_intrinsic_value() and
    resolver_engine.resolve_intrinsic_value() accept (None = auto
    CAPM/currency/analyst-or-history) - pass the caller's own
    _dcf_overrides_for(ticker) tuple straight through so an admin's
    per-ticker DCF override changes both the on-screen Intrinsic Value AND
    this card consistently, exactly as the instruction requires. Note:
    growth_rate here, if given, only affects MODEL growth (g_model, what
    the model itself would assume) - the reverse solve always searches the
    full [GROWTH_MIN, GROWTH_MAX] band regardless, since the whole point is
    to find what growth the PRICE implies, independent of what the model
    would have guessed.

    Returns a dict:
        ok:                bool - False when there's no positive FCF base
                            to build a reverse DCF on at all (never a fake
                            number in that case).
        reason:             str | None - set when ok is False, e.g.
                            "no positive FCF base".
        implied_growth:     float | None - decimal (0.083 = 8.3%), the g*
                            the bisection solved for. Negative is valid.
        implied_growth_capped: "low" | "high" | None - set when the price
                            sits outside what [GROWTH_MIN, GROWTH_MAX] can
                            produce at all, so implied_growth is the bound
                            itself, not a true equality solution.
        model_growth:       float | None - decimal, the SAME growth rate
                            the on-screen Intrinsic Value figure assumed
                            (dcf_intrinsic_value's own growth_rate_used).
        base_fcf_per_share: float | None
        discount_rate:      float | None - decimal, CAPM-resolved or override.
        perpetual_rate:      float | None - decimal, currency-resolved or override.
        growth_years:        int
        price:               float
        value_default:       bool - passthrough of dcf_intrinsic_value's own
                            "defaulted" flag (red-flag rule: the base FCF
                            or another core input was itself an assumption).
        sentence:            str | None - the plain-English summary line.
    """
    out = {
        "ok": False,
        "reason": None,
        "implied_growth": None,
        "implied_growth_capped": None,
        "model_growth": None,
        "base_fcf_per_share": None,
        "discount_rate": None,
        "perpetual_rate": None,
        "growth_years": growth_years,
        "price": current_price,
        "value_default": False,
        "sentence": None,
    }

    if not current_price or current_price <= 0:
        out["reason"] = "no current price"
        return out

    _value, g_model, meta = dcf_intrinsic_value(
        ticker,
        info=info,
        cashflow_df=cashflow_df,
        currency=currency,
        discount_rate=discount_rate,
        perpetual_rate=perpetual_rate,
        growth_rate=growth_rate,
        growth_years=growth_years,
        manual_fcf=manual_fcf,
    )

    fcf_per_share = meta.get("fcf_per_share_used")
    d0 = meta.get("discount_rate_used")
    p0 = meta.get("perpetual_rate_used")

    if not fcf_per_share or fcf_per_share <= 0 or d0 is None or p0 is None:
        out["reason"] = "no positive FCF base"
        return out

    g_star, capped = _solve_growth(fcf_per_share, current_price, d0, p0, growth_years)

    out.update({
        "ok": True,
        "implied_growth": round(g_star, 4),
        "implied_growth_capped": capped,
        "model_growth": round(g_model, 4) if g_model is not None else None,
        "base_fcf_per_share": round(fcf_per_share, 4),
        "discount_rate": round(d0, 4),
        "perpetual_rate": round(p0, 4),
        "growth_years": growth_years,
        "value_default": bool(meta.get("defaulted")),
    })

    _implied_disp = _display_pct(g_star, capped)
    _model_disp = f"{g_model * 100:.1f}%" if g_model is not None else "-"
    out["sentence"] = (
        f"At {current_price:,.2f}, the market is pricing in {_implied_disp} annual FCF growth "
        f"for {growth_years} years (then {p0 * 100:.1f}% forever). "
        f"The model assumes {_model_disp}."
    )
    return out


def _display_pct(g, capped):
    """'8.3%' for a real interior solution, '≥ 60.0%' / '≤ -30.0%' when the
    bisection hit a bound (the price sits outside what the band can
    produce at all - see compute()'s own docstring)."""
    if capped == "low":
        return f"≤ {g * 100:.1f}%"
    if capped == "high":
        return f"≥ {g * 100:.1f}%"
    return f"{g * 100:.1f}%"
