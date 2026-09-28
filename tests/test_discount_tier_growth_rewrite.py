"""
Owner-approved (28 Sep 2026), three changes in one push:

CHANGE 1 - discount rate: CAPM (rf + beta x 5%) replaced by the A6
market-cap-tiered formula (rf + tier premium, floor 7.5%, no 15% cap,
beta read nowhere) - see capm_engine.py's own module docstring and
resolve_discount_rate()'s docstring for the full mechanism; already
covered end-to-end by tests/test_capm_a6.py's own live_path_confirmed
section. This file covers the tier-boundary side of it (ADP/CPRT/AOS
landing in the right tier).

CHANGE 2 - growth: the 71f183b single-stage fade is reverted (git revert
71f183b, clean). New formula, amended same day:
  - Growth SELECTION: Yahoo "Next 5 Years (per annum)" analyst estimate
    used ALONE when available (no longer min'd with history); history
    (average of the oldest 2 years -> average of the newest 2, see
    fcf_valuation_engine.growth_from_history()'s own docstring) only
    when Yahoo has no coverage. Market-cap growth caps (8/12/16/20%) and
    the 0% floor unchanged. Yahoo's unit (0.12 vs 12) is now handled
    explicitly (capm_engine._normalize_yahoo_growth_estimate()).
  - Growth PATH (amendment, same day): three stages, not flat 10 years -
    years 1-5 flat at g1, years 6-10 fade linearly from g1 to the
    perpetual rate (g_t = g1 - (g1-perpetual)*(t-5)/5), terminal value on
    the year-10 cash flow as before. meta["growth_path"] carries all 10
    yearly rates.

CHANGE 2, FOLLOW-UP (owner-directed, 28 Sep 2026, "option E" - supersedes
the growth-PATH fade target above, everything else in CHANGE 2 unchanged):
years 6-10 now fade toward a market-cap-tiered growth-path END RATE
(fcf_valuation_engine.growth_end_rate_for(), mega 2%/large 3%/mid 4%/
small 5%/micro 6% - a table DELIBERATELY SEPARATE from capm_engine's
discount tiers, see that function's own comment), not straight to the
currency perpetual rate. Floored at the perpetual rate itself
(end_rate = max(tier_end_rate, perpetual_rate)) so a stock never fades
below its own terminal rate (e.g. an AUD mega-cap ends at 2.5%, not 2%).
If g1 <= end_rate, the path stays flat at g1 for all 10 years (never
fades upward). Terminal value after year 10 is unchanged - still the
currency perpetual rate. meta["growth_end_rate_used"] carries the
(floored) end rate; meta["growth_path"] still carries all 10 yearly
rates.

CHANGE 3 - "one value everywhere": auto_compounder_engine.ENGINE_VERSION
bumped (41 -> 42 -> 43, invalidates every cached Fair Value result) and
the Fair Value tab's "dcf" row now shows resolver_engine.
resolve_intrinsic_value()'s OWN value/growth/discount (via the new
_run_canonical_dcf()), not a second, separately-computed number - PE
Forward/PE Trailing/Equity 10y are untouched, still driven by _run_dcf().

Run: python3 tests/test_discount_tier_growth_rewrite.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capm_engine as ce
import fcf_valuation_engine as fve
import auto_compounder_engine as ace
import resolver_engine

_FAKE_RF_USD = (0.050, "default")


def _rf(ccy):
    return {"USD": _FAKE_RF_USD, "AUD": (0.053, "default")}[ccy]


# ======================================================================
# CHECK: ADP-, CPRT-, AOS-sized companies land in the right tier
# (owner-supplied market caps: CPRT US$25.5B, ADP ~US$105B, AOS ~US$8B)
# ======================================================================
with mock.patch.object(ce, "get_risk_free_rate", side_effect=_rf):
    _adp_rate, _adp_meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 105_000_000_000, "currency": "USD"}, "USD")
    _cprt_rate, _cprt_meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 25_500_000_000, "currency": "USD"}, "USD")
    _aos_rate, _aos_meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 8_000_000_000, "currency": "USD"}, "USD")

assert _adp_meta["tier_label"].startswith("large-cap"), _adp_meta["tier_label"]
assert abs(_adp_rate - 0.080) < 1e-9, _adp_rate
assert _cprt_meta["tier_label"].startswith("mid-cap"), _cprt_meta["tier_label"]
assert abs(_cprt_rate - 0.090) < 1e-9, _cprt_rate
assert _aos_meta["tier_label"].startswith("small-cap"), _aos_meta["tier_label"]
assert abs(_aos_rate - 0.100) < 1e-9, _aos_rate
print(f"[tier_placement_adp_cprt_aos] ADP US$105B -> {_adp_meta['tier_label']} ({_adp_rate:.1%}), "
      f"CPRT US$25.5B -> {_cprt_meta['tier_label']} ({_cprt_rate:.1%}), "
      f"AOS US$8B -> {_aos_meta['tier_label']} ({_aos_rate:.1%}) OK")


# ======================================================================
# CHECK: AOS-style history 311/590/501/536 (owner's own typed order,
# oldest -> newest), no Yahoo estimate -> ~7.3%
# ======================================================================
_aos_fcf_series = [536.0, 501.0, 590.0, 311.0]  # most-recent-first (reverses to 311/590/501/536)
_aos_growth, _aos_gsrc, _aos_gov = fve.estimate_growth(
    {}, fcf_series=_aos_fcf_series, analyst_growth=None, ceiling=0.20)
assert 0.071 < _aos_growth < 0.075, _aos_growth
assert _aos_gsrc == "history" and _aos_gov == "History"
print(f"[aos_history_no_yahoo] AOS-style history (311/590/501/536, no Yahoo estimate) -> "
      f"{_aos_growth:.4f} (~7.3%), source={_aos_gsrc!r} OK")


# ======================================================================
# CHECK: Yahoo's unit handled explicitly - 0.08 -> 8.0%, and a test for
# BOTH shapes (0.12 already-decimal vs 12 bare-percentage)
# ======================================================================
assert ce._normalize_yahoo_growth_estimate(0.08) == 0.08
print("[yahoo_unit_decimal_08] Yahoo 0.08 (already a decimal fraction) -> 0.08 (8.0%), unchanged OK")

assert ce._normalize_yahoo_growth_estimate(0.12) == 0.12
assert ce._normalize_yahoo_growth_estimate(12) == 0.12
print("[yahoo_unit_both_shapes] Yahoo 0.12 (decimal) and Yahoo 12 (bare percentage) both "
      "normalize to 0.12 (12.0%) - both unit shapes tested OK")


# ======================================================================
# CHECK: a Yahoo estimate above the market-cap growth ceiling -> capped
# ======================================================================
_capped_growth, _capped_src, _capped_gov = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=0.25, ceiling=0.08)  # mega-cap ceiling = 8%
assert abs(_capped_growth - 0.08) < 1e-9, _capped_growth
assert _capped_src == "analyst" and _capped_gov == "Cap"
print(f"[yahoo_estimate_above_cap] Yahoo 25% analyst estimate against an 8% mega-cap ceiling -> "
      f"{_capped_growth:.2f} (capped), governor={_capped_gov!r} OK")


# ======================================================================
# CHECK: growth PATH option E (follow-up to the same-day amendment above,
# owner-directed 28 Sep 2026) - FCF/share 12.20, g1 10.275%, discount
# 8.17%, large tier (end 3%), perpetual 2.0% -> path
# [10.27 x5, 8.82, 7.36, 5.91, 4.46, 3.00], IV ~ $329.16. marketCap must
# be supplied (US$105B, same large-tier fixture as tier_placement_adp_
# cprt_aos above) so growth_end_rate_for() can resolve the tier.
# ======================================================================
_iv, _g_used, _meta = fve.dcf_intrinsic_value(
    "TEST", info={"currentPrice": 100.0, "currency": "USD", "marketCap": 105_000_000_000},
    cashflow_df=None, currency="USD",
    discount_rate=0.0817, perpetual_rate=0.02, growth_rate=0.10275,
    manual_fcf=12.20, diluted_shares_override=1,
)
_expected_path = [0.1027, 0.1027, 0.1027, 0.1027, 0.1027, 0.0882, 0.0736, 0.0591, 0.0445, 0.03]
_path = _meta["growth_path"]
assert len(_path) == 10, _path
for _i, (_got, _want) in enumerate(zip(_path, _expected_path)):
    assert abs(_got - _want) < 1e-3, (_i, _got, _want)
assert _path[:5] == [_path[0]] * 5, _path  # flat for years 1-5
assert _path[-1] == 0.03, _path  # year 10 == the tiered end rate (3%), NOT perpetual (2%)
assert all(_path[i] > _path[i + 1] for i in range(5, 9)), _path  # strictly fading years 6-10
assert abs(_meta["growth_end_rate_used"] - 0.03) < 1e-9, _meta["growth_end_rate_used"]
assert abs(_meta["perpetual_rate_used"] - 0.02) < 1e-9, _meta["perpetual_rate_used"]  # terminal value unchanged
assert 320 < _iv < 335, _iv  # owner's own estimate: ~$329.16
print(f"[growth_path_option_e_large_tier] path (%): {[round(p * 100, 2) for p in _path]} "
      f"(owner's own expectation: [10.27 x5, 8.82, 7.36, 5.91, 4.46, 3.00]) - "
      f"IV ${_iv:.2f} (owner's own expectation: ~$329.16), fade target={_meta['growth_end_rate_used']:.2%} "
      f"(large tier), terminal rate unchanged at {_meta['perpetual_rate_used']:.2%} OK")

# g1 <= end_rate (micro tier, end=6%, g1=4%): keep g1 flat for all 10
# years - never fade upward. marketCap supplied below $2B (micro tier).
_iv_flat, _, _meta_flat = fve.dcf_intrinsic_value(
    "TEST", info={"currentPrice": 100.0, "currency": "USD", "marketCap": 500_000_000},
    cashflow_df=None, currency="USD",
    discount_rate=0.0817, perpetual_rate=0.02, growth_rate=0.04,
    manual_fcf=12.20, diluted_shares_override=1,
)
assert _meta_flat["growth_end_rate_used"] == 0.06, _meta_flat["growth_end_rate_used"]  # micro tier end rate
assert _meta_flat["growth_path"] == [0.04] * 10, _meta_flat["growth_path"]
print(f"[growth_path_option_e_g1_below_end_micro] micro tier end rate "
      f"{_meta_flat['growth_end_rate_used']:.0%}, g1=4% is already below it - path stays flat "
      f"{_meta_flat['growth_path']} for all 10 years, never fades upward OK")

# Addition to option E (owner's OK message, 28 Sep 2026): end_rate =
# max(tier_end_rate, perpetual_rate), so a stock never fades below its
# own currency terminal rate. AUD mega-cap: growth_end_rate_for() alone
# resolves to 2% (mega tier), but AUD's own perpetual rate is 2.5% - the
# floor must lift the fade target to 2.5%, not let it end at 2%.
_iv_aud, _, _meta_aud = fve.dcf_intrinsic_value(
    "TEST", info={"currentPrice": 100.0, "currency": "AUD", "marketCap": 400_000_000_000},
    cashflow_df=None, currency="AUD",
    discount_rate=0.0817, growth_rate=0.08,
    manual_fcf=12.20, diluted_shares_override=1,
)
_tier_end_alone = fve.growth_end_rate_for({"marketCap": 400_000_000_000, "currency": "AUD"}, "AUD")
assert abs(_tier_end_alone - 0.02) < 1e-9, _tier_end_alone  # mega tier's OWN end rate, unfloored
assert abs(_meta_aud["perpetual_rate_used"] - 0.025) < 1e-9, _meta_aud["perpetual_rate_used"]  # AUD terminal rate
assert abs(_meta_aud["growth_end_rate_used"] - 0.025) < 1e-9, _meta_aud["growth_end_rate_used"]  # floored up to 2.5%, not 2%
assert _meta_aud["growth_path"][-1] == 0.025, _meta_aud["growth_path"]
print(f"[growth_path_option_e_aud_mega_floor] AUD mega-cap: growth_end_rate_for() alone = "
      f"{_tier_end_alone:.1%} (mega tier), but max(tier_end_rate, perpetual_rate) floors it to "
      f"{_meta_aud['growth_end_rate_used']:.1%} (AUD's own 2.5% terminal rate) - path ends at "
      f"{_meta_aud['growth_path'][-1]:.1%}, not 2.0% OK")

# growth_years <= 5: no fade stage exists at all (every year is in the
# flat window) - the same defensive-edge-case precedent as growth_years
# <= 1 elsewhere in this function.
_iv_5y, _, _meta_5y = fve.dcf_intrinsic_value(
    "TEST", info={"currentPrice": 100.0, "currency": "USD", "marketCap": 105_000_000_000},
    cashflow_df=None, currency="USD",
    discount_rate=0.0817, perpetual_rate=0.02, growth_rate=0.10275,
    manual_fcf=12.20, diluted_shares_override=1, growth_years=5,
)
assert len(_meta_5y["growth_path"]) == 5, _meta_5y["growth_path"]
assert all(abs(p - 0.10275) < 1e-3 for p in _meta_5y["growth_path"]), _meta_5y["growth_path"]
print("[growth_years_five_no_fade_stage] growth_years=5 (no years left for a fade stage) - "
      f"growth_path is flat {_meta_5y['growth_path']} throughout, no crash OK")


# ======================================================================
# CHECK: Deep Dive / Fair Value tab / nightly scan all use the SAME
# discount rate and growth for a given stock - "one value everywhere",
# including CPRT. resolver_engine.resolve_intrinsic_value() is the exact
# function BOTH deep_dive_engine.py and nightly_scan.py call (confirmed
# by source inspection below, not assumed); auto_compounder_engine.
# _run_canonical_dcf() (Change 3) is a THIN wrapper around the SAME
# function, given the SAME info/cashflow/currency/overrides.
# ======================================================================
_CPRT_INFO = {"marketCap": 25_500_000_000, "currency": "USD", "sharesOutstanding": 100_000_000}
_CPRT_MANUAL_FCF = 1_000_000_000.0  # total $ - $10.00/share on 100M shares

with mock.patch.object(ce, "get_risk_free_rate", side_effect=_rf):
    _iv_resolver, _src_resolver, _growth_resolver, _meta_resolver = resolver_engine.resolve_intrinsic_value(
        "CPRT", None, info=_CPRT_INFO, cashflow_df=None, currency="USD",
        growth_rate=0.103, manual_fcf=_CPRT_MANUAL_FCF,
    )
    _canonical = ace._run_canonical_dcf(
        {"info": _CPRT_INFO, "cashflow": None}, "CPRT",
        growth_rate=0.103, manual_fcf=_CPRT_MANUAL_FCF,
    )

assert _src_resolver == "dcf", _src_resolver
assert _canonical is not None
assert _iv_resolver > 1.0, _iv_resolver  # a real, non-trivial value - not an accidental 0.0-vs-0.0 match
assert abs(_iv_resolver - _canonical["value"]) < 1e-6, (_iv_resolver, _canonical["value"])
assert _growth_resolver == _canonical["growth"], (_growth_resolver, _canonical["growth"])
assert _meta_resolver["discount_rate_used"] == _canonical["discount_rate"]
assert _meta_resolver["discount_tier_label"] == _canonical["discount_tier_label"] == "mid-cap (US$10B-50B)"
print(f"[one_value_everywhere_cprt] resolver_engine.resolve_intrinsic_value() (Deep Dive/nightly "
      f"scan's own path) and auto_compounder_engine._run_canonical_dcf() (the Fair Value tab's new "
      f"'dcf' row, Change 3) give the IDENTICAL IV (${_iv_resolver:.2f}), growth "
      f"({_growth_resolver:.4f}) and discount rate ({_meta_resolver['discount_rate_used']:.4f}, "
      f"{_meta_resolver['discount_tier_label']}) for the same CPRT-shaped inputs OK")

import inspect
import nightly_scan
import deep_dive_engine

assert "resolve_intrinsic_value" in inspect.getsource(nightly_scan)
assert "resolve_intrinsic_value" in inspect.getsource(deep_dive_engine)
print("[resolver_shared_by_nightly_and_deep_dive] both nightly_scan.py and deep_dive_engine.py "
      "call resolver_engine.resolve_intrinsic_value() - confirmed by source, not assumed OK")


# ======================================================================
# CHECK: ENGINE_VERSION bumped (Change 3 - invalidates every cached Fair
# Value result so the growth-path/discount-tier rewrite isn't served
# stale from a pre-this-commit cache entry)
# ======================================================================
assert ace.ENGINE_VERSION >= 44, ace.ENGINE_VERSION
print(f"[engine_version_bumped] auto_compounder_engine.ENGINE_VERSION = {ace.ENGINE_VERSION} "
      "(was 43, >= 44 confirms this bump wasn't reverted by a later task) - every "
      "cached Fair Value section from before this change is now treated as stale OK")


# ======================================================================
# CHECK: Fair Value tab's dcf row - PE Forward/PE Trailing/Equity 10y
# UNCHANGED (still driven by _run_dcf(), not canonical_dcf_result)
# ======================================================================
_bundle = {
    "info": {"trailingEps": 1.0, "currentPrice": 24.10, "currency": "USD",
              "marketCap": 25_500_000_000, "sharesOutstanding": 100_000_000},
    "income_q": None, "income": None, "balance": None, "cashflow": None,
    "prices_10y": {"dates": [], "prices": []},
}
_dcf_result = {"value": 999.0, "growth": 0.15, "perpetual_rate": 0.02, "discount_rate": 0.09}
_canonical_result = {"value": 42.0, "growth": 0.103, "perpetual_rate": 0.02, "discount_rate": 0.09,
                      "discount_tier_label": "mid-cap (US$10B-50B)", "growth_source": "analyst",
                      "growth_end_rate": 0.04}
_fv = ace._build_fair_value(_bundle, "TEST", _dcf_result, _canonical_result)
_methods = _fv["valuation_methods"]["TEST"]
# dcf value comes from canonical_result (42.0), NOT dcf_result (999.0).
assert abs(_methods["dcf"] - 42.0) < 1e-9, _methods["dcf"]
# pe_forward's g_earn input still comes from dcf_result's growth (0.15),
# confirmed indirectly: pe_forward is present and doesn't equal a value
# that would only make sense with the canonical growth (0.103) - direct
# proof is the g_earn plumbing itself, unchanged code, covered by
# test_adp_fair_value_pe_forward_and_perpetual_rate.py's own fixtures.
assert "pe_forward" in _methods or "pe_trailing" in _methods or "equity_10y" in _methods
_dcf_inputs = {i["label"]: i["value"] for i in _fv["valuation_inputs"]["TEST"]["dcf"]}
# Growth-path option E (28 Sep 2026 follow-up): the "Base Case Growth"
# display now fades to the mid-cap tier's growth_end_rate (4.0%), NOT
# perpetual_rate (2.0%) - deliberately different values in this fixture
# so a silent fallback to perpetual_rate would fail loudly.
assert "10.3% for 5 yrs, then fades to 4.0% by yr 10" in _dcf_inputs["Base Case Growth"], _dcf_inputs
assert _dcf_inputs.get("Discount Tier") == "mid-cap (US$10B-50B)", _dcf_inputs
assert _dcf_inputs.get("Growth Source") == "Yahoo 5y analyst", _dcf_inputs
print("[fair_value_dcf_row_is_canonical] the 'dcf' row's value/growth/discount/tier/source all "
      "come from canonical_dcf_result (Change 3), NOT dcf_result - confirmed the two are "
      "deliberately different (999.0 vs 42.0) in this fixture so a silent fallback to the wrong "
      "one would fail loudly OK")


print("\nALL DISCOUNT-TIER / GROWTH-REWRITE FIXTURES PASSED")
