"""
Resolves each fundamental for a stock to a single value plus provenance.

Since the manual-override dictionaries are now intentionally EMPTY (no cell may
be hand-populated - see quality_engine / intrinsic_value_engine /
stock_classifier), every value here is computed from the stock's own data:

    quality   -> auto_quality_engine (fundamentals)
    intrinsic -> DCF on historical free cash flow, else P/E blend, else N/A
    type      -> auto_stock_type_engine (sector rule)

Each resolver also returns whether the value had to fall back to a
default/average assumption, so the app can render those cells in red.

ORDERING: resolve_quality_score() must be called BEFORE
resolve_intrinsic_value(), because the P/E-blend fallback needs the quality
score (quality_pe = 10 + quality_score / 5) as an input.
"""

from quality_engine import QUALITY_SCORES
from intrinsic_value_engine import INTRINSIC_VALUES
from stock_classifier import CLASSIFICATIONS

from auto_quality_engine import get_quality_score as _auto_quality
from auto_intrinsic_value_engine import get_intrinsic_value as _auto_intrinsic
from auto_stock_type_engine import get_stock_type as _auto_stock_type
from fcf_valuation_engine import dcf_intrinsic_value, GROWTH_FLOOR, GROWTH_CEIL
import capm_engine


def resolve_quality_score(ticker, info=None):
    """Returns (score, source, defaulted)."""
    if ticker in QUALITY_SCORES:
        return QUALITY_SCORES[ticker], "manual", False
    score, defaulted = _auto_quality(ticker, info=info)
    return score, "auto", defaulted


def resolve_intrinsic_value(
    ticker,
    quality_score,
    info=None,
    cashflow_df=None,
    currency=None,
    discount_rate=None,
    perpetual_rate=None,
    growth_rate=None,
    manual_fcf=None,
):
    """
    Resolution order for intrinsic value:
        1. DCF / FCF model, where free cash flow is positive and meaningful,
           with the discount rate (CAPM), growth rate (analyst consensus ->
           historical FCF -> reported growth -> average) and terminal growth
           rate (currency-based) all auto-calculated when the caller leaves
           them as None - the app's "Valuation & FCF inputs" panel passes an
           explicit value instead whenever Auto mode is off or a per-stock
           override is set.
        2. P/E-blend auto method (financials, negative-FCF, early growth).
        3. N/A when there's nothing to value on (no positive EPS either).

    Returns (intrinsic_value, source_label, dcf_growth_used, meta) where meta
    is a dict of provenance/default flags:
        meta["growth_source"]    : "analyst"|"history"|"analyst+history"|"info"|"default"|"manual"|None
        meta["growth_governor"]  : "Yahoo"|"History"|"Info"|"Default"|"Manual"|None
        meta["fcf_source"]       : "history" | "info" | "manual" | "none"
        meta["fcf_used"]         : float | None (the base FCF the DCF actually compounded from)
        meta["fcf_per_share_used"] : float | None (fcf_used / shares outstanding)
        meta["discount_source"]  : "capm" | "capm-default" | "manual" | "fallback"
        meta["perpetual_source"] : "currency" | "manual" | "fallback"
        meta["discount_rate_used"]  : float | None
        meta["perpetual_rate_used"] : float | None
        meta["growth_default"]   : bool  (DCF fell back to average growth)
        meta["value_default"]    : bool  (the intrinsic value rests on an
                                          assumption - render it red)
    """
    dcf_value, growth_used, dcf_meta = dcf_intrinsic_value(
        ticker,
        info=info,
        cashflow_df=cashflow_df,
        currency=currency,
        discount_rate=discount_rate,
        perpetual_rate=perpetual_rate,
        growth_rate=growth_rate,
        manual_fcf=manual_fcf,
    )
    if dcf_value > 0:
        meta = {
            "growth_source": dcf_meta.get("growth_source"),
            "growth_governor": dcf_meta.get("growth_governor"),
            "growth_ceiling_used": dcf_meta.get("growth_ceiling_used"),
            "fcf_source": dcf_meta.get("fcf_source"),
            "fcf_used": dcf_meta.get("fcf_used"),
            "fcf_per_share_used": dcf_meta.get("fcf_per_share_used"),
            "discount_source": dcf_meta.get("discount_source"),
            "perpetual_source": dcf_meta.get("perpetual_source"),
            "discount_rate_used": dcf_meta.get("discount_rate_used"),
            "perpetual_rate_used": dcf_meta.get("perpetual_rate_used"),
            "growth_default": dcf_meta.get("growth_default", False),
            "value_default": dcf_meta.get("defaulted", False),
            # Task 10: pure passthrough of fcf_valuation_engine's own
            # provenance flag - no new resolution logic here, same as every
            # other *_default/*_source key above.
            "fcf_base_normalized": dcf_meta.get("fcf_base_normalized", False),
            "fcf_base_raw": dcf_meta.get("fcf_base_raw"),
            "fcf_base_used": dcf_meta.get("fcf_base_used"),
            # DCF fix passthrough - same pure-provenance pattern as above.
            "discount_floored": dcf_meta.get("discount_floored", False),
            "fx_converted": dcf_meta.get("fx_converted"),
            "fx_rate_used": dcf_meta.get("fx_rate_used"),
            "fx_fallback": dcf_meta.get("fx_fallback", False),
        }
        return dcf_value, "dcf", growth_used, meta

    # Fall back to the P/E-blend method for names DCF can't value.
    pe_value, pe_defaulted = _auto_intrinsic(ticker, quality_score, info=info)
    meta = {
        "growth_source": None,
        "growth_governor": None,
        "growth_ceiling_used": None,
        "fcf_source": "none",
        "discount_source": None,
        "perpetual_source": None,
        "discount_rate_used": None,
        "perpetual_rate_used": None,
        "growth_default": False,
        "value_default": pe_defaulted,
    }
    return pe_value, "pe-blend", None, meta


# --------------------------------------------------------------------------- #
# Bear / Base / Bull DCF scenarios
# --------------------------------------------------------------------------- #
# Fixed, disclosed sensitivity offsets applied around the resolved BASE case
# (whatever resolve_intrinsic_value() was just given - Auto CAPM/analyst/
# currency, the global Valuation & FCF defaults, or a per-stock override).
# Re-clamped to the model's normal safety bounds, same as everywhere else.
BEAR_DISCOUNT_DELTA = 0.02        # +2pp harsher discount rate
BULL_DISCOUNT_DELTA = -0.02       # -2pp friendlier discount rate
BEAR_GROWTH_DELTA = -0.05         # -5pp growth
BULL_GROWTH_DELTA = 0.05          # +5pp growth
BEAR_PERPETUAL_DELTA = -0.01      # -1pp terminal growth
BULL_PERPETUAL_DELTA = 0.01       # +1pp terminal growth


def dcf_scenarios(ticker, quality_score, info=None, cashflow_df=None, currency=None,
                   discount_rate=None, perpetual_rate=None, growth_rate=None,
                   manual_fcf=None):
    """
    Bear / Base / Bull DCF fair-value estimates for one stock.

    Base = whatever resolve_intrinsic_value() resolves to RIGHT NOW for this
    ticker given the discount_rate/perpetual_rate/growth_rate passed in - the
    exact same three optional params the app already threads through from its
    Valuation & FCF panel (None = auto CAPM/analyst/currency; an explicit
    number = the global default or a per-stock override, unchanged).
    Bear / Bull = those SAME resolved base parameters shifted by the fixed
    sensitivity offsets above, each independently re-clamped and re-run.

    Returns None when DCF isn't a usable method for this name (financials,
    negative FCF - the app is on the P/E-blend method instead, and a +-2pp
    discount-rate sensitivity on that isn't a meaningful thing to show).

    Returns {"bear": case, "base": case, "bull": case} where each case is
    {"discount_rate", "growth_rate", "perpetual_rate", "value_per_share"}.
    """
    base_value, base_label, base_growth, base_meta = resolve_intrinsic_value(
        ticker, quality_score, info=info, cashflow_df=cashflow_df, currency=currency,
        discount_rate=discount_rate, perpetual_rate=perpetual_rate,
        growth_rate=growth_rate, manual_fcf=manual_fcf)

    if base_label != "dcf" or base_growth is None:
        return None

    ccy = currency or (info or {}).get("currency") or "USD"
    d0 = base_meta.get("discount_rate_used")
    p0 = base_meta.get("perpetual_rate_used")
    g0 = base_growth
    if d0 is None or p0 is None:
        return None

    def _case(dd, dg, dp):
        d = max(capm_engine.DISCOUNT_FLOOR, min(d0 + dd, capm_engine.DISCOUNT_CEIL))
        g = max(GROWTH_FLOOR, min(g0 + dg, GROWTH_CEIL))
        p = p0 + dp
        if p >= d:
            p = max(0.0, d - 0.01)
        val, _g, _m = dcf_intrinsic_value(
            ticker, info=info, cashflow_df=cashflow_df, currency=ccy,
            discount_rate=d, perpetual_rate=p, growth_rate=g, manual_fcf=manual_fcf)
        return {"discount_rate": d, "growth_rate": g, "perpetual_rate": p,
                "value_per_share": val}

    return {
        "bear": _case(BEAR_DISCOUNT_DELTA, BEAR_GROWTH_DELTA, BEAR_PERPETUAL_DELTA),
        "base": {"discount_rate": d0, "growth_rate": g0, "perpetual_rate": p0,
                 "value_per_share": base_value},
        "bull": _case(BULL_DISCOUNT_DELTA, BULL_GROWTH_DELTA, BULL_PERPETUAL_DELTA),
    }


def resolve_stock_type(ticker, info=None):
    """Returns (stock_type, source, defaulted)."""
    if ticker in CLASSIFICATIONS:
        return CLASSIFICATIONS[ticker], "manual", False
    stype, defaulted = _auto_stock_type(ticker, info=info)
    return stype, "auto", defaulted
