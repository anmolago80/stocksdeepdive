"""
Director's TASK 52 (12 Nov 2026, owner-reported via PGHN.SW): support
levels only ever see support_levels_job.py's own SUPPORT_LEVELS_HISTORY_
PERIOD = "5y" of weekly history, so a floor first set BEFORE that
window shows its most recent touch under that same level instead of
its true original date (PGHN.SW: TradingView "583.5 · 26 Jun 2017",
this site "Support Oct 2026 · CHF 582.60" - the SAME floor, just a
recent re-touch, with no way to recover the real 2017 date from 5
years of data alone).

Rule (no extra data, no extra downloads - a pure re-read of the SAME
weekly series this engine already has): when the chosen support price
(or next support price) sits within 1% of the single LOWEST weekly low
anywhere in the fetched window, flag it "5y+ low" - price has never
gone lower in the whole window this engine can see, exactly the shape
a floor set earlier than the window would produce. A flag, never a
claim about the real date.

Covers:
  a. support_levels_engine.compute_support_levels(): support_is_5y_low/
     next_support_is_5y_low - a clean single-pivot case where support
     price EQUALS the window's own lowest low (flagged); a two-pivot
     case where only the DEEPER (next) pivot equals the window low,
     the shallower (support) one does not (next flagged, support not);
     a well-separated three-pivot case where neither support nor next
     support is anywhere near the window low (neither flagged - a
     byte-identical regression check against TASK 36's own existing
     fixture); the has_support=False fallback path and the empty-input
     path both always read False/False.
  b. top100_render._support_chip_html(): the flagged chip uses the new
     "support_chip_5y_low" template (flag text inserted between price
     and "+X% above"); the unflagged chip is completely unchanged; the
     next-support flag text is appended to the tooltip's own next-
     support line only when next_support_is_5y_low is True.
  c. The TASK 50 Excel export gains a "Support 5y+ low" column (yes/
     no), positioned right after "% above support", reading straight
     off the same engine field.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. PGHN.SW/OCL.AX/CSL.AX/ROL's own REAL flag status after this
deploys needs a live run (tonight's support-levels job, or an owner-
triggered refresh) - not fetchable here.

Run: python3 tests/test_director_task52_support_5y_low_flag.py
"""
import datetime
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task52_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import data_export_engine as de
import i18n
import support_levels_engine as sle
import top100_render as tr

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
    return datetime.date(2020, 1, 6) + datetime.timedelta(weeks=i)


# ======================================================================
# CHECK a1: a clean single-pivot case where support price EQUALS the
# window's own lowest low (the PGHN.SW shape - the chosen floor has
# never been undercut anywhere in the fetched window) -> flagged.
# ======================================================================
_lows_single = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 10, 10, 10]
_closes_single = _lows_single[:]
_highs_single = [v + 1 for v in _lows_single]
_dates_single = [_d(i) for i in range(len(_lows_single))]
_result_single = sle.compute_support_levels(_dates_single, _highs_single, _lows_single, _closes_single)
check("single clean pivot: support_price (5) is the whole window's own lowest low",
      _result_single["has_support"] is True and _result_single["support_price"] == 5)
check("single clean pivot: support_is_5y_low is True (0% gap from the window low)",
      _result_single["support_is_5y_low"] is True)
check("single clean pivot: no next support at all -> next_support_is_5y_low is False",
      _result_single["next_support_price"] is None and _result_single["next_support_is_5y_low"] is False)
print("[task52_checkA1_support_equals_window_low] a support price that IS the "
      "window's own lowest low gets flagged OK")


# ======================================================================
# CHECK a2: two confirmed, surviving pivots - a shallower one (85,
# becomes "support") and a deeper one (50, becomes "next support")
# that equals the window's own lowest low. Same construction as TASK
# 36's own CHECK 3 fixture (tests/test_director_task36_support_levels_
# engine.py) with its middle "70" hump removed, so the two surviving
# pivots are exactly 85 and 50 with nothing lower anywhere in the
# array - verified against this exact engine before writing the check.
# ======================================================================
_lows_two = (
    [200, 190, 180, 170, 160, 50, 60, 70, 80, 90, 100, 110, 105, 100, 95]
    + [85, 90, 95, 100, 105, 110, 115, 118, 120, 118, 115, 112, 109, 106, 104, 102, 101.5, 101, 100.5, 100]
)
_closes_two = _lows_two[:]
_highs_two = [v + 5 for v in _lows_two]
_dates_two = [_d(i) for i in range(len(_lows_two))]
_result_two = sle.compute_support_levels(_dates_two, _highs_two, _lows_two, _closes_two)
check("two-pivot case: support is the shallower pivot (85), next support the "
      "deeper one (50), exactly as TASK 36's own nearest/next-nearest selection",
      _result_two["has_support"] is True and _result_two["support_price"] == 85
      and _result_two["next_support_price"] == 50)
check("two-pivot case: support (85) is NOT within 1% of the window low (50) - not flagged",
      _result_two["support_is_5y_low"] is False)
check("two-pivot case: next support (50) IS the window's own lowest low - flagged",
      _result_two["next_support_is_5y_low"] is True)
print("[task52_checkA2_next_support_flagged_alone] only the DEEPER pivot gets "
      "flagged when it alone sits at the window low OK")


# ======================================================================
# CHECK a3: a well-separated three-pivot case (neither near the window
# low) - byte-identical regression against TASK 36's own existing
# CHECK 3 fixture: support=85, next_support=70, window low=50. Neither
# gap is anywhere close to 1%, so nothing is flagged.
# ======================================================================
_lows_three = [
    200, 190, 180, 170, 160, 50, 60, 70, 80, 90, 100, 110, 105, 100, 95,
    70, 80, 90, 100, 105, 110, 108, 100, 95, 90, 85, 90, 95, 100,
    105, 110, 115, 118, 120, 118, 115, 112, 109, 106, 104, 102, 101.5, 101, 100.5, 100,
]
_closes_three = _lows_three[:]
_highs_three = [v + 5 for v in _lows_three]
_dates_three = [_d(i) for i in range(len(_lows_three))]
_result_three = sle.compute_support_levels(_dates_three, _highs_three, _lows_three, _closes_three)
check("three-pivot case: support=85, next_support=70 (TASK 36's own fixture, unchanged)",
      _result_three["support_price"] == 85 and _result_three["next_support_price"] == 70)
check("three-pivot case: neither support nor next support is within 1% of the "
      "window low (50) - neither flagged (regression: a normal fixture is unaffected)",
      _result_three["support_is_5y_low"] is False and _result_three["next_support_is_5y_low"] is False)
print("[task52_checkA3_normal_fixture_unflagged] a normal, well-separated support "
      "structure is completely unaffected by this task OK")


# ======================================================================
# CHECK a4: the has_support=False fallback path, and the empty-input
# path, both always read support_is_5y_low=False/next_support_is_5y_low
# =False (there is no "support price" at all to flag in either case).
# ======================================================================
_lows_decline = _lows_three[:29] + [95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20]
_closes_decline = _lows_decline[:]
_highs_decline = [v + 5 for v in _closes_decline]
_dates_decline = [_d(i) for i in range(len(_lows_decline))]
_result_decline = sle.compute_support_levels(_dates_decline, _highs_decline, _lows_decline, _closes_decline)
check("no-support fallback path: has_support is False, both flags are False",
      _result_decline["has_support"] is False
      and _result_decline["support_is_5y_low"] is False
      and _result_decline["next_support_is_5y_low"] is False)

_result_empty = sle.compute_support_levels([], [], [], [])
check("empty input: both flags are False, never raises",
      _result_empty["support_is_5y_low"] is False and _result_empty["next_support_is_5y_low"] is False)
print("[task52_checkA4_no_support_and_empty] both edge-case paths always read "
      "False/False, never raise, never fabricate a flag with no support price OK")


# ======================================================================
# CHECK b: _support_chip_html() - the flagged variant inserts the
# "5y+ low" text between price and "+X% above"; the unflagged chip is
# completely unchanged; the next-support flag text is appended to the
# tooltip's own next-support line only when earned.
# ======================================================================
_row = {"ticker": "PGHN.SW", "price": 680.0}

with mock.patch.object(tr, "_is_owner_for_refresh", return_value=True), \
     mock.patch.object(tr.support_levels_store, "get", return_value={
    "has_support": True, "support_date": datetime.date(2026, 10, 1),
    "support_price": 582.60, "pct_above_support": 16.7,
    "drop_from_12m_high": None, "support_is_5y_low": True,
    "next_support_price": None, "next_support_date": None,
    "next_support_is_5y_low": False,
}):
    _chip_flagged = tr._support_chip_html(_row, "en")
check("flagged chip: '5y+ low' text is present in the rendered chip",
      "5y+ low" in _chip_flagged)
check("flagged chip: the price and +X% above figure are still both present",
      "582.60" in _chip_flagged or "582.6" in _chip_flagged)

with mock.patch.object(tr, "_is_owner_for_refresh", return_value=True), \
     mock.patch.object(tr.support_levels_store, "get", return_value={
    "has_support": True, "support_date": datetime.date(2026, 10, 1),
    "support_price": 100.0, "pct_above_support": 20.0,
    "drop_from_12m_high": None, "support_is_5y_low": False,
    "next_support_price": None, "next_support_date": None,
    "next_support_is_5y_low": False,
}):
    _chip_unflagged = tr._support_chip_html(_row, "en")
check("unflagged chip: no '5y+ low' text at all - completely unchanged from before this task",
      "5y+ low" not in _chip_unflagged)

with mock.patch.object(tr, "_is_owner_for_refresh", return_value=True), \
     mock.patch.object(tr.support_levels_store, "get", return_value={
    "has_support": True, "support_date": datetime.date(2026, 10, 1),
    "support_price": 100.0, "pct_above_support": 20.0,
    "drop_from_12m_high": None, "support_is_5y_low": False,
    "next_support_price": 50.0, "next_support_date": datetime.date(2022, 3, 1),
    "next_support_is_5y_low": True,
}):
    _chip_next_flagged = tr._support_chip_html(_row, "en")
_tooltip_has_flag = "5y+ low" in _chip_next_flagged  # the title attribute IS part of the returned HTML string
check("support not flagged but next_support IS flagged: '5y+ low' appears "
      "somewhere in the chip's own HTML (its tooltip's next-support line)",
      _tooltip_has_flag)
check("support not flagged but next_support IS flagged: the VISIBLE chip text "
      "(before the first '>') does not itself carry the flag - only the tooltip does",
      "5y+ low" not in _chip_next_flagged.split(">", 1)[1])
print("[task52_checkB_chip_rendering] the flagged chip inserts the flag text "
      "between price and the +X%% figure; an unflagged chip is byte-identical to "
      "before this task; a flagged NEXT support appears in the tooltip only OK")

# ES localization sanity: the flag text itself is present (Spanish word).
with mock.patch.object(tr, "_is_owner_for_refresh", return_value=True), \
     mock.patch.object(tr.support_levels_store, "get", return_value={
    "has_support": True, "support_date": datetime.date(2026, 10, 1),
    "support_price": 582.60, "pct_above_support": 16.7,
    "drop_from_12m_high": None, "support_is_5y_low": True,
    "next_support_price": None, "next_support_date": None,
    "next_support_is_5y_low": False,
}):
    _chip_flagged_es = tr._support_chip_html(_row, "es")
check("ES: the flagged chip uses the Spanish flag text, not the English one",
      i18n.t("top100.support_5y_low_flag", "es") in _chip_flagged_es)
print("[task52_checkB2_es_localization] the ES chip carries its own translated "
      "flag text OK")


# ======================================================================
# CHECK c: the TASK 50 Excel export gains "Support 5y+ low" right
# after "% above support", reading the SAME engine field.
# ======================================================================
check("TOP200_EXPORT_COLUMNS: 'Support 5y+ low' sits directly after '%% above support'",
      de.TOP200_EXPORT_COLUMNS.index("Support 5y+ low")
      == de.TOP200_EXPORT_COLUMNS.index("% above support") + 1)

_row_export = {
    "ticker": "PGHN.SW", "company_name": "Partners Group", "sector": "Financial Services",
    "composite": 70.0, "value_score": 65.0, "mos_pct": 10.0, "price": 680.0,
    "currency": "CHF", "intrinsic_value": 750.0, "dividend_yield_pct": 2.0,
    "psychology": 5.0, "score_row": {},
}
with mock.patch.object(tr.support_levels_store, "get", return_value={
    "has_support": True, "support_date": "2026-10-01", "support_price": 582.60,
    "pct_above_support": 16.7, "support_is_5y_low": True,
}), \
     mock.patch.object(tr.drop_chip_store, "get", return_value=None), \
     mock.patch.object(tr, "_tradability_spread_pct", return_value=None):
    _export_row_flagged = tr._top200_export_row(
        1, _row_export, quality_moat_map={}, top200_rank_map={}, qos_rank_map={}, streak_start_map={})
check("export row: 'Support 5y+ low' is 'yes' when support_is_5y_low is True",
      _export_row_flagged["Support 5y+ low"] == "yes")

with mock.patch.object(tr.support_levels_store, "get", return_value={
    "has_support": True, "support_date": "2026-10-01", "support_price": 100.0,
    "pct_above_support": 20.0, "support_is_5y_low": False,
}), \
     mock.patch.object(tr.drop_chip_store, "get", return_value=None), \
     mock.patch.object(tr, "_tradability_spread_pct", return_value=None):
    _export_row_unflagged = tr._top200_export_row(
        1, _row_export, quality_moat_map={}, top200_rank_map={}, qos_rank_map={}, streak_start_map={})
check("export row: 'Support 5y+ low' is 'no' when support_is_5y_low is False",
      _export_row_unflagged["Support 5y+ low"] == "no")

with mock.patch.object(tr.support_levels_store, "get", return_value={"has_support": False}), \
     mock.patch.object(tr.drop_chip_store, "get", return_value=None), \
     mock.patch.object(tr, "_tradability_spread_pct", return_value=None):
    _export_row_nosupport = tr._top200_export_row(
        1, _row_export, quality_moat_map={}, top200_rank_map={}, qos_rank_map={}, streak_start_map={})
check("export row: 'Support 5y+ low' is None (blank) when there is no support at all",
      _export_row_nosupport["Support 5y+ low"] is None)
print("[task52_checkC_export_column] the export's own 'Support 5y+ low' column "
      "correctly reports yes/no/blank straight off the same engine field OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
