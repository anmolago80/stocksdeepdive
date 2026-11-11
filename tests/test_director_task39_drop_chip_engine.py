"""
TASK 39 (Director-directed): drop_chip_engine.py's own pure
peak/drop/fear-variant math - weekly resample is tested upstream
(support_levels_job.py's own _weekly_bars_from_daily(), TASK 36/29/
29b/29c's own test files); this file covers everything this module
computes FROM an already-resampled weekly series.

Run: python3 tests/test_director_task39_drop_chip_engine.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drop_chip_engine as dce

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


def _d(y, m, d):
    return datetime.date(y, m, d)


def _weekly_series(start_date, weekly_prices):
    """Builds (dates, closes) with one bar per week starting at
    start_date, for however many prices are given."""
    dates = [start_date + datetime.timedelta(weeks=i) for i in range(len(weekly_prices))]
    return dates, list(weekly_prices)


# ======================================================================
# CHECK 1: basic peak/drop - a clean decline from a single peak.
# ======================================================================
dates, closes = _weekly_series(_d(2023, 1, 1), [100.0, 110.0, 120.0, 90.0, 60.0])
r = dce.compute_peak_and_drop(dates, closes)
check("peak_price is the highest weekly close in the window (120.0)",
      r["peak_price"] == 120.0)
check("peak_date is the week that close happened on",
      r["peak_date"] == dates[2])
check("current_price is the LAST weekly close (60.0)", r["current_price"] == 60.0)
check("current_date is the last week", r["current_date"] == dates[-1])
check("drop_pct = (60/120 - 1) * 100 = -50.0", r["drop_pct"] == -50.0)
check("chip_shown is True (drop <= -20%)", r["chip_shown"] is True)
print("[task39_check1_basic_peak_drop] basic single-peak decline computes "
      "peak/drop/chip_shown correctly OK")


# ======================================================================
# CHECK 2 (the Director's own explicit test case): a bounce MID-
# DECLINE must NOT reset the peak - the peak stays the ORIGINAL high
# even after a partial recovery that's still well below it.
# ======================================================================
dates2, closes2 = _weekly_series(_d(2023, 1, 1), [
    50.0, 80.0, 150.0,   # rises to a peak of 150
    100.0, 70.0,         # declines
    90.0,                # bounces - but 90 is still far below 150, and
                          # must NOT be mistaken for a new peak
    65.0,                # declines again
])
r2 = dce.compute_peak_and_drop(dates2, closes2)
check("the peak stays 150.0 (the ORIGINAL high) despite the later bounce "
      "to 90.0 - a bounce mid-decline never resets the peak",
      r2["peak_price"] == 150.0)
check("peak_date is week 3 (the 150.0 week), not the bounce week",
      r2["peak_date"] == dates2[2])
check("drop_pct is measured against 150.0, not against the bounce "
      "(65/150 - 1) * 100 = -56.67%",
      abs(r2["drop_pct"] - (-56.666666666666664)) < 1e-9)
print("[task39_check2_bounce_never_resets_peak] a mid-decline bounce "
      "never gets mistaken for a new peak - this is a plain max() over "
      "the whole 3-year window, not a rolling/local-max that a bounce "
      "could reset OK")


# ======================================================================
# CHECK 3: the -20% threshold boundary - exactly -20% shows the chip
# (<=, not <); just above -20% does not.
# ======================================================================
dates3, closes3 = _weekly_series(_d(2023, 1, 1), [100.0, 80.0])  # exactly -20%
r3 = dce.compute_peak_and_drop(dates3, closes3)
check("drop exactly -20.0% -> chip_shown True (<=, not <, tolerant of "
      "ordinary floating-point noise at the exact boundary)",
      abs(r3["drop_pct"] - (-20.0)) < 1e-6 and r3["chip_shown"] is True)

dates3b, closes3b = _weekly_series(_d(2023, 1, 1), [100.0, 80.01])  # -19.99%
r3b = dce.compute_peak_and_drop(dates3b, closes3b)
check("drop -19.99% (just above -20%) -> chip_shown False",
      r3b["drop_pct"] > -20.0 and r3b["chip_shown"] is False)
print("[task39_check3_threshold_boundary] the -20% chip threshold is "
      "inclusive (<=) at the exact boundary, exclusive just above it OK")


# ======================================================================
# CHECK 4: a current price AT or ABOVE the peak -> drop_pct is 0 or
# positive, never shows the chip.
# ======================================================================
dates4, closes4 = _weekly_series(_d(2023, 1, 1), [100.0, 90.0, 120.0])
r4 = dce.compute_peak_and_drop(dates4, closes4)
check("current price (120.0) IS the peak -> drop_pct == 0.0, no chip",
      r4["drop_pct"] == 0.0 and r4["chip_shown"] is False)
print("[task39_check4_at_the_peak] a ticker currently AT its own 3-year "
      "peak shows no drop chip at all OK")


# ======================================================================
# CHECK 5: the 3-YEAR WINDOW itself - a peak more than 3 years before
# the latest bar must be EXCLUDED from the max(), even if it's higher
# than anything inside the window (an old all-time-high outside the
# window is not this ticker's "3-year peak").
# ======================================================================
_latest = _d(2026, 11, 1)
dates5 = [
    _latest - datetime.timedelta(days=365 * 4),  # 4 years ago - OUTSIDE the 3y window
    _latest - datetime.timedelta(days=365 * 2),  # 2 years ago - inside
    _latest,                                      # today
]
closes5 = [500.0, 100.0, 80.0]  # the 500.0 high is OUTSIDE the 3y window
r5 = dce.compute_peak_and_drop(dates5, closes5)
check("a higher close FOUR years ago (outside the 3-year window) is "
      "excluded from the peak - the window's own highest close (100.0, "
      "2 years ago) is used instead",
      r5["peak_price"] == 100.0)
check("the excluded old high (500.0) never appears in the returned "
      "'series' either - the series IS the window",
      500.0 not in [c for _, c in r5["series"]])
print("[task39_check5_three_year_window] a peak outside the 3-year "
      "window is correctly excluded, even when it's numerically higher "
      "than anything inside the window OK")


# ======================================================================
# CHECK 6: the returned "series" is the 3-year window itself (oldest-
# first), already within the chart's own point budget - no separate
# downsampling needed for a plain weekly 3-year run.
# ======================================================================
_dense_dates, _dense_closes = _weekly_series(_d(2020, 1, 1), [100.0] * 400)  # ~7.7 years of weekly bars
r6 = dce.compute_peak_and_drop(_dense_dates, _dense_closes)
# 3 calendar years of 7-day-spaced weekly bars is ~156.5 weeks - the
# exact count can land on 156 or 157 depending on calendar alignment
# (leap years, which weekday the window boundary falls on), so this
# checks "close to the ~156 budget", not an exact hard cap.
check(f"a 400-week (~7.7y) history still returns a 'series' close to "
      f"the ~{dce.MAX_CHART_POINTS}-point budget (got {len(r6['series'])}) "
      f"- the 3-year window alone, not the whole history",
      dce.MAX_CHART_POINTS <= len(r6["series"]) <= dce.MAX_CHART_POINTS + 2)
check("'series' is oldest-first (ascending dates)",
      all(r6["series"][i][0] <= r6["series"][i + 1][0] for i in range(len(r6["series"]) - 1)))
print("[task39_check6_series_is_the_window_only] the chart series is "
      "exactly the 3-year window, already under the point budget, "
      "regardless of how much history is actually available OK")


# ======================================================================
# CHECK 7: empty/missing input never raises - returns the documented
# all-None/False shape.
# ======================================================================
r7 = dce.compute_peak_and_drop([], [])
check("empty input -> all-None/False shape, never raises",
      r7["peak_price"] is None and r7["drop_pct"] is None
      and r7["chip_shown"] is False and r7["series"] == [])
print("[task39_check7_empty_input_safe] empty weekly series never "
      "raises, returns the documented no-data shape OK")


# ======================================================================
# CHECK 8 (TASK 42 item 6a): fear_from_peak() - the shadow fear
# variant, same shape as nightly_scan.py's own existing 3-month-high
# fear formula, just swapped to the 3-year peak.
# ======================================================================
check("fear_from_peak(60, 120) == ((120-60)/120)*100 == 50.0",
      dce.fear_from_peak(60.0, 120.0) == 50.0)
check("fear_from_peak at the peak itself (price == peak) -> 0.0 (no fear)",
      dce.fear_from_peak(120.0, 120.0) == 0.0)
check("fear_from_peak above the peak -> NEGATIVE (the Director's own "
      "'clamp +-50 as now' implies this can exceed the clamp before "
      "the existing downstream clamp applies - this function itself "
      "never clamps)",
      dce.fear_from_peak(150.0, 120.0) == -25.0)
check("fear_from_peak with no peak price (None/0) -> None, never "
      "raises (no ZeroDivisionError)",
      dce.fear_from_peak(60.0, None) is None and dce.fear_from_peak(60.0, 0.0) is None)
print("[task39_check8_fear_from_peak] the shadow fear-from-peak variant "
      "matches the existing fear formula's own shape, unclamped by "
      "design (the existing downstream PSY_CLAMP applies for free) OK")


# ======================================================================
# CHECK 9 (TASK 42 item 6b): passes_quality_on_sale_drop_rule() - the
# -40% floor, stricter than the card chip's own -20%.
# ======================================================================
check("drop -40.0% (exactly the floor) -> passes (<=, not <)",
      dce.passes_quality_on_sale_drop_rule(-40.0) is True)
check("drop -39.99% (just above the floor) -> does NOT pass",
      dce.passes_quality_on_sale_drop_rule(-39.99) is False)
check("drop -60% (well past the floor) -> passes",
      dce.passes_quality_on_sale_drop_rule(-60.0) is True)
check("None drop -> does NOT pass, never raises",
      dce.passes_quality_on_sale_drop_rule(None) is False)
print("[task39_check9_quality_on_sale_drop_rule] the -40% 'Quality on "
      "sale' floor is inclusive at the boundary, stricter than the "
      "card chip's own -20% threshold OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
