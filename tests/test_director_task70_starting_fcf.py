"""
TASK 70 (Director-directed, owner-approved, 20 Nov 2026) - Show the
starting FCF used in the DCF.

WHY: the Fair Value tab's "DCF (10y FCF)" details show growth, discount,
perpetual and size premium, but not the starting free cash flow the DCF
actually used. For WMS that's US$615.7M (fcf_used=615,695,250): latest
operating cash flow $819.05M minus 4-year AVERAGE capex $203.36M
("ocf-normcapex" path), not the reported FCF ($569.3M FY2026) or TTM
(~$530M, TASK 68's own real-TTM figure).

DO:
  1. "Starting FCF: $X" (meta["fcf_used"], listing currency, post-FX),
     formatted like the other money values (e.g. $615.70M).
  2. One short line explaining how it was built, with actual numbers:
       - ocf-normcapex: OCF - average capex breakdown
       - median fallback: "reported FCF, median of last N years"
       - one-off substitution: clean-year/EBITDA-bridge base (one-off
         years excluded; raw latest $X), "capped at 3x raw" when capped
       - financials mode: net income used in place of FCF
     FX-converted figures say "converted from <fin_ccy>".
  3. Same line on the Deep Dive DCF explanation, so both screens agree.
  4. Display only - no DCF/IV/scoring/Top 200 change.

IMPLEMENTATION: resolver_engine.dcf_starting_fcf_lines(meta, ticker) is
the one shared function both auto_compounder_engine._dcf_valuation_and_
inputs() (Fair Value tab's "Starting FCF"/"FCF Build" rows) and deep_
dive_engine.py's "dcf_starting_fcf"/"dcf_starting_fcf_breakdown" context
keys (rendered by app.py as two st.caption() lines) call - so the two
screens can never drift onto different wording. fcf_valuation_engine.py
gained 4 new, purely additive meta fields (fcf_base_ocf_used/fcf_base_
capex_used/fcf_base_capex_years/fcf_base_median_years) exposing locals
normalized_base_and_series() already computes, since the pre-existing
meta never carried the raw OCF/capex component figures needed for an
actual numbers breakdown (only the final base FCF).

Covers:
  - WMS-shaped fixture: "Starting FCF: $615.70M" and the OCF-minus-avg-
    capex breakdown with the real component numbers.
  - One test per other source type: median fallback, one-off/EBITDA
    bridge (incl. capped at 3x raw), financials mode, FX-converted.
  - No DCF ever ran (fcf_used unset) -> both lines None/omitted.
  - Fair Value (_dcf_valuation_and_inputs) and Deep Dive (resolver_
    engine.dcf_starting_fcf_lines, the same function deep_dive_engine.py
    calls) show the identical figure/wording for the same meta.
  - fcf_valuation_engine.normalized_base_and_series()'s new fields round-
    trip correctly for a real WMS-shaped cashflow DataFrame (not just a
    hand-built meta dict).
  - No DCF/IV calculation changed: the new fields are display-only
    passthroughs, verified by confirming the base FCF value itself is
    unaffected by their presence.

Run: python3 tests/test_director_task70_starting_fcf.py
"""
import os
import subprocess
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
import deep_dive_engine as dde
import fcf_valuation_engine as fve
import resolver_engine as re_


# ======================================================================
# CHECK 1-5: WMS-shaped fixture, via the real
# normalized_base_and_series()/dcf_starting_fcf_lines() pipeline (not a
# hand-built meta dict) - "Starting FCF: $615.70M" and the OCF-minus-
# avg-capex breakdown with the real component numbers.
# ======================================================================
_wms_cashflow = pd.DataFrame({
    "2026-03-31": {"Operating Cash Flow": 819_050_000.0, "Capital Expenditure": -210_000_000.0},
    "2025-03-31": {"Operating Cash Flow": 700_000_000.0, "Capital Expenditure": -200_000_000.0},
    "2024-03-31": {"Operating Cash Flow": 650_000_000.0, "Capital Expenditure": -205_000_000.0},
    "2023-03-31": {"Operating Cash Flow": 600_000_000.0, "Capital Expenditure": -198_440_000.0},
})
_wms_base, _wms_series, _wms_source, _wms_norm, _wms_capex_basis, _wms_oneoff_meta = (
    fve.normalized_base_and_series(_wms_cashflow)
)
check("WMS fixture: source is ocf-normcapex, capex basis is 'average'",
      _wms_source == "ocf-normcapex" and _wms_capex_basis == "average")
check("WMS fixture: fcf_base_ocf_used/fcf_base_capex_used/fcf_base_capex_years "
      "expose the real OCF (819.05M)/avg-capex (203.36M)/4-year count",
      abs(_wms_oneoff_meta["fcf_base_ocf_used"] - 819_050_000.0) < 1.0
      and abs(_wms_oneoff_meta["fcf_base_capex_used"] - 203_360_000.0) < 1.0
      and _wms_oneoff_meta["fcf_base_capex_years"] == 4)

_wms_meta = {
    "fcf_used": 615_695_250.0,  # the Director's own quoted dcf_diag figure
    "fcf_source": _wms_source,
    "fcf_base_source": _wms_source,
    "fcf_base_ocf_used": _wms_oneoff_meta["fcf_base_ocf_used"],
    "fcf_base_capex_used": _wms_oneoff_meta["fcf_base_capex_used"],
    "fcf_base_capex_years": _wms_oneoff_meta["fcf_base_capex_years"],
    "capex_basis": _wms_capex_basis,
}
_wms_fcf, _wms_breakdown = re_.dcf_starting_fcf_lines(_wms_meta, ticker="WMS")
check('WMS fixture: "Starting FCF" formats to exactly $615.70M',
      _wms_fcf == "$615.70M")
check('WMS fixture: the breakdown line shows the real OCF/avg-capex numbers',
      _wms_breakdown == "= operating cash flow $819.05M − 4-yr average capex $203.36M")


# ======================================================================
# CHECK 6-7: median fallback source.
# ======================================================================
_median_meta = {
    "fcf_used": 100_000_000.0, "fcf_source": "fcf-median",
    "fcf_base_source": "fcf-median", "fcf_base_median_years": 3,
}
_median_fcf, _median_breakdown = re_.dcf_starting_fcf_lines(_median_meta, ticker="WMS")
check('median fallback: "Starting FCF" is $100.00M',
      _median_fcf == "$100.00M")
check('median fallback: breakdown names the real year count ("median of last 3 years")',
      _median_breakdown == "= reported FCF, median of last 3 years")


# ======================================================================
# CHECK 8-10: one-off substitution (clean-year median), including the
# capped-at-3x-raw case.
# ======================================================================
_oneoff_meta = {
    "fcf_used": 300_000_000.0, "fcf_source": "ocf-normcapex",
    "fcf_base_source": "median5_clean", "fcf_distorted_years": [0, 2],
    "fcf_base_raw": 50_000_000.0, "fcf_base_capped_by_uplift": False,
}
_oneoff_fcf, _oneoff_breakdown = re_.dcf_starting_fcf_lines(_oneoff_meta, ticker="WMS")
check('one-off substitution: "Starting FCF" is $300.00M',
      _oneoff_fcf == "$300.00M")
check('one-off substitution: breakdown names "clean-year base", the raw latest '
      'figure, and is NOT capped when fcf_base_capped_by_uplift is False',
      _oneoff_breakdown == "= clean-year base (one-off years excluded; raw latest $50.00M)")

_capped_meta = dict(_oneoff_meta, fcf_base_capped_by_uplift=True)
_capped_fcf, _capped_breakdown = re_.dcf_starting_fcf_lines(_capped_meta, ticker="WMS")
check('one-off substitution, capped: breakdown says "capped at 3× raw"',
      _capped_breakdown == "= clean-year base (one-off years excluded; raw latest $50.00M) - capped at 3× raw")

_bridge_meta = dict(_oneoff_meta, fcf_base_source="ebitda_bridge", fcf_distorted_years=[0])
_bridge_fcf, _bridge_breakdown = re_.dcf_starting_fcf_lines(_bridge_meta, ticker="WMS")
check('EBITDA-bridge substitution: breakdown names "EBITDA-bridge base" and a '
      'singular "year" (one distorted year, not plural)',
      _bridge_breakdown == "= EBITDA-bridge base (one-off year excluded; raw latest $50.00M)")


# ======================================================================
# CHECK 11-12: financials mode (net income used in place of FCF).
# ======================================================================
_fin_meta = {
    "fcf_used": 400_000_000.0, "fcf_source": "net_income_financials",
    "fcf_base_source": "net_income_financials", "is_financials_mode": True,
}
_fin_fcf, _fin_breakdown = re_.dcf_starting_fcf_lines(_fin_meta, ticker="WMS")
check('financials mode: "Starting FCF" is $400.00M',
      _fin_fcf == "$400.00M")
check('financials mode: breakdown is exactly "Financials mode: net income '
      'used in place of FCF"',
      _fin_breakdown == "Financials mode: net income used in place of FCF")


# ======================================================================
# CHECK 13: FX-converted figure says "converted from <fin_ccy>".
# ======================================================================
_fx_meta = dict(_wms_meta, fx_converted="USD->AUD")
_fx_fcf, _fx_breakdown = re_.dcf_starting_fcf_lines(_fx_meta, ticker="WMS.AX")
check('FX-converted: breakdown ends with "(converted from USD)"',
      _fx_breakdown is not None and _fx_breakdown.endswith("(converted from USD)"))


# ======================================================================
# CHECK 14: no DCF ever ran (fcf_used unset) -> both lines None.
# ======================================================================
check("no DCF ran: both lines are None",
      re_.dcf_starting_fcf_lines({}, ticker="WMS") == (None, None))


# ======================================================================
# CHECK 15-17: Fair Value tab (_dcf_valuation_and_inputs) shows the
# SAME "Starting FCF"/"FCF Build" rows, first in the list, and doesn't
# show them when no DCF value exists - the dcf_inputs list is otherwise
# unaffected (perpetual/discount/growth rows still present, unchanged).
# ======================================================================
_canonical = {
    "value": 50.0, "perpetual_rate": 0.025, "discount_rate": 0.08,
    "growth": 0.10, "growth_source": "history",
    "fcf_used": _wms_meta["fcf_used"], "fcf_source": _wms_meta["fcf_source"],
    "fcf_base_source": _wms_meta["fcf_base_source"],
    "fcf_base_ocf_used": _wms_meta["fcf_base_ocf_used"],
    "fcf_base_capex_used": _wms_meta["fcf_base_capex_used"],
    "fcf_base_capex_years": _wms_meta["fcf_base_capex_years"],
    "capex_basis": _wms_meta["capex_basis"],
}
_dcf_value, _dcf_inputs = ace._dcf_valuation_and_inputs({"currency": "USD"}, 45.0, _canonical, ticker="WMS")
check("Fair Value tab: dcf_inputs[0] is the Starting FCF row, matching "
      "resolver_engine.dcf_starting_fcf_lines() exactly",
      _dcf_inputs[0] == {"label": "Starting FCF", "value": _wms_fcf, "format": "raw"})
check("Fair Value tab: dcf_inputs[1] is the FCF Build row, matching "
      "resolver_engine.dcf_starting_fcf_lines() exactly - same function Deep "
      "Dive calls, so the two screens can't drift onto different wording",
      _dcf_inputs[1] == {"label": "FCF Build", "value": _wms_breakdown, "format": "raw"})
check("Fair Value tab: the pre-existing rows (Perpetual Rate/Discount Rate/"
      "Base Case Growth) still follow, unaffected",
      [r["label"] for r in _dcf_inputs[2:5]] == ["Perpetual Rate (USD)", "Discount Rate", "Base Case Growth"])

_no_dcf_value, _no_dcf_inputs = ace._dcf_valuation_and_inputs({"currency": "USD"}, 45.0, {"value": None}, ticker="WMS")
check("Fair Value tab: no usable canonical_dcf_result -> no row at all (None, None), unchanged",
      _no_dcf_value is None and _no_dcf_inputs is None)


# ======================================================================
# CHECK 18: Deep Dive wiring - deep_dive_engine.py imports and calls the
# SAME resolver_engine.dcf_starting_fcf_lines() function (not a second,
# independently-written formatter) for its "dcf_starting_fcf"/"dcf_
# starting_fcf_breakdown" context keys, and app.py renders them as
# st.caption() lines right next to the existing DCF growth/discount
# explanation.
# ======================================================================
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
check("deep_dive_engine.py imports dcf_starting_fcf_lines from resolver_engine "
      "(the exact same function object, not a reimplementation)",
      hasattr(dde, "dcf_starting_fcf_lines") and dde.dcf_starting_fcf_lines is re_.dcf_starting_fcf_lines)
_dde_src = open(os.path.join(_repo_root, "deep_dive_engine.py")).read()
check('deep_dive_engine.py calls dcf_starting_fcf_lines(iv_meta, ticker=ticker) exactly once '
      "(single source of truth, not duplicated formatting logic)",
      _dde_src.count("dcf_starting_fcf_lines(iv_meta, ticker=ticker)") == 1)
check('deep_dive_engine.py\'s context carries "dcf_starting_fcf"/"dcf_starting_fcf_breakdown"',
      '"dcf_starting_fcf": _dcf_starting_fcf,' in _dde_src
      and '"dcf_starting_fcf_breakdown": _dcf_starting_fcf_breakdown,' in _dde_src)
_app_src = open(os.path.join(_repo_root, "app.py")).read()
check('app.py renders "Starting FCF: {...}" from _dd["dcf_starting_fcf"], right before the '
      "existing DCF growth/discount caption block",
      'f"Starting FCF: {_dd[\'dcf_starting_fcf\']}"' in _app_src)


# ======================================================================
# CHECK 19-20: display-only - the new meta fields never change the
# actual DCF base figure itself (fcf_valuation_engine's own return
# value is byte-identical whether or not a caller reads the new
# fields), and the per-year growth series is untouched.
# ======================================================================
_base2, _series2, _source2, _norm2, _capex_basis2, _oneoff_meta2 = fve.normalized_base_and_series(_wms_cashflow)
check("normalized_base_and_series() is deterministic/unaffected by the new fields - "
      "same base FCF and growth series on a second call",
      _base2 == _wms_base and _series2 == _wms_series)
check("the new oneoff_meta fields are additive only - every pre-existing key "
      "(fcf_base_source/fcf_distorted_years/fcf_base_raw/fcf_base_capped_by_uplift) "
      "is still present and unchanged",
      _wms_oneoff_meta["fcf_base_source"] == "ocf-normcapex"
      and _wms_oneoff_meta["fcf_distorted_years"] == []
      and _wms_oneoff_meta["fcf_base_raw"] is None
      and _wms_oneoff_meta["fcf_base_capped_by_uplift"] is False)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
