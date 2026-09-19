"""
property_vs_index_engine.py

Money Tools: "\U0001F3E0 Property vs \U0001F4C8 S&P 500" - pure-logic engine behind the
newest Money Tool. Mirrors debt_recycling_engine.py's own modelling
conventions exactly (see that module's docstring for the shared DNA):
simple (non-compounding) interest-only loan, after-tax income legs paid
out annually rather than reinvested, growth compounded and taxed AT
DISPOSAL with the 50% CGT discount at the user's marginal rate. Every
function here is deterministic arithmetic over numbers the caller
supplies - no network, no Streamlit, no file I/O - same contract as
debt_recycling_engine.py/budget_planner_engine.py alongside it.
Deliberately self-contained (does NOT import debt_recycling_engine.py)
so this tool's own engine never depends on - or risks being disturbed
by - a change to a protected scoring/analysis-adjacent module; the
after_tax_capital_gain() helper below reimplements the identical
50%-discount formula for that reason. debt_recycling_engine.py itself
is never touched by this tool - verified by diff.

THE QUESTION THIS ANSWERS: "I have `cash` and could borrow `loan` -
should I buy a `cash + loan` investment property, or just put `cash`
into the S&P 500?" Both sides are run over the SAME time horizon and
taxed at the SAME user-supplied marginal rate (+ Medicare levy, shown as
its own addend exactly like the Super tool's own "+2% Medicare" line),
so the two headline numbers are always an apples-to-apples after-tax
dollar comparison. AU-only (no US branch) - the task spec's own inputs
(stamp duty, Medicare levy, negative gearing) are all AU-specific.

PROPERTY SIDE - four lines, always summing exactly to the headline (the
Debt Recycling card's own "the numbers must add up on screen" rule):
  1. Capital growth after CGT - the property is assumed sold at `years`.
     cost base = price + buy_costs (stamp duty + legals, paid OUT OF THE
     CASH at purchase - never financed, and never affects the loan/price
     split); proceeds = future_price minus sell_costs (a % of the SALE
     price, not the purchase price); the 50% CGT discount applies to
     (proceeds - cost_base) at the marginal rate (see
     after_tax_capital_gain() below) - a loss (true for small enough
     `years`, since buy+sell costs alone are a real cost the first day)
     passes through UNDISCOUNTED, the same convention debt_recycling_
     engine.after_tax_capital_gain_au() uses for a loss.
  2. Rent after tax - weekly rent x (52 - vacancy weeks) x years, taxed
     ONCE at the full marginal rate (no CGT discount - rent is ordinary
     income, paid out each year, never reinvested, so it accumulates
     LINEARLY - same non-reinvestment convention debt_recycling_
     engine's module docstring documents for every income leg there).
  3. Loan interest after deduction - negative gearing: the FULL interest
     bill is deductible at the marginal rate (no cap - matches debt_
     recycling_engine's AU convention), simple interest-only,
     non-compounding.
  4. Holding costs after deduction - same deductible-at-marginal
     treatment as the loan interest line, for the property's other
     yearly running costs (maintenance/rates/insurance/management,
     entered as one number).

INDEX (S&P 500) SIDE - two lines summing to the headline:
  1. Growth after CGT - the S&P 500's TOTAL return is split into a
     price-growth component (total - dividend yield) the balance
     compounds at, and the dividend yield (below); `cash` compounds at
     the price-growth rate alone and is taxed at disposal with the same
     50% CGT discount as the property side (no buy/sell costs on this
     side - a lump-sum index purchase's brokerage is not modelled, per
     the task spec's own inputs, which lists buy/sell costs only for the
     property).
  2. Dividends after tax - EACH year's dividend is that year's OWN
     start-of-year balance x dividend yield (a genuinely compounding
     dividend STREAM, since the balance is growing at the price-growth
     rate even though dividend cash itself is paid out, not reinvested)
     - taxed ONCE at the full marginal rate (ordinary income, no
     franking modelled - the S&P 500 is a US index, DR's own AU-only
     franking treatment doesn't apply here) and paid out annually, the
     same non-reinvestment convention as the property's rent line. The
     sum of `years` distinct dividend payments is a finite geometric
     series - see _geometric_growth_sum() below.

BOTH SIDES share the exact same after_tax_capital_gain() 50%-discount
function and the exact same "linear, non-reinvested, taxed once at
marginal" income treatment - deliberately, so neither side gets an
uneven advantage baked into the model itself; only the INPUTS (leverage,
rent, growth assumptions) drive the comparison.

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
DEFAULT_SP500_TOTAL_RETURN = 0.09
DEFAULT_SP500_DIVIDEND_YIELD = 0.013
DEFAULT_MARGINAL_RATE = 0.37
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
# Shared after-tax primitives (mirrors debt_recycling_engine.py exactly -
# see module docstring for why these are reimplemented here rather than
# imported).
# --------------------------------------------------------------------------- #

def after_tax_capital_gain(gain, tax_rate):
    """The 50% CGT discount at the marginal rate - identical convention
    to debt_recycling_engine.after_tax_capital_gain_au. A loss
    (gain <= 0) passes through undiscounted; None-safe."""
    if gain is None:
        return 0.0
    if gain <= 0:
        return gain
    taxable = gain * 0.5
    return gain - taxable * tax_rate


def _geometric_growth_sum(rate, n):
    """sum_{k=0}^{n-1} (1 + rate)^k - the number of TERMS a start-of-year
    dividend stream sums to over `n` years. n <= 0 -> 0.0 (no terms).
    rate == 0 is handled separately (an n-term sum of 1's) to avoid a
    0/0 division in the closed form."""
    if n <= 0:
        return 0.0
    if rate == 0:
        return float(n)
    return ((1 + rate) ** n - 1) / rate


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


def property_pi_phase_weekly_cash(loan, loan_rate, io_period, term, holding_costs, tax_rate):
    """The FIRST year of the P&I phase's own after-tax weekly cash
    figure, split into its interest (deductible) and principal (not
    deductible, but not a real "cost" either - it converts cash into
    equity) components - module docstring's "of which $Z/wk is
    principal" UI copy reads straight off this. The first P&I year's
    interest is still exactly loan x rate (the whole IO phase leaves the
    balance untouched), so no day-by-day schedule is needed here - one
    year is enough. None if this loan's own design has no P&I phase at
    all (term <= io_period)."""
    n = max(int(term or 0) - int(io_period or 0), 0)
    if n <= 0:
        return None
    schedule = property_loan_schedule(loan, loan_rate, io_period, term, int(io_period or 0) + 1)
    first_pi_interest = schedule["interest_per_year"][-1]
    annual_pi_payment = schedule["annual_pi_payment"]
    principal = max((annual_pi_payment or 0.0) - first_pi_interest, 0.0)
    return {
        "annual_pi_payment": annual_pi_payment,
        "interest_pretax": first_pi_interest,
        "principal": principal,
        "interest_after_tax": first_pi_interest * (1 - tax_rate),
        "costs_after_tax_yearly": property_holding_costs_after_tax(holding_costs, 1, tax_rate),
    }


def after_tax_weekly_shortfall_pi_phase(loan, loan_rate, io_period, term,
                                        holding_costs, weekly_rent,
                                        vacancy_weeks, tax_rate):
    """Same shape/convention as after_tax_weekly_shortfall() below
    (module docstring, AFTER-TAX WEEKLY SHORTFALL) but for the FIRST
    year of the P&I phase: the payment's interest component is
    deductible as always; its principal component is NOT deductible and
    IS counted here as a real after-tax cash outflow, even though it
    also builds equity (property_pi_phase_weekly_cash()'s own
    docstring). Returns (shortfall_per_week, principal_per_week) - both
    None if this loan's own design has no P&I phase (term <= io_period)."""
    pi = property_pi_phase_weekly_cash(loan, loan_rate, io_period, term, holding_costs, tax_rate)
    if pi is None:
        return None, None
    rent_after_tax_yearly = property_rent_after_tax(weekly_rent, vacancy_weeks, 1, tax_rate)
    net_yearly = pi["interest_after_tax"] + pi["principal"] + pi["costs_after_tax_yearly"] - rent_after_tax_yearly
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


def property_capital_growth_after_cgt(price, growth_rate, years, buy_costs,
                                      sell_costs_pct, tax_rate):
    """{"future_price", "sell_costs_dollar", "cost_base", "taxable_gain",
    "after_tax"} for the property sold at `years` - module docstring,
    PROPERTY SIDE line 1."""
    future_price = property_future_value(price, growth_rate, years)
    sell_costs_dollar = future_price * (sell_costs_pct or 0.0)
    proceeds = future_price - sell_costs_dollar
    cost_base = price + (buy_costs or 0.0)
    taxable_gain = proceeds - cost_base
    after_tax = after_tax_capital_gain(taxable_gain, tax_rate)
    return {
        "future_price": future_price,
        "sell_costs_dollar": sell_costs_dollar,
        "cost_base": cost_base,
        "taxable_gain": taxable_gain,
        "after_tax": after_tax,
    }


def property_rent_after_tax(weekly_rent, vacancy_weeks, years, tax_rate):
    working_weeks = max(WEEKS_PER_YEAR - (vacancy_weeks or 0), 0)
    pretax = (weekly_rent or 0.0) * working_weeks * years
    return pretax * (1 - tax_rate)


def property_interest_per_year(loan, loan_rate):
    return (loan or 0.0) * (loan_rate or 0.0)


def property_interest_after_tax(loan, loan_rate, io_period, term, years, tax_rate):
    """Total interest after tax over `years` (mark-to-market: "if sold at
    year `years`") under the two-phase IO-then-P&I schedule
    (property_loan_schedule() above) - replaces the old pure-IO formula
    (loan x rate x years x (1 - tax)); io_period == years reproduces it
    exactly, to the cent (property_loan_schedule()'s own docstring)."""
    schedule = property_loan_schedule(loan, loan_rate, io_period, term, years)
    pretax = sum(schedule["interest_per_year"])
    return pretax * (1 - tax_rate)


def property_holding_costs_after_tax(holding_costs, years, tax_rate):
    pretax = (holding_costs or 0.0) * years
    return pretax * (1 - tax_rate)


def property_breakdown(cash, loan, loan_rate, weekly_rent, vacancy_weeks,
                       holding_costs, growth_rate, years, buy_costs,
                       sell_costs_pct, tax_rate, io_period, term):
    """The property card's full breakdown at `years` - the four lines
    (module docstring) plus "headline", their exact sum. io_period/term
    describe the loan's own two-phase structure (module docstring, LOAN
    STRUCTURE) - io_period == years reproduces the old pure-IO numbers."""
    price = property_price(cash, loan)
    growth = property_capital_growth_after_cgt(
        price, growth_rate, years, buy_costs, sell_costs_pct, tax_rate)
    rent_after_tax = property_rent_after_tax(weekly_rent, vacancy_weeks, years, tax_rate)
    interest_after_tax = property_interest_after_tax(loan, loan_rate, io_period, term, years, tax_rate)
    costs_after_tax = property_holding_costs_after_tax(holding_costs, years, tax_rate)
    headline = growth["after_tax"] + rent_after_tax - interest_after_tax - costs_after_tax
    return {
        "price": price,
        "growth": growth,
        "rent_after_tax": rent_after_tax,
        "interest_after_tax": interest_after_tax,
        "costs_after_tax": costs_after_tax,
        "headline": headline,
    }


# --------------------------------------------------------------------------- #
# Index (S&P 500) side.
# --------------------------------------------------------------------------- #

def index_price_growth_rate(total_return, dividend_yield):
    return (total_return or 0.0) - (dividend_yield or 0.0)


def index_growth_after_cgt(cash, price_growth_rate, years, tax_rate):
    future_value = (cash or 0.0) * (1 + (price_growth_rate or 0.0)) ** years
    pretax_gain = future_value - (cash or 0.0)
    return after_tax_capital_gain(pretax_gain, tax_rate)


def index_dividends_after_tax(cash, price_growth_rate, dividend_yield, years, tax_rate):
    geometric_sum = _geometric_growth_sum(price_growth_rate or 0.0, years)
    pretax = (cash or 0.0) * (dividend_yield or 0.0) * geometric_sum
    return pretax * (1 - tax_rate)


def index_breakdown(cash, total_return, dividend_yield, years, tax_rate):
    """The S&P 500 card's full breakdown at `years` - the two lines
    (module docstring) plus "headline", their exact sum."""
    price_growth_rate = index_price_growth_rate(total_return, dividend_yield)
    growth_after_cgt = index_growth_after_cgt(cash, price_growth_rate, years, tax_rate)
    dividends_after_tax = index_dividends_after_tax(
        cash, price_growth_rate, dividend_yield, years, tax_rate)
    headline = growth_after_cgt + dividends_after_tax
    return {
        "price_growth_rate": price_growth_rate,
        "growth_after_cgt": growth_after_cgt,
        "dividends_after_tax": dividends_after_tax,
        "headline": headline,
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
                               vacancy_weeks, tax_rate):
    """Positive = costs you this much/wk after tax (below break-even);
    negative = pays for itself by this much/wk after tax (above it) -
    module docstring, AFTER-TAX WEEKLY SHORTFALL, for why the divisor is
    52 (a full calendar year) and not (52 - vacancy). Always represents
    an INTEREST-ONLY year's cash bill (one year of loan x rate) - this
    is the "IO phase" figure the two-phase caption pairs with
    after_tax_weekly_shortfall_pi_phase() above for the P&I phase; it
    intentionally does not take io_period/term so it keeps working
    unchanged for every old call site and every old-format saved
    scenario (io_period == years never reaches a P&I phase anyway)."""
    interest_after_tax_yearly = property_interest_per_year(loan, loan_rate) * (1 - tax_rate)
    costs_after_tax_yearly = property_holding_costs_after_tax(holding_costs, 1, tax_rate)
    rent_after_tax_yearly = property_rent_after_tax(weekly_rent, vacancy_weeks, 1, tax_rate)
    net_yearly = interest_after_tax_yearly + costs_after_tax_yearly - rent_after_tax_yearly
    return net_yearly / WEEKS_PER_YEAR


# --------------------------------------------------------------------------- #
# Year-by-year (mark-to-market) series for the crossover chart.
# --------------------------------------------------------------------------- #

def property_series(cash, loan, loan_rate, weekly_rent, vacancy_weeks,
                    holding_costs, growth_rate, years, buy_costs,
                    sell_costs_pct, tax_rate, io_period, term):
    """[headline-if-sold-at-t for t = 0..years] - property, mark-to-
    market (module docstring, YEAR-BY-YEAR section). series[years] is
    exactly property_breakdown(...)["headline"] at the same inputs.
    Interest at each t is CUMULATIVE interest to year t under the same
    fixed io_period/term loan schedule (property_loan_schedule() above),
    not a flat per-year rate multiplied by t - the crossover chart's own
    "cumulative interest per year" update (task, 19 Sep 2026)."""
    price = property_price(cash, loan)
    series = []
    for t in range(years + 1):
        growth = property_capital_growth_after_cgt(
            price, growth_rate, t, buy_costs, sell_costs_pct, tax_rate)
        rent = property_rent_after_tax(weekly_rent, vacancy_weeks, t, tax_rate)
        interest = property_interest_after_tax(loan, loan_rate, io_period, term, t, tax_rate)
        costs = property_holding_costs_after_tax(holding_costs, t, tax_rate)
        series.append(growth["after_tax"] + rent - interest - costs)
    return series


def index_series(cash, total_return, dividend_yield, years, tax_rate):
    """[headline-if-sold-at-t for t = 0..years] - S&P 500, mark-to-
    market. series[years] is exactly index_breakdown(...)["headline"]
    at the same inputs."""
    price_growth_rate = index_price_growth_rate(total_return, dividend_yield)
    series = []
    for t in range(years + 1):
        growth = index_growth_after_cgt(cash, price_growth_rate, t, tax_rate)
        dividends = index_dividends_after_tax(cash, price_growth_rate, dividend_yield, t, tax_rate)
        series.append(growth + dividends)
    return series


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
       sp500_total_return=DEFAULT_SP500_TOTAL_RETURN,
       sp500_dividend_yield=DEFAULT_SP500_DIVIDEND_YIELD,
       marginal_rate=DEFAULT_MARGINAL_RATE, medicare_levy=MEDICARE_LEVY,
       years=DEFAULT_YEARS, buy_costs=DEFAULT_BUY_COSTS,
       sell_costs_pct=DEFAULT_SELL_COSTS_PCT,
       io_period=DEFAULT_IO_PERIOD, term=DEFAULT_LOAN_TERM):
    """One call, everything the UI needs: both cards' breakdowns, the
    break-even rent + coverage + shortfall, the year-by-year series for
    the crossover chart (+ its crossover year), and the loan's own
    two-phase (IO -> P&I) structure (module docstring, LOAN STRUCTURE).
    tax_rate = marginal_rate + medicare_levy throughout - the Super
    tool's own "+2% Medicare shown separately, applied together"
    convention. io_period/term are defensively clamped here too (never
    trust the caller): io_period <= years, term >= io_period - the same
    constraint the UI enforces on its own two new inputs."""
    years = max(MIN_YEARS, min(int(years), MAX_YEARS))
    io_period = max(0, min(int(io_period if io_period is not None else years), years))
    term = max(io_period, int(term if term is not None else io_period))
    tax_rate = (marginal_rate or 0.0) + (medicare_levy or 0.0)

    property_bd = property_breakdown(
        cash, loan, loan_rate, weekly_rent, vacancy_weeks, holding_costs,
        property_growth, years, buy_costs, sell_costs_pct, tax_rate,
        io_period, term)
    index_bd = index_breakdown(cash, sp500_total_return, sp500_dividend_yield, years, tax_rate)

    be_rent = break_even_weekly_rent(loan, loan_rate, holding_costs, vacancy_weeks)
    coverage_pct = rent_coverage_pct(weekly_rent, be_rent)
    weekly_shortfall = after_tax_weekly_shortfall(
        loan, loan_rate, holding_costs, weekly_rent, vacancy_weeks, tax_rate)
    pi_weekly_shortfall, pi_weekly_principal = after_tax_weekly_shortfall_pi_phase(
        loan, loan_rate, io_period, term, holding_costs, weekly_rent, vacancy_weeks, tax_rate)

    prop_series = property_series(
        cash, loan, loan_rate, weekly_rent, vacancy_weeks, holding_costs,
        property_growth, years, buy_costs, sell_costs_pct, tax_rate,
        io_period, term)
    idx_series = index_series(cash, sp500_total_return, sp500_dividend_yield, years, tax_rate)
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
        "tax_rate": tax_rate,
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
