"""
super_engine.py

Part 42 (13 Sep 2026, mock: section 4 of services_2345_mock.html) - pure
math for the new "Super & Retirement projector" Money Tool. NO Streamlit,
NO network calls, NO imports from any scoring/analysis engine - this
module is called by app.py's _render_super_tool the same way every other
Money Tool calls its own *_engine.py (debt_recycling_engine.py,
budget_planner_engine.py), and is unit-tested standalone (see
test_pf_super.py in this Part's own test harness).

AU-ONLY v1, per the instruction's own scope: the Superannuation Guarantee
(SG), the 15% contributions tax, and the concessional cap are all
Australian-specific rules. A US 401(k)/IRA variant is explicitly out of
scope for this Part (the render layer shows a one-line placeholder for
country="us" instead of calling into this module at all).

RATES - every one below is a named, dated constant with its own source
note, per the instruction's own "verify the current rate before
hardcoding" requirement. All three were re-verified via live web search
on 12 Sep 2026 (this Part's own build date) against ato.gov.au directly -
see this Part's report for the search transcript summary.
"""

# Superannuation Guarantee (SG) rate: the minimum % of ordinary time
# earnings an AU employer must pay into super. 12% is the FINAL step of
# the legislated glide path that ran 9.5% (to 30 Jun 2021) up through
# 12% from 1 Jul 2025 - there is no further scheduled increase, so this
# has now been the flat rate for AU workers since 1 Jul 2025 and remains
# so through FY2026-27 (verified 12 Sep 2026).
# Source: https://www.ato.gov.au/tax-rates-and-codes/key-superannuation-rates-and-thresholds/super-guarantee
SUPER_GUARANTEE_RATE = 0.12

# Contributions tax on concessional (before-tax) super contributions -
# SG and salary-sacrifice both count. Flat 15% for anyone below the
# Division 293 income threshold (see DIVISION_293_THRESHOLD_AUD below);
# this module does not model the extra 15% Division 293 tax itself, only
# flags when it applies (division293_check) - the ATO assesses that
# separately outside the fund.
CONTRIBUTIONS_TAX_RATE = 0.15

# Concessional (before-tax) contributions cap per financial year -
# indexed in $2,500 steps tied to AWOTE, so it does not move every year.
# $30,000 applied 1 Jul 2024-30 Jun 2026 (FY2024-25 and FY2025-26); it
# stepped up to $32,500 from 1 Jul 2026 (FY2026-27) - the period this
# app's "today" (12 Sep 2026) actually falls inside, so v1 uses the
# CURRENT $32,500 figure rather than the now-superseded $30,000 the
# original mock illustrated with (that mock was drawn before this
# indexation step - see this Part's report for the full note).
# Source: https://www.ato.gov.au/individuals-and-families/super-for-individuals-and-families/super/growing-and-keeping-track-of-your-super/caps-limits-and-tax-on-super-contributions/concessional-contributions-cap
CONCESSIONAL_CAP_AUD = 32500.0

# Division 293 tax: an EXTRA 15% tax (on top of the standard 15% above)
# on concessional contributions for anyone whose combined income +
# low-tax contributions exceed this threshold. Unchanged since it was
# introduced at $250,000 in FY2017-18 - not indexed, unlike the caps
# above (verified 12 Sep 2026).
# Source: https://www.ato.gov.au/individuals-and-families/super-for-individuals-and-families/super/growing-and-keeping-track-of-your-super/caps-limits-and-tax-on-super-contributions/division-293-tax
DIVISION_293_THRESHOLD_AUD = 250000.0

# Preservation age floor shown as a caption only (this module never
# blocks a retirement-age input below this - it's informational, per the
# instruction's own "min 60 note re preservation age" wording, since
# everyone born after 30 Jun 1964 already has a preservation age of 60).
MIN_PRESERVATION_AGE = 60


def project_super(age, retirement_age, balance, salary,
                   extra_sacrifice_monthly, return_rate_annual,
                   sg_rate=SUPER_GUARANTEE_RATE,
                   contrib_tax_rate=CONTRIBUTIONS_TAX_RATE):
    """Year-by-year compounding projection from `age` to `retirement_age`,
    for BOTH paths at once: "baseline" (SG only) and "with_sacrifice" (SG
    + the stated extra salary sacrifice). Contributions are added ONCE A
    YEAR, at year-end, after that year's growth on the OPENING balance -
    the simplest, most easily hand-checked compounding order (never
    overstates growth on money that hasn't been contributed yet):

        balance_end_of_year = balance_start_of_year * (1 + return_rate)
                               + (gross_annual_contributions * (1 - contrib_tax_rate))

    Salary and the extra sacrifice amount are held CONSTANT across every
    year (no wage growth, no inflation-adjustment of the sacrifice) -
    nominal dollars throughout, exactly as the instruction's own framing
    requires ("Nominal dollars, one caption saying so").

    Returns {"years", "baseline": [{"age","balance"}, ...],
    "with_sacrifice": [...], "end_balance_baseline", "end_balance_with_
    sacrifice", "delta", "sg_annual", "extra_annual"}. An empty/zero-year
    request (retirement_age <= age) returns both series empty and both
    end balances equal to the starting balance (delta 0.0)."""
    years = max(int(retirement_age) - int(age), 0)
    sg_annual = salary * sg_rate
    extra_annual = extra_sacrifice_monthly * 12.0

    def _run(extra):
        bal = balance
        series = []
        for y in range(1, years + 1):
            gross_contrib = sg_annual + extra
            net_contrib = gross_contrib * (1 - contrib_tax_rate)
            bal = bal * (1 + return_rate_annual) + net_contrib
            series.append({"age": age + y, "balance": round(bal, 2)})
        return series

    baseline = _run(0.0)
    with_sacrifice = _run(extra_annual)
    end_baseline = baseline[-1]["balance"] if baseline else round(balance, 2)
    end_with_sacrifice = with_sacrifice[-1]["balance"] if with_sacrifice else round(balance, 2)

    return {
        "years": years,
        "baseline": baseline,
        "with_sacrifice": with_sacrifice,
        "end_balance_baseline": end_baseline,
        "end_balance_with_sacrifice": end_with_sacrifice,
        "delta": round(end_with_sacrifice - end_baseline, 2),
        "sg_annual": round(sg_annual, 2),
        "extra_annual": round(extra_annual, 2),
    }


def concessional_cap_check(salary, extra_sacrifice_monthly,
                            sg_rate=SUPER_GUARANTEE_RATE, cap=CONCESSIONAL_CAP_AUD):
    """⚠ Cap check: does SG + the stated extra sacrifice exceed the
    concessional cap for the year? Returns {"sg_annual", "extra_annual",
    "total", "cap", "exceeds", "headroom"} - headroom is the $ still
    available under the cap (0.0, never negative, when already over)."""
    sg_annual = salary * sg_rate
    extra_annual = extra_sacrifice_monthly * 12.0
    # Round to the cent BEFORE comparing to the cap - every dollar figure
    # here is ultimately cents-precision money, and comparing raw floats
    # (e.g. 0.12 * some salary) can land a fraction of a cent either side
    # of an exact-cap boundary purely from binary floating-point
    # representation, not a real difference. Rounding first makes an
    # "exactly at the cap" input behave exactly like the display figure
    # a user actually sees.
    total = round(sg_annual + extra_annual, 2)
    return {
        "sg_annual": round(sg_annual, 2),
        "extra_annual": round(extra_annual, 2),
        "total": total,
        "cap": cap,
        "exceeds": total > cap,
        "headroom": round(max(cap - total, 0.0), 2),
    }


def division293_check(salary, concessional_total, threshold=DIVISION_293_THRESHOLD_AUD):
    """Division 293 note: applies when salary + concessional
    contributions together exceed the threshold (the instruction's own
    simplified test - true Division 293 "income" also folds in
    reportable fringe benefits and net investment losses, which this
    calculator doesn't collect, so this is a conservative single-line
    FLAG to raise with an accountant, never a computed extra-tax figure).
    Returns {"combined", "threshold", "applies"}."""
    combined = salary + concessional_total
    return {
        "combined": round(combined, 2),
        "threshold": threshold,
        "applies": combined > threshold,
    }


def tax_wedge(sacrifice_amount, marginal_rate, contrib_tax_rate=CONTRIBUTIONS_TAX_RATE):
    """The mock's own "that $500 costs you ≈$305 in take-home (39%
    marginal) but lands as $425 in super (15% contributions tax)"
    caption, computed generically for ANY sacrifice_amount (monthly OR
    annual - purely a multiplier, the caller decides which figure to
    pass) and ANY marginal_rate (0-1).

    take_home_cost: what this amount would have been worth AFTER income
    tax if taken as ordinary salary instead (the true opportunity cost of
    sacrificing it) = amount * (1 - marginal_rate).

    super_landing: what actually lands in the fund after the flat 15%
    contributions tax = amount * (1 - contrib_tax_rate).

    head_start_pct: how much further ahead the super outcome is than the
    take-home outcome, as a PERCENTAGE OF THE TAKE-HOME FIGURE (not the
    marginal rate itself, which only looks similar at some marginal
    rates - see this module's own report note) = (super_landing -
    take_home_cost) / take_home_cost * 100, or None if take_home_cost is
    0 (marginal_rate == 100%, a degenerate input).

    Returns {"take_home_cost", "super_landing", "head_start_pct"}."""
    take_home_cost = sacrifice_amount * (1 - marginal_rate)
    super_landing = sacrifice_amount * (1 - contrib_tax_rate)
    head_start_pct = (
        (super_landing - take_home_cost) / take_home_cost * 100.0
        if take_home_cost else None
    )
    return {
        "take_home_cost": round(take_home_cost, 2),
        "super_landing": round(super_landing, 2),
        "head_start_pct": round(head_start_pct, 1) if head_start_pct is not None else None,
    }
