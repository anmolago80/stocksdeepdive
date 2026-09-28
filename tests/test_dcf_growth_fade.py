"""
Owner-approved fix (28 Sep 2026), "Fix 2: fade stage-1 growth".

fcf_valuation_engine.dcf_intrinsic_value()'s stage-1 loop used to compound
the SAME flat growth rate for all growth_years (10) years, then drop
straight to the (much lower) perpetual rate at the terminal-value
boundary - a discontinuity the owner asked to smooth into a linear fade:

    g_t = g1 - (g1 - perpetual_rate) * (t-1) / (growth_years-1)

g_1 == g1 (the stage-1 rate exactly as chosen today, after the existing
cap/floor - growth SELECTION is unchanged, only how it's applied across
the 10 years). g_10 == perpetual_rate exactly, so the terminal value
(still computed on the year-10 cash flow) picks up with no discontinuity.
meta["growth_path"] carries all growth_years yearly rates used.

DCF IMPLEMENTATIONS IN THIS CODEBASE (file:line), confirmed by source
inspection below, not just assumed:

  1. fcf_valuation_engine.dcf_intrinsic_value() (fcf_valuation_engine.py,
     stage-1 loop ~line 778) - THE ONE real, live valuation DCF. Every
     one of these funnels through it:
       - Deep Dive's own headline Intrinsic Value: deep_dive_engine.py:227
         -> resolver_engine.resolve_intrinsic_value() -> resolver_engine.py:77
         (base case) and :201 (bear/bull scenario cases) -> dcf_intrinsic_value().
       - The nightly scan: nightly_scan.py:61 imports resolve_intrinsic_value,
         calls it at nightly_scan.py:438 - same resolver_engine path as Deep Dive.
       - The Fair Value tab: auto_compounder_engine._run_dcf() (~line 1650)
         calls fcf_valuation_engine.dcf_intrinsic_value() directly - "Runs
         the SAME dcf_intrinsic_value() the main site's Deep Dive page uses"
         per that function's own docstring.
       - admin_data_audit.py (A1/A6 diagnostic dry-runs, lines ~331/488/494)
         and app.py's own A6 bulk dry-run panel (~29911/29917) call it
         directly too, for side-by-side comparisons - same one function.
       - portfolio_health_engine.fetch_snapshot() (line 162) forwards its
         discount_rate/perpetual_rate/growth_rate straight through to it.

  2. reverse_dcf_engine._dcf_value_for_growth() (reverse_dcf_engine.py:56-69)
     - a SEPARATE, "bare, unclamped mirror of dcf_intrinsic_value()'s own
     stage-1/stage-2 discounting arithmetic" (that module's own docstring,
     reverse_dcf_engine.py:57), used ONLY internally by _solve_growth()'s
     binary search to answer "what flat growth rate would the current
     price imply" - never used to compute a displayed Intrinsic Value
     itself (the module's own forward DCF check, reverse_dcf_engine.py:171,
     calls the real dcf_intrinsic_value()). NOT changed here, per
     instruction - still flat growth, verified below.

  3. auto_compounder_engine._mini_dcf_iv() (auto_compounder_engine.py:
     2042-2057) - the Fair Value/Compounder page's own "Value vs Book"
     IV/BV ratio SERIES chart: a workbook-quirk mini-DCF (9 explicit years,
     terminal value applied to the ALREADY-discounted year-9 flow -
     documented as "a real quirk of the workbook's own formula,
     transcribed faithfully rather than 'corrected'"). Used only for that
     chart, never the headline Fair Value number. NOT changed here, per
     instruction - still flat growth, verified below.

Run: python3 tests/test_dcf_growth_fade.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace
import compounder_ui as cui
import fcf_valuation_engine as fve
import reverse_dcf_engine
import nightly_scan
import deep_dive_engine

# ======================================================================
# Fixture: FCF/share 3.83, g1 16%, discount 9.5%, perpetual 2.0%
# ======================================================================
_FCF_PER_SHARE = 3.83
_G1 = 0.16
_DISCOUNT = 0.095
_PERPETUAL = 0.02
_INFO = {"currentPrice": 100.0, "currency": "USD"}

_iv_faded, _g_used, _meta = fve.dcf_intrinsic_value(
    "TEST", info=_INFO, cashflow_df=None, currency="USD",
    discount_rate=_DISCOUNT, perpetual_rate=_PERPETUAL, growth_rate=_G1,
    manual_fcf=_FCF_PER_SHARE, diluted_shares_override=1,
)

assert 90 < _iv_faded < 92.5, _iv_faded
print(f"[faded_matches_owner_number] faded DCF value ${_iv_faded:.2f} (owner's own expectation: ~$91.2) OK")

# growth SELECTION unchanged - the returned growth_rate_used is still the
# raw stage-1 rate chosen, not some averaged/faded figure.
assert abs(_g_used - _G1) < 1e-9, _g_used
print(f"[growth_selection_unchanged] growth_rate_used returned is still {_g_used} (== g1, "
      "not an average of the fade) OK")

# growth_path: 10 entries, first == g1, last == perpetual_rate exactly.
_path = _meta.get("growth_path")
assert _path is not None and len(_path) == 10, _path
assert abs(_path[0] - _G1) < 1e-4, _path[0]
assert abs(_path[-1] - _PERPETUAL) < 1e-4, _path[-1]
# strictly decreasing (a genuine fade, not flat/no-op)
assert all(_path[i] > _path[i + 1] for i in range(len(_path) - 1)), _path
print(f"[growth_path_recorded] meta['growth_path'] has {len(_path)} yearly rates, "
      f"{_path[0]:.4f} -> {_path[-1]:.4f}, strictly decreasing OK")

# ---- Flat-growth comparison (old behaviour, computed independently here, ----
# ---- NOT by calling the engine - proves the fade is a real, large change) ----
_cf = _FCF_PER_SHARE
_flat = 0.0
for _t in range(1, 11):
    _cf *= (1 + _G1)
    _flat += _cf / ((1 + _DISCOUNT) ** _t)
_term_cf = _cf * (1 + _PERPETUAL)
_flat += (_term_cf / (_DISCOUNT - _PERPETUAL)) / ((1 + _DISCOUNT) ** 10)
assert 145 < _flat < 147, _flat
print(f"[flat_baseline_matches_owner_number] independently-computed flat-growth value "
      f"${_flat:.2f} (owner's own expectation: ~$146) - faded (${_iv_faded:.2f}) is "
      f"meaningfully lower, as expected OK")

# ---- growth_years == 1 edge case: no ZeroDivisionError, g_t == g1 ----
_iv_1y, _, _meta_1y = fve.dcf_intrinsic_value(
    "TEST", info=_INFO, cashflow_df=None, currency="USD",
    discount_rate=_DISCOUNT, perpetual_rate=_PERPETUAL, growth_rate=_G1,
    manual_fcf=_FCF_PER_SHARE, diluted_shares_override=1, growth_years=1,
)
assert _meta_1y.get("growth_path") == [round(_G1, 4)], _meta_1y.get("growth_path")
print("[growth_years_one_no_crash] growth_years=1 doesn't divide by zero - "
      f"growth_path is just [{_G1}] OK")

# ======================================================================
# Display: "growth fades X% -> Y%" wherever the DCF growth is shown
# ======================================================================

# Fair Value tab (auto_compounder_engine._build_fair_value / compounder_ui).
_bundle = {
    "info": {"trailingEps": 1.0, "currentPrice": 24.10, "currency": "USD"},
    "income_q": None, "income": None, "balance": None, "cashflow": None,
    "prices_10y": {"dates": [], "prices": []},
}
_dcf_result = {"value": 100.0, "growth": _G1, "perpetual_rate": _PERPETUAL, "discount_rate": _DISCOUNT}
_fv = ace._build_fair_value(_bundle, "TEST", _dcf_result)
_dcf_inputs = {i["label"]: i for i in _fv["valuation_inputs"]["TEST"]["dcf"]}
_growth_row = _dcf_inputs["Base Case Growth"]
assert _growth_row["format"] == "raw", _growth_row
assert _growth_row["value"] == "16.0% -> 2.0% (fades)", _growth_row["value"]
print(f"[fair_value_tab_shows_fade] Fair Value tab's Base Case Growth row reads "
      f"{_growth_row['value']!r} OK")
assert cui._cp_format(_growth_row["value"], _growth_row["format"]) == "16.0% -> 2.0% (fades)"
print("[cp_format_raw_passthrough] compounder_ui._cp_format() renders the 'raw' fmt "
      "as the pre-formatted string, unmangled OK")

# Deep Dive page: the fade caption is present in app.py, gated on the same
# dcf_growth/dcf_perpetual fields deep_dive_engine.py already threads
# through (source-level check - see the app.py-only admin-panel test
# precedent set earlier this session for why: rendering page_deep_dive()
# end-to-end needs the same heavy mocking for no extra confidence here).
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"),
          encoding="utf-8") as _f:
    _app_src = _f.read()
assert "DCF growth fades" in _app_src
assert "_dd['dcf_growth']:.1f}% -> {_dd['dcf_perpetual']:.1f}%" in _app_src
print("[deep_dive_shows_fade] app.py's Deep Dive rendering contains the "
      "'DCF growth fades X% -> Y%' caption, keyed off dd['dcf_growth']/dd['dcf_perpetual'] OK")

# ======================================================================
# The two OTHER DCF-shaped implementations are confirmed present, and
# confirmed UNCHANGED (still flat growth, not faded).
# ======================================================================
_rdcf_src = inspect.getsource(reverse_dcf_engine._dcf_value_for_growth)
assert "growth_years - 1" not in _rdcf_src and "(t - 1)" not in _rdcf_src
assert "cash_flow * (1 + growth_rate)" in _rdcf_src
print("[reverse_dcf_untouched] reverse_dcf_engine._dcf_value_for_growth() still compounds "
      "a single flat growth_rate - confirmed NOT faded (own copy, deliberately left alone) OK")

_mini_dcf_src = inspect.getsource(ace._mini_dcf_iv)
assert "growth_years" not in _mini_dcf_src  # doesn't even take a growth_years param
assert "(1 + g_earn) ** k" in _mini_dcf_src
print("[mini_dcf_iv_untouched] auto_compounder_engine._mini_dcf_iv() still compounds a "
      "single flat g_earn for its own 9-year mini-DCF - confirmed NOT faded (own copy, "
      "deliberately left alone) OK")

# ======================================================================
# Confirm the real call chain: Deep Dive / nightly scan / Fair Value tab
# all funnel through fcf_valuation_engine.dcf_intrinsic_value() - not
# assumed, checked via source.
# ======================================================================
_nightly_src = inspect.getsource(nightly_scan)
assert "resolve_intrinsic_value" in _nightly_src
print("[nightly_scan_uses_resolver] nightly_scan.py calls resolver_engine.resolve_intrinsic_value(), "
      "the same path Deep Dive uses OK")

_dd_src = inspect.getsource(deep_dive_engine)
assert "resolve_intrinsic_value" in _dd_src
print("[deep_dive_uses_resolver] deep_dive_engine.py calls resolver_engine.resolve_intrinsic_value() OK")

_run_dcf_src = inspect.getsource(ace._run_dcf)
assert "fcf_valuation_engine.dcf_intrinsic_value" in _run_dcf_src
print("[fair_value_tab_uses_dcf_intrinsic_value] auto_compounder_engine._run_dcf() (the Fair "
      "Value tab's DCF) calls fcf_valuation_engine.dcf_intrinsic_value() directly OK")

print("\nALL DCF GROWTH-FADE FIXTURES PASSED")
