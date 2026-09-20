"""
property_vs_index_engine.py

Money Tools: "\U0001F3E0 Property vs \U0001F4C8 S&P 500" - pure-logic engine behind the
newest Money Tool. Mirrors debt_recycling_engine.py's own modelling
conventions exactly (see that module's docstring for the shared DNA):
simple (non-compounding) interest-only loan, after-tax income legs paid
out annually rather than reinvested, growth compounded and taxed AT
DISPOSAL with the 50% CGT discount. Every function here is
deterministic arithmetic over numbers the caller supplies - no network,
no Streamlit, no file I/O - same contract as debt_recycling_engine.py/
budget_planner_engine.py alongside it. Deliberately self-contained
(does NOT import debt_recycling_engine.py) so this tool's own engine
never depends on - or risks being disturbed by - a change to a
protected scoring/analysis-adjacent module; income_tax()/tax_on_extra()
below reimplement AU bracket tax independently for that reason.
debt_recycling_engine.py itself is never touched by this tool -
verified by diff.

THE QUESTION THIS ANSWERS: "I have `cash` and could borrow `loan` -
should I buy a `cash + loan` investment property, or just put `cash`
into the S&P 500?" Both sides are run over the SAME time horizon
against the SAME `other_income` (your taxable income excluding this
investment - Commit B, 20 Sep 2026, replacing a flat marginal-rate
input - see "TAX MODEL" below), so the two headline numbers are always
an apples-to-apples after-tax dollar comparison. AU-only (no US
branch) - the task spec's own inputs (stamp duty, Medicare levy,
negative gearing) are all AU-specific.

TAX MODEL (Commit B, 20 Sep 2026): real AU resident tax brackets
(BRACKETS below) + a flat 2% Medicare levy (MEDICARE_LEVY - no low-
income threshold/shading, no LITO - deliberately out of scope, see
that constant's own comment), via income_tax()/tax_on_extra(). This
replaced a single flat tax_rate = marginal_rate + medicare_levy that
every tax line used to multiply its own pretax amount by - wrong in
two places that matter: the negative-gearing refund (a loss x flat
rate is only correct if `other_income` sits in exactly the bracket
that flat rate represents) and CGT at sale (a large discounted gain
lands mostly in the TOP bracket it reaches, not at some blended flat
rate). Every tax line - rent, interest, holding costs, dividends,
capital gains - now goes through tax_on_extra(), and a whole year's
worth of lines are STACKED together via property_year_tax_legs()/
index_year_tax_legs() rather than each taxed in isolation against the
bare `other_income` (B5's own "the rental loss and the capital gain
land in the same tax return, so they must be computed together, not
separately" - see those two functions' own docstrings for exactly how
the stacking - and the sequential-stacking display-allocation
convention that gives a well-defined per-line split - works, including
why a capital LOSS is the one leg that never joins the stack).

PROPERTY SIDE - four lines, always summing exactly to the headline (the
Debt Recycling card's own "the numbers must add up on screen" rule):
  1. Capital growth after CGT - the property is assumed sold at `years`.
     cost base = price + buy_costs (stamp duty + legals, paid OUT OF THE
     CASH at purchase - never financed, and never affects the loan/price
     split); proceeds = future_price minus sell_costs (a % of the SALE
     price, not the purchase price); the 50% CGT discount applies to
     (proceeds - cost_base), taxed via the stacking described above (see
     property_year_tax_legs() below) - a loss (true for small enough
     `years`, since buy+sell costs alone are a real cost the first day)
     passes through UNDISCOUNTED and untaxed, the same convention debt_
     recycling_engine.after_tax_capital_gain_au() uses for a loss.
  2. Rent after tax - weekly rent x (52 - vacancy weeks), one year at a
     time (no CGT discount - rent is ordinary income, paid out each
     year, never reinvested, so it accumulates LINEARLY - same non-
     reinvestment convention debt_recycling_engine's module docstring
     documents for every income leg there), each year's own tax
     line stacked against that year's other recurring lines (see TAX
     MODEL above).
  3. Loan interest after deduction - negative gearing: the FULL interest
     bill is deductible (no cap - matches debt_recycling_engine's AU
     convention), simple interest-only, non-compounding.
  4. Holding costs after deduction - same deductible treatment as the
     loan interest line, for the property's other yearly running costs
     (maintenance/rates/insurance/management, entered as one number).

INDEX (S&P 500) SIDE - two lines summing to the headline:
  1. Growth after CGT - `capital_gain` IS the price-growth rate the
     balance compounds at, directly; `cash` compounds at that rate
     alone and is taxed at disposal with the same 50% CGT discount as
     the property side (no buy/sell costs on this side - a lump-sum
     index purchase's brokerage is not modelled, per the task spec's
     own inputs, which lists buy/sell costs only for the property).
  2. Dividends after tax - EACH year's dividend is that year's OWN
     start-of-year balance x dividend_yield (a genuinely compounding
     dividend STREAM, since the balance is growing at capital_gain even
     though dividend cash itself is paid out, not reinvested) - ordinary
     income, no franking modelled (the S&P 500 is a US index, DR's own
     AU-only franking treatment doesn't apply here), paid out and taxed
     one year at a time via the stacking described in TAX MODEL above -
     same non-reinvestment convention as the property's rent line.

  COMMIT A (20 Sep 2026): capital_gain and dividend_yield are
  INDEPENDENT, ADDITIVE inputs, not a total split into two parts. The
  ORIGINAL model had index_price_growth_rate(total_return,
  dividend_yield) return total_return - dividend_yield - the first box
  was a ceiling the second was carved out of, so raising the dividend
  yield LOWERED the price-growth component and the headline barely
  moved, even though the two boxes read as independent in the UI.
  index_price_growth_rate() now returns capital_gain unchanged
  (dividend_yield is accepted and ignored - kept only so this
  function's signature, and every call site below that still passes
  both args, needs no churn). Total return is now a DERIVED display
  value (capital_gain + dividend_yield, computed in the UI layer) -
  never an input anywhere in this engine. Dividends are still paid out,
  not reinvested, and still taxed annually, exactly as before - a
  reinvestment toggle was considered and dropped.

BOTH SIDES share the exact same 50%-CGT-discount treatment and the
exact same "linear, non-reinvested, one year's tax line stacked against
that year's other lines" income treatment - deliberately, so neither
side gets an uneven advantage baked into the model itself; only the
INPUTS (leverage, rent, growth assumptions) drive the comparison.

BREAK-EVEN RENT: the weekly rent at which the property's YEARLY CASH
bill (loan interest + holding costs, both PRE-tax - a cash-flow
question, not a tax one) is exactly covered by rent collected over the
same (52 - vacancy) working weeks: (loan x rate + holding_costs) /
(52 - vacancy).

AFTER-TAX WEEKLY SHORTFALL: at the user's OWN entered rent, how many
after-tax dollars per week the property costs (or pays back) on top of
its rent - (interest_after_tax_per_year + holding_costs_after_tax_per_
year - rent_after_tax_per_year) / 52 (a full CALENDAR year, unlike the
break-even rent above, which divides by the (52 - vacancy) WORKING weeks
rent is actually collected over - the shortfall is a smooth "cost per
week of ownership" figure, spread over every week of the year, not a
rent-conversion rate).

YEAR-BY-YEAR (MARK-TO-MARKET) SERIES for the crossover chart: exactly
the same formulas as the headline lines above, generalised to "if sold
at year t" for every t = 0..years - see debt_recycling_engine.py's own
module docstring ("MARK-TO-MARKET GROWTH") for why this pattern (tax the
position AS IF SOLD at every single year, not only at the horizon) keeps
the chart's two curves point-for-point comparable rather than a
final-year cliff.

LOAN STRUCTURE (Task, 19 Sep 2026, "realistic loan structure ... IO
period -> P&I"): the loan is no longer interest-only for the whole hold.
It has its own two fixed features, independent of how many years the
owner actually holds the property for - `io_period` (interest-only
years) and `term` (the loan's total term) - see property_loan_schedule()
below for the year-by-year mechanics. Years 1..io_period behave exactly
as before (balance constant, interest = loan x rate); from year
io_period+1 the loan amortises at a fixed ANNUAL principal-and-interest
payment A = loan x [r(1+r)^n]/[(1+r)^n - 1], n = term - io_period, same
annual (non-compounding-within-the-year) convention as the rest of this
module. Only the INTEREST portion of a P&I payment is ever deductible or
enters the headline through the "Loan interest after deduction" line;
the PRINCIPAL portion is never deducted and never enters the headline -
it converts cash into equity (reduces the loan balance) and nets out
exactly at sale, the same way the old lump interest-only repayment did.
REGRESSION ANCHOR: io_period == years leaves every modelled year in the
IO branch (the P&I phase is never reached within the hold), reproducing
the old pure-IO numbers to the cent - property_loan_schedule()'s own
docstring spells out why.
"""

DEFAULT_CASH = 300_000.0
DEFAULT_LOAN = 700_000.0
DEFAULT_LOAN_RATE = 0.0634
DEFAULT_WEEKLY_RENT = 650.0
DEFAULT_VACANCY_WEEKS = 2
DEFAULT_HOLDING_COSTS = 8_000.0
DEFAULT_PROPERTY_GROWTH = 0.05
DEFAULT_SP500_CAPITAL_GAIN = 0.077
DEFAULT_SP500_DIVIDEND_YIELD = 0.013
# Commit A (20 Sep 2026) back-compat alias: capital_gain and dividend_
# yield are now independent, additive inputs (see index_price_growth_
# rate()'s own docstring) - "total return" is a derived display value,
# never an input, but this name is kept for one release in case
# anything outside this module still imports it (grepped the whole
# codebase before this commit - nothing currently does). Computed from
# the two real defaults so it can never drift out of sync with them;
# reproduces the exact old 0.09 value at today's defaults.
DEFAULT_SP500_TOTAL_RETURN = DEFAULT_SP500_CAPITAL_GAIN + DEFAULT_SP500_DIVIDEND_YIELD

# Commit B (20 Sep 2026): the flat marginal_rate + medicare_levy input
# is gone entirely - see income_tax()/tax_on_extra() below.
# DEFAULT_MARGINAL_RATE is deleted outright (no back-compat alias, this
# time - a taxable-income figure isn't a renamed rate, it's a different
# KIND of input; there's no equivalent value to alias it to). Grepped
# the whole codebase first - nothing outside this module and the one
# app.py widget this task removes ever referenced it.
DEFAULT_OTHER_INCOME = 170_000.0

TAX_YEAR = "2026-27"
# Australian resident tax brackets, 2026-27 -
# https://www.ato.gov.au/tax-rates-and-codes/tax-rates-australian-residents
# Update this one constant (and TAX_YEAR above) when rates change - see
# income_tax() below, the only function that reads it.
BRACKETS = (
    (18_200, 0.00),
    (45_000, 0.15),
    (135_000, 0.30),
    (190_000, 0.37),
    (float("inf"), 0.45),
)
# Flat 2% of taxable income - deliberately NOT modelling the real
# Medicare levy's own low-income threshold/phase-in shading, and NOT
# modelling LITO either (task instruction, B3): both are real, but
# unverified for this tool and out of scope. A genuine simplification,
# stated plainly rather than silently assumed.
MEDICARE_LEVY = 0.02
DEFAULT_YEARS = 10
DEFAULT_BUY_COSTS = 45_000.0
DEFAULT_SELL_COSTS_PCT = 0.02
DEFAULT_IO_PERIOD = 5
DEFAULT_LOAN_TERM = 30

MIN_YEARS = 1
MAX_YEARS = 30
WEEKS_PER_YEAR = 52


# --------------------------------------------------------------------------- #
# Bracket-tax primitives (Commit B, 20 Sep 2026). Replace the old flat
# tax_rate = marginal_rate + medicare_levy scalar - every real tax line
# in this model used to multiply its own pretax amount by that ONE
# flat rate, which is wrong in two places that matter (B1's own "why"):
# the negative-gearing refund (loss x flat rate overstates or
# understates it depending on which bracket the loss actually falls
# in), and CGT at sale (a large discounted gain lands mostly in the top
# bracket, not at the flat rate). tax_on_extra() below is what every
# tax line in this model now goes through - see property_year_tax_legs()/
# index_year_tax_legs() further down for how a whole year's worth of
# lines are stacked together rather than each taxed in isolation
# against the bare salary (B5's own "must be computed together, not
# separately").
# --------------------------------------------------------------------------- #

def income_tax(taxable_income):
    """BRACKETS tax + flat MEDICARE_LEVY - never negative (a taxable_
    income <= 0 owes $0, it never generates a refund on its own -
    tax_on_extra() below is what turns a NEGATIVE marginal change into
    a refund, by comparing two income_tax() calls). No low-income
    Medicare threshold/phase-in shading, no LITO - see MEDICARE_LEVY's
    own comment for why both are deliberately out of scope."""
    taxable_income = max(taxable_income or 0.0, 0.0)
    tax = 0.0
    lower = 0
    for upper, rate in BRACKETS:
        if taxable_income <= lower:
            break
        tax += (min(taxable_income, upper) - lower) * rate
        lower = upper
        if taxable_income <= upper:
            break
    return tax + taxable_income * MEDICARE_LEVY


def marginal_rate_at(taxable_income):
    """The rate the NEXT dollar of `taxable_income` is taxed at,
    including the flat Medicare levy - FOR DISPLAY ONLY (the "Marginal
    rate 39%" caption), never for computing an actual tax line. A
    single point-in-time rate would get a STACKED amount wrong the
    moment it crosses a bracket boundary - every real tax line goes
    through tax_on_extra() below instead, which is exact regardless."""
    taxable_income = max(taxable_income or 0.0, 0.0)
    for upper, rate in BRACKETS:
        if taxable_income <= upper:
            return rate + MEDICARE_LEVY
    return BRACKETS[-1][1] + MEDICARE_LEVY


def tax_on_extra(other_income, extra):
    """income_tax(other_income + extra) - income_tax(other_income) -
    the one function every tax line in this model goes through. `extra`
    may be negative (a net rental loss, a single deductible leg), in
    which case the result is negative too and IS the refund - not a
    separate code path, just the same subtraction landing the other
    way. Exact at any bracket boundary `extra` happens to cross, unlike
    a flat rate applied to `extra` in isolation."""
    other_income = other_income or 0.0
    extra = extra or 0.0
    return income_tax(other_income + extra) - income_tax(other_income)


def property_year_tax_legs(rent, interest, holding, capital_gain, other_income):
    """One year's after-tax property lines, sequentially stacked (B5):
    rent (income) -> interest (deduction) -> holding costs (deduction)
    -> capital gain (only in the sale year - `capital_gain` is None for
    every other year). Each leg is its OWN tax_on_extra() call against
    the RUNNING cumulative income left by the legs before it, in this
    fixed order - rent first, deductions next, capital gain stacked
    LAST on top of the net rental position, exactly matching B5's own
    "extra = net_rental_position + discounted_capital_gain" (net
    rental position settled first, gain stacked on top).

    This is a TELESCOPING SUM: summing the four legs' own after-tax
    deltas always reproduces the exact combined bracket-correct total
    for ANY fixed order chosen - income_tax(x1)-income_tax(x0) +
    income_tax(x2)-income_tax(x1) + ... collapses to income_tax(xN)-
    income_tax(x0) regardless of where the intermediate steps fall.
    The order only decides which LINE "gets" a bracket boundary a
    stacked amount happens to cross - a display-allocation convention,
    never a source of error in the total (verified in this commit's
    own test suite against B5's combined "net first, tax once"
    formula, both for a recurring year and a stacked sale year).

    A capital LOSS (capital_gain < 0) is the one exception to the
    stacking chain, per B5's explicit instruction: it does not offset
    salary or the rental position, so it never joins the running
    income base and carries zero tax effect of its own - added to the
    total at its full (undiscounted) value, "$0 tax rather than a
    refund".

    Returns {"rent_after_tax", "interest_cost_after_tax" (a POSITIVE
    after-tax COST, to be SUBTRACTED - the property_interest_after_
    tax() convention this replaces), "holding_cost_after_tax" (same),
    "capital_gain_after_tax" (0.0 when capital_gain is None), "tax"
    (total, all four legs), "after_tax_total"} - after_tax_total always
    equals rent_after_tax - interest_cost_after_tax -
    holding_cost_after_tax + capital_gain_after_tax exactly."""
    other_income = other_income or 0.0
    rent = rent or 0.0
    interest = interest or 0.0
    holding = holding or 0.0
    running = other_income

    rent_tax = tax_on_extra(running, rent)
    running += rent
    rent_after_tax = rent - rent_tax

    interest_tax = tax_on_extra(running, -interest)
    running += -interest
    interest_cost_after_tax = interest + interest_tax

    holding_tax = tax_on_extra(running, -holding)
    running += -holding
    holding_cost_after_tax = holding + holding_tax

    capital_gain_after_tax = 0.0
    gain_tax = 0.0
    if capital_gain is not None:
        if capital_gain > 0:
            discounted = capital_gain * 0.5
            gain_tax = tax_on_extra(running, discounted)
            capital_gain_after_tax = capital_gain - gain_tax
        else:
            capital_gain_after_tax = capital_gain

    total_tax = rent_tax + interest_tax + holding_tax + gain_tax
    after_tax_total = (
        rent_after_tax - interest_cost_after_tax - holding_cost_after_tax
        + capital_gain_after_tax
    )
    return {
        "rent_after_tax": rent_after_tax,
        "interest_cost_after_tax": interest_cost_after_tax,
        "holding_cost_after_tax": holding_cost_after_tax,
        "capital_gain_after_tax": capital_gain_after_tax,
        "tax": total_tax,
        "after_tax_total": after_tax_total,
    }


def index_year_tax_legs(dividend, capital_gain, other_income):
    """Index-side equivalent of property_year_tax_legs() above: dividend
    (income) -> capital gain (only in the sale year, stacked last).
    Same telescoping-sum and loss-exclusion rules - see that function's
    own docstring. Returns {"dividend_after_tax",
    "capital_gain_after_tax", "tax", "after_tax_total"}."""
    other_income = other_income or 0.0
    dividend = dividend or 0.0
    running = other_income

    dividend_tax = tax_on_extra(running, dividend)
    running += dividend
    dividend_after_tax = dividend - dividend_tax

    capital_gain_after_tax = 0.0
    gain_tax = 0.0
    if capital_gain is not None:
        if capital_gain > 0:
            discounted = capital_gain * 0.5
            gain_tax = tax_on_extra(running, discounted)
            capital_gain_after_tax = capital_gain - gain_tax
        else:
            capital_gain_after_tax = capital_gain

    total_tax = dividend_tax + gain_tax
    after_tax_total = dividend_after_tax + capital_gain_after_tax
    return {
        "dividend_after_tax": dividend_after_tax,
        "capital_gain_after_tax": capital_gain_after_tax,
        "tax": total_tax,
        "after_tax_total": after_tax_total,
    }


# --------------------------------------------------------------------------- #
# Two-phase (interest-only, then principal-and-interest) loan schedule -
# see the module docstring's own "LOAN STRUCTURE" section.
# --------------------------------------------------------------------------- #

def property_loan_schedule(loan, loan_rate, io_period, term, years):
    """Year-by-year (simple annual convention, matching every other
    formula in this module) loan schedule: years 1..io_period are
    interest-only (balance constant, interest = start-of-year balance x
    rate); years io_period+1..term amortise at the fixed annual payment
    A = loan x [r(1+r)^n]/[(1+r)^n - 1], n = term - io_period (rate == 0
    falls back to straight-line principal, A = loan / n, to avoid a 0/0
    division in the compounding formula). `years` (how long the owner
    actually holds it) can be shorter OR longer than the loan's own
    io_period/term - the schedule only ever generates `years` entries and
    simply never reaches the P&I phase if years <= io_period.

    REGRESSION ANCHOR (io_period == years): every generated year has
    year <= io_period, so every year takes the "interest-only" branch
    below and the balance never moves - interest_per_year is `years`
    copies of loan x rate, i.e. exactly property_interest_per_year(loan,
    loan_rate) x years, the old pure-IO model's own total. A loan whose
    own design has no P&I phase at all (term <= io_period) behaves the
    same way for every year, forever - `annual_pi_payment` is None in
    that case since there is nothing to pay it with.

    Returns {"interest_per_year": [year 1, year 2, ... year `years`],
    "balance_after_year": [balance at the end of each of those years],
    "annual_pi_payment": the fixed A once/if the P&I phase starts within
    this loan's own design, else None}."""
    loan = loan or 0.0
    rate = loan_rate or 0.0
    io_period = max(0, int(io_period or 0))
    term = max(io_period, int(term or 0))
    years = max(0, int(years or 0))
    n = term - io_period

    annual_pi_payment = None
    if n > 0:
        if rate > 0:
            growth = (1 + rate) ** n
            annual_pi_payment = loan * (rate * growth) / (growth - 1)
        else:
            annual_pi_payment = loan / n

    balance = loan
    interest_per_year = []
    balance_after_year = []
    for year in range(1, years + 1):
        interest = balance * rate
        if year > io_period and annual_pi_payment is not None:
            principal = min(max(annual_pi_payment - interest, 0.0), balance)
            balance = max(balance - principal, 0.0)
        interest_per_year.append(interest)
        balance_after_year.append(balance)

    return {
        "interest_per_year": interest_per_year,
        "balance_after_year": balance_after_year,
        "annual_pi_payment": annual_pi_payment,
    }


def property_pi_phase_weekly_cash(loan, loan_rate, io_period, term, holding_costs, other_income):
    """The FIRST year of the P&I phase's own after-tax weekly cash
    figure, split into its interest (deductible) and principal (not
    deductible, but not a real "cost" either - it converts cash into
    equity) components - module docstring's "of which $Z/wk is
    principal" UI copy reads straight off this. The first P&I year's
    interest is still exactly loan x rate (the whole IO phase leaves the
    balance untouched), so no day-by-day schedule is needed here - one
    year is enough. None if this loan's own design has no P&I phase at
    all (term <= io_period).

    Commit B: interest/holding costs now stacked via property_year_tax_
    legs() (interest -> holding, no rent leg here - this function's own
    caller, after_tax_weekly_shortfall_pi_phase() below, adds rent to
    the SAME stack itself for its own final total; this function's
    "interest_after_tax"/"costs_after_tax_yearly" are only ever read in
    isolation, e.g. a future UI copy reading "of which $Z/wk is
    principal" straight off this dict)."""
    n = max(int(term or 0) - int(io_period or 0), 0)
    if n <= 0:
        return None
    schedule = property_loan_schedule(loan, loan_rate, io_period, term, int(io_period or 0) + 1)
    first_pi_interest = schedule["interest_per_year"][-1]
    annual_pi_payment = schedule["annual_pi_payment"]
    principal = max((annual_pi_payment or 0.0) - first_pi_interest, 0.0)
    legs = property_year_tax_legs(0.0, first_pi_interest, holding_costs, None, other_income)
    return {
        "annual_pi_payment": annual_pi_payment,
        "interest_pretax": first_pi_interest,
        "principal": principal,
        "interest_after_tax": legs["interest_cost_after_tax"],
        "costs_after_tax_yearly": legs["holding_cost_after_tax"],
    }


def after_tax_weekly_shortfall_pi_phase(loan, loan_rate, io_period, term,
                                        holding_costs, weekly_rent,
                                        vacancy_weeks, other_income):
    """Same shape/convention as after_tax_weekly_shortfall() below
    (module docstring, AFTER-TAX WEEKLY SHORTFALL) but for the FIRST
    year of the P&I phase: the payment's interest component is
    deductible as always; its principal component is NOT deductible and
    IS counted here as a real after-tax cash outflow, even though it
    also builds equity (property_pi_phase_weekly_cash()'s own
    docstring). Returns (shortfall_per_week, principal_per_week) - both
    None if this loan's own design has no P&I phase (term <= io_period).

    Commit B: rent/interest/holding for this one year are stacked
    together via property_year_tax_legs() (B5's "net first, tax once"
    rule, generalised to any single recurring year) - principal is
    added on afterward, unchanged, since it was never part of the tax
    stack to begin with (never deductible, never taxed)."""
    pi = property_pi_phase_weekly_cash(loan, loan_rate, io_period, term, holding_costs, other_income)
    if pi is None:
        return None, None
    working_weeks = max(WEEKS_PER_YEAR - (vacancy_weeks or 0), 0)
    rent_year = (weekly_rent or 0.0) * working_weeks
    legs = property_year_tax_legs(rent_year, pi["interest_pretax"], holding_costs, None, other_income)
    net_yearly = -legs["after_tax_total"] + pi["principal"]
    return net_yearly / WEEKS_PER_YEAR, pi["principal"] / WEEKS_PER_YEAR


# --------------------------------------------------------------------------- #
# Property side.
# --------------------------------------------------------------------------- #

def property_price(cash, loan):
    """price = cash + loan - the tool states plainly (UI copy, not here)
    that buy costs come OUT OF the cash, not on top of it."""
    return (cash or 0.0) + (loan or 0.0)


def property_future_value(price, growth_rate, years):
    return price * (1 + (growth_rate or 0.0)) ** years


def property_capital_growth(price, growth_rate, years, buy_costs, sell_costs_pct):
    """{"future_price", "sell_costs_dollar", "cost_base", "taxable_gain"}
    for the property sold at `years` - the ECONOMIC (pretax) gain/loss
    only. Commit B (20 Sep 2026): renamed from property_capital_growth_
    after_cgt and dropped the "after_tax" key it used to compute
    directly - the tax treatment now depends on stacking this gain
    against that year's own rental position and other_income (B5's
    "must be computed together, not separately"), which this function
    has no visibility into on its own. See property_year_tax_legs() and
    _property_mark_to_market() below for where "after_tax" is actually
    computed now. Nothing outside this module ever called the old name
    (grepped before this commit), so no back-compat alias is kept."""
    future_price = property_future_value(price, growth_rate, years)
    sell_costs_dollar = future_price * (sell_costs_pct or 0.0)
    proceeds = future_price - sell_costs_dollar
    cost_base = price + (buy_costs or 0.0)
    taxable_gain = proceeds - cost_base
    return {
        "future_price": future_price,
        "sell_costs_dollar": sell_costs_dollar,
        "cost_base": cost_base,
        "taxable_gain": taxable_gain,
    }


def property_interest_per_year(loan, loan_rate):
    return (loan or 0.0) * (loan_rate or 0.0)


def _property_mark_to_market(cash, loan, loan_rate, weekly_rent, vacancy_weeks,
                             holding_costs, growth_rate, years, buy_costs,
                             sell_costs_pct, other_income, io_period, term):
    """[{"year", "growth", "rent_after_tax", "interest_cost_after_tax",
    "holding_cost_after_tax", "capital_gain_after_tax", "tax",
    "cumulative_after_tax"} for year = 0..years] - Commit B's year-by-
    year, bracket-tax-stacked mark-to-market series (module docstring's
    "YEAR-BY-YEAR" section + B5/B6). Shared by property_breakdown()
    (which is just this series' own LAST point) and property_series()
    (which is just [point["cumulative_after_tax"] for point in this
    series]) - one implementation, so B6's "series[years] is exactly
    property_breakdown()['headline']" holds by construction, not by
    keeping two copies in sync by hand.

    At every point t, years 1..t-1 are taxed as pure recurring cash
    flow (rent/interest/holding, no capital gain - B5's "years 1..N-1"),
    and year t ITSELF is taxed as the sale year - its own recurring
    cash flow stacked together with the capital gain from selling at
    year t (B5's "Year N" rule) - exactly what mark-to-market means:
    "if sold at year t". `other_income` is the SAME every year (not
    cumulative across years - B5's "both sides are computed against
    the SAME other_income, independently"), so years 1..t-1's own
    recurring-only after-tax total is identical regardless of which t
    is being evaluated - computed once per year and carried forward as
    a running sum (O(years) total, not O(years^2))."""
    price = property_price(cash, loan)
    schedule = property_loan_schedule(loan, loan_rate, io_period, term, years)
    working_weeks = max(WEEKS_PER_YEAR - (vacancy_weeks or 0), 0)
    rent_per_year = (weekly_rent or 0.0) * working_weeks
    holding_per_year = holding_costs or 0.0

    out = []
    # Running sums over years 1..t-1's own recurring-only (no capital
    # gain) after-tax legs - NOT year t's own single-year figures. Every
    # point below reports the TOTAL over the whole hold up to and
    # including year t (property_breakdown() needs the full-hold total
    # rent/interest/holding, matching what property_rent_after_tax() et
    # al used to return before this commit), with year t's own
    # contribution using the sale-year-stacked treatment and every year
    # before it using the recurring-only treatment.
    cumulative_after_tax = 0.0
    cumulative_rent = 0.0
    cumulative_interest = 0.0
    cumulative_holding = 0.0
    for t in range(0, years + 1):
        growth = property_capital_growth(price, growth_rate, t, buy_costs, sell_costs_pct)
        if t == 0:
            rent_t, interest_t = 0.0, 0.0
        else:
            rent_t = rent_per_year
            interest_t = schedule["interest_per_year"][t - 1]
        legs = property_year_tax_legs(rent_t, interest_t, holding_per_year, growth["taxable_gain"], other_income)
        out.append({
            "year": t,
            "growth": growth,
            "rent_after_tax_total": cumulative_rent + legs["rent_after_tax"],
            "interest_cost_after_tax_total": cumulative_interest + legs["interest_cost_after_tax"],
            "holding_cost_after_tax_total": cumulative_holding + legs["holding_cost_after_tax"],
            "capital_gain_after_tax": legs["capital_gain_after_tax"],
            "tax": legs["tax"],
            "cumulative_after_tax": cumulative_after_tax + legs["after_tax_total"],
        })
        if t >= 1:
            recurring_only = property_year_tax_legs(rent_t, interest_t, holding_per_year, None, other_income)
            cumulative_after_tax += recurring_only["after_tax_total"]
            cumulative_rent += recurring_only["rent_after_tax"]
            cumulative_interest += recurring_only["interest_cost_after_tax"]
            cumulative_holding += recurring_only["holding_cost_after_tax"]
    return out


def property_breakdown(cash, loan, loan_rate, weekly_rent, vacancy_weeks,
                       holding_costs, growth_rate, years, buy_costs,
                       sell_costs_pct, other_income, io_period, term):
    """The property card's full breakdown at `years` - the four lines
    (module docstring) plus "headline". io_period/term describe the
    loan's own two-phase structure (module docstring, LOAN STRUCTURE).

    Commit B (20 Sep 2026): tax_rate replaced by other_income - every
    tax line is now bracket-computed and stacked year by year (B4/B5/
    B6), not one flat rate applied to lump-summed totals (wrong under a
    progressive schedule - see _property_mark_to_market()'s own
    docstring). This is just that series' own last point (t = years).
    "rent_after_tax"/"interest_after_tax"/"costs_after_tax" keep their
    OLD key names and OLD sign convention (interest/costs are POSITIVE
    after-tax COSTS, to be SUBTRACTED) even though they're computed
    completely differently now - app.py's own rendering (Commit C's
    job to regroup, per the task) reads them unchanged. Their SPLIT
    across the three recurring legs (and the growth/capital-gain leg)
    is a display-allocation convention - property_year_tax_legs()'s own
    docstring explains why the total is exact regardless, even though
    the individual split depends on the (documented, fixed) order
    chosen."""
    series = _property_mark_to_market(
        cash, loan, loan_rate, weekly_rent, vacancy_weeks, holding_costs,
        growth_rate, years, buy_costs, sell_costs_pct, other_income, io_period, term)
    point = series[-1]
    price = property_price(cash, loan)
    growth = dict(point["growth"])
    growth["after_tax"] = point["capital_gain_after_tax"]
    return {
        "price": price,
        "growth": growth,
        "rent_after_tax": point["rent_after_tax_total"],
        "interest_after_tax": point["interest_cost_after_tax_total"],
        "costs_after_tax": point["holding_cost_after_tax_total"],
        "headline": point["cumulative_after_tax"],
    }


# --------------------------------------------------------------------------- #
# Index (S&P 500) side.
# --------------------------------------------------------------------------- #

def index_price_growth_rate(capital_gain, dividend_yield=None):
    """Commit A (20 Sep 2026): capital_gain and dividend_yield are
    independent, additive inputs, not a total split into two parts -
    see the module docstring's own "INDEX (S&P 500) SIDE" section.
    dividend_yield is accepted and ignored - kept so index_breakdown()/
    index_series() below (and any other existing caller) don't need
    signature churn."""
    return capital_gain or 0.0


def index_pretax_gain(cash, price_growth_rate, years):
    """The economic (pretax) gain only - Commit B: tax treatment moved
    out (see _index_mark_to_market() below), same reasoning as property_
    capital_growth()'s own docstring."""
    future_value = (cash or 0.0) * (1 + (price_growth_rate or 0.0)) ** years
    return future_value - (cash or 0.0)


def _index_mark_to_market(cash, capital_gain, dividend_yield, years, other_income):
    """[{"year", "dividend_after_tax_total", "capital_gain_after_tax",
    "tax", "cumulative_after_tax"} for year = 0..years] - index side of
    _property_mark_to_market() above; same year-by-year, bracket-tax-
    stacked mark-to-market design (B5/B6), just two legs (dividend,
    capital gain) instead of four. See that function's own docstring
    for the shared reasoning - index_breakdown() is this series' own
    last point, index_series() is [point["cumulative_after_tax"], ...]."""
    price_growth_rate = index_price_growth_rate(capital_gain, dividend_yield)
    out = []
    cumulative_after_tax = 0.0
    cumulative_dividend = 0.0
    for t in range(0, years + 1):
        pretax_gain = index_pretax_gain(cash, price_growth_rate, t)
        if t == 0:
            dividend_t = 0.0
        else:
            # this YEAR's own dividend: start-of-year balance x yield -
            # matches index_dividends_after_tax()'s old geometric-sum
            # total, computed here one year at a time instead (t-1
            # years of compounding already behind this year's balance).
            dividend_t = (cash or 0.0) * (1 + price_growth_rate) ** (t - 1) * (dividend_yield or 0.0)
        legs = index_year_tax_legs(dividend_t, pretax_gain, other_income)
        out.append({
            "year": t,
            "price_growth_rate": price_growth_rate,
            "dividend_after_tax_total": cumulative_dividend + legs["dividend_after_tax"],
            "capital_gain_after_tax": legs["capital_gain_after_tax"],
            "tax": legs["tax"],
            "cumulative_after_tax": cumulative_after_tax + legs["after_tax_total"],
        })
        if t >= 1:
            recurring_only = index_year_tax_legs(dividend_t, None, other_income)
            cumulative_after_tax += recurring_only["after_tax_total"]
            cumulative_dividend += recurring_only["dividend_after_tax"]
    return out


def index_breakdown(cash, capital_gain, dividend_yield, years, other_income):
    """The S&P 500 card's full breakdown at `years` - the two lines
    (module docstring) plus "headline". Commit A: `capital_gain` and
    `dividend_yield` are independent, additive inputs. Commit B: tax_
    rate replaced by other_income - bracket-computed and stacked year
    by year (see _index_mark_to_market()'s own docstring), same
    reasoning as property_breakdown()."""
    series = _index_mark_to_market(cash, capital_gain, dividend_yield, years, other_income)
    point = series[-1]
    return {
        "price_growth_rate": point["price_growth_rate"],
        "growth_after_cgt": point["capital_gain_after_tax"],
        "dividends_after_tax": point["dividend_after_tax_total"],
        "headline": point["cumulative_after_tax"],
    }


# --------------------------------------------------------------------------- #
# Break-even rent + the after-tax weekly shortfall/surplus at the user's
# own entered rent.
# --------------------------------------------------------------------------- #

def break_even_weekly_rent(loan, loan_rate, holding_costs, vacancy_weeks):
    """(loan interest + holding costs, both PRE-tax) / (52 - vacancy) -
    module docstring, BREAK-EVEN RENT. None if there are no working
    weeks at all left to collect rent over (vacancy_weeks >= 52)."""
    working_weeks = WEEKS_PER_YEAR - (vacancy_weeks or 0)
    if working_weeks <= 0:
        return None
    yearly_cash_bill = property_interest_per_year(loan, loan_rate) + (holding_costs or 0.0)
    return yearly_cash_bill / working_weeks


def rent_coverage_pct(weekly_rent, break_even_rent):
    """What % of the break-even rent the user's own entered rent covers
    - 100% = exactly break-even, under 100% = costs money, over 100% =
    pays for itself. None if break_even_rent is None/zero."""
    if not break_even_rent:
        return None
    return (weekly_rent or 0.0) / break_even_rent * 100.0


def after_tax_weekly_shortfall(loan, loan_rate, holding_costs, weekly_rent,
                               vacancy_weeks, other_income):
    """Positive = costs you this much/wk after tax (below break-even);
    negative = pays for itself by this much/wk after tax (above it) -
    module docstring, AFTER-TAX WEEKLY SHORTFALL, for why the divisor is
    52 (a full calendar year) and not (52 - vacancy). Always represents
    an INTEREST-ONLY year's cash bill (one year of loan x rate) - this
    is the "IO phase" figure the two-phase caption pairs with
    after_tax_weekly_shortfall_pi_phase() above for the P&I phase; it
    intentionally does not take io_period/term so it keeps working
    unchanged for every old call site and every old-format saved
    scenario (io_period == years never reaches a P&I phase anyway).

    Commit B: one year's rent/interest/holding stacked together via
    property_year_tax_legs() (B5's "net first, tax once" rule), same
    reasoning as after_tax_weekly_shortfall_pi_phase() above."""
    working_weeks = max(WEEKS_PER_YEAR - (vacancy_weeks or 0), 0)
    rent_yearly = (weekly_rent or 0.0) * working_weeks
    interest_yearly = property_interest_per_year(loan, loan_rate)
    legs = property_year_tax_legs(rent_yearly, interest_yearly, holding_costs, None, other_income)
    return -legs["after_tax_total"] / WEEKS_PER_YEAR


# --------------------------------------------------------------------------- #
# Year-by-year (mark-to-market) series for the crossover chart.
# --------------------------------------------------------------------------- #

def property_series(cash, loan, loan_rate, weekly_rent, vacancy_weeks,
                    holding_costs, growth_rate, years, buy_costs,
                    sell_costs_pct, other_income, io_period, term):
    """[headline-if-sold-at-t for t = 0..years] - property, mark-to-
    market (module docstring, YEAR-BY-YEAR section). series[years] is
    exactly property_breakdown(...)["headline"] at the same inputs (B6:
    both are the same _property_mark_to_market() call, one reads every
    point's cumulative_after_tax, the other reads just the last one).
    Commit B: tax_rate replaced by other_income - each point t is
    bracket-computed and stacked using B5's "Year N" rule for t itself
    (years before t stay recurring-only) - see _property_mark_to_
    market()'s own docstring."""
    series = _property_mark_to_market(
        cash, loan, loan_rate, weekly_rent, vacancy_weeks, holding_costs,
        growth_rate, years, buy_costs, sell_costs_pct, other_income, io_period, term)
    return [point["cumulative_after_tax"] for point in series]


def index_series(cash, capital_gain, dividend_yield, years, other_income):
    """[headline-if-sold-at-t for t = 0..years] - S&P 500, mark-to-
    market. series[years] is exactly index_breakdown(...)["headline"]
    at the same inputs (B6 - see property_series()'s own docstring for
    the same reasoning, index side). Commit A: `capital_gain` and
    `dividend_yield` are independent, additive inputs. Commit B: tax_
    rate replaced by other_income - see _index_mark_to_market()'s own
    docstring."""
    series = _index_mark_to_market(cash, capital_gain, dividend_yield, years, other_income)
    return [point["cumulative_after_tax"] for point in series]


def find_crossover_year(property_series_vals, index_series_vals):
    """First year t (1..years) at which the leader at year 0 stops
    leading - i.e. the sign of (property - index) flips relative to year
    0. None if they never cross (one leads start-to-finish), or either
    series has fewer than 2 points. If the two are exactly tied at year
    0 (property_series_vals[0] == index_series_vals[0]), the first
    year with a genuine, non-zero gap sets the reference direction
    instead - that year alone is never reported as "the crossover"."""
    n = min(len(property_series_vals), len(index_series_vals))
    if n < 2:
        return None
    diffs = [property_series_vals[t] - index_series_vals[t] for t in range(n)]
    base = diffs[0]
    for t in range(1, n):
        if base > 0 and diffs[t] <= 0:
            return t
        if base < 0 and diffs[t] >= 0:
            return t
        if base == 0 and diffs[t] != 0:
            base = diffs[t]
    return None


# --------------------------------------------------------------------------- #
# Top-level convenience: everything the UI needs from one call.
# --------------------------------------------------------------------------- #

def run(cash=DEFAULT_CASH, loan=DEFAULT_LOAN, loan_rate=DEFAULT_LOAN_RATE,
       weekly_rent=DEFAULT_WEEKLY_RENT, vacancy_weeks=DEFAULT_VACANCY_WEEKS,
       holding_costs=DEFAULT_HOLDING_COSTS, property_growth=DEFAULT_PROPERTY_GROWTH,
       sp500_capital_gain=DEFAULT_SP500_CAPITAL_GAIN,
       sp500_dividend_yield=DEFAULT_SP500_DIVIDEND_YIELD,
       other_income=DEFAULT_OTHER_INCOME,
       years=DEFAULT_YEARS, buy_costs=DEFAULT_BUY_COSTS,
       sell_costs_pct=DEFAULT_SELL_COSTS_PCT,
       io_period=DEFAULT_IO_PERIOD, term=DEFAULT_LOAN_TERM):
    """One call, everything the UI needs: both cards' breakdowns, the
    break-even rent + coverage + shortfall, the year-by-year series for
    the crossover chart (+ its crossover year), and the loan's own
    two-phase (IO -> P&I) structure (module docstring, LOAN STRUCTURE).

    Commit B (20 Sep 2026): the flat marginal_rate + medicare_levy
    tax_rate scalar is GONE - replaced by `other_income` (your taxable
    income excluding this investment), threaded through to every tax
    line via tax_on_extra() (bracket tax, B4) and property_year_tax_
    legs()/index_year_tax_legs() (B5's per-year stacking - "must be
    computed together, not separately"). io_period/term are defensively
    clamped here too (never trust the caller): io_period <= years, term
    >= io_period - the same constraint the UI enforces on its own two
    inputs."""
    years = max(MIN_YEARS, min(int(years), MAX_YEARS))
    io_period = max(0, min(int(io_period if io_period is not None else years), years))
    term = max(io_period, int(term if term is not None else io_period))
    other_income = other_income or 0.0

    property_bd = property_breakdown(
        cash, loan, loan_rate, weekly_rent, vacancy_weeks, holding_costs,
        property_growth, years, buy_costs, sell_costs_pct, other_income,
        io_period, term)
    index_bd = index_breakdown(cash, sp500_capital_gain, sp500_dividend_yield, years, other_income)

    be_rent = break_even_weekly_rent(loan, loan_rate, holding_costs, vacancy_weeks)
    coverage_pct = rent_coverage_pct(weekly_rent, be_rent)
    weekly_shortfall = after_tax_weekly_shortfall(
        loan, loan_rate, holding_costs, weekly_rent, vacancy_weeks, other_income)
    pi_weekly_shortfall, pi_weekly_principal = after_tax_weekly_shortfall_pi_phase(
        loan, loan_rate, io_period, term, holding_costs, weekly_rent, vacancy_weeks, other_income)

    prop_series = property_series(
        cash, loan, loan_rate, weekly_rent, vacancy_weeks, holding_costs,
        property_growth, years, buy_costs, sell_costs_pct, other_income,
        io_period, term)
    idx_series = index_series(cash, sp500_capital_gain, sp500_dividend_yield, years, other_income)
    crossover_year = find_crossover_year(prop_series, idx_series)

    winner = "property" if property_bd["headline"] >= index_bd["headline"] else "index"

    n_design = max(term - io_period, 0)
    two_phase_active = years > io_period and n_design > 0
    loan_sched = property_loan_schedule(loan, loan_rate, io_period, term, years)
    total_interest_pretax = sum(loan_sched["interest_per_year"])
    balance_at_sale = loan_sched["balance_after_year"][-1] if loan_sched["balance_after_year"] else (loan or 0.0)
    principal_repaid = (loan or 0.0) - balance_at_sale

    return {
        "years": years,
        "other_income": other_income,
        "marginal_rate_pct": marginal_rate_at(other_income) * 100.0,
        "property": property_bd,
        "index": index_bd,
        "winner": winner,
        "break_even_weekly_rent": be_rent,
        "coverage_pct": coverage_pct,
        "after_tax_weekly_shortfall": weekly_shortfall,
        "property_series": prop_series,
        "index_series": idx_series,
        "crossover_year": crossover_year,
        "interest_per_year": property_interest_per_year(loan, loan_rate),
        # -- two-phase (IO -> P&I) loan structure - module docstring, LOAN STRUCTURE --
        "io_period": io_period,
        "term": term,
        "pi_phase_years_design": n_design,
        "two_phase_active": two_phase_active,
        "annual_pi_payment": loan_sched["annual_pi_payment"],
        "total_interest_pretax": total_interest_pretax,
        "balance_at_sale": balance_at_sale,
        "principal_repaid": principal_repaid,
        "pi_phase_weekly_shortfall": pi_weekly_shortfall,
        "pi_phase_weekly_principal": pi_weekly_principal,
    }
