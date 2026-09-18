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


def property_interest_after_tax(loan, loan_rate, years, tax_rate):
    pretax = property_interest_per_year(loan, loan_rate) * years
    return pretax * (1 - tax_rate)


def property_holding_costs_after_tax(holding_costs, years, tax_rate):
    pretax = (holding_costs or 0.0) * years
    return pretax * (1 - tax_rate)


def property_breakdown(cash, loan, loan_rate, weekly_rent, vacancy_weeks,
                       holding_costs, growth_rate, years, buy_costs,
                       sell_costs_pct, tax_rate):
    """The property card's full breakdown at `years` - the four lines
    (module docstring) plus "headline", their exact sum."""
    price = property_price(cash, loan)
    growth = property_capital_growth_after_cgt(
        price, growth_rate, years, buy_costs, sell_costs_pct, tax_rate)
    rent_after_tax = property_rent_after_tax(weekly_rent, vacancy_weeks, years, tax_rate)
    interest_after_tax = property_interest_after_tax(loan, loan_rate, years, tax_rate)
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
    52 (a full calendar year) and not (52 - vacancy)."""
    interest_after_tax_yearly = property_interest_after_tax(loan, loan_rate, 1, tax_rate)
    costs_after_tax_yearly = property_holding_costs_after_tax(holding_costs, 1, tax_rate)
    rent_after_tax_yearly = property_rent_after_tax(weekly_rent, vacancy_weeks, 1, tax_rate)
    net_yearly = interest_after_tax_yearly + costs_after_tax_yearly - rent_after_tax_yearly
    return net_yearly / WEEKS_PER_YEAR


# --------------------------------------------------------------------------- #
# Year-by-year (mark-to-market) series for the crossover chart.
# --------------------------------------------------------------------------- #

def property_series(cash, loan, loan_rate, weekly_rent, vacancy_weeks,
                    holding_costs, growth_rate, years, buy_costs,
                    sell_costs_pct, tax_rate):
    """[headline-if-sold-at-t for t = 0..years] - property, mark-to-
    market (module docstring, YEAR-BY-YEAR section). series[years] is
    exactly property_breakdown(...)["headline"] at the same inputs."""
    price = property_price(cash, loan)
    series = []
    for t in range(years + 1):
        growth = property_capital_growth_after_cgt(
            price, growth_rate, t, buy_costs, sell_costs_pct, tax_rate)
        rent = property_rent_after_tax(weekly_rent, vacancy_weeks, t, tax_rate)
        interest = property_interest_after_tax(loan, loan_rate, t, tax_rate)
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
       sell_costs_pct=DEFAULT_SELL_COSTS_PCT):
    """One call, everything the UI needs: both cards' breakdowns, the
    break-even rent + coverage + shortfall, and the year-by-year series
    for the crossover chart (+ its crossover year). tax_rate =
    marginal_rate + medicare_levy throughout - the Super tool's own "+2%
    Medicare shown separately, applied together" convention."""
    years = max(MIN_YEARS, min(int(years), MAX_YEARS))
    tax_rate = (marginal_rate or 0.0) + (medicare_levy or 0.0)

    property_bd = property_breakdown(
        cash, loan, loan_rate, weekly_rent, vacancy_weeks, holding_costs,
        property_growth, years, buy_costs, sell_costs_pct, tax_rate)
    index_bd = index_breakdown(cash, sp500_total_return, sp500_dividend_yield, years, tax_rate)

    be_rent = break_even_weekly_rent(loan, loan_rate, holding_costs, vacancy_weeks)
    coverage_pct = rent_coverage_pct(weekly_rent, be_rent)
    weekly_shortfall = after_tax_weekly_shortfall(
        loan, loan_rate, holding_costs, weekly_rent, vacancy_weeks, tax_rate)

    prop_series = property_series(
        cash, loan, loan_rate, weekly_rent, vacancy_weeks, holding_costs,
        property_growth, years, buy_costs, sell_costs_pct, tax_rate)
    idx_series = index_series(cash, sp500_total_return, sp500_dividend_yield, years, tax_rate)
    crossover_year = find_crossover_year(prop_series, idx_series)

    winner = "property" if property_bd["headline"] >= index_bd["headline"] else "index"

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
    }
