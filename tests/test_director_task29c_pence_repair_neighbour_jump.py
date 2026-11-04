"""
TASK 29c (Director-directed, 4 Nov 2026, real incident - HSBA.L):
the pence repair misses a large historic price move.

EVIDENCE (owner screenshot, deep-dive HSBA.L Fundamentals): 10y price
chart ~5-13 GBP except mid-2020 to end-2020 where it jumps to ~300-370
and back; "10y Average GBP 23.73" (should be ~6-8).

ROOT CAUSE (verified in code): repair_mixed_unit_price_series()/
_dataframe() only rescaled a point whose ratio to the CURRENT price
fell inside _PENCE_HISTORY_REPAIR_RATIO_RANGE (30x-300x). HSBA.L is
now ~GBP 12.7; its 2020 pence closes (~300-370p) are only ~24-29x the
current price - UNDER the 30x floor - so they were left untouched.
Any ".L" ticker whose price has moved enough since a pence segment has
the same gap, not just HSBA.L.

FIX: a second, complementary detection pass, additive to the existing
reference-price check (never replacing it): walk the series for a
consecutive-pair ratio inside _PENCE_HISTORY_NEIGHBOUR_JUMP_RATIO_
RANGE (50x-200x, around the literal x100 unit-conversion factor) -
Yahoo's own unit switch is a flat data artifact, not a real trading
event, so two ADJACENT days straddling it have nearly identical REAL
prices regardless of how far either side has since drifted from
today's price. Each segment bordered by such a jump is then rescaled
as a WHOLE (never split point-by-point by its own internal noise),
decided purely by which side - divided or not - sits closer to the
reference price in log-space. A series with NO detected jump anywhere
is a complete no-op for this second pass - it defers entirely to the
existing reference-price check, never judges the whole series as "one
segment" against the reference (that was tried and reverted during
this task's own development: it mis-fired on the pre-existing ratio-
boundary regression fixture, re-touching a point the reference check
deliberately leaves alone as a genuine price).

Applies automatically everywhere TASK 29/29b/28 already call the
shared repair functions (fundamentals_data.get_bundle()'s own
prices_10y, normalize_pence_quote()'s history_df - shared by nightly_
scan.analyze_ticker_lite()/app.py's Deep Dive headline/api_v1.py/
portfolio_*, nightly_scan._reprice_row()'s close_series, app.py's Deep
Dive 6-month chart's own independent re-fetch, compounder_ui.py's
Trading Cost tab bid/ask/last normalisation) - no call site itself
needed to change, confirmed by this task's own diff touching only
fundamentals_data.py plus a BUNDLE_VERSION bump.

OUT OF SCOPE (per the task's own instruction): no change to Top 200 /
Top 20 Australia selection or ranking - confirmed by this task's own
diff never touching top100_engine.py/top100_store.py.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task29c_pence_repair_neighbour_jump.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task29c_neighbour_jump_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import fundamentals_data as fd

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


# ======================================================================
# Shared HSBA.L-shaped fixture: real price glides from ~GBP 8 down to
# ~GBP 3.3 through 2020 (reported by Yahoo in PENCE the whole time -
# raw ~800 down to ~310), then back up to today's ~GBP 12.7 (reported
# in POUNDS once the series switches units) - the island's OWN closing
# values are all only ~24-29x today's price (24.4-29.1x), UNDER the
# existing 30x floor, so PASS 1 (reference-price check) catches none
# of them; only PASS 2 (neighbour-jump) can.
# ======================================================================
_HSBA_REFERENCE = 12.7
_HSBA_PRE_ISLAND = [8.0, 6.0, 4.0, 3.5]
_HSBA_ISLAND = [350.0, 330.0, 300.0, 320.0, 360.0, 370.0, 310.0]  # raw pence, 24.4-29.1x reference
_HSBA_POST_ISLAND = [3.1, 4.0, 5.0, 7.0, 9.0, 11.0, 12.7]
_HSBA_SERIES = _HSBA_PRE_ISLAND + _HSBA_ISLAND + _HSBA_POST_ISLAND  # 18 points


# ======================================================================
# T1 (the fix itself): repair_mixed_unit_price_series() on the
# HSBA.L-shaped fixture - the whole island is rescaled flat into GBP,
# the 10y average lands in the sane ~6-8 range the task's own evidence
# names, and the log line counts every point from BOTH passes.
# ======================================================================
_log1 = []
_repaired1 = fd.repair_mixed_unit_price_series(
    _HSBA_SERIES, _HSBA_REFERENCE, ticker="HSBA.L", log=_log1.append
)
check("FIX: every point in the repaired series sits in a sane GBP "
      "range (2-16) - no 300-370 spike left in the output",
      all(2.0 <= p <= 16.0 for p in _repaired1))
check("FIX: the island's own 7 points are all now close to their "
      "true GBP value (3.0-3.7), not the raw 300-370 pence figures",
      all(2.9 <= p <= 3.8 for p in _repaired1[4:11]))
_avg1 = sum(_repaired1) / len(_repaired1)
check(f"FIX: the 10y average ({_avg1:.2f}) is sane (4-8), not the "
      f"live incident's own broken 'GBP 23.73'",
      4.0 <= _avg1 <= 8.0)
check('FIX: the "[units] HSBA.L history repaired: 7 of 18 points..." '
      "log line fires, counting the island's own 7 points",
      _log1 == ["[units] HSBA.L history repaired: 7 of 18 points "
                "rescaled pence->GBP"])
print(f"[task29c_t1_hsba_series_repair] HSBA.L-shaped series (2020 "
      f"island only 24-29x today's price) fully repaired, 10y avg="
      f"{_avg1:.2f} OK")


# ======================================================================
# T2: repair_mixed_unit_price_dataframe() on the same shape, OHLC -
# every column moves together per-row, exactly like PASS 1 already
# guarantees.
# ======================================================================
_idx = pd.date_range("2019-01-01", periods=len(_HSBA_SERIES), freq="MS")
_hsba_df = pd.DataFrame({
    "Open": [c * 0.995 for c in _HSBA_SERIES], "High": [c * 1.01 for c in _HSBA_SERIES],
    "Low": [c * 0.99 for c in _HSBA_SERIES], "Close": _HSBA_SERIES,
}, index=_idx)
_log2 = []
_repaired_df2 = fd.repair_mixed_unit_price_dataframe(
    _hsba_df, _HSBA_REFERENCE, ticker="HSBA.L", log=_log2.append
)
check("FIX: the dataframe's Close column is fully repaired, sane "
      "throughout (2-16)",
      all(2.0 <= c <= 16.0 for c in _repaired_df2["Close"]))
check("FIX: every OHLC column for the island's own rows moved "
      "together (Open/Close ratio preserved exactly)",
      all(abs(_repaired_df2["Open"].iloc[i] / _repaired_df2["Close"].iloc[i]
              - _hsba_df["Open"].iloc[i] / _hsba_df["Close"].iloc[i]) < 1e-9
          for i in range(len(_HSBA_SERIES))))
check('FIX: the dataframe path logs the same "7 of 18" line',
      _log2 == ["[units] HSBA.L history repaired: 7 of 18 points "
                "rescaled pence->GBP"])
print("[task29c_t2_hsba_dataframe_repair] HSBA.L-shaped OHLC frame "
      "fully repaired, every column moves together per-row OK")


# ======================================================================
# T3 (regression guard, item "BCG.L-shaped fixture still repaired"):
# the ORIGINAL TASK 29 incident shape (a single transition, caught
# entirely by PASS 1 alone) is completely unaffected by this task.
# ======================================================================
_bcg_series = [230.0] * 30 + [2.29, 2.31, 2.28, 2.32, 2.30, 2.30]
_log3 = []
_repaired3 = fd.repair_mixed_unit_price_series(
    _bcg_series, 2.30, ticker="BCG.L", log=_log3.append
)
check("REGRESSION GUARD: the BCG.L-shaped single-transition series "
      "is still repaired flat in GBP",
      (max(_repaired3) / min(_repaired3)) < 1.3)
check('REGRESSION GUARD: the "30 of 36 points" log line is unchanged',
      _log3 == ["[units] BCG.L history repaired: 30 of 36 points "
                "rescaled pence->GBP"])
print("[task29c_t3_bcg_regression] BCG.L's own original TASK 29 shape "
      "(caught entirely by PASS 1) is completely unaffected OK")


# ======================================================================
# T4 (regression guard, item "genuine large moves WITHOUT a ~100x
# jump... untouched"): a real 60% crash (ratio ~2.5x between
# consecutive days) must never be mistaken for a unit switch, however
# far the post-crash price then sits from the reference.
# ======================================================================
_crash_series = [10.0, 10.2, 9.8, 4.0, 4.1, 4.3, 4.0]
_log4 = []
_repaired4 = fd.repair_mixed_unit_price_series(
    _crash_series, 4.0, ticker="CRASHCO.L", log=_log4.append
)
check("REGRESSION GUARD: a genuine 60% crash (no ~100x neighbour "
      "jump anywhere) is left byte-identical",
      _repaired4 == _crash_series)
check("REGRESSION GUARD: no '[units]' log line at all for a series "
      "with nothing to repair", _log4 == [])
print("[task29c_t4_genuine_crash_untouched] a real 60% crash (ratio "
      "~2.5x, nowhere near the 50x-200x jump band) is never mistaken "
      "for a unit switch OK")

# A second, sharper genuine move (an 80% crash, ratio 5x) - still
# nowhere near the jump band, still untouched.
_sharp_crash = [20.0, 19.5, 4.0, 3.9, 4.1]
_repaired4b = fd.repair_mixed_unit_price_series(_sharp_crash, 4.0, ticker="SHARPCO.L")
check("REGRESSION GUARD: an 80% crash (ratio 5x) is also left "
      "untouched - still far short of the 50x floor",
      _repaired4b == _sharp_crash)
print("[task29c_t4b_sharper_crash_untouched] an even sharper genuine "
      "move (80% crash, ratio 5x) is also never mistaken for a unit "
      "switch OK")


# ======================================================================
# T5 (regression guard, item "GBp-flagged... unchanged"): a
# consistently pence-quoted ticker (no internal jump, every point
# uniformly ~100x the reference - PASS 1 alone already resolves 100%
# of it) ends up with the exact same outcome as before this task.
# ======================================================================
_shel_series = [2590.0, 2605.0, 2615.0, 2600.0]
_repaired5 = fd.repair_mixed_unit_price_series(_shel_series, 26.15, ticker="SHEL.L")
check("REGRESSION GUARD: a consistently-pence (GBp-flagged) ticker's "
      "outcome is unchanged - fully divided by 100",
      [round(p, 2) for p in _repaired5] == [25.9, 26.05, 26.15, 26.0])
print("[task29c_t5_gbp_flagged_unchanged] a GBp-flagged, consistently "
      "pence-quoted ticker's outcome is byte-identical to before this "
      "task OK")


# ======================================================================
# T6 (regression guard, item "plain-GBP... unchanged"): an
# already-correct GBP series with no internal jump at all is left
# completely untouched.
# ======================================================================
_glen_series = [12.0, 12.2, 12.4, 12.5]
_log6 = []
_repaired6 = fd.repair_mixed_unit_price_series(
    _glen_series, 12.5, ticker="GLEN.L", log=_log6.append
)
check("REGRESSION GUARD: an already-correct plain-GBP series is left "
      "byte-identical", _repaired6 == _glen_series)
check("REGRESSION GUARD: no log line for a series with nothing to "
      "repair", _log6 == [])
print("[task29c_t6_plain_gbp_unchanged] a plain-GBP .L ticker with no "
      "internal jump is completely unaffected OK")


# ======================================================================
# T7 (regression guard, item "non-.L tickers unchanged" + the pre-
# existing ratio-boundary regression this task's own development
# caught and fixed): a series whose points sit right at the existing
# _PENCE_HISTORY_REPAIR_RATIO_RANGE boundaries, with NO neighbour jump
# anywhere, must behave EXACTLY as PASS 1 alone would - this is the
# exact fixture an earlier (reverted) version of this fix broke, by
# treating "no jump found" as "one giant segment" and judging it
# against the reference as a whole.
# ======================================================================
_lo, _hi = fd._PENCE_HISTORY_REPAIR_RATIO_RANGE
_ref7 = 1.0
_boundary_series = [_lo * _ref7, _hi * _ref7, (_lo - 0.1) * _ref7, (_hi + 1.0) * _ref7]
_repaired7 = fd.repair_mixed_unit_price_series(_boundary_series, _ref7)
check("REGRESSION GUARD: a point at the lower ratio-range boundary "
      "(30.0x) is still rescaled",
      abs(_repaired7[0] - (_lo * _ref7) / 100.0) < 1e-9)
check("REGRESSION GUARD: a point at the upper ratio-range boundary "
      "(300.0x) is still rescaled",
      abs(_repaired7[1] - (_hi * _ref7) / 100.0) < 1e-9)
check("REGRESSION GUARD: a point just BELOW the lower boundary "
      "(29.9x) is STILL left untouched - not swept up by a false "
      "'whole series is one segment' judgement",
      _repaired7[2] == (_lo - 0.1) * _ref7)
check("REGRESSION GUARD: a point just ABOVE the upper boundary "
      "(301x) is STILL left untouched",
      _repaired7[3] == (_hi + 1.0) * _ref7)
print("[task29c_t7_ratio_boundary_still_correct] the pre-existing "
      "30x/300x ratio-boundary fixture (no neighbour jump anywhere) "
      "behaves exactly as PASS 1 alone always did OK")


# ======================================================================
# T8: _neighbour_jump_segments() returns [] (not one all-covering
# segment) when no jump is found anywhere - the exact contract T7
# above depends on.
# ======================================================================
check("FIX: _neighbour_jump_segments() on a series with no jump "
      "anywhere returns [] (a true no-op signal), never one segment "
      "spanning the whole series",
      fd._neighbour_jump_segments([1.0, 1.1, 0.9, 1.05]) == [])
check("FIX: _neighbour_jump_segments() correctly finds a single real "
      "jump boundary",
      fd._neighbour_jump_segments([1.0, 1.1, 110.0, 105.0]) == [(0, 2), (2, 4)])
check("FIX: _neighbour_jump_segments() correctly finds two real jump "
      "boundaries (into and out of a pence island)",
      fd._neighbour_jump_segments([1.0, 100.0, 95.0, 1.02]) == [(0, 1), (1, 3), (3, 4)])
print("[task29c_t8_neighbour_jump_segments] segment boundary detection "
      "returns [] for 'nothing found', correct segments otherwise OK")


# ======================================================================
# T9 (item "Apply at every place Task 29/29b/28 call the repair"):
# every one of the six real call sites across the codebase still
# calls the SAME two shared functions - confirmed directly (not just
# asserted) by grepping the actual call sites, so this task's fix
# automatically reaches all of them with zero call-site changes.
# ======================================================================
import subprocess
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_grep = subprocess.run(
    ["grep", "-rn", "repair_mixed_unit_price_series(\\|repair_mixed_unit_price_dataframe(",
     "fundamentals_data.py", "nightly_scan.py", "app.py", "compounder_ui.py"],
    cwd=_repo_root, capture_output=True, text=True,
)
_call_sites = [l for l in _grep.stdout.splitlines() if "def repair_mixed_unit_price" not in l]
check("FIX: at least 6 real call sites of the shared repair functions "
      "exist across the codebase (get_bundle, normalize_pence_quote "
      "x2, _reprice_row, the Deep Dive chart, the Trading Cost tab)",
      len(_call_sites) >= 6)
print(f"[task29c_t9_call_sites_confirmed] {len(_call_sites)} real call "
      f"sites of the shared repair functions found - this task's fix "
      f"reaches every one of them with zero call-site changes OK")


# ======================================================================
# T10 (BUNDLE_VERSION bump): confirmed bumped past TASK 29b's own 8.
# ======================================================================
check("BUNDLE_VERSION bumped to 9", fd.BUNDLE_VERSION == 9)
print("[task29c_t10_bundle_version] BUNDLE_VERSION == 9 OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
