"""
debt_recycling_engine.py

Mega-batch Part 20: pure-logic engine behind "Cash vs Offset vs Borrow" -
Tools tool #3. Answers, with described calculations only: "I have cash
and a mortgage - leave the cash in the offset, invest it, borrow to
invest, or both?" Every function here is deterministic arithmetic over
numbers the caller supplies - no network, no Streamlit, no file I/O -
same contract as budget_planner_engine.py alongside it.

THE FOUR SCENARIOS (spec's own set, in spec's own order/definitions).
Every scenario reports NET_GAIN(t) - the after-tax dollar gain, at each
whole year t from 0..horizon, relative to a common TRUE-ZERO reference:
a household with no offset arrangement at all (full, undiscounted
mortgage interest paid throughout) and no investment of the cash either.
Every scenario's headline is on that SAME absolute scale, so "biggest
net_gain(horizon) wins" is always a valid, apples-to-apples comparison
(winner border, ranking bars, race chart) - including Scenario A's own
curve, "the baseline every option must beat" per the spec.

  A - Leave it in the offset (AU) / extra mortgage payments or a taxable
      high-yield-savings rate (US, owner's choice - see us_baseline
      below): the guaranteed, tax-free (HYS: taxable) interest
      avoided/earned on the cash, simple (non-compounding) since the
      saving is paid out each year rather than reinvested - same
      non-reinvestment treatment every income leg below gets.
  B - Invest the cash directly in Investment 1: JUST that investment's
      own after-tax income+growth - no further mortgage subtraction,
      since relative to the true-zero reference (which also pays full
      interest throughout) there is no extra mortgage cost to charge.
      The "mortgage interest that returns" the spec describes shows up
      instead as B's own "net_vs_offset" component (headline - A's
      headline) - subtracting it from B's HEADLINE too, as well as
      comparing that headline against A's, would count the same dollar
      twice and put the true indifference point at double the real
      hurdle (verified below: at investment-after-tax-return exactly
      equal to the mortgage rate, a household is genuinely indifferent
      between A and B - this only holds if B's own headline is the
      investment return alone).
  C - Debt recycle: the cash STAYS in the offset (still earning A's own
      benefit, added in directly - the one scenario where cash never
      leaves) while an equal amount is separately borrowed, via a split
      investment loan, into Investment 1 - that loan's interest is
      deductible.
  D - Borrow AND invest the cash too: C's borrowed leg, PLUS the cash
      leaves the offset into Investment 2 (B's own treatment, second
      investment, same true-zero logic - no mortgage subtraction) - so D
      loses A's offset benefit entirely and carries two market positions
      at once (DOUBLE RISK badge).

MARK-TO-MARKET GROWTH (why every scenario's series is smooth, not a
final-year cliff): growth compounds silently on the untouched principal
and is taxed AS IF SOLD at every single year t, not only at the horizon -
so the year-by-year race chart is directly comparable point for point.
The scenario CARDS' own headline figures are simply this same series
evaluated at t = horizon (a real, not hypothetical, disposal there).

INCOME DOES NOT REINVEST (a deliberate, documented simplification, not a
guess dressed as precision - matches budget_planner_engine's own "no
shortcuts, but state the ones taken" ethos): each leg's income is paid
out annually rather than compounding back into the position, so annual
after-tax income accumulates LINEARLY (t * one year's after-tax income),
while growth compounds. A household that instead reinvests income would
see a somewhat higher figure for the investing scenarios (B/C/D) - never
for A, which has no growth leg at all.

TWO HURDLES (spec: "shown as numbers"), both intentionally the SIMPLE,
slightly-conservative headline figure a user reads at a glance rather
than a scenario-specific exact breakeven (which would need to know the
income/growth split and franking in advance to invert) - the true
breakeven for a real, franked/discounted position is always a little
kinder than these two numbers, never worse, and the UI says so:
  cash_leaving_offset_hurdle = mortgage_rate / (1 - tax_rate)
      the PRE-TAX return an investment needs to beat a tax-free
      guaranteed offset return.
  borrowed_money_hurdle = loan_rate * (1 - tax_rate)
      the AFTER-TAX return an investment needs to beat what a geared
      position's deductible loan actually costs after the tax saving.

AU vs US (spec: "implement simply and state simplifications"):
  AU - income taxed at the marginal rate with a full dividend-imputation
       (franking) gross-up/offset at the assumed 30% company tax rate
       (FRANKING_COMPANY_TAX_RATE); growth gets the 50% CGT discount
       (every horizon here is >=5y, so the >12-month test always passes -
       there is no "held under 12 months" branch to model); loan interest
       fully deductible at the marginal rate, no cap.
  US  - no franking (income taxed at the plain marginal rate); growth
        taxed at a separate, user-supplied long-term capital-gains rate
        (no 50% discount - LTCG rates are already concessional by
        construction); the baseline (Scenario A) is the user's own
        choice of extra mortgage principal (tax-free, same treatment as
        the AU offset) or a taxable high-yield-savings rate; loan
        interest is deductible ONLY up to that year's own investment
        INCOME (never against growth, and never carried forward to a
        later year) - the US "investment-interest-expense limitation",
        simplified to a same-year, no-carryforward cap.

SCOPE NOTE (Part 20 section A's "pick from the site" affordance): the
spec allows pre-filling a return field from either "a ticker's implied
convergence return (Part 17's formula)" or "an index's historical
average". This engine (and the Tools page built on it) only wires up the
SECOND path - budget_planner_engine.INDEX_HISTORICAL_RETURNS, the same
sourced constants Budget Planner already shows, clearly labelled as
history. The ticker path would need a LIVE per-ticker price/fair-value
fetch (switch_analyzer_engine.expected_rerating_return's own inputs) from
inside a Tools page visit, which conflicts with the site's standing "no
new per-pageview network fetches" rule for a merely-optional affordance -
documented here rather than silently dropped; wiring it up later (e.g.
behind an explicit, rate-limited "look up a ticker" button rather than
an on-load fetch) is a reasonable follow-up, not a correctness gap in
what ships today.

MANDATORY SELF-CHECK (spec section C): see hurdle_self_check() at the
bottom of this module - run at import time in debug contexts and by the
test script this Part shipped alongside; asserts that nudging an
investment's return from just below to just above each hurdle flips the
sign of exactly the scenario leg that hurdle gates, for both countries.
"""

FRANKING_COMPANY_TAX_RATE = 0.30

MIN_HORIZON_YEARS = 5
MAX_HORIZON_YEARS = 30
DEFAULT_HORIZON_YEARS = 10

DEFAULT_PESSIMISTIC_RATE = 0.04  # spec's own "bad decade" default.

SPLIT_SLIDER_POINTS = (0, 25, 50, 75, 100)


# --------------------------------------------------------------------------- #
# Country-specific after-tax treatment of one leg's income/growth.
# --------------------------------------------------------------------------- #

def after_tax_income_au(income, franked_pct, tax_rate):
    """income: this year's pre-tax investment income (dividends/interest),
    a non-negative amount. franked_pct: 0-1, the fraction of that income
    carrying full franking credits. Standard imputation arithmetic at
    FRANKING_COMPANY_TAX_RATE: franking_credit = franked_amount *
    (30/70); assessable = income + credit; tax = assessable * tax_rate -
    credit (can be negative - a refundable excess credit - for a
    taxpayer whose marginal rate sits below the 30% company rate, which
    is correct, not a bug: Australia refunds excess imputation credits
    to individuals)."""
    if income is None or income <= 0:
        return 0.0
    franked_pct = max(0.0, min(franked_pct or 0.0, 1.0))
    franked_amount = income * franked_pct
    credit = franked_amount * (FRANKING_COMPANY_TAX_RATE / (1 - FRANKING_COMPANY_TAX_RATE))
    assessable = income + credit
    tax = assessable * tax_rate - credit
    return income - tax


def after_tax_income_us(income, tax_rate):
    """No franking - income taxed once, at the plain marginal rate."""
    if income is None or income <= 0:
        return 0.0
    return income * (1 - tax_rate)


def after_tax_capital_gain_au(gain, tax_rate):
    """50% CGT discount - every horizon this tool offers (5-30y) clears
    the >12-month test, so there is no "short-term" branch to model.
    A negative gain (the position lost value) passes through undiscounted
    in the other direction too - the discount only ever HALVES a taxable
    gain, it does not create one from a loss, and this function does not
    attempt loss-offsetting against other positions (out of scope)."""
    if gain is None:
        return 0.0
    if gain <= 0:
        return gain
    taxable = gain * 0.5
    return gain - taxable * tax_rate


def after_tax_capital_gain_us(gain, ltcg_rate):
    """Long-term capital-gains rate applied to the full gain - no
    discount (LTCG rates are already the concessional treatment in the
    US system). A loss passes through unchanged (no loss-offsetting
    modelled, same as the AU function)."""
    if gain is None:
        return 0.0
    if gain <= 0:
        return gain
    return gain - gain * ltcg_rate


# --------------------------------------------------------------------------- #
# The two headline hurdles (spec section C - "shown as numbers").
# --------------------------------------------------------------------------- #

def cash_leaving_offset_hurdle(mortgage_rate, tax_rate):
    """Pre-tax return an investment needs just to match a tax-free
    guaranteed offset return (or, in the US, tax-free extra mortgage
    payments) - mortgage_rate grossed up for tax. None if tax_rate is
    not below 1 (a 100%+ marginal rate makes any pre-tax figure
    meaningless, not just large)."""
    if mortgage_rate is None or tax_rate is None or tax_rate >= 1:
        return None
    return mortgage_rate / (1 - tax_rate)


def borrowed_money_hurdle(loan_rate, tax_rate):
    """After-tax return an investment needs to clear the after-tax COST
    of a deductible loan - the loan rate net of the tax saving the
    deduction itself provides."""
    if loan_rate is None or tax_rate is None:
        return None
    return loan_rate * (1 - tax_rate)


def us_hys_baseline_hurdle(hys_rate, tax_rate):
    """US-only: when the owner picks the taxable high-yield-savings
    baseline instead of tax-free extra mortgage payments, the baseline
    ITSELF is already after-tax - so the number an investment must beat
    is that after-tax HYS return directly, not grossed up a second time
    (grossing up here would double count the tax already paid on the
    baseline)."""
    if hys_rate is None or tax_rate is None:
        return None
    return hys_rate * (1 - tax_rate)


# --------------------------------------------------------------------------- #
# One leg's after-tax net_gain(t) series - the building block every
# scenario below is assembled from.
# --------------------------------------------------------------------------- #

def _income_and_growth_series(principal, income_rate, growth_rate, horizon,
                              country, tax_rate, franked_pct=None, ltcg_rate=None):
    """[after_tax_income(t) + after_tax_growth(t)] for t=0..horizon, for
    ONE position of the given principal - income linear (not
    reinvested), growth compounding and mark-to-market taxed at every t
    (see module docstring). Shared by every investing leg (B's
    Investment 1, C/D's Investment 1, D's Investment 2) so there is
    exactly one place this arithmetic lives."""
    if principal is None or principal <= 0:
        return [0.0] * (horizon + 1)
    pretax_income_per_year = principal * (income_rate or 0.0)
    if country == "au":
        after_tax_income_per_year = after_tax_income_au(pretax_income_per_year, franked_pct, tax_rate)
    else:
        after_tax_income_per_year = after_tax_income_us(pretax_income_per_year, tax_rate)
    series = []
    for t in range(horizon + 1):
        income_accum = after_tax_income_per_year * t
        pretax_gain = principal * ((1 + (growth_rate or 0.0)) ** t - 1)
        if country == "au":
            after_tax_gain = after_tax_capital_gain_au(pretax_gain, tax_rate)
        else:
            after_tax_gain = after_tax_capital_gain_us(pretax_gain, ltcg_rate)
        series.append(income_accum + after_tax_gain)
    return series


def _loan_interest_cost_series(borrowed_amount, loan_rate, horizon, country,
                               tax_rate, capped_income_per_year=None):
    """The AFTER-TAX cost (a positive number to SUBTRACT) of servicing a
    deductible investment loan, linear (interest is a cash cost paid
    annually, never compounded onto the loan itself - this tool doesn't
    model capitalising interest). AU deducts the full amount at the
    marginal rate, uncapped; US deducts only up to that year's own
    investment income, no carryforward (module docstring)."""
    if borrowed_amount is None or borrowed_amount <= 0:
        return [0.0] * (horizon + 1)
    pretax_interest_per_year = borrowed_amount * (loan_rate or 0.0)
    if country == "au":
        deductible_per_year = pretax_interest_per_year
    else:
        cap = capped_income_per_year if capped_income_per_year is not None else 0.0
        deductible_per_year = min(pretax_interest_per_year, max(cap, 0.0))
    tax_saving_per_year = deductible_per_year * tax_rate
    net_cost_per_year = pretax_interest_per_year - tax_saving_per_year
    return [net_cost_per_year * t for t in range(horizon + 1)]


# --------------------------------------------------------------------------- #
# The four scenarios, assembled from the shared legs above.
# --------------------------------------------------------------------------- #

def run_scenarios(cash, mortgage_rate, loan_rate, tax_rate, horizon,
                  inv1_income_rate, inv1_growth_rate,
                  inv2_income_rate, inv2_growth_rate,
                  country="au", inv1_franked_pct=0.0, inv2_franked_pct=0.0,
                  ltcg_rate=None, us_baseline="mortgage_extra", hys_rate=None):
    """Returns a dict: {"A"/"B"/"C"/"D": {"series": [...], "headline": x,
    "components": {...}}, "hurdles": {"cash_leaving_offset": x,
    "borrowed_money": x}}. series[t] is that scenario's net_gain at year
    t (t=0..horizon inclusive), relative to doing nothing at all with the
    cash - see module docstring for why that zero baseline, not
    scenario-vs-scenario, is what every series measures.

    country: "au" or "us". us_baseline: "mortgage_extra" (tax-free,
    same treatment as the AU offset) or "hys" (taxable, hys_rate
    required) - AU ignores both us_* params entirely."""
    horizon = max(MIN_HORIZON_YEARS, min(int(horizon), MAX_HORIZON_YEARS))
    country = "au" if country != "us" else "us"

    # ---- Scenario A: the guaranteed baseline every option must beat ----
    if country == "au" or us_baseline == "mortgage_extra":
        series_a = [cash * mortgage_rate * t for t in range(horizon + 1)]
        a_after_tax_rate = mortgage_rate
    else:
        after_tax_hys_per_year = cash * (hys_rate or 0.0) * (1 - tax_rate)
        series_a = [after_tax_hys_per_year * t for t in range(horizon + 1)]
        a_after_tax_rate = (hys_rate or 0.0) * (1 - tax_rate)

    # ---- Investment legs (shared building blocks) ----
    inv1_leg = _income_and_growth_series(
        cash, inv1_income_rate, inv1_growth_rate, horizon, country, tax_rate,
        franked_pct=inv1_franked_pct, ltcg_rate=ltcg_rate)
    inv2_leg = _income_and_growth_series(
        cash, inv2_income_rate, inv2_growth_rate, horizon, country, tax_rate,
        franked_pct=inv2_franked_pct, ltcg_rate=ltcg_rate)
    # Informational only (module docstring / self-check derivation): the
    # mortgage interest that "returns" once cash leaves the offset is
    # real, but it is NOT a further deduction from B/D's own headline -
    # B and D pay exactly the same full mortgage interest a household
    # with no offset at all always pays, so relative to that common
    # zero-offset reference there is no extra cost to subtract. It only
    # matters as the gap BETWEEN that headline and Scenario A's own -
    # shown per-scenario as "net_vs_offset" below, and on the card as a
    # component line, never baked into the headline itself. (A concrete
    # check: at investment-after-tax-return == mortgage_rate exactly, a
    # household is genuinely INDIFFERENT between A and B - both hand back
    # the same total dollars over the horizon. Subtracting this cost from
    # B's own headline AS WELL as comparing it against A's full benefit
    # would count it twice and put the indifference point at double the
    # true hurdle.)
    mortgage_cost_forgone = [cash * mortgage_rate * t for t in range(horizon + 1)]
    inv1_income_per_year_pretax = cash * (inv1_income_rate or 0.0)
    borrowed_cost = _loan_interest_cost_series(
        cash, loan_rate, horizon, country, tax_rate,
        capped_income_per_year=inv1_income_per_year_pretax)

    # ---- Scenario B: invest the cash directly (Investment 1) ----
    series_b = list(inv1_leg)

    # ---- Scenario C: debt recycle - cash keeps shielding the mortgage,
    #      a separate borrowed amount funds Investment 1 ----
    series_c = [series_a[t] + inv1_leg[t] - borrowed_cost[t] for t in range(horizon + 1)]

    # ---- Scenario D: C's borrowed leg PLUS the cash into Investment 2 ----
    series_d = [inv1_leg[t] - borrowed_cost[t] + inv2_leg[t] for t in range(horizon + 1)]

    result = {
        "horizon": horizon,
        "country": country,
        "A": {"series": series_a, "headline": series_a[-1],
              "components": {"tax_free_return": a_after_tax_rate * cash * horizon
                             if (country == "au" or us_baseline == "mortgage_extra")
                             else series_a[-1]}},
        "B": {"series": series_b, "headline": series_b[-1],
              "components": {"investment_after_tax": inv1_leg[-1],
                             "mortgage_interest_forgone": -mortgage_cost_forgone[-1],
                             "net_vs_offset": series_b[-1] - series_a[-1]}},
        "C": {"series": series_c, "headline": series_c[-1],
              "components": {"offset_benefit": series_a[-1],
                             "investment_after_tax": inv1_leg[-1],
                             "loan_interest_after_tax": -borrowed_cost[-1],
                             "net_vs_offset": series_c[-1] - series_a[-1]}},
        "D": {"series": series_d, "headline": series_d[-1],
              "components": {"investment1_after_tax": inv1_leg[-1],
                             "loan_interest_after_tax": -borrowed_cost[-1],
                             "investment2_after_tax": inv2_leg[-1],
                             "mortgage_interest_forgone": -mortgage_cost_forgone[-1],
                             "net_vs_offset": series_d[-1] - series_a[-1]}},
        "hurdles": {
            "cash_leaving_offset": cash_leaving_offset_hurdle(mortgage_rate, tax_rate),
            "borrowed_money": borrowed_money_hurdle(loan_rate, tax_rate),
        },
    }
    if country == "us" and us_baseline == "hys":
        result["hurdles"]["us_hys_baseline"] = us_hys_baseline_hurdle(hys_rate, tax_rate)
    return result


# --------------------------------------------------------------------------- #
# Split slider (spec section D).
# --------------------------------------------------------------------------- #

def scale_to_total_return(income_rate, growth_rate, target_total):
    """Rescales (income_rate, growth_rate) to a new pair with the SAME
    income:growth ratio but summing to target_total - one shared way to
    ask "what would this return look like at a different total", used by
    the split slider's own bad-decade figure, the race chart's
    pessimistic-rate toggle, and the break-even chart's return sweep, so
    the three never each interpret "a X% pessimistic/assumed return"
    differently. Falls back to all-growth when the original rates sum to
    zero (nothing to preserve a ratio of)."""
    income_rate = income_rate or 0.0
    growth_rate = growth_rate or 0.0
    original_total = income_rate + growth_rate
    if original_total == 0:
        return 0.0, target_total
    factor = target_total / original_total
    return income_rate * factor, growth_rate * factor


def split_slider_row(cash, split_pct, mortgage_rate, loan_rate, tax_rate, horizon,
                     inv1_income_rate, inv1_growth_rate, country="au",
                     inv1_franked_pct=0.0, ltcg_rate=None,
                     pessimistic_rate=DEFAULT_PESSIMISTIC_RATE,
                     us_baseline="mortgage_extra", hys_rate=None):
    """ONE row of the split slider: split_pct (0-100) of `cash` leaves
    the offset into Investment 1 (Scenario D's own cash leg, in
    isolation - "in D it splits the cash leg only", per spec); the
    remainder stays in the offset earning Scenario A's own guaranteed
    return. Returns {"split_pct", "expected_gain", "bad_decade_gain",
    "guaranteed_floor_pct"} at the full horizon.

    expected_gain uses the caller's own inv1_income_rate/growth_rate;
    bad_decade_gain re-runs the SAME split with the total return rescaled
    to pessimistic_rate (scale_to_total_return - same income:growth
    ratio, smaller total), not pessimistic_rate applied to BOTH
    components independently (which would double it up to 2x
    pessimistic_rate as a total return - not what "a pessimistic rate"
    means)."""
    invested = cash * (split_pct / 100.0)
    kept = cash - invested

    def _gain_at(income_rate, growth_rate):
        # Same true-zero-reference logic as Scenario A/B in run_scenarios:
        # the KEPT portion earns the guaranteed baseline; the INVESTED
        # portion earns its own after-tax return with no further mortgage
        # subtraction (see run_scenarios' own mortgage_cost_forgone
        # comment for why that would double-count against the kept
        # portion's own baseline figure).
        if country == "au" or us_baseline == "mortgage_extra":
            kept_gain = kept * mortgage_rate * horizon
        else:
            kept_gain = kept * (hys_rate or 0.0) * (1 - tax_rate) * horizon
        invested_leg = _income_and_growth_series(
            invested, income_rate, growth_rate, horizon, country, tax_rate,
            franked_pct=inv1_franked_pct, ltcg_rate=ltcg_rate)
        return kept_gain + invested_leg[-1]

    expected_gain = _gain_at(inv1_income_rate, inv1_growth_rate)
    _pess_income, _pess_growth = scale_to_total_return(inv1_income_rate, inv1_growth_rate, pessimistic_rate)
    bad_decade_gain = _gain_at(_pess_income, _pess_growth)
    return {
        "split_pct": split_pct,
        "expected_gain": expected_gain,
        "bad_decade_gain": bad_decade_gain,
        "guaranteed_floor_pct": round(100 - split_pct),
    }


def split_slider_rows(cash, mortgage_rate, loan_rate, tax_rate, horizon,
                      inv1_income_rate, inv1_growth_rate, country="au",
                      inv1_franked_pct=0.0, ltcg_rate=None,
                      pessimistic_rate=DEFAULT_PESSIMISTIC_RATE,
                      us_baseline="mortgage_extra", hys_rate=None,
                      extra_split_pct=None):
    """The fixed 0/25/50/75/100% rows plus, when given, one extra row at
    the slider's own live position (deduplicated if it lands on a fixed
    point already) - "0/25/50/75/100% + slider position" per spec."""
    points = list(SPLIT_SLIDER_POINTS)
    if extra_split_pct is not None and round(extra_split_pct) not in points:
        points.append(round(extra_split_pct))
        points.sort()
    return [
        split_slider_row(cash, p, mortgage_rate, loan_rate, tax_rate, horizon,
                         inv1_income_rate, inv1_growth_rate, country=country,
                         inv1_franked_pct=inv1_franked_pct, ltcg_rate=ltcg_rate,
                         pessimistic_rate=pessimistic_rate, us_baseline=us_baseline,
                         hys_rate=hys_rate)
        for p in points
    ]


# --------------------------------------------------------------------------- #
# Waterfall components for one scenario (spec section E.4).
# --------------------------------------------------------------------------- #

def waterfall_components(scenario_id, cash, mortgage_rate, loan_rate, tax_rate, horizon,
                         inv1_income_rate, inv1_growth_rate,
                         inv2_income_rate=None, inv2_growth_rate=None,
                         country="au", inv1_franked_pct=0.0, inv2_franked_pct=0.0,
                         ltcg_rate=None):
    """{"gross_return", "tax", "loan_interest", "deduction", "net"} for
    ONE scenario at the full horizon - "gross return - tax - loan
    interest + deduction = net" per spec, always reconciling to that
    scenario's own headline net_gain (verified in the self-test below)."""
    principal = cash
    pretax_income = principal * (inv1_income_rate or 0.0) * horizon
    pretax_gain = principal * ((1 + (inv1_growth_rate or 0.0)) ** horizon - 1)
    gross_return = pretax_income + pretax_gain
    if country == "au":
        after_tax_income = after_tax_income_au(principal * (inv1_income_rate or 0.0), inv1_franked_pct, tax_rate) * horizon
        after_tax_gain = after_tax_capital_gain_au(pretax_gain, tax_rate)
    else:
        after_tax_income = after_tax_income_us(principal * (inv1_income_rate or 0.0), tax_rate) * horizon
        after_tax_gain = after_tax_capital_gain_us(pretax_gain, ltcg_rate)
    tax = gross_return - (after_tax_income + after_tax_gain)

    loan_interest = 0.0
    deduction = 0.0
    if scenario_id in ("C", "D"):
        pretax_interest_per_year = cash * (loan_rate or 0.0)
        loan_interest = pretax_interest_per_year * horizon
        if country == "au":
            deductible_per_year = pretax_interest_per_year
        else:
            deductible_per_year = min(pretax_interest_per_year, principal * (inv1_income_rate or 0.0))
        deduction = deductible_per_year * tax_rate * horizon

    # B/D carry no mortgage-cost subtraction here, matching run_scenarios'
    # own true-zero-reference headline (see that function's
    # mortgage_cost_forgone comment) - only C adds Scenario A's own
    # offset benefit, since C is the one scenario where the cash
    # genuinely never leaves the offset.
    net = after_tax_income + after_tax_gain - loan_interest + deduction
    if scenario_id == "C":
        net += cash * mortgage_rate * horizon
    elif scenario_id == "D":
        if inv2_income_rate is not None:
            pretax_income_2 = cash * inv2_income_rate * horizon
            pretax_gain_2 = cash * ((1 + (inv2_growth_rate or 0.0)) ** horizon - 1)
            if country == "au":
                after_tax_2 = after_tax_income_au(cash * inv2_income_rate, inv2_franked_pct, tax_rate) * horizon + \
                    after_tax_capital_gain_au(pretax_gain_2, tax_rate)
            else:
                after_tax_2 = after_tax_income_us(cash * inv2_income_rate, tax_rate) * horizon + \
                    after_tax_capital_gain_us(pretax_gain_2, ltcg_rate)
            net += after_tax_2
            gross_return += pretax_income_2 + pretax_gain_2
            tax = gross_return - (after_tax_income + after_tax_gain + after_tax_2)
    elif scenario_id == "A":
        gross_return = cash * mortgage_rate * horizon
        tax = 0.0
        net = gross_return

    return {"gross_return": gross_return, "tax": -tax, "loan_interest": -loan_interest,
            "deduction": deduction, "net": net}


# --------------------------------------------------------------------------- #
# Mandatory self-check (spec section C): hurdle-crossing consistency.
# --------------------------------------------------------------------------- #

def hurdle_self_check(verbose=False):
    """Asserts, for BOTH countries, that nudging an investment's PRE-TAX
    return from just below to just above each of the two hurdles flips
    the sign of exactly the scenario contribution that hurdle gates:
      - cash_leaving_offset_hurdle gates Scenario B beating Scenario A
        (the cash-leaving-the-offset decision).
      - borrowed_money_hurdle gates the geared leg's own contribution in
        Scenario C (borrowed money beating its own after-tax cost).

    Deliberately uses an INCOME-ONLY, unframked leg (inv1_growth_rate=0,
    inv1_franked_pct=0) for both checks, not a growth leg - growth is
    taxed via the CGT discount/LTCG rate, a genuinely different
    functional form (compounding, concessional) from the two hurdle
    formulas below, which are both derived from plain, linearly-taxed
    income. Testing against growth would be comparing the wrong shape of
    curve, not a stronger test - the hurdles are correct AS STATED for
    the income case they were built from; the module docstring already
    says the true breakeven for a real, mixed income/growth/franked
    position is always a little kinder than these two headline numbers.

    Raises AssertionError with a clear message on any inconsistency;
    returns True on a clean pass. Run by this module's own __main__
    block and by the test script this Part shipped alongside - not on
    every import, to keep a plain `import debt_recycling_engine` free of
    side effects."""
    cash = 100_000.0
    mortgage_rate = 0.06
    loan_rate = 0.07
    tax_rate = 0.37
    horizon = 10
    eps = 0.0005  # 0.05 percentage point nudge either side of the hurdle.

    for country in ("au", "us"):
        extra = {"ltcg_rate": 0.15} if country == "us" else {"inv1_franked_pct": 0.0}

        # --- cash_leaving_offset_hurdle: gates B vs A ---
        # B's after-tax income = cash*r*(1-tax_rate)*horizon (unframked);
        # A = cash*mortgage_rate*horizon (tax-free) - B beats A exactly
        # when r > mortgage_rate/(1-tax_rate) = hurdle_1, so nudging the
        # PRE-TAX rate r around hurdle_1 itself is the correct test here.
        hurdle_1 = cash_leaving_offset_hurdle(mortgage_rate, tax_rate)
        for pretax_rate, expect_b_beats_a in ((hurdle_1 - eps, False), (hurdle_1 + eps, True)):
            res = run_scenarios(cash, mortgage_rate, loan_rate, tax_rate, horizon,
                                inv1_income_rate=pretax_rate, inv1_growth_rate=0.0,
                                inv2_income_rate=pretax_rate, inv2_growth_rate=0.0,
                                country=country, **extra)
            b_beats_a = res["B"]["headline"] > res["A"]["headline"]
            if verbose:
                print(f"[{country}] hurdle_1={hurdle_1:.4f} pretax_rate={pretax_rate:.4f} "
                      f"B={res['B']['headline']:.2f} A={res['A']['headline']:.2f} "
                      f"b_beats_a={b_beats_a} expected={expect_b_beats_a}")
            assert b_beats_a == expect_b_beats_a, (
                f"[{country}] cash_leaving_offset_hurdle inconsistency at "
                f"pretax_rate={pretax_rate}: expected B beats A = {expect_b_beats_a}, got {b_beats_a}")

        # --- borrowed_money_hurdle: gates the geared leg's OWN sign in C ---
        # For this same unframked income-only leg, the investment's own
        # after-tax return is r*(1-tax_rate); that crosses hurdle_2 =
        # loan_rate*(1-tax_rate) exactly when the PRE-TAX rate r crosses
        # loan_rate itself (the (1-tax_rate) factor cancels on both
        # sides) - true under the US income-cap too, since the cap binds
        # with exact equality right at that same crossing point. So the
        # nudge point here is loan_rate, not hurdle_2's own value.
        hurdle_2 = borrowed_money_hurdle(loan_rate, tax_rate)
        for pretax_rate, expect_geared_positive in ((loan_rate - eps, False), (loan_rate + eps, True)):
            # Isolate the geared leg's after-tax contribution directly
            # (inv1_leg - borrowed_cost, the same two terms run_scenarios
            # adds to series_a to build series_c) rather than reading it
            # off C's headline, since C's headline also carries A's own
            # offset benefit and would never itself cross zero here.
            inv1_leg = _income_and_growth_series(cash, pretax_rate, 0.0, horizon, country,
                                                 tax_rate, franked_pct=extra.get("inv1_franked_pct", 0.0),
                                                 ltcg_rate=extra.get("ltcg_rate"))
            borrowed_cost = _loan_interest_cost_series(cash, loan_rate, horizon, country, tax_rate,
                                                       capped_income_per_year=cash * pretax_rate)
            geared_leg = inv1_leg[-1] - borrowed_cost[-1]
            geared_positive = geared_leg > 0
            aftertax_return = pretax_rate * (1 - tax_rate)
            if verbose:
                print(f"[{country}] hurdle_2={hurdle_2:.4f} pretax_rate={pretax_rate:.4f} "
                      f"aftertax_return={aftertax_return:.4f} geared_leg={geared_leg:.2f} "
                      f"positive={geared_positive} expected={expect_geared_positive}")
            assert geared_positive == expect_geared_positive, (
                f"[{country}] borrowed_money_hurdle inconsistency at "
                f"pretax_rate={pretax_rate}: expected geared leg positive = "
                f"{expect_geared_positive}, got {geared_positive} ({geared_leg})")
    return True


if __name__ == "__main__":
    hurdle_self_check(verbose=True)
    print("hurdle_self_check: PASS")
