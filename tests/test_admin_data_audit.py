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
    # Audit fixes Commit 4 (30 Sep 2026): whole_company_shares() now
    # returns a 4-tuple (shares, flagged, source, note) - note is None
    # here since this mock represents an ACCEPTED candidate.
    with mock.patch("share_class_engine.whole_company_shares", return_value=(122_000_000, True, "implied", None)), \
         mock.patch("share_class_engine._fetch_diluted_shares", return_value=122_500_000):
        rows = ada.check_a3b_shares(tickers=["HEI"])
assert len(rows) == 1
r = rows[0]
assert r["sharesOutstanding"] == 55_235_561
assert r["impliedSharesOutstanding"] == 122_000_000
assert r["whole_company_shares"] == 122_000_000 and r["whole_company_flagged"] is True
assert r["whole_company_source"] == "implied"
assert r["whole_company_note"] is None
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


# ---- A1 pre-fix snapshot mechanism (27 Sep 2026, owner-directed, MANDATORY) ----
import scan_store

# The module-level import above already ran capture_a1_before_snapshot_
# once() once (with nothing yet saved in this fresh TESTVOL) - remove
# that empty snapshot so this block can test real capture behaviour
# from a clean slate, same as a fresh production deploy would see it.
if os.path.exists(ada._snapshot_path()):
    os.remove(ada._snapshot_path())

scan_store.save_scan("TEST_UNIVERSE", [{"Ticker": "AAPL", "Intrinsic Value": 150.0, "MOS %": -10.0}], "test")

ada.capture_a1_before_snapshot_once(tickers=["AAPL", "MSFT"])
snap = ada.load_a1_before_snapshot()
assert snap is not None and "AAPL" in snap["rows"] and "MSFT" not in snap["rows"]
assert snap["rows"]["AAPL"]["row"]["Intrinsic Value"] == 150.0
print("[a1_snapshot_capture] captures exactly the tickers with a saved row, skips ones with "
      "none, from the live scan_store state at capture time OK")

# Idempotency: change scan_store afterwards, re-run the capture - must NOT overwrite.
scan_store.save_scan("TEST_UNIVERSE", [{"Ticker": "AAPL", "Intrinsic Value": 999.0, "MOS %": 50.0}], "test")
ada.capture_a1_before_snapshot_once(tickers=["AAPL"])
snap2 = ada.load_a1_before_snapshot()
assert snap2["rows"]["AAPL"]["row"]["Intrinsic Value"] == 150.0  # still the FIRST capture
print("[a1_snapshot_idempotent] a second capture call never overwrites an existing snapshot, "
      "even when scan_store has since changed - this is the whole mechanism's point OK")

# check_a1_before_after() prefers the snapshot for a captured ticker, falls back to a live
# read (via _find_any_saved_row) for one the snapshot never saw.
scan_store.save_scan("TEST_UNIVERSE", [
    {"Ticker": "AAPL", "Intrinsic Value": 999.0, "MOS %": 50.0},
    {"Ticker": "MSFT", "Intrinsic Value": 400.0, "MOS %": 5.0},
], "test")
with mock.patch("fundamentals_data.get_bundle", return_value=None):
    rows = ada.check_a1_before_after(tickers=["AAPL", "MSFT"])
by_ticker = {r["ticker"]: r for r in rows}
assert by_ticker["AAPL"]["before"]["row"]["Intrinsic Value"] == 150.0  # snapshot, not live 999.0
assert by_ticker["AAPL"]["before_source"] == "snapshot"
assert by_ticker["MSFT"]["before"]["row"]["Intrinsic Value"] == 400.0  # live fallback, snapshot never had it
assert by_ticker["MSFT"]["before_source"] == "live"
print("[check_a1_before_after_snapshot_wiring] AAPL's 'before' comes from the frozen snapshot "
      "(ignoring scan_store's now-different live row) while MSFT, never captured, correctly "
      "falls back to a live read OK")


# ---- check_a6_discount_tiers(): mocked bundle + capm_engine + dcf_intrinsic_value ----
import capm_engine
import fcf_valuation_engine

_A6_INFO = {"currency": "USD", "currentPrice": 150.0, "marketCap": 100_000_000_000}
_A6_BUNDLE = {"info": _A6_INFO, "cashflow": pd.DataFrame()}


def _fake_dcf(ticker, info=None, cashflow_df=None, currency=None, discount_rate=None, **kw):
    # Old (auto CAPM) path: discount_rate=None -> a fixed "old" IV/rate.
    # New (tiered) path: discount_rate=<tier rate> passed straight through.
    if discount_rate is None:
        return 200.0, 0.08, {"discount_rate_used": 0.085}
    return 220.0, 0.08, {"discount_rate_used": discount_rate}


with mock.patch("fundamentals_data.get_bundle", return_value=_A6_BUNDLE), \
     mock.patch("fcf_valuation_engine.dcf_intrinsic_value", side_effect=_fake_dcf), \
     mock.patch("capm_engine.resolve_discount_rate_by_market_cap",
                return_value=(0.080, {"tier_label": "large-cap (US$50B-200B)", "rf_source": "default"})):
    rows = ada.check_a6_discount_tiers(tickers=["MSFT"])
assert len(rows) == 1
r = rows[0]
assert r["price"] == 150.0
assert r["current_model"]["discount_rate"] == 0.085
assert r["current_model"]["intrinsic_value"] == 200.0
assert abs(r["current_model"]["mos_pct"] - round((200.0 - 150.0) / 200.0 * 100.0, 2)) < 1e-6
assert r["tier_model"]["discount_rate"] == 0.080
assert r["tier_model"]["tier_label"] == "large-cap (US$50B-200B)"
assert r["tier_model"]["intrinsic_value"] == 220.0
assert abs(r["tier_model"]["mos_pct"] - round((220.0 - 150.0) / 220.0 * 100.0, 2)) < 1e-6
_expected_delta = round(r["tier_model"]["mos_pct"] - r["current_model"]["mos_pct"], 2)
assert r["mos_delta_pts"] == _expected_delta
print("[check_a6_discount_tiers] old (auto-CAPM) vs new (tiered) discount rate/IV/MOS both "
      "computed from the SAME dcf_intrinsic_value() call, differing only in the discount_rate= "
      "override, with mos_delta_pts correctly derived OK")

# A ticker whose bundle fetch fails must not crash the whole check.
with mock.patch("fundamentals_data.get_bundle", side_effect=Exception("network down")):
    rows_err = ada.check_a6_discount_tiers(tickers=["BADTICKER"])
assert rows_err[0]["ticker"] == "BADTICKER" and "error" in rows_err[0]
print("[check_a6_discount_tiers_error_isolated] one ticker's fetch failure is captured, not "
      "raised OK")


# ---- check_mer(): mocked funds_data + etf_insights.get_fund_facts ----
with mock.patch.object(ada, "yf") as mock_yf:
    ops = pd.DataFrame({"IVV": [0.0007]}, index=["Annual Report Expense Ratio"])
    mock_yf.Ticker.return_value.funds_data.fund_operations = ops
    with mock.patch("etf_insights.get_fund_facts", return_value={"mer": 0.07}):
        rows = ada.check_mer(tickers=["IVV"])
assert abs(rows[0]["raw_expense_ratio"] - 0.0007) < 1e-9
assert rows[0]["site_displays_mer_pct"] == 0.07
print("[check_mer] raw fraction (0.0007) next to the site's own normalized display (0.07%) OK")


# ---- Batch B1a: check_b1_dividend_currency() ----
class _FakeDividends(dict):
    """Minimal stand-in for a pandas Series with .empty/.tail()/.items()."""
    def __init__(self, mapping):
        super().__init__(mapping)
    @property
    def empty(self):
        return len(self) == 0
    def tail(self, n):
        items = list(self.items())[-n:]
        return _FakeDividends(dict(items))


with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.return_value.info = {"currency": "AUD", "trailingAnnualDividendRate": 2.5}
    mock_yf.Ticker.return_value.dividends = _FakeDividends({"2026-03-01": 1.25, "2026-09-01": 1.30})
    rows = ada.check_b1_dividend_currency(tickers=["BHP.AX"])
assert rows[0]["currency"] == "AUD" and rows[0]["trailingAnnualDividendRate"] == 2.5
assert len(rows[0]["last_two_payments"]) == 2
assert rows[0]["last_two_payments"][-1]["amount"] == 1.30
print("[check_b1_dividend_currency] last-2 payments + trailingAnnualDividendRate + listing "
      "currency all wired correctly OK")

with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.return_value.info = {"currency": "AUD"}
    mock_yf.Ticker.return_value.dividends = _FakeDividends({})
    rows = ada.check_b1_dividend_currency(tickers=["EMPTY.AX"])
assert rows[0]["last_two_payments"] == []
print("[check_b1_dividend_currency_empty] no dividend history -> empty list, not a crash OK")


# ---- Batch B1b: check_b1_lease_rows() ----
_LEASE_CF_YEARS = ["2026-06-30", "2025-06-30"]
_lease_cf_df = pd.DataFrame.from_dict({
    "Repayments Of Lease Liabilities": [-120.0, -100.0],
    "Some Unrelated Row": [50.0, 40.0],
}, orient="index", columns=_LEASE_CF_YEARS)
_lease_bs_df = pd.DataFrame.from_dict({
    "Total Debt": [5000.0, 4800.0],
    "Long Term Lease Liabilities": [900.0, 850.0],
    "Cash": [200.0, 180.0],
}, orient="index", columns=_LEASE_CF_YEARS)

with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.return_value.cashflow = _lease_cf_df
    mock_yf.Ticker.return_value.balance_sheet = _lease_bs_df
    rows = ada.check_b1_lease_rows(tickers=["WES.AX"])
r = rows[0]
assert "Repayments Of Lease Liabilities" in r["cashflow_lease_rows"]
assert "Some Unrelated Row" not in r["cashflow_lease_rows"]
assert r["cashflow_lease_rows"]["Repayments Of Lease Liabilities"] == [-120.0, -100.0]
assert r["total_debt_latest"] == 5000.0
assert "Long Term Lease Liabilities" in r["balance_sheet_lease_rows"]
assert "Cash" not in r["balance_sheet_lease_rows"]
print("[check_b1_lease_rows] tolerant substring match finds the real lease/repayment row and "
      "skips unrelated ones, Total Debt and balance-sheet lease liabilities both captured OK")

with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.side_effect = Exception("network down")
    rows_err = ada.check_b1_lease_rows(tickers=["ERR.AX"])
assert rows_err[0]["ticker"] == "ERR.AX" and "error" in rows_err[0]
print("[check_b1_lease_rows_error_isolated] a fetch failure is captured per-ticker, not raised OK")


# ---- Batch B1c: check_b1_price_to_book_fx() ----
with mock.patch.object(ada, "yf") as mock_yf:
    mock_yf.Ticker.return_value.info = {
        "priceToBook": 8.5, "currentPrice": 280.0, "bookValue": 32.0,
        "currency": "AUD", "financialCurrency": "USD",
    }
    rows = ada.check_b1_price_to_book_fx(tickers=["CSL.AX"])
r = rows[0]
assert r["priceToBook_yahoo"] == 8.5
assert abs(r["price_over_book_computed"] - 8.75) < 1e-6  # 280/32, NOT the same as Yahoo's 8.5
assert r["currency"] == "AUD" and r["financialCurrency"] == "USD"
print("[check_b1_price_to_book_fx] Yahoo's priceToBook shown next to the locally-computed "
      "price/book and BOTH currencies, so a mismatch between them (8.5 vs 8.75 here) is visible "
      "OK")


print("\nALL ADMIN_DATA_AUDIT FIXTURES PASSED")
