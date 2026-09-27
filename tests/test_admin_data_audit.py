"""Data-correctness audit item 5 (27 Sep 2026, owner-directed): unit
fixtures for admin_data_audit.py's own check-function logic, with
yfinance mocked (this sandbox has no live network access - confirmed
EGRESS_BLOCKED earlier in this audit). Proves the MECHANISM is correct
against synthetic data shaped like the real thing; does NOT (and
cannot, from here) prove today's real Yahoo numbers - that's exactly
why this panel has to run on the production server itself.
Run: python3 test_admin_data_audit.py
"""
import os
import sys
import time
from unittest import mock

import pandas as pd

import tempfile

TESTVOL = os.path.join(tempfile.gettempdir(), "admin_data_audit_test_vol")
os.makedirs(TESTVOL, exist_ok=True)
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin_data_audit as ada
import scheduler_engine

# Speed the fixture up - no need for the real 0.4s pace between mocked calls.
ada._PACE_SECONDS = 0.0

YEARS = ["2026-06-30", "2025-06-30", "2024-06-30"]


def _df(rows, years=YEARS):
    return pd.DataFrame.from_dict(rows, orient="index", columns=years)


# ---- refusal_reason(): lock check ----
import datetime as _dt

_safe_hour = _dt.datetime(2026, 9, 27, 10, 0, tzinfo=_dt.timezone.utc)
assert ada.refusal_reason(now=_safe_hour) is None
print("[refusal_safe_hour] 10:00 UTC, no lock -> may run OK")

for _h in (20, 23, 0, 2):
    _t = _dt.datetime(2026, 9, 27, _h, 30, tzinfo=_dt.timezone.utc)
    reason = ada.refusal_reason(now=_t)
    assert reason is not None and "20:00-03:00" in reason, (_h, reason)
assert ada.refusal_reason(now=_dt.datetime(2026, 9, 27, 3, 0, tzinfo=_dt.timezone.utc)) is None
print("[refusal_window] 20:00-03:00 UTC refused, 03:00 itself allowed (half-open window) OK")

_lock_path = scheduler_engine._lock_path(ada.NIGHTLY_LOCK_JOB_NAME)
with open(_lock_path, "w") as f:
    import json
    json.dump({"pid": 1, "boot_id": "test", "started": "now", "heartbeat": time.time()}, f)
try:
    reason = ada.refusal_reason(now=_safe_hour)
    assert reason is not None and "lock" in reason.lower(), reason
    print("[refusal_lock_held] fresh nightly lock -> refused even at a safe hour OK")
finally:
    os.remove(_lock_path)
assert ada.refusal_reason(now=_safe_hour) is None
print("[refusal_lock_cleared] lock removed -> may run again OK")


# ---- check_a1_rates(): mocked ^TNX history + capm_engine ----
class _FakeHist:
    empty = False
    def __init__(self, close):
        self._close = close
    def __getitem__(self, key):
        assert key == "Close"
        return self

class _FakeCloseSeries(list):
    @property
    def iloc(self):
        return self

with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.return_value.history.return_value = pd.DataFrame({"Close": [4.0, 4.2, 5.16]})
    with mock.patch("capm_engine.get_risk_free_rate", side_effect=[(0.0516, "live"), (0.045, "default")]):
        rates = ada.check_a1_rates()
assert rates["usd"]["raw_close"] == 5.16
assert rates["usd"]["rate"] == 0.0516 and rates["usd"]["source"] == "live"
assert rates["aud"]["rate"] == 0.045 and rates["aud"]["source"] == "default"
print("[check_a1_rates] raw ^TNX close + USD/AUD rate+source all wired correctly OK")


# ---- check_a3b_shares(): mocked info + share_class_engine ----
with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.return_value.info = {
        "sharesOutstanding": 55_235_561, "impliedSharesOutstanding": 122_000_000,
    }
    with mock.patch("share_class_engine.whole_company_shares", return_value=(122_000_000, True, "implied")), \
         mock.patch("share_class_engine._fetch_diluted_shares", return_value=122_500_000):
        rows = ada.check_a3b_shares(tickers=["HEI"])
assert len(rows) == 1
r = rows[0]
assert r["sharesOutstanding"] == 55_235_561
assert r["impliedSharesOutstanding"] == 122_000_000
assert r["whole_company_shares"] == 122_000_000 and r["whole_company_flagged"] is True
assert r["whole_company_source"] == "implied"
assert r["diluted_average_shares_filed"] == 122_500_000
print("[check_a3b_shares] all 4 reported fields (sharesOutstanding/implied/filed-diluted/"
      "resolver-chosen) present and correctly sourced OK")

# A ticker whose info fetch fails entirely must not crash the whole check.
with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.return_value.info = mock.PropertyMock(side_effect=Exception("boom"))
    type(mock_yf.Ticker.return_value).info = property(lambda self: (_ for _ in ()).throw(Exception("boom")))
    rows_err = ada.check_a3b_shares(tickers=["BADTICKER"])
assert rows_err[0]["ticker"] == "BADTICKER" and "info_error" in rows_err[0]
print("[check_a3b_shares_error_isolated] one ticker's fetch failure is captured, not raised OK")


# ---- _cumulative_or_per_half(): the A5 verdict logic itself ----
assert ada._cumulative_or_per_half(1000.0, 1000.0) .startswith("cumulative")
assert ada._cumulative_or_per_half(500.0, 1000.0).startswith("per-half")
assert ada._cumulative_or_per_half(700.0, 1000.0).startswith("neither")
assert ada._cumulative_or_per_half(None, 1000.0) == "insufficient data"
assert ada._cumulative_or_per_half(500.0, 0) == "insufficient data"
print("[a5_verdict_logic] cumulative (~equal) / per-half (~half) / neither / insufficient-data "
      "all classify correctly OK")


# ---- check_a5_half_year(): mocked quarterly+annual statements ----
def _mk_income_stmt(net_income, diluted_eps, years=YEARS):
    return _df({"Net Income": net_income, "Diluted EPS": diluted_eps}, years=years)


with mock.patch.object(ada, "yf") as mock_yf:
    # Cumulative case: June quarterly column ALREADY equals the June annual
    # column (both represent the full year) - "cumulative".
    q_df = _mk_income_stmt([1000.0, 900.0, 800.0], [2.0, 1.8, 1.6], years=["2026-06-30", "2025-12-31", "2025-06-30"])
    a_df = _mk_income_stmt([1000.0, 900.0, 800.0], [2.0, 1.8, 1.6], years=YEARS)
    mock_yf.Ticker.return_value.quarterly_income_stmt = q_df
    mock_yf.Ticker.return_value.income_stmt = a_df
    rows = ada.check_a5_half_year(tickers=["CUMTEST.AX"])
assert rows[0]["verdict_net_income"].startswith("cumulative")
assert rows[0]["verdict_diluted_eps"].startswith("cumulative")
assert rows[0]["net_income"]["quarterly_june"] == 1000.0
print("[a5_cumulative_case] quarterly June column equal to annual June column -> 'cumulative' OK")

with mock.patch.object(ada, "yf") as mock_yf:
    # Per-half case: June quarterly column is HALF the annual June column.
    q_df2 = _mk_income_stmt([500.0, 450.0, 400.0], [1.0, 0.9, 0.8], years=["2026-06-30", "2025-12-31", "2025-06-30"])
    a_df2 = _mk_income_stmt([1000.0, 900.0, 800.0], [2.0, 1.8, 1.6], years=YEARS)
    mock_yf.Ticker.return_value.quarterly_income_stmt = q_df2
    mock_yf.Ticker.return_value.income_stmt = a_df2
    rows2 = ada.check_a5_half_year(tickers=["HALFTEST.AX"])
assert rows2[0]["verdict_net_income"].startswith("per-half")
assert rows2[0]["verdict_diluted_eps"].startswith("per-half")
print("[a5_per_half_case] quarterly June column half the annual June column -> 'per-half' OK")

# A ticker with no usable statements at all must not crash the batch.
with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.side_effect = Exception("network down")
    rows3 = ada.check_a5_half_year(tickers=["ERRTEST.AX"])
assert rows3[0]["ticker"] == "ERRTEST.AX" and "error" in rows3[0]
print("[a5_error_isolated] a fetch failure is captured per-ticker, not raised OK")


# ---- check_mer(): mocked funds_data + etf_insights.get_fund_facts ----
with mock.patch.object(ada, "yf") as mock_yf:
    ops = pd.DataFrame({"IVV": [0.0007]}, index=["Annual Report Expense Ratio"])
    mock_yf.Ticker.return_value.funds_data.fund_operations = ops
    with mock.patch("etf_insights.get_fund_facts", return_value={"mer": 0.07}):
        rows = ada.check_mer(tickers=["IVV"])
assert abs(rows[0]["raw_expense_ratio"] - 0.0007) < 1e-9
assert rows[0]["site_displays_mer_pct"] == 0.07
print("[check_mer] raw fraction (0.0007) next to the site's own normalized display (0.07%) OK")


print("\nALL ADMIN_DATA_AUDIT FIXTURES PASSED")
