"""
Step 4 (owner-directed, 30 Sep 2026, KO fix). Live symptom: KO Deep Dive
DCF $26.79 vs P/E methods $73-80 and price $86.84 - KO's operating cash
flow was depressed two years running by cash one-offs (2024 fairlife
earn-out, 2025 IRS tax deposit); the pre-existing Task-10 3-year-median
outlier swap can't help when two of the three years it compares are
themselves the distorted ones.

Fix lives in fcf_valuation_engine.py: _oneoff_metric_series(),
_detect_distorted_years(), _ebitda_bridge_base(), and the new block
inside normalized_base_and_series() (see that function's own docstring
for the full algorithm and the return-shape change to a 6-tuple).
income_df is optional and threaded through dcf_intrinsic_value() ->
resolve_intrinsic_value() -> auto_compounder_engine.py's _run_dcf()/
_run_canonical_dcf() and deep_dive_engine.py (see each of those
functions'/modules' own comments on how income_df is sourced - always
opportunistic/cache-only, never a new fetch on the nightly bulk path).

Run: python3 tests/test_step4_fcf_oneoff.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve


def _mk_cashflow_df(ocf, capex):
    """Cash-flow statement DataFrame, most-recent-first columns, same
    shape test_dcf_outlier_guards.py's own _mkdf() uses."""
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


def _mk_income_df(oi, da, pretax=None, tax=None, revenue=None):
    """Income statement DataFrame, most-recent-first columns, same
    year-count/column-naming convention as _mk_cashflow_df() above."""
    rows = {"Operating Income": oi, "Reconciled Depreciation": da}
    if pretax is not None:
        rows["Pretax Income"] = pretax
    if tax is not None:
        rows["Tax Provision"] = tax
    if revenue is not None:
        rows["Total Revenue"] = revenue
    cols = {}
    n = len(oi)
    for i in range(n):
        cols[f"202{6 - i}-06-30"] = [rows[label][i] for label in rows]
    return pd.DataFrame(cols, index=list(rows.keys()))


_COMMON_DCF = dict(discount_rate=0.08, perpetual_rate=0.02, growth_rate=0.05)
_INFO = {
    "sharesOutstanding": 100.0,
    "currency": "USD",
    "financialCurrency": "USD",
    "currentPrice": 50.0,
}


# ======================================================================
# CHECK 1: KO-shaped fixture - two consecutive one-off-depressed years
# (index 0 = 2025 IRS tax deposit, index 1 = 2024 fairlife earn-out),
# 2023/2022/2021 clean. OCF: 2025 recovers +12% off a -51%-depressed
# 2024 (matches the real Dow rescan's own growth-bar description) -
# still >25% below the last clean year (2023), so the two-year
# propagation catches 2025 even though its own year-over-year OCF rose.
# ======================================================================
KO_OCF = [658.56, 588.0, 1200.0, 1150.0, 1100.0]
KO_CAPEX = [-150.0, -150.0, -150.0, -150.0, -150.0]
KO_CF = _mk_cashflow_df(KO_OCF, KO_CAPEX)
# EBITDA (OI + D&A) barely moves 2023->2024 (2.5% drop, well under the
# 10% tolerance) while OCF craters 51% - exactly the one-off signature.
KO_OI = [450.0, 390.0, 400.0, 410.0, 420.0]
KO_DA = [50.0, 50.0, 50.0, 50.0, 50.0]
KO_INCOME = _mk_income_df(KO_OI, KO_DA)

_base_before, _series_before, _src_before, _bn_before, _cb_before, _meta_before = (
    fve.normalized_base_and_series(KO_CF, info={})
)
assert _src_before == "ocf-normcapex", _src_before
assert abs(_base_before - 508.56) < 1e-6, _base_before  # raw latest-year OCF-capex, undistorted view
assert _meta_before["fcf_base_source"] == "ocf-normcapex", _meta_before
assert _meta_before["fcf_distorted_years"] == [], _meta_before
print(f"[ko_before_income_df] no income_df -> base stays the raw/undistorted "
      f"{_base_before} (source={_src_before!r}), mechanism never fires OK")

_base_after, _series_after, _src_after, _bn_after, _cb_after, _meta_after = (
    fve.normalized_base_and_series(KO_CF, info={}, income_df=KO_INCOME)
)
assert _src_after == "ocf-normcapex", _src_after
assert _bn_after is False, _bn_after  # Task-10's OWN swap flag - superseded, not additionally fired
assert _meta_after["fcf_base_source"] == "median5_clean", _meta_after
assert _meta_after["fcf_distorted_years"] == [0, 1], _meta_after  # 2025, 2024 (0-indexed, 0=latest)
assert abs(_meta_after["fcf_base_raw"] - 508.56) < 1e-6, _meta_after
assert abs(_base_after - 1000.0) < 1e-6, _base_after  # median of the 3 clean years' series values
assert _series_after == [950.0, 1000.0, 1050.0] or sorted(_series_after) == [950.0, 1000.0, 1050.0], _series_after
print(f"[ko_distorted_years_detected] income_df supplied -> years [0, 1] (2025, 2024) flagged "
      f"distorted, base corrected from raw {_meta_after['fcf_base_raw']} to normalised "
      f"{_base_after} (source={_meta_after['fcf_base_source']!r}) OK")

iv_before, g_before, meta_before_full = fve.dcf_intrinsic_value(
    "KOTEST", info=_INFO, cashflow_df=KO_CF, currency="USD", **_COMMON_DCF,
)
iv_after, g_after, meta_after_full = fve.dcf_intrinsic_value(
    "KOTEST", info=_INFO, cashflow_df=KO_CF, currency="USD", income_df=KO_INCOME, **_COMMON_DCF,
)
assert meta_before_full.get("fcf_base_source") == "ocf-normcapex", meta_before_full.get("fcf_base_source")
assert meta_after_full.get("fcf_base_source") == "median5_clean", meta_after_full.get("fcf_base_source")
assert meta_after_full.get("fcf_distorted_years") == [0, 1], meta_after_full.get("fcf_distorted_years")
assert meta_after_full.get("fcf_base_raw_per_share") is not None
assert iv_after > iv_before, (iv_before, iv_after)
print(f"[ko_end_to_end_iv] KO-shaped fixture: intrinsic value per share BEFORE the fix "
      f"(no income_df) = {iv_before:.2f}, AFTER (income_df supplied, one-off years excluded) "
      f"= {iv_after:.2f} - normalisation lifts the DCF toward the region the other valuation "
      f"methods already sit in, same direction as the real KO symptom OK")


# ======================================================================
# CHECK 2: genuine-decline fixture - OCF AND the cross-check metric both
# fall ~40% together (a real operating deterioration, not a one-off) ->
# nothing marked distorted, mechanism doesn't fire at all.
# ======================================================================
DECLINE_OCF = [600.0, 1000.0, 980.0, 960.0, 940.0]
DECLINE_CAPEX = [-100.0, -100.0, -100.0, -100.0, -100.0]
DECLINE_CF = _mk_cashflow_df(DECLINE_OCF, DECLINE_CAPEX)
DECLINE_OI = [360.0, 600.0, 590.0, 580.0, 570.0]  # also -40% in lockstep with OCF
DECLINE_DA = [40.0, 40.0, 40.0, 40.0, 40.0]
DECLINE_INCOME = _mk_income_df(DECLINE_OI, DECLINE_DA)

_d_base, _d_series, _d_src, _d_bn, _d_cb, _d_meta = fve.normalized_base_and_series(
    DECLINE_CF, info={}, income_df=DECLINE_INCOME,
)
assert _d_meta["fcf_distorted_years"] == [], _d_meta
assert _d_meta["fcf_base_source"] == "ocf-normcapex", _d_meta
print("[genuine_decline_untouched] OCF -40% with EBITDA also -40% in lockstep -> "
      "nothing flagged distorted, base/series identical to the pre-Step-4 computation OK")


# ======================================================================
# CHECK 3: single-outlier fixture - Task 10's own pre-existing 3-year-
# median swap (no income_df, mechanism never fires) is PRESERVED
# unchanged by this Step-4 addition. Latest year's OCF is a one-off low
# relative to the prior 2 years (a working-capital swing, not a capex
# spike - Task 10's own scenario), no income_df supplied.
# ======================================================================
OUTLIER_OCF = [40.0, 120.0, 130.0, 125.0]
OUTLIER_CAPEX = [-10.0, -10.0, -10.0, -10.0]
OUTLIER_CF = _mk_cashflow_df(OUTLIER_OCF, OUTLIER_CAPEX)

_o_base, _o_series, _o_src, _o_bn, _o_cb, _o_meta = fve.normalized_base_and_series(OUTLIER_CF, info={})
assert _o_src == "ocf-normcapex", _o_src
assert _o_bn is True, _o_bn  # Task-10 swap fired, exactly as it did before Step 4 existed
assert _o_meta["fcf_base_source"] == "ocf-normcapex", _o_meta  # oneoff_meta mirrors plain source - mechanism never ran
assert _o_meta["fcf_distorted_years"] == [], _o_meta
print(f"[single_outlier_task10_preserved] no income_df -> Task 10's own 3-year-median swap "
      f"still fires (base_normalized={_o_bn}), base={_o_base} - Step 4 is purely additive here, "
      f"identical to pre-existing behaviour OK")


# ======================================================================
# CHECK 4: fewer than FCF_ONEOFF_MIN_CLEAN_YEARS (3) clean years remain
# in the window -> falls to the EBITDA bridge.
# ======================================================================
BRIDGE_OCF = [600.0, 600.0, 600.0, 600.0, 1000.0]
BRIDGE_CAPEX = [-80.0, -80.0, -80.0, -80.0, -80.0]
BRIDGE_CF = _mk_cashflow_df(BRIDGE_OCF, BRIDGE_CAPEX)
# EBITDA barely moves year to year (well under the 10% tolerance at
# every step) while OCF drops 40% off the oldest (clean) year and then
# stays flat at that depressed level - the primary test fires once
# (oldest pair) and the two-year propagation carries it through the
# remaining three years, leaving only 1 clean year in the 5-year window.
BRIDGE_OI = [480.0, 470.0, 500.0, 490.0, 460.0]
BRIDGE_DA = [60.0, 60.0, 60.0, 60.0, 60.0]
BRIDGE_PRETAX = [420.0, 410.0, 440.0, 430.0, 400.0]
BRIDGE_TAX = [90.0, 88.0, 92.0, 90.0, 84.0]
BRIDGE_INCOME = _mk_income_df(BRIDGE_OI, BRIDGE_DA, pretax=BRIDGE_PRETAX, tax=BRIDGE_TAX)

_b_base, _b_series, _b_src, _b_bn, _b_cb, _b_meta = fve.normalized_base_and_series(
    BRIDGE_CF, info={}, income_df=BRIDGE_INCOME,
)
assert len(_b_meta["fcf_distorted_years"]) >= 3, _b_meta  # <3 clean years left in the 5-year window
assert _b_meta["fcf_base_source"] == "ebitda_bridge", _b_meta
_expected_bridge = BRIDGE_OI[0] + BRIDGE_DA[0] - 80.0 - BRIDGE_OI[0] * (BRIDGE_TAX[0] / BRIDGE_PRETAX[0])
assert abs(_b_base - _expected_bridge) < 1e-6, (_b_base, _expected_bridge)
print(f"[fewer_than_3_clean_years_ebitda_bridge] {len(_b_meta['fcf_distorted_years'])} distorted "
      f"years leave <3 clean -> base falls to the EBITDA bridge = {_b_base:.2f} "
      f"(source={_b_meta['fcf_base_source']!r}) OK")


# ======================================================================
# CHECK 5: the new fields never enter scoring/selection - grep source,
# not just import success (mirrors this session's established pattern
# for every other display-only field this session has added).
# ======================================================================
import inspect
import ranking_engine
import top100_engine

for _mod, _names in (
    (ranking_engine, ["calculate_long_score"]),
    (top100_engine, ["composite_score"]),
):
    for _name in _names:
        if hasattr(_mod, _name):
            _src = inspect.getsource(getattr(_mod, _name))
            for _field in ("fcf_base_source", "fcf_distorted_years", "fcf_base_raw"):
                assert _field not in _src, f"{_mod.__name__}.{_name} references {_field}!"
print("[no_scoring_leakage] fcf_base_source/fcf_distorted_years/fcf_base_raw never referenced "
      "inside ranking_engine.calculate_long_score or top100_engine.composite_score OK")

# ======================================================================
# CHECK 6 (Push 3 point 7, owner-directed, 30 Sep 2026, stability-signal
# retrofit): CSL-shaped fixture - OCF -35%, operating income -50% (an
# impairment), revenue +5%, gross profit +4%. No D&A row (so the top
# "ebitda_addback" tier can't apply) - the OLD stability signal would
# have fallen straight to operating-income-alone (tier 2 pre-retrofit)
# and wrongly read the write-down-driven -50% OI collapse as a genuine
# operating deterioration, never firing. The retrofit's revenue+gross-
# profit dual tier (both comfortably stable) correctly overrides that
# and fires.
# ======================================================================
# 5 years so the full end-to-end base computation has enough CLEAN
# years (>= FCF_ONEOFF_MIN_CLEAN_YEARS, 3) to median without needing
# the EBITDA bridge (which itself requires a D&A row this fixture
# deliberately omits) - years 1-4 flat/clean, only year 0 (latest)
# carries the CSL-shaped one-off.
CSL_OCF = [650.0, 1000.0, 1000.0, 1000.0, 1000.0]
CSL_CAPEX = [-50.0] * 5
CSL_CF = _mk_cashflow_df(CSL_OCF, CSL_CAPEX)


def _mk_income_df_no_da(oi, revenue, gross_profit):
    """Same shape as _mk_income_df() above but WITHOUT a D&A row -
    forces _oneoff_metric_series() past the ebitda_addback tier."""
    rows = {"Operating Income": oi, "Total Revenue": revenue, "Gross Profit": gross_profit}
    cols = {}
    n = len(oi)
    for i in range(n):
        cols[f"202{6 - i}-06-30"] = [rows[label][i] for label in rows]
    return pd.DataFrame(cols, index=list(rows.keys()))


CSL_OI = [250.0, 500.0, 500.0, 500.0, 500.0]              # year 0: -50%, the impairment's own visible effect
CSL_REVENUE = [1050.0, 1000.0, 1000.0, 1000.0, 1000.0]     # year 0: +5%
CSL_GROSS_PROFIT = [416.0, 400.0, 400.0, 400.0, 400.0]     # year 0: +4%
CSL_INCOME = _mk_income_df_no_da(CSL_OI, CSL_REVENUE, CSL_GROSS_PROFIT)

_metric_series, _secondary_series, _tier = fve._oneoff_metric_series(CSL_INCOME)
assert _tier == "revenue_gross_profit", _tier   # confirms ebitda_addback tier was skipped (no D&A row)
_csl_distorted = fve._detect_distorted_years(CSL_OCF, _metric_series, secondary_metric_series=_secondary_series)
assert _csl_distorted == [True, False, False, False, False], _csl_distorted
print(f"[csl_stability_signal_retrofit_fcf] CSL-shaped (OCF -35%, OI -50% impairment, revenue +5%, "
      f"gross profit +4%, no D&A row) -> tier={_tier!r} (ebitda_addback correctly skipped), "
      f"distorted={_csl_distorted} - the revenue+gross-profit dual signal correctly fires where "
      "operating-income-alone would have wrongly read this as a genuine decline OK")

_csl_base, _csl_series, _csl_src, _csl_bn, _csl_cb, _csl_meta = fve.normalized_base_and_series(
    CSL_CF, info={}, income_df=CSL_INCOME,
)
assert _csl_meta["fcf_distorted_years"] == [0], _csl_meta
print(f"[csl_stability_signal_retrofit_end_to_end] normalized_base_and_series() end-to-end: "
      f"year 0 (latest) flagged distorted, base={_csl_base:.2f} (source={_csl_meta['fcf_base_source']!r}) OK")


print("STEP4_FCF_ONEOFF_SWEEP_DONE")
