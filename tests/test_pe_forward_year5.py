"""
Growth 1y-blend / PE Forward year-5 fix (owner-directed, 1 Oct 2026,
KNSL/Kinsale Capital live case): Fair Value tab, 1 Oct 2026 - DCF showed
"Base Case Growth: 2.5% for 5 yrs, then fades to 5.0% by yr 10" (base
rate below end rate) while PE Forward showed "Forecast EPS (5y):
$21.65" - Yahoo's info["forwardEps"] (its NEXT-FISCAL-YEAR consensus)
used directly as a year-5 figure with no further compounding.

Bug: forecast_eps_5y = forward_eps (next FY) used AS-IS, discounted 5
years - undervaluing every covered company by ~35-40% on this method
(Push 3, 30 Sep 2026 introduced the forwardEps branch; this task fixes
its compounding).

Fix: forecast_eps_5y = forward_eps * (1 + g_earn) ** 4 - forward_eps is
already "1 year out", so only 4 more years of the DCF's own g_earn are
compounded to reach year 5. The trailing-EPS-compounded fallback
(base_eps * (1 + g_earn) ** 5) is unchanged - it starts from TODAY's
EPS and needs all 5 years.

_pe_forward_method() now returns a 6-tuple: (value, forecast_eps_5y,
actual_pe, year5_price_undiscounted, reason, forward_eps_used) - see
that function's own docstring in auto_compounder_engine.py.

Run: python3 tests/test_pe_forward_year5.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace

# ======================================================================
# KNSL-shaped fixture (the live case): forwardEps=21.65 (Yahoo's
# next-FY consensus), normalised EPS=17.92 (this method's "Actual P/E"
# multiple uses normalised EPS, not forwardEps), price=322.75,
# discount_rate=9.5%.
# ======================================================================
_KNSL_FORWARD_EPS = 21.65
_KNSL_NORMALIZED_EPS = 17.92
_KNSL_PRICE = 322.75
_KNSL_DISCOUNT_RATE = 0.095

_knsl_bundle = {
    "info": {
        "forwardEps": _KNSL_FORWARD_EPS, "currentPrice": _KNSL_PRICE, "currency": "USD",
    },
}
_knsl_normalized_eps = {"value": _KNSL_NORMALIZED_EPS, "raw": _KNSL_NORMALIZED_EPS,
                        "distorted_years": [], "source": "ttm", "growth_series": []}

# ----------------------------------------------------------------------
# g_earn = 11.25% (the growth 1y-blend fix's KNSL-shaped base rate) ->
# forecast_eps_5y ~= 33.15, actual P/E ~= 18.01, year-5 price ~= 597,
# discounted value ~= 379 (instruction's own worked example).
# ----------------------------------------------------------------------
_G_EARN_BLEND = 0.1125
_result_blend = ace._pe_forward_method(
    _knsl_bundle, _G_EARN_BLEND, _KNSL_DISCOUNT_RATE, _knsl_normalized_eps)
assert _result_blend is not None
(_value_b, _eps5_b, _fair_pe_b, _pe_b, _year5_b, _reason_b, _fwd_used_b,
 _source_b) = _result_blend
assert _reason_b is None, _reason_b
assert _fwd_used_b == _KNSL_FORWARD_EPS, _fwd_used_b
# This fixture's growth_series is empty (no fiscal-year history), so
# fair_pe_5y falls back to today's own P/E (clamped to [5, 40]) -
# numerically identical to the pre-fix actual_pe-driven value below.
assert _source_b == "current (insufficient history)", _source_b
assert abs(_fair_pe_b - _pe_b) < 1e-9, (_fair_pe_b, _pe_b)

_expected_eps5_b = _KNSL_FORWARD_EPS * (1 + _G_EARN_BLEND) ** 4
assert abs(_eps5_b - _expected_eps5_b) < 0.01, (_eps5_b, _expected_eps5_b)
assert abs(_eps5_b - 33.16) < 0.05, _eps5_b
assert abs(_pe_b - (_KNSL_PRICE / _KNSL_NORMALIZED_EPS)) < 1e-9, _pe_b
assert abs(_pe_b - 18.01) < 0.01, _pe_b
assert abs(_value_b - 379.4) < 1.0, _value_b
print(f"[knsl_blend_growth_11_25pct] forward_eps={_KNSL_FORWARD_EPS} compounded 4 more "
      f"years at g_earn={_G_EARN_BLEND:.2%} -> Forecast EPS(yr5)={_eps5_b:.2f}, "
      f"Actual P/E={_pe_b:.2f}, discounted value=${_value_b:.2f} (expected ~=$379) OK")

# ----------------------------------------------------------------------
# g_earn = 2.5% (the bare next-year consensus, pre-blend-fix growth
# rate from the same live KNSL symptom) -> discounted value ~= 272.
# ----------------------------------------------------------------------
_G_EARN_BARE = 0.025
_result_bare = ace._pe_forward_method(
    _knsl_bundle, _G_EARN_BARE, _KNSL_DISCOUNT_RATE, _knsl_normalized_eps)
assert _result_bare is not None
(_value_bare, _eps5_bare, _fair_pe_bare, _pe_bare, _year5_bare, _reason_bare, _fwd_used_bare,
 _source_bare) = _result_bare
assert _reason_bare is None, _reason_bare
assert abs(_value_bare - 272) < 3.0, _value_bare
print(f"[knsl_bare_growth_2_5pct] same fixture at g_earn={_G_EARN_BARE:.2%} (the DCF's "
      f"pre-blend-fix growth rate) -> discounted value=${_value_bare:.2f} (expected "
      f"~=$272) OK")

assert _value_b > _value_bare, (_value_b, _value_bare)
print("[higher_growth_higher_value] the blended 11.25% growth rate produces a strictly "
      "higher PE Forward value than the bare 2.5% rate OK")

# ======================================================================
# CSL-style: negative TRAILING EPS, but a positive forward EPS AND a
# positive normalised EPS (the one-off-resistant figure this method's
# "Actual P/E" multiple uses) - this method must still produce a
# value, not silently withhold it the way a trailing-EPS-only guard
# would (the CSL live symptom Push 3, 30 Sep 2026 already fixed; this
# task must not regress it).
# ======================================================================
_csl_bundle = {
    "info": {
        "forwardEps": 15.0, "currentPrice": 280.0, "currency": "AUD",
        # trailingEps deliberately negative/absent - this method never
        # reads it directly, only normalized_eps["value"] and
        # info["forwardEps"], so a negative trailingEps alone can't
        # block this method any more.
        "trailingEps": -3.5,
    },
}
_csl_normalized_eps = {"value": 8.0, "raw": -3.5, "distorted_years": [0],
                        "source": "median5_clean", "growth_series": []}
_result_csl = ace._pe_forward_method(_csl_bundle, 0.08, 0.085, _csl_normalized_eps)
assert _result_csl is not None
(_value_csl, _eps5_csl, _fair_pe_csl, _pe_csl, _year5_csl, _reason_csl, _fwd_used_csl,
 _source_csl) = _result_csl
assert _reason_csl is None, _reason_csl
assert _value_csl is not None and _value_csl > 0, _value_csl
assert _fwd_used_csl == 15.0, _fwd_used_csl
assert abs(_pe_csl - (280.0 / 8.0)) < 1e-9, _pe_csl
print(f"[csl_style_negative_trailing_eps_still_values] negative trailingEps (-3.5) with "
      f"a positive forwardEps (15.0) and positive normalised EPS (8.0) -> PE Forward "
      f"still produces a value (${_value_csl:.2f}), not withheld OK")

# ======================================================================
# Trailing-fallback branch (no forwardEps at all) is UNCHANGED - same
# fixture/assertions as tests/test_adp_fair_value_pe_forward_and_
# perpetual_rate.py's own ADP-like case, just re-confirmed here under
# the new 6-tuple return shape.
# ======================================================================
_TRAILING_EPS = 1.0
_G_EARN_ADP = 17.84 ** (1 / 5) - 1
_PRICE_NOW = 24.10
_DISCOUNT_RATE_ADP = 0.093
_adp_bundle = {
    "info": {"trailingEps": _TRAILING_EPS, "currentPrice": _PRICE_NOW, "currency": "USD"},
    "income_q": None, "income": None, "balance": None, "cashflow": None,
    "prices_10y": {"dates": [], "prices": []},
}
_adp_normalized_eps = ace._normalized_eps(_adp_bundle)
assert _adp_normalized_eps["value"] == 1.0, _adp_normalized_eps

_result_adp = ace._pe_forward_method(
    _adp_bundle, _G_EARN_ADP, _DISCOUNT_RATE_ADP, _adp_normalized_eps)
assert _result_adp is not None
(_value_adp, _eps5_adp, _fair_pe_adp, _pe_adp, _year5_adp, _reason_adp, _fwd_used_adp,
 _source_adp) = _result_adp
assert _reason_adp is None, _reason_adp
assert _fwd_used_adp is None, _fwd_used_adp  # no forwardEps -> trailing branch, no "next FY" figure
assert abs(_eps5_adp - 17.84) < 0.01, _eps5_adp
assert abs(_pe_adp - 24.10) < 0.01, _pe_adp
assert abs(_year5_adp - 429.844) < 0.5, _year5_adp
assert 274 < _value_adp < 278, _value_adp
print(f"[trailing_fallback_unchanged] no forwardEps -> trailing-EPS-compounded-5y "
      f"fallback produces the identical ADP-case figures as before this fix "
      f"(Forecast EPS(yr5)={_eps5_adp:.2f}, value=${_value_adp:.2f}) OK")

print("\nALL PE FORWARD YEAR-5 FIXTURES PASSED")
