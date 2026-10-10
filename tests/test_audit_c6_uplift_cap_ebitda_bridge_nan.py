"""Audit fix C6 (Fable finding V8, 10 Oct 2026, instruction_combined_
10oct.md PART C): "Uplift cap when the raw latest FCF <= 0, and
NaN-filter the EBITDA bridge."

Two independent gaps in fcf_valuation_engine.py's Step 4 one-off
mechanism (normalized_base_and_series()'s own "oneoff_meta" block, see
test_step4_fcf_oneoff.py for that mechanism's full history):

1. UPLIFT CAP, raw latest FCF <= 0 (_ocf_based_base_and_series() and
   _financials_base_and_series()): the safety valve that caps a
   substituted clean-year/EBITDA-bridge base at
   FCF_ONEOFF_UPLIFT_CAP_MULTIPLE (3.0x) times the raw (un-adjusted)
   latest-year figure only ever compared against that raw figure when
   it was POSITIVE (`fcf_base_raw > 0`). A one-off-depressed or loss-
   making latest year - exactly the shape this mechanism exists to
   handle - left `fcf_base_raw` zero or negative, and the cap simply
   never fired: an EBITDA-bridge or median-of-clean-years substitution
   could inflate the base by any multiple with nothing to stop it.

   Fix: compare against abs(fcf_base_raw) instead of requiring
   fcf_base_raw > 0 - a substitution more than 3x the MAGNITUDE of a
   loss-making raw figure is now capped exactly as a substitution more
   than 3x a profitable one already was. A raw of exactly 0 is still
   left uncapped (nothing to multiply against). The existing CZR-shaped
   fixture (positive raw, test_step4_fcf_oneoff.py) is unaffected -
   verified by re-running it below.

2. NaN-FILTER THE EBITDA BRIDGE (_ebitda_bridge_base()): _row() does
   not drop NaN (unlike every OCF/capex caller elsewhere in this
   module, which filters with `if v == v`), so a latest-year Operating
   Income or D&A figure missing from a sparse income-statement year
   silently produced a NaN ebitda_latest/cash_taxes that propagated
   straight through to the returned base - a NaN FCF base that gap 1's
   own cap comparison couldn't catch either (every NaN comparison is
   False in Python), so a NaN base could reach the DCF itself.

   Fix: _ebitda_bridge_base() now returns None (the same "can't build a
   genuine bridge" signal it already returns for a missing oi/da row)
   when either of the two latest-year figures it actually uses (oi[0],
   da[0]) is NaN, before doing any arithmetic with them. A NaN at any
   OTHER position in the row (not the latest year) is irrelevant to
   this function - it only ever reads index 0 - and is confirmed below
   to still produce a normal result.

Both fixes are in fcf_valuation_engine.py only. Neither touches
anything in _PUBLIC_FIELD_MAP, pool selection, or any other module -
this is a correctness fix to an existing internal safety valve, not a
new feature.

Expected live effect (per the instruction's own characterisation): "a
small set of rows" - this only changes tickers whose Step 4 mechanism
BOTH fires AND (a) has a raw latest-year figure <= 0, or (b) hits a
sparse income-statement year with a NaN latest Operating Income or
D&A - both are edge cases within an already-narrow mechanism (Step 4
itself only fires for tickers with income_df available AND a detected
one-off distortion in the 5-year window). Exact tickers affected can't
be enumerated from this sandbox (no live Yahoo data) - any ticker
whose Deep Dive shows "DCF Unreliable" or an outsized DCF alongside a
negative or near-zero most-recent-year OCF/net-income is a candidate
worth checking after this lands.

Run: python3 tests/test_audit_c6_uplift_cap_ebitda_bridge_nan.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import fcf_valuation_engine as fve

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


def _mk_income_df(oi, da):
    cols = {}
    for i in range(len(oi)):
        cols[f"202{6 - i}-06-30"] = [oi[i], da[i]]
    return pd.DataFrame(cols, index=["Operating Income", "Reconciled Depreciation"])


def _mk_ni_income_df(ni, oi, da):
    rows = {"Net Income": ni, "Operating Income": oi, "Reconciled Depreciation": da}
    cols = {}
    n = len(ni)
    for i in range(n):
        cols[f"202{6 - i}-06-30"] = [rows[k][i] for k in rows]
    return pd.DataFrame(cols, index=list(rows.keys()))


# ======================================================================
# CHECK 1: uplift cap now fires when the raw latest-year figure is
# NEGATIVE - a CZR-shaped fixture (same OI/DA/capex as
# test_step4_fcf_oneoff.py's own CZR fixture) but with the latest
# year's OCF shifted down so raw (ocf[0]+avg_capex) is -40 instead of
# +13.7. Clean years (1-4) median to 153, which was never capped
# before this fix (fcf_base_raw > 0 was False for -40, so the whole
# safety valve was skipped).
# ======================================================================
NEG_OCF = [10.0, 203.0, 203.0, 203.0, 203.0]   # raw = 10-50 = -40
NEG_CAPEX = [-50.0] * 5
NEG_CF = _mk_cashflow_df(NEG_OCF, NEG_CAPEX)
NEG_OI = [500.0] * 5    # flat EBITDA - trivially within tolerance every year
NEG_DA = [100.0] * 5
NEG_INCOME = _mk_income_df(NEG_OI, NEG_DA)

_neg_base, _neg_series, _neg_src, _neg_bn, _neg_cb, _neg_meta = fve.normalized_base_and_series(
    NEG_CF, info={}, income_df=NEG_INCOME,
)
check("negative-raw fixture: Step 4 fires via median5_clean (year 0 distorted)",
      _neg_meta["fcf_base_source"] == "median5_clean" and _neg_meta["fcf_distorted_years"] == [0])
check("negative-raw fixture: fcf_base_raw correctly recorded as -40.0 (negative)",
      abs(_neg_meta["fcf_base_raw"] - (-40.0)) < 1e-6)
check("negative-raw fixture: cap NOW fires (fcf_base_capped_by_uplift=True) - before this "
      "fix it never would have, since fcf_base_raw>0 was False for -40",
      _neg_meta["fcf_base_capped_by_uplift"] is True)
check("negative-raw fixture: base is capped at 3.0x the MAGNITUDE of raw (3*40=120), not "
      "left at the uncapped clean median of 153",
      abs(_neg_base - 120.0) < 1e-6)
print(f"  [uplift_cap_fires_negative_raw] raw={_neg_meta['fcf_base_raw']:.1f} (negative), "
      f"uncapped clean median would have been 153.0, cap=3.0x|raw|=120.0 -> base={_neg_base:.1f}, "
      f"fcf_base_capped_by_uplift={_neg_meta['fcf_base_capped_by_uplift']} "
      f"(pre-fix this base would have been 153.0, uncapped)")


# ======================================================================
# CHECK 2: the pre-existing positive-raw CZR fixture (test_step4_
# fcf_oneoff.py) is byte-for-byte unaffected by this fix - same inputs,
# same base, same cap - proving this is additive, not a behaviour
# change for the case that already worked.
# ======================================================================
CZR_OCF = [63.7, 203.0, 203.0, 203.0, 203.0]
CZR_CAPEX = [-50.0] * 5
CZR_CF = _mk_cashflow_df(CZR_OCF, CZR_CAPEX)
CZR_OI = [500.0] * 5
CZR_DA = [100.0] * 5
CZR_INCOME = _mk_income_df(CZR_OI, CZR_DA)

_czr_base, _czr_series, _czr_src, _czr_bn, _czr_cb, _czr_meta = fve.normalized_base_and_series(
    CZR_CF, info={}, income_df=CZR_INCOME,
)
check("pre-existing CZR (positive-raw) fixture: base unchanged at 41.1 (3.0x*13.7)",
      abs(_czr_base - 41.1) < 1e-6)
check("pre-existing CZR fixture: fcf_base_raw unchanged at 13.7 (positive)",
      abs(_czr_meta["fcf_base_raw"] - 13.7) < 1e-6)
check("pre-existing CZR fixture: still capped exactly as before this fix",
      _czr_meta["fcf_base_capped_by_uplift"] is True)


# ======================================================================
# CHECK 3: financials-mode (_financials_base_and_series()) gets the
# identical magnitude-based fix - a loss-making latest-year net income
# (-10) against a clean median of 150 (15x the magnitude) is now
# capped at 3.0x*10=30, never capped before this fix.
# ======================================================================
NI_NEG = [-10.0, 150.0, 150.0, 150.0, 150.0]
NI_OI = [500.0] * 5
NI_DA = [100.0] * 5
NI_INCOME = _mk_ni_income_df(NI_NEG, NI_OI, NI_DA)
_fin_result = fve._financials_base_and_series(NI_INCOME)
check("financials-mode negative-latest-net-income fixture returns a result (not None - "
      ">=2 positive net-income points exist)",
      _fin_result is not None)
_fin_base, _fin_series, _fin_src, _fin_bn, _fin_cb, _fin_meta = _fin_result
check("financials-mode: Step 4 fires via median5_clean",
      _fin_meta["fcf_base_source"] == "median5_clean" and _fin_meta["is_financials_mode"] is True)
check("financials-mode: fcf_base_raw correctly recorded as -10.0 (negative)",
      abs(_fin_meta["fcf_base_raw"] - (-10.0)) < 1e-6)
check("financials-mode: cap NOW fires for the negative raw figure",
      _fin_meta["fcf_base_capped_by_uplift"] is True)
check("financials-mode: base capped at 3.0x|raw|=30.0, not the uncapped clean median of 150",
      abs(_fin_base - 30.0) < 1e-6)


# ======================================================================
# CHECK 4: _ebitda_bridge_base() returns None (never NaN) when the
# latest-year Operating Income is NaN - direct unit test of the
# function this mechanism falls back to when fewer than
# FCF_ONEOFF_MIN_CLEAN_YEARS clean years remain in the window.
# ======================================================================
_oi_nan_latest = [float("nan"), 400.0, 410.0, 420.0, 430.0]
_da_ok = [50.0] * 5
_income_oi_nan = _mk_income_df(_oi_nan_latest, _da_ok)
_bridge_oi_nan = fve._ebitda_bridge_base(_income_oi_nan, avg_capex=-50.0)
check("_ebitda_bridge_base() returns None (not NaN) when the latest-year Operating "
      "Income is NaN - before this fix it returned float('nan'), which the uplift-cap "
      "comparison downstream couldn't catch either (NaN comparisons are always False)",
      _bridge_oi_nan is None)

_da_nan_latest = [float("nan")] + [50.0] * 4
_oi_ok = [400.0] * 5
_income_da_nan = _mk_income_df(_oi_ok, _da_nan_latest)
_bridge_da_nan = fve._ebitda_bridge_base(_income_da_nan, avg_capex=-50.0)
check("_ebitda_bridge_base() also returns None when the latest-year D&A is NaN "
      "(the other half of the same ebitda_latest = oi[0] + da[0] computation)",
      _bridge_da_nan is None)

# A NaN at a NON-latest position is irrelevant to this function (it
# only ever reads index 0) - confirms the fix is targeted, not a
# blanket "any NaN anywhere" rejection.
_oi_mid_nan = [400.0, float("nan"), 410.0, 420.0, 430.0]
_income_mid_nan = _mk_income_df(_oi_mid_nan, _da_ok)
_bridge_mid_nan = fve._ebitda_bridge_base(_income_mid_nan, avg_capex=-50.0)
check("_ebitda_bridge_base() still returns a normal (non-NaN, non-None) value when the "
      "NaN sits at a NON-latest position - this function only reads index 0, so an "
      "older year's NaN is irrelevant to it",
      _bridge_mid_nan is not None and _bridge_mid_nan == _bridge_mid_nan)

# A fully clean fixture gets the exact same bridge value as before -
# proves the fix adds a guard, it doesn't change the formula.
_income_clean = _mk_income_df([400.0] * 5, [50.0] * 5)
_bridge_clean = fve._ebitda_bridge_base(_income_clean, avg_capex=-50.0)
check("_ebitda_bridge_base() with no NaN anywhere is completely unaffected by this fix",
      _bridge_clean is not None and _bridge_clean == _bridge_mid_nan)


# ======================================================================
# CHECK 5: end-to-end through normalized_base_and_series() - a fixture
# shaped so Step 4 fires with FEWER than FCF_ONEOFF_MIN_CLEAN_YEARS
# clean years (forcing the ebitda_bridge path, not median5_clean), and
# the latest-year Operating Income is NaN. Before this fix, the NaN
# would have propagated all the way to the returned base (a NaN DCF
# input); now _ebitda_bridge_base() returns None, oneoff_source stays
# None, and the function correctly falls through to the pre-existing
# Task-10-level base instead of returning NaN.
# ======================================================================
# 3 of the 5 window years one-off-distorted (indices 0,1,2), leaving
# only 2 clean (< FCF_ONEOFF_MIN_CLEAN_YEARS=3) - forces the bridge
# branch. OI/DA held flat so the EBITDA cross-check never itself
# objects to the OCF drop.
BRIDGE_OCF = [50.0, 55.0, 60.0, 400.0, 410.0]
BRIDGE_CAPEX = [-50.0] * 5
BRIDGE_CF = _mk_cashflow_df(BRIDGE_OCF, BRIDGE_CAPEX)
BRIDGE_OI_NAN_LATEST = [float("nan"), float("nan"), float("nan"), 420.0, 430.0]
BRIDGE_DA = [50.0] * 5
BRIDGE_INCOME_NAN = _mk_income_df(BRIDGE_OI_NAN_LATEST, BRIDGE_DA)

_e2e_base, _e2e_series, _e2e_src, _e2e_bn, _e2e_cb, _e2e_meta = fve.normalized_base_and_series(
    BRIDGE_CF, info={}, income_df=BRIDGE_INCOME_NAN,
)
check("end-to-end NaN-bridge fixture: the returned base is a real number, never NaN",
      _e2e_base is None or _e2e_base == _e2e_base)
check("end-to-end NaN-bridge fixture: Step 4's oneoff mechanism did NOT substitute a base "
      "(fcf_base_source falls back to the plain 'ocf-normcapex' tag, not 'ebitda_bridge') "
      "- _ebitda_bridge_base() returning None correctly aborted the substitution",
      _e2e_meta["fcf_base_source"] == "ocf-normcapex")
print(f"  [end_to_end_nan_bridge_falls_through] base={_e2e_base}, "
      f"fcf_base_source={_e2e_meta['fcf_base_source']!r} (never 'ebitda_bridge', never NaN)")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
