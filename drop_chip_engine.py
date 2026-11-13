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

# TASK 53 (Director-directed, 13 Nov 2026, RR.L live case): a weekly
# series that still has an unrepaired pence/pounds segment inside it
# (support_levels_job._weekly_bars_from_daily()'s own pence-repair
# passes can miss one - see that module's own docstring for exactly
# why: a ticker whose real price has risen enough within the window
# that a genuinely-pence point's ratio to TODAY's price drifts under
# PASS 1's 30x floor, with no internal unit-switch boundary anywhere
# in the window for PASS 2 to find either) produces a "peak" many
# multiples of today's price - RR.L: peak 393.42 (still pence, i.e.
# really GBP 3.93) vs current GBP 13.70, ratio ~28.7x, read as a
# bogus "-97% drop" that wrongly admitted it to "Quality on sale".
# No real operating company's genuine weekly-close peak is 5x+ its
# own current price within a 3-year window (that would be among the
# most extreme single-stock collapses on record) - a ratio past this
# floor is treated as a still-unrepaired data artifact, never a real
# drop, and suppressed at the source rather than guessed at further.
PEAK_TO_CURRENT_IMPLAUSIBLE_RATIO = 5.0

# TASK 53's own audit threshold for a single-week close-to-close jump
# inside an already-computed 3-year series - the same order of
# magnitude as fundamentals_data._PENCE_HISTORY_NEIGHBOUR_JUMP_RATIO_
# RANGE's own lower bound (50.0), reused here as a plain "is this
# series suspect" signal, not a repair decision (that repair already
# happened upstream, or didn't).
JUMP_AUDIT_RATIO_FLOOR = 50.0


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
      "drop_pct": float|None,          # (current/peak - 1) * 100 - None
                                        # whenever peak_implausible is True
                                        # (TASK 53): never a trustworthy
                                        # figure to show or rank on in
                                        # that case, so every existing
                                        # caller (chip_shown, "Quality on
                                        # sale"'s own drop-rule check) is
                                        # automatically protected without
                                        # touching their own code
      "drop_pct_raw": float|None,      # TASK 53: the SAME formula's
                                        # result WITHOUT the plausibility
                                        # guard - always populated when a
                                        # peak/current exist, diagnostic/
                                        # audit use only, never consumed
                                        # by chip_shown or the drop rule
      "peak_implausible": bool,        # TASK 53: True when peak_price is
                                        # more than PEAK_TO_CURRENT_
                                        # IMPLAUSIBLE_RATIO times current_
                                        # price - almost certainly a still-
                                        # unrepaired pence/pounds data
                                        # artifact (support_levels_job's
                                        # own pence-repair passes can miss
                                        # a segment whose ratio to TODAY's
                                        # price has drifted under their
                                        # own detection floor - see
                                        # PEAK_TO_CURRENT_IMPLAUSIBLE_
                                        # RATIO's own comment above), never
                                        # a real multi-year collapse
      "chip_shown": bool,              # drop_pct <= DROP_CHIP_THRESHOLD_PCT
                                        # (always False when drop_pct is None)
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
            "drop_pct": None, "drop_pct_raw": None, "peak_implausible": False,
            "chip_shown": False, "series": [],
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

    drop_pct_raw = ((current_price / peak_price) - 1.0) * 100.0 if peak_price else None
    # TASK 53 (Director-directed, 13 Nov 2026, RR.L live case): see
    # PEAK_TO_CURRENT_IMPLAUSIBLE_RATIO's own comment above for why a
    # peak many multiples of today's price is treated as a still-
    # unrepaired pence/pounds artifact rather than a real collapse.
    peak_implausible = (
        peak_price is not None and peak_price > 0
        and current_price is not None and current_price > 0
        and (peak_price / current_price) > PEAK_TO_CURRENT_IMPLAUSIBLE_RATIO
    )
    drop_pct = None if peak_implausible else drop_pct_raw
    # A tiny epsilon absorbs ordinary floating-point noise (e.g. a
    # price that's "exactly" -20.0% in human terms can land on
    # -19.999999999999996 after the division above) so a genuine
    # boundary case is never wrongly excluded by float imprecision.
    chip_shown = drop_pct is not None and drop_pct <= DROP_CHIP_THRESHOLD_PCT + 1e-9

    return {
        "peak_date": peak_date, "peak_price": peak_price,
        "current_date": current_date, "current_price": current_price,
        "drop_pct": drop_pct, "drop_pct_raw": drop_pct_raw,
        "peak_implausible": peak_implausible,
        "chip_shown": chip_shown, "series": series,
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


def _series_has_suspect_jump(series, ratio_floor=JUMP_AUDIT_RATIO_FLOOR):
    """True if any two CONSECUTIVE closes in `series` ((date, close)
    pairs, oldest-first) differ by a ratio >= `ratio_floor` - the same
    signature fundamentals_data._neighbour_jump_segments() itself looks
    for (a pence<->pounds unit switch between two adjacent trading
    days), checked here against an ALREADY-COMPUTED weekly series as a
    pure audit signal - this function never repairs anything, only
    reports. A None/non-numeric/non-positive close is skipped (never
    itself compared), same as the upstream detector."""
    prev = None
    for _date, close in series or []:
        if isinstance(close, (int, float)) and close == close and close > 0:
            if prev is not None:
                ratio = max(close, prev) / min(close, prev)
                if ratio >= ratio_floor:
                    return True
            prev = close
    return False


def audit_suspect_entries(entries, peak_ratio_floor=PEAK_TO_CURRENT_IMPLAUSIBLE_RATIO,
                           jump_ratio_floor=JUMP_AUDIT_RATIO_FLOOR):
    """TASK 53's own audit - `entries`: {ticker: drop_chip_store.get(
    ticker)'s own return dict}, already filtered by the caller to
    whichever tickers it wants scanned (e.g. every ".L" ticker in the
    store - this function itself is ticker-agnostic, no currency/
    market logic of its own).

    Flags an entry whose peak_price/current_price ratio exceeds
    `peak_ratio_floor` (report-only here - compute_peak_and_drop()
    itself already suppresses drop_pct/chip_shown for these, via its
    own "peak_implausible" field, so this is "how many/which tickers
    did that guard actually catch", not a second repair decision) OR
    whose own stored 3-year series has a single-week close-to-close
    jump at or above `jump_ratio_floor` (a signal a still-unrepaired
    segment may be hiding INSIDE the window even when the overall
    peak/current ratio itself looks plausible - e.g. a brief pence-
    quoted blip that's since reverted). Either condition alone is
    enough to flag.

    Returns [{"ticker", "peak_price", "current_price",
    "peak_to_current_ratio", "drop_pct", "drop_pct_raw", "has_jump"}],
    sorted by peak_to_current_ratio descending (worst/most implausible
    first; an entry flagged only for its own jump, with no implausible
    ratio, sorts last since its own ratio reads None)."""
    out = []
    for ticker, entry in (entries or {}).items():
        if not entry:
            continue
        peak_price = entry.get("peak_price")
        current_price = entry.get("current_price")
        ratio = (
            peak_price / current_price
            if peak_price and current_price and current_price > 0 else None
        )
        has_jump = _series_has_suspect_jump(entry.get("series"), jump_ratio_floor)
        flagged_peak = ratio is not None and ratio > peak_ratio_floor
        if not (flagged_peak or has_jump):
            continue
        out.append({
            "ticker": ticker, "peak_price": peak_price, "current_price": current_price,
            "peak_to_current_ratio": ratio, "drop_pct": entry.get("drop_pct"),
            "drop_pct_raw": entry.get("drop_pct_raw"), "has_jump": has_jump,
        })
    out.sort(key=lambda r: (r["peak_to_current_ratio"] is None, -(r["peak_to_current_ratio"] or 0.0)))
    return out
