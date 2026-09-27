"""Batch B2, item 2 (27 Sep 2026, owner-directed, CONFIRMED BUG): the
Retained Earnings "Value Created" windows (_value_created() in auto_
compounder_engine.py) paired mismatched periods two ways:
1. A fixed-FY horizon (2Y/5Y/10Y) summed retained earnings over N
   fiscal years but measured price change over only N-1 of them
   (anchored at the oldest INCLUDED year's own year-end, not the
   year-end BEFORE it).
2. The TTM-based horizons always added retained_ttm on top of a
   fiscal-year sum that could already include the same period TTM
   falls back to when it doesn't have genuinely fresher quarterly data
   than the latest annual column - double-counting one real year of
   earnings as two.

Reproduces each bug first (proves the FIXED values differ from what
the OLD code would have produced), then proves the fix.
Run: python3 test_b2_2_value_created_periods.py
"""
import datetime
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace

# 11 fiscal years, newest first: 2026 down to 2016 - enough depth for the
# full-workbook branch (n_avail >= 10) AND one extra anchor year (2016)
# for the TTM window's own year-before check.
YEARS = [str(y) for y in range(2026, 2015, -1)]  # "2026" .. "2016", 11 labels
EPS_BY_YEAR = {y: 10.0 for y in YEARS}  # flat $10 EPS/yr, no dividends -> retained = eps count
YEAR_END_PRICE = {y: 100.0 + (2026 - int(y)) * 10.0 for y in YEARS}  # 2026:100, 2025:110, ... 2016:200
# (prices DECREASE as years get more recent in this table's construction
#  is irrelevant - what matters is each year has a DISTINCT, known price
#  so an off-by-one anchor is immediately visible in the assertions.)

FAKE_INCOME_DF = object()  # opaque - _statement_col_dates is mocked directly below
FY_DATES = {y: datetime.date(int(y), 6, 30) for y in YEARS}


def _fake_eps_series(bundle):
    return [(y, EPS_BY_YEAR[y]) for y in YEARS]


def _fake_year_end_prices(prices_10y, statement_df=None):
    return dict(YEAR_END_PRICE)


def _fake_col_dates(df):
    if df is FAKE_INCOME_DF:
        return [(y, FY_DATES[y]) for y in YEARS]
    return []  # income_q - irrelevant here, _ttm_overlaps_latest_fy is mocked directly


BUNDLE = {"income": FAKE_INCOME_DF, "dividends": {"dates": [], "amounts": []}, "prices_10y": {}}


def _run(retained_ttm, price_now, ttm_overlaps):
    with mock.patch.object(ace, "_eps_series", side_effect=_fake_eps_series), \
         mock.patch.object(ace, "_year_end_prices", side_effect=_fake_year_end_prices), \
         mock.patch.object(ace, "_statement_col_dates", side_effect=_fake_col_dates), \
         mock.patch.object(ace, "_ttm_overlaps_latest_fy", return_value=ttm_overlaps):
        return ace._value_created(BUNDLE, retained_ttm, price_now)


# ---- Bug 1: fixed-FY window (2Y) price anchor ----
result = _run(retained_ttm=None, price_now=None, ttm_overlaps=False)
two_y = result["2Y"]
# Retained earnings: 2 years (2026, 2025) of $10 EPS, no dividends = 20.0.
assert two_y["retained_earnings"] == 20.0
# The OLD (buggy) code would have anchored start_price at 2025's own
# year-end (110.0), giving value_created = (100.0 - 110.0) / 20.0 = -0.5.
_old_buggy_value_created = (YEAR_END_PRICE["2026"] - YEAR_END_PRICE["2025"]) / 20.0
# The FIX anchors at the year BEFORE 2025 (i.e. 2024's year-end, 120.0):
_correct_value_created = (YEAR_END_PRICE["2026"] - YEAR_END_PRICE["2024"]) / 20.0
assert two_y["value_created"] != _old_buggy_value_created
assert abs(two_y["value_created"] - _correct_value_created) < 1e-9
assert "2024" in two_y["window"]  # window caption now correctly names the true start year
print("[fy_window_price_anchor_fixed] a 2Y window's price now spans the SAME 2 years its "
      f"retained-earnings sum covers (2024->2026, {_correct_value_created:.4f}), not the old "
      f"1-year-short anchor (2025->2026, {_old_buggy_value_created:.4f}) OK")

# ---- Bug 1 boundary: a window with NO year before it can't render (no faking) ----
with mock.patch.object(ace, "_eps_series", side_effect=lambda b: [(y, EPS_BY_YEAR[y]) for y in YEARS[:2]]), \
     mock.patch.object(ace, "_year_end_prices", side_effect=_fake_year_end_prices), \
     mock.patch.object(ace, "_statement_col_dates", side_effect=lambda df: [(y, FY_DATES[y]) for y in YEARS[:2]] if df is FAKE_INCOME_DF else []), \
     mock.patch.object(ace, "_ttm_overlaps_latest_fy", return_value=False):
    tiny_result = ace._value_created(BUNDLE, None, None)
assert tiny_result is None or "2Y" not in tiny_result  # exactly 2 years available -> no anchor year exists
print("[fy_window_no_anchor_year] exactly N years of history (no year before the window) -> "
      "that window is omitted rather than faked with a wrong anchor OK")

# ---- Bug 2: TTM term dropped when it overlaps the latest fiscal year ----
result_overlap = _run(retained_ttm=10.0, price_now=500.0, ttm_overlaps=True)
result_fresh = _run(retained_ttm=10.0, price_now=500.0, ttm_overlaps=False)
ttm_slice_sum = 10.0 * 10  # 10 years of $10 EPS, no dividends
assert result_overlap["TTM"]["retained_earnings"] == ttm_slice_sum  # retained_ttm term DROPPED
assert result_fresh["TTM"]["retained_earnings"] == ttm_slice_sum + 10.0  # retained_ttm term KEPT
print("[ttm_term_dropped_on_overlap] when TTM overlaps the latest fiscal year, retained_ttm is "
      "excluded from the sum (100.0, not 110.0) - genuinely fresh TTM data still adds its own "
      "term on top (110.0) OK")

# TTM window's own price anchor also uses the year BEFORE ttm_slice's oldest year (2016, since
# ttm_slice is 2026..2017 - 10 years - and eps_series[10] is 2016).
_ttm_correct_start = YEAR_END_PRICE["2016"]
assert abs(result_fresh["TTM"]["value_created"] - (500.0 - _ttm_correct_start) / (ttm_slice_sum + 10.0)) < 1e-9
assert "2016" in result_fresh["TTM"]["window"]
print("[ttm_window_price_anchor_fixed] the TTM window's start_price is anchored at the year "
      "BEFORE its own 10-FY slice (2016), the same off-by-one fix as the fixed-FY windows OK")


print("\nALL B2.2 VALUE-CREATED-PERIODS FIXTURES PASSED")
