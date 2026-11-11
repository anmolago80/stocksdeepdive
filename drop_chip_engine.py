"""
drop_chip_engine.py

TASK 39 (Director-directed, owner-reported): "drop from 3-year peak"
chip + mini price chart, extending TASK 36's support chip.

PEAK DEFINITION (the Director's own exact rule): the highest WEEKLY
CLOSE in the last 3 calendar years - resampled from daily closes to
weekly, same resample convention support_levels_job.py's own
_weekly_bars_from_daily() already uses (pandas "W", week-ending-
Sunday). NOT an intraday high, NOT a pivot high - a plain max() over
the weekly-close window. A bounce mid-decline must never reset the
peak: windowing by calendar date (the last PEAK_WINDOW_YEARS years
from the LATEST bar) and taking a single max() over that whole window
already guarantees this - there is no rolling/local-max logic here
that a recovery could ever "reset".

DROP %: (current price / peak - 1) * 100 - negative when below the
peak, 0 or positive when the current price IS (or exceeds) the peak.
The chip is shown only when drop <= DROP_CHIP_THRESHOLD_PCT (-20%);
otherwise nothing is shown for the drop (per the Director's own rule).

Every public function here is pure (no I/O, no yfinance/pandas
dependency beyond what the caller already resampled) - takes/returns
plain lists/dicts of (date, price) pairs, mirroring support_levels_
engine.py's own module shape so this stays trivially unit-testable
and reusable by whatever caller already has the weekly bars on hand
(support_levels_job.py's own nightly pass - see that module's own
"build together" wiring, TASK 42 item 6b).
"""

import datetime

PEAK_WINDOW_YEARS = 3
DROP_CHIP_THRESHOLD_PCT = -20.0
# TASK 42 item 6b (Director-directed, 8 Nov 2026): "Quality on sale"
# tab's own drop-depth floor - a DEEPER cut than the card chip's own
# -20% ("at least 40% below the 3-year peak").
QUALITY_ON_SALE_DROP_THRESHOLD_PCT = -40.0
# Max chart points the Director asked for ("~156 points max") - a
# plain 3-year weekly window is already <= this (3 * 52 = 156, give or
# take a partial week at either end), so no separate downsampling step
# is needed; this constant exists only so a caller can assert the
# window never silently grows past the budget it was asked to respect.
MAX_CHART_POINTS = 156
# TASK 42 item 6a (Director-directed, 8 Nov 2026, CSL.AX live case):
# the SAME clamp ranking_engine.PSY_CLAMP already applies to the
# EXISTING (3-month-high) fear term when it feeds calculate_long_score()
# - this module's own fear_from_peak() deliberately does NOT re-clamp
# (the Director's own "clamp +-50 as now" means "let the existing
# downstream clamp do it", not "add a second one here" - a caller
# that feeds this into calculate_long_score()/recompute_value_score()
# gets that exact clamp for free, same as today's fear term does).
FEAR_FROM_PEAK_CLAMP = 50.0


def _years_ago(reference_date, years):
    """`reference_date` minus `years` calendar years - handles the
    29 Feb edge case by falling back to 28 Feb (datetime.date has no
    native year-subtraction)."""
    try:
        return reference_date.replace(year=reference_date.year - years)
    except ValueError:
        return reference_date.replace(year=reference_date.year - years, day=28)


def compute_peak_and_drop(weekly_dates, weekly_closes, window_years=PEAK_WINDOW_YEARS):
    """weekly_dates/weekly_closes: parallel oldest-first lists (the
    SAME shape support_levels_job._weekly_bars_from_daily() already
    produces - a caller computing both the support chip and this one
    from the SAME weekly bars never resamples twice).

    Returns {
      "peak_date": date|None, "peak_price": float|None,
      "current_date": date|None, "current_price": float|None,
      "drop_pct": float|None,          # (current/peak - 1) * 100
      "chip_shown": bool,              # drop_pct <= DROP_CHIP_THRESHOLD_PCT
      "series": [(date, close), ...],  # the 3-year window itself, oldest-
                                        # first, parallel to what the mini
                                        # chart renders from - already
                                        # <= MAX_CHART_POINTS, no further
                                        # downsampling needed
    } - an all-None/False "no data" shape (never raises) when there
    are too few weeks to even have a current price."""
    if not weekly_closes or not weekly_dates:
        return {
            "peak_date": None, "peak_price": None,
            "current_date": None, "current_price": None,
            "drop_pct": None, "chip_shown": False, "series": [],
        }

    current_date = weekly_dates[-1]
    current_price = weekly_closes[-1]
    window_start = _years_ago(current_date, window_years)

    series = [
        (d, c) for d, c in zip(weekly_dates, weekly_closes)
        if d >= window_start
    ]
    if not series:
        series = [(current_date, current_price)]

    peak_date, peak_price = max(series, key=lambda dc: dc[1])

    drop_pct = ((current_price / peak_price) - 1.0) * 100.0 if peak_price else None
    # A tiny epsilon absorbs ordinary floating-point noise (e.g. a
    # price that's "exactly" -20.0% in human terms can land on
    # -19.999999999999996 after the division above) so a genuine
    # boundary case is never wrongly excluded by float imprecision.
    chip_shown = drop_pct is not None and drop_pct <= DROP_CHIP_THRESHOLD_PCT + 1e-9

    return {
        "peak_date": peak_date, "peak_price": peak_price,
        "current_date": current_date, "current_price": current_price,
        "drop_pct": drop_pct, "chip_shown": chip_shown, "series": series,
    }


def fear_from_peak(current_price, peak_price):
    """TASK 42 item 6a's shadow fear variant: % below the 3-year weekly
    peak, same shape as nightly_scan.py's own EXISTING
    `fear = ((high_price - current_price) / high_price) * 100` - just
    swapping the 3-month high for this module's own 3-year peak.
    Unclamped by design - see FEAR_FROM_PEAK_CLAMP's own comment above
    for why. None when peak_price is missing/zero (nothing to divide
    by), never a ZeroDivisionError."""
    if not peak_price:
        return None
    return ((peak_price - current_price) / peak_price) * 100.0


def passes_quality_on_sale_drop_rule(drop_pct, threshold=QUALITY_ON_SALE_DROP_THRESHOLD_PCT):
    """TASK 42 item 6b: True when `drop_pct` (compute_peak_and_drop()'s
    own "drop_pct", already negative-below-peak) is at or below the
    "Quality on sale" tab's own -40% floor - a STRICTER cut than the
    card chip's own -20% (every "Quality on sale" candidate would also
    show the drop chip, but not every drop-chip ticker qualifies for
    this tab). False (never raises) for a missing drop_pct - no data
    to qualify on. Same tiny float-precision epsilon as compute_peak_
    and_drop()'s own chip_shown check."""
    return drop_pct is not None and drop_pct <= threshold + 1e-9
