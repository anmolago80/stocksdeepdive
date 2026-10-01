"""
Push 3 (owner-directed, 30 Sep 2026, from instruction_final_tonight.md):
earnings one-off normalisation - the EPS-side twin of fcf_valuation_
engine.py's Step 4 FCF mechanism. Live symptom: CSL.AX Fair Value tab -
trailing EPS -$7.51 (a one-off write-down) x average P/E 24.31 = PE
Trailing "intrinsic value" -$182.58; Rational Compounder -$42.28 with
Equity Growth 0.0%; the four-method average -$21.99; PE Forward missing
entirely. A negative EPS is not a low valuation, it is no valuation.

Fix lives in auto_compounder_engine.py: EPS_ONEOFF_DROP/EPS_ONEOFF_
EBITDA_TOLERANCE/EPS_ONEOFF_TWO_YEAR_THRESHOLD, _normalized_eps()
(reusing fcf_valuation_engine's own retrofitted _oneoff_metric_series()/
_detect_distorted_years() - see tests/test_step4_fcf_oneoff.py's own
CSL-shaped fixture for the shared stability-signal retrofit itself),
and the _pe_forward_method()/_equity_10y_method()/_build_fair_value()
rewrites.

Run: python3 tests/test_eps_oneoff_normalisation.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace


def _mk_income_df(diluted_eps, oi, da, net_income=None, revenue=None, gross_profit=None):
    """Income statement DataFrame, most-recent-first columns - same
    convention as tests/test_step4_fcf_oneoff.py's own _mk_income_df()."""
    rows = {"Diluted EPS": diluted_eps, "Operating Income": oi, "Reconciled Depreciation": da}
    if net_income is not None:
        rows["Net Income"] = net_income
    if revenue is not None:
        rows["Total Revenue"] = revenue
    if gross_profit is not None:
        rows["Gross Profit"] = gross_profit
    cols = {}
    n = len(diluted_eps)
    for i in range(n):
        cols[f"202{6 - i}-06-30"] = [rows[label][i] for label in rows]
    return pd.DataFrame(cols, index=list(rows.keys()))


def _bundle(income_df, trailing_eps=None, forward_eps=None, price=100.0, currency="USD",
            trailing_pe=None):
    info = {"currentPrice": price, "currency": currency, "sharesOutstanding": 100_000_000}
    if trailing_eps is not None:
        info["trailingEps"] = trailing_eps
    if forward_eps is not None:
        info["forwardEps"] = forward_eps
    if trailing_pe is not None:
        # No price history in this fixture (prices_10y is empty and
        # trailing EPS itself may be negative, so _avg_pe_3pt()'s own
        # 3 legs all skip) - trailingPE lets it fall back to a fixed
        # multiple, same as a real bundle with no usable price history
        # would.
        info["trailingPE"] = trailing_pe
    return {
        "info": info, "income": income_df, "income_q": None,
        "balance": None, "cashflow": None, "dividends": {},
        "prices_10y": {"dates": [], "prices": []},
    }


# ======================================================================
# CHECK 1: CSL-shaped fixture - one negative year (a one-off write-down),
# EBITDA flat -> normalised EPS from the 4 clean years, PE Trailing
# positive, average over 3 methods (PE Forward via forwardEps, PE
# Trailing, DCF - no balance-sheet data in this fixture so Equity 10y
# is naturally absent), captions present.
# ======================================================================
CSL_EPS = [-7.51, 8.20, 7.80, 7.50, 7.20]     # year 0 (latest): one-off write-down
CSL_OI = [500.0, 495.0, 490.0, 485.0, 480.0]  # flat - the write-down sits below operating income
CSL_DA = [50.0] * 5
CSL_INCOME = _mk_income_df(CSL_EPS, CSL_OI, CSL_DA)

_csl_normalized = ace._normalized_eps({"income": CSL_INCOME, "info": {}, "income_q": None})
assert _csl_normalized["source"] == "median5_clean", _csl_normalized
assert _csl_normalized["distorted_years"] == [0], _csl_normalized
assert abs(_csl_normalized["raw"] - (-7.51)) < 1e-9, _csl_normalized
assert abs(_csl_normalized["value"] - 7.80) < 1e-9, _csl_normalized   # median of [7.20,7.50,7.80,8.20]
print(f"[csl_normalized_eps] one negative year (-$7.51, a one-off write-down) flagged distorted, "
      f"EBITDA stayed flat -> normalised EPS = ${_csl_normalized['value']:.2f} (median of the 4 "
      f"clean years), raw latest-year EPS ${_csl_normalized['raw']:.2f} kept for the caption OK")

_csl_bundle = _bundle(CSL_INCOME, trailing_eps=-7.51, forward_eps=9.00, price=200.0, trailing_pe=24.31)
_dcf_result = {"value": 220.0, "growth": 0.08, "perpetual_rate": 0.02, "discount_rate": 0.09}
_canonical_dcf_result = dict(_dcf_result)
_fv = ace._build_fair_value(_csl_bundle, "CSLTEST", _dcf_result, _canonical_dcf_result)
_methods = _fv["valuation_methods"]["CSLTEST"]
_reasons = _fv["valuation_method_reasons"].get("CSLTEST", {})

# BEFORE (what the raw, un-normalised trailing EPS would have done -
# the exact CSL live symptom): -7.51 x avg_pe would have been negative.
_avg_pe_csl = ace._avg_pe_3pt(_csl_bundle)
_pe_trailing_before = -7.51 * _avg_pe_csl if _avg_pe_csl else None
assert _pe_trailing_before is not None and _pe_trailing_before < 0, _pe_trailing_before

# AFTER: PE Trailing is now positive (normalised EPS x avg P/E).
assert "pe_trailing" in _methods, (_methods, _reasons)
assert _methods["pe_trailing"] > 0, _methods["pe_trailing"]
assert "pe_trailing" not in _reasons
print(f"[csl_before_after_pe_trailing] BEFORE (raw -$7.51 EPS x avg P/E {_avg_pe_csl:.2f}) = "
      f"${_pe_trailing_before:.2f} (negative, the exact live symptom) -> AFTER (normalised $7.80 "
      f"EPS x same avg P/E) = ${_methods['pe_trailing']:.2f} (positive) OK")

# PE Forward: Yahoo's own forwardEps (9.00) used as the STARTING point
# - unaffected by the trailing-EPS write-down - then compounded the
# remaining 4 years at g_earn (growth 1y-blend / PE Forward year-5 fix,
# 1 Oct 2026, owner-directed, KNSL/Kinsale Capital live case: forwardEps
# is Yahoo's NEXT-FISCAL-YEAR consensus, not a year-5 figure, so it's no
# longer used directly as forecast_eps_5y - see _pe_forward_method()'s
# own docstring). g_earn here is _dcf_result["growth"] = 0.08, so
# forecast_eps_5y = 9.00 x 1.08^4 = 12.24, shown as a combined
# "Forward EPS (next FY): $9.00 -> Forecast EPS (yr 5): $12.24" string
# under the (renamed) "Forecast EPS (yr 5)" label - not the bare 9.00
# this test asserted before the fix.
assert "pe_forward" in _methods, (_methods, _reasons)
assert _methods["pe_forward"] > 0, _methods["pe_forward"]
_pe_forward_inputs = {i["label"]: i["value"] for i in _fv["valuation_inputs"]["CSLTEST"]["pe_forward"]}
_csl_eps5_value = _pe_forward_inputs["Forecast EPS (yr 5)"]
assert isinstance(_csl_eps5_value, str), _csl_eps5_value
assert "$9.00" in _csl_eps5_value and "$12.24" in _csl_eps5_value, _csl_eps5_value
print(f"[csl_pe_forward_uses_analyst_estimate] PE Forward = ${_methods['pe_forward']:.2f}, "
      f"Forecast EPS(yr 5) row reads {_csl_eps5_value!r} - Yahoo's own forwardEps ($9.00) "
      "compounded 4 more years at g_earn=8% (not trailing-EPS-compounded, and not forwardEps "
      "used bare any more) OK")

# Average over 3 methods (no balance-sheet data -> equity_10y absent).
assert "equity_10y" not in _methods, _methods
_non_price = [k for k in _methods if k != "price"]
assert len(_non_price) == 3, _non_price
print(f"[csl_average_of_3_methods] {len(_non_price)} non-price methods present "
      f"({sorted(_non_price)}) - equity_10y naturally absent (no balance-sheet data in this "
      "fixture), matching the instruction's own 'average over 3 methods' OK")

# Caption: "EPS Basis" line present with the normalised/raw figures.
_pe_trailing_inputs = _fv["valuation_inputs"]["CSLTEST"]["pe_trailing"]
_eps_basis = next((i for i in _pe_trailing_inputs if i["label"] == "EPS Basis"), None)
assert _eps_basis is not None, _pe_trailing_inputs
assert "Normalised" in _eps_basis["value"] and "7.80" in _eps_basis["value"] and "-7.51" in _eps_basis["value"], _eps_basis
print(f"[csl_caption] EPS Basis caption: {_eps_basis['value']!r} OK")

# eps_meta provenance fields.
_eps_meta = _fv["eps_meta"]["CSLTEST"]
assert _eps_meta["eps_base_source"] == "median5_clean", _eps_meta
assert _eps_meta["eps_distorted_years"] == [0], _eps_meta
assert abs(_eps_meta["eps_base_raw"] - (-7.51)) < 1e-9, _eps_meta
print(f"[csl_eps_meta] eps_meta provenance: {_eps_meta} OK")


# ======================================================================
# CHECK 2: genuine-decline fixture - a real, sustained deterioration:
# EPS worsens every year (a genuine multi-year decline into losses,
# not a one-off) while OI/EBITDA falls in lockstep every single year
# (well past the 10% tolerance each time) -> untouched, no year ever
# flagged distorted (the metric never "holds up" the way a one-off
# distortion's cross-check would), and the resulting median (mostly
# negative years) is itself <=0 - PE Trailing correctly shows "not
# meaningful", not a silently wrong low-but-positive number.
# ======================================================================
DECLINE_EPS = [-8.0, -6.0, -4.0, -2.0, 0.5]      # worsening every year, most recent worst
DECLINE_OI = [x * 60 for x in DECLINE_EPS]        # OI/EBITDA declines in lockstep - never "stable"
DECLINE_DA = [5.0, 5.0, 5.0, 5.0, 5.0]
DECLINE_INCOME = _mk_income_df(DECLINE_EPS, DECLINE_OI, DECLINE_DA)

_decline_normalized = ace._normalized_eps({"income": DECLINE_INCOME, "info": {}, "income_q": None})
assert _decline_normalized["distorted_years"] == [], _decline_normalized
assert _decline_normalized["value"] is not None and _decline_normalized["value"] <= 0, _decline_normalized
print(f"[genuine_decline_untouched] EPS worsens every year (-8.0 -> -6.0 -> -4.0 -> -2.0 -> 0.5) "
      f"with OI/EBITDA declining in lockstep every year (never 'stable' the way a one-off "
      f"distortion's cross-check would read) -> nothing flagged distorted, median EPS = "
      f"${_decline_normalized['value']:.2f} (<=0, a genuinely bad number, not artificially "
      "'corrected' upward) OK")

_decline_bundle = _bundle(DECLINE_INCOME, trailing_eps=-2.00, price=50.0, trailing_pe=15.0)
_decline_fv = ace._build_fair_value(_decline_bundle, "DECLINETEST", _dcf_result, _canonical_dcf_result)
_decline_methods = _decline_fv["valuation_methods"]["DECLINETEST"]
_decline_reasons = _decline_fv["valuation_method_reasons"].get("DECLINETEST", {})
assert "pe_trailing" not in _decline_methods, _decline_methods
assert _decline_reasons.get("pe_trailing") == "negative_earnings", _decline_reasons
print("[genuine_decline_pe_trailing_not_meaningful] normalised EPS is None (year 0 not distorted, "
      "so it's the raw -$2.00 figure the None-guard sees) -> PE Trailing excluded, reason "
      "'negative_earnings', NOT silently shown as a large negative 'intrinsic value' OK")


# ======================================================================
# CHECK 3: fewer than EPS_ONEOFF_MIN_CLEAN_YEARS (3) clean years in the
# window -> falls to TTM (if usable) or None + "not meaningful".
# ======================================================================
SPARSE_EPS = [-1.0, -1.5]   # only 2 years of history at all, both negative
SPARSE_OI = [100.0, 150.0]
SPARSE_DA = [10.0, 10.0]
SPARSE_INCOME = _mk_income_df(SPARSE_EPS, SPARSE_OI, SPARSE_DA)
_sparse_normalized = ace._normalized_eps({"income": SPARSE_INCOME, "info": {}, "income_q": None})
assert _sparse_normalized["source"] == "none", _sparse_normalized
assert _sparse_normalized["value"] is None, _sparse_normalized
print("[fewer_than_3_clean_years] only 2 fiscal years of history (both negative) -> 'none' source, "
      "value=None - not enough clean years for median5_clean, TTM also unusable (negative) OK")


# ======================================================================
# CHECK 4: forward EPS present -> PE Forward unchanged/unaffected by
# whether the trailing-EPS mechanism fired at all (already covered by
# CHECK 1's csl_pe_forward_uses_analyst_estimate, this adds a CLEAN
# (non-distorted) ticker to confirm the forward path is identical
# whether or not normalisation fired).
# ======================================================================
CLEAN_EPS = [6.0, 5.8, 5.6, 5.4, 5.2]
CLEAN_OI = [400.0, 390.0, 380.0, 370.0, 360.0]
CLEAN_DA = [40.0] * 5
CLEAN_INCOME = _mk_income_df(CLEAN_EPS, CLEAN_OI, CLEAN_DA)
_clean_normalized = ace._normalized_eps({"income": CLEAN_INCOME, "info": {}, "income_q": None})
assert _clean_normalized["distorted_years"] == [], _clean_normalized
_clean_bundle = _bundle(CLEAN_INCOME, trailing_eps=6.0, forward_eps=6.5, price=120.0)
_clean_fv = ace._build_fair_value(_clean_bundle, "CLEANTEST", _dcf_result, _canonical_dcf_result)
_clean_pe_forward_inputs = {
    i["label"]: i["value"] for i in _clean_fv["valuation_inputs"]["CLEANTEST"]["pe_forward"]
}
# Same g_earn=0.08 as CHECK 1's _dcf_result -> forecast_eps_5y = 6.5 x 1.08^4 = 8.84
# (PE Forward year-5 fix, 1 Oct 2026: forwardEps is compounded 4 more years, not used bare).
_clean_eps5_value = _clean_pe_forward_inputs["Forecast EPS (yr 5)"]
assert isinstance(_clean_eps5_value, str), _clean_eps5_value
assert "$6.50" in _clean_eps5_value and "$8.84" in _clean_eps5_value, _clean_eps5_value
print(f"[forward_eps_unaffected_by_normalisation] a clean (non-distorted) ticker's PE Forward also "
      f"starts from Yahoo's own forwardEps (6.5, then compounded to yr 5) - row reads "
      f"{_clean_eps5_value!r} - identical behaviour whether or not the EPS normalisation "
      "mechanism itself fired OK")


# ======================================================================
# CHECK 5: ADP/CPRT/AOS-shaped fixtures (this session's own established
# tier-placement fixtures - see tests/test_discount_tier_growth_rewrite.
# py) are unaffected: no income_df distortion in any of them (flat,
# clean EPS series), _normalized_eps() reports no distorted years.
# ======================================================================
for _name, _eps in (
    ("ADP", [8.0, 7.8, 7.6, 7.4, 7.2]),
    ("CPRT", [10.0, 3.0, 2.9, 2.8, 2.7]),   # a real +233% jump but NOT flagged (only drops trigger)
    ("AOS", [3.0, 2.9, 2.8, 2.7, 2.6]),
):
    _oi = [x * 60 for x in _eps]
    _da = [x * 5 for x in _eps]
    _income = _mk_income_df(_eps, _oi, _da)
    _normalized = ace._normalized_eps({"income": _income, "info": {}, "income_q": None})
    assert _normalized["distorted_years"] == [], (_name, _normalized)
    print(f"[{_name.lower()}_unaffected] {_name}-shaped clean EPS series -> no distorted years, "
          "unaffected by this fix OK")


# ======================================================================
# CHECK 6: ENGINE_VERSION bumped.
# ======================================================================
assert ace.ENGINE_VERSION >= 52, ace.ENGINE_VERSION
print(f"[engine_version_bumped] auto_compounder_engine.ENGINE_VERSION = {ace.ENGINE_VERSION} "
      "(was 51, >= 52 confirms this bump wasn't reverted by a later task) OK")


# ======================================================================
# CHECK 7: never enters scoring/selection - grep source, not just
# import success (mirrors this session's established pattern for every
# other display-only field).
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
            for _field in ("eps_base_source", "eps_distorted_years", "eps_base_raw", "normalized_eps"):
                assert _field not in _src, f"{_mod.__name__}.{_name} references {_field}!"
print("[no_scoring_leakage] eps_base_source/eps_distorted_years/eps_base_raw/normalized_eps never "
      "referenced inside ranking_engine.calculate_long_score or top100_engine.composite_score OK")


print("\nALL EPS ONE-OFF NORMALISATION FIXTURES PASSED")
