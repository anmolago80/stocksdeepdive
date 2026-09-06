"""
switch_analyzer_engine.py

Mega-batch Part 17: pure-logic engine behind the "Switch Analyzer" tab on
the (signed-in) My Portfolio page - an opportunity-cost calculator for
"should I sell holding A and buy candidate B instead", built around one
honest fact this app has never modelled before: SELLING costs real money
(capital-gains tax + brokerage) before a single dollar of that switch can
start compounding again in the new position. Every function here is a
plain, deterministic calculation over numbers the caller supplies - no
network calls, no Streamlit, no file I/O - so it is trivially unit
testable and safe to import from anywhere.

THE TOLL (this module's central idea): selling a position worth V (current
market value) with an original cost base C triggers:
  - a capital gain of max(V - C, 0)
  - Australian individual CGT: a HELD-OVER-12-MONTHS position gets the
    50% CGT discount (only half the gain is taxable); held under 12
    months, the full gain is taxable. This module only ever applies the
    literal 12-month/50% rule - it is not general tax advice and never
    claims to model trusts, companies, other jurisdictions, or any
    other discount rate.
  - brokerage on BOTH legs of the switch (the sell trade AND the buy
    trade back into B), since both are unavoidable costs of the round
    trip, not just the sell.
That leaves proceeds P < V available to redeploy into B. compute_toll()
below reproduces the mega-batch's own worked example exactly:
    V=$30,000, C=$18,000, tax_rate=39%, brokerage=$20/trade, held>=12mo
    -> gain=$12,000, taxable (50% disc.)=$6,000, tax=$2,340,
       brokerage_total=$40, P=$27,620

THE BRIDGE (Z): expressed as an annualised drag, "how much return does B
have to make up, per year over N years, just to catch back up to what A
would have been worth by staying at V and compounding un-molested":
    Z = (V / P) ** (1/N) - 1
Worked example above, N=5y -> Z ~= 1.666%/yr (~1.67%/yr).

THE VERDICT: this module does not predict returns. The caller supplies
each side's own MODEL-DERIVED annualised return estimate (see
app.py's _switch_expected_return, which re-rates today's price to the
site's own Fair-Value estimate over the same N years - the same
mechanics as the bridge formula above, just run on price->IV instead of
V->P) and this module only compares the resulting spread against Z. The
result is reported as a fact about the model's own numbers ("the model
implies B needs to out-return A by ~2.1%/yr to clear the switching
cost, and today it is estimated to out-return it by ~3.4%/yr - clears by
~1.3%/yr"), never as a recommendation to act.

Never touches scoring engines, never suggests a portfolio weight, never
invents a number the caller didn't supply.
"""

import math

CGT_DISCOUNT_ELIGIBLE_DAYS = 365
CGT_DISCOUNT_RATE = 0.5

# Switch Analyzer's own, clearly-declared risk-disclosure thresholds -
# separate constants from every other tab's own thresholds (e.g.
# app.py's _CHECKLIST_THRESHOLDS), never shared, so a future change to
# one can never silently move the other.
CONCENTRATION_FLAG_PCT = 25.0   # candidate's resulting % of portfolio value
HIGH_CORRELATION_FLAG = 0.70    # correlation vs the rest of the portfolio


def capital_gain(sale_value, cost_base):
    """max(V - C, 0) - a loss is never taxed, and this module never
    models capital-loss offsetting against other gains (out of scope:
    that depends on the WHOLE tax return, not just this one switch)."""
    if sale_value is None or cost_base is None:
        return None
    return max(sale_value - cost_base, 0.0)


def cgt_discount_applies(held_days):
    """True once the position has been held >= 365 days (the AU
    individual 12-month CGT discount test). None held_days -> False
    (never assume a discount that hasn't been earned yet)."""
    if held_days is None:
        return False
    return held_days >= CGT_DISCOUNT_ELIGIBLE_DAYS


def compute_toll(sale_value, cost_base, tax_rate, brokerage, held_days=None):
    """The full switching toll for selling one position and buying
    another. tax_rate and brokerage are per the user's own "Switch
    Analyzer settings" (persisted per portfolio - see portfolio_store.
    get_switch_settings/set_switch_settings); brokerage is a flat
    dollar amount per trade, charged twice (sell + buy).

    Returns None if sale_value/cost_base/tax_rate/brokerage aren't all
    provided (this module never guesses a tax rate or brokerage fee).
    Otherwise a dict:
        gain, discount_applied, taxable_gain, tax, brokerage_total,
        proceeds_after_toll, toll_total, toll_pct_of_value
    proceeds_after_toll can legitimately be negative only in a
    pathological input (tax_rate > 100% etc.) - callers should treat a
    non-positive result as "this switch cannot be analysed" rather than
    dividing by it.
    """
    if sale_value is None or cost_base is None or tax_rate is None or brokerage is None:
        return None
    gain = capital_gain(sale_value, cost_base)
    discount_applied = cgt_discount_applies(held_days)
    taxable_gain = gain * (1 - CGT_DISCOUNT_RATE) if discount_applied else gain
    tax = taxable_gain * tax_rate
    brokerage_total = brokerage * 2
    toll_total = tax + brokerage_total
    proceeds = sale_value - toll_total
    return {
        "gain": gain,
        "discount_applied": discount_applied,
        "taxable_gain": taxable_gain,
        "tax": tax,
        "brokerage_total": brokerage_total,
        "proceeds_after_toll": proceeds,
        "toll_total": toll_total,
        "toll_pct_of_value": (toll_total / sale_value) if sale_value else None,
    }


def toll_without_discount(sale_value, cost_base, tax_rate, brokerage):
    """The SAME toll, forcing discount_applied=False - used only to build
    the "12-month CGT counterfactual" chip (compute_toll() already run
    with the holding's real held_days gives the "as it stands today"
    number; this gives "if sold before reaching 12 months" for
    comparison, or vice versa - see cgt_twelve_month_counterfactual)."""
    return compute_toll(sale_value, cost_base, tax_rate, brokerage, held_days=0)


def toll_with_discount(sale_value, cost_base, tax_rate, brokerage):
    """The SAME toll, forcing discount_applied=True - "if held to (or
    past) 12 months" counterfactual."""
    return compute_toll(sale_value, cost_base, tax_rate, brokerage,
                         held_days=CGT_DISCOUNT_ELIGIBLE_DAYS)


def cgt_twelve_month_counterfactual(sale_value, cost_base, tax_rate, brokerage, held_days):
    """None if the position already qualifies for the discount (held_days
    >= 365) or any required input is missing - the chip only has
    something useful to say for a position that HASN'T reached 12
    months yet. Otherwise {"tax_now", "tax_after_12mo", "extra_tax_now",
    "days_remaining"} - purely a same-value comparison (assumes the
    position is still worth `sale_value` on the later date, which the UI
    must state plainly since that's obviously not guaranteed)."""
    if held_days is None or held_days >= CGT_DISCOUNT_ELIGIBLE_DAYS:
        return None
    now = toll_without_discount(sale_value, cost_base, tax_rate, brokerage)
    later = toll_with_discount(sale_value, cost_base, tax_rate, brokerage)
    if now is None or later is None:
        return None
    return {
        "tax_now": now["tax"],
        "tax_after_12mo": later["tax"],
        "extra_tax_now": now["tax"] - later["tax"],
        "days_remaining": CGT_DISCOUNT_ELIGIBLE_DAYS - held_days,
    }


def annualised_toll_rate(sale_value, proceeds_after_toll, years):
    """Z = (V / P) ** (1/N) - 1 - the annualised return drag the switch
    must overcome. None if proceeds_after_toll <= 0 (the toll consumed
    the whole position - see compute_toll's own docstring) or sale_value
    <= 0 or years <= 0."""
    if not sale_value or not proceeds_after_toll or proceeds_after_toll <= 0 or not years or years <= 0:
        return None
    return (sale_value / proceeds_after_toll) ** (1.0 / years) - 1.0


def expected_rerating_return(price, fair_value, years):
    """The SAME family of formula as the toll bridge, applied to a
    model's own fair-value estimate instead of a sale/proceeds pair:
    "if this ticker's price re-rated all the way to the model's fair
    value over N years, what constant annual return would that imply".
    None if price/fair_value aren't both positive, or years <= 0. A
    fair_value below price implies a NEGATIVE expected return (the
    model considers it overvalued) - that is reported as-is, not
    floored at zero, since the caller (the verdict) needs the true
    signed spread."""
    if not price or not fair_value or price <= 0 or fair_value <= 0 or not years or years <= 0:
        return None
    return (fair_value / price) ** (1.0 / years) - 1.0


def break_even_years(sale_value, proceeds_after_toll, return_spread):
    """Closed-form inversion of the SAME Z = (V/P)**(1/N) - 1 formula:
    given a (assumed-constant) annual return-spread B is expected to
    hold over A, solve for the number of years N* at which the
    annualised toll would exactly equal that spread:
        N* = ln(V / P) / ln(1 + return_spread)
    Only meaningful for return_spread > 0 (with return_spread <= 0 the
    switch never breaks even on a bigger annual edge - a bigger N alone
    won't save it either, since Z(N) only ever falls toward the SAME
    return_spread from above; see the caller for the "never flips"
    framing). None if sale_value/proceeds_after_toll aren't both
    positive."""
    if not sale_value or not proceeds_after_toll or sale_value <= 0 or proceeds_after_toll <= 0:
        return None
    if return_spread is None or return_spread <= 0:
        return None
    ratio = sale_value / proceeds_after_toll
    if ratio <= 1:
        return 0.0
    return math.log(ratio) / math.log(1.0 + return_spread)


def price_flip_for_incumbent(fair_value, target_return, years):
    """Addendum to Fix #3b ("Value Opportunity gets graphs"): inverse of
    expected_rerating_return() - the incumbent's OWN share price P at
    which its own implied return, expected_rerating_return(P, fair_value,
    years), would equal `target_return` exactly. The crossover chart/box
    calls this with target_return = (the candidate's implied return -
    today's annualised toll), so the returned P is "the price at which
    net = candidate_return - incumbent_return(P) - toll would hit zero" -
    the same "when would this flip" question the tab's own flip_title
    copy already asks, just solved for a hypothetical PRICE instead of a
    hypothetical holding PERIOD (see break_even_years above for that
    other inversion of the same family of formula).

    Derivation: expected_rerating_return(P, FV, N) = (FV/P)**(1/N) - 1
    = target_return  =>  FV/P = (1+target_return)**N  =>
    P = FV / (1+target_return)**N.

    None if fair_value isn't positive, years isn't positive, or
    (1 + target_return) isn't positive (a target_return at or below
    -100%/yr has no real solution - never guessed)."""
    if not fair_value or fair_value <= 0 or not years or years <= 0:
        return None
    base = 1.0 + (target_return if target_return is not None else 0.0)
    if base <= 0:
        return None
    return fair_value / (base ** years)


def verdict(return_spread, toll_rate):
    """{"passes": bool, "margin_pct": float} or None if either input is
    missing. margin_pct is signed: positive = clears the toll by that
    many points/yr, negative = falls short by that many points/yr -
    the caller renders "passes/fails by ~X%/yr" straight off its sign,
    never a buy/sell instruction."""
    if return_spread is None or toll_rate is None:
        return None
    margin = return_spread - toll_rate
    return {"passes": margin >= 0, "margin_pct": margin}


def blended_expected_return(return_a, return_b, trim_fraction):
    """Weighted blend of two per-annum return estimates by the fraction
    of the position being trimmed out of A and into B (0 = no trim / all
    A, 1 = full switch / all B). None if either return is missing."""
    if return_a is None or return_b is None or trim_fraction is None:
        return None
    trim_fraction = min(max(trim_fraction, 0.0), 1.0)
    return return_a * (1 - trim_fraction) + return_b * trim_fraction


def trimmed_toll(sale_value, cost_base, tax_rate, brokerage, trim_fraction, held_days=None):
    """The toll for selling only `trim_fraction` of the holding (gain and
    cost base both scale with the fraction sold; brokerage does NOT -
    it's a flat fee per trade regardless of size, so a small trim pays
    proportionally MORE in brokerage drag than a full switch - this is
    reported plainly via toll_pct_of_value, never smoothed away)."""
    if sale_value is None or cost_base is None or trim_fraction is None:
        return None
    trim_fraction = min(max(trim_fraction, 0.0), 1.0)
    return compute_toll(
        sale_value * trim_fraction, cost_base * trim_fraction,
        tax_rate, brokerage, held_days=held_days,
    )


def concentration_flag(candidate_resulting_pct):
    """True if the candidate's resulting share of total portfolio value
    (after the switch) would exceed CONCENTRATION_FLAG_PCT. None input
    -> False (never flag on missing data)."""
    if candidate_resulting_pct is None:
        return False
    return candidate_resulting_pct > CONCENTRATION_FLAG_PCT


def high_correlation_flag(correlation_vs_rest_of_portfolio):
    """True if the candidate's correlation to the rest of the portfolio
    (excluding the holding being switched out of) is >= the declared
    threshold. None input (not enough overlapping history to compute a
    correlation at all) -> False, never a guess."""
    if correlation_vs_rest_of_portfolio is None:
        return False
    return correlation_vs_rest_of_portfolio >= HIGH_CORRELATION_FLAG
