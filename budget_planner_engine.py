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

instruction_budget_ledger.md (9 Oct 2026) added is_ledger_live() below -
a one env-var read, same minimal-dependency spirit as the rest of this
module (it's a switch check, not network/file I/O).

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

import os

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


def is_ledger_live():
    """The Budget Ledger's own switch (instruction_budget_ledger.md, 9
    Oct 2026, Director-directed) - same unset/""/0/off=OFF, 1/on=ON
    pattern as scanner_engine.is_scanner_presentation_live()/currency_
    view_engine.is_currency_view_live(). Unset = only the owner sees
    the Ledger/Insights tabs; "1" = every signed-in user does. Andrew
    sets the Railway variable himself later - Claude Code never does.
    Re-read on every call (cheap - one env var lookup) so a later
    change takes effect on the next request with no redeploy needed."""
    raw = (os.environ.get("LEDGER_LIVE") or "").strip().lower()
    return raw in ("1", "on")

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


# --------------------------------------------------------------------------- #
# Budget Ledger STEP 3 (9 Oct 2026, Director-directed, build go): the
# tiles/pace-chart/category-chart maths - every definition below is
# exactly the instruction's own wording, each one independently unit-
# tested against fixtures (tests/test_budget_ledger_step3_tiles.py).
# Money in/out here is always plain DOLLARS (float), not cents - the
# cents/float boundary is tools_store.py's own concern (storage), this
# module stays "plain numbers the caller supplies", same rule as the
# rest of this file.
#
# Director's own decision (9 Oct 2026): the plan has no month-by-month
# history, so a past month's "plan" in Insights is simply the CURRENT
# plan's total, carried back - never invented, never a stored snapshot
# per month. Every function below that takes a `plan`/`bills_plan`
# argument is deliberately agnostic to which month it's being asked
# about for exactly this reason - the caller always passes today's
# plan, whichever month it's computing tiles/charts for.
# --------------------------------------------------------------------------- #

def ledger_plan_total(category_plan_amounts):
    """plan = sum of the user's category plans for the month.
    category_plan_amounts: {category_id: dollar_amount}, same shape
    tools_store.get_budget_plan()'s own "categories" field already has -
    only filled (non-None) categories are ever present in it."""
    return sum(v for v in (category_plan_amounts or {}).values() if v)


def ledger_bills_plan(category_plan_amounts, is_bill_map):
    """bills_plan = sum of plans for categories flagged is_bill.
    is_bill_map: {category_id: bool}, same shape tools_store.
    is_category_bill() answers one category at a time for."""
    return sum(
        v for k, v in (category_plan_amounts or {}).items()
        if v and is_bill_map.get(k)
    )


def ledger_everyday_plan(plan, bills_plan):
    """everyday_plan = plan - bills_plan."""
    return plan - bills_plan


def ledger_spent_total(entries):
    """spent = sum of amount_cents for the month, in dollars. Refunds
    (negative amount_cents, per tools_store's own storage convention)
    reduce it, same as any other row - no special-casing needed since
    they're just negative numbers in the same sum.
    `entries`: list of dicts shaped like tools_store.list_ledger_
    entries()'s own return value (reads "amount_cents")."""
    return sum(e["amount_cents"] for e in (entries or [])) / 100.0


def ledger_everyday_spent(entries, is_bill_map):
    """everyday_spent = spent in categories NOT flagged is_bill."""
    return sum(
        e["amount_cents"] for e in (entries or [])
        if not is_bill_map.get(e.get("category"))
    ) / 100.0


def ledger_days_in_month(year, month):
    """Number of days in this calendar year/month - plain date
    arithmetic (next month's 1st, minus a day), no `calendar` import."""
    from datetime import date as _d, timedelta as _td
    if month == 12:
        _next_first = _d(year + 1, 1, 1)
    else:
        _next_first = _d(year, month + 1, 1)
    return (_next_first - _td(days=1)).day


def ledger_today_of_month(year, month, today_date):
    """today = today's day of the month, if (year, month) IS the
    month today_date falls in; days_in_month otherwise (a past month
    is treated as fully elapsed, per the instruction's own wording -
    the Ledger's own month picker never offers a future month, so that
    case is academic here, but this function is agnostic to it too:
    it falls into the same "not today's month" branch)."""
    if (today_date.year, today_date.month) == (year, month):
        return today_date.day
    return ledger_days_in_month(year, month)


def ledger_safe_to_spend_per_day(plan, spent, days_in_month, today):
    """Safe to spend per day = (plan - spent) / days left, INCLUDING
    today. On the last day (today == days_in_month), divides by 1, not
    0. If the result would be <= 0 (already at or past plan), returns
    0.0 - the caller shows "$0" + "already at plan" for that case."""
    days_left = max(days_in_month - today + 1, 1)
    remaining = plan - spent
    if remaining <= 0:
        return 0.0
    return remaining / days_left


def ledger_projection_visible(today, everyday_entry_count):
    """The "On current pace, month ends" tile is hidden (the caller
    shows "needs a few days of data" instead) until today >= 3 AND
    there is at least one everyday (non-bill) entry logged."""
    return today >= 3 and everyday_entry_count > 0


def ledger_projection(bills_plan, everyday_spent, today, days_in_month):
    """projection = bills_plan + everyday_spent / today * days_in_month -
    "on current pace, what the whole month ends at." Caller is
    responsible for checking ledger_projection_visible() first; this
    function itself just does the arithmetic (today=0 would divide by
    zero, so it returns None rather than raising in that edge case,
    even though the visibility gate above should already prevent it
    from ever being called with today < 3)."""
    if not today:
        return None
    return bills_plan + (everyday_spent / today) * days_in_month


def ledger_pace_line_value(bills_plan, everyday_plan, day, days_in_month):
    """One point on the pace chart's own "pace" line (grey dashed):
    bills_plan + everyday_plan * day / days_in_month - the whole
    month's bills counted from day 1 (per the mock's own rule: "a
    category marked as a bill is counted in full on day 1"), the
    everyday budget ramping up linearly across the month."""
    if days_in_month <= 0:
        return bills_plan
    return bills_plan + everyday_plan * (day / days_in_month)
