"""
currency_view_engine.py

SECTION B of instruction_top200_amendments_and_currency_view.md (5 Oct
2026, Director-directed): the home-currency setting and the currency-risk
note beside the margin of safety. Pure Python (no Streamlit dependency),
same "*_engine.py owns the logic" split as top100_engine.py/currency_risk_
engine.py.

COMMIT B1: just the switch, read here so every caller (the account-bar
widget in app.py, the Deep Dive note in COMMIT B2, the Currency Risk tool
additions in COMMIT B3, the Portfolio table in COMMIT B4) shares one
definition.

COMMIT B2: the shared method itself - scenario_effects()/mos_view() -
"one function, shared by every place that shows it" (the task's own
words). Every caller (the Deep Dive note here, COMMIT B3's Currency Risk
tool table, COMMIT B4's Portfolio exposure table) goes through
scenario_effects(), which does nothing but call currency_risk_engine's
own get_fx_history()/slice_range()/period_stats()/position_impact() in
sequence - the SAME functions/order the Currency Risk page's own section
C already uses for this pair - never a second, independently-computed
rate. No network call of its own: get_fx_history() is itself a disk-
cached read (currency_risk_engine.CACHE_TTL_SECONDS), so a page view
here never fetches live.

Display only - nothing in this module (or anything it gates) ever
changes intrinsic value, margin of safety, any score, any scan row, any
stored value, or the Top 200 selection/ranking. mos_view() takes an
already-rendered MOS percentage as an input and returns a VIEW of it;
it never writes anything back.
"""
import os

import currency_risk_engine as cre


def is_currency_view_live():
    """The switch - same unset/""/0/off=OFF, 1/on=ON pattern as
    top100_engine.is_backfill_live()/financials_classifier.
    is_financials_store_live(). Re-read on every call (cheap - one env
    var lookup) so Andrew's own go takes effect on the next request
    without a redeploy. Claude Code never sets this - the Director does,
    on Railway, after Andrew has checked the owner-only preview."""
    raw = (os.environ.get("CURRENCY_VIEW_LIVE") or "").strip().lower()
    return raw in ("1", "on")


def visible_to(email, is_owner_fn):
    """Whether the home-currency control / the Deep Dive note / the
    Currency Risk tool additions / the Portfolio table should render for
    this email right now: everyone once the switch is live, owner-only
    while it's off. `is_owner_fn` is passed in (rather than importing
    ai_gate here) so this module stays a plain, dependency-light *_engine
    module - callers pass ai_gate.is_owner."""
    return is_currency_view_live() or bool(is_owner_fn(email))


SCENARIO_KEYS = cre.POSITION_SCENARIO_KEYS  # ["average", "plus_1sigma", "minus_1sigma"]


def scenario_effects(home_currency, stock_currency, position_size=1.0,
                      range_key=cre.DEFAULT_RANGE):
    """None (never available/an estimate) or:
      {"current_rate", "average", "sigma", "stale",
       "scenarios": {key: {"scenario_rate", "pct_change", "amount_change",
                            "value_factor"}, ...}}
    for the three cre.POSITION_SCENARIO_KEYS, home_currency as cre's
    `base` and stock_currency as its `quote` - i.e. current_rate/average/
    sigma are themselves the SAME numbers the Currency Risk page's own
    hero figure and section A/C would show for this exact pair and
    window, fetched/cached/sliced/stat'd by calling cre.get_fx_history()
    -> cre.slice_range() -> cre.period_stats() in that order, never a
    second, independent computation. value_factor = current_rate /
    scenario_rate - cre.position_impact()'s own ratio (its pct_change is
    just (value_factor-1)*100); re-derived here from pct_change rather
    than recomputed, since position_impact() doesn't return it directly.

    None whenever: either currency is blank, they're equal (no FX leg to
    describe), there's no cached history for the pair, or there isn't
    enough of it (period_stats() needs >=2 closes) - the task's own "no
    exchange-rate history... or less than the tool itself requires: the
    note says the currency view is not available for this pair. Never an
    estimate" rule. No network call: get_fx_history() is itself a disk-
    cached read."""
    home = (home_currency or "").strip().upper()
    stock = (stock_currency or "").strip().upper()
    if not home or not stock or home == stock:
        return None
    history = cre.get_fx_history(home, stock)
    if not history:
        return None
    dates, closes = cre.slice_range(history["dates"], history["closes"], range_key)
    stats = cre.period_stats(closes)
    if not stats:
        return None
    impacts = cre.position_impact(position_size, stats["today"], stats["average"], stats["sigma"])
    scenarios = {}
    for imp in impacts:
        scenarios[imp["key"]] = dict(imp, value_factor=1.0 + imp["pct_change"] / 100.0)
    return {
        "current_rate": stats["today"], "average": stats["average"], "sigma": stats["sigma"],
        "stale": bool(history.get("stale")), "scenarios": scenarios,
    }


def mos_view(mos_pct, home_currency, stock_currency, range_key=cre.DEFAULT_RANGE):
    """None (no note - "withheld, n/a: no note" / "not available for this
    pair: never an estimate") or:
      {"view_at_average", "view_low", "view_high", "effects"}
    all three view_* as PERCENTAGES already (e.g. 65.8, not 0.658), NOT
    yet rounded to the task's "one decimal place" display rule - callers
    format that themselves. view_low/view_high are the lower/higher of
    the two +-1-sigma results, per the task's own "the range is the
    lower and the higher of the two standard-deviation results" rule
    (which sigma is "worse" depends on the pair/direction, so this is
    never assumed to be plus_1sigma/minus_1sigma in that order).

    The method (task's own words): MOS_view = 1 - (1 - MOS) / f, where f
    is scenario_effects()'s own value_factor for that scenario - the
    SAME ratio cre.position_impact() already computed, never a second
    way. None whenever mos_pct is None (no MOS on the page) or
    scenario_effects() itself returns None (same currency, or no/
    insufficient history)."""
    if mos_pct is None:
        return None
    effects = scenario_effects(home_currency, stock_currency, position_size=1.0,
                                range_key=range_key)
    if not effects:
        return None
    mos_frac = mos_pct / 100.0
    views = {
        key: (1.0 - (1.0 - mos_frac) / sc["value_factor"]) * 100.0
        for key, sc in effects["scenarios"].items()
    }
    view_avg = views.get("average")
    sd_values = [v for k, v in views.items() if k != "average"]
    if view_avg is None or not sd_values:
        return None
    return {
        "view_at_average": view_avg,
        "view_low": min(sd_values),
        "view_high": max(sd_values),
        "effects": effects,
    }


def adjusted_value(price, margin_pct):
    """Director, 8 Oct 2026 (delivered with the PUSH 1 go-ahead message):
    "the currency-adjusted value in the STOCK'S OWN trading currency
    after each margin of safety" - the value, in the SAME currency as
    `price` (never converted), that reproduces `margin_pct` exactly via
    margin = 1 - price / adjusted_value. `margin_pct` is a percentage
    (e.g. 25.5, not 0.255) - one of mos_view()'s own view_at_average/
    view_low/view_high, or a single scenario's view value computed the
    same way (currency_risk_render.py's per-scenario rows) - never an
    independently-derived figure.

    Algebraically this is also equal to intrinsic_value * value_factor
    (value_factor = current_rate / scenario_rate, the same ratio mos_
    view()'s own scenarios already carry) - this form is preferred
    because it needs only `price` and the margin already shown, not a
    second intrinsic-value read, so it can never drift from the
    percentage sitting right next to it.

    None when price is None (no live price to anchor to) or margin_pct
    is 100 or more (the stock's own price would be zero or negative in
    that scenario - degenerate, never invented)."""
    if price is None or margin_pct is None:
        return None
    denom = 1.0 - margin_pct / 100.0
    if denom <= 0:
        return None
    return price / denom
