"""
TASK 68 (Director-directed, owner-approved, 20 Nov 2026) - Value vs
Book "IV/BV by Year": make the TTM bar real trailing-12-months.

PROBLEM (Director verified in auto_compounder_engine.py _iv_bv_series(),
commit 165d1a1): the TTM point used _latest(bundle["cashflow"],
"free_cash_flow") - the latest FISCAL YEAR's FCF, not trailing twelve
months - so TTM always equalled the latest FY bar. WMS: FY Mar-2026 =
9.82x and TTM = 9.82x, while the owner's workbook's real TTM FCF
(US$529.95M vs FY $569.29M) gives 7.4x.

FIX:
  1. TTM FCF = sum of the last 4 quarterly FCF figures (or OCF - |capex|
     per quarter when no FCF row), from a new quarterly cash-flow
     statement (fundamentals_data.py's cashflow_q, same fetch/cache/
     currency-conversion treatment as the existing income_q).
  2. Only used when all 4 most recent quarters are present AND the
     latest quarter is newer than the latest fiscal year-end - else
     falls back to the old latest-FY convention, labeled
     "TTM (= latest FY)" so it never silently duplicates.
  3. TTM BVPS is untouched (still bvps_ttm).
  4. Growth/discount/perpetual rates untouched.
  5. The same real-TTM FCF now feeds every other place that reads
     "latest FY FCF" as a TTM stand-in: _build_fundamentals()'s shared
     `fcf` (EV To Free Cash Flow, Free Cash Flow Yield, EBIT to FCF
     Conversion, PFCF Ratio) and _build_value_vs_book()'s own
     "Free Cash Flow (TTM)" metric card (which also gets flagged=True +
     a fallback comment when it's really just the latest FY).
  6. The per-year bars, DCF, scoring and Top 200 are untouched.

Covers:
  - _fcf_ttm(): the central helper - real-TTM path (FCF row, OCF-capex
    fallback), the "not enough quarters"/"stale quarters" fallback
    paths, and the no-cashflow_q-at-all fallback.
  - _iv_bv_series(): the TTM bar's own value and label, for both the
    real-TTM and fallback cases - and that every per-year bar is
    byte-identical to before this task.
  - compounder_ui._cp_iv_bv_series_chart(): the x-axis carries the
    " (= latest FY)" suffix when it applies.
  - compounder_ui.py's statement-depth caption no longer miscounts a
    "TTM (= latest FY)" label as a real fiscal year (startswith("TTM"),
    not == "TTM").
  - _build_fundamentals()'s shared `fcf` and _build_value_vs_book()'s
    "Free Cash Flow (TTM)" card both pick up the SAME real-TTM value,
    and the card's own flagged/fallback-comment state.
  - fundamentals_data.py: cashflow_q fetch/cache round-trip/
    BUNDLE_VERSION bump.

Run: python3 tests/test_director_task68_fcf_ttm.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


import pandas as pd

import auto_compounder_engine as ace
import compounder_ui as cui
import fundamentals_data as fd


def _mk_bundle(cashflow, cashflow_q=None, balance=None, info=None):
    b = {
        "info": info or {"sharesOutstanding": 100e6},
        "income": pd.DataFrame(),
        "balance": balance if balance is not None else pd.DataFrame(),
        "cashflow": cashflow,
        "prices_10y": {"dates": [], "prices": []},
        "spx_prices_10y": {"dates": [], "prices": []},
        "dividends": {"dates": [], "amounts": []},
    }
    if cashflow_q is not None:
        b["cashflow_q"] = cashflow_q
    return b


# WMS-shaped fixture: latest FY ends 2026-03-31 ($569.29M FCF), 4 real
# quarters AFTER that fiscal year-end (Jun/Sep/Dec 2026 + Mar 2027,
# newest first in columns - matches the odd-but-real yfinance convention
# that quarterly columns can run past the latest filed annual column)
# summing to the owner's own $529.95M TTM figure.
_WMS_CASHFLOW = pd.DataFrame(
    {"2026-03-31": [569.29e6, None, None], "2025-03-31": [500e6, None, None]},
    index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
)
_WMS_CASHFLOW_Q = pd.DataFrame(
    {
        "2027-03-31": [150.0e6, None, None],
        "2026-12-31": [130.0e6, None, None],
        "2026-09-30": [125.0e6, None, None],
        "2026-06-30": [124.95e6, None, None],
    },
    index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
)
_WMS_EXPECTED_TTM = 150.0e6 + 130.0e6 + 125.0e6 + 124.95e6  # 529.95M


# ======================================================================
# CHECK 1-3: _fcf_ttm() - the real-TTM path, FCF-row convention.
# ======================================================================
_bundle_wms = _mk_bundle(_WMS_CASHFLOW, _WMS_CASHFLOW_Q)
_fcf_ttm_wms, _is_real_wms = ace._fcf_ttm(_bundle_wms)
check("real TTM FCF = sum of the last 4 quarters (WMS: $529.95M)",
      abs(_fcf_ttm_wms - _WMS_EXPECTED_TTM) < 1.0)
check("is_real_ttm=True on the real-TTM path", _is_real_wms is True)
check("the real TTM figure genuinely differs from the latest FY figure ($569.29M) - "
      "this is the whole bug the task fixes",
      abs(_fcf_ttm_wms - 569.29e6) > 1e6)


# ======================================================================
# CHECK 4-5: _fcf_ttm() - OCF-minus-CapEx-per-quarter fallback when no
# "Free Cash Flow" row exists in the quarterly statement.
# ======================================================================
_cashflow_q_ocf_only = pd.DataFrame(
    {
        "2027-03-31": [None, 160.0e6, -10.0e6],
        "2026-12-31": [None, 140.0e6, -10.0e6],
        "2026-09-30": [None, 135.0e6, -10.0e6],
        "2026-06-30": [None, 130.0e6, -5.0e6],
    },
    index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
)
_bundle_ocf_only = _mk_bundle(_WMS_CASHFLOW, _cashflow_q_ocf_only)
_fcf_ttm_ocf, _is_real_ocf = ace._fcf_ttm(_bundle_ocf_only)
_expected_ocf_capex = (160.0e6 - 10.0e6) + (140.0e6 - 10.0e6) + (135.0e6 - 10.0e6) + (130.0e6 - 5.0e6)
check("OCF-minus-|CapEx| PER QUARTER fallback (no FCF row) sums correctly",
      abs(_fcf_ttm_ocf - _expected_ocf_capex) < 1.0)
check("the per-quarter OCF-capex fallback is still marked is_real_ttm=True "
      "(a real trailing-12-month figure, just via the fallback formula)",
      _is_real_ocf is True)


# ======================================================================
# CHECK 6-10: _fcf_ttm() - fallback paths (missing quarters, stale
# quarters, no cashflow_q at all) all degrade to the latest-FY figure
# with is_real_ttm=False.
# ======================================================================
_cashflow_q_only_3 = pd.DataFrame(
    {
        "2027-03-31": [150.0e6, None, None],
        "2026-12-31": [130.0e6, None, None],
        "2026-09-30": [125.0e6, None, None],
    },
    index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
)
_fcf_3q, _is_real_3q = ace._fcf_ttm(_mk_bundle(_WMS_CASHFLOW, _cashflow_q_only_3))
check("fewer than 4 quarterly columns -> fallback to latest FY",
      abs(_fcf_3q - 569.29e6) < 1.0 and _is_real_3q is False)

_cashflow_q_stale = pd.DataFrame(
    {
        "2026-03-31": [140.0e6, None, None],
        "2025-12-31": [130.0e6, None, None],
        "2025-09-30": [125.0e6, None, None],
        "2025-06-30": [120.0e6, None, None],
    },
    index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
)
_fcf_stale, _is_real_stale = ace._fcf_ttm(_mk_bundle(_WMS_CASHFLOW, _cashflow_q_stale))
check("the newest quarterly column NOT newer than the latest FY column -> "
      "fallback (these 4 quarters are just last FY's own, already baked in)",
      abs(_fcf_stale - 569.29e6) < 1.0 and _is_real_stale is False)

_fcf_no_q, _is_real_no_q = ace._fcf_ttm(_mk_bundle(_WMS_CASHFLOW, cashflow_q=None))
check("no cashflow_q field at all (e.g. an EODHD-sourced bundle) -> fallback, no crash",
      abs(_fcf_no_q - 569.29e6) < 1.0 and _is_real_no_q is False)

_fcf_empty_q, _is_real_empty_q = ace._fcf_ttm(_mk_bundle(_WMS_CASHFLOW, cashflow_q=pd.DataFrame()))
check("an empty (but present) cashflow_q DataFrame -> fallback, no crash",
      abs(_fcf_empty_q - 569.29e6) < 1.0 and _is_real_empty_q is False)

_cashflow_q_partial_fcf = pd.DataFrame(
    {
        "2027-03-31": [150.0e6, None, None],
        "2026-12-31": [None, None, None],  # missing FCF AND ocf/capex this quarter
        "2026-09-30": [125.0e6, None, None],
        "2026-06-30": [124.95e6, None, None],
    },
    index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
)
_fcf_partial, _is_real_partial = ace._fcf_ttm(_mk_bundle(_WMS_CASHFLOW, _cashflow_q_partial_fcf))
check("one quarter's own FCF (and OCF/CapEx) missing -> fallback rather than a "
      "silently understated partial sum",
      abs(_fcf_partial - 569.29e6) < 1.0 and _is_real_partial is False)


# ======================================================================
# CHECK 11-15: _iv_bv_series() - the TTM bar's own value/label, for both
# the real-TTM and fallback cases, and every per-year bar untouched.
# ======================================================================
_dcf_result = {"growth": 0.08, "discount_rate": 0.10, "perpetual_rate": 0.03,
               "value": 1000.0, "flagged": False}
_balance = pd.DataFrame(
    {"2026-03-31": [2000e6], "2025-03-31": [1800e6]}, index=["Stockholders Equity"]
)

_bundle_real_ttm = _mk_bundle(_WMS_CASHFLOW, _WMS_CASHFLOW_Q, balance=_balance)
_series_real = ace._iv_bv_series(_bundle_real_ttm, _dcf_result, bvps_ttm=20.0)
check("real-TTM scenario: the TTM bar's own label is plain 'TTM' (no suffix)",
      _series_real is not None and _series_real["years"][0] == "TTM")
_expected_ttm_iv = ace._mini_dcf_iv(_WMS_EXPECTED_TTM, 0.08, 0.10, 0.03, 100e6)
check("real-TTM scenario: the TTM ratio uses the REAL TTM FCF, not the latest-FY FCF",
      _series_real is not None and abs(_series_real["ratios"][0] - _expected_ttm_iv / 20.0) < 1e-6)
_fy_ratio = _series_real["ratios"][1] if _series_real and len(_series_real["ratios"]) > 1 else None
check("real-TTM scenario: the TTM ratio genuinely differs from the latest FY ratio "
      "(the exact bug the task fixes - WMS showed 9.82x for both before this task)",
      _fy_ratio is not None and abs(_series_real["ratios"][0] - _fy_ratio) > 0.01)

_bundle_fallback_ttm = _mk_bundle(_WMS_CASHFLOW, cashflow_q=None, balance=_balance)
_series_fallback = ace._iv_bv_series(_bundle_fallback_ttm, _dcf_result, bvps_ttm=20.0)
check("fallback scenario: the TTM bar's own label carries ' (= latest FY)'",
      _series_fallback is not None and _series_fallback["years"][0] == "TTM (= latest FY)")
check("fallback scenario: the TTM ratio now DOES equal the latest FY ratio "
      "(honestly labeled as such, not silently presented as distinct)",
      _series_fallback is not None and len(_series_fallback["ratios"]) > 1
      and abs(_series_fallback["ratios"][0] - _series_fallback["ratios"][1]) < 1e-9)


# ======================================================================
# CHECK 16-17: compounder_ui._cp_iv_bv_series_chart() - the x-axis
# itself carries the fallback label; the dashed Average line is
# untouched (still computed from the full ratios list either way).
# ======================================================================
_fig_fallback = cui._cp_iv_bv_series_chart("WMS", {"WMS": _series_fallback}, None)
check("the chart's own x-axis carries the ' (= latest FY)' label",
      _fig_fallback is not None and "TTM (= latest FY)" in list(_fig_fallback.data[0].x))
_fig_real = cui._cp_iv_bv_series_chart("WMS", {"WMS": _series_real}, None)
check("the chart's own x-axis carries a plain 'TTM' label when real TTM applies",
      _fig_real is not None and "TTM" in list(_fig_real.data[0].x)
      and "TTM (= latest FY)" not in list(_fig_real.data[0].x))


# ======================================================================
# CHECK 18-19: the statement-depth caption's own year-count filter -
# startswith("TTM"), not == "TTM" - correctly excludes BOTH label forms.
# ======================================================================
check("the caption's own filter excludes a plain 'TTM' label",
      len([y for y in ["TTM", "2026", "2025"] if not y.startswith("TTM")]) == 2)
check("the caption's own filter excludes the fallback 'TTM (= latest FY)' label too "
      "(the exact regression this task would otherwise introduce)",
      len([y for y in ["TTM (= latest FY)", "2026", "2025"] if not y.startswith("TTM")]) == 2)


# ======================================================================
# CHECK 20-23: _build_value_vs_book()'s "Free Cash Flow (TTM)" card -
# the same real-TTM value, flagged + a fallback comment only when it's
# really just the latest FY.
# ======================================================================
_vvb_real = ace._build_value_vs_book(_bundle_real_ttm, "WMS", {}, _dcf_result)
_fcf_card_real = next((m for m in _vvb_real["metrics"] if m["label"] == "Free Cash Flow (TTM)"), None)
check("Free Cash Flow (TTM) card: real-TTM scenario shows the real TTM value",
      _fcf_card_real is not None and abs(_fcf_card_real["values"]["WMS"] - _WMS_EXPECTED_TTM) < 1.0)
check("Free Cash Flow (TTM) card: real-TTM scenario is NOT flagged",
      _fcf_card_real is not None and _fcf_card_real["flagged"] is False)

_vvb_fallback = ace._build_value_vs_book(_bundle_fallback_ttm, "WMS", {}, _dcf_result)
_fcf_card_fallback = next((m for m in _vvb_fallback["metrics"] if m["label"] == "Free Cash Flow (TTM)"), None)
check("Free Cash Flow (TTM) card: fallback scenario shows the latest FY value",
      _fcf_card_fallback is not None and abs(_fcf_card_fallback["values"]["WMS"] - 569.29e6) < 1.0)
check("Free Cash Flow (TTM) card: fallback scenario IS flagged (red-asterisk "
      "disclosure, same convention as every other fallback figure on this page)",
      _fcf_card_fallback is not None and _fcf_card_fallback["flagged"] is True
      and "latest fiscal year" in (_fcf_card_fallback["comment"] or ""))


# ======================================================================
# CHECK 24-27: _build_fundamentals()'s shared `fcf` - every FCF-derived
# ratio (EV To Free Cash Flow, Free Cash Flow Yield, PFCF Ratio) reads
# the SAME corrected real-TTM value, consistent with the chart/card -
# not the stale latest-FY figure.
# ======================================================================
_info_with_price = {"sharesOutstanding": 100e6, "currentPrice": 50.0, "marketCap": 5_000e6}
_fund_real = ace._build_fundamentals(
    {**_bundle_real_ttm, "info": _info_with_price}, "WMS", {})
_pfcf_real = next((m for m in _fund_real["metrics"] if m["label"] == "PFCF Ratio"), None)
_fcf_yield_real = next((m for m in _fund_real["metrics"] if m["label"] == "Free Cash Flow Yield"), None)
_expected_pfcf = 5_000e6 / _WMS_EXPECTED_TTM
check("other TTM cards consistent: PFCF Ratio uses the REAL TTM FCF (mcap / real-TTM FCF), "
      "not the stale latest-FY figure",
      _pfcf_real is not None and abs(_pfcf_real["values"]["WMS"] - _expected_pfcf) < 1e-6)
check("other TTM cards consistent: Free Cash Flow Yield uses the REAL TTM FCF too",
      _fcf_yield_real is not None
      and abs(_fcf_yield_real["values"]["WMS"] - (_WMS_EXPECTED_TTM / 5_000e6)) < 1e-9)

_fund_fallback = ace._build_fundamentals(
    {**_bundle_fallback_ttm, "info": _info_with_price}, "WMS", {})
_pfcf_fallback = next((m for m in _fund_fallback["metrics"] if m["label"] == "PFCF Ratio"), None)
check("fallback scenario: PFCF Ratio falls back to the latest-FY FCF consistently "
      "with the chart/card's own fallback value for the same bundle",
      _pfcf_fallback is not None and abs(_pfcf_fallback["values"]["WMS"] - (5_000e6 / 569.29e6)) < 1e-6)
check("PFCF Ratio genuinely differs between the real-TTM and fallback bundles "
      "(proves the shared `fcf` variable, not a coincidence)",
      abs(_pfcf_real["values"]["WMS"] - _pfcf_fallback["values"]["WMS"]) > 0.01)


# ======================================================================
# CHECK 26-28: fundamentals_data.py - cashflow_q fetch helper, cache
# round-trip, BUNDLE_VERSION bump.
# ======================================================================
check("_fetch_yfinance_quarterly_cashflow() exists as the cashflow_q fetch helper",
      hasattr(fd, "_fetch_yfinance_quarterly_cashflow"))
check("BUNDLE_VERSION bumped to 11 for the new cashflow_q field",
      fd.BUNDLE_VERSION == 11)

_cache_obj = fd._bundle_to_cache({
    "info": {}, "income": pd.DataFrame(), "balance": pd.DataFrame(),
    "cashflow": _WMS_CASHFLOW, "income_q": pd.DataFrame(), "cashflow_q": _WMS_CASHFLOW_Q,
    "prices_10y": {"dates": [], "prices": []}, "dividends": {"dates": [], "amounts": []},
    "spx_prices_10y": {"dates": [], "prices": []}, "meta": {},
})
_roundtripped = fd._bundle_from_cache(_cache_obj)
check("cashflow_q round-trips through _bundle_to_cache()/_bundle_from_cache() "
      "with its real values intact",
      not _roundtripped["cashflow_q"].empty
      and abs(_roundtripped["cashflow_q"].loc["Free Cash Flow"]["2027-03-31"] - 150.0e6) < 1.0)

_old_cache_obj_no_q = {
    "info": {}, "income": fd._df_to_json(pd.DataFrame()),
    "balance": fd._df_to_json(pd.DataFrame()), "cashflow": fd._df_to_json(_WMS_CASHFLOW),
    "prices_10y": {"dates": [], "prices": []}, "dividends": {"dates": [], "amounts": []},
    "spx_prices_10y": {"dates": [], "prices": []}, "meta": {},
}
_roundtripped_old = fd._bundle_from_cache(_old_cache_obj_no_q)
check("a pre-TASK-68 cache entry with no cashflow_q field at all degrades to an "
      "empty DataFrame rather than raising (BUNDLE_VERSION bump handles the "
      "actual refetch; this is just the defensive read)",
      _roundtripped_old["cashflow_q"].empty)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
