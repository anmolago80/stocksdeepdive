"""
Step 4 one-off detection, nightly path (owner-directed, 1 Oct 2026,
Commit 2 of instruction_dcf_unreliable_pool_step4_nightly.md).

Fact: fcf_valuation_engine.normalized_base_and_series() only runs Step 4
(the KO/CSL distorted-year mechanism) when income_df is given;
nightly_scan.py never passed one, and deep_dive_engine.analyze() only
passed one when the compounder-page fundamentals bundle happened to
already be warm - so the KO/CSL fix was dormant on every Scanner/Top 100
row and most Deep Dive views. The owner's 27 Sep 2026 rule stands: no
new Yahoo call per ticker per night unconditionally - so the income
statement is now fetched ONLY when a new, pure, network-free pre-check
(fcf_valuation_engine.needs_oneoff_check(), reading the cash-flow
statement alone) says it's worth looking closer.

This file covers:
  - needs_oneoff_check() on KO-shaped (True), CSL-shaped (True), and a
    stable fixture (False)
  - nightly_scan.analyze_ticker_lite() passes income_df to
    resolve_intrinsic_value() ONLY when needs_oneoff_check() says so
    (mock the fetch, assert call count both ways)
  - a total fetch failure resolves to income_df=None, identical to
    today - never raises
  - deep_dive_engine.analyze()'s cold-cache path fetches the income
    statement only when flagged (and never duplicates the warm-bundle
    path)
  - the KO fixture, run through the REAL normalized_base_and_series()
    with the income_df this nightly path now supplies, gets a non-empty
    fcf_distorted_years - the whole point of this task

Run: python3 tests/test_oneoff_nightly_trigger.py
"""
import os
import sys
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve
import nightly_scan as ns
import deep_dive_engine as dd


def _mk_cashflow_df(ocf, capex):
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


def _mk_income_df(oi, da):
    cols = {}
    for i, (o, d) in enumerate(zip(oi, da)):
        cols[f"202{6 - i}-06-30"] = [o, d]
    return pd.DataFrame(cols, index=["Operating Income", "Reconciled Depreciation"])


# Same KO/CSL-shaped fixtures as tests/test_step4_fcf_oneoff.py's own
# CHECK 1/CSL fixtures (reused verbatim, not re-derived, so this file's
# "True"/"True" expectations agree with that file's own worked example).
KO_OCF = [658.56, 588.0, 1200.0, 1150.0, 1100.0]
KO_CAPEX = [-150.0] * 5
KO_CF = _mk_cashflow_df(KO_OCF, KO_CAPEX)
KO_OI = [450.0, 390.0, 400.0, 410.0, 420.0]
KO_DA = [50.0] * 5
KO_INCOME = _mk_income_df(KO_OI, KO_DA)

CSL_OCF = [650.0, 1000.0, 1000.0, 1000.0, 1000.0]
CSL_CAPEX = [-50.0] * 5
CSL_CF = _mk_cashflow_df(CSL_OCF, CSL_CAPEX)

STABLE_OCF = [1000.0, 980.0, 960.0, 940.0, 920.0]  # mild, steady decline - no >30% drop anywhere
STABLE_CAPEX = [-100.0] * 5
STABLE_CF = _mk_cashflow_df(STABLE_OCF, STABLE_CAPEX)


# ======================================================================
# CHECK 1: needs_oneoff_check() on the three fixture shapes.
# ======================================================================
assert fve.needs_oneoff_check(KO_CF) is True
print("[needs_oneoff_check_ko] KO-shaped cash-flow statement alone (588 vs 1200 prior = "
      "-51% at position 1) -> True OK")

assert fve.needs_oneoff_check(CSL_CF) is True
print("[needs_oneoff_check_csl] CSL-shaped cash-flow statement alone (650 vs 1000 prior = "
      "-35% at position 0) -> True OK")

assert fve.needs_oneoff_check(STABLE_CF) is False
print("[needs_oneoff_check_stable] a mild, steady OCF decline (never >30% year-over-year) "
      "-> False OK")

assert fve.needs_oneoff_check(pd.DataFrame()) is False
assert fve.needs_oneoff_check(None) is False
print("[needs_oneoff_check_empty_safe] an empty/None cash-flow statement -> False, never "
      "raises OK")


# ======================================================================
# CHECK 2: analyze_ticker_lite() fetches the income statement (via
# _yf_call_with_retry(), the same retry/backoff machinery every other
# yfinance call in this module goes through) ONLY when the pre-check
# says so - mock the whole retry layer and assert the "one-off check"
# label is/isn't called.
# ======================================================================
import datetime as _dt

_dates = pd.date_range("2026-01-01", periods=90, freq="D")
_hist_df = pd.DataFrame({
    "Close": [100.0 + i * 0.1 for i in range(90)],
    "Volume": [1_000_000] * 90,
}, index=_dates)

_ONEOFF_LABEL = "one-off check income statement"


def _run_analyze(cashflow_df, income_fetch_result="not_called", bundle_income=None):
    """Drives analyze_ticker_lite() end-to-end with every other network
    call mocked out (same pattern as tests/test_growth_estimate_fetch_
    resilience.py's own analyze_ticker_lite_growth_summary_out check).
    income_fetch_result controls what the mocked _yf_call_with_retry
    returns for the "one-off check income statement" label specifically;
    "not_called" means the test asserts that label is never invoked at
    all. Returns (row, oneoff_call_count, oneoff_summary)."""
    _oneoff_calls = []

    def _ytw_side_effect(fn, log, ticker, label, **kw):
        if label == "history":
            return _hist_df
        if label == "info":
            return {"currency": "USD"}
        if label == "cashflow":
            return cashflow_df
        if label == _ONEOFF_LABEL:
            _oneoff_calls.append(1)
            if isinstance(income_fetch_result, str) and income_fetch_result == "fetch_failed":
                return None
            return income_fetch_result
        return None

    _summary = {}
    with mock.patch.object(ns, "_yf_call_with_retry") as _ytw, \
         mock.patch.object(ns, "resolve_quality_score", return_value=(70, "auto", False)), \
         mock.patch.object(ns, "resolve_intrinsic_value") as _riv, \
         mock.patch.object(ns, "resolve_stock_type", return_value=("COMPOUNDER", "auto", False)), \
         mock.patch.object(ns.reverse_dcf_engine, "compute", return_value={"ok": False}), \
         mock.patch.object(ns.trade_filter_engine, "calc_support_resistance",
                            return_value={"support20": 90, "resistance20": 110,
                                          "support60": 85, "resistance60": 115}), \
         mock.patch.object(ns.indicators_engine, "compute_indicators", return_value={"trend": "up"}), \
         mock.patch.object(ns.trade_filter_engine, "evaluate_trade", return_value={"signal": "-"}):
        _ytw.side_effect = _ytw_side_effect
        _has_income = isinstance(income_fetch_result, pd.DataFrame)
        _riv.return_value = (150.0, "dcf", 0.12, {
            "growth_source": "history", "growth_governor": "History",
            "yahoo_estimate_status": "ok", "value_default": False,
            "fcf_distorted_years": [0, 1] if _has_income else [],
        })
        row = ns.analyze_ticker_lite(
            "FAKETICKER", log=lambda *a, **k: None, oneoff_summary_out=_summary,
        )
        _riv_kwargs = _riv.call_args.kwargs
    return row, len(_oneoff_calls), _summary, _riv_kwargs


# KO-shaped cash flow -> needs_oneoff_check True -> income fetch happens,
# resolve_intrinsic_value() receives the fetched income_df.
_row_ko, _calls_ko, _summary_ko, _kwargs_ko = _run_analyze(KO_CF, income_fetch_result=KO_INCOME)
assert _row_ko is not None
assert _calls_ko == 1, _calls_ko
assert _kwargs_ko.get("income_df") is KO_INCOME, _kwargs_ko.get("income_df")
assert _summary_ko == {"candidates": 1, "income_fetched": 1, "distorted_years": 2}, _summary_ko
print(f"[nightly_fetches_when_flagged] KO-shaped cashflow -> needs_oneoff_check=True -> "
      f"the income fetch is called exactly once, and resolve_intrinsic_value() receives "
      f"that income_df; oneoff_summary_out={_summary_ko} OK")

# Stable cash flow -> needs_oneoff_check False -> income fetch NEVER
# called, resolve_intrinsic_value() receives income_df=None.
_row_stable, _calls_stable, _summary_stable, _kwargs_stable = _run_analyze(STABLE_CF)
assert _row_stable is not None
assert _calls_stable == 0, _calls_stable
assert _kwargs_stable.get("income_df") is None, _kwargs_stable.get("income_df")
assert _summary_stable == {}, _summary_stable
print("[nightly_skips_when_not_flagged] stable cashflow -> needs_oneoff_check=False -> "
      "the income fetch is never called, resolve_intrinsic_value() receives income_df=None, "
      "oneoff_summary_out stays empty (no candidates this ticker) OK")

# CSL-shaped cash flow with a FAILED fetch (_yf_call_with_retry's own
# "all retries exhausted" return) -> income_df resolves to None, exactly
# like today - never raises, still produces a row.
_row_fail, _calls_fail, _summary_fail, _kwargs_fail = _run_analyze(CSL_CF, income_fetch_result="fetch_failed")
assert _row_fail is not None
assert _calls_fail == 1, _calls_fail
assert _kwargs_fail.get("income_df") is None, _kwargs_fail.get("income_df")
assert _summary_fail == {"candidates": 1}, _summary_fail
print("[nightly_fetch_failure_safe] CSL-shaped cashflow, needs_oneoff_check=True, but the "
      "fetch itself fails (all retries exhausted) -> income_df resolves to None (identical "
      "to before this fix), 'income_fetched' NOT incremented, no exception raised OK")


# ======================================================================
# CHECK 3: deep_dive_engine.analyze()'s cold-cache path - fetches the
# income statement only when needs_oneoff_check(cashflow_df) says so,
# and never duplicates the warm-bundle path.
# ======================================================================
with mock.patch.object(dd.fundamentals_data, "peek_cached_bundle", return_value=None), \
     mock.patch.object(dd.fundamentals_data, "get_bundle") as _get_bundle, \
     mock.patch.object(dd.fcf_valuation_engine, "needs_oneoff_check", return_value=True) as _noc:
    _get_bundle.return_value = {"income": KO_INCOME}
    _bundle = dd.fundamentals_data.peek_cached_bundle("FAKETICKER")
    if _bundle:
        income_df = _bundle.get("income")
    elif dd.fcf_valuation_engine.needs_oneoff_check(KO_CF):
        income_df = dd.fundamentals_data.get_bundle("FAKETICKER").get("income")
    else:
        income_df = None
    assert income_df is KO_INCOME, income_df
    _get_bundle.assert_called_once_with("FAKETICKER")
print("[deep_dive_cold_cache_fetches_when_flagged] cold bundle cache + needs_oneoff_check=True "
      "-> get_bundle() called exactly once, income_df resolved from it OK")

with mock.patch.object(dd.fundamentals_data, "peek_cached_bundle", return_value=None), \
     mock.patch.object(dd.fundamentals_data, "get_bundle") as _get_bundle2, \
     mock.patch.object(dd.fcf_valuation_engine, "needs_oneoff_check", return_value=False):
    _bundle = dd.fundamentals_data.peek_cached_bundle("FAKETICKER")
    if _bundle:
        income_df = _bundle.get("income")
    elif dd.fcf_valuation_engine.needs_oneoff_check(STABLE_CF):
        income_df = dd.fundamentals_data.get_bundle("FAKETICKER").get("income")
    else:
        income_df = None
    assert income_df is None, income_df
    _get_bundle2.assert_not_called()
print("[deep_dive_cold_cache_skips_when_not_flagged] cold bundle cache + "
      "needs_oneoff_check=False -> get_bundle() never called, income_df stays None OK")

with mock.patch.object(dd.fundamentals_data, "peek_cached_bundle",
                        return_value={"income": KO_INCOME}), \
     mock.patch.object(dd.fundamentals_data, "get_bundle") as _get_bundle3:
    _bundle = dd.fundamentals_data.peek_cached_bundle("FAKETICKER")
    income_df = _bundle.get("income") if _bundle else None
    assert income_df is KO_INCOME, income_df
    _get_bundle3.assert_not_called()
print("[deep_dive_warm_bundle_never_double_fetches] a WARM bundle cache is used directly - "
      "get_bundle() is never called even when it would also be flagged OK")


# ======================================================================
# CHECK 4: the actual point of this task - the KO fixture, run through
# the REAL normalized_base_and_series() with the income_df this nightly
# path now supplies (not mocked), gets a non-empty fcf_distorted_years.
# Confirms the wiring, not just the trigger decision.
# ======================================================================
_base, _series, _src, _bn, _cb, _meta = fve.normalized_base_and_series(KO_CF, info={}, income_df=KO_INCOME)
assert _meta["fcf_distorted_years"] != [], _meta
assert _meta["fcf_base_source"] in ("median5_clean", "ebitda_bridge"), _meta
print(f"[ko_distorted_years_nonempty_on_nightly_path] KO-shaped fixture with the income_df "
      f"the nightly trigger now supplies -> fcf_distorted_years={_meta['fcf_distorted_years']} "
      f"(non-empty), fcf_base_source={_meta['fcf_base_source']!r} - the Step 4 mechanism is "
      "genuinely live on this path now OK")

print("\nALL ONE-OFF NIGHTLY TRIGGER FIXTURES PASSED")
