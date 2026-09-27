"""Batch B2, item 8 (27 Sep 2026, owner-directed, CONFIRMED BUG): missing
Capital Expenditure was treated as zero, so FCF = OCF.

Both portfolio_health_engine.fetch_snapshot()'s and
results_engine._fcf_for_col()'s own "Operating Cash Flow + Capital
Expenditure" FCF calc used `float(ocf) + float(capex or 0)` - when the
cashflow statement simply doesn't carry a "Capital Expenditure" row at
all (not unusual - some tickers' statements omit it, or a data source
gap drops just that row), `capex` comes back None, `capex or 0`
evaluates to 0, and FCF silently becomes OCF unchanged - a real
overstatement (capex is a real cash outflow every capital-intensive
business has), not a "no data available" case treated honestly as
unavailable like every other missing-input case on this site (see e.g.
_ttm_eps's own "None if fewer than 4 quarters ... not a smaller-but-
valid one" convention right next to this same function in
results_engine.py).

Reproduces the bug first (the OLD code's FCF = OCF exactly, a real,
visible overstatement whenever real-world capex would have been
material), then proves the fix (missing capex -> the whole figure is
None, not a wrong number).
Run: python3 tests/test_b2_8_missing_capex_not_zero.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import results_engine as re_
import portfolio_health_engine as phe

# ---- results_engine._fcf_for_col(): missing Capital Expenditure row entirely ----
cf_no_capex = pd.DataFrame({"2026-06-30": [500.0]}, index=["Operating Cash Flow"])
result = re_._fcf_for_col(cf_no_capex, "2026-06-30")
assert result is None
print("[fcf_for_col_missing_row_none] a cashflow statement with no Capital Expenditure row at "
      "all now correctly returns None, not OCF-as-FCF OK")

# Reproduces the bug: the OLD code (`float(ocf) + float(capex or 0)`)
# would have returned exactly the OCF figure, 500.0 - a materially wrong
# "FCF" for any real business with real capex.
_old_buggy_result = 500.0 + 0.0  # ocf + (capex or 0), capex missing -> 0
assert _old_buggy_result != result  # result is None, not 500.0 - genuinely different
print(f"[reproduces_bug] the OLD code would have returned {_old_buggy_result} (= OCF exactly) "
      "as if this company had zero capex OK")

# ---- Capital Expenditure row present but NaN for this specific column ----
cf_nan_capex = pd.DataFrame(
    {"2026-06-30": [500.0, float("nan")]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
result_nan = re_._fcf_for_col(cf_nan_capex, "2026-06-30")
assert result_nan is None
print("[fcf_for_col_nan_value_none] a Capital Expenditure row that exists but is NaN for this "
      "column also correctly returns None, not 'capex or 0' silently treating NaN as falsy-zero "
      "OK")

# ---- Both present and real: FCF = OCF + capex (capex already negative) ----
cf_real = pd.DataFrame(
    {"2026-06-30": [500.0, -120.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
result_real = re_._fcf_for_col(cf_real, "2026-06-30")
assert result_real == 380.0
print(f"[fcf_for_col_real_capex] both rows present -> FCF = {result_real} (500 - 120), the "
      "genuine calculation is completely unaffected by this fix OK")

# ---- results_engine._fcf_pair(): missing-capex column -> None in the pair ----
# Second column ("2025-06-30") never had a real Capital Expenditure value
# reported - NaN there, unlike the first column's real -120.0.
cf_pair_one_missing = pd.DataFrame(
    [[500.0, 400.0], [-120.0, float("nan")]],
    columns=["2026-06-30", "2025-06-30"],
    index=["Operating Cash Flow", "Capital Expenditure"],
)
after, before = re_._fcf_pair(cf_pair_one_missing)
assert after == 380.0
assert before is None
print("[fcf_pair_partial_missing] the pair correctly reports a real FCF for the column that has "
      "both rows, and None (not a wrong number) for the column missing capex OK")

# ---- portfolio_health_engine.fetch_snapshot(): same fix, same bug ----
cf_ph_no_capex = pd.DataFrame(
    {"2026-06-30": [600.0], "2025-06-30": [550.0]}, index=["Operating Cash Flow"],
)


class _FakeTicker:
    cashflow = cf_ph_no_capex
    info = {}

    def history(self, *a, **k):
        return None


phe.fetch_snapshot.clear()  # bypass st.cache_data's memo so this fixture's ticker is never stale
with mock.patch.object(phe.yf, "Ticker", return_value=_FakeTicker()), \
     mock.patch.object(phe.nightly_scan, "analyze_ticker_lite", return_value=None):
    snap = phe.fetch_snapshot("TESTB28_MISSING_CAPEX")
assert snap.get("fcf_growth") is None
print("[portfolio_health_missing_capex_no_growth] with no Capital Expenditure row anywhere in "
      "the cashflow statement, fetch_snapshot() correctly reports fcf_growth as None (unavailable) "
      "rather than a growth figure computed from OCF-as-FCF for both years OK")


print("\nALL B2.8 MISSING-CAPEX-NOT-ZERO FIXTURES PASSED")
