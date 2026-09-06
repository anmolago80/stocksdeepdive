"""
budget_planner_engine.py

Mega-batch Part 18: pure-logic engine behind the "Budget Planner" - the
first tool in the free 🧰 Tools hub. Type a family budget in a minute, see
savings $/mo and $/yr, then what those savings would have become invested
in a plain index fund - plus what each expense category "costs" in
forgone compounding. Every function here is a plain, deterministic
calculation over numbers the caller supplies - no network, no Streamlit,
no file I/O - so it is trivially unit testable and safe to import from
anywhere (including a future tool that wants the same projection maths -
see the shared PROJECTION panel note below).

THE TEN PRESET CATEGORIES (spec's own set/order, icon + id): a visitor
skips whichever don't apply - every value defaults to blank/None, never 0
forced, so an unfilled category is excluded from money_out rather than
silently counted as "$0 spent here".

THE PROJECTION FORMULA (reproducible by construction - the spec gives one
worked example to verify against): ANNUAL compounding of the YEARLY
savings figure,
    FV = yearly_savings * ((1 + r) ** N - 1) / r
i.e. the whole year's savings is treated as one deposit at each year's
mark, compounded at the nominal annual rate r for the remaining years -
not monthly compounding of a monthly contribution. Worked example this
module is verified against (mega-batch Part 18, section A):
    $9,000 in, $6,800 out -> $2,200/mo, $26,400/yr
    10y @ 10% -> FV = 26400 * ((1.10**10 - 1) / 0.10) = 420,748 (spec: "≈$420,700")
    deposits alone (10 * 26400)                        = 264,000 (spec: "$264,000")
    10y @ 6%  -> FV = 26400 * ((1.06**10 - 1) / 0.06)  = 347,973 (spec: "≈$348,000")
All three match the spec's own approximations to within its own rounding.

THE PER-CATEGORY COMPOUNDING FIGURE ("10y invested ≈ $X" beside each
filled category) reuses this SAME formula rather than a second one -
"the same arithmetic every time" is this site's whole ethos - treating
the category's monthly amount x12 as its own "yearly savings" figure
run through the identical future_value_of_savings() below. The spec
gives no separate worked example for this figure, so this is a
deliberate, documented choice, not a guess dressed as the spec's own
number.

HISTORICAL RATES: one constants block, sourced, exactly as the spec
requires ("put both indices' long-run average total returns... in ONE
constants block with a source comment"). These are the same kind of
long-run nominal total-return averages (dividends/distributions
reinvested, before tax and fund fees) commonly cited by index providers
and long-run return studies - NOT a forecast, and labelled as history
everywhere the UI shows them (never "expected return").

Never touches scoring engines, never suggests cutting an expense or
buying a product, never names a specific ETF/fund ticker."""

# --------------------------------------------------------------------------- #
# The ten preset monthly categories - spec's own set, in spec's own order.
# id is the persistence key (tools_store); icon+label render on the page
# via i18n (label text lives in i18n.py, never hardcoded English here).
# --------------------------------------------------------------------------- #
CATEGORIES = [
    {"id": "mortgage", "icon": "\U0001F3E0"},   # 🏠 Mortgage / rent
    {"id": "transport", "icon": "\U0001F697"},  # 🚗 Transport (car, petrol, fares)
    {"id": "food", "icon": "\U0001F6D2"},       # 🛒 Food & groceries
    {"id": "utilities", "icon": "\U0001F4A1"},  # 💡 Utilities (power, water, internet, phone)
    {"id": "insurance", "icon": "\U0001F6E1"},  # 🛡️ Insurances
    {"id": "health", "icon": "⚕"},         # ⚕️ Health
    {"id": "education", "icon": "\U0001F393"},  # 🎓 School / childcare
    {"id": "subscriptions", "icon": "\U0001F4FA"},  # 📺 Subscriptions
    {"id": "fun", "icon": "\U0001F377"},        # 🍷 Fun, eating out, hobbies
    {"id": "other", "icon": "❔"},          # ❔ Everything else
]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]

# Source: long-run NOMINAL total-return averages (dividends/distributions
# reinvested), before tax and fund fees - the S&P 500 figure is the
# commonly-cited ~10%/yr nominal average since 1926 (Ibbotson/S&P Dow
# Jones Indices annual return series; Damodaran NYU historical returns
# data uses the same order of magnitude); the ASX 200 figure is the
# commonly-cited ~9%/yr long-run nominal accumulation-index average
# (S&P/ASX 200 Accumulation Index and predecessor All Ordinaries
# Accumulation Index long-run studies, e.g. Vanguard/ASX index research).
# Both are HISTORY, not a prediction - every page that shows either
# constant must label it as a historical average, per the spec.
INDEX_HISTORICAL_RETURNS = {
    "sp500": 0.10,
    "asx200": 0.09,
}
CAUTIOUS_RATE = 0.06  # spec's own fixed "cautious" comparison rate.

MIN_PROJECTION_YEARS = 5
MAX_PROJECTION_YEARS = 40
DEFAULT_PROJECTION_YEARS = 10


def future_value_of_savings(yearly_savings, annual_rate, years):
    """FV = yearly_savings * ((1 + r) ** N - 1) / r - annual compounding of
    the yearly savings figure (see module docstring for the worked
    example this reproduces exactly). r == 0 falls back to plain
    deposits (no growth) rather than dividing by zero; any missing/
    non-positive input returns None/0 rather than guessing."""
    if yearly_savings is None or annual_rate is None or years is None:
        return None
    if years <= 0:
        return 0.0
    if yearly_savings <= 0:
        return 0.0
    if annual_rate == 0:
        return yearly_savings * years
    return yearly_savings * (((1 + annual_rate) ** years - 1) / annual_rate)


def deposits_only(yearly_savings, years):
    """The "just saved, never invested" baseline - the same yearly figure
    added up with zero growth, for the three-figure comparison and the
    two-line chart's second series."""
    if yearly_savings is None or years is None or years <= 0:
        return 0.0
    return max(yearly_savings, 0.0) * years


def projection_series(yearly_savings, annual_rate, years):
    """Year-by-year (invested, deposits-only) pair at every whole year
    from 0..years inclusive, for the "invested vs just-saved" two-line
    chart - each point independently re-derived from
    future_value_of_savings()/deposits_only() at that year count, so the
    series is always consistent with the three headline figures computed
    at the full horizon."""
    if yearly_savings is None or annual_rate is None or years is None:
        return [], [], []
    years = max(int(years), 0)
    xs = list(range(years + 1))
    invested = [future_value_of_savings(yearly_savings, annual_rate, y) for y in xs]
    saved = [deposits_only(yearly_savings, y) for y in xs]
    return xs, invested, saved


def savings_summary(money_in, category_values):
    """money_in: float or None. category_values: {category_id: float or
    None}. Returns {money_in, money_out, savings_month, savings_year,
    savings_rate} - savings_rate is None whenever money_in is missing or
    zero (nothing to take a rate of), never a divide-by-zero guess."""
    money_out = sum(v for v in category_values.values() if v is not None and v > 0)
    if money_in is None:
        return {
            "money_in": None, "money_out": money_out,
            "savings_month": None, "savings_year": None, "savings_rate": None,
        }
    savings_month = money_in - money_out
    savings_year = savings_month * 12
    savings_rate = (savings_month / money_in) if money_in > 0 else None
    return {
        "money_in": money_in, "money_out": money_out,
        "savings_month": savings_month, "savings_year": savings_year,
        "savings_rate": savings_rate,
    }


def savings_rate_descriptor(savings_rate):
    """Purely factual band descriptor - no praise/judgment words, no
    "should" (spec's own constraint). None input (nothing to describe
    yet, e.g. no income entered) -> None."""
    if savings_rate is None:
        return None
    pct = round(savings_rate * 100)
    if savings_rate >= 0:
        return f"saving {pct}% of income"
    return f"spending {abs(pct)}% more than income coming in"


def category_compounding(monthly_amount, annual_rate, years):
    """The small amber "Ny invested ≈ $X" figure beside one filled
    category - see module docstring for why this reuses
    future_value_of_savings() on monthly_amount*12 rather than a second
    formula. None for an unfilled (None/zero/negative) category - no
    figure is shown for a category the visitor skipped."""
    if monthly_amount is None or monthly_amount <= 0:
        return None
    return future_value_of_savings(monthly_amount * 12, annual_rate, years)


def clamp_years(years):
    """Keeps the spinner's value inside the spec's 5-40 range regardless
    of what a caller (a stale saved plan, a malformed query param) hands
    in - never lets an out-of-range horizon silently reach the maths."""
    if years is None:
        return DEFAULT_PROJECTION_YEARS
    return max(MIN_PROJECTION_YEARS, min(int(years), MAX_PROJECTION_YEARS))
