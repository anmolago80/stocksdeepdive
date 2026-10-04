"""
Commit 4 of instruction_financials_income_store_and_top200_guard.md
(4 Oct 2026, Director-directed) - the live path, behind the
FINANCIALS_STORE_LIVE switch. With the switch ON and a financials-mode
ticker: nightly_scan.py reads income_df from financials_income_store
instead of its own one-off-check fetch; deep_dive_engine.py reads the
same store, falling back to a single one-ticker/24h-limited fetch
(financials_income_store.fetch_for_deep_dive()) on a cold entry; and
top100_engine.select_top100_pool() excludes any row whose "FCF Source"
is "ocf_fallback_financials" (a financials-mode ticker whose DCF still
ended up on the OCF path) from the pool. With the switch OFF, every one
of these three call sites is byte-identical to before this commit.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. No test here makes a live network call.

Run: python3 tests/test_financials_income_store_commit4.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="financials_income_store_commit4_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("FINANCIALS_STORE_LIVE", None)

import financials_classifier as fc
import financials_income_store as fis
import fcf_valuation_engine as fve
import nightly_scan as ns
import deep_dive_engine as dd
import top100_engine as te
import top100_store as ts
import scan_store
import fundamentals_data


def _switch(on):
    if on:
        os.environ["FINANCIALS_STORE_LIVE"] = "1"
    else:
        os.environ.pop("FINANCIALS_STORE_LIVE", None)


def _clear_store():
    for f in os.listdir(fis._store_dir()):
        os.remove(os.path.join(fis._store_dir(), f))


def _mk_income_df(net_income, cols=None):
    cols = cols or [f"202{5 - i}-12-31" for i in range(len(net_income))]
    return pd.DataFrame({"Net Income Common Stockholders": net_income}, index=cols).T


BANK_INFO = {"sector": "Financial Services", "industry": "Insurance - Property & Casualty",
             "currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 100.0, "currentPrice": 50.0, "marketCap": 5_000_000_000.0}
GOOD_INCOME = _mk_income_df([412.0, 380.0, 350.0, 300.0, 250.0],
                            cols=["2026-06-30", "2025-06-30", "2024-06-30",
                                  "2023-06-30", "2022-06-30"])
# Only one positive year (position 0) -> _financials_base_and_series()
# returns None (needs >=2 positive points) -> dispatcher falls back to
# the OCF path, tagged "ocf_fallback_financials".
THIN_INCOME = _mk_income_df([5.0, -2.0, -3.0, -1.0, -4.0],
                            cols=["2026-06-30", "2025-06-30", "2024-06-30",
                                  "2023-06-30", "2022-06-30"])

_dates = pd.date_range("2026-01-01", periods=90, freq="D")
_hist_df = pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(90)],
                          "Volume": [1_000_000] * 90}, index=_dates)
# Stable, mild cash-flow decline -> needs_oneoff_check() is False (no
# >30% year-over-year drop anywhere) - matches tests/
# test_oneoff_nightly_trigger.py's own STABLE_CF fixture shape.
STABLE_CF = pd.DataFrame(
    {"2026-06-30": [1000.0, -100.0], "2025-06-30": [980.0, -100.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
assert fve.needs_oneoff_check(STABLE_CF) is False

_ONEOFF_LABEL = "one-off check income statement"
_DCF_RATES = dict(discount_rate=0.08, perpetual_rate=0.02, growth_rate=0.05)


def _run_scan(ticker, cashflow_df=STABLE_CF, info=None):
    """Drives nightly_scan.analyze_ticker_lite() end-to-end with every
    other network call mocked out - same pattern as tests/
    test_oneoff_nightly_trigger.py's own _run_analyze(). Returns
    (row, oneoff_fetch_call_count, resolve_intrinsic_value_kwargs)."""
    info = info or dict(BANK_INFO)
    _oneoff_calls = []

    def _ytw_side_effect(fn, log, ticker_, label, **kw):
        if label == "history":
            return _hist_df
        if label == "info":
            return info
        if label == "cashflow":
            return cashflow_df
        if label == _ONEOFF_LABEL:
            _oneoff_calls.append(1)
            return None
        return None

    with mock.patch.object(ns, "_yf_call_with_retry") as _ytw, \
         mock.patch.object(ns, "resolve_quality_score", return_value=(70, "auto", False)), \
         mock.patch.object(ns, "resolve_intrinsic_value") as _riv, \
         mock.patch.object(ns, "resolve_stock_type", return_value=("COMPOUNDER", "auto", False)), \
         mock.patch.object(ns.reverse_dcf_engine, "compute", return_value={"ok": False}), \
         mock.patch.object(ns.trade_filter_engine, "calc_support_resistance",
                            return_value={"support20": 90, "resistance20": 110,
                                          "support60": 85, "resistance60": 115}), \
         mock.patch.object(ns.indicators_engine, "compute_indicators", return_value={"trend": "up"}), \
         mock.patch.object(ns.trade_filter_engine, "evaluate_trade", return_value={"signal": "-"}), \
         mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None):
        _ytw.side_effect = _ytw_side_effect
        _riv.return_value = (150.0, "dcf", 0.12, {
            "growth_source": "history", "growth_governor": "History",
            "yahoo_estimate_status": "ok", "value_default": False,
            "fcf_distorted_years": [],
        })
        row = ns.analyze_ticker_lite(ticker, log=lambda *a, **k: None)
        riv_kwargs = _riv.call_args.kwargs
    return row, len(_oneoff_calls), riv_kwargs


# ======================================================================
# C1: switch OFF -> income_df stays None for this fixture (identical to
# before this commit - needs_oneoff_check(STABLE_CF) is False, so no
# fetch happens, and the store is never consulted for valuation at all).
# Switch ON + a store entry already filled by a prior night -> the scan
# reads income_df straight from the store, makes NO new fetch of its
# own, and the SAME income table that was fetched fresh via Way 1 on
# some other night gives the IDENTICAL dcf_intrinsic_value() result
# when it comes back out of the store instead - "a bank already on net
# income today gives the same value from the store."
# ======================================================================
_clear_store()
_switch(False)
_, _calls_off, _kwargs_off = _run_scan("INSURER1", cashflow_df=STABLE_CF)
assert _calls_off == 0, _calls_off
assert _kwargs_off.get("income_df") is None, _kwargs_off.get("income_df")
_dcf_off, _, _meta_off = fve.dcf_intrinsic_value(
    "INSURER1", info=BANK_INFO, cashflow_df=STABLE_CF, currency="USD",
    income_df=None, **_DCF_RATES,
)
assert _meta_off["fcf_source"] == "ocf_fallback_financials", _meta_off["fcf_source"]
print(f"[C1_switch_off_unchanged] switch OFF, stable cashflow -> no fetch, income_df=None, "
      f"dcf falls back to OCF (fcf_source={_meta_off['fcf_source']!r}), value={_dcf_off} - "
      "identical to before this commit OK")

_switch(True)
fis.save("INSURER1", GOOD_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
_, _calls_on, _kwargs_on = _run_scan("INSURER1", cashflow_df=STABLE_CF)
assert _calls_on == 0, _calls_on  # no new Yahoo call - the store supplied it
_income_from_scan = _kwargs_on.get("income_df")
assert _income_from_scan is not None
pd.testing.assert_frame_equal(_income_from_scan.astype(float), GOOD_INCOME.astype(float),
                               check_names=False)
_dcf_on, _, _meta_on = fve.dcf_intrinsic_value(
    "INSURER1", info=BANK_INFO, cashflow_df=STABLE_CF, currency="USD",
    income_df=_income_from_scan, **_DCF_RATES,
)
assert _meta_on["fcf_source"] == "net_income_financials", _meta_on["fcf_source"]
assert _dcf_on != _dcf_off, (_dcf_on, _dcf_off)

# The exact same income table fed in directly (as if a fresh Way-1
# fetch had supplied it tonight instead of the store) gives the
# byte-identical value - the store round trip changes nothing.
_dcf_direct, _, _meta_direct = fve.dcf_intrinsic_value(
    "INSURER1", info=BANK_INFO, cashflow_df=STABLE_CF, currency="USD",
    income_df=GOOD_INCOME, **_DCF_RATES,
)
assert _dcf_direct == _dcf_on, (_dcf_direct, _dcf_on)
assert _meta_direct["fcf_source"] == _meta_on["fcf_source"] == "net_income_financials"
print(f"[C1_switch_on_store_wiring] switch ON + a store entry filled on a prior night -> "
      f"zero new fetches tonight, income_df sourced from the store, dcf value={_dcf_on} on the "
      f"net-income path (vs ${_dcf_off} on the OCF fallback with the switch off) - the store "
      "round trip gives the byte-identical value a fresh fetch would have OK")
_switch(False)
_clear_store()


# ======================================================================
# C2: fewer than two positive net-income years -> falls back to OCF
# (never net income) even with the switch ON and a store entry present
# - "fewer than two positive years -> fallback" - and that fallback row
# is excluded from the Top 100 pool - "...AND pool-ineligible."
# ======================================================================
_switch(True)
fis.save("THININS", THIN_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
_thin_entry = fis.get("THININS")
_dcf_thin, _, _meta_thin = fve.dcf_intrinsic_value(
    "THININS", info=BANK_INFO, cashflow_df=STABLE_CF, currency="USD",
    income_df=_thin_entry["income"], **_DCF_RATES,
)
assert _meta_thin["fcf_source"] == "ocf_fallback_financials", _meta_thin["fcf_source"]
print(f"[C2_fewer_than_two_years_fallback] a stored income table with only one positive net-"
      f"income year -> fcf_source={_meta_thin['fcf_source']!r} even with the switch ON and a "
      "store entry present OK")
_switch(False)
_clear_store()

_now_dt = datetime.now(timezone.utc)
_FAKE_CADENCE = {"S&P 500": "daily"}
_patch_sector_fill = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill.start()


def _top100_row(ticker, long_score, mos=20.0, fcf_source=None):
    row = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": 70,
        "MOS %": mos, "Psychology": 5.0, "Discovery (lite)": 10.0,
        "Long Score": long_score, "Price": 50.0, "Intrinsic Value": 55.0,
        "DCF Unreliable": False,
    }
    if fcf_source is not None:
        row["FCF Source"] = fcf_source
    return row


def _save_universe(universe, rows, generated_at=_now_dt):
    payload = {
        "universe": universe, "source": "test", "generated_at": generated_at.isoformat(),
        "run_night": None, "rows": rows, "attention_lite": True, "degraded": False,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


def _clear_scan(*universes):
    for u in universes:
        p = scan_store._path(u)
        if os.path.exists(p):
            os.remove(p)


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

_clean_rows = [_top100_row(f"CLEAN{i}", long_score=50.0 + i) for i in range(5)]
_fallback_row = _top100_row("THININS", long_score=95.0, mos=70.0,
                             fcf_source="ocf_fallback_financials")

_switch(True)
_clear_scan("S&P 500")
_save_universe("S&P 500", _clean_rows + [_fallback_row])
_logs_on = []
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    _pool_on = te.select_top100_pool(log=_logs_on.append)
_pool_on_tickers = {r["ticker"] for r in _pool_on}
assert "THININS" not in _pool_on_tickers, _pool_on_tickers
assert {f"CLEAN{i}" for i in range(5)} <= _pool_on_tickers, _pool_on_tickers
_log_line_on = next(l for l in _logs_on if "fallback-financials" in l)
assert "excluded 1 fallback-financials row(s): THININS" in _log_line_on, _log_line_on
print(f"[C2_pool_ineligible_switch_on] switch ON -> THININS (FCF Source=ocf_fallback_financials) "
      f"excluded from the pool, every CLEAN ticker still present, log line {_log_line_on!r} OK")
_clear_scan("S&P 500")
_switch(False)


# ======================================================================
# C3: Deep Dive on-view sourcing - store hit, one-ticker fetch (not
# attempted recently), skip (attempted recently), and a fetch failure
# that still produces the fallback caption. Reconstructs deep_dive_
# engine.py's own income_df-sourcing block (lines ~279-307) verbatim,
# since dd.analyze() itself needs a news/social/price-history harness
# this task's own scope doesn't touch - same precedent as tests/
# test_oneoff_nightly_trigger.py's own CHECK 3.
# ======================================================================
def _deep_dive_income_df(ticker, info, cashflow_df):
    _financials_mode_live = (
        fc.is_financials_store_live() and fc.is_financials(info, ticker=ticker)
    )
    if _financials_mode_live:
        _fin_entry = fis.get(ticker)
        if _fin_entry is not None:
            income_df = _fin_entry["income"]
        elif not fis.deep_dive_attempted_recently(ticker):
            income_df = fis.fetch_for_deep_dive(ticker)
        else:
            income_df = None
    else:
        _bundle = fundamentals_data.peek_cached_bundle(ticker)
        if _bundle:
            income_df = _bundle.get("income")
        elif fve.needs_oneoff_check(cashflow_df):
            income_df = fundamentals_data.get_bundle(ticker).get("income")
        else:
            income_df = None
    return income_df, _financials_mode_live


_switch(True)
_clear_store()

# (a) store hit - fetch_for_deep_dive() is never called.
fis.save("DDHIT", GOOD_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
with mock.patch.object(fis, "fetch_for_deep_dive") as _fetch_mock:
    _income_a, _mode_a = _deep_dive_income_df("DDHIT", BANK_INFO, STABLE_CF)
    _fetch_mock.assert_not_called()
assert _mode_a is True
pd.testing.assert_frame_equal(_income_a.astype(float), GOOD_INCOME.astype(float), check_names=False)
print("[C3a_store_hit] a warm store entry is used directly, fetch_for_deep_dive() never called OK")

# (b) no entry, not attempted recently - fetch_for_deep_dive() called exactly once.
_clear_store()
with mock.patch.object(fis, "fetch_for_deep_dive", return_value=GOOD_INCOME) as _fetch_mock_b:
    _income_b, _ = _deep_dive_income_df("DDFETCH", BANK_INFO, STABLE_CF)
    _fetch_mock_b.assert_called_once_with("DDFETCH")
assert _income_b is GOOD_INCOME
print("[C3b_one_ticker_fetch] no store entry, no recent attempt -> fetch_for_deep_dive() called "
      "exactly once for this one ticker OK")

# (c) no entry, attempted recently (0h ago) - fetch_for_deep_dive() never called, income_df None.
fis.record_deep_dive_attempt("DDSKIP", success=False)
with mock.patch.object(fis, "fetch_for_deep_dive") as _fetch_mock_c:
    _income_c, _ = _deep_dive_income_df("DDSKIP", BANK_INFO, STABLE_CF)
    _fetch_mock_c.assert_not_called()
assert _income_c is None
print("[C3c_recent_attempt_skipped] an attempt recorded moments ago -> fetch_for_deep_dive() "
      "never called again within 24h, income_df stays None OK")

# (d) a genuine fetch failure (fetch_for_deep_dive() returns None, having already
# recorded the failed attempt itself) -> income_df None -> the real DCF still
# falls back to OCF -> the caption formula (deep_dive_engine.py's own
# "fallback_financials_caption" line) comes back True.
_clear_store()
with mock.patch.object(fis, "fetch_for_deep_dive", return_value=None) as _fetch_mock_d:
    _income_d, _mode_d = _deep_dive_income_df("DDFAIL", BANK_INFO, STABLE_CF)
    _fetch_mock_d.assert_called_once_with("DDFAIL")
assert _income_d is None
_dcf_fail, _, _meta_fail = fve.dcf_intrinsic_value(
    "DDFAIL", info=BANK_INFO, cashflow_df=STABLE_CF, currency="USD",
    income_df=_income_d, **_DCF_RATES,
)
_caption = bool(_mode_d and _meta_fail.get("fcf_source") == "ocf_fallback_financials")
assert _caption is True
print(f"[C3d_fetch_failure_caption] a failed one-ticker fetch -> income_df stays None, the real "
      f"DCF still produces a value (${_dcf_fail}) via the OCF fallback, and the caption formula "
      "comes back True (fallback_financials_caption) - the value is never withheld, only "
      "captioned OK")

# Switch OFF -> the financials-mode branch is never entered at all, even
# with a store entry sitting right there (never consulted).
_switch(False)
fis.save("DDOFF", GOOD_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
with mock.patch.object(fis, "fetch_for_deep_dive") as _fetch_mock_off, \
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None):
    _income_off, _mode_off = _deep_dive_income_df("DDOFF", BANK_INFO, STABLE_CF)
    _fetch_mock_off.assert_not_called()
assert _mode_off is False
assert _income_off is None  # peek_cached_bundle() None + needs_oneoff_check(STABLE_CF) False
print("[C3e_switch_off_store_never_consulted] switch OFF -> the Deep Dive financials-mode "
      "branch is never entered, a store entry is never read even though one exists OK")
_clear_store()


# ======================================================================
# C4: scan and Deep Dive give the SAME value for the SAME fixture - both
# source income_df from the identical store entry and feed it through
# the identical dcf_intrinsic_value() dispatch.
# ======================================================================
_switch(True)
fis.save("PARITY1", GOOD_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)

_, _, _scan_kwargs = _run_scan("PARITY1", cashflow_df=STABLE_CF)
_income_scan_path = _scan_kwargs["income_df"]
_income_dd_path, _ = _deep_dive_income_df("PARITY1", BANK_INFO, STABLE_CF)
pd.testing.assert_frame_equal(_income_scan_path.astype(float), _income_dd_path.astype(float),
                               check_names=False)

_dcf_scan, _, _meta_scan = fve.dcf_intrinsic_value(
    "PARITY1", info=BANK_INFO, cashflow_df=STABLE_CF, currency="USD",
    income_df=_income_scan_path, **_DCF_RATES,
)
_dcf_dd, _, _meta_dd = fve.dcf_intrinsic_value(
    "PARITY1", info=BANK_INFO, cashflow_df=STABLE_CF, currency="USD",
    income_df=_income_dd_path, **_DCF_RATES,
)
assert _dcf_scan == _dcf_dd, (_dcf_scan, _dcf_dd)
assert _meta_scan["fcf_source"] == _meta_dd["fcf_source"] == "net_income_financials"
print(f"[C4_scan_and_deep_dive_parity] both paths source the identical income table from the "
      f"store and compute the identical DCF value (${_dcf_scan}) OK")
_switch(False)
_clear_store()


# ======================================================================
# C5: switch OFF -> select_top100_pool()'s output is byte-identical
# whether or not "FCF Source": "ocf_fallback_financials" rows are
# present in the fixture - the exclusion mechanism is a complete no-op
# with the switch off, exactly as before this commit.
# ======================================================================
_clear_scan("S&P 500")
_save_universe("S&P 500", _clean_rows + [_fallback_row])
_logs_off = []
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    _pool_off_with = te.select_top100_pool(log=_logs_off.append)
assert not any("fallback-financials" in l for l in _logs_off), _logs_off
_scores_with = {r["ticker"]: r["value_score"] for r in _pool_off_with}
assert "THININS" in _scores_with, _scores_with  # never excluded with the switch off

_clear_scan("S&P 500")
_save_universe("S&P 500", _clean_rows)
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    _pool_off_without = te.select_top100_pool(log=lambda *a, **k: None)
_scores_without = {r["ticker"]: r["value_score"] for r in _pool_off_without}
for t in _scores_without:
    assert _scores_with[t] == _scores_without[t], (t, _scores_with[t], _scores_without[t])
print("[C5_switch_off_byte_identical] switch OFF -> every CLEAN ticker's value_score is byte-"
      "identical whether or not a fallback-financials-tagged row is present in the same scan "
      "file, and that row is never excluded OK")
_clear_scan("S&P 500")
_patch_sector_fill.stop()
_switch(False)


print("\nALL FINANCIALS INCOME STORE COMMIT 4 CHECKS PASSED")
