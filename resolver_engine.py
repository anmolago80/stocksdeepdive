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
from fcf_valuation_engine import dcf_intrinsic_value, GROWTH_FLOOR, GROWTH_CEIL, trading_currency_for
import capm_engine
import currency_format


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
    income_df=None,
    income_df_currency_converted=False,
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
        meta["discount_tier_label"] : str | None (A6 size-premium band, e.g. "mid-cap (US$10B-50B)" -
                                          Push 2, 30 Sep 2026: a continuous log-linear interpolation
                                          now sets the actual premium, this is a display-only band name)
        meta["risk_free_used"]   : float | None (Push 2 - the risk-free component of discount_rate_used)
        meta["risk_free_source"] : "live" | "default" | None (Push 2)
        meta["market_cap_usd"]   : float | None (Push 2 - USD-converted market cap used for the lookup)
        meta["premium_used"]     : float | None (Push 2 - the interpolated size premium over risk-free)
        meta["perpetual_source"] : "currency" | "manual" | "fallback"
        meta["discount_rate_used"]  : float | None
        meta["perpetual_rate_used"] : float | None
        meta["growth_default"]   : bool  (DCF fell back to average growth)
        meta["value_default"]    : bool  (the intrinsic value rests on an
                                          assumption - render it red)
        meta["yahoo_estimate_status"] : "ok"|"ok_1y"|"no_coverage"|"non_positive"|"fetch_failed"|None
                                          (only set on the auto growth path -
                                          see capm_engine.get_growth_estimates_5y())
        meta["fcf_base_source"] : "median5_clean"|"ebitda_bridge"| the plain fcf_source
                                          value when Step 4's distorted-year mechanism
                                          didn't fire - see fcf_valuation_engine.
                                          normalized_base_and_series()'s own docstring
        meta["fcf_distorted_years"] : list[int] - 0-indexed positions (0=latest) flagged
                                          as a one-off cash distortion, empty when unused
        meta["is_financials_mode"] : bool - financials mode (2 Oct 2026, owner-directed) -
                                          True whenever financials_classifier.is_financials()
                                          said yes for this ticker, whether the DCF ended up
                                          on the net-income path or its OCF fallback - see
                                          fcf_valuation_engine.normalized_base_and_series()'s
                                          own docstring

    income_df (Step 4, 30 Sep 2026, owner-directed): optional income
    statement, passed straight through to dcf_intrinsic_value()/
    normalized_base_and_series() for the distorted-year cross-check -
    None (the default) means that mechanism never fires, identical to
    before it existed.

    income_df_currency_converted (Addendum 2 item 1 of instruction_
    financials_income_store_and_top200_guard.md, 5 Oct 2026): pure
    passthrough to dcf_intrinsic_value() - see that function's own
    docstring. False (the default) for every caller that predates this.
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
        income_df=income_df,
        income_df_currency_converted=income_df_currency_converted,
    )
    if dcf_value > 0:
        meta = {
            "growth_source": dcf_meta.get("growth_source"),
            "growth_governor": dcf_meta.get("growth_governor"),
            "growth_ceiling_used": dcf_meta.get("growth_ceiling_used"),
            # Growth 1y-blend fix (1 Oct 2026, owner-directed, KNSL live
            # case): the two raw inputs behind the "analyst_1y"/
            # "analyst_1y_blend" growth_source - see fcf_valuation_
            # engine.estimate_growth()'s own docstring. None/None for
            # every other growth_source.
            "growth_1y_consensus": dcf_meta.get("growth_1y_consensus"),
            "growth_history_capped": dcf_meta.get("growth_history_capped"),
            "fcf_source": dcf_meta.get("fcf_source"),
            "fcf_used": dcf_meta.get("fcf_used"),
            "fcf_per_share_used": dcf_meta.get("fcf_per_share_used"),
            "discount_source": dcf_meta.get("discount_source"),
            "discount_tier_label": dcf_meta.get("discount_tier_label"),
            # Push 2 (30 Sep 2026) passthrough - the size-premium
            # breakdown (risk-free + premium) behind discount_rate_used,
            # same pure-provenance pattern as every other *_used key
            # here - see capm_engine.resolve_discount_rate()'s own
            # docstring.
            "risk_free_used": dcf_meta.get("risk_free_used"),
            "risk_free_source": dcf_meta.get("risk_free_source"),
            "market_cap_usd": dcf_meta.get("market_cap_usd"),
            "premium_used": dcf_meta.get("premium_used"),
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
            "fx_unavailable": dcf_meta.get("fx_unavailable", False),
            "fx_reason": dcf_meta.get("fx_reason"),
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
            # Audit fixes Commit 4 (30 Sep 2026) passthrough - same pure-
            # provenance pattern as every other *_flagged/*_source key
            # above. See share_class_engine.whole_company_shares()'s own
            # docstring and fcf_valuation_engine.dcf_intrinsic_value()'s
            # meta docstring for what each means - app.py's Fair Value
            # panel shows a small caption next to the Shares row when
            # either share_count_flagged or share_count_note is set.
            "share_count_flagged": dcf_meta.get("share_count_flagged", False),
            "share_count_source": dcf_meta.get("share_count_source"),
            "share_count_note": dcf_meta.get("share_count_note"),
            "market_cap_missing": dcf_meta.get("market_cap_missing", False),
            # Step 4 (30 Sep 2026, KO fix) passthrough - same pure-
            # provenance pattern as every other *_source key above. See
            # fcf_valuation_engine.normalized_base_and_series()'s own
            # docstring for what these mean.
            "fcf_base_source": dcf_meta.get("fcf_base_source"),
            "fcf_distorted_years": dcf_meta.get("fcf_distorted_years"),
            "fcf_base_raw_per_share": dcf_meta.get("fcf_base_raw_per_share"),
            # Step 4 uplift safety valve (2 Oct 2026, owner decision) -
            # pure passthrough, same pattern as every other *_source/
            # *_used key above. See fcf_valuation_engine.normalized_
            # base_and_series()'s own docstring.
            "fcf_base_capped_by_uplift": dcf_meta.get("fcf_base_capped_by_uplift", False),
            # Financials mode (2 Oct 2026, owner-directed) passthrough -
            # pure provenance, same pattern as every other *_mode/*_source
            # key above. See fcf_valuation_engine.normalized_base_and_
            # series()'s own docstring.
            "is_financials_mode": dcf_meta.get("is_financials_mode", False),
        }
        # TASK 70b (Director-directed, owner-approved, 21 Nov 2026, WMS
        # live case): this curated `meta` is the ACTUAL root of the bug -
        # it already dropped fcf_base_ocf_used/fcf_base_capex_used/fcf_
        # base_capex_years/fcf_base_median_years before auto_compounder_
        # engine._run_canonical_dcf() ever saw them (that function calls
        # resolve_intrinsic_value(), not dcf_intrinsic_value() directly),
        # AND this same dict is deep_dive_engine.py's own iv_meta - so
        # Deep Dive's "FCF Build" breakdown was ALSO silently missing for
        # the plain ocf-normcapex/fcf-median cases, not "probably
        # unaffected" as first assumed. Fills in whichever of DCF_
        # STARTING_FCF_FIELDS isn't already explicit above (every field
        # that mechanism needs beyond what this dict already carried) -
        # never overrides fcf_distorted_years/fcf_base_capped_by_uplift's
        # own explicit defaults just above.
        for _k in DCF_STARTING_FCF_FIELDS:
            if _k not in meta:
                meta[_k] = dcf_meta.get(_k)
        return dcf_value, "dcf", growth_used, meta

    # Exchange-rate fallback-of-1 fix (3 Oct 2026, Director-directed,
    # Commit 6): the DCF wasn't abandoned for a valuation reason (no
    # usable FCF) - it's abandoned because the statements genuinely
    # cannot be converted into the listing currency (no live rate, no
    # static fallback, no cross-rate via USD either - see fcf_
    # valuation_engine.fx_rate()'s own docstring). The P/E-blend method
    # below needs no FX conversion of its own (EPS/quality/sector inputs
    # are already in listing currency), so it WOULD still produce a
    # number here - but that number would imply a false confidence this
    # ticker can be valued at all, when the real problem is "we don't
    # know this currency's exchange rate", not "DCF doesn't apply to
    # this kind of company". Skip the P/E-blend fallback entirely and
    # return no intrinsic value / no MOS, with the reason, exactly as
    # this task's own instruction specifies.
    if dcf_meta.get("fx_unavailable"):
        return 0, "none", None, {
            "growth_source": None,
            "growth_governor": None,
            "growth_ceiling_used": None,
            "fcf_source": "none",
            "discount_source": None,
            "perpetual_source": None,
            "discount_rate_used": None,
            "perpetual_rate_used": None,
            "growth_default": False,
            "value_default": False,
            "fcf_reason": None,
            "fx_unavailable": True,
            "fx_reason": dcf_meta.get("fx_reason"),
        }

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
        "fx_unavailable": False,
        "fx_reason": None,
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

    # Stage 1a (3 Oct 2026, Director-directed): trading_currency_for()
    # adds the .L/.TO suffix fallback this bare info.get() chain never
    # had - a no-op for every ticker whose info["currency"] is already
    # populated (the overwhelming majority).
    ccy = currency or trading_currency_for(ticker, info)
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

# Audit fix C4 / Fable finding V4 (10 Oct 2026, instruction_combined_
# 10oct.md PART C): the low-side counterpart - a DCF intrinsic value
# far enough BELOW the current price (less than a third of it) is
# just as much a sign of a bad-data artifact (overstated share count,
# a busted P/E-blend input, ...) as the existing high-side ">3x" case,
# but was never checked at all until this fix. Display-only label text
# for whichever caller renders dcf_looks_unreliable(symmetric=True)'s
# low-side case - see that function's own docstring for why this is
# opt-in, never the default.
DCF_UNRELIABLE_LOW_LABEL = "Model unreliable: value less than a third of price"


def dcf_looks_unreliable(intrinsic_value, current_price, symmetric=False):
    """
    True when a DCF intrinsic value is far enough above the current price
    that the model output is more likely a bad-data artifact than a real
    mispricing - display only, see DCF_SANITY_MULTIPLE's own comment.

    `symmetric` (audit fix C4 / Fable finding V4, 10 Oct 2026): False
    (the default) preserves this function's EXACT original high-side-
    only behaviour for every existing caller - in particular top100_
    engine.py's own pool-exclusion checks (_is_dcf_unreliable() and
    run_nightly()'s own re-check), which this fix's own instruction
    explicitly requires stay unchanged ("Pool selection unchanged:
    don't extend the Top 200 exclusion"). True also flags the
    symmetric low-side case (intrinsic_value < current_price /
    DCF_SANITY_MULTIPLE, see DCF_UNRELIABLE_LOW_LABEL above) - used
    only by nightly_scan.py's own new, separate display-only field
    (never read by anything that feeds pool selection/scoring/ranking).
    """
    if not intrinsic_value or not current_price or current_price <= 0:
        return False
    if intrinsic_value > DCF_SANITY_MULTIPLE * current_price:
        return True
    if symmetric and intrinsic_value < current_price / DCF_SANITY_MULTIPLE:
        return True
    return False


def _dcf_money_m(value, ticker):
    """"$X.XXM"-style money string for a DCF dollar figure (TASK 70,
    20 Nov 2026) - currency_format.format_money()'s own ticker-aware
    symbol lookup, just applied to the value already divided into
    millions, since every figure this is used for (a DCF's starting
    FCF, the OCF/capex it was built from) is reported at that scale."""
    if value is None:
        return None
    return f"{currency_format.format_money(value / 1_000_000.0, ticker, decimals=2)}M"


# TASK 70b (Director-directed, owner-approved, 21 Nov 2026, WMS live
# case): every meta key dcf_starting_fcf_lines() below reads - the
# single source of truth for it, so a caller building its own curated
# "dcf result" dict (auto_compounder_engine._run_dcf()/_run_canonical_
# dcf()) can pass all of them through generically instead of hand-
# listing each one and silently dropping a future addition, which is
# exactly how TASK 70 shipped with the Fair Value tab's "Starting FCF"/
# "FCF Build" rows silently missing: both curated dicts never carried
# fcf_used (dcf_starting_fcf_lines() got None and returned (None, None))
# even though the function itself worked correctly when given the full
# meta dict directly (as Deep Dive's iv_meta always is). Kept in sync
# with dcf_starting_fcf_lines()'s own body by
# tests/test_director_task70b_fair_value_passthrough.py's static check.
DCF_STARTING_FCF_FIELDS = (
    "fcf_used", "fcf_source", "fcf_base_source", "fcf_distorted_years",
    "fcf_base_raw", "fcf_base_capped_by_uplift", "fcf_base_normalized",
    "fcf_base_median_years", "fcf_base_ocf_used", "fcf_base_capex_used",
    "fcf_base_capex_years", "fx_converted",
)


def dcf_starting_fcf_lines(meta, ticker=None):
    """TASK 70 (Director-directed, owner-approved, 20 Nov 2026) - the
    DCF's starting free cash flow, as a (formatted "$X.XXM" figure, one-
    line method breakdown) pair, for display only (next to the DCF's
    growth/discount/perpetual/size-premium inputs). Neither string is
    prefixed with a label - each caller builds its own "Starting FCF:
    {fcf}"-shaped line in whatever form its own screen already renders
    inputs in (a "label: value" row on the Fair Value tab, a plain
    st.caption() sentence on Deep Dive), so the two screens show
    identical wording/numbers without this function assuming either
    one's own rendering convention. `meta` is the dict
    fcf_valuation_engine.dcf_intrinsic_value() returns (passed straight
    through by resolve_intrinsic_value() above as its own 4th return
    value) - the SAME dict both auto_compounder_engine.py's Fair Value
    tab and deep_dive_engine.py's Deep Dive context already read every
    other "dcf_*" input from, so calling this one function from both
    places is what keeps the two screens showing the same figure/
    wording, rather than two independently-written captions drifting
    apart over time.

    Returns (None, None) when no DCF ever produced a base FCF at all
    (meta["fcf_used"] unset) - the caller should simply omit both lines,
    same as every other DCF input row already does when its value is
    None.

    Priority mirrors fcf_valuation_engine.normalized_base_and_series()'s
    own docstring on how these sources can combine: Step 4's one-off
    clean-year/EBITDA-bridge substitution (meta["fcf_base_source"])
    supersedes Task 10's own 3-year-median outlier swap (meta["fcf_base_
    normalized"]), which in turn supersedes the plain per-source
    breakdown - all three read EXISTING meta fields (fcf_base_raw/fcf_
    distorted_years/fcf_base_capped_by_uplift), already disclosed
    elsewhere on both screens, just not previously paired with the
    actual Starting FCF dollar figure. The plain "ocf-normcapex"/"ocf_
    fallback_financials" breakdown is the one case needing NEW meta
    fields (fcf_base_ocf_used/fcf_base_capex_used/fcf_base_capex_years) -
    see normalized_base_and_series()'s own oneoff_meta comment - since
    the existing meta never exposed the raw OCF/capex components, only
    the final base figure."""
    fcf_used = meta.get("fcf_used")
    if fcf_used is None:
        return None, None
    fcf_money = _dcf_money_m(fcf_used, ticker)

    fcf_base_source = meta.get("fcf_base_source")
    fcf_source = meta.get("fcf_source")
    breakdown = None

    if fcf_base_source in ("median5_clean", "ebitda_bridge"):
        _method_label = "clean-year" if fcf_base_source == "median5_clean" else "EBITDA-bridge"
        _n = len(meta.get("fcf_distorted_years") or [])
        _raw_txt = _dcf_money_m(meta.get("fcf_base_raw"), ticker) or "n/a"
        breakdown = (
            f"= {_method_label} base (one-off year{'s' if _n != 1 else ''} excluded; "
            f"raw latest {_raw_txt})"
        )
        if meta.get("fcf_base_capped_by_uplift"):
            breakdown += " - capped at 3× raw"
    elif meta.get("fcf_base_normalized"):
        # Task 10's own 3-year-median outlier swap - distinct from Step
        # 4's one-off mechanism above (the two are mutually exclusive per
        # ticker). Same wording as app.py's existing Deep Dive caption
        # for this case (dcf_base_normalized).
        _raw_txt = _dcf_money_m(meta.get("fcf_base_raw"), ticker) or "n/a"
        breakdown = f"= 3-year-median base (outlier year excluded; raw latest {_raw_txt})"
    elif fcf_base_source == "fcf-median" or fcf_source == "fcf-median":
        _n = meta.get("fcf_base_median_years") or 3
        breakdown = f"= reported FCF, median of last {_n} year{'s' if _n != 1 else ''}"
    elif fcf_base_source == "net_income_financials":
        breakdown = "Financials mode: net income used in place of FCF"
    elif (
        fcf_base_source in ("ocf-normcapex", "ocf_fallback_financials")
        or fcf_source in ("ocf-normcapex", "ocf_fallback_financials")
    ) and meta.get("fcf_base_ocf_used") is not None:
        _ocf_txt = _dcf_money_m(meta["fcf_base_ocf_used"], ticker)
        _capex_txt = _dcf_money_m(meta.get("fcf_base_capex_used"), ticker)
        _years = meta.get("fcf_base_capex_years")
        _years_txt = f"{_years}-yr " if _years else ""
        breakdown = f"= operating cash flow {_ocf_txt} − {_years_txt}average capex {_capex_txt}"
    elif fcf_source == "info":
        breakdown = "= Yahoo reported free cash flow (no cash-flow statement available)"
    elif fcf_source == "manual":
        breakdown = "= manually overridden free cash flow"

    if breakdown and meta.get("fx_converted"):
        _fin_ccy = str(meta["fx_converted"]).split("->")[0].strip()
        if _fin_ccy:
            breakdown += f" (converted from {_fin_ccy})"

    return fcf_money, breakdown


def resolve_stock_type(ticker, info=None):
    """Returns (stock_type, source, defaulted)."""
    if ticker in CLASSIFICATIONS:
        return CLASSIFICATIONS[ticker], "manual", False
    stype, defaulted = _auto_stock_type(ticker, info=info)
    return stype, "auto", defaulted
