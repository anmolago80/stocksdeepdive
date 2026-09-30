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
        meta["discount_source"]  : "tiered" | "tiered-default" | "manual" | "fallback"
        meta["discount_tier_label"] : str | None (A6 market-cap tier, e.g. "mid-cap (US$10B-50B)")
        meta["perpetual_source"] : "currency" | "manual" | "fallback"
        meta["discount_rate_used"]  : float | None
        meta["perpetual_rate_used"] : float | None
        meta["growth_default"]   : bool  (DCF fell back to average growth)
        meta["value_default"]    : bool  (the intrinsic value rests on an
                                          assumption - render it red)
        meta["yahoo_estimate_status"] : "ok"|"no_coverage"|"non_positive"|"fetch_failed"|None
                                          (only set on the auto growth path -
                                          see capm_engine.get_growth_estimates_5y())
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
            "discount_tier_label": dcf_meta.get("discount_tier_label"),
            "perpetual_source": dcf_meta.get("perpetual_source"),
            "discount_rate_used": dcf_meta.get("discount_rate_used"),
            "perpetual_rate_used": dcf_meta.get("perpetual_rate_used"),
            # Growth-path option E (28 Sep 2026) passthrough - the fade's
            # own end rate (tiered, floored at perpetual_rate_used), same
            # pure-provenance pattern as every other *_used key above.
            "growth_end_rate_used": dcf_meta.get("growth_end_rate_used"),
            # Growth-estimate-fetch resilience fix (28 Sep 2026) passthrough
            # - "ok"/"no_coverage"/"fetch_failed"/None, same pure-provenance
            # pattern as every other *_used/*_source key above - see
            # capm_engine.get_growth_estimates_5y()'s own docstring.
            "yahoo_estimate_status": dcf_meta.get("yahoo_estimate_status"),
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
            # Audit fix 1.4 passthrough - same pattern as discount_floored.
            "discount_manual_clamped": dcf_meta.get("discount_manual_clamped", False),
            "perpetual_manual_clamped": dcf_meta.get("perpetual_manual_clamped", False),
            "fx_converted": dcf_meta.get("fx_converted"),
            "fx_rate_used": dcf_meta.get("fx_rate_used"),
            "fx_fallback": dcf_meta.get("fx_fallback", False),
            # Outlier-guard fix (28 Sep 2026) passthrough - "average" or
            # "midpoint (capex rising)", same pure-provenance pattern as
            # every other *_used/*_basis key above - see
            # fcf_valuation_engine.normalized_base_and_series()'s own
            # docstring for the rising-capex guard this reports on.
            "capex_basis": dcf_meta.get("capex_basis"),
            # Growth-never-zero rewrite (30 Sep 2026) passthrough - same
            # pure-provenance pattern as every other *_used/*_reason key
            # above. growth_raw is the pre-cap figure estimate_growth()
            # found; fcf_reason is only ever "negative_normalised_fcf"
            # on THIS branch (dcf_value > 0 means the DCF succeeded, so
            # "negative_fcf" - the abandon-the-DCF reason - can never
            # appear here; see the pe-blend branch below for that one).
            "growth_raw": dcf_meta.get("growth_raw"),
            "fcf_reason": dcf_meta.get("fcf_reason"),
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
        # Growth-never-zero rewrite (30 Sep 2026): why the DCF itself
        # was abandoned before falling back to P/E-blend - pure
        # passthrough of fcf_valuation_engine.dcf_intrinsic_value()'s
        # own meta, same pattern as every other key above. "negative_
        # fcf" here means there was truly no usable free cash flow
        # anywhere for this ticker (see that function's own comment on
        # this exact key).
        "fcf_reason": dcf_meta.get("fcf_reason"),
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
        # Audit fix 1.3: clamp to MIN_DISCOUNT_RATE (0.075), not the
        # superseded DISCOUNT_FLOOR (0.05) - MIN_DISCOUNT_RATE is the
        # tightened floor capm_engine.resolve_discount_rate() itself now
        # enforces, added specifically because a rate that close to AUD
        # terminal growth blows the Gordon-growth terminal value up by an
        # order of magnitude (see capm_engine.py's module docstring for
        # the CSL.AX case this was fixed for). Clamping a Bear/Bull
        # scenario to the old, looser floor could reproduce exactly that
        # bug for a defensive low-beta stock with a negative Bull delta.
        d = max(capm_engine.MIN_DISCOUNT_RATE, min(d0 + dd, capm_engine.DISCOUNT_CEIL))
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


# --------------------------------------------------------------------------- #
# DCF sanity flag (display only)
# --------------------------------------------------------------------------- #
# Owner-directed outlier guard (28 Sep 2026, TOYO false positive). A DCF that
# lands more than 3x the current market price is far more likely to reflect
# a bad/thin data input (see fcf_valuation_engine.py's REPORTED_GROWTH_CAP_
# FRACTION and the rising-capex guard in normalized_base_and_series() - both
# aimed at the same root causes) than a genuine 3x-undervalued stock. This is a
# DISPLAY-ONLY sanity check - it must never feed Top 100 selection, scoring,
# or ranking; callers only use it to show a warning next to the number.
DCF_SANITY_MULTIPLE = 3.0


def dcf_looks_unreliable(intrinsic_value, current_price):
    """
    True when a DCF intrinsic value is far enough above the current price
    that the model output is more likely a bad-data artifact than a real
    mispricing - display only, see DCF_SANITY_MULTIPLE's own comment.
    """
    if not intrinsic_value or not current_price or current_price <= 0:
        return False
    return intrinsic_value > DCF_SANITY_MULTIPLE * current_price


def resolve_stock_type(ticker, info=None):
    """Returns (stock_type, source, defaulted)."""
    if ticker in CLASSIFICATIONS:
        return CLASSIFICATIONS[ticker], "manual", False
    stype, defaulted = _auto_stock_type(ticker, info=info)
    return stype, "auto", defaulted
