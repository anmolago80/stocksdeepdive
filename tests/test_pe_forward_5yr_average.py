"""
PE Forward 5-yr average P/E (owner decision, 3 Oct 2026,
instruction_pe_forward_5y_compounder_caps.md, Commit 1): the multiple
_pe_forward_method() applies to its year-5 price is now fair_pe_5y - the
MEDIAN of up to the last 5 fiscal-year P/Es (auto_compounder_engine.
_fair_pe_5y(), pairing _normalized_eps()'s "growth_series" against
_year_end_prices()) - not today's single-day multiple, so a de-rating
or a spike doesn't carry straight into a 5-year forecast. Needs >= 3
usable fiscal years, else falls back to today's own P/E (flagged
pe_forward_multiple_source="current (insufficient history)"). Either
way the multiple actually applied is clamped to [PE_FORWARD_5Y_FLOOR,
PE_FORWARD_5Y_CEIL] = [5, 40].

Run: python3 tests/test_pe_forward_5yr_average.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace


def _mk_income_df(eps_newest_first, n=None):
    """Income statement DataFrame, most-recent-first columns, fiscal
    year-end 30 June - same convention as tests/test_eps_oneoff_
    normalisation.py's own _mk_income_df(). EPS is held CONSTANT across
    years by callers that only want to vary price (so no year is ever
    flagged a one-off distortion), except the 2-usable-years fixture,
    which passes its own 2-entry list."""
    n = n if n is not None else len(eps_newest_first)
    cols = {}
    for i in range(n):
        cols[f"{2026 - i}-06-30"] = [eps_newest_first[i], 100.0, 10.0]
    return pd.DataFrame(cols, index=["Diluted EPS", "Operating Income", "Reconciled Depreciation"])


def _mk_bundle(eps_newest_first, year_end_prices_newest_first, price_now, forward_eps=None,
               currency="USD"):
    """year_end_prices_newest_first[i] is the fiscal-year-end price paired
    with eps_newest_first[i]'s column (both newest-first, matching
    _mk_income_df()'s column order) - built into a monthly prices_10y
    series (one point per fiscal year-end, dated on that same day so
    _year_end_prices()'s "on or before" match is exact)."""
    n = len(eps_newest_first)
    income = _mk_income_df(eps_newest_first, n=n)
    dates, prices = [], []
    for i in range(n - 1, -1, -1):  # oldest to newest, as a real monthly series would be
        dates.append(f"{2026 - i}-06-30")
        prices.append(year_end_prices_newest_first[i])
    info = {"currentPrice": price_now, "currency": currency, "sharesOutstanding": 100_000_000}
    if forward_eps is not None:
        info["forwardEps"] = forward_eps
    return {"info": info, "income": income, "prices_10y": {"dates": dates, "prices": prices}}


# ======================================================================
# KNSL-shaped fixture (the instruction's own worked example): forward
# EPS 21.73, g=8.8%, discount=9.4%, 5 fiscal-year P/Es ~= 32/30/28/26/19
# (EPS held at a constant 10/share so each year's P/E is just that
# year's price / 10) -> median 28 -> value ~= 30.46 x 28 / 1.094^5 ~=
# $544 (was $363 under today's single-day multiple).
# ======================================================================
_KNSL_EPS = [10.0] * 5
_KNSL_PRICES = [320.0, 300.0, 280.0, 260.0, 190.0]  # -> P/Es 32/30/28/26/19, median 28
_knsl_bundle = _mk_bundle(_KNSL_EPS, _KNSL_PRICES, price_now=322.75, forward_eps=21.73)
_knsl_g_earn = 0.088
_knsl_discount = 0.094

_knsl_normalized_eps = ace._normalized_eps(_knsl_bundle)
assert _knsl_normalized_eps["source"] == "median5_clean", _knsl_normalized_eps
assert _knsl_normalized_eps["distorted_years"] == [], _knsl_normalized_eps

_fair_pe, _reason = ace._fair_pe_5y(_knsl_bundle, _knsl_normalized_eps)
assert _reason is None, _reason
assert abs(_fair_pe - 28.0) < 0.01, _fair_pe
print(f"[knsl_fair_pe_5y_median] 5 fiscal-year P/Es 32/30/28/26/19 -> median {_fair_pe:.1f} "
      "(expected 28.0) OK")

_result = ace._pe_forward_method(_knsl_bundle, _knsl_g_earn, _knsl_discount, _knsl_normalized_eps)
assert _result is not None
(_value, _eps5, _fair_pe_5y, _today_pe, _year5_price, _reason, _fwd_used, _source) = _result
assert _reason is None, _reason
assert _fwd_used == 21.73, _fwd_used
assert _source == "5yr_average", _source
assert abs(_fair_pe_5y - 28.0) < 0.01, _fair_pe_5y
_expected_eps5 = 21.73 * (1 + _knsl_g_earn) ** 4
assert abs(_eps5 - _expected_eps5) < 0.01, (_eps5, _expected_eps5)
assert abs(_eps5 - 30.46) < 0.05, _eps5
_expected_value = _expected_eps5 * 28.0 / ((1 + _knsl_discount) ** 5)
assert abs(_value - _expected_value) < 0.01, (_value, _expected_value)
assert abs(_value - 544) < 2.0, _value
print(f"[knsl_value_544] Forecast EPS(yr5)={_eps5:.2f} x fair_pe_5y={_fair_pe_5y:.1f} "
      f"discounted -> ${_value:.2f} (instruction's own target ~=$544, was $363 under "
      "today's single-day multiple) OK")

# ======================================================================
# Peak-multiple fixture: today's P/E (45x) is well above the 5-yr
# median (30x, built from a flat 10/share EPS and prices 350/320/300/
# 280/250 -> P/Es 35/32/30/28/25) - the owner's "value falls by a
# third" case: the multiplier actually applied (30x) is exactly 2/3 of
# what today's multiple (45x) would have produced, so the discounted
# value is exactly 2/3 of what it would have been under the old,
# today's-P/E-driven method.
# ======================================================================
_PEAK_EPS = [10.0] * 5
_PEAK_PRICES = [350.0, 320.0, 300.0, 280.0, 250.0]  # -> P/Es 35/32/30/28/25, median 30
_peak_bundle = _mk_bundle(_PEAK_EPS, _PEAK_PRICES, price_now=450.0)  # 450/10 = 45x today
_peak_g_earn, _peak_discount = 0.08, 0.09
_peak_normalized_eps = ace._normalized_eps(_peak_bundle)
_peak_result = ace._pe_forward_method(
    _peak_bundle, _peak_g_earn, _peak_discount, _peak_normalized_eps)
assert _peak_result is not None
(_peak_value, _peak_eps5, _peak_fair_pe, _peak_today_pe, _peak_year5, _peak_reason,
 _peak_fwd_used, _peak_source) = _peak_result
assert _peak_reason is None, _peak_reason
assert abs(_peak_fair_pe - 30.0) < 0.01, _peak_fair_pe
assert abs(_peak_today_pe - 45.0) < 0.01, _peak_today_pe
assert _peak_source == "5yr_average", _peak_source
_peak_value_at_todays_pe = _peak_eps5 * _peak_today_pe / ((1 + _peak_discount) ** 5)
_ratio = _peak_value / _peak_value_at_todays_pe
assert abs(_ratio - (30.0 / 45.0)) < 1e-9, _ratio
print(f"[peak_multiple_value_falls_a_third] today's P/E (45.0x) vs 5-yr median (30.0x) -> "
      f"value is {_ratio:.3f}x what today's multiple alone would have produced "
      "(expected 0.667x, i.e. falls by a third) OK")

# ======================================================================
# 2-usable-years fixture: only 2 fiscal years of statement history
# (both usable - positive EPS, matched price) is still fewer than
# PE_FORWARD_5Y_MIN_YEARS (3) - falls back to today's own P/E (clamped),
# flagged pe_forward_multiple_source="current (insufficient history)".
# ======================================================================
_TWO_YEAR_EPS = [10.0, 10.0]
_TWO_YEAR_PRICES = [250.0, 220.0]  # -> P/Es 25x/22x, but only 2 years - not trusted
_two_year_bundle = _mk_bundle(_TWO_YEAR_EPS, _TWO_YEAR_PRICES, price_now=300.0)  # today 30x
# No quarterly statement in this fixture -> _eps_ttm() falls back to
# info["trailingEps"] (see that function's own docstring) - needed so
# normalized_eps["value"] is usable (base_eps) even though the 2-year
# window can't produce a median5_clean figure.
_two_year_bundle["info"]["trailingEps"] = 10.0
_two_year_normalized_eps = ace._normalized_eps(_two_year_bundle)
_fair_pe_2y, _reason_2y = ace._fair_pe_5y(_two_year_bundle, _two_year_normalized_eps)
assert _fair_pe_2y is None, _fair_pe_2y
assert _reason_2y == "insufficient_history", _reason_2y

_two_year_result = ace._pe_forward_method(
    _two_year_bundle, 0.08, 0.09, _two_year_normalized_eps)
assert _two_year_result is not None
(_ty_value, _ty_eps5, _ty_fair_pe, _ty_today_pe, _ty_year5, _ty_reason, _ty_fwd_used,
 _ty_source) = _two_year_result
assert _ty_reason is None, _ty_reason
assert _ty_source == "current (insufficient history)", _ty_source
assert abs(_ty_fair_pe - _ty_today_pe) < 1e-9, (_ty_fair_pe, _ty_today_pe)
assert abs(_ty_today_pe - 30.0) < 0.01, _ty_today_pe
print("[insufficient_history_falls_back] only 2 usable fiscal years -> fair_pe_5y falls "
      f"back to today's own P/E ({_ty_today_pe:.1f}x), flagged "
      f"pe_forward_multiple_source={_ty_source!r} OK")

# ======================================================================
# Floor clamp: a median well below 5x is clamped up to
# PE_FORWARD_5Y_FLOOR (5.0).
# ======================================================================
_FLOOR_EPS = [10.0] * 5
_FLOOR_PRICES = [40.0, 30.0, 20.0, 30.0, 20.0]  # -> P/Es 4/3/2/3/2, median 3
_floor_bundle = _mk_bundle(_FLOOR_EPS, _FLOOR_PRICES, price_now=100.0)
_floor_normalized_eps = ace._normalized_eps(_floor_bundle)
_floor_fair_pe, _floor_reason = ace._fair_pe_5y(_floor_bundle, _floor_normalized_eps)
assert _floor_reason is None, _floor_reason
assert abs(_floor_fair_pe - 3.0) < 0.01, _floor_fair_pe  # the raw median, UNCLAMPED
_floor_result = ace._pe_forward_method(_floor_bundle, 0.08, 0.09, _floor_normalized_eps)
(_fl_value, _fl_eps5, _fl_fair_pe, _fl_today_pe, _fl_year5, _fl_reason, _fl_fwd_used,
 _fl_source) = _floor_result
assert _fl_reason is None, _fl_reason
assert abs(_fl_fair_pe - ace.PE_FORWARD_5Y_FLOOR) < 1e-9, _fl_fair_pe
print(f"[floor_clamp] raw median P/E (3.0x, from 4/3/2/3/2) clamped up to "
      f"PE_FORWARD_5Y_FLOOR ({ace.PE_FORWARD_5Y_FLOOR}x) before being applied OK")

# ======================================================================
# Ceiling clamp: a median well above 40x is clamped down to
# PE_FORWARD_5Y_CEIL (40.0).
# ======================================================================
_CEIL_EPS = [10.0] * 5
_CEIL_PRICES = [500.0, 550.0, 480.0, 520.0, 450.0]  # -> P/Es 50/55/48/52/45, median 50
_ceil_bundle = _mk_bundle(_CEIL_EPS, _CEIL_PRICES, price_now=1000.0)
_ceil_normalized_eps = ace._normalized_eps(_ceil_bundle)
_ceil_fair_pe, _ceil_reason = ace._fair_pe_5y(_ceil_bundle, _ceil_normalized_eps)
assert _ceil_reason is None, _ceil_reason
assert abs(_ceil_fair_pe - 50.0) < 0.01, _ceil_fair_pe  # the raw median, UNCLAMPED
_ceil_result = ace._pe_forward_method(_ceil_bundle, 0.08, 0.09, _ceil_normalized_eps)
(_cl_value, _cl_eps5, _cl_fair_pe, _cl_today_pe, _cl_year5, _cl_reason, _cl_fwd_used,
 _cl_source) = _ceil_result
assert _cl_reason is None, _cl_reason
assert abs(_cl_fair_pe - ace.PE_FORWARD_5Y_CEIL) < 1e-9, _cl_fair_pe
print(f"[ceiling_clamp] raw median P/E (50.0x, from 50/55/48/52/45) clamped down to "
      f"PE_FORWARD_5Y_CEIL ({ace.PE_FORWARD_5Y_CEIL}x) before being applied OK")

# ======================================================================
# _build_fair_value() end-to-end: the "5-yr average P/E" display row
# shows BOTH numbers, and is flagged (red) only on the insufficient-
# history fallback - never on a genuine 5-yr median.
# ======================================================================
_dcf_result = {"growth": _knsl_g_earn, "discount_rate": _knsl_discount}
_fv = ace._build_fair_value(_knsl_bundle, "KNSL", _dcf_result, None)
_pe_forward_rows = {r["label"]: r for r in _fv["valuation_inputs"]["KNSL"]["pe_forward"]}
assert "5-yr average P/E" in _pe_forward_rows, list(_pe_forward_rows)
_row = _pe_forward_rows["5-yr average P/E"]
assert _row["value"] == "28.0x (today " + f"{322.75 / 10.0:.1f}x)", _row["value"]
assert not _row.get("flagged"), _row
print(f"[display_row] 'pe_forward' valuation_inputs shows {_row['value']!r}, not flagged OK")

_fv_fallback = ace._build_fair_value(_two_year_bundle, "TWOYR", {"growth": 0.08, "discount_rate": 0.09}, None)
_pe_forward_rows_fb = {r["label"]: r for r in _fv_fallback["valuation_inputs"]["TWOYR"]["pe_forward"]}
assert _pe_forward_rows_fb["5-yr average P/E"]["flagged"] is True, _pe_forward_rows_fb

print("\nALL PE FORWARD 5-YR AVERAGE FIXTURES PASSED")
