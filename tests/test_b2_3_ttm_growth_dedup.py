"""Batch B2, item 3 (27 Sep 2026, owner-directed, CONFIRMED BUG): TTM EPS
duplicates the latest fiscal year in growth averages.

_build_earnings_trends() in auto_compounder_engine.py prepends a "TTM"
point to full_eps and used it directly for BOTH the display series
(EPS chart, P/E chart - fine, still informative) AND every growth-
derived figure (the YoY eps_growth series, "Average 10 Year Growth",
"10Y Growth (3Y AVG)"). Whenever _eps_ttm() has no genuinely fresher
quarterly data than the latest annual column (_ttm_overlaps_latest_fy()
- the exact same condition B2.2 already introduced this helper for),
"TTM" is a fallback read of the SAME period the latest fiscal year
already covers - counting both as independent growth-series points
double-counts one real year of earnings as two, dragging any average
of the growth series toward zero.

Reproduces the bug first (the OLD code's growth average, computed with
the TTM-vs-lastFY point included, differs from - and is wrong relative
to - the fiscal-years-only figure), then proves the fix.
Run: python3 test_b2_3_ttm_growth_dedup.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace

# 5 fiscal years, newest first, with a DELIBERATELY uneven growth
# pattern so a TTM-vs-lastFY point (comparing $12.00 against itself)
# would visibly skew the average if wrongly included.
FY_EPS = [("2026", 12.00), ("2025", 10.00), ("2024", 8.00), ("2023", 8.50), ("2022", 7.00)]
YEAR_END_PRICE = {y: 100.0 for y, _ in FY_EPS}

BUNDLE = {
    "income": object(), "income_q": object(), "dividends": {"dates": [], "amounts": []},
    "prices_10y": {}, "info": {"currentPrice": 150.0},
}


def _fake_eps_series(bundle):
    return FY_EPS


def _fake_year_end_prices(prices_10y, statement_df=None):
    return dict(YEAR_END_PRICE)


def _run(ttm_value, ttm_overlaps):
    with mock.patch.object(ace, "_eps_series", side_effect=_fake_eps_series), \
         mock.patch.object(ace, "_eps_ttm", return_value=(ttm_value, False)), \
         mock.patch.object(ace, "_year_end_prices", side_effect=_fake_year_end_prices), \
         mock.patch.object(ace, "_ttm_overlaps_latest_fy", return_value=ttm_overlaps), \
         mock.patch.object(ace, "_bvps", return_value=None), \
         mock.patch.object(ace, "_avg_pe_3pt", return_value=None), \
         mock.patch.object(ace, "_pe_avg_3y_by_eps", return_value=None):
        result = ace._build_earnings_trends(BUNDLE, "TEST", ref={})
        return result["metrics"], result["series"]["TEST"]


# ---- TTM overlaps the latest FY: the fallback TTM value repeats 2026's $12.00 ----
metrics_overlap, series_overlap = _run(ttm_value=12.00, ttm_overlaps=True)
growth_years_overlap = series_overlap["eps_growth"]["years"]
assert "TTM" not in growth_years_overlap  # the TTM-vs-2026 comparison point is gone
_expected_fy_only_growth = [
    (12.00 - 10.00) / 10.00,  # 2026 vs 2025
    (10.00 - 8.00) / 8.00,    # 2025 vs 2024
    (8.00 - 8.50) / 8.50,     # 2024 vs 2023
    (8.50 - 7.00) / 7.00,     # 2023 vs 2022
]
assert series_overlap["eps_growth"]["values"] == _expected_fy_only_growth
print("[eps_growth_excludes_overlapping_ttm] the TTM-vs-latest-FY comparison point is entirely "
      "absent from the growth series when TTM overlaps the latest fiscal year - only genuine "
      "YoY fiscal-year comparisons remain OK")

_avg_metric_overlap = next(m for m in metrics_overlap if "Average" in m["label"] and "Growth" in m["label"])
_correct_avg = sum(_expected_fy_only_growth) / len(_expected_fy_only_growth)
# Reproduces the bug: the OLD code would have included one more point
# (TTM $12.00 vs 2026 $12.00 = 0.0 growth), pulling the average DOWN.
_old_buggy_growth_vals = [0.0] + _expected_fy_only_growth  # TTM-vs-lastFY point = 0% growth
_old_buggy_avg = sum(_old_buggy_growth_vals) / len(_old_buggy_growth_vals)
_avg_value = _avg_metric_overlap["values"]["TEST"]
assert _avg_value != _old_buggy_avg
assert abs(_avg_value - _correct_avg) < 1e-9
print(f"[average_growth_not_dragged_down] Average Growth is the correct {_correct_avg:.4f} "
      f"(4 genuine YoY comparisons), not the old buggy {_old_buggy_avg:.4f} that an extra "
      "0%-growth TTM-vs-itself point would have produced OK")

# ---- TTM genuinely fresher than the latest FY: it DOES count as an extra point ----
metrics_fresh, series_fresh = _run(ttm_value=13.50, ttm_overlaps=False)
growth_years_fresh = series_fresh["eps_growth"]["years"]
assert "TTM" in growth_years_fresh  # a genuinely fresh TTM point IS included
_ttm_growth = (13.50 - 12.00) / 12.00
assert series_fresh["eps_growth"]["values"][0] == _ttm_growth
print("[fresh_ttm_still_counts] when TTM genuinely has fresher quarterly data than the latest "
      "FY, its own growth comparison point IS included, exactly as before this fix OK")

# ---- Display series (EPS/P/E charts) are UNCHANGED - TTM still shown there ----
assert "TTM" in series_overlap["eps"]["years"]
print("[display_series_unaffected] the raw EPS chart still shows the TTM point even when it "
      "duplicates the latest FY - only GROWTH averages exclude it, per the instruction's own "
      "scope OK")


print("\nALL B2.3 TTM-GROWTH-DEDUP FIXTURES PASSED")
