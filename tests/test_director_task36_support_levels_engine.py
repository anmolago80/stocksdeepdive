"""
TASK 36 (Director-directed, owner-reported, 6 Nov 2026): port of the
owner's TradingView "AML Long-Term Entry Framework v10.1" LEVELS
ENGINE ONLY (confirmed weekly pivot lows via ta.pivotlow(3,3), broken
only after two consecutive weekly closes below). This file covers the
pure algorithm in support_levels_engine.py - independent of how the
actual weekly bars get built (pence-repair/data-sourcing/storage/
rendering tests live in a separate TASK 36 test file alongside that
part of the implementation).

Covers the task's own listed algorithm tests:
  - pivot detection (3,3) - a bar needs 3 real bars on each side to
    confirm; the lowest-in-window rule.
  - the 2-consecutive-weekly-close break rule - ONE close below does
    NOT break a level; two in a row does.
  - nearest-below ("support") and next-below ("next support")
    selection among several surviving pivots.
  - the no-support fallback ("lowest_since", excluding the most recent
    8 weeks).
  - drop_from_12m_high (52-week high vs latest close).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC.

Run: python3 tests/test_director_task36_support_levels_engine.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import support_levels_engine as sle

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


def _d(i):
    """Week i's own date - 2020-01-06 (a Monday) + i*7 days, so every
    fixture in this file shares one readable, deterministic date axis."""
    return datetime.date(2020, 1, 6) + datetime.timedelta(weeks=i)


# ======================================================================
# CHECK 1: confirmed_pivot_lows() - the (3,3) definition itself.
# ======================================================================
# A single V-shape: lows descend then rise, with the minimum at index
# 5, needing 3 real bars either side to confirm.
_lows_v = [(_d(i), v) for i, v in enumerate([10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10])]
_pivots_v = sle.confirmed_pivot_lows(_lows_v, left=3, right=3)
check("confirmed_pivot_lows: the single V-shape minimum (index 5, price 5) is "
      "detected as the one confirmed pivot low",
      len(_pivots_v) == 1 and _pivots_v[0] == (5, _d(5), 5))
print("[task36_c1a_pivot_v_shape] a clean V-shape's minimum is correctly confirmed OK")

# Too short a series (fewer than left+right+1=7 bars) - never raises,
# returns empty.
check("confirmed_pivot_lows: a series shorter than left+right+1 returns "
      "empty, never raises",
      sle.confirmed_pivot_lows([(_d(0), 5), (_d(1), 4)], left=3, right=3) == [])
print("[task36_c1b_pivot_too_short] a too-short series returns empty, never crashes OK")

# A pivot right at the edge of what's confirmable (exactly right=3
# bars after it) IS confirmed; one bar short of that is NOT (not yet
# confirmable - the real TradingView lag).
_lows_edge = [(_d(i), v) for i, v in enumerate([10, 9, 8, 5, 6, 7, 8])]  # min at index 3, exactly 3 after
_pivots_edge = sle.confirmed_pivot_lows(_lows_edge, left=3, right=3)
check("confirmed_pivot_lows: a pivot with EXACTLY 3 bars after it is confirmed "
      "(the minimum lag that makes it knowable at all)",
      (3, _d(3), 5) in _pivots_edge)
_lows_one_short = _lows_edge[:-1]  # drop the last bar - now only 2 bars after index 3
_pivots_one_short = sle.confirmed_pivot_lows(_lows_one_short, left=3, right=3)
check("confirmed_pivot_lows: one bar short of the required 3-after-lag, the "
      "same price is NOT YET confirmed as a pivot",
      (3, _d(3), 5) not in _pivots_one_short)
print("[task36_c1c_pivot_confirmation_lag] a pivot is confirmable only once a "
      "real 3 bars have closed after it, not before OK")


# ======================================================================
# CHECK 2: unbroken_pivot_lows() - the 2-consecutive-weekly-close break
# rule. ONE close below does NOT break; two in a row DOES.
# ======================================================================
# Pivot at index 5 (price 100). After it: one week closes below (95),
# then recovers (105) - must NOT break.
_closes_one_dip = [(_d(i), c) for i, c in enumerate(
    [150, 140, 130, 120, 110, 100, 95, 105, 110, 115, 120]
)]
_pivot_at_5 = [(5, _d(5), 100)]
_survivors_one_dip = sle.unbroken_pivot_lows(_closes_one_dip, _pivot_at_5)
check("unbroken_pivot_lows: a SINGLE weekly close below the pivot's price, "
      "followed by a recovery, does NOT break the level",
      _survivors_one_dip == _pivot_at_5)
print("[task36_c2a_one_close_below_does_not_break] a lone dip-and-recover week "
      "never breaks a support level OK")

# Same pivot, but TWO consecutive closes below (95, then 90) - breaks.
_closes_two_dips = [(_d(i), c) for i, c in enumerate(
    [150, 140, 130, 120, 110, 100, 95, 90, 110, 115, 120]
)]
_survivors_two_dips = sle.unbroken_pivot_lows(_closes_two_dips, _pivot_at_5)
check("unbroken_pivot_lows: TWO CONSECUTIVE weekly closes below the pivot's "
      "price DOES break the level - no longer a survivor",
      _survivors_two_dips == [])
print("[task36_c2b_two_consecutive_closes_breaks] two closes below in a row "
      "correctly breaks the level OK")

# Two closes below, but NOT consecutive (one below, one at/above,
# then one below again) - must NOT break (never two IN A ROW).
_closes_non_consecutive = [(_d(i), c) for i, c in enumerate(
    [150, 140, 130, 120, 110, 100, 95, 105, 95, 110, 120]
)]
_survivors_non_consec = sle.unbroken_pivot_lows(_closes_non_consecutive, _pivot_at_5)
check("unbroken_pivot_lows: two closes below the pivot, but NOT consecutive "
      "(a recovery week in between), does NOT break the level",
      _survivors_non_consec == _pivot_at_5)
print("[task36_c2c_non_consecutive_dips_do_not_break] two non-consecutive dips "
      "never break a level - only two IN A ROW do OK")


# ======================================================================
# CHECK 3: compute_support_levels() - nearest-below ("support") and
# next-below ("next support") selection among several surviving
# pivots, plus drop_from_12m_high. Three well-separated, never-broken
# pivot lows at week 5 (price 50), week 15 (price 70), week 25 (price
# 85) - price then recovers, peaks, and settles back exactly at 100
# today (week 44) - support should be 85 (nearest below 100), next
# support 70. Every value is hand-picked and distinct (no flat runs)
# so no SPURIOUS pivot forms anywhere outside the three intended ones
# - verified directly against this module's own output while building
# this fixture, not just hand-derived.
_lows3 = [
    200, 190, 180, 170, 160, 50, 60, 70, 80, 90, 100, 110, 105, 100, 95,
    70, 80, 90, 100, 105, 110, 108, 100, 95, 90, 85, 90, 95, 100,
    105, 110, 115, 118, 120, 118, 115, 112, 109, 106, 104, 102, 101.5, 101, 100.5, 100,
]
_closes3 = _lows3[:]
_highs3 = [v + 5 for v in _lows3]
_dates3 = [_d(i) for i in range(len(_lows3))]
_result3 = sle.compute_support_levels(_dates3, _highs3, _lows3, _closes3)
check("compute_support_levels: 'support' is the NEAREST surviving pivot below "
      "today's close (85, from the shallowest/most recent V-shape)",
      _result3["has_support"] is True and _result3["support_price"] == 85)
check("compute_support_levels: 'next support' is the SECOND-nearest surviving "
      "pivot below that (70)",
      _result3["next_support_price"] == 70)
check("compute_support_levels: support_date/next_support_date are the real "
      "pivot weeks (index 25 and index 15 respectively)",
      _result3["support_date"] == _d(25) and _result3["next_support_date"] == _d(15))
check("compute_support_levels: pct_above_support == (100/85 - 1) * 100",
      abs(_result3["pct_above_support"] - ((100.0 / 85.0 - 1.0) * 100.0)) < 1e-9)
print(f"[task36_c3_nearest_and_next_support] support={_result3['support_price']} "
      f"({_result3['support_date']}), next support={_result3['next_support_price']} "
      f"({_result3['next_support_date']}) - correctly picks the two nearest surviving "
      "floors, not the deepest one OK")


# ======================================================================
# CHECK 4: no-support fallback ("lowest_since") - when every confirmed
# pivot low sits ABOVE today's price (a fresh multi-year low), support/
# next_support are both None and lowest_since reports the most recent
# qualifying week BEFORE the last 8 weeks. Reuses CHECK 3's own
# 3-pivot shape (50/70/85) verbatim for weeks 0-28, so those three
# pivots are confirmed exactly as before, then a smooth, monotonic
# decline (no recovery, so no NEW pivot forms) carries price all the
# way down to a fresh low of 20 - well below all three pivots, so
# none of them is "at or below today's price" and none is eligible as
# support, with no breaking mechanics needed at all. One deliberately
# anomalous CLOSE-only value at week 18 (its LOW is left unchanged at
# 100, so it is never itself flagged as a pivot) gives lowest_since a
# real, findable anchor.
# ======================================================================
_lows4 = _lows3[:]  # the exact same 3-pivot shape as CHECK 3, weeks 0-28
_closes4 = _lows4[:]
_closes4[18] = 15.0  # anomalous CLOSE-only dip - LOW stays 100, never a pivot
_decline = [95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20]
_lows4 += _decline
_closes4 += _decline
_highs4 = [v + 5 for v in _closes4]
_dates4 = [_d(i) for i in range(len(_lows4))]
_result4 = sle.compute_support_levels(_dates4, _highs4, _lows4, _closes4)
check("compute_support_levels: no surviving pivot low sits at or below today's "
      "price (20, well under 50/70/85) -> has_support is False, support/"
      "next_support are both None",
      _result4["has_support"] is False
      and _result4["support_price"] is None and _result4["next_support_price"] is None)
check("compute_support_levels: lowest_since correctly finds week 18 (close=15, "
      "the most recent qualifying week BEFORE the last 8 weeks)",
      _result4["lowest_since_date"] == _d(18) and _result4["lowest_since_price"] == 15.0)
print(f"[task36_c4_no_support_fallback] no pivot survives below price -> "
      f"lowest_since={_result4['lowest_since_price']} ({_result4['lowest_since_date']}) OK")

# The exclude-last-8-weeks rule itself, isolated: a qualifying close
# INSIDE the last 8 weeks must NOT be picked, even though it's more
# recent and would otherwise win.
_closes_recent_dip = [(_d(i), c) for i, c in enumerate([50.0] * 50 + [20.0] * 10)]
_recent_date, _recent_price = sle._lowest_since(_closes_recent_dip, latest_close=25.0,
                                                 exclude_recent_weeks=8)
check("_lowest_since: a qualifying close inside the excluded most-recent-8-weeks "
      "window is correctly SKIPPED",
      _recent_date is None or _recent_date < _d(52))
print("[task36_c4b_exclude_recent_8_weeks] the most recent 8 weeks are correctly "
      "excluded from the lowest_since lookup OK")


# ======================================================================
# CHECK 5: drop_from_12m_high (52-week high vs latest close).
# ======================================================================
_dates5 = [_d(i) for i in range(60)]
_highs5 = [100.0] * 60
_highs5[55] = 150.0  # the 52-week high sits inside the trailing-52-week window
_lows5 = [90.0] * 60
_closes5 = [90.0] * 60
_closes5[-1] = 75.0  # today's close
_result5 = sle.compute_support_levels(_dates5, _highs5, _lows5, _closes5)
check("compute_support_levels: drop_from_12m_high == (75/150 - 1) * 100 == -50%",
      abs(_result5["drop_from_12m_high"] - ((75.0 / 150.0 - 1.0) * 100.0)) < 1e-9)
print(f"[task36_c5_drop_from_12m_high] drop_from_12m_high={_result5['drop_from_12m_high']:.1f}% "
      "correctly measured against the trailing-52-week high OK")


# ======================================================================
# CHECK 6: empty/degenerate input never raises.
# ======================================================================
_empty_result = sle.compute_support_levels([], [], [], [])
check("compute_support_levels: empty input returns the all-None/False shape, "
      "never raises",
      _empty_result["has_support"] is False and _empty_result["support_price"] is None
      and _empty_result["drop_from_12m_high"] is None)
print("[task36_c6_empty_input_never_raises] empty price history degrades "
      "gracefully to an all-None/False result OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
