"""Audit fix C1 (Fable findings V2 + V9, 10 Oct 2026, instruction_
combined_10oct.md PART C): a DCF base derived from an income_df that
fundamentals_data.get_bundle()/peek_cached_bundle() already converted
to listing currency was getting converted a SECOND time, because the
caller never threaded that fact into dcf_intrinsic_value()'s own
income_df_currency_converted parameter (added by Addendum 2 item 1,
5 Oct 2026, for the financials_income_store path only).

V2 - two callers of get_bundle()/peek_cached_bundle() never set the
flag at all:
  - nightly_scan.analyze_ticker_lite()'s one-off-check fetch branch
    (`elif fcf_valuation_engine.needs_oneoff_check(cashflow_df):`).
  - deep_dive_engine.analyze()'s own peek_cached_bundle()/get_bundle()
    branches (the non-financials-mode `else:` block).
Both now set _income_df_currency_converted = True right after a
successful fetch, mirroring the already-correct financials_income_
store branch next to them.

V9 - even with the flag correctly set, fcf_valuation_engine.
dcf_intrinsic_value()'s own skip check only recognised the financials-
mode "net_income_financials" fcf_source - never the EBITDA bridge
(meta["fcf_base_source"] == "ebitda_bridge", see _ebitda_bridge_
base()'s own docstring), which ALSO computes its base from income_df's
own operating-income/D&A/tax rows (only its capex term comes from
cashflow_df, same as every other OCF-based path). Broadened the skip
check to also cover that case.

Expected live effect (owner's own report): a financials-reports-in-
USD/lists-on-ASX name (e.g. QBE.AX) whose one-off check or EBITDA-
bridge fallback fires loses a double USD->AUD conversion - IV ~-34%
once fixed (verified below to the sandbox's own static USD->AUD
1.52 fallback rate: 1 - 1/1.52 = -34.2%, matching the owner's own
"~-34%" figure almost exactly).

Verified NOT to affect: the pre-existing "median5_clean" one-off path
(KO's own fixture, Step 4) - its substituted value comes from the
cash-flow-based series, not income_df, so it must still convert
normally even with income_df_currency_converted=True; and the
already-correct "net_income_financials" skip, which must keep working
exactly as Addendum 2 item 1 shipped it (regression check).

Run: python3 tests/test_audit_c1_income_df_currency_double_conversion.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import fcf_valuation_engine as fve
import nightly_scan
import deep_dive_engine

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


def _mk_cashflow_df(ocf, capex):
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


def _mk_income_df(oi, da, pretax=None, tax=None, ni=None):
    rows = {}
    if ni is not None:
        rows["Net Income"] = ni
    rows["Operating Income"] = oi
    rows["Reconciled Depreciation"] = da
    if pretax is not None:
        rows["Pretax Income"] = pretax
    if tax is not None:
        rows["Tax Provision"] = tax
    cols = {}
    n = len(oi)
    for i in range(n):
        cols[f"202{6 - i}-06-30"] = [rows[label][i] for label in rows]
    return pd.DataFrame(cols, index=list(rows.keys()))


_COMMON_DCF = dict(discount_rate=0.08, perpetual_rate=0.02, growth_rate=0.05)

# ======================================================================
# FIXTURE: QBE.AX-shaped - EBITDA bridge path (reused verbatim from the
# already-verified Step 4 fixture, tests/test_step4_fcf_oneoff.py CHECK
# 4 - "fewer than 3 clean years -> EBITDA bridge").
# ======================================================================
BRIDGE_OCF = [600.0, 600.0, 600.0, 600.0, 1000.0]
BRIDGE_CAPEX = [-80.0, -80.0, -80.0, -80.0, -80.0]
BRIDGE_CF = _mk_cashflow_df(BRIDGE_OCF, BRIDGE_CAPEX)
BRIDGE_OI = [480.0, 470.0, 500.0, 490.0, 460.0]
BRIDGE_DA = [60.0, 60.0, 60.0, 60.0, 60.0]
BRIDGE_PRETAX = [420.0, 410.0, 440.0, 430.0, 400.0]
BRIDGE_TAX = [90.0, 88.0, 92.0, 90.0, 84.0]
BRIDGE_INCOME = _mk_income_df(BRIDGE_OI, BRIDGE_DA, pretax=BRIDGE_PRETAX, tax=BRIDGE_TAX)

_QBE_INFO = {
    "financialCurrency": "USD", "currency": "AUD",
    "sharesOutstanding": 100.0, "marketCap": 5_000_000_000,
}

_b_base, _b_series, _b_src, _b_bn, _b_cb, _b_meta = fve.normalized_base_and_series(
    BRIDGE_CF, info={}, income_df=BRIDGE_INCOME,
)
check("fixture sanity: the bridge fixture still triggers the EBITDA "
      "bridge path (fcf_base_source == 'ebitda_bridge'), same as "
      "test_step4_fcf_oneoff.py's own CHECK 4",
      _b_meta["fcf_base_source"] == "ebitda_bridge")

iv_buggy, _, meta_buggy = fve.dcf_intrinsic_value(
    "QBETEST", info=_QBE_INFO, cashflow_df=BRIDGE_CF, income_df=BRIDGE_INCOME,
    currency="AUD", income_df_currency_converted=False, **_COMMON_DCF,
)
iv_fixed, _, meta_fixed = fve.dcf_intrinsic_value(
    "QBETEST", info=_QBE_INFO, cashflow_df=BRIDGE_CF, income_df=BRIDGE_INCOME,
    currency="AUD", income_df_currency_converted=True, **_COMMON_DCF,
)

check("V9: with income_df_currency_converted=False (the pre-fix caller "
      "state), the EBITDA-bridge base still gets converted USD->AUD "
      "(the bug this closes)",
      meta_buggy.get("fx_converted") == "USD->AUD")
check("V9: with income_df_currency_converted=True (the fixed caller "
      "state), the EBITDA-bridge base is NOT converted again - marked "
      "fx_converted_upstream instead",
      meta_fixed.get("fx_converted") is None
      and meta_fixed.get("fx_converted_upstream") == "USD->AUD")
check("V9: both runs actually used the EBITDA bridge (fcf_base_source)",
      meta_buggy.get("fcf_base_source") == "ebitda_bridge"
      and meta_fixed.get("fcf_base_source") == "ebitda_bridge")
check("V9: the fix REDUCES intrinsic value (double conversion was "
      "inflating it) - fixed < buggy",
      iv_fixed > 0 and iv_fixed < iv_buggy)

_pct_change = (iv_fixed - iv_buggy) / iv_buggy
check(f"V9: the magnitude matches the owner's own '~-34%' report to the "
      f"sandbox's static USD->AUD fallback rate (actual: {_pct_change:.1%})",
      -0.40 < _pct_change < -0.28)

print(f"  [ticker-level] QBE.AX-shaped fixture: old (buggy, double-converted) "
      f"IV={iv_buggy:.2f} AUD -> new (fixed) IV={iv_fixed:.2f} AUD "
      f"({_pct_change:+.1%})")


# ======================================================================
# REGRESSION: the pre-existing "net_income_financials" skip (Addendum 2
# item 1) must keep working exactly as before - a simple financials-
# mode fixture with NO distortion, so it returns net_income_financials
# directly (not median5_clean/ebitda_bridge).
# ======================================================================
NI_INCOME = _mk_income_df(
    oi=[300.0, 300.0, 300.0], da=[0.0, 0.0, 0.0], ni=[300.0, 290.0, 310.0],
)
_NI_INFO = {
    "sector": "Financial Services", "industry": "Insurance",
    "financialCurrency": "USD", "currency": "AUD",
    "sharesOutstanding": 100.0, "marketCap": 2_000_000_000,
}
_CF_UNUSED = _mk_cashflow_df([100.0, 100.0, 100.0], [-10.0, -10.0, -10.0])

_ni_base, _ni_series, _ni_src, *_ = fve.normalized_base_and_series(
    _CF_UNUSED, info=_NI_INFO, income_df=NI_INCOME,
)
check("regression fixture sanity: the net-income fixture hits the "
      "net_income_financials path directly (no distortion)",
      _ni_src == "net_income_financials")

_, _, ni_meta_false = fve.dcf_intrinsic_value(
    "NITEST", info=_NI_INFO, cashflow_df=_CF_UNUSED, income_df=NI_INCOME,
    currency="AUD", income_df_currency_converted=False, **_COMMON_DCF,
)
_, _, ni_meta_true = fve.dcf_intrinsic_value(
    "NITEST", info=_NI_INFO, cashflow_df=_CF_UNUSED, income_df=NI_INCOME,
    currency="AUD", income_df_currency_converted=True, **_COMMON_DCF,
)
check("regression: net_income_financials still converts when the flag "
      "is False (unchanged pre-existing behaviour)",
      ni_meta_false.get("fx_converted") == "USD->AUD")
check("regression: net_income_financials still SKIPS conversion when "
      "the flag is True (Addendum 2 item 1's own mechanism, untouched)",
      ni_meta_true.get("fx_converted") is None
      and ni_meta_true.get("fx_converted_upstream") == "USD->AUD")


# ======================================================================
# REGRESSION: "median5_clean" must NEVER skip conversion, even with
# income_df_currency_converted=True - its substituted value comes from
# the cash-flow-based series, not income_df (unlike ebitda_bridge).
# KO's own fixture, reused verbatim from test_step4_fcf_oneoff.py.
# ======================================================================
KO_OCF = [658.56, 588.0, 1200.0, 1150.0, 1100.0]
KO_CAPEX = [-150.0, -150.0, -150.0, -150.0, -150.0]
KO_CF = _mk_cashflow_df(KO_OCF, KO_CAPEX)
KO_OI = [450.0, 390.0, 400.0, 410.0, 420.0]
KO_DA = [50.0, 50.0, 50.0, 50.0, 50.0]
KO_INCOME = _mk_income_df(KO_OI, KO_DA)

_ko_base, _ko_series, _ko_src, *_ = fve.normalized_base_and_series(
    KO_CF, info={}, income_df=KO_INCOME,
)
check("fixture sanity: KO's own fixture still triggers median5_clean, "
      "same as test_step4_fcf_oneoff.py's own check",
      _ko_src == "ocf-normcapex")

_ko_info = {"financialCurrency": "USD", "currency": "AUD", "sharesOutstanding": 100.0,
            "marketCap": 2_000_000_000}
_, _, ko_meta_true = fve.dcf_intrinsic_value(
    "KOTEST", info=_ko_info, cashflow_df=KO_CF, income_df=KO_INCOME,
    currency="AUD", income_df_currency_converted=True, **_COMMON_DCF,
)
check("median5_clean is NOT in the broadened skip condition - still "
      "converts even when income_df_currency_converted=True (its value "
      "comes from cash-flow data, not income_df)",
      ko_meta_true.get("fx_converted") == "USD->AUD")
check("...and fcf_base_source really was median5_clean for this run "
      "(confirms the check above is meaningful, not vacuous)",
      ko_meta_true.get("fcf_base_source") == "median5_clean")


# ======================================================================
# V2 wiring: nightly_scan.py / deep_dive_engine.py now set the flag in
# the two previously-missing branches. Source inspection - driving the
# full analyze_ticker_lite()/analyze() pipeline through these exact
# branches needs heavy get_bundle()/peek_cached_bundle() mocking with
# no behavioural change of its own left to prove beyond "the flag is
# now set", which source inspection answers directly (same pattern
# already used in this session for B2/B3's own wiring proofs).
# ======================================================================
_ns_src = inspect.getsource(nightly_scan.analyze_ticker_lite)
_ns_oneoff_branch = _ns_src[_ns_src.index("elif fcf_valuation_engine.needs_oneoff_check"):]
check("nightly_scan.py: the one-off-check branch now sets "
      "_income_df_currency_converted = True after a successful fetch",
      "_income_df_currency_converted = True" in _ns_oneoff_branch[:_ns_oneoff_branch.index("Commit 2 of instruction_financials_income_store")])

_dd_src = inspect.getsource(deep_dive_engine.analyze)
_dd_else_branch = _dd_src[_dd_src.index("else:\n        _bundle = fundamentals_data.peek_cached_bundle"):]
_dd_else_branch = _dd_else_branch[:_dd_else_branch.index("intrinsic_value, intrinsic_src")]
check("deep_dive_engine.py: both the peek_cached_bundle() and "
      "get_bundle() branches now set _income_df_currency_converted = "
      "True after a successful fetch",
      _dd_else_branch.count("_income_df_currency_converted = True") == 2)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
