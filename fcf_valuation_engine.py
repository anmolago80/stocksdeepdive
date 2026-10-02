"""
Discounted Free Cash Flow (DCF) intrinsic value.

Values a company on the cash it actually throws off rather than an assumed
multiple. Every input is *sourced* from the stock's own data - nothing here is
a hand-entered per-stock number.

Model (a standard two-stage DCF):
    1. Base = latest annual free cash flow per share (from the cash-flow
       statement, else info["freeCashflow"], else a per-stock manual override).
    2. Grow it for GROWTH_YEARS at an estimated growth rate, discounting each
       year back to today at the discount rate.
    3. Add a Gordon-growth terminal value for everything beyond the horizon.
    intrinsic = sum(discounted stage-1 cash flows) + discounted terminal value

GROWTH RATE, in priority order (growth-never-zero rewrite, 30 Sep 2026,
owner-directed - see estimate_growth()'s own docstring for the full
mechanics; growth NEVER resolves to a flat 0% any more):
    1. ANALYST CONSENSUS - Yahoo's own "Next 5 Years (per annum)" analyst
       growth estimate for this specific stock, when Yahoo has coverage
       AND the value is positive (capm_engine.get_growth_estimates_5y,
       which now also reads the "LTG" row label Yahoo has switched to -
       see that function's own docstring). This is the only input here
       that comes from outside the stock's own reported financials.
    2. HISTORICAL FCF CAGR - only when Yahoo's estimate is missing or
       non-positive, and only when there are enough data points to trust
       a trend (see growth_from_history()'s own docstring for the
       "average of the oldest 2 years -> average of the newest 2"
       formula). A NOISY (high year-to-year volatility) but still
       positive signal is now USED, not rejected - just held to a
       tighter, tier-relative cap (see HISTORY_VOLATILE_CAP_FRACTION).
    3. A single-period reported growth figure (info["revenueGrowth"],
       then info["earningsGrowth"]) - held to a tier-relative cap (see
       REPORTED_GROWTH_CAP_FRACTION) regardless of market-cap tier, the
       least trustworthy signal here (no multi-year smoothing, no
       analyst consensus).
    4. The market-cap-interpolated growth-path END RATE itself (growth_
       end_rate_for(), always strictly positive) - used only when
       nothing else above is usable. Flagged as a DEFAULT so the app
       can render it in red, but never 0%.

DISCOUNT RATE - market-cap-tiered cost of equity, computed per stock
(capm_engine.py, A6 design, owner-approved live 28 Sep 2026):
    discount_rate = risk_free_rate(currency) + market_cap_tier_premium
Beta is not used anywhere in this formula any more - see capm_engine.py's
own module docstring for the full rationale and the tier table.

TERMINAL / PERPETUAL GROWTH RATE - tied to the stock's own CURRENCY
(capm_engine.PERPETUAL_GROWTH_BY_CCY), on the reasoning that no company can
out-grow its own economy forever in a Gordon-growth model. Replaces the old
flat 3% for every stock.

Defaults:
    growth_years     = 10

All three key inputs (growth, discount, perpetual) remain parameters - the
app's "Valuation & FCF inputs" panel passes an explicit value when Auto mode
is off, or when a per-stock override is set, and it's used as-is instead of
being auto-calculated. A per-stock manual FCF can also be supplied so the
model still does the maths, it just starts from a cash-flow figure you trust.

Returns (intrinsic_value_per_share, growth_rate_used, meta) where meta records
where each input came from and whether a default/average had to be assumed.
"""

import logging
import math
import re
import time

import yfinance as yf

import capm_engine
import financials_classifier
import share_class_engine

# Growth-never-zero rewrite (owner-directed, 30 Sep 2026): same per-module
# logger convention as capm_engine.py's "sdd.growth" - used only for the
# defensive WARNING tripwire in estimate_growth() (see GROWTH_FLOOR's own
# comment), never for a normal/expected code path.
_growth_logger = logging.getLogger("sdd.growth")

# Kept only as an absolute last-resort fallback if capm_engine itself throws
# (e.g. both the live and fallback risk-free lookups somehow fail).
DEFAULT_DISCOUNT_RATE = 0.09
# Owner-reported (28 Sep 2026): this used to be a hardcoded 0.03 (3%) -
# a THIRD terminal-growth value, matching neither of capm_engine.
# PERPETUAL_GROWTH_BY_CCY's own currency-keyed rates (AUD 2.5% / USD
# 2.0%). Imported from capm_engine.DEFAULT_PERPETUAL_GROWTH instead, so
# this rarely-hit exception-path fallback (capm_engine.resolve_
# perpetual_rate() itself raising - see the `except Exception:` below)
# stays consistent with the rest of the system rather than silently
# drifting from it.
DEFAULT_PERPETUAL_RATE = capm_engine.DEFAULT_PERPETUAL_GROWTH
DEFAULT_GROWTH_YEARS = 10

# Clamp estimated growth into a defensible band: no negative compounding in
# stage 1, and a ceiling so a hot trailing number can't produce a fantasy
# valuation. Terminal growth must stay below the discount rate or Gordon blows
# up.
#
# Growth-never-zero rewrite (owner-directed, 30 Sep 2026): GROWTH_FLOOR
# stays 0.0 as a pure mathematical clamp (stage 1 can't compound at a
# negative rate), but it is now provably UNREACHABLE - every one of
# estimate_growth()'s four priority paths (Yahoo/history/reported/tier-
# end-rate default) requires or produces a strictly positive number
# before this floor is even applied. See that function's own _finalize()
# helper for the WARNING-level tripwire that fires if this floor is ever
# actually hit - a defensive check, not an expected code path. DEFAULT_
# GROWTH (the old flat 5% "nothing else available" fallback) is REMOVED
# - priority 4 now resolves to the market-cap-tiered growth-path end
# rate instead (see growth_end_rate_for(), always > 0).
GROWTH_FLOOR = 0.00
GROWTH_CEIL = 0.20

# Growth-never-zero rewrite (owner-directed, 30 Sep 2026, replacing the
# 28 Sep TOYO fix's flat REPORTED_GROWTH_CAP=0.08): a single-period
# info["revenueGrowth"]/info["earningsGrowth"] figure - still the least
# trustworthy growth signal this module has (no multi-year smoothing, no
# analyst consensus, easily distorted by a one-off swing) - is now
# capped at a FRACTION of the market-cap tier ceiling instead of a flat
# number, so the cap stays proportionate if the tier table itself ever
# changes. Applied as max(end_rate, ceiling * REPORTED_GROWTH_CAP_
# FRACTION) - today's tiers (8/12/16/20%) give 4/6/8/10% caps, closing
# the same TOYO-shaped false positive (reported growth riding a loose
# tier ceiling straight to the top) the flat 8% constant closed, without
# hard-coding a single number that stops tracking the tier table.
# Raise to the FULL tier ceiling by changing this one constant to 1.0.
REPORTED_GROWTH_CAP_FRACTION = 0.5

# Same tier-relative treatment for a volatile-but-still-positive historical
# FCF CAGR (see estimate_growth()'s "history_volatile" branch) - a noisy
# multi-year trend is a real, multi-year signal (unlike a single reported
# period), so it's USED rather than rejected outright, but held to the
# same fractional cap as the reported-growth fallback rather than the
# full tier ceiling a clean history signal gets.
HISTORY_VOLATILE_CAP_FRACTION = 0.5

# growth_from_history() needs at least this many data points before its
# CAGR is trusted as a real trend at all: n=2 is EXACTLY 0% by
# construction (see that function's own "oldest_avg==newest_avg
# regardless" comment - both smoothing windows are the identical 2
# points), and n=3 still has overlapping smoothing windows with only 1
# elapsed index-period - both read as "not enough distinct years to
# measure a trend", not a genuine (if flat) growth signal, so
# estimate_growth() treats both as "no history" rather than a real 0%.
MIN_HISTORY_POINTS_FOR_TREND = 4

# Task 10: how far the latest year's capex-normalised OCF may deviate from
# the median of the last up-to-3 years before it's treated as an outlier
# reporting year and swapped for that median instead (see
# normalized_base_and_series). 0.40 = 40%.
FCF_OUTLIER_THRESHOLD = 0.40

# Step 4 (owner-directed, 30 Sep 2026, KO fix): Task 10's 3-year-median
# swap can't help when the DISTORTION spans two of the three comparison
# years (KO's own shape - 2024's fairlife earn-out AND 2025's IRS tax
# deposit both depressed OCF while the underlying business was fine).
# This targets the ROOT CAUSE instead of just an outlier-vs-recent-
# median comparison: a year is "distorted" when its own operating cash
# flow fell more than FCF_ONEOFF_OCF_DROP (30%) against the immediately
# prior year while EBITDA held up (fell less than FCF_ONEOFF_EBITDA_
# TOLERANCE, 10%) - a real operating deterioration would show up in
# both; a one-off cash item hits OCF alone. FCF_ONEOFF_TWO_YEAR_
# THRESHOLD (25%) extends a detected distortion into the following
# (more recent) year too, as long as ITS OWN OCF is still that far
# below the last genuinely clean year AND its own EBITDA is also still
# within tolerance of the last clean year's EBITDA - the two-year one-
# off shape. See _detect_distorted_years()'s own docstring for the
# exact algorithm.
#
# TIGHTENED 2 Oct 2026 (owner decision, 18:15 AEST, incident report
# analysis (a)/(b) - 486 DCF-unreliable rows and inflated intrinsic
# values the night before, including real blue-chips). An initial draft
# of this tightening also removed the operating-income/revenue fallback
# cross-check tiers in _oneoff_metric_series() - REVERTED the same day,
# 18:40 AEST: the owner's own admin audit (1,329 tickers, median dIV
# +0.4%, only 17 > +50%) showed Step 4 was NOT over-firing broadly and
# the 486 DCF-unreliable rows predated it (cyclical miners etc.), so
# those tiers stay exactly as they were - CSL's own fixture depends on
# tier 2. Two changes DID stick:
#   1. The two-year extension above now also re-tests the EXTENDED
#      year's own cross-check metric against tolerance (see _detect_
#      distorted_years()'s own docstring) - a year whose metric has
#      also fallen ends the distortion run rather than being carried
#      into it.
#   2. An uplift safety valve (see normalized_base_and_series()'s own
#      docstring) caps any substituted base at FCF_ONEOFF_UPLIFT_CAP_
#      MULTIPLE (3.0x, revised same-day from an initial 1.5x once the
#      admin audit showed KO/~2.06x, BALL/~2.4x and INCY/~3.0x are all
#      genuine one-offs that must NOT be capped) times the raw latest-
#      year figure - only a substitution past that points to an actual
#      multi-year cycle rather than a one-off.
FCF_ONEOFF_OCF_DROP = 0.30
FCF_ONEOFF_EBITDA_TOLERANCE = 0.10
FCF_ONEOFF_TWO_YEAR_THRESHOLD = 0.25
# Uplift safety valve multiple (2 Oct 2026, owner decision, revised
# same-day 18:40 AEST from an initial 1.5x to 3.0x) - see
# normalized_base_and_series()'s own docstring.
FCF_ONEOFF_UPLIFT_CAP_MULTIPLE = 3.0
# How many of the most recent years this mechanism considers, and the
# minimum number of CLEAN (non-distorted) years within that window
# required before trusting their median as the base - fewer than this
# falls to the EBITDA bridge instead (see _ebitda_bridge_base()).
FCF_ONEOFF_WINDOW_YEARS = 5
FCF_ONEOFF_MIN_CLEAN_YEARS = 3

# Continuous-in-market-cap growth ceiling/end rate (owner-directed, 1 Oct
# 2026 23:09 AEST, "all you are doing is interpolating"): the step tables
# this replaced had the same cliff problem capm_engine.py's size premium
# had before its own Push 2 fix - a company at $9.9B got a 16% ceiling, at
# $10.1B 12%, with nothing about the business changing across that 2%
# price move. Same log-linear-interpolation mechanism as capm_engine.
# _interpolate_size_premium() (see that function's own docstring for the
# general approach), applied here as a local copy (_log_interpolate()
# below) rather than a shared import - capm_engine.py's version is tightly
# coupled to its own module-local SIZE_PREMIUM_ANCHORS_USD table and is a
# live, already-working discount-rate path; duplicating the ~15-line
# interpolation body here, with this comment pointing at the original, is
# lower-risk than threading a new shared helper through a function that
# already has its own tests and callers.
#
# Thresholds are USD-equivalent so they're consistent across currencies.
# FX_TO_USD_APPROX is a rough, static snapshot used only to BUCKET a
# company for interpolation - not a live rate - so being off by a few
# percent only shifts the result slightly, never jumps it across a band.
FX_TO_USD_APPROX = {
    "USD": 1.0,
    "AUD": 0.65,
}

# (market cap USD, value) pairs, LARGEST CAP FIRST - same convention as
# capm_engine.SIZE_PREMIUM_ANCHORS_USD. Anchors sit at the GEOMETRIC
# MIDPOINT of each of the old step table's tiers (owner choice: "midpoints,
# not tier floors", so the typical company already sitting mid-tier keeps
# today's value exactly; only a company near an old boundary sees its
# number move, and only by a little) - e.g. the old mid-cap tier ran
# $2B-$10B, so its anchor sits at sqrt(2B * 10B) = $4.47B. The top and
# bottom anchors ($200B and $775M = sqrt(300M*2B)) are flat beyond
# themselves: >= $200B stays at the mega-cap value, <= $775M stays at the
# small-cap value - see _log_interpolate()'s own docstring.
GROWTH_CEILING_ANCHORS_USD = [
    (200_000_000_000, 0.08),   # mega: 8% from $200B upward (flat above)
    (44_700_000_000,  0.12),   # large midpoint: sqrt(10B * 200B)
    (4_470_000_000,   0.16),   # mid midpoint: sqrt(2B * 10B)
    (775_000_000,     0.20),   # small midpoint: sqrt(300M * 2B); flat below
]

# Same midpoint convention as GROWTH_CEILING_ANCHORS_USD above, for the
# stage-1 fade's own END RATE (growth_end_rate_for() below) - a
# DELIBERATELY SEPARATE anchor table from the ceiling's, not derived from
# it, even though today's values happen to match capm_engine.SIZE_PREMIUM_
# ANCHORS_USD one-for-one at every anchor: growth end rate and discount-
# rate premium are two different knobs (one shapes the cash-flow growth
# path, the other the cost of equity) that only coincide in value because
# the owner chose the same numbers for both - hard-linking them would mean
# a future change to one silently move the other.
GROWTH_END_RATE_ANCHORS_USD = [
    (200_000_000_000, 0.02),   # flat above $200B
    (100_000_000_000, 0.03),   # sqrt(50B * 200B)
    (22_400_000_000,  0.04),   # sqrt(10B * 50B)
    (4_470_000_000,   0.05),   # sqrt(2B * 10B)
    (775_000_000,     0.06),   # sqrt(300M * 2B); flat below
]


def _log_interpolate(anchors, market_cap_usd):
    """Log-linear interpolation of a value between the fixed (market cap
    USD, value) `anchors` (descending by cap), on log10(market_cap_usd) -
    the same mechanism as capm_engine._interpolate_size_premium() (see
    that function's own docstring; this is a local copy, not a shared
    import - see GROWTH_CEILING_ANCHORS_USD's own comment above for why).

    Above the top anchor the value is flat at the top anchor's own value;
    at or below the bottom anchor (including a non-positive market cap)
    it's flat at the bottom anchor's value; between two anchors it's a
    straight line in LOG-cap space, so a company halfway (in orders of
    magnitude) between two anchors gets a value halfway between theirs.

    Boundary-exactness: at any anchor's OWN market cap, this returns that
    anchor's exact value (frac lands on exactly 0.0 or 1.0 - no floating-
    point surprise), so a company sitting exactly on an anchor never sees
    a value a hair off the one named in the anchor table."""
    if market_cap_usd <= 0:
        return anchors[-1][1]
    if market_cap_usd >= anchors[0][0]:
        return anchors[0][1]
    if market_cap_usd <= anchors[-1][0]:
        return anchors[-1][1]
    log_cap = math.log10(market_cap_usd)
    for i in range(len(anchors) - 1):
        hi_cap, hi_val = anchors[i]
        lo_cap, lo_val = anchors[i + 1]
        if lo_cap <= market_cap_usd <= hi_cap:
            frac = (log_cap - math.log10(lo_cap)) / (math.log10(hi_cap) - math.log10(lo_cap))
            return lo_val + frac * (hi_val - lo_val)
    return anchors[-1][1]   # unreachable given the short-circuits above; defensive only


def growth_ceiling_for(info, currency=None):
    """
    Market-cap-interpolated growth ceiling - replaces the flat GROWTH_CEIL
    as the upper bound estimate_growth() (and a manual override) is
    clamped to. A continuous log-linear curve through GROWTH_CEILING_
    ANCHORS_USD (see that table's own comment), not a step function - see
    this module's own "Continuous-in-market-cap" comment above for why.
    Falls back to the original flat GROWTH_CEIL whenever market cap isn't
    available - same fail-safe philosophy as everything else in this
    module: a missing data point should never block a valuation, just make
    it slightly less size-aware. Rounded to 4 decimals so stored meta is
    stable.
    """
    info = info or {}
    market_cap = info.get("marketCap")
    if not market_cap or market_cap <= 0:
        return GROWTH_CEIL

    ccy = (currency or info.get("currency") or "USD").upper()
    market_cap_usd = market_cap * FX_TO_USD_APPROX.get(ccy, 1.0)

    return round(_log_interpolate(GROWTH_CEILING_ANCHORS_USD, market_cap_usd), 4)


def growth_end_rate_for(info, currency=None):
    """
    Market-cap-interpolated growth-path END RATE - the rate the stage-1
    fade (years 6-growth_years) targets, NOT the currency perpetual/
    terminal rate the Gordon terminal value still uses (a separate input -
    see dcf_intrinsic_value()'s own stage-1 loop comment). A continuous
    log-linear curve through GROWTH_END_RATE_ANCHORS_USD (see that
    table's own comment), not a step function. Falls back to DEFAULT_
    PERPETUAL_RATE whenever market cap isn't available - same fail-safe
    philosophy as growth_ceiling_for() above; a missing data point should
    never block a valuation. Rounded to 4 decimals so stored meta is
    stable.
    """
    info = info or {}
    market_cap = info.get("marketCap")
    if not market_cap or market_cap <= 0:
        return DEFAULT_PERPETUAL_RATE

    ccy = (currency or info.get("currency") or "USD").upper()
    market_cap_usd = market_cap * FX_TO_USD_APPROX.get(ccy, 1.0)

    return round(_log_interpolate(GROWTH_END_RATE_ANCHORS_USD, market_cap_usd), 4)


# Cash-flow-statement row labels vary across yfinance versions / listings -
# AND across data sources: a bundle built from EODHD instead of yfinance
# returns the same rows under EODHD's own lower-camelCase JSON keys
# (audit fix 1.2). auto_compounder_engine.py's own _ROW_ALIASES already
# lists both spellings for exactly this reason; these lists mirror that
# (kept as a separate, local list rather than importing
# auto_compounder_engine, which itself imports this module - importing it
# back would be a circular import).
_FCF_LABELS = ("Free Cash Flow", "FreeCashFlow", "Free cash flow", "freeCashFlow")
_OCF_LABELS = (
    "Operating Cash Flow", "Total Cash From Operating Activities",
    "OperatingCashFlow", "Cash Flow From Continuing Operating Activities",
    "totalCashFromOperatingActivities",
)
_CAPEX_LABELS = (
    "Capital Expenditure", "CapitalExpenditures", "Capital Expenditures",
    "capitalExpenditures",
)
# Financials mode (owner-directed, 2 Oct 2026, VERSION A - see
# financials_classifier.py's own docstring for why): a bank/insurer's
# operating cash flow includes float/deposit flows that aren't
# shareholder cash, so net income - not OCF - is the DCF base for a
# financials-mode ticker. Local copy, not a reuse of auto_compounder_
# engine.py's own "net_income" row-alias list, which is for compounder
# display, not the DCF, and orders fallbacks differently - same mirror-
# not-import reasoning as every other label tuple in this module.
_NET_INCOME_LABELS = ("Net Income Common Stockholders", "Net Income")
# Step 4 (owner-directed, 30 Sep 2026): income-statement row labels for
# the distorted-year cross-check and EBITDA bridge below - same mirror-
# not-import reasoning as the cash-flow labels above, spelled to match
# auto_compounder_engine._ROW_ALIASES exactly (revenue/operating_income/
# reconciled_depreciation/pretax_income/tax_provision) for consistency.
_OPERATING_INCOME_LABELS = ("Operating Income", "Total Operating Income As Reported")
_REVENUE_LABELS = ("Total Revenue", "Revenue", "Operating Revenue", "totalRevenue")
_DA_LABELS = ("Reconciled Depreciation",)
_PRETAX_INCOME_LABELS = ("Pretax Income", "Income Before Tax", "incomeBeforeTax")
_TAX_PROVISION_LABELS = ("Tax Provision", "Income Tax Expense", "incomeTaxExpense")
# Push 3 point 7 (owner-directed, 30 Sep 2026, stability-signal fix -
# retrofitted into the ALREADY-SHIPPED Step 4 mechanism, not just the
# new EPS one below): gross profit for the revenue+gross-profit dual
# check - see _oneoff_metric_series()'s own docstring for why this
# replaces plain operating-income/revenue as the middle tier.
_GROSS_PROFIT_LABELS = ("Gross Profit", "grossProfit")
# Any income-statement row whose label matches this is an add-back
# candidate for the top (EBITDA) tier - CSL's own shape (a write-down
# that collapsed operating income) is exactly what this catches: the
# write-down amount is added BACK to operating income before computing
# EBITDA, so a one-off impairment/restructuring charge doesn't itself
# look like an operating deterioration.
_ONEOFF_ADDBACK_LABEL_RE = re.compile(r"(?i)impair|write.?down|write.?off|restructur")


def _addback_series(income_df):
    """Push 3 point 7: per-year (most-recent-first) sum of every income-
    statement row whose label matches _ONEOFF_ADDBACK_LABEL_RE - unlike
    every other row lookup in this module, this deliberately does NOT
    use a fixed alias list (impairment/write-down/restructuring line
    labels vary far more than revenue/operating-income ones do across
    tickers and data sources), so it scans every row in the statement.
    Returns a list the same length as income_df's columns (0.0 for a
    year with no matching row), or None if income_df has no rows at
    all. Reported as a POSITIVE expense magnitude in this codebase's
    income-statement shape (already subtracted to reach operating
    income), so it's ADDED BACK, not subtracted."""
    if income_df is None or getattr(income_df, "empty", True):
        return None
    n = income_df.shape[1]
    total = [0.0] * n
    matched = False
    for label in income_df.index:
        if _ONEOFF_ADDBACK_LABEL_RE.search(str(label)):
            try:
                vals = [float(v) for v in income_df.loc[label].tolist()]
            except Exception:
                continue
            if len(vals) != n:
                continue
            matched = True
            total = [t + (v if v == v else 0.0) for t, v in zip(total, vals)]
    return total if matched else [0.0] * n


def _row(cashflow_df, labels):
    """Return the first matching row (as a list of floats, most-recent-first)
    from a cash-flow DataFrame, or None. Tries an exact label match first,
    then falls back to a case-insensitive substring match (mirrors
    auto_compounder_engine._find_row()'s tolerance) so a source whose exact
    spelling isn't in `labels` - e.g. an EODHD key we haven't enumerated -
    still resolves instead of silently returning None."""
    if cashflow_df is None or getattr(cashflow_df, "empty", True):
        return None
    for label in labels:
        if label in cashflow_df.index:
            try:
                return [float(v) for v in cashflow_df.loc[label].tolist()]
            except Exception:
                continue
    lower_idx = {str(i).lower(): i for i in cashflow_df.index}
    for label in labels:
        nl = label.lower()
        for li, orig in lower_idx.items():
            if nl in li or li in nl:
                try:
                    return [float(v) for v in cashflow_df.loc[orig].tolist()]
                except Exception:
                    continue
    return None


def _oneoff_metric_series(income_df):
    """Step 4 (owner-directed, 30 Sep 2026), RETROFITTED by Push 3 point 7
    (owner-directed, 30 Sep 2026, stability-signal fix). UNCHANGED by the
    2 Oct 2026 tightening (owner amendment, 18:40 AEST): an earlier draft
    of that tightening removed tiers 2/3 here, but the owner's own A6-
    style admin audit (1,329 tickers, median dIV +0.4%, only 17 > +50%)
    showed Step 4 was NOT over-firing broadly and the 486 DCF-unreliable
    rows predated it (cyclical miners etc.) - so this function is back to
    its pre-tightening shape; CSL's own fixture still depends on tier 2.
    See _detect_distorted_years()'s own docstring and normalized_base_
    and_series()'s own docstring for what DID change in this tightening
    (the two-year extension re-test, and the uplift safety-valve cap).

    Per-year (most-recent-first) cross-check metric(s) for
    _detect_distorted_years() below. In order:
      1. EBITDA (operating income + D&A) WITH one-off items (impairment/
         write-down/write-off/restructuring rows - see _ONEOFF_ADDBACK_
         LABEL_RE) added back, when both operating income and D&A rows
         are available - CSL's own shape (a write-down that collapsed
         operating income, and therefore the plain oi+da EBITDA this
         tier used to compute) is exactly what the add-back catches.
         Requires oi+da; the add-back itself defaults to 0 for any year
         with no matching row, so a ticker with no one-off rows at all
         (e.g. KO's own fixture) gets an IDENTICAL series to the pre-
         retrofit plain-EBITDA tier.
      2. Revenue AND gross profit, BOTH required, checked as a dual
         signal (_detect_distorted_years() only calls a year "stable"
         on this tier when NEITHER moved past the tolerance) - CSL's own
         shape again: with no D&A row available, tier 1 can't apply, and
         operating income ALONE (the pre-retrofit tier 2) would have
         wrongly read as "the business deteriorated" when the drop was
         actually the write-down; revenue/gross profit correctly show
         the underlying business was fine.
      3. Operating income alone - LAST RESORT (demoted from tier 2 pre-
         retrofit), only reached when neither of the above can be
         computed.
    Returns (primary_series, secondary_series, tier) - tier is
    "ebitda_addback"|"revenue_gross_profit"|"operating_income"|None.
    secondary_series is only non-None for the "revenue_gross_profit"
    tier (gross profit); every other tier is a single-signal check,
    same as before this retrofit. (None, None, None) when income_df is
    missing or has none of these rows - the caller then can't run the
    cross-check at all and falls through to the pre-existing (Task 10)
    outlier-swap logic unchanged."""
    if income_df is None or getattr(income_df, "empty", True):
        return None, None, None
    oi = _row(income_df, _OPERATING_INCOME_LABELS)
    da = _row(income_df, _DA_LABELS)
    if oi and da and len(oi) == len(da):
        addback = _addback_series(income_df) or [0.0] * len(oi)
        if len(addback) != len(oi):
            addback = [0.0] * len(oi)
        return [o + d + a for o, d, a in zip(oi, da, addback)], None, "ebitda_addback"
    rev = _row(income_df, _REVENUE_LABELS)
    gp = _row(income_df, _GROSS_PROFIT_LABELS)
    if rev and gp and len(rev) == len(gp):
        return rev, gp, "revenue_gross_profit"
    if oi:
        return oi, None, "operating_income"
    return None, None, None


def needs_oneoff_check(cashflow_df):
    """Step 4 nightly-path trigger (owner-directed, 1 Oct 2026, Commit 2
    of instruction_dcf_unreliable_pool_step4_nightly.md): a cheap, pure,
    network-free pre-check so nightly_scan.py/deep_dive_engine.py can
    decide whether a ticker is WORTH the one extra income-statement
    fetch Step 4 itself needs, without ever paying that cost
    unconditionally (owner's 27 Sep 2026 rule: no new Yahoo call per
    ticker per night just-in-case).

    True when the cash-flow statement ALONE shows the latest year's OCF
    (index 0) fell more than FCF_ONEOFF_OCF_DROP (30%) against the prior
    year (index 1), OR the second-latest year (index 1) fell more than
    that against ITS prior year (index 2) - the same primary-signal drop
    test _detect_distorted_years() runs for positions 0 and 1 of its own
    5-year window, minus the EBITDA/revenue cross-check (which needs the
    income statement this function exists to avoid fetching speculatively).
    Checking both positions (not just the latest) catches a KO-shaped
    two-year distortion (2024 AND 2025 both depressed vs clean 2023) at
    the cash-flow-only stage, even though neither position alone is
    checked against the OLDER clean year the way the two-year-extension
    rule inside _detect_distorted_years() does - this function only
    needs to flag "worth fetching the income statement to look closer",
    not reproduce the full distortion call.

    A False here does NOT mean Step 4 wouldn't flag anything (the real
    check, run after the income statement is actually fetched, also
    requires the cross-check metric to have held up) - it means Step 4's
    OWN primary signal can't possibly fire for this ticker from the
    cash-flow data alone, so fetching the income statement would be
    pure waste. Never raises - fewer than 2 usable OCF years (nothing
    to compare) returns False, same as "no distortion possible"."""
    ocf = _row(cashflow_df, _OCF_LABELS)
    ocf = [v for v in (ocf or []) if v == v]
    for i in (0, 1):
        if i + 1 < len(ocf):
            prior_val = ocf[i + 1]
            if prior_val not in (None, 0):
                drop = (prior_val - ocf[i]) / abs(prior_val)
                if drop > FCF_ONEOFF_OCF_DROP:
                    return True
    return False


def _detect_distorted_years(primary, metric_series, secondary_metric_series=None,
                             primary_drop_threshold=FCF_ONEOFF_OCF_DROP,
                             metric_tolerance=FCF_ONEOFF_EBITDA_TOLERANCE,
                             two_year_threshold=FCF_ONEOFF_TWO_YEAR_THRESHOLD,
                             treat_negative_as_distorted=False):
    """Step 4 (owner-directed, 30 Sep 2026, KO fix), GENERALISED by Push 3
    point 7/point 1 (owner-directed, 30 Sep 2026) so the SAME function
    drives both the FCF mechanism (normalized_base_and_series(), passing
    OCF as `primary`, FCF_ONEOFF_*'s own defaults) and the new EPS one
    (passing net income/diluted EPS as `primary`, EPS_ONEOFF_DROP/
    EPS_ONEOFF_EBITDA_TOLERANCE via the threshold kwargs) rather than a
    second copy of the same algorithm. Returns a list of bool, same
    length/order as `primary` (most-recent-first), True for a year whose
    own reporting is judged a one-off distortion rather than a genuine
    operating change.

    Primary test, per year i (comparing against the immediately prior,
    older year i+1): `primary` fell more than `primary_drop_threshold`
    (OR, when treat_negative_as_distorted, `primary[i]` is itself
    negative - the EPS mechanism's own "or is negative" rule) while the
    cross-check metric (_oneoff_metric_series() above) fell less than
    `metric_tolerance` over the SAME span - a real operating
    deterioration would show up in both; a one-off item (an earn-out
    payment, a tax settlement, a write-down, a working-capital swing)
    hits the primary series alone. When `secondary_metric_series` is
    given (the revenue+gross-profit dual-signal tier), a year only
    counts as "the metric held up" when BOTH series stayed within
    tolerance - either one dropping past it means a genuine decline. If
    the metric(s) ALSO fell past tolerance, nothing is marked - see
    FCF_ONEOFF_EBITDA_TOLERANCE's own comment - that's a genuine
    decline, not a one-off.

    Two-year extension, TIGHTENED 2 Oct 2026 (owner decision, 18:15 AEST,
    incident report analysis (b)): once a year is marked distorted, the
    NEXT (more recent) year may be marked too, but ONLY if it ALSO
    passes the cross-check metric itself - its own metric_series value
    is within `metric_tolerance` of the last genuinely CLEAN year's
    metric value (not the already-distorted one immediately before it) -
    in ADDITION to the pre-existing "still more than `two_year_threshold`
    below the last clean year's own PRIMARY value" test. Previously the
    extension never re-checked the metric at all, so a year whose own
    EBITDA had ALSO fallen - a genuine down-cycle year, not a one-off -
    could still be extended into the distortion run and dropped from the
    base/growth series. Now a year failing the metric re-test ends the
    run: it is left un-distorted (so its own real, lower figure counts
    toward the clean median, same "never masks a real decline"
    philosophy as the primary test), and it becomes the new "last clean
    year" reference for anything after it. KO's own shape still passes
    both re-tests at every step (the fairlife earn-out and the IRS tax
    deposit years both held EBITDA within tolerance of 2023's), so KO
    continues to normalise unchanged.

    Implemented as a single oldest-to-newest sweep so "last clean year"
    (for both the primary value and the metric) always means what it
    says."""
    n = len(primary)
    distorted = [False] * n
    if n == 0:
        return distorted
    last_clean_value = primary[n - 1]   # oldest year in the window - nothing before it to test
    last_clean_metric = (
        metric_series[n - 1] if metric_series is not None and n - 1 < len(metric_series) else None
    )
    for i in range(n - 2, -1, -1):  # walk oldest -> newest (i+1 = prior/older year)
        is_distorted = False
        primary_signal = False
        prior_val = primary[i + 1]
        if prior_val not in (None, 0):
            drop = (prior_val - primary[i]) / abs(prior_val)
            if drop > primary_drop_threshold:
                primary_signal = True
        if treat_negative_as_distorted and primary[i] is not None and primary[i] < 0:
            primary_signal = True
        if primary_signal:
            metric_ok = False
            if metric_series is not None and i + 1 < len(metric_series):
                this_m, prior_m = metric_series[i], metric_series[i + 1]
                if this_m is not None and prior_m not in (None, 0):
                    metric_drop = (prior_m - this_m) / abs(prior_m)
                    if metric_drop < metric_tolerance:
                        metric_ok = True
            if metric_ok and secondary_metric_series is not None and i + 1 < len(secondary_metric_series):
                this_s, prior_s = secondary_metric_series[i], secondary_metric_series[i + 1]
                if this_s is not None and prior_s not in (None, 0):
                    secondary_drop = (prior_s - this_s) / abs(prior_s)
                    if secondary_drop >= metric_tolerance:
                        metric_ok = False
                else:
                    metric_ok = False
            if metric_ok:
                is_distorted = True
        if not is_distorted and distorted[i + 1] and last_clean_value not in (None, 0):
            still_depressed = (last_clean_value - primary[i]) / abs(last_clean_value) > two_year_threshold
            if still_depressed:
                metric_also_ok = False
                if metric_series is not None and i < len(metric_series):
                    this_m = metric_series[i]
                    if this_m is not None and last_clean_metric not in (None, 0):
                        metric_drop2 = (last_clean_metric - this_m) / abs(last_clean_metric)
                        if metric_drop2 < metric_tolerance:
                            metric_also_ok = True
                if metric_also_ok:
                    is_distorted = True
        distorted[i] = is_distorted
        if not is_distorted:
            last_clean_value = primary[i]
            last_clean_metric = metric_series[i] if metric_series is not None and i < len(metric_series) else None
    return distorted


def _ebitda_bridge_base(income_df, avg_capex):
    """Step 4 (owner-directed, 30 Sep 2026): base = EBITDA - average
    capex - cash taxes, for when fewer than FCF_ONEOFF_MIN_CLEAN_YEARS
    clean years remain in the distorted-year window (see normalized_
    base_and_series() below). Requires a GENUINE EBITDA figure
    (operating income + D&A both present) - never built from the
    weaker operating-income-only or revenue-only fallback tiers
    _oneoff_metric_series() may have used for the DETECTION test alone;
    returns None (caller falls through to the pre-existing "fcf-median"/
    "info"/"none" cascade) rather than fabricate a bridge from a metric
    that was never meant to carry this formula.

    cash_taxes = operating_income * effective_tax_rate(tax_provision,
    pretax_income) - the same NOPAT-style tax estimate auto_compounder_
    engine.py already applies to operating income elsewhere in this
    codebase (A4 helper, lazily imported here since that module itself
    imports fcf_valuation_engine at module level - a module-level
    import back would be circular, same reasoning as the label lists
    above). avg_capex is already negative (capex sign convention - see
    normalized_base_and_series()'s own comment), so it's ADDED, not
    subtracted, to match that convention."""
    oi = _row(income_df, _OPERATING_INCOME_LABELS)
    da = _row(income_df, _DA_LABELS)
    if not oi or not da:
        return None
    ebitda_latest = oi[0] + da[0]
    pretax = _row(income_df, _PRETAX_INCOME_LABELS)
    tax = _row(income_df, _TAX_PROVISION_LABELS)
    pretax_latest = pretax[0] if pretax else None
    tax_latest = tax[0] if tax else None
    import auto_compounder_engine
    rate = auto_compounder_engine.effective_tax_rate(tax_latest, pretax_latest)
    cash_taxes = oi[0] * rate
    return ebitda_latest + avg_capex - cash_taxes


def extract_fcf_history(cashflow_df):
    """
    Pull a free-cash-flow series (most recent first) out of a yfinance
    cash-flow statement. Uses the explicit Free Cash Flow row if present,
    otherwise reconstructs it as Operating Cash Flow + Capital Expenditure
    (CapEx is reported negative, so addition nets it out).

    Returns a list of annual FCF values (most recent first), or [] if the
    statement isn't usable.
    """
    fcf = _row(cashflow_df, _FCF_LABELS)
    if fcf:
        cleaned = [v for v in fcf if v == v]  # drop NaN
        if len(cleaned) >= 2:
            return cleaned
        # else: FCF row present but too sparse - fall through to rebuild it.

    ocf = _row(cashflow_df, _OCF_LABELS)
    capex = _row(cashflow_df, _CAPEX_LABELS)
    if ocf and capex:
        n = min(len(ocf), len(capex))
        out = []
        for i in range(n):
            o, c = ocf[i], capex[i]
            if o == o and c == c:
                out.append(o + c)  # capex is negative in the statement
        if out:
            return out

    # Last resort: whatever single FCF point we could salvage (not enough for
    # a CAGR, but the caller can still use it as the base cash flow).
    if fcf:
        return [v for v in fcf if v == v]
    return []


def _mean(xs):
    xs = [x for x in xs if x == x]
    return sum(xs) / len(xs) if xs else 0.0


def _coeff_of_variation(series):
    """Volatility measure: stdev / mean-of-absolute-values. High = noisy, so
    a CAGR between two endpoints can't be trusted."""
    series = [x for x in series if x == x]
    if len(series) < 2:
        return 999.0
    scale = _mean([abs(x) for x in series])
    if scale == 0:
        return 999.0
    m = _mean(series)
    var = _mean([(x - m) ** 2 for x in series])
    return (var ** 0.5) / scale


def _ocf_based_base_and_series(cashflow_df, info=None, income_df=None):
    """
    Produce a *normalised* current free cash flow and an FCF series for growth.

    Financials mode (owner-directed, 2 Oct 2026): this is the STANDARD
    (OCF-based) computation, renamed from normalized_base_and_series()
    when that name became a thin dispatcher - see the new normalized_
    base_and_series() wrapper below and _financials_base_and_series()
    for the net-income path a bank/insurer takes instead. Nothing in
    this function's own logic changed; every doc comment below
    describing it as "normalized_base_and_series" below is now reached
    only via that wrapper, not directly.

    The single most recent year is a bad base when a company had a one-off
    capex spike (e.g. COH building a new plant): that year's FCF craters and,
    if used raw, both collapses the DCF level AND turns the growth CAGR
    negative. To avoid that we NORMALISE capex - subtract the AVERAGE capex
    (over the available years) from the latest operating cash flow - so a
    single heavy investment year doesn't define the whole valuation.

    Task 10: even after that capex normalisation, the latest year's OCF
    itself can still be a one-off outlier - a working-capital swing, an
    unusual receivable/payable timing shift, a divestment or litigation
    item flowing through operating cash flow, none of which capex
    normalisation touches. When the latest normalised value deviates from
    the median of the last up-to-3 years by more than
    FCF_OUTLIER_THRESHOLD, the median is used as the base instead. Stable
    tickers (deviation within the threshold, or fewer than 2 years of
    history to compare against) are completely unaffected - identical base
    to before this normalisation existed. The reported-FCF fallback path
    below already bases itself on a 3-year median unconditionally, so it
    needed no separate outlier check.

    Rising-capex guard (owner-directed, 28 Sep 2026, TOYO false positive -
    live symptoms: price $4.47, DCF $41.67, growth 20% from the "reported
    growth" fallback, a micro-cap): averaging capex correctly smooths a
    one-off spike in an OLDER year, but does the opposite when the
    company's capex has genuinely been RISING - it pulls the normalised
    base UP toward what the company used to spend, understating today's
    real capex burden and overstating FCF (and the DCF built on it).
    When the latest year's capex is more than 1.5x the average capex,
    the BASE year alone uses the MIDPOINT of (latest capex, average
    capex) instead of the plain average - a partial correction (still
    smooths a little, unlike using latest capex raw) rather than a full
    swing to "capex hasn't fallen since this program ramped up, it's
    still elevated". The historical fcf_series used for the growth CAGR
    is UNCHANGED (still averaged for every year, base year included) -
    this only affects the single BASE value the DCF actually compounds
    from. meta["capex_basis"] ("average" | "midpoint (capex rising)")
    records which basis was actually used, for the app to disclose.

    Step 4 (owner-directed, 30 Sep 2026, KO fix - see _detect_distorted_
    years()'s own docstring for the algorithm): a genuine one-off cash
    distortion (an earn-out payment, a tax settlement) that spans TWO of
    the three years Task 10's own outlier swap compares against can't be
    caught by that check - the distorted years pull the comparison
    median down too. When `income_df` is given AND at least one of the
    last FCF_ONEOFF_WINDOW_YEARS (5) years is flagged distorted, this
    SUPERSEDES the Task-10/rising-capex logic above for this computation:
    the base becomes the median of the CLEAN (non-distorted) years'
    average-capex-basis FCF in that window (>= FCF_ONEOFF_MIN_CLEAN_
    YEARS, 3, required), or, with fewer clean years than that, an
    EBITDA-capex-cash-taxes bridge (_ebitda_bridge_base()) - and the
    growth-CAGR series has the distorted years dropped from it too.
    `income_df` is None for every EXISTING caller (nightly_scan.py's
    lite scan, and a cold-cache Deep Dive view - see resolver_engine.py/
    deep_dive_engine.py's own comments on how it's threaded in) - this
    mechanism simply never fires for them, and Task 10/the rising-capex
    guard behave EXACTLY as before. A ticker with income_df available
    but no distortion detected in the window is likewise completely
    unaffected - only a genuinely one-off-distorted ticker's base
    changes.

    TIGHTENED 2 Oct 2026 (owner decision, 18:15 AEST, incident report
    analysis (a)/(b); REVISED same-day 18:40 AEST once the owner's own
    admin audit showed Step 4 wasn't over-firing broadly - see
    _oneoff_metric_series()'s own docstring for what got reverted): the
    cross-check tier gate that was briefly added here (skip Step 4
    entirely without a genuine EBITDA tier) is GONE - all three
    _oneoff_metric_series() tiers are back in play, exactly as before
    this tightening. What stuck: whenever the mechanism fires and
    substitutes a clean-year base, that base is capped at
    FCF_ONEOFF_UPLIFT_CAP_MULTIPLE (3.0x) times the raw (un-adjusted)
    latest-year figure when that raw figure is positive - a genuine
    one-off shouldn't inflate the base past that; a larger gap points
    to an actual multi-year cycle, not a one-off, and capping prevents
    the substitution from overstating the DCF. The 3.0x multiple itself
    was revised same-day from an initial 1.5x after the admin audit
    showed real one-offs (KO ~2.06x, BALL ~2.4x, INCY ~3.0x) sitting
    well above 1.5x that must NOT be capped. oneoff_meta["fcf_base_
    capped_by_uplift"] records whether the cap actually fired. Task 10's
    own median swap above is untouched.

    Returns (base_fcf, fcf_series_recent_first, source, base_normalized,
    capex_basis, oneoff_meta) where source is one of "ocf-normcapex" |
    "fcf-median" | "info" | "none" (UNCHANGED semantics - still names
    which of the four top-level bases was used, even when oneoff_meta
    below further refines "ocf-normcapex"), base_normalized is True only
    when the Task-10 outlier swap above actually fired (always False for
    every other source, including whenever oneoff_meta's own mechanism
    fired instead), and capex_basis is "average" | "midpoint (capex
    rising)" | None (None for every source other than "ocf-normcapex").
    oneoff_meta = {"fcf_base_source": str, "fcf_distorted_years": list,
    "fcf_base_raw": float|None, "fcf_base_capped_by_uplift": bool} -
    fcf_base_source is "median5_clean" or "ebitda_bridge" when this
    mechanism fired, else the SAME string as `source` above (so a
    caller always has one field to display regardless of path);
    fcf_distorted_years is the list of 0-indexed positions (0 = latest
    year) flagged within the window, empty when the mechanism didn't
    fire; fcf_base_raw is the un-adjusted latest-year figure (ocf[0] +
    base_capex, the same value "base" would have been without this
    mechanism), None when it didn't fire; fcf_base_capped_by_uplift is
    True only when the uplift cap above actually reduced the
    substituted base, always False otherwise.
    """
    info = info or {}

    ocf = _row(cashflow_df, _OCF_LABELS)
    capex = _row(cashflow_df, _CAPEX_LABELS)
    ocf = [v for v in (ocf or []) if v == v]
    capex = [v for v in (capex or []) if v == v]

    if len(ocf) >= 2 and len(capex) >= 1:
        avg_capex = _mean(capex)                 # capex is negative in statements
        series = [o + avg_capex for o in ocf]    # OCF - normalised capex (growth series - UNCHANGED)

        latest_capex = capex[0]
        capex_basis = "average"
        base_capex = avg_capex
        if avg_capex != 0 and abs(latest_capex) > 1.5 * abs(avg_capex):
            base_capex = (latest_capex + avg_capex) / 2.0
            capex_basis = "midpoint (capex rising)"
        base = ocf[0] + base_capex                # latest OCF, base-year capex basis (may differ from series[0])

        # Audit fixes Commit 4 (30 Sep 2026, owner-directed): the Task-10
        # outlier-median swap below is skipped whenever the rising-capex
        # guard just fired. `series` (and therefore `median`) is built
        # ENTIRELY on the AVERAGE capex basis (see the docstring above -
        # "UNCHANGED, still averaged for every year, base year
        # included"), but `base` here can be on the MIDPOINT basis -
        # comparing the two is apples-to-oranges for the base year
        # itself, and swapping `base = median` in that case silently
        # replaces the deliberately midpoint-guarded figure with an
        # average-basis one - undoing the rising-capex guard exactly in
        # the strongest ramps (where midpoint and average diverge the
        # most, making the outlier test MORE likely to fire), while
        # capex_basis kept reporting "midpoint (capex rising)" even
        # though an average-basis number was what actually got used.
        # Skipping the swap here (rather than rebuilding a base-basis
        # median to compare against) keeps capex_basis always honestly
        # describing the number `base` actually holds - see this
        # function's own docstring.
        base_normalized = False
        recent = series[:3]
        # Thin-history median fix (owner-directed, 1 Oct 2026, Commit 3
        # of instruction_dcf_unreliable_pool_step4_nightly.md): with
        # exactly 2 comparison points, `sorted(recent)[len(recent)//2]`
        # (index 1) always picked the LARGER of the two values. `base`
        # IS `recent[0]` here (this branch only runs when capex_basis is
        # "average", the only case where base_capex==avg_capex), so the
        # old code effectively compared base against max(base, other) -
        # zero deviation, and therefore no swap, whenever base ITSELF
        # was the inflated one. An inflated latest year could never be
        # swapped out even though the outlier check exists specifically
        # to catch a distorted base.
        #
        # Fix: with exactly 2 points, compare base against the single
        # OTHER point (`recent[1]`) directly, instead of against
        # whichever of the two happens to be larger. This is two-sided
        # by construction - an inflated base (recent[1] is the lower,
        # correct reference) swaps DOWN to it, and a DEPRESSED base
        # (recent[1] is the higher, correct reference) swaps UP to it -
        # both directions now actually compare against the real other
        # data point rather than against itself. (The instruction's own
        # "use the lower value as the reference" describes the result
        # in the originally-reported inflated-latest shape, where
        # recent[1] - the correct reference - happens to be the lower
        # of the two; a literal unconditional min(recent) would instead
        # make base its own reference whenever base is already the
        # smaller value, permanently zeroing the deviation and making a
        # depressed-latest swap impossible - the opposite of the
        # instruction's own two-sided test requirement, so this reads
        # "the other point" rather than "the smaller of the two" to
        # satisfy both.)
        #
        # 3+ points keep the real median unchanged; fewer than 2 (the
        # `len(recent) >= 2` guard) is unchanged - no swap either way.
        # FCF_OUTLIER_THRESHOLD (40%) and every other condition here are
        # untouched.
        if capex_basis != "midpoint (capex rising)" and len(recent) >= 2:
            median = recent[1] if len(recent) == 2 else sorted(recent)[len(recent) // 2]
            if median != 0 and abs(base - median) / abs(median) > FCF_OUTLIER_THRESHOLD:
                base = median
                base_normalized = True

        # Step 4 (owner-directed, 30 Sep 2026, KO fix; TIGHTENED 2 Oct
        # 2026, see this function's own docstring): the distorted-year
        # mechanism, when it fires, SUPERSEDES everything computed above
        # for this ticker - a two-year-spanning one-off distortion
        # defeats Task 10's narrower 3-year-vs-latest comparison.
        # income_df is None for every existing caller, so this block is
        # a no-op for them. (The tier gate this block originally had
        # here - "skip entirely unless metric_tier is ebitda_addback" -
        # was REVERTED same-day, 18:40 AEST: the owner's own admin audit
        # showed Step 4 wasn't over-firing, so all three _oneoff_metric_
        # series() tiers are back in play, exactly as before this
        # tightening - CSL's own fixture depends on tier 2.)
        oneoff_meta = {
            "fcf_base_source": "ocf-normcapex", "fcf_distorted_years": [],
            "fcf_base_raw": None, "fcf_base_capped_by_uplift": False,
        }
        if income_df is not None:
            metric_series, secondary_series, _metric_tier = _oneoff_metric_series(income_df)
            distorted = _detect_distorted_years(ocf, metric_series, secondary_metric_series=secondary_series)
            window_distorted = distorted[:FCF_ONEOFF_WINDOW_YEARS]
            if any(window_distorted):
                window_series = series[:FCF_ONEOFF_WINDOW_YEARS]
                clean_values = [v for v, d in zip(window_series, window_distorted) if not d]
                distorted_positions = [i for i, d in enumerate(window_distorted) if d]
                oneoff_base = None
                oneoff_source = None
                if len(clean_values) >= FCF_ONEOFF_MIN_CLEAN_YEARS:
                    oneoff_base = sorted(clean_values)[len(clean_values) // 2]
                    oneoff_source = "median5_clean"
                else:
                    bridge = _ebitda_bridge_base(income_df, avg_capex)
                    if bridge is not None:
                        oneoff_base = bridge
                        oneoff_source = "ebitda_bridge"
                if oneoff_source is not None:
                    clean_growth_series = [
                        v for i, v in enumerate(series)
                        if i >= len(distorted) or not distorted[i]
                    ]
                    fcf_base_raw = ocf[0] + base_capex
                    # Safety valve (2 Oct 2026, owner decision, REVISED
                    # same-day 18:40 AEST from an initial 1.5x to 3.0x
                    # after the owner's own admin audit showed KO
                    # (~2.06x), BALL (~2.4x) and INCY (~3.0x) are all
                    # genuine one-offs that must NOT be capped - only a
                    # substitution exceeding 3x the raw latest-year
                    # figure points to an actual multi-year cycle rather
                    # than a one-off, so only THAT gets capped.
                    capped_by_uplift = False
                    if fcf_base_raw is not None and fcf_base_raw > 0 and oneoff_base > FCF_ONEOFF_UPLIFT_CAP_MULTIPLE * fcf_base_raw:
                        oneoff_base = FCF_ONEOFF_UPLIFT_CAP_MULTIPLE * fcf_base_raw
                        capped_by_uplift = True
                    return (
                        oneoff_base, clean_growth_series, "ocf-normcapex", False, "average",
                        {
                            "fcf_base_source": oneoff_source,
                            "fcf_distorted_years": distorted_positions,
                            "fcf_base_raw": fcf_base_raw,
                            "fcf_base_capped_by_uplift": capped_by_uplift,
                        },
                    )

        return base, series, "ocf-normcapex", base_normalized, capex_basis, oneoff_meta

    # Fall back to the reported FCF line. Use the MEDIAN of the last few years
    # as the base so one outlier year doesn't dominate; keep the raw series for
    # the growth estimate.
    fcf = extract_fcf_history(cashflow_df)
    if fcf:
        recent = sorted(fcf[:3])
        base = recent[len(recent) // 2]          # median of up to 3 latest
        return base, fcf, "fcf-median", False, None, {
            "fcf_base_source": "fcf-median", "fcf_distorted_years": [], "fcf_base_raw": None,
            "fcf_base_capped_by_uplift": False,
        }

    info_fcf = info.get("freeCashflow", 0) or 0
    if info_fcf > 0:
        return info_fcf, [], "info", False, None, {
            "fcf_base_source": "info", "fcf_distorted_years": [], "fcf_base_raw": None,
            "fcf_base_capped_by_uplift": False,
        }

    return None, [], "none", False, None, {
        "fcf_base_source": "none", "fcf_distorted_years": [], "fcf_base_raw": None,
        "fcf_base_capped_by_uplift": False,
    }


def _financials_base_and_series(income_df):
    """
    Financials-mode base/series (owner-directed, 2 Oct 2026, VERSION A -
    see financials_classifier.py's own docstring): for a bank/insurer,
    net income substitutes for free cash flow - operating cash flow for
    a financial includes float/deposit flows that aren't shareholder
    cash (KNSL live case: OCF ~US$1.0B vs net income ~US$412M, giving a
    DCF of ~US$930/share against PE Forward/PE Trailing/Rational
    Compounder clustering US$340-410). No capex normalisation applies
    in this mode - net income already sits below D&A and capex is not
    a shareholder-cash deduction for a bank's loan book - so this reuses
    only the Task-10 outlier-median swap and Step 4 distorted-year
    mechanism from _ocf_based_base_and_series() above, fed the raw net-
    income series instead of OCF-minus-capex. The EBITDA bridge
    (_ebitda_bridge_base()) is NOT reachable here - it needs avg_capex,
    which has no meaning for a financial - so a distorted window with
    fewer than FCF_ONEOFF_MIN_CLEAN_YEARS clean years simply falls
    through to the Task-10-level base instead of substituting a bridge
    value.

    Returns None when fewer than 2 usable (non-NaN) POSITIVE net-income
    points exist - the caller (normalized_base_and_series() below) then
    falls back to the standard OCF path, tagged source=
    "ocf_fallback_financials". A negative latest net income is left to
    the existing negative-FCF handling (fcf_reason) in dcf_intrinsic_
    value() unchanged - it is not itself a reason to return None here as
    long as >= 2 OTHER positive points exist for the base/series.

    Returns the same 6-tuple shape as _ocf_based_base_and_series():
    (base_fcf, fcf_series_recent_first, source, base_normalized,
    capex_basis, oneoff_meta) - source is always "net_income_financials"
    here (the wrapper renames it to "ocf_fallback_financials" on
    fallback, never this function), capex_basis is always None (capex
    doesn't apply), and oneoff_meta carries "is_financials_mode": True
    so a caller always has one field to detect financials mode
    regardless of which path within it fired.
    """
    ni = _row(income_df, _NET_INCOME_LABELS)
    ni = [v for v in (ni or []) if v == v]
    if len([v for v in ni if v > 0]) < 2:
        return None

    series = list(ni)   # net income, most-recent-first - no capex normalisation in this mode
    base = series[0]

    base_normalized = False
    recent = series[:3]
    if len(recent) >= 2:
        median = recent[1] if len(recent) == 2 else sorted(recent)[len(recent) // 2]
        if median != 0 and abs(base - median) / abs(median) > FCF_OUTLIER_THRESHOLD:
            base = median
            base_normalized = True

    oneoff_meta = {
        "fcf_base_source": "net_income_financials", "fcf_distorted_years": [],
        "fcf_base_raw": None, "fcf_base_capped_by_uplift": False,
        "is_financials_mode": True,
    }

    metric_series, secondary_series, _metric_tier = _oneoff_metric_series(income_df)
    distorted = _detect_distorted_years(series, metric_series, secondary_metric_series=secondary_series)
    window_distorted = distorted[:FCF_ONEOFF_WINDOW_YEARS]
    if any(window_distorted):
        window_series = series[:FCF_ONEOFF_WINDOW_YEARS]
        clean_values = [v for v, d in zip(window_series, window_distorted) if not d]
        distorted_positions = [i for i, d in enumerate(window_distorted) if d]
        if len(clean_values) >= FCF_ONEOFF_MIN_CLEAN_YEARS:
            oneoff_base = sorted(clean_values)[len(clean_values) // 2]
            clean_growth_series = [
                v for i, v in enumerate(series)
                if i >= len(distorted) or not distorted[i]
            ]
            fcf_base_raw = series[0]
            capped_by_uplift = False
            if fcf_base_raw > 0 and oneoff_base > FCF_ONEOFF_UPLIFT_CAP_MULTIPLE * fcf_base_raw:
                oneoff_base = FCF_ONEOFF_UPLIFT_CAP_MULTIPLE * fcf_base_raw
                capped_by_uplift = True
            return (
                oneoff_base, clean_growth_series, "net_income_financials", False, None,
                {
                    "fcf_base_source": "median5_clean",
                    "fcf_distorted_years": distorted_positions,
                    "fcf_base_raw": fcf_base_raw,
                    "fcf_base_capped_by_uplift": capped_by_uplift,
                    "is_financials_mode": True,
                },
            )
        # Fewer than FCF_ONEOFF_MIN_CLEAN_YEARS clean years and no EBITDA
        # bridge available in this mode (see docstring) - fall through
        # and use the Task-10-level base/series computed above as-is.

    return base, series, "net_income_financials", base_normalized, None, oneoff_meta


def normalized_base_and_series(cashflow_df, info=None, income_df=None):
    """
    Dispatch to the financials-mode (net income) or standard (OCF)
    base/series computation, per financials_classifier.is_financials()
    - the single shared classifier moat_engine.py also uses, so the two
    valuation paths can never put the same ticker on different sides of
    the financials-mode line (owner-directed, 2 Oct 2026, VERSION A).

    Returns the identical 6-tuple shape either way: (base_fcf,
    fcf_series_recent_first, source, base_normalized, capex_basis,
    oneoff_meta) - see _ocf_based_base_and_series()'s and
    _financials_base_and_series()'s own docstrings for what each field
    means on their respective paths.

    Fallback: a financials-mode ticker with fewer than 2 usable positive
    net-income points (including when income_df itself is None - every
    EXISTING caller before this task) falls back to the standard OCF
    path unchanged, but tagged source="ocf_fallback_financials" with
    oneoff_meta["is_financials_mode"]=True, so a caller can still tell
    this was a financial whose DCF ended up OCF-based via the fallback,
    not the net-income path.
    """
    info = info or {}
    if financials_classifier.is_financials(info):
        result = _financials_base_and_series(income_df) if income_df is not None else None
        if result is not None:
            return result
        base, series, _source, base_normalized, capex_basis, oneoff_meta = _ocf_based_base_and_series(
            cashflow_df, info=info, income_df=income_df
        )
        oneoff_meta = dict(oneoff_meta)
        oneoff_meta["is_financials_mode"] = True
        return base, series, "ocf_fallback_financials", base_normalized, capex_basis, oneoff_meta

    return _ocf_based_base_and_series(cashflow_df, info=info, income_df=income_df)


def growth_from_history(fcf_history, dates=None):
    """
    Compound annual growth rate of free cash flow across the available years.

    fcf_history is most-recent-first (yfinance order). Needs at least two
    positive endpoints to be meaningful. Returns a decimal growth rate, or
    None if history can't support an estimate.

    `dates` (B2.4, 27 Sep 2026, owner-directed, CONFIRMED BUG - added
    here but NOT YET wired into any live caller, see this function's
    own call site in dcf_intrinsic_value() for why): optional, same
    length and order as fcf_history (most-recent-first). When given,
    the elapsed-years denominator is the REAL calendar span between
    the oldest and newest dates, not the column COUNT - a gap in the
    statement history (a missing year) would otherwise understate
    elapsed time and overstate the CAGR (same fix as auto_compounder_
    engine._equity_growth_rate(), which has the identical root cause
    for per-share equity growth). Omitted (the default, and every
    EXISTING caller today), this keeps the exact prior column-count
    behavior - a deliberate backward-compatible default, not an
    oversight: dcf_intrinsic_value()'s own fcf_series can be built via
    more than one fallback path with differing column-to-value
    correspondence (see normalized_base_and_series()'s own "ocf-
    normcapex" vs "fcf-median" sources), and wiring real dates through
    every one of those paths safely is deferred as its own follow-up
    rather than risked here on the live discount-rate-adjacent growth
    calculation without production data to verify it against.
    """
    if not fcf_history or len(fcf_history) < 2:
        return None

    # Reorder oldest -> newest for a clean CAGR.
    series = list(reversed(fcf_history))

    # Growth-rewrite fix (owner-directed, 28 Sep 2026): the two endpoints
    # are each the AVERAGE of the two oldest / two newest data points
    # (series[:2] / series[-2:]), not the single oldest/newest value -
    # smooths a one-off spike or dip at either edge of the window from
    # single-handedly setting the whole CAGR. The `years` denominator
    # changes to match: since each endpoint is now itself a 2-point
    # window rather than a single year, the elapsed time is measured
    # between the two windows' own "centers" - n-2 index-periods for n
    # data points (e.g. 4 points: points 1&2 center on an effective
    # "year 1.5", points 3&4 on "year 3.5", 2 years apart) - not n-1 (the
    # full raw index span, which was correct for the single-point
    # endpoints this formula used before today but overstates the gap
    # between two SMOOTHED endpoints). Floored at 1: n==2 has
    # oldest_avg==newest_avg regardless (both windows are the identical
    # 2 points), so the ratio is always exactly 1.0 (0% CAGR - correctly
    # reads as "not enough distinct years to measure a trend" rather
    # than extrapolating from two noisy raw endpoints, what the OLD
    # (pre-28-Sep-2026) first-vs-last formula did) whatever years is;
    # n==3's single index-period is the smallest genuine gap.
    oldest_avg = _mean(series[:2])
    newest_avg = _mean(series[-2:])

    if dates and len(dates) == len(fcf_history):
        # Same window-center reasoning as the value endpoints above,
        # applied to real calendar dates instead of index counts - the
        # midpoint DATE of the oldest pair to the midpoint DATE of the
        # newest pair, so a regularly-spaced series' real-date years
        # still closely agrees with the no-dates (index-count) years
        # just above, and a genuine gap still changes the answer.
        dates_asc = list(reversed(dates))
        oldest_mid = dates_asc[0] + (dates_asc[1] - dates_asc[0]) / 2
        newest_mid = dates_asc[-2] + (dates_asc[-1] - dates_asc[-2]) / 2
        years = ((newest_mid - oldest_mid).days / 365.25) if (oldest_mid and newest_mid) else None
        if not years or years <= 0:
            years = max(len(series) - 2, 1)
    else:
        years = max(len(series) - 2, 1)

    # CAGR only makes sense between two positive endpoints.
    if oldest_avg is None or newest_avg is None or oldest_avg <= 0 or newest_avg <= 0:
        return None

    try:
        cagr = (newest_avg / oldest_avg) ** (1.0 / years) - 1.0
    except Exception:
        return None
    return cagr


def estimate_growth(info, fcf_series=None, analyst_growth=None, ceiling=None,
                     end_rate=None, currency=None, yahoo_estimate_status=None,
                     extra_out=None):
    """
    Estimate a stage-1 growth rate and report where it came from.

    Growth-never-zero rewrite (owner-directed, 30 Sep 2026, replacing the
    28 Sep growth-rewrite's own priority order): growth NEVER resolves to
    a flat 0% any more - see GROWTH_FLOOR's own comment for why that
    floor is now provably unreachable. `currency` is accepted for
    interface symmetry with the DCF's own end_rate computation (growth_
    end_rate_for(info, currency)) but isn't read directly here - it's
    the CALLER's job to pass the already-computed `end_rate` (see below),
    never this function's.

    `end_rate` (should always be passed by a real caller): the SAME
    floored market-cap-tiered growth-path end rate the DCF's own stage-1
    fade loop uses - max(growth_end_rate_for(info, currency),
    perpetual_rate) - computed ONCE by dcf_intrinsic_value() before
    calling this function, and reused in both places so the two can
    never diverge. Used here as (a) the volatile-history/reported-growth
    caps' floor (max(end_rate, ceiling * fraction) - never cap BELOW a
    stock's own fade target) and (b) the priority-4 fallback value
    itself, which is why that fallback is never 0%. Falls back to
    DEFAULT_PERPETUAL_RATE (always > 0) if omitted, for any caller that
    hasn't been updated to pass it - no real call site in this codebase
    should hit that fallback.

    `ceiling` overrides the module-level GROWTH_CEIL - pass the result of
    growth_ceiling_for(info, currency) to apply the market-cap-tiered
    ceiling (defaults to the flat GROWTH_CEIL if not given, e.g. for
    callers/tests that don't need size-awareness).

    The returned `governor` tells you what actually determined the FINAL
    (post-cap) number:
        "Yahoo"           - the analyst estimate was used, uncapped.
        "History"         - a clean historical FCF CAGR was used,
                             uncapped.
        "HistoryVolatile" - a historical FCF CAGR was used despite high
                             year-to-year volatility, capped at
                             max(end_rate, ceiling * HISTORY_VOLATILE_
                             CAP_FRACTION).
        "Info"            - a single-period reported revenueGrowth/
                             earningsGrowth figure, capped at
                             max(end_rate, ceiling * REPORTED_GROWTH_CAP_
                             FRACTION) regardless of tier.
        "Default"         - no usable signal anywhere - resolved to
                             end_rate itself (flagged red in the UI via
                             growth_default/defaulted), never 0%.
        "Cap"             - a Yahoo or clean-history signal that would
                             otherwise have been used was ABOVE the
                             market-cap tier ceiling, so the ceiling
                             itself is what's actually driving the
                             number (the volatile-history/info/default
                             paths already name their own cap explicitly
                             via the governors above, so this label is
                             reserved for the Yahoo/clean-history paths).

        (Step 1d, 30 Sep 2026, briefly added a separate "Cap1y" governor
        and a tighter fractional cap for the next-year-analyst tier -
        REMOVED the same day, 20:55 AEST, owner decision: the market-cap
        tier ceiling is the safety limit and was built for exactly this;
        a second per-source cap under it adds a rule without adding
        safety. The "analyst_1y" source below is now governed by the
        plain ceiling alone, same as a genuine LTG value - see priority
        1's own comment.)

        (1 Oct 2026 20:37 AEST, owner decision, KNSL/Kinsale Capital live
        case: the "analyst_1y" tier above was itself found to be the
        wrong number alone - see priority 1's "ok_1y" paragraph below.
        Two NEW governor strings replace "Yahoo" for this one tier only:
        "next-year consensus blended with history (history capped at
        ceiling)" when clean history is available, "next-year
        consensus, floored at end rate" when it isn't - a genuine "ok"
        LTG value is NOT affected, it still reports "Yahoo"/"Cap".)

    Priority, first usable match wins - see this module's own docstring
    for the same list at a glance:
        1. Yahoo's analyst_growth, if it's a real number AND > 0 (a
           non-positive Yahoo estimate is real DATA, not noise, but
           it's not a growth signal this stage-1 loop can compound on -
           falls through to the next source instead).

           Step 1d (owner-directed, 30 Sep 2026, from the Dow 30 rescan's
           own "Yahoo coverage - Yahoo LTG 0, Yahoo 1y 28" line - Yahoo's
           5-year figure is absent for essentially every Dow name, so
           the +1y/0y fallback (Step 1c) now supplies most of this
           priority's hits): when `yahoo_estimate_status` is "ok_1y" -
           a ONE-YEAR consensus (capm_engine.get_growth_estimates_5y()'s
           own next-fiscal-year/current-fiscal-year fallback, not a
           genuine 5-year figure) - the source is tagged "analyst_1y"
           (distinct from plain "analyst" - see meta["growth_source"]'s
           own docstring, so a Deep Dive/Fair Value label can still say
           "next year" rather than implying a 5-year figure).

           Owner decision, 1 Oct 2026 20:37 AEST (KNSL/Kinsale Capital
           live case): a bare one-year consensus is cycle-dominated
           (KNSL 2026 ~= flat on a strong 2025) - the wrong kind of
           number to use alone as a 10-year DCF base rate, so it is now
           BLENDED with clean FCF history when enough exists (>=
           MIN_HISTORY_POINTS_FOR_TREND points, the same gate priority 2
           below uses): history is capped at the tier ceiling BEFORE
           averaging with the consensus, then the blend itself is
           floored at end_rate and capped at the tier ceiling - source
           becomes "analyst_1y_blend" in this case. With no usable
           history, the consensus is used alone (source stays
           "analyst_1y") but floored at end_rate so an analyst-sourced
           base never starts the fade already running uphill - see
           meta["growth_1y_consensus"]/meta["growth_history_capped"]
           for the two raw inputs behind either path (the second is
           None when no history was blended in). A genuine "ok" LTG
           value is governed EXACTLY like before this decision - the
           plain market-cap tier ceiling only, no floor, no blend.
           (Revised 30 Sep 2026, 20:55 AEST, owner decision - see this
           function's own docstring intro above: a tighter per-source
           cap briefly existed here the same day and was removed - the
           tier ceiling is the one safety limit for every analyst-
           sourced figure, next-year or 5-year alike. That removal
           stands; the 1 Oct change is a separate, additional fix.)
        2. Historical FCF CAGR (growth_from_history(fcf_series)), if
           it's > 0 AND fcf_series has at least MIN_HISTORY_POINTS_FOR_
           TREND points (fewer is "no history", not a real 0%/degenerate
           trend - see that constant's own comment). A CLEAN signal
           (coefficient of variation <= 0.60) gets the plain tier
           ceiling; a volatile one is still USED, just capped tighter.
        3. A single-period reported growth figure - info["revenueGrowth"]
           tried first, then info["earningsGrowth"], first value > 0
           wins.
        4. Nothing usable above -> end_rate itself (always > 0).

    Returns (growth_rate, source, governor, raw_rate) - raw_rate is the
    PRE-CAP figure (before any tier/fraction clamp was applied), for
    display next to the number the model actually compounds from (see
    this module's own meta["growth_raw"]). growth_rate is clamped to
    [GROWTH_FLOOR, cap] - GROWTH_FLOOR is a defensive floor only; see its
    own comment for why it should be unreachable after this rewrite.

    No-uphill-fade invariant (1 Oct 2026): for the "analyst_1y"/
    "analyst_1y_blend" paths, growth_rate is always >= end_rate by
    construction (each explicitly floors at end_rate before returning).
    The "analyst"/"history"/"history_volatile"/"info"/"default" paths do
    NOT carry this same floor - a genuine signal below end_rate on any
    of those paths can still produce a raw growth_rate BELOW the
    end_rate this function was given. This function itself does not
    raise that raw growth_rate (unlike analyst_1y/analyst_1y_blend,
    deliberately - see Commit 4 of instruction_dcf_unreliable_pool_
    step4_nightly.md: lowering the base to chase a tier end rate would
    be the wrong fix). Instead, the CALLER (dcf_intrinsic_value(), right
    after calling this function) lowers meta["growth_end_rate_used"]
    itself to min(end_rate, growth_rate) - a PLAIN min(), not also
    re-floored at perpetual_rate, even though end_rate's own original
    computation already is - for exactly these paths when growth_rate <
    end_rate, so the invariant growth_used >= growth_end_rate_used holds
    UNCONDITIONALLY on every auto path at the REPORTED/meta level (a
    plain min() against growth_rate can never exceed growth_rate; a
    second re-floor against perpetual_rate could, whenever growth_rate
    itself falls below perpetual_rate) - see dcf_intrinsic_value()'s own
    comment at that call site for the full reasoning (the stage-1 fade
    loop already goes flat rather than fading uphill when growth_rate <=
    end_rate; this change is about correctly reporting/describing that
    flat path, not altering the DCF math itself).

    extra_out (1 Oct 2026, optional, mutated in place like nightly_scan.
    py's own growth_summary_out param): when given, the "analyst_1y"/
    "analyst_1y_blend" paths populate extra_out["growth_1y_consensus"]
    (the raw next-year consensus) and extra_out["growth_history_capped"]
    (the ceiling-capped history CAGR actually blended in, or None when
    no history was used) - every other path leaves `extra_out` alone.
    """
    info = info or {}
    ceiling = GROWTH_CEIL if ceiling is None else ceiling
    if end_rate is None:
        # Defensive fallback only - every real call site in this codebase
        # computes end_rate once (dcf_intrinsic_value()'s own stage-1
        # fade target) and passes it in. DEFAULT_PERPETUAL_RATE is always
        # > 0, so priority 4 below still never resolves to 0 even here.
        end_rate = DEFAULT_PERPETUAL_RATE

    def _finalize(raw_rate, source, natural_governor, cap=None):
        _cap = ceiling if cap is None else cap
        governor = "Cap" if raw_rate > _cap else natural_governor
        result = max(GROWTH_FLOOR, min(raw_rate, _cap))
        if result <= 0:
            # Tripwire, not an expected path - see GROWTH_FLOOR's own
            # comment. Every priority branch below requires or produces
            # a strictly positive raw_rate/cap, so this should never
            # actually fire; logged (not raised) so a future regression
            # shows up in the logs rather than silently reintroducing a
            # flat 0% growth rate.
            _growth_logger.warning(
                "estimate_growth: result <= 0 (%.4f) after clamping - source=%s, "
                "raw_rate=%.4f, cap=%.4f - should be unreachable after the "
                "growth-never-zero rewrite (30 Sep 2026)", result, source, raw_rate, _cap,
            )
        return result, source, governor, raw_rate

    if analyst_growth is not None and analyst_growth > 0:
        if yahoo_estimate_status == "ok_1y":
            # Owner decision, 1 Oct 2026 20:37 AEST (KNSL/Kinsale Capital
            # live case): a BARE next-year consensus is cycle-dominated
            # (KNSL 2026 ~= flat on a strong 2025) and is the wrong kind
            # of number to use alone as a 10-year DCF base rate - it was
            # previously governed exactly like a genuine "ok" LTG value
            # (see the removed Step 1d comment this replaces), which let
            # one unusual year set the whole growth path. Now blended
            # with clean FCF history when there's enough of it (the SAME
            # MIN_HISTORY_POINTS_FOR_TREND gate priority 2 below uses),
            # capping the history side at the tier ceiling BEFORE
            # averaging - a 35% history on a 20%-ceiling name
            # contributes 20, not 35, to the blend. With no usable
            # history, the consensus is used alone but floored at
            # end_rate - an analyst-sourced base should never start the
            # fade already running uphill. Genuine "ok" LTG is untouched
            # - it still falls to the plain _finalize() call below,
            # ceiling-only, no floor, no blend.
            h = growth_from_history(fcf_series)
            has_enough_history_1y = (
                fcf_series is not None and len(fcf_series) >= MIN_HISTORY_POINTS_FOR_TREND
            )
            if h is not None and h > 0 and has_enough_history_1y:
                history_capped = min(h, ceiling)
                base = max(end_rate, min((analyst_growth + history_capped) / 2.0, ceiling))
                if extra_out is not None:
                    extra_out["growth_1y_consensus"] = analyst_growth
                    extra_out["growth_history_capped"] = history_capped
                return _finalize(
                    base, "analyst_1y_blend",
                    "next-year consensus blended with history (history capped at ceiling)",
                )
            base = max(analyst_growth, end_rate)
            if extra_out is not None:
                extra_out["growth_1y_consensus"] = analyst_growth
                extra_out["growth_history_capped"] = None
            return _finalize(base, "analyst_1y", "next-year consensus, floored at end rate")
        return _finalize(analyst_growth, "analyst", "Yahoo")

    g = growth_from_history(fcf_series)
    has_enough_history = fcf_series is not None and len(fcf_series) >= MIN_HISTORY_POINTS_FOR_TREND
    if g is not None and g > 0 and has_enough_history:
        if _coeff_of_variation(fcf_series) <= 0.60:
            return _finalize(g, "history", "History")
        return _finalize(
            g, "history_volatile", "HistoryVolatile",
            cap=max(end_rate, ceiling * HISTORY_VOLATILE_CAP_FRACTION),
        )

    # Reported growth - revenueGrowth tried first, then earningsGrowth
    # (SWAPPED from the 28 Sep TOYO fix's earningsGrowth-first order,
    # owner-directed 30 Sep 2026): revenue is the less easily distorted
    # of the two single-period figures (earnings can swing on a one-off
    # item with no revenue change at all), so it's preferred when both
    # are present. See REPORTED_GROWTH_CAP_FRACTION's own comment for
    # why this is capped tier-relatively rather than a flat number.
    for key in ("revenueGrowth", "earningsGrowth"):
        val = info.get(key)
        if val is not None and val > 0:
            return _finalize(
                val, "info", "Info",
                cap=max(end_rate, ceiling * REPORTED_GROWTH_CAP_FRACTION),
            )

    # Nothing usable anywhere - the market-cap-tiered growth-path end
    # rate itself, always > 0 (see growth_end_rate_for()/PERPETUAL_
    # GROWTH_BY_CCY, both strictly positive) - never DEFAULT_GROWTH's old
    # flat 5%, which had no relationship to this stock's own tier/
    # currency. cap=end_rate pins the result to exactly end_rate
    # regardless of the tier ceiling's own value, so this branch can
    # never be mislabeled "Cap" by _finalize().
    return _finalize(end_rate, "default", "Default", cap=end_rate)


# DCF fix: static FX fallback, used only when a live rate can't be fetched -
# approximate, occasionally-updated, same spirit as every other fallback
# constant in this module/app (RISK_FREE_FALLBACK, _SP500_STATIC_FALLBACK,
# etc.). Flagged via meta["fx_fallback"] when actually used, same as any
# other assumption on this site.
_FX_STATIC_FALLBACK = {
    ("USD", "AUD"): 1.52,
    ("AUD", "USD"): 0.66,
}

# Per-process cache so one page render (which can call fx_rate for several
# tickers sharing the same currency pair) fetches each live rate at most
# once.
#
# Audit fix 2.4: this used to have no TTL at all, despite the comment
# above claiming it only needs to live "for the duration of one run" - but
# this IS a long-running server process (Streamlit on Railway), not a
# script, so the first USD/AUD (etc.) rate fetched after a deploy used to
# stay frozen for that whole deployment's uptime, drifting further from
# the real rate the longer the process ran. Now stores (result, fetched_at)
# and expires after _FX_CACHE_TTL_SECONDS, the same kind of TTL every
# other live feed in this app uses (e.g. capm_engine.get_risk_free_rate's
# 3h TTL - FX moves faster than a bond yield, so this uses a shorter one).
_fx_cache = {}
_FX_CACHE_TTL_SECONDS = 1800


def fx_rate(from_ccy, to_ccy):
    """
    Exchange rate to convert an amount FROM from_ccy INTO to_ccy (multiply
    by this). Returns (rate, source) where source is "live" or "fallback".

    Same currency in both slots is always (1.0, "live") - not really a live
    fetch, but not an assumption either, so it's never flagged red.
    """
    from_ccy = (from_ccy or "").upper()
    to_ccy = (to_ccy or "").upper()
    if not from_ccy or not to_ccy:
        return 1.0, "fallback"
    if from_ccy == to_ccy:
        return 1.0, "live"

    cache_key = (from_ccy, to_ccy)
    cached = _fx_cache.get(cache_key)
    if cached is not None:
        result, fetched_at = cached
        if time.time() - fetched_at < _FX_CACHE_TTL_SECONDS:
            return result

    result = None
    try:
        hist = yf.Ticker(f"{from_ccy}{to_ccy}=X").history(period="5d")
        if hist is not None and not hist.empty:
            rate = float(hist["Close"].iloc[-1])
            if rate == rate and rate > 0:   # not NaN
                result = (rate, "live")
    except Exception:
        result = None

    if result is None:
        if cache_key in _FX_STATIC_FALLBACK:
            result = (_FX_STATIC_FALLBACK[cache_key], "fallback")
        elif (to_ccy, from_ccy) in _FX_STATIC_FALLBACK:
            result = (1.0 / _FX_STATIC_FALLBACK[(to_ccy, from_ccy)], "fallback")
        else:
            # Unknown pair and no live rate - a 1.0 no-op is the least-bad
            # option (leaves the original unit-mismatch bug for THIS pair
            # only, rather than inventing a number with no basis at all);
            # still flagged as a fallback so it's visibly not a real rate.
            result = (1.0, "fallback")

    _fx_cache[cache_key] = (result, time.time())
    return result


def dcf_intrinsic_value(
    ticker,
    info=None,
    cashflow_df=None,
    currency=None,
    discount_rate=None,
    perpetual_rate=None,
    growth_rate=None,
    growth_years=DEFAULT_GROWTH_YEARS,
    manual_fcf=None,
    diluted_shares_override=None,
    income_df=None,
):
    """
    Returns (intrinsic_value_per_share, growth_rate_used, meta).

    income_df (Step 4, 30 Sep 2026, owner-directed, KO fix): optional
    income statement, passed straight through to normalized_base_and_
    series()'s own distorted-year cross-check - see that function's own
    docstring. None (the default, and every caller that predates this)
    means that mechanism simply never fires - identical behaviour to
    before it existed. deep_dive_engine.py's Deep Dive path opportunis-
    tically supplies it via fundamentals_data.peek_cached_bundle()
    (cache-only, zero added fetch cost) when the compounder-page
    fundamentals bundle happens to already be warm for this ticker;
    nightly_scan.py's lite scan never passes it at all (no new fetch
    added to the bulk nightly path - see that module's own analyze_
    ticker_lite() docstring for the established cost-avoidance
    philosophy this follows).

    intrinsic_value is 0 when FCF or shares are unavailable/non-positive, so
    callers can fall back to another method.

    discount_rate / perpetual_rate / growth_rate each default to None, which
    means "auto-calculate" (CAPM / currency-based / analyst-or-history). Pass
    an explicit number for any of the three (from the app's Valuation & FCF
    panel, global or per-stock) and it's used as-is instead.

    diluted_shares_override (audit A3, 27 Sep 2026; A3b, 27 Sep 2026):
    for a dual-class company, info["sharesOutstanding"] can reflect
    only ONE listed class (see share_class_engine.whole_company_
    shares()'s own docstring for the HEI/HEICO root cause this fixes) -
    understating per-share value for the affected names. None (the
    default) means "resolve it the normal way" - see below, NOT "skip
    the correction": A3b moved the actual resolution into share_class_
    engine.whole_company_shares(info, ticker=ticker), called right
    below whenever this parameter isn't explicitly given, which tries
    (a) info["impliedSharesOutstanding"] (already on the SAME `info`
    every caller already has - zero new network calls) then (b), only
    for a ticker on that module's own small explicit multi-class list,
    a cached targeted fetch (a handful of calls a night, never a fetch
    added to the general per-ticker path). A caller that already has a
    full fundamentals bundle on hand (auto_compounder_engine._run_dcf())
    passes the already-corrected share count explicitly instead, which
    always takes priority over the auto-resolution below.

    meta = {
        "growth_source":    "analyst" | "history" | "analyst+history" | "info" | "default" | "manual",
        "growth_governor":  "Yahoo" | "History" | "Info" | "Default" | "Manual" | "Cap" | None,
        "growth_ceiling_used": float,  # the market-cap-tiered ceiling actually applied
        "fcf_source":       "history" | "info" | "manual" | "none",
        "discount_source":  "tiered" | "tiered-default" | "manual" | "fallback",
        "discount_tier_label": str | None,  # A6: e.g. "mid-cap (US$10B-50B)" - only set on the "tiered"/"tiered-default" auto path
        "perpetual_source": "currency" | "manual" | "fallback",
        "growth_default": bool,   # True when the average growth had to be used
        "defaulted":      bool,   # True if any core input was an assumption
        "fcf_base_normalized": bool,  # Task 10: True if an outlier reporting
                                       # year's base was swapped for the
                                       # 3-year median (see FCF_OUTLIER_THRESHOLD)
        "fcf_base_raw":   float | None,  # the un-swapped latest-year value, when normalized
                                       # (or, Step 4: the un-adjusted latest-year figure,
                                       # when fcf_base_source is "median5_clean"/"ebitda_bridge")
        "fcf_base_used":  float | None,  # the median actually used, when normalized
        "fcf_base_source": str,  # Step 4 (30 Sep 2026, KO fix): "median5_clean" |
                                       # "ebitda_bridge" | the plain fcf_source value when
                                       # this mechanism didn't fire - see normalized_base_
                                       # and_series()'s own docstring
        "fcf_distorted_years": list,  # Step 4: 0-indexed positions (0=latest) flagged as a
                                       # one-off cash distortion within the 5-year window;
                                       # empty when the mechanism didn't fire
        "fcf_base_raw_per_share": float | None,  # Step 4: fcf_base_raw / shares, only
                                       # set alongside fcf_base_raw (see above)
        "fcf_used":       float | None,  # the actual base FCF (listing currency, post-FX) fed into the model
        "fcf_per_share_used": float | None,  # fcf_used / shares - the number stage 1 compounds from
        "discount_floored": bool,  # True if capm_engine floored the market-cap-tier rate itself at MIN_DISCOUNT_RATE
        "fx_converted":   str | None,  # DCF fix: "USD->AUD" etc. when financials/listing currency differ
        "fx_rate_used":   float | None,  # the rate actually applied
        "fx_fallback":    bool,   # True if fx_rate() had to use the static fallback table
        "growth_path":    list[float] | None,  # growth-path option E (28 Sep 2026, owner-
                                       # approved): the growth_years yearly rates
                                       # actually used in stage 1 - flat at growth_rate
                                       # for years 1-5, fading linearly to
                                       # growth_end_rate_used over years 6-growth_years
                                       # (never above growth_rate - flat throughout if
                                       # growth_rate is already <= the end rate) - see
                                       # the stage-1 loop's own comment below.
        "growth_end_rate_used": float | None,  # the market-cap-tiered end rate the fade
                                       # targets, floored at perpetual_rate_used (a
                                       # stock never fades below its own terminal rate)
        "yahoo_estimate_status": "ok" | "ok_1y" | "no_coverage" | "non_positive" | "fetch_failed" | None,
                                       # only set on the auto (growth_rate=None) path -
                                       # see capm_engine.get_growth_estimates_5y()'s own
                                       # docstring ("non_positive" added 30 Sep 2026 -
                                       # a real Yahoo value was found but was <=0, not
                                       # usable as a growth signal). None on the
                                       # manual/Cap path (the Yahoo lookup is never
                                       # attempted when a caller supplies growth_rate
                                       # explicitly).
        "capex_basis": "average" | "midpoint (capex rising)" | None,  # rising-
                                       # capex guard (28 Sep 2026, owner-directed) -
                                       # only set on the "ocf-normcapex" fcf_source
                                       # path, see normalized_base_and_series()'s
                                       # own docstring. None for every other
                                       # fcf_source (manual/fcf-median/info/none),
                                       # which have no per-year capex figure to
                                       # compare against.
        "fcf_reason": "negative_normalised_fcf" | "negative_fcf" | None,  # growth-
                                       # never-zero rewrite (30 Sep 2026, owner-
                                       # directed) - "negative_normalised_fcf" when
                                       # normalized_base_and_series() found a base
                                       # but it was <=0 (whether or not the info
                                       # ["freeCashflow"] fallback then rescued it);
                                       # "negative_fcf" when the DCF was actually
                                       # abandoned (fcf <= 0 at the final check) -
                                       # see those two call sites' own comments.
        "growth_raw": float | None,  # growth-never-zero rewrite (30 Sep 2026) - the
                                       # PRE-CAP growth figure estimate_growth() found,
                                       # before any tier/fraction clamp - see that
                                       # function's own "raw_rate" return value. Only
                                       # set on the auto (growth_rate=None) path.
        "share_count_flagged": bool,  # audit fixes Commit 4 (30 Sep 2026) - True when
                                       # share_class_engine.whole_company_shares() applied
                                       # a dual-class override (shares above is NOT plain
                                       # info["sharesOutstanding"]). Always False when
                                       # diluted_shares_override was passed explicitly.
        "share_count_source": str | None,  # "bundle" | "implied" | "filed" | None -
                                       # see whole_company_shares()'s own docstring.
        "share_count_note": str | None,  # set only when a dual-class candidate was
                                       # FOUND but REJECTED (ratio above the sanity
                                       # ceiling, or an uncorroborated "implied" figure) -
                                       # None when no candidate existed, and None when
                                       # one was accepted (share_count_flagged=True is
                                       # the accept signal, this is the reject reason).
        "market_cap_missing": bool,  # audit fixes Commit 4 (30 Sep 2026) - True when
                                       # info["marketCap"] was missing/non-positive, so
                                       # the market-cap-tiered discount rate silently
                                       # fell through to the smallest (micro-cap)
                                       # tier's premium - also forces defaulted=True.
        "is_financials_mode": bool,  # financials mode (2 Oct 2026, owner-directed,
                                       # VERSION A) - True whenever normalized_base_
                                       # and_series() put this ticker on the net-
                                       # income path OR its ocf_fallback_financials
                                       # fallback (financials_classifier.is_financials()
                                       # said yes either way) - False for every
                                       # standard OCF ticker. Display-only flag; see
                                       # fcf_source for which of the two it actually was.
    }
    """
    meta = {
        "growth_source": None,
        "growth_governor": None,
        "growth_ceiling_used": None,
        "fcf_source": "none",
        "discount_source": None,
        "discount_tier_label": None,
        "perpetual_source": None,
        "growth_default": False,
        "defaulted": False,
        "discount_rate_used": None,
        "perpetual_rate_used": None,
        "growth_end_rate_used": None,
        # Task 10: set only when normalized_base_and_series() swapped the
        # latest reporting year's base for the 3-year median because it was
        # an outlier (see FCF_OUTLIER_THRESHOLD) - never set for a manual
        # FCF override, and never set by the "fcf-median"/"info"/"none"
        # sources (those either already are a median, or aren't a
        # single-year figure at all).
        "fcf_base_normalized": False,
        "fcf_base_raw": None,
        "fcf_base_used": None,
        # Step 4 uplift safety valve (2 Oct 2026, owner decision) - see
        # normalized_base_and_series()'s own docstring.
        "fcf_base_capped_by_uplift": False,
        "fcf_used": None,
        "fcf_per_share_used": None,
        "discount_floored": False,
        "fx_converted": None,
        "fx_rate_used": None,
        "fx_fallback": False,
        "growth_path": None,
        "yahoo_estimate_status": None,
        "capex_basis": None,
        # Growth-never-zero rewrite (30 Sep 2026, owner-directed):
        # "negative_normalised_fcf" | "negative_fcf" | None - see the two
        # call sites below for the exact distinction (a normalised base
        # that came back <=0 but got rescued by the info["freeCashflow"]
        # fallback, vs. the DCF actually being abandoned because nothing
        # usable was ever found). Display-only - never affects the DCF
        # math itself.
        "fcf_reason": None,
        # Same rewrite: the PRE-CAP growth figure (before any tier/
        # fraction clamp), alongside growth_rate_used (the number the
        # model actually compounds from) - see estimate_growth()'s own
        # "raw_rate" return value.
        "growth_raw": None,
        # Audit fixes Commit 4 (30 Sep 2026, owner-directed): share_class_
        # engine.whole_company_shares()'s own (flagged, source, note) used
        # to be discarded here entirely (only `shares` itself was kept) -
        # a caller had no way to know whether a dual-class override was
        # applied, or a candidate was found but REJECTED (ratio above the
        # sanity ceiling, or uncorroborated) versus never having found one
        # at all. share_count_flagged mirrors the old, silently-dropped
        # `_dc_flagged`; share_count_source is "bundle"/"implied"/"filed"/
        # None; share_count_note is the human-readable rejection reason,
        # only ever set when a candidate existed but was turned down -
        # None both when no candidate existed AND when one was accepted
        # (accepted needs no explanatory note, only share_count_flagged).
        # None when diluted_shares_override was passed explicitly (the
        # caller already resolved this itself, share_class_engine.
        # whole_company_shares() is never even called on that path).
        "share_count_flagged": False,
        "share_count_source": None,
        "share_count_note": None,
        # Audit fixes Commit 4 (30 Sep 2026, owner-directed) - pure
        # passthrough of capm_engine.resolve_discount_rate_by_market_
        # cap()'s own market_cap_missing flag; True also forces
        # meta["defaulted"] True (see the "tiered" discount-rate branch
        # below) so the red "estimated inputs" treatment fires.
        "market_cap_missing": False,
        "is_financials_mode": False,
    }

    try:
        if info is None:
            info = yf.Ticker(ticker).info or {}
        currency = currency or info.get("currency") or "USD"

        if diluted_shares_override:
            shares = diluted_shares_override
        else:
            shares, _dc_flagged, _dc_source, _dc_note = share_class_engine.whole_company_shares(
                info, ticker=ticker,
            )
            meta["share_count_flagged"] = _dc_flagged
            meta["share_count_source"] = _dc_source
            meta["share_count_note"] = _dc_note
        if shares <= 0:
            return 0, None, meta

        # --- Base free cash flow -------------------------------------------
        # 1) user-supplied manual override, else 2) a capex-NORMALISED base
        # (latest operating cash flow minus AVERAGE capex) so a one-off capex
        # spike doesn't collapse the valuation.
        norm_base, fcf_series, base_src, base_normalized, capex_basis, oneoff_meta = normalized_base_and_series(
            cashflow_df, info=info, income_df=income_df
        )
        # Financials mode (2 Oct 2026, owner-directed) - set unconditionally
        # here, even on the manual-override/no-base branches below, since
        # it's the classifier's answer for this ticker, not a property of
        # which base ended up used.
        meta["is_financials_mode"] = oneoff_meta.get("is_financials_mode", False)

        if manual_fcf is not None and manual_fcf > 0:
            fcf = float(manual_fcf)
            meta["fcf_source"] = "manual"
        elif norm_base is not None and norm_base > 0:
            fcf = norm_base
            meta["fcf_source"] = base_src
            meta["capex_basis"] = capex_basis
            if base_normalized:
                meta["fcf_base_normalized"] = True
                meta["fcf_base_raw"] = round(fcf_series[0], 2) if fcf_series else None
                meta["fcf_base_used"] = round(norm_base, 2)
            # Step 4 (owner-directed, 30 Sep 2026, KO fix): always set
            # (mirrors the plain fcf_source when the mechanism didn't
            # fire - see normalized_base_and_series()'s own docstring),
            # so a caller always has one field to check regardless of
            # path. fcf_base_raw here reuses the SAME meta key Task 10's
            # own swap above sets - the two mechanisms are mutually
            # exclusive per ticker (this one returns early, before Task
            # 10's swap ever runs, whenever it fires), so there is never
            # a conflict over which value the key should hold.
            meta["fcf_base_source"] = oneoff_meta["fcf_base_source"]
            meta["fcf_distorted_years"] = oneoff_meta["fcf_distorted_years"]
            if oneoff_meta["fcf_base_source"] in ("median5_clean", "ebitda_bridge"):
                meta["fcf_base_raw"] = (
                    round(oneoff_meta["fcf_base_raw"], 2) if oneoff_meta["fcf_base_raw"] is not None else None
                )
                meta["fcf_base_used"] = round(norm_base, 2)
                # Step 4 uplift safety valve (2 Oct 2026, owner decision)
                # - pure passthrough, see normalized_base_and_series()'s
                # own docstring.
                meta["fcf_base_capped_by_uplift"] = oneoff_meta.get("fcf_base_capped_by_uplift", False)
        else:
            # A6 negative-FCF disclosure (owner-directed, 30 Sep 2026):
            # norm_base was found but was <=0 (capex genuinely exceeds
            # operating cash flow) - distinct from norm_base being None
            # (no OCF/capex data at all, e.g. no cash-flow statement).
            # Recorded whether or not the info["freeCashflow"] fallback
            # just below happens to rescue it - the normalised base was
            # still negative, which is worth disclosing either way.
            if norm_base is not None and norm_base <= 0:
                meta["fcf_reason"] = "negative_normalised_fcf"
            fcf = info.get("freeCashflow", 0) or 0
            meta["fcf_source"] = "info" if fcf > 0 else "none"

        if fcf <= 0:
            # The DCF is abandoned here - overrides "negative_normalised_
            # fcf" above (a more specific, final reason) whenever the
            # fallback didn't rescue it either. A manual override or a
            # positive norm_base/info fallback never reaches this branch,
            # so "negative_fcf" only ever means "there was truly no
            # usable free cash flow anywhere for this ticker".
            meta["fcf_reason"] = "negative_fcf"
            return 0, None, meta

        # --- Currency conversion: reported financials vs listing currency --
        # Some companies (e.g. ASX-listed CSL.AX) report financial
        # statements in a different currency than the one they trade in
        # (info["financialCurrency"] == "USD" while the stock trades in
        # AUD). Without this, fcf below silently stays in the STATEMENT
        # currency while fcf_per_share is presented to the app/user as a
        # PRICE-currency (listing currency) per-share figure - a straight
        # unit mismatch. Applied to whichever fcf was just chosen above,
        # manual override included: manual_fcf isn't documented anywhere as
        # being in a different unit than the auto-derived figure, and both
        # flow into the exact same fcf_per_share line below, so keeping one
        # consistent meaning (statement currency in, listing currency out)
        # is the least surprising reading rather than inventing an
        # undocumented special case for the manual path.
        fin_ccy = (info.get("financialCurrency") or currency or "").upper()
        listing_ccy = (currency or info.get("currency") or "").upper()
        if fin_ccy and listing_ccy and fin_ccy != listing_ccy:
            _fx, _fx_src = fx_rate(fin_ccy, listing_ccy)
            fcf = fcf * _fx
            meta["fx_converted"] = f"{fin_ccy}->{listing_ccy}"
            meta["fx_rate_used"] = round(_fx, 4)
            if _fx_src == "fallback":
                meta["fx_fallback"] = True

        # --- Discount rate (CAPM, per stock) --------------------------------
        if discount_rate is not None:
            # Audit fix 1.4: a manual override used to bypass the CAPM
            # auto-path's [MIN_DISCOUNT_RATE, DISCOUNT_CEIL] band entirely -
            # a typo (e.g. "1" meant as "10") could push the discount rate
            # to near-zero, force the perpetual-rate guard below down to
            # match, and inflate the intrinsic value by roughly an order of
            # magnitude with nothing on screen flagging it as unusual
            # (manual overrides don't set value_default). Clamp to the same
            # band the auto path is already held to.
            _dr_capped = not (capm_engine.MIN_DISCOUNT_RATE <= discount_rate <= capm_engine.DISCOUNT_CEIL)
            discount_rate = max(capm_engine.MIN_DISCOUNT_RATE,
                                 min(discount_rate, capm_engine.DISCOUNT_CEIL))
            meta["discount_source"] = "manual"
            # Deliberately a separate key from discount_floored (which the
            # auto/tiered path below sets for a different reason - the
            # A6 market-cap tier premium sitting below MIN_DISCOUNT_RATE,
            # e.g. the mega-cap tier at today's rates - and which the app
            # renders with tier-specific caption text): this is a manual
            # value that was out of bounds and got clamped, which needs
            # its own, accurate on-screen text.
            if _dr_capped:
                meta["discount_manual_clamped"] = True
        else:
            try:
                discount_rate, capm_meta = capm_engine.resolve_discount_rate(info, currency)
                meta["discount_source"] = "tiered-default" if capm_meta["defaulted"] else "tiered"
                meta["discount_floored"] = bool(capm_meta.get("floored"))
                meta["discount_tier_label"] = capm_meta.get("tier_label")
                # Audit fixes Commit 4 (30 Sep 2026, owner-directed):
                # capm_engine.resolve_discount_rate_by_market_cap() now
                # sets its own meta["defaulted"] when marketCap was
                # missing (see that function's own comment) - propagated
                # here into the outer, DCF-level "defaulted" flag (the
                # one that actually drives the red "estimated inputs"
                # treatment on screen) specifically for THIS cause, not
                # generically for every capm_meta["defaulted"] reason
                # (a risk-free-rate fallback already has its own, older,
                # narrower "tiered-default" discount_source label above -
                # left exactly as it was, out of this commit's scope).
                meta["market_cap_missing"] = bool(capm_meta.get("market_cap_missing"))
                if meta["market_cap_missing"]:
                    meta["defaulted"] = True
                # Push 2 (owner-directed, 30 Sep 2026): thread the size-
                # premium breakdown through to the DCF-level meta so a
                # caller (app.py's Deep Dive caption, auto_compounder_
                # engine.py's Fair Value tab) can render "Discount X% =
                # Y% risk-free + Z% size premium (band, US$capB)"
                # instead of just the combined rate - see capm_engine.
                # resolve_discount_rate()'s own docstring for where these
                # come from.
                meta["risk_free_used"] = capm_meta.get("risk_free_used")
                meta["risk_free_source"] = capm_meta.get("rf_source")
                meta["market_cap_usd"] = capm_meta.get("market_cap_usd")
                meta["premium_used"] = capm_meta.get("premium_used")
            except Exception:
                discount_rate = DEFAULT_DISCOUNT_RATE
                meta["discount_source"] = "fallback"

        # --- Terminal / perpetual growth rate (currency-based) --------------
        if perpetual_rate is not None:
            # Audit fix 1.4 (continued): bound a manual perpetual-rate
            # override too, not just discount rate - an unbounded high
            # value would otherwise survive up to the "must be strictly
            # below discount_rate" guard just below and could still land
            # within a hair of the (now-floored) discount rate, reproducing
            # the same near-zero-spread blowup. Currency-based auto rates
            # only ever run 2.0-2.5% (see PERPETUAL_GROWTH_BY_CCY); allow a
            # manual override some real headroom either side of that
            # without permitting an extreme value.
            _pr_capped = not (-0.02 <= perpetual_rate <= 0.06)
            perpetual_rate = max(-0.02, min(perpetual_rate, 0.06))
            meta["perpetual_source"] = "manual"
            if _pr_capped:
                meta["perpetual_manual_clamped"] = True
        else:
            try:
                perpetual_rate = capm_engine.resolve_perpetual_rate(currency, discount_rate)
                meta["perpetual_source"] = "currency"
            except Exception:
                perpetual_rate = DEFAULT_PERPETUAL_RATE
                meta["perpetual_source"] = "fallback"

        # Guard: terminal growth must be strictly below the discount rate.
        if perpetual_rate >= discount_rate:
            perpetual_rate = max(0.0, discount_rate - 0.01)

        meta["discount_rate_used"] = round(discount_rate, 4)
        meta["perpetual_rate_used"] = round(perpetual_rate, 4)

        # --- Growth rate ----------------------------------------------------
        growth_ceiling = growth_ceiling_for(info, currency)
        meta["growth_ceiling_used"] = growth_ceiling
        # Growth-never-zero rewrite (owner-directed, 30 Sep 2026): end_rate
        # is now computed ONCE, here, before growth is resolved - the SAME
        # value both estimate_growth()'s own priority-2/3 caps and
        # priority-4 fallback use, AND the stage-1 fade loop's own target
        # below (meta["growth_end_rate_used"]) - moved up from its old
        # position right before that loop so the two can never diverge
        # (previously two separate computations of the same formula, only
        # coincidentally identical).
        end_rate = max(growth_end_rate_for(info, currency), perpetual_rate)
        if growth_rate is not None:
            capped = growth_rate > growth_ceiling
            growth_rate = max(GROWTH_FLOOR, min(growth_rate, growth_ceiling))
            meta["growth_source"] = "manual"
            meta["growth_governor"] = "Cap" if capped else "Manual"
        else:
            analyst_growth = None
            # Growth-estimate-fetch resilience fix (owner-directed, 28 Sep
            # 2026): get_growth_estimates_5y() now returns (value, status)
            # - status distinguishes "Yahoo genuinely has no +5y estimate
            # for this name" (no_coverage) from "the fetch itself failed,
            # even after retrying" (fetch_failed) - see that function's
            # own docstring. Surfaced here (not just used to decide the
            # fallback) so the app can show the real reason instead of
            # always implying "no Yahoo coverage" when growth_source ends
            # up "history". Growth-never-zero rewrite (30 Sep 2026): a
            # THIRD status, "non_positive", now means the fetch found a
            # real Yahoo LTG/+5y value that was <=0 - a real data fact,
            # not a failure, but not usable as a growth signal either
            # (estimate_growth()'s own priority 1 falls through on it).
            try:
                analyst_growth, yahoo_estimate_status = capm_engine.get_growth_estimates_5y(ticker)
            except Exception:
                analyst_growth, yahoo_estimate_status = None, "fetch_failed"
            meta["yahoo_estimate_status"] = yahoo_estimate_status
            _growth_extra = {}
            growth_rate, gsrc, governor, growth_raw = estimate_growth(
                info, fcf_series=fcf_series, analyst_growth=analyst_growth,
                ceiling=growth_ceiling, end_rate=end_rate, currency=currency,
                yahoo_estimate_status=yahoo_estimate_status, extra_out=_growth_extra)
            meta["growth_source"] = gsrc
            meta["growth_governor"] = governor
            meta["growth_raw"] = round(growth_raw, 4)
            # Owner decision, 1 Oct 2026 (KNSL/Kinsale Capital): the two
            # raw inputs behind the "analyst_1y"/"analyst_1y_blend"
            # blend, for the Deep Dive/Fair Value/Scanner tooltip - see
            # estimate_growth()'s own docstring. None/None for every
            # other growth_source, same as every other optional meta
            # field in this dict.
            meta["growth_1y_consensus"] = _growth_extra.get("growth_1y_consensus")
            meta["growth_history_capped"] = _growth_extra.get("growth_history_capped")
            if gsrc == "default":
                meta["growth_default"] = True
                meta["defaulted"] = True
            # No-uphill-fade fix (owner-directed, 1 Oct 2026, Commit 4 of
            # instruction_dcf_unreliable_pool_step4_nightly.md): the
            # "analyst"(ok)/"history"/"history_volatile"/"info"/
            # "default" paths carry no floor at end_rate (see estimate_
            # growth()'s own "No-uphill-fade invariant" docstring,
            # reported not fixed by the growth-1y-blend task's own scope
            # guard) - when one of them resolves BELOW end_rate, the
            # stage-1 loop below never actually fades uphill (`fade =
            # growth_rate > end_rate` stays False, so the path goes flat
            # at growth_rate for the whole horizon - see that loop's own
            # comment), but meta["growth_end_rate_used"] still reported
            # the ORIGINAL (higher) tier end rate, misdescribing what the
            # model actually did (the KNSL symptom: "2.5% for 5 yrs, then
            # fades to 5.0% by yr 10" when the path was actually flat at
            # 2.5% the whole way). Owner decision: don't raise the base -
            # lower the REPORTED end rate to match it instead: min(end_
            # rate, growth_rate) - NOT also re-floored at perpetual_rate
            # here, even though the instruction's own phrasing says
            # "still floored at the perpetual rate as today": end_rate
            # ITSELF was already computed as max(tier_end_rate,
            # perpetual_rate) above, before growth was ever resolved, so
            # that floor is already baked into the value this min() reads
            # - "as today" describes that existing, unchanged computation,
            # not a second, independent floor applied AFTER the min().
            # Re-flooring here would be self-defeating: whenever
            # growth_rate itself falls below perpetual_rate (a real,
            # reachable case - perpetual_rate is independent of
            # growth_rate), max(min(end_rate, growth_rate), perpetual_
            # rate) could exceed growth_rate again, reintroducing the
            # exact invariant violation this fix exists to close. A plain
            # min(end_rate, growth_rate) can never exceed growth_rate by
            # construction, so growth_used >= growth_end_rate_used holds
            # unconditionally once this fires - see this task's own
            # invariant sweep (tests/test_no_uphill_fade_all_sources.py)
            # for the verification. A company growing 1% is now modelled
            # (and described) as flat at 1%, never accelerating just
            # because its market-cap tier says it should fade up to a
            # higher end rate. The two analyst_1y paths keep their own
            # end-rate FLOOR from the growth-1y-blend task unchanged (a
            # one-year consensus is cyclical, not structural) - excluded
            # here on purpose, though the min() below would be a no-op
            # for them anyway since growth_rate already can't be below
            # end_rate on those two paths by construction. "manual" is
            # also excluded - out of this task's own stated scope
            # (analyst/history/history_volatile/info/default only).
            if gsrc in ("analyst", "history", "history_volatile", "info", "default") and growth_rate < end_rate:
                end_rate = min(end_rate, growth_rate)
                meta["growth_governor"] = "end rate lowered to base - no uphill fade"

        fcf_per_share = fcf / shares
        # Surfaced so the app can actually show what went into the model -
        # previously the normalised base FCF (fcf) and the per-share figure
        # derived from it (fcf_per_share, the number the whole stage-1/
        # terminal-value ladder above is built on) were computed here and
        # then discarded; nothing downstream could answer "what FCF is this
        # using" without recomputing it by hand.
        meta["fcf_used"] = round(fcf, 2)
        meta["fcf_per_share_used"] = round(fcf_per_share, 4)
        # Step 4 (30 Sep 2026, KO fix): the raw (pre-distortion-swap)
        # per-share figure, for the Deep Dive caption's "vs raw $X/share"
        # - same shares denominator as fcf_per_share_used just above, so
        # the two are directly comparable. None unless the mechanism
        # actually fired (meta["fcf_base_raw"] is only set then too -
        # see the oneoff_meta block above).
        if meta.get("fcf_base_source") in ("median5_clean", "ebitda_bridge") and meta.get("fcf_base_raw") is not None:
            meta["fcf_base_raw_per_share"] = round(meta["fcf_base_raw"] / shares, 4)

        # Stage 1: discount each year's grown cash flow back to today.
        # Growth-path option E (owner-directed, 28 Sep 2026 follow-up to
        # the same-day amendment this replaces): three sub-stages, not
        # one flat rate for the whole horizon:
        #   years 1-5:  flat at growth_rate (g1, as chosen above)
        #   years 6-N:  fade linearly from g1 down to end_rate, reaching
        #               end_rate exactly in year N (growth_years,
        #               normally 10):
        #                   g_t = g1 - (g1 - end_rate) * (t-5) / (N-5)
        # end_rate is the market-cap-tiered growth end rate (growth_end_
        # rate_for() above) - NOT perpetual_rate, which is currency-
        # based and stays the Gordon terminal-value input just below,
        # unchanged - floored at perpetual_rate itself (max(tier end
        # rate, perpetual_rate)) so a stock never fades BELOW its own
        # terminal rate (e.g. an AUD mega-cap tier end of 2% would sit
        # below AUD's own 2.5% perpetual rate - floored up to 2.5%
        # instead, so the path never implies negative growth relative to
        # the terminal assumption it's about to hand off to).
        #
        # A company growing at, say, 10% today plausibly still grows near
        # 10% for the next several years, but not flat all the way to the
        # terminal-value boundary - fading only the back half is the
        # owner's own explicit design (replacing both the pre-28-Sep-2026
        # flat-10-years model AND the since-reverted single-stage fades
        # this repo has already tried, first to perpetual_rate from year
        # 1, then to perpetual_rate from year 6). If g1 is already AT or
        # BELOW end_rate, the fade never applies at all - flat g1 for
        # every year, never fading upward. growth_years <= 5 also has no
        # fade stage (every year is in the flat window) - same divide-
        # by-(growth_years-5) guard as growth_years<=1 elsewhere in this
        # codebase. meta["growth_path"] carries all growth_years yearly
        # rates actually used; meta["growth_end_rate_used"] is the
        # (floored) end_rate itself, for display. end_rate itself was
        # already computed once, above, before growth was resolved - see
        # that computation's own comment for why (growth-never-zero
        # rewrite, 30 Sep 2026).
        meta["growth_end_rate_used"] = round(end_rate, 4)
        fade = growth_rate > end_rate

        intrinsic = 0.0
        cash_flow = fcf_per_share
        growth_path = []
        for year in range(1, growth_years + 1):
            if year <= 5 or growth_years <= 5 or not fade:
                g_t = growth_rate
            else:
                g_t = growth_rate - (growth_rate - end_rate) * (year - 5) / (growth_years - 5)
            growth_path.append(round(g_t, 4))
            cash_flow = cash_flow * (1 + g_t)
            intrinsic += cash_flow / ((1 + discount_rate) ** year)
        meta["growth_path"] = growth_path

        # Stage 2: Gordon terminal value on the final year's cash flow.
        terminal_cf = cash_flow * (1 + perpetual_rate)
        terminal_value = terminal_cf / (discount_rate - perpetual_rate)
        intrinsic += terminal_value / ((1 + discount_rate) ** growth_years)

        # Audit fix B2.5 (27 Sep 2026, owner-directed, CONFIRMED BUG):
        # intrinsic used to be rounded to 2dp HERE, before any caller
        # computed MOS from it - corrupting MOS (and misclassifying
        # penny stocks) whenever the true value had meaningful precision
        # below a cent. Round only for display (nightly_scan.py and
        # every other caller already re-round independently); keep full
        # precision in the value MOS is actually computed from.
        return intrinsic, round(growth_rate, 4), meta

    except Exception:
        return 0, None, meta
