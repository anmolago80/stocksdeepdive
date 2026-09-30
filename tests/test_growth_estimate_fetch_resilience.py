"""
Growth-estimate-fetch resilience fix (owner-approved, 28 Sep 2026).

Root cause of the reported bug (live ADP Deep Dive showing "historical avg
(12.0%, capped)" instead of Yahoo's ~10.3% 5y analyst estimate): capm_engine.
get_growth_estimates_5y() was a bare try/except with NO retry and NO
crumb-reset, unlike every other yfinance call site in this codebase (see
nightly_scan.py's own module comment above _YF_RETRY_ATTEMPTS). A single
poisoned yfinance crumb - caused by ANY 429 anywhere else in the process,
not necessarily on this exact ticker - silently and permanently (until the
30-minute cache entry expired) fell back to "no Yahoo coverage", with
nothing logged anywhere (a bare except: pass). Since the growth-rewrite
(0d7ee0b) made Yahoo's estimate the SOLE, decisive growth signal when
available (no longer blended with history), a lost estimate now silently
swaps the whole growth methodology instead of just nudging a blended
number - exactly what was observed on ADP.

This fix:
  1. get_growth_estimates_5y() (and get_risk_free_rate()'s yf.Ticker(...).
     history() call) now reuse nightly_scan.py's own _yf_call_with_retry()/
     _reset_poisoned_yf_crumb() - the SAME helper every other yfinance
     call site in this codebase already uses for this exact failure mode -
     imported lazily (see get_risk_free_rate()'s own docstring for why a
     module-level import would be circular).
  2. A genuine total-failure fetch now logs a WARNING (ticker + exception
     type), instead of vanishing silently.
  3. get_growth_estimates_5y() now returns (value, status) - status
     distinguishes "ok" | "no_coverage" (Yahoo genuinely has nothing on
     this name) | "fetch_failed" (the fetch itself broke, even after
     retrying - Yahoo's real coverage is unknown). Threaded through
     fcf_valuation_engine/resolver_engine/deep_dive_engine/app.py/
     auto_compounder_engine as meta["yahoo_estimate_status"]/
     dd["dcf_yahoo_estimate_status"], so "historical avg" now reads
     "historical avg (no Yahoo estimate)" vs "historical avg (Yahoo
     fetch failed)" instead of always implying the former.
  4. nightly_scan.py's run_universe_scan() logs one summary line per scan:
     counts of tickers using Yahoo 5y / historical avg (no estimate) /
     historical avg (fetch failed) / cap.
  5. get_growth_estimates_5y() logs the growth_estimates DataFrame's own
     index labels once per process (first ticker that returns a real
     DataFrame), so the "+5y"-shaped row name is actually visible in logs.

Run: python3 tests/test_growth_estimate_fetch_resilience.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import capm_engine as ce
import fcf_valuation_engine as fve
import nightly_scan as ns
import resolver_engine
import auto_compounder_engine as ace


def _fake_ticker_factory(sequence):
    """Returns a yf.Ticker(...)-shaped stand-in whose .growth_estimates
    property pops one item off `sequence` per access - either an
    Exception to raise, or a DataFrame/None to return."""
    state = {"calls": 0}

    class _FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def growth_estimates(self):
            state["calls"] += 1
            item = sequence[min(state["calls"] - 1, len(sequence) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

    return _FakeTicker, state


# Growth-never-zero rewrite (30 Sep 2026): Yahoo's row label is "LTG" now
# (see capm_engine.py's own comment on _LTG_LABEL_KEY), not "+5y" - this
# fixture is seeded to match the live shape, not the pre-29-Sep-2026 one.
_FIVE_Y_DF = pd.DataFrame(
    {"stock": [0.01, 0.02, 0.03, 0.04, 0.103]},
    index=["0q", "+1q", "0y", "+1y", "LTG"],
)
_NO_FIVE_Y_DF = pd.DataFrame(
    {"stock": [0.01, 0.02, 0.03, 0.04]},
    index=["0q", "+1q", "0y", "+1y"],
)
# Proves the old code's own bug is fixed: a label MATCH whose row is
# all-NaN must fall through to the next candidate label instead of
# stopping (the old unconditional `break` after the first match).
# LTG is tried first and is NaN here; +5y (the old label, kept as a
# fallback) carries the real value - fetch must return it, not "no_
# coverage".
_LTG_NAN_FALLS_THROUGH_TO_FIVE_Y_DF = pd.DataFrame(
    {"stock": [0.01, 0.02, 0.03, 0.04, float("nan"), 0.114]},
    index=["0q", "+1q", "0y", "+1y", "LTG", "+5y"],
)


# ======================================================================
# CHECK: a poisoned-crumb/429 first call -> reset + retry -> the real
# estimate is returned (status "ok"), not silently dropped to "no
# coverage" or "fetch_failed" - the exact bug this fix addresses.
# ======================================================================
_rate_limit_exc = Exception("YFRateLimitError: Too Many Requests. Rate limited (429)")
_FakeTicker1, _state1 = _fake_ticker_factory([_rate_limit_exc, _FIVE_Y_DF])
with mock.patch.object(ce, "yf") as _fake_yf1, \
     mock.patch("time.sleep", return_value=None), \
     mock.patch.object(ns, "_reset_poisoned_yf_crumb") as _reset_spy:
    _fake_yf1.Ticker.side_effect = _FakeTicker1
    _val, _status = ce.get_growth_estimates_5y("POISONTEST")
assert _status == "ok", _status
assert _val is not None and abs(_val - 0.103) < 1e-9, _val
assert _state1["calls"] == 2, _state1["calls"]  # first call failed, second (the retry) succeeded
assert _reset_spy.called, "a rate-limit-looking failure must reset the poisoned crumb before retrying"
print(f"[poisoned_crumb_retries_then_succeeds] first call 429'd, crumb reset fired, retry "
      f"succeeded -> value={_val:.4f} status={_status!r} in {_state1['calls']} call(s) OK")


# ======================================================================
# CHECK: a genuine "no +5y row" (Yahoo has coverage for OTHER horizons,
# just not this one - or the table is shaped unexpectedly) -> "no
# coverage", NOT "fetch failed". No exception is ever raised here - the
# fetch itself succeeds cleanly, Yahoo just doesn't have this row.
# ======================================================================
_FakeTicker2, _state2 = _fake_ticker_factory([_NO_FIVE_Y_DF])
with mock.patch.object(ce, "yf") as _fake_yf2, \
     mock.patch("time.sleep", return_value=None):
    _fake_yf2.Ticker.side_effect = _FakeTicker2
    _val2, _status2 = ce.get_growth_estimates_5y("NOFIVEYTEST")
assert _val2 is None, _val2
assert _status2 == "no_coverage", _status2
assert _state2["calls"] == 1, _state2["calls"]  # a clean (non-raising) miss is never retried
print(f"[no_five_year_row_is_no_coverage] growth_estimates table with no +5y row (no "
      f"exception raised) -> status={_status2!r}, not 'fetch_failed' OK")

# ======================================================================
# CHECK (growth-never-zero rewrite, 30 Sep 2026): a label MATCH whose
# value is NaN must fall through to the next candidate, not stop - the
# exact bug the old unconditional `break` had. LTG matches first but is
# NaN; +5y (old label, kept as a fallback) has the real value.
# ======================================================================
_FakeTickerLtgFallthrough, _stateLtgFallthrough = _fake_ticker_factory(
    [_LTG_NAN_FALLS_THROUGH_TO_FIVE_Y_DF])
with mock.patch.object(ce, "yf") as _fake_yf_ltg, \
     mock.patch("time.sleep", return_value=None):
    _fake_yf_ltg.Ticker.side_effect = _FakeTickerLtgFallthrough
    _val_ltg, _status_ltg = ce.get_growth_estimates_5y("LTGFALLTHROUGHTEST")
assert _status_ltg == "ok", _status_ltg
assert _val_ltg is not None and abs(_val_ltg - 0.114) < 1e-9, _val_ltg
print(f"[ltg_nan_falls_through_to_five_y] LTG row present but NaN, +5y row has the real "
      f"value -> value={_val_ltg:.4f} status={_status_ltg!r} (old code's unconditional "
      f"break would have returned 'no_coverage' here) OK")

# Same check for a fetch that raises on EVERY attempt (exhausts retries) -
# genuinely "fetch_failed", correctly distinct from the no-coverage case
# just above.
_FakeTicker3, _state3 = _fake_ticker_factory([_rate_limit_exc] * 10)
with mock.patch.object(ce, "yf") as _fake_yf3, \
     mock.patch("time.sleep", return_value=None), \
     mock.patch.object(ce._growth_logger, "warning") as _warn_spy:
    _fake_yf3.Ticker.side_effect = _FakeTicker3
    _val3, _status3 = ce.get_growth_estimates_5y("ALWAYSFAILTEST")
assert _val3 is None, _val3
assert _status3 == "fetch_failed", _status3
assert _state3["calls"] == ns._YF_RETRY_ATTEMPTS, _state3["calls"]  # exhausted every retry attempt
assert _warn_spy.called, "a genuine total failure must log a WARNING"
_warn_args = str(_warn_spy.call_args)
assert "ALWAYSFAILTEST" in _warn_args, _warn_args  # ticker
assert "Exception" in _warn_args, _warn_args  # exception type
print(f"[exhausted_retries_is_fetch_failed] every attempt 429'd ({_state3['calls']} "
      f"attempts) -> status={_status3!r}, a WARNING was logged with the ticker and "
      f"exception type OK")


# ======================================================================
# CHECK: dcf_intrinsic_value()'s meta correctly surfaces yahoo_estimate_
# status on the auto growth path, and NOT on the manual/Cap path (the
# Yahoo lookup is never attempted when growth_rate is supplied
# explicitly).
# ======================================================================
_INFO = {"currentPrice": 100.0, "currency": "USD", "marketCap": 25_000_000_000}
_CASHFLOW_NONE = None

# Growth-never-zero rewrite (30 Sep 2026): estimate_growth() now requires
# a REAL fcf_series of >= MIN_HISTORY_POINTS_FOR_TREND (4) points before
# trusting growth_from_history()'s result (mocking growth_from_history()
# alone, with cashflow_df=None, no longer reaches the "history" branch at
# all - fcf_series itself would be empty). A minimal 4-column cashflow
# statement supplies that length; growth_from_history()'s mocked return
# value (0.12) is what actually determines the resolved rate either way.
_HISTORY_CF = pd.DataFrame(
    {f"202{6 - i}-06-30": [500.0 - i * 10, -20.0] for i in range(4)},
    index=["Operating Cash Flow", "Capital Expenditure"],
)

with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "fetch_failed")), \
     mock.patch.object(fve, "growth_from_history", return_value=0.12), \
     mock.patch.object(fve, "_coeff_of_variation", return_value=0.0):
    _iv, _g, _meta = fve.dcf_intrinsic_value(
        "TEST", info=_INFO, cashflow_df=_HISTORY_CF, currency="USD",
        discount_rate=0.09, perpetual_rate=0.02, manual_fcf=12.20,
        diluted_shares_override=1,
    )
assert _meta["growth_source"] == "history", _meta["growth_source"]
assert _meta["yahoo_estimate_status"] == "fetch_failed", _meta["yahoo_estimate_status"]
print("[dcf_meta_surfaces_fetch_failed] growth fell back to history AND meta correctly "
      "flags yahoo_estimate_status='fetch_failed' (not silently 'no_coverage') OK")

with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")), \
     mock.patch.object(fve, "growth_from_history", return_value=0.12), \
     mock.patch.object(fve, "_coeff_of_variation", return_value=0.0):
    _iv2, _g2, _meta2 = fve.dcf_intrinsic_value(
        "TEST", info=_INFO, cashflow_df=_HISTORY_CF, currency="USD",
        discount_rate=0.09, perpetual_rate=0.02, manual_fcf=12.20,
        diluted_shares_override=1,
    )
assert _meta2["yahoo_estimate_status"] == "no_coverage", _meta2["yahoo_estimate_status"]
print("[dcf_meta_surfaces_no_coverage] a genuine no-coverage result is distinctly flagged "
      "too (not merged with fetch_failed) OK")

# Manual growth override: Yahoo is never even queried, so the status key
# stays None - this must not be misread by any display code as "no
# coverage" (see app.py/auto_compounder_engine's history-only gating).
_iv3, _g3, _meta3 = fve.dcf_intrinsic_value(
    "TEST", info=_INFO, cashflow_df=_CASHFLOW_NONE, currency="USD",
    discount_rate=0.09, perpetual_rate=0.02, growth_rate=0.08, manual_fcf=12.20,
    diluted_shares_override=1,
)
assert _meta3["growth_source"] == "manual", _meta3["growth_source"]
assert _meta3.get("yahoo_estimate_status") is None, _meta3.get("yahoo_estimate_status")
print("[dcf_meta_manual_path_untouched] a manual growth override never touches the Yahoo "
      "lookup - yahoo_estimate_status stays None OK")


# ======================================================================
# CHECK: resolver_engine.resolve_intrinsic_value() passes yahoo_estimate_
# status through unchanged (pure provenance passthrough, same pattern as
# every other *_status/*_source key).
# ======================================================================
_INFO_WITH_SHARES = dict(_INFO, sharesOutstanding=100_000_000)
with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "fetch_failed")), \
     mock.patch.object(fve, "growth_from_history", return_value=0.12), \
     mock.patch.object(fve, "_coeff_of_variation", return_value=0.0):
    _iv_r, _src_r, _g_r, _meta_r = resolver_engine.resolve_intrinsic_value(
        "TEST", None, info=_INFO_WITH_SHARES, cashflow_df=_CASHFLOW_NONE, currency="USD",
        discount_rate=0.09, perpetual_rate=0.02, manual_fcf=1_000_000_000.0,
    )
assert _src_r == "dcf", _src_r
assert _meta_r["yahoo_estimate_status"] == "fetch_failed", _meta_r["yahoo_estimate_status"]
print("[resolver_engine_passthrough] resolve_intrinsic_value()'s meta carries "
      "yahoo_estimate_status straight through from dcf_intrinsic_value() OK")


# ======================================================================
# CHECK: auto_compounder_engine's Fair Value tab "Growth Source" display
# distinguishes the two failure modes, matching app.py's Deep Dive
# caption logic exactly.
# ======================================================================
_bundle = {
    "info": {"trailingEps": 1.0, "currentPrice": 24.10, "currency": "USD",
              "marketCap": 25_500_000_000, "sharesOutstanding": 100_000_000},
    "income_q": None, "income": None, "balance": None, "cashflow": None,
    "prices_10y": {"dates": [], "prices": []},
}
_dcf_result = {"value": 999.0, "growth": 0.15, "perpetual_rate": 0.02, "discount_rate": 0.09}

_canonical_fetch_failed = {
    "value": 42.0, "growth": 0.12, "perpetual_rate": 0.02, "discount_rate": 0.09,
    "discount_tier_label": "mid-cap (US$10B-50B)", "growth_source": "history",
    "growth_end_rate": 0.04, "yahoo_estimate_status": "fetch_failed",
}
_fv1 = ace._build_fair_value(_bundle, "TEST", _dcf_result, _canonical_fetch_failed)
_dcf_inputs1 = {i["label"]: i["value"] for i in _fv1["valuation_inputs"]["TEST"]["dcf"]}
assert "Yahoo fetch failed" in _dcf_inputs1.get("Growth Source", ""), _dcf_inputs1
print("[fair_value_growth_source_fetch_failed] Growth Source row reads "
      f"{_dcf_inputs1.get('Growth Source')!r} OK")

_canonical_no_coverage = dict(_canonical_fetch_failed, yahoo_estimate_status="no_coverage")
_fv2 = ace._build_fair_value(_bundle, "TEST", _dcf_result, _canonical_no_coverage)
_dcf_inputs2 = {i["label"]: i["value"] for i in _fv2["valuation_inputs"]["TEST"]["dcf"]}
assert "no Yahoo estimate" in _dcf_inputs2.get("Growth Source", ""), _dcf_inputs2
print("[fair_value_growth_source_no_coverage] Growth Source row reads "
      f"{_dcf_inputs2.get('Growth Source')!r} OK")

# An "analyst"-sourced growth (Yahoo genuinely used) must NOT get either
# qualifier appended - the distinction only applies when growth_source
# is actually "history".
_canonical_yahoo = dict(_canonical_fetch_failed, growth_source="analyst",
                        yahoo_estimate_status="ok")
_fv3 = ace._build_fair_value(_bundle, "TEST", _dcf_result, _canonical_yahoo)
_dcf_inputs3 = {i["label"]: i["value"] for i in _fv3["valuation_inputs"]["TEST"]["dcf"]}
assert _dcf_inputs3.get("Growth Source") == "Yahoo 5y analyst", _dcf_inputs3
print("[fair_value_growth_source_yahoo_unqualified] an analyst-sourced growth shows plain "
      f"{_dcf_inputs3.get('Growth Source')!r}, no qualifier appended OK")


# ======================================================================
# CHECK: nightly_scan._growth_source_bucket() classifies every combo
# correctly, including "cap" taking priority over the raw source.
# ======================================================================
assert ns._growth_source_bucket({"growth_governor": "Yahoo", "growth_source": "analyst"}) == "yahoo_5y"
assert ns._growth_source_bucket(
    {"growth_governor": "History", "growth_source": "history", "yahoo_estimate_status": "no_coverage"}
) == "history_no_estimate"
assert ns._growth_source_bucket(
    {"growth_governor": "History", "growth_source": "history", "yahoo_estimate_status": "fetch_failed"}
) == "history_fetch_failed"
assert ns._growth_source_bucket({"growth_governor": "Cap", "growth_source": "analyst"}) == "cap"
assert ns._growth_source_bucket({"growth_governor": "Cap", "growth_source": "history"}) == "cap"
assert ns._growth_source_bucket({"growth_governor": "Info", "growth_source": "info"}) == "other"
assert ns._growth_source_bucket({"growth_governor": None, "growth_source": None}) == "other"
print("[growth_source_bucket_classification] all 6 combos (yahoo_5y/history_no_estimate/"
      "history_fetch_failed/cap-over-analyst/cap-over-history/other) bucket correctly OK")

# Growth-never-zero rewrite (30 Sep 2026): "non_positive" (a real Yahoo
# estimate found but <=0, checked before the history branches) and
# "history_volatile" (buckets the same as plain "history" for this
# summary line - both are governed by the SAME yahoo_estimate_status
# distinction).
assert ns._growth_source_bucket(
    {"growth_governor": "Default", "growth_source": "default", "yahoo_estimate_status": "non_positive"}
) == "yahoo_non_positive"
assert ns._growth_source_bucket(
    {"growth_governor": "HistoryVolatile", "growth_source": "history_volatile",
     "yahoo_estimate_status": "no_coverage"}
) == "history_no_estimate"
print("[growth_source_bucket_never_zero_additions] 'non_positive' yahoo_estimate_status buckets "
      "as 'yahoo_non_positive' (checked before the history branches); 'history_volatile' source "
      "buckets the same as plain 'history' OK")


# ======================================================================
# CHECK: analyze_ticker_lite()'s growth_summary_out out-param actually
# accumulates real bucket counts end-to-end (not just the standalone
# bucket helper above) - mock resolve_intrinsic_value's meta directly
# since a real network fetch isn't available here.
# ======================================================================
import datetime
import pandas as _pd

_dates = _pd.date_range("2026-01-01", periods=90, freq="D")
_hist_df = _pd.DataFrame({
    "Close": [100.0 + i * 0.1 for i in range(90)],
    "Volume": [1_000_000] * 90,
}, index=_dates)

with mock.patch.object(ns, "_yf_call_with_retry") as _ytw, \
     mock.patch.object(ns, "resolve_quality_score", return_value=(70, "auto", False)), \
     mock.patch.object(ns, "resolve_intrinsic_value") as _riv, \
     mock.patch.object(ns, "resolve_stock_type", return_value=("COMPOUNDER", "auto", False)), \
     mock.patch.object(ns.reverse_dcf_engine, "compute", return_value={"ok": False}), \
     mock.patch.object(ns.trade_filter_engine, "calc_support_resistance",
                        return_value={"support20": 90, "resistance20": 110, "support60": 85, "resistance60": 115}), \
     mock.patch.object(ns.indicators_engine, "compute_indicators", return_value={"trend": "up"}), \
     mock.patch.object(ns.trade_filter_engine, "evaluate_trade", return_value={"signal": "-"}):
    def _ytw_side_effect(fn, log, ticker, label, **kw):
        if label == "history":
            return _hist_df
        if label == "info":
            return {"currency": "USD"}
        return None
    _ytw.side_effect = _ytw_side_effect
    _riv.return_value = (150.0, "dcf", 0.12, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "fetch_failed", "value_default": False,
    })
    _summary = {}
    _row = ns.analyze_ticker_lite("FAKETICKER", log=lambda *a, **k: None, growth_summary_out=_summary)
assert _row is not None
assert _summary == {"history_fetch_failed": 1}, _summary
print("[analyze_ticker_lite_growth_summary_out] one full analyze_ticker_lite() call with "
      f"a mocked fetch_failed iv_meta correctly increments the out-param: {_summary} OK")


print("\nALL GROWTH-ESTIMATE-FETCH-RESILIENCE FIXTURES PASSED")
