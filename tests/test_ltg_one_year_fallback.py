"""
LTG-fallback fix (owner-directed, 30 Sep 2026, Step 1c of instruction_
tonight_sequence_v2.md). Root cause, confirmed from real production
evidence - NOT guessed:

  - A live production request (PAF.AX, 08:50:44 UTC, the WARNING-level
    diagnostics deployed earlier the same night) logged the full raw
    growth_estimates frame: columns ['stockTrend', 'indexTrend'], index
    ['0q','+1q','0y','+1y','LTG'], with the LTG row's own 'stockTrend'
    value NaN (only 'indexTrend', the benchmark column, populated).
  - The owner then ran a Dow 30 rescan and pasted the RMD.AX frame dump
    at 09:25 UTC, confirming the SAME shape: the LTG row exists, the
    'stockTrend' column is correctly matched (not a wrong-column-name
    bug), units are decimal, but the stock's own LTG value is NaN -
    Yahoo genuinely isn't populating long-term growth for that name.

This is real DATA, not a parse bug - capm_engine's label/column matching
was already correct. The fix: when LTG (and any +5y/5y/5year labels)
match but have no usable value, fall back to this SAME account's own
shorter-horizon rows - '+1y' (next fiscal year) first, then '0y'
(current fiscal year) - before giving up. A new status "ok_1y" (not
"ok") distinguishes this from a genuine LTG figure, so downstream labels
can honestly read "Yahoo analyst (next year)".

This file covers:
  - the exact real PAF.AX-shaped frame (LTG NaN, +1y/0y ALSO NaN in the
    same 'stockTrend' column - the real case actually observed in
    production) still correctly falls through to (None, "no_coverage")
    - the fallback must never invent a value that isn't there
  - the same frame shape but with a real +1y value present (LTG NaN,
    +1y populated) - the fallback fires, returns "ok_1y", and the value
    is correctly scale-normalized
  - +1y NaN but 0y populated - the SECOND fallback tier fires
  - a genuine non-positive LTG value is returned as-is ("non_positive"),
    never overridden by a positive +1y/0y fallback figure - a real,
    confirmed data point is never second-guessed
  - nightly_scan._growth_coverage_bucket() correctly buckets "ok"/
    "ok_1y"/everything-else into "yahoo_ltg"/"yahoo_1y"/"yahoo_none"
  - capm_engine.reset_growth_null_row_log() actually resets the
    per-universe once-flag (was per-process before this fix)

Run: python3 tests/test_ltg_one_year_fallback.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import capm_engine as ce
import nightly_scan as ns


def _reset_once_flags():
    ce._growth_estimate_labels_logged = False
    ce._growth_estimate_raw_value_logged = False
    ce._growth_estimate_raw_frame_logged = False
    ce._growth_estimate_null_row_logged = False


# ======================================================================
# CHECK 1: the exact real PAF.AX frame shape (08:50:44 UTC production
# dump) - LTG's 'stockTrend' is NaN, and so is EVERY OTHER row's
# 'stockTrend' (only 'indexTrend' is populated) - the fallback must not
# invent a value that genuinely isn't there anywhere in the frame.
# ======================================================================
_reset_once_flags()
df_all_nan_stock = pd.DataFrame(
    {
        "stockTrend": [None, None, None, None, None],
        "indexTrend": [0.4987, 0.2381, 0.3175, 0.3440, 0.1220],
    },
    index=["0q", "+1q", "0y", "+1y", "LTG"],
)
with mock.patch("nightly_scan._yf_call_with_retry", return_value=df_all_nan_stock):
    val1, status1 = ce._fetch_growth_estimates_5y_uncached("PAF.AX")
assert val1 is None and status1 == "no_coverage", (val1, status1)
print("[real_paf_frame_no_fallback_value] the exact real production frame shape "
      "(LTG AND +1y/0y all NaN in 'stockTrend') correctly falls through to "
      "(None, 'no_coverage') - the fallback never invents a value OK")


# ======================================================================
# CHECK 2: LTG NaN, but +1y has a real, positive value - the fallback
# fires, returns "ok_1y" (not "ok"), scale-normalized correctly.
# ======================================================================
_reset_once_flags()
df_1y_ok = pd.DataFrame(
    {
        "stockTrend": [None, None, None, 8.4, None],  # +1y = 8.4 (percent units)
        "indexTrend": [0.4987, 0.2381, 0.3175, 0.3440, 0.1220],
    },
    index=["0q", "+1q", "0y", "+1y", "LTG"],
)
with mock.patch("nightly_scan._yf_call_with_retry", return_value=df_1y_ok):
    val2, status2 = ce._fetch_growth_estimates_5y_uncached("RMD.AX")
assert status2 == "ok_1y", status2
assert abs(val2 - 0.084) < 1e-9, val2
print(f"[ltg_nan_1y_fallback_fires] LTG NaN, +1y=8.4 (percent) -> fallback returns "
      f"(value={val2}, status='ok_1y') OK")


# ======================================================================
# CHECK 3: LTG NaN, +1y ALSO NaN, but 0y has a real positive value - the
# SECOND fallback tier (0y) fires.
# ======================================================================
_reset_once_flags()
df_0y_ok = pd.DataFrame(
    {
        "stockTrend": [None, None, 0.062, None, None],  # 0y = 0.062 (decimal units)
        "indexTrend": [0.4987, 0.2381, 0.3175, 0.3440, 0.1220],
    },
    index=["0q", "+1q", "0y", "+1y", "LTG"],
)
with mock.patch("nightly_scan._yf_call_with_retry", return_value=df_0y_ok):
    val3, status3 = ce._fetch_growth_estimates_5y_uncached("XRO.AX")
assert status3 == "ok_1y", status3
assert abs(val3 - 0.062) < 1e-9, val3
print(f"[ltg_and_1y_nan_0y_fallback_fires] LTG NaN, +1y NaN, 0y=0.062 -> fallback "
      f"tries +1y first (NaN, skipped) then 0y -> (value={val3}, status='ok_1y') OK")


# ======================================================================
# CHECK 4: a genuine NON-POSITIVE LTG value is real, confirmed data -
# it must be returned as-is ("non_positive"), never second-guessed by a
# positive +1y/0y fallback figure that happens to also be present.
# ======================================================================
_reset_once_flags()
df_ltg_negative = pd.DataFrame(
    {
        "stockTrend": [None, None, None, 0.05, -0.03],  # LTG = -3% (real, negative)
        "indexTrend": [0.4987, 0.2381, 0.3175, 0.3440, 0.1220],
    },
    index=["0q", "+1q", "0y", "+1y", "LTG"],
)
with mock.patch("nightly_scan._yf_call_with_retry", return_value=df_ltg_negative):
    val4, status4 = ce._fetch_growth_estimates_5y_uncached("NEGCO")
assert status4 == "non_positive", status4
assert abs(val4 - (-0.03)) < 1e-9, val4
print(f"[non_positive_ltg_not_overridden] a genuine non-positive LTG (-3%) is "
      f"returned as-is (value={val4}, status='non_positive') even though +1y=5% "
      f"was also present - a real data point is never overridden by the fallback OK")


# ======================================================================
# CHECK 5: nightly_scan._growth_coverage_bucket() buckets "ok"/"ok_1y"/
# everything-else into "yahoo_ltg"/"yahoo_1y"/"yahoo_none".
# ======================================================================
assert ns._growth_coverage_bucket({"yahoo_estimate_status": "ok"}) == "yahoo_ltg"
assert ns._growth_coverage_bucket({"yahoo_estimate_status": "ok_1y"}) == "yahoo_1y"
assert ns._growth_coverage_bucket({"yahoo_estimate_status": "no_coverage"}) == "yahoo_none"
assert ns._growth_coverage_bucket({"yahoo_estimate_status": "non_positive"}) == "yahoo_none"
assert ns._growth_coverage_bucket({"yahoo_estimate_status": "fetch_failed"}) == "yahoo_none"
assert ns._growth_coverage_bucket({"yahoo_estimate_status": None}) == "yahoo_none"
print("[growth_coverage_bucket] 'ok'->yahoo_ltg, 'ok_1y'->yahoo_1y, everything else "
      "->yahoo_none OK")


# ======================================================================
# CHECK 6: reset_growth_null_row_log() actually resets the flag (was
# per-process before this fix; nightly_scan.run_universe_scan() now
# calls this once per universe).
# ======================================================================
ce._growth_estimate_null_row_logged = True
ce.reset_growth_null_row_log()
assert ce._growth_estimate_null_row_logged is False
print("[reset_growth_null_row_log] resets the per-universe once-flag back to False OK")


print("\nSWEEP_DONE")
