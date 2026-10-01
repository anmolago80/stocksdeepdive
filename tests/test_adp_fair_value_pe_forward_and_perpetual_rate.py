"""
Owner-approved fix (28 Sep 2026, ADP Fair Value tab screenshot), Items 1
and 3 from the read-only investigation this session already did.

ITEM 1: _pe_forward_method() computed Forecast EPS(5y) x Actual P/E and
presented the raw product as today's Intrinsic Value with NO
discounting - a genuine year-5 price target, not a present value (the
owner's own reported example: $429.88 shown, ~$276 the correct
discounted figure at ADP's 9.3% DCF discount rate). Fixed by discounting
the year-5 product back 5 years at the SAME discount_rate the DCF box
already uses, and relabelling the chart bar "PE Forward (Y5,
discounted)" (compounder_ui.py's _CP_VALUATION_METHOD_ORDER).

ITEM 3: fcf_valuation_engine.DEFAULT_PERPETUAL_RATE was a hardcoded
0.03 (3%) - a THIRD terminal-growth value matching neither of
capm_engine.PERPETUAL_GROWTH_BY_CCY's own currency-keyed rates (AUD
2.5% / USD 2.0%). Now imported from capm_engine.DEFAULT_PERPETUAL_
GROWTH instead of hardcoded. Also: the Fair Value tab's "Perpetual
Rate" input label now names the currency it was resolved from (e.g.
"Perpetual Rate (USD)"), since a bare "2.0%" invited exactly the
"shouldn't this match 2.5%?" question that started this investigation
- it's currency-keyed by design, not a bug, and now says so on screen.

DISPLAY-ONLY CONFIRMATION: both fixes are confined to auto_compounder_
engine.py's _pe_forward_method()/_build_fair_value() (used ONLY by the
Rational Compounder Analysis / Fair Value tab - see this session's
earlier call-graph investigation: nightly_scan.py never calls either)
and compounder_ui.py's chart label. The ONE line that changed in
fcf_valuation_engine.py (DEFAULT_PERPETUAL_RATE) is used in exactly one
place in that whole file - the `except Exception:` fallback inside
dcf_intrinsic_value() when capm_engine.resolve_perpetual_rate() itself
raises. That function is a pure dict-lookup + arithmetic on a string
currency code (PERPETUAL_GROWTH_BY_CCY.get(...) then a max/min clamp) -
no I/O, no network call, nothing that realistically throws for any
currency string derived from info.get("currency") - so in practice this
exception path is never hit in live Scanner/Top 100/nightly-scan runs,
and this change alters zero live/saved scan values. It is, strictly,
the SAME constant the live DCF's exception fallback would use if that
branch were ever reached - flagged here explicitly rather than silently
claimed as fully isolated.

Run: python3 tests/test_adp_fair_value_pe_forward_and_perpetual_rate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capm_engine
import fcf_valuation_engine
import auto_compounder_engine as ace
import compounder_ui as cui

# ======================================================================
# ITEM 1: pe_forward now discounts back 5 years at the DCF's own rate
# ======================================================================

# ADP-like inputs, reverse-engineered so forecast_eps_5y == 17.84 and
# actual_pe == 24.10 exactly: trailing_eps = 1.0 (a bare unit so
# forecast_eps_5y = (1+g)^5 and actual_pe = price_now directly), solved
# for the growth rate g that makes (1+g)^5 = 17.84.
_TRAILING_EPS = 1.0
_G_EARN = 17.84 ** (1 / 5) - 1
_PRICE_NOW = 24.10
_DISCOUNT_RATE = 0.093

_bundle = {
    "info": {"trailingEps": _TRAILING_EPS, "currentPrice": _PRICE_NOW, "currency": "USD"},
    "income_q": None,   # forces _eps_ttm() to fall back to info["trailingEps"]
    "income": None,
    "balance": None,
    "cashflow": None,
    "prices_10y": {"dates": [], "prices": []},
}

# Push 3 (owner-directed, 30 Sep 2026): _pe_forward_method() now takes
# a normalized_eps dict (see auto_compounder_engine._normalized_eps())
# in place of reading trailing EPS internally - this bundle's shape
# (income=None, trailingEps=1.0) makes _normalized_eps() fall through
# to the SAME 1.0 TTM value this fixture was built around, via
# ace._normalized_eps(_bundle) directly (the same call _build_fair_
# value() itself makes internally), not hand-constructed, so this stays
# in sync with the real function.
_normalized_eps_fixture = ace._normalized_eps(_bundle)
assert _normalized_eps_fixture["value"] == 1.0, _normalized_eps_fixture

# PE Forward year-5 fix (1 Oct 2026, owner-directed, KNSL/Kinsale
# Capital live case): _pe_forward_method() now returns a 6-tuple (added
# forward_eps_used) - see that function's own docstring. This fixture's
# bundle has no "forwardEps" key at all, so it exercises the UNCHANGED
# trailing-EPS-compounded branch (forward_eps_used stays None) - the
# formula-level assertions below remain numerically valid.
_result = ace._pe_forward_method(_bundle, _G_EARN, _DISCOUNT_RATE, _normalized_eps_fixture)
assert _result is not None
_value, _forecast_eps_5y, _actual_pe, _year5_price, _reason, _forward_eps_used = _result
assert _reason is None, _reason
assert _forward_eps_used is None, _forward_eps_used

assert abs(_forecast_eps_5y - 17.84) < 0.01, _forecast_eps_5y
assert abs(_actual_pe - 24.10) < 0.01, _actual_pe
assert abs(_year5_price - 429.844) < 0.5, _year5_price
print(f"[undiscounted_year5_matches_owner_report] Forecast EPS(5y)={_forecast_eps_5y:.2f} x "
      f"Actual P/E={_actual_pe:.2f} = ${_year5_price:.2f} (owner reported $429.88) OK")

assert 274 < _value < 278, _value
print(f"[discounted_value_matches_owner_estimate] discounted at {_DISCOUNT_RATE:.1%} over 5y: "
      f"${_value:.2f} (owner's own estimate: ~$276) OK")

# No discount rate available -> withheld, never silently shown undiscounted.
assert ace._pe_forward_method(_bundle, _G_EARN, None) is None
print("[no_discount_rate_withholds_method] discount_rate=None -> method returns None, "
      "not the undiscounted year-5 number OK")

# The chart bar itself is relabelled so the number's nature is clear on sight.
_pe_forward_entry = next(e for e in cui._CP_VALUATION_METHOD_ORDER if e[0] == "pe_forward")
assert "Y5" in _pe_forward_entry[1] and "discount" in _pe_forward_entry[1].lower(), _pe_forward_entry
print(f"[chart_label_relabelled] pe_forward bar label is now {_pe_forward_entry[1]!r} OK")

# _build_fair_value() end-to-end: dcf_result carries the discount rate,
# pe_forward_value comes out discounted and consistent with the direct call.
# Change 3 (28 Sep 2026, later same day): _build_fair_value() now takes a
# SEPARATE canonical_dcf_result for the "dcf" row itself (see that
# function's own docstring) - dcf_result here still drives pe_forward/
# pe_trailing/equity_10y exactly as before; a matching canonical result is
# passed too so the "dcf" row's own assertions below keep working.
_dcf_result = {"value": 5000.0, "growth": _G_EARN, "perpetual_rate": 0.02, "discount_rate": _DISCOUNT_RATE}
_canonical_dcf_result = {"value": 5000.0, "growth": _G_EARN, "perpetual_rate": 0.02, "discount_rate": _DISCOUNT_RATE}
_fv = ace._build_fair_value(_bundle, "ADP", _dcf_result, _canonical_dcf_result)
_methods = _fv["valuation_methods"]["ADP"]
assert abs(_methods["pe_forward"] - _value) < 0.01, _methods["pe_forward"]
print(f"[build_fair_value_wires_through] _build_fair_value()'s own pe_forward value "
      f"(${_methods['pe_forward']:.2f}) matches the direct discounted calculation OK")

_pe_forward_inputs = {i["label"]: i["value"] for i in _fv["valuation_inputs"]["ADP"]["pe_forward"]}
assert "Year 5 Price (undiscounted)" in _pe_forward_inputs
assert abs(_pe_forward_inputs["Year 5 Price (undiscounted)"] - 429.844) < 0.5
print("[year5_price_shown_as_input] the raw, undiscounted year-5 figure is still shown as its "
      "own labelled input line (transparency), separate from the now-discounted headline value OK")

# ======================================================================
# ITEM 3: DEFAULT_PERPETUAL_RATE imported from capm_engine, not hardcoded
# ======================================================================

assert fcf_valuation_engine.DEFAULT_PERPETUAL_RATE == capm_engine.DEFAULT_PERPETUAL_GROWTH
assert fcf_valuation_engine.DEFAULT_PERPETUAL_RATE == 0.025  # no longer the old hardcoded 0.03
print(f"[default_perpetual_rate_imported] fcf_valuation_engine.DEFAULT_PERPETUAL_RATE "
      f"({fcf_valuation_engine.DEFAULT_PERPETUAL_RATE}) now equals capm_engine.DEFAULT_PERPETUAL_GROWTH, "
      "not a separately-hardcoded 0.03 OK")

# The Fair Value tab's "Perpetual Rate" input label now names the currency.
_dcf_result_usd = {"value": 5000.0, "growth": _G_EARN, "perpetual_rate": 0.02, "discount_rate": _DISCOUNT_RATE}
_fv_usd = ace._build_fair_value(_bundle, "ADP", _dcf_result_usd, _dcf_result_usd)
_dcf_inputs_usd = {i["label"]: i["value"] for i in _fv_usd["valuation_inputs"]["ADP"]["dcf"]}
assert "Perpetual Rate (USD)" in _dcf_inputs_usd, list(_dcf_inputs_usd)
assert abs(_dcf_inputs_usd["Perpetual Rate (USD)"] - 0.02) < 1e-9
print("[perpetual_rate_label_names_currency] a USD bundle's dcf input row is labelled "
      "'Perpetual Rate (USD)', not a bare 'Perpetual Rate' OK")

_bundle_aud = dict(_bundle)
_bundle_aud["info"] = dict(_bundle["info"])
_bundle_aud["info"]["currency"] = "AUD"
_aud_result = {"value": 5000.0, "growth": _G_EARN, "perpetual_rate": 0.025, "discount_rate": _DISCOUNT_RATE}
_fv_aud = ace._build_fair_value(_bundle_aud, "ADP.AX", _aud_result, _aud_result)
_dcf_inputs_aud = {i["label"]: i["value"] for i in _fv_aud["valuation_inputs"]["ADP.AX"]["dcf"]}
assert "Perpetual Rate (AUD)" in _dcf_inputs_aud, list(_dcf_inputs_aud)
print("[perpetual_rate_label_currency_varies] an AUD bundle's dcf input row is labelled "
      "'Perpetual Rate (AUD)' - confirms the label is dynamic, not hardcoded to one currency OK")

# ======================================================================
# DISPLAY-ONLY: confirm neither fix touches the live Scanner/Top 100/MOS
# scoring path (deep_dive_engine.dcf_intrinsic_value / nightly_scan.py).
# ======================================================================
import nightly_scan
import inspect

_nightly_src = inspect.getsource(nightly_scan)
assert "_pe_forward_method" not in _nightly_src
assert "_build_fair_value" not in _nightly_src
assert "auto_compounder_engine.build_sections" not in _nightly_src
print("[nightly_scan_untouched] nightly_scan.py (the Scanner/Top 100/nightly-scan pipeline) "
      "references none of the changed functions OK")

print("\nALL ADP FAIR VALUE (PE FORWARD DISCOUNTING + PERPETUAL RATE) FIXTURES PASSED")
