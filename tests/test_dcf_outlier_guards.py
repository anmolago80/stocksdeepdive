"""
Bad-data outlier guards (owner-directed, 28 Sep 2026, TOYO false positive -
live symptoms: price $4.47, DCF $41.67, growth 20% from the "reported
growth" fallback, a micro-cap). Three guards, all in fcf_valuation_engine.py/
resolver_engine.py:

1. REPORTED-GROWTH CAP: the "info" fallback (info["earningsGrowth"]/
   info["revenueGrowth"], a single-period figure) is now held to
   REPORTED_GROWTH_CAP (8%), via min(REPORTED_GROWTH_CAP, tier_ceiling) -
   see fcf_valuation_engine.py's own comment on that constant.
   Historical-avg growth and the market-cap tier ceilings themselves
   (8/12/16/20%) are UNCHANGED.
2. RISING CAPEX: normalized_base_and_series() now compares the latest
   year's capex against the average; when it's more than 1.5x the
   average, the BASE year alone uses the MIDPOINT of (latest capex,
   average capex) instead of the plain average - meta["capex_basis"]
   records "average" or "midpoint (capex rising)". The historical growth
   series (used for CAGR) is UNCHANGED - still fully average-based.
3. SANITY FLAG (resolver_engine.dcf_looks_unreliable(), DISPLAY ONLY):
   fires when the DCF intrinsic value is more than 3x the current price.
   Never touches Top 100 selection/scoring - verified explicitly below by
   inspecting calculate_long_score()'s and composite_score()'s own source.

Run: python3 tests/test_dcf_outlier_guards.py
"""
import inspect
import os
import sys
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve
import resolver_engine
import ranking_engine
import top100_engine
import nightly_scan


def _mkdf(ocf, capex):
    """A cash-flow statement DataFrame shaped like normalized_base_and_
    series() expects: one column per year, most-recent-first, two rows
    (Operating Cash Flow, Capital Expenditure - capex already negative,
    matching the real statement convention)."""
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


# TOYO-shaped: OCF volatile enough that historical growth is degenerate
# (0%, fails the "g > 0" clean check) -> falls through to the "info"
# reported-growth fallback (40% -> capped to 8%). Capex more than doubles
# in the latest year (-30 vs an average of -11.25) -> midpoint basis.
TOYO_CF = _mkdf([120, 40, 130, 30], [-30, -5, -5, -5])
TOYO_INFO = {"earningsGrowth": 0.40, "currency": "USD", "marketCap": 50_000_000}

# ADP-, CPRT-, AOS-shaped: smooth, positive, low-volatility OCF with
# STABLE capex (latest year well under 1.5x the average) -> clean
# historical growth is used, average capex basis, unaffected by either
# guard. Market caps match this repo's own tier-placement fixtures
# (tests/test_discount_tier_growth_rewrite.py): ADP ~$105B (large-cap),
# CPRT ~$25.5B (mid-cap), AOS ~$8B.
ADP_CF = _mkdf([500, 470, 450, 430], [-40, -38, -36, -34])
ADP_INFO = {"currency": "USD", "marketCap": 105_000_000_000}

CPRT_CF = _mkdf([300, 280, 260, 240], [-25, -24, -23, -22])
CPRT_INFO = {"currency": "USD", "marketCap": 25_500_000_000}

AOS_CF = _mkdf([250, 230, 210, 190], [-15, -14, -13, -12])
AOS_INFO = {"currency": "USD", "marketCap": 8_000_000_000}


# ======================================================================
# CHECK 1a (unit level): normalized_base_and_series() - rising capex ->
# midpoint basis, TOYO-shaped. The growth series stays fully average-
# based (Task 2 requirement: "the historical growth series is
# unchanged").
# ======================================================================
_base, _series, _src, _base_norm, _capex_basis = fve.normalized_base_and_series(TOYO_CF, info={})
assert _src == "ocf-normcapex", _src
assert _capex_basis == "midpoint (capex rising)", _capex_basis
assert abs(_base - 99.375) < 1e-6, _base
assert _base_norm is False, _base_norm  # not also caught by the Task 10 outlier-vs-median swap
assert _series == [108.75, 28.75, 118.75, 18.75], _series  # unchanged - still fully average-capex-based
print(f"[toyo_capex_midpoint] latest capex -30 vs average -11.25 (>1.5x) -> capex_basis="
      f"{_capex_basis!r}, base={_base} (avg-only would have been {120 - 11.25}), growth "
      f"series {_series} untouched OK")

# ======================================================================
# CHECK 1b (unit level): stable capex (ADP/CPRT/AOS-shaped) -> "average"
# basis, unaffected by the guard.
# ======================================================================
for _name, _cf in (("ADP", ADP_CF), ("CPRT", CPRT_CF), ("AOS", AOS_CF)):
    _b, _s, _sr, _bn, _cb = fve.normalized_base_and_series(_cf, info={})
    assert _cb == "average", (_name, _cb)
    assert _bn is False, (_name, _bn)
print("[stable_capex_average_basis] ADP/CPRT/AOS-shaped (latest capex well under 1.5x average) "
      "-> capex_basis='average' for all three, unaffected by the guard OK")


# ======================================================================
# CHECK 2a (unit level): REPORTED_GROWTH_CAP - a reported 40% figure
# caps to 8% (governor="Cap"), NOT the tier's own looser ceiling.
# ======================================================================
_g, _gsrc, _gov = fve.estimate_growth({"earningsGrowth": 0.40}, fcf_series=None,
                                       analyst_growth=None, ceiling=0.20)
assert abs(_g - 0.08) < 1e-9, _g
assert _gsrc == "info" and _gov == "Cap", (_gsrc, _gov)
print(f"[reported_growth_capped] info earningsGrowth=40% against a 20% micro-cap tier ceiling -> "
      f"{_g:.2f} (8% REPORTED_GROWTH_CAP, not the looser 20% tier ceiling), source={_gsrc!r}, "
      f"governor={_gov!r} OK")

# A value already at/under the cap is NOT flagged "Cap" - the cap is a
# ceiling, not a re-labeling of every "info" result.
_g2, _gsrc2, _gov2 = fve.estimate_growth({"earningsGrowth": 0.05}, fcf_series=None,
                                          analyst_growth=None, ceiling=0.20)
assert abs(_g2 - 0.05) < 1e-9 and _gov2 == "Info", (_g2, _gov2)
print(f"[reported_growth_under_cap_uncapped] info earningsGrowth=5% (under the 8% cap) -> "
      f"{_g2:.2f}, governor={_gov2!r} (not 'Cap') OK")

# "or the tier cap if lower" - a hypothetical future tier tighter than 8%
# must still win via the min(REPORTED_GROWTH_CAP, ceiling) design.
_g3, _gsrc3, _gov3 = fve.estimate_growth({"earningsGrowth": 0.30}, fcf_series=None,
                                          analyst_growth=None, ceiling=0.05)
assert abs(_g3 - 0.05) < 1e-9, _g3
print(f"[reported_growth_respects_tighter_tier_cap] a hypothetical 5% tier ceiling (tighter than "
      f"the 8% REPORTED_GROWTH_CAP) still wins via min() -> {_g3:.2f} OK")

# Historical-avg growth and the tier caps themselves are UNCHANGED - the
# 20/16/12/8% ceilings are read straight off the untouched table.
assert fve.MARKET_CAP_GROWTH_CEILINGS == [
    (200_000_000_000, 0.08), (10_000_000_000, 0.12), (2_000_000_000, 0.16), (0, 0.20),
], fve.MARKET_CAP_GROWTH_CEILINGS
print("[tier_ceilings_unchanged] MARKET_CAP_GROWTH_CEILINGS (8/12/16/20%) untouched by this fix OK")


# ======================================================================
# CHECK 3 (full pipeline): TOYO-shaped - reported growth 40% -> 8%,
# rising capex -> midpoint basis, resulting IV > 3x an (artificially low,
# matching the live incident's own $4.47-vs-$41.67 ~9x ratio) price ->
# sanity flag fires.
# ======================================================================
with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _iv_toyo, _g_toyo, _m_toyo = fve.dcf_intrinsic_value(
        "TOYO", info=TOYO_INFO, cashflow_df=TOYO_CF, currency="USD",
        discount_rate=0.12, perpetual_rate=0.02, diluted_shares_override=10,
    )
assert _m_toyo["growth_source"] == "info", _m_toyo["growth_source"]
assert _m_toyo["growth_governor"] == "Cap", _m_toyo["growth_governor"]
assert abs(_g_toyo - 0.08) < 1e-6, _g_toyo
assert _m_toyo["capex_basis"] == "midpoint (capex rising)", _m_toyo["capex_basis"]
assert _m_toyo["fcf_base_normalized"] is False, _m_toyo["fcf_base_normalized"]
assert abs(_iv_toyo - 147.51) < 0.5, _iv_toyo
_toyo_price = _iv_toyo / 10  # ~$14.75 -> IV is ~10x price, matching the live incident's own ~9x
assert resolver_engine.dcf_looks_unreliable(_iv_toyo, _toyo_price) is True
print(f"[toyo_full_pipeline] growth 40% -> {_g_toyo:.2f} ({_m_toyo['growth_source']}/"
      f"{_m_toyo['growth_governor']}), capex_basis={_m_toyo['capex_basis']!r}, IV=${_iv_toyo:.2f} "
      f"vs price ${_toyo_price:.2f} ({_iv_toyo / _toyo_price:.1f}x) -> "
      f"dcf_looks_unreliable=True OK")

# resolver_engine.resolve_intrinsic_value() threads capex_basis straight
# through (pure passthrough, same pattern as every other *_source/*_used
# key) - this is what deep_dive_engine.py/auto_compounder_engine.py both
# read to display it. resolve_intrinsic_value() has no diluted_shares_
# override parameter (unlike dcf_intrinsic_value() itself) - it always
# resolves shares from info["sharesOutstanding"] via share_class_engine,
# so this call needs that key explicitly (TOYO_INFO omits it since the
# full-pipeline check above passes diluted_shares_override instead).
_TOYO_INFO_WITH_SHARES = dict(TOYO_INFO, sharesOutstanding=10)
with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _iv_r, _src_r, _g_r, _meta_r = resolver_engine.resolve_intrinsic_value(
        "TOYO", None, info=_TOYO_INFO_WITH_SHARES, cashflow_df=TOYO_CF, currency="USD",
        discount_rate=0.12, perpetual_rate=0.02, manual_fcf=None,
    )
assert _src_r == "dcf", _src_r
assert _meta_r["capex_basis"] == "midpoint (capex rising)", _meta_r["capex_basis"]
assert abs(_iv_r - _iv_toyo) < 0.01, (_iv_r, _iv_toyo)  # same 10 shares as the full-pipeline check above
print("[resolver_engine_capex_basis_passthrough] resolve_intrinsic_value()'s meta carries "
      "capex_basis straight through from dcf_intrinsic_value() OK")

# nightly_scan.py's per-row "DCF Unreliable" field uses the exact same
# helper with the row's own intrinsic/current_price - confirmed by
# calling it the same way analyze_ticker_lite() does.
assert nightly_scan.dcf_looks_unreliable is resolver_engine.dcf_looks_unreliable
assert nightly_scan.dcf_looks_unreliable(_iv_toyo, _toyo_price) is True
print("[nightly_scan_same_helper] nightly_scan.py imports the exact same dcf_looks_unreliable "
      "helper (not a second, re-derived copy) OK")


# ======================================================================
# CHECK 4 (full pipeline): ADP-, CPRT-, AOS-shaped - unchanged. Historical
# growth used (clean, positive, well under each tier's ceiling), average
# capex basis, no sanity flag at a normal price/IV ratio.
# ======================================================================
_EXPECTED = {
    "ADP": (ADP_CF, ADP_INFO, 0.08, 0.0544),
    "CPRT": (CPRT_CF, CPRT_INFO, 0.09, 0.0847),
    "AOS": (AOS_CF, AOS_INFO, 0.10, 0.102),
}
_results = {}
with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    for _name, (_cf, _info, _dr, _expected_g) in _EXPECTED.items():
        _iv, _g, _m = fve.dcf_intrinsic_value(
            _name, info=_info, cashflow_df=_cf, currency="USD",
            discount_rate=_dr, perpetual_rate=0.02, diluted_shares_override=100,
        )
        assert _m["growth_source"] == "history", (_name, _m["growth_source"])
        assert _m["growth_governor"] == "History", (_name, _m["growth_governor"])
        assert abs(_g - _expected_g) < 0.002, (_name, _g, _expected_g)
        assert _m["capex_basis"] == "average", (_name, _m["capex_basis"])
        assert _m["fcf_base_normalized"] is False, (_name, _m["fcf_base_normalized"])
        # No sanity flag at a normal price/IV ratio (price == IV here, the
        # most conservative "definitely not >3x" case).
        assert resolver_engine.dcf_looks_unreliable(_iv, _iv) is False, _name
        _results[_name] = (_iv, _g, _m)
        print(f"[{_name.lower()}_full_pipeline_unchanged] growth {_g:.2%} (history/History, "
              f"unchanged), capex_basis='average' (unchanged), IV=${_iv:.2f}, no sanity flag OK")


# ======================================================================
# CHECK 5: the sanity flag is genuinely DISPLAY-ONLY - grep the actual
# source of calculate_long_score() (ranking_engine.py) and
# composite_score() (top100_engine.py) and confirm neither mentions the
# new field/function by name, so a future refactor can't accidentally
# wire it into scoring/selection without this test catching it.
# ======================================================================
_long_score_src = inspect.getsource(ranking_engine.calculate_long_score)
_composite_score_src = inspect.getsource(top100_engine.composite_score)
for _needle in ("dcf_unreliable", "DCF Unreliable", "dcf_looks_unreliable", "capex_basis"):
    assert _needle not in _long_score_src, _needle
    assert _needle not in _composite_score_src, _needle
print("[never_feeds_scoring] calculate_long_score()'s and composite_score()'s own source "
      "mention neither the sanity flag nor capex_basis - confirmed display-only OK")


print("\nFixture values (per the task's own request):")
print(f"  TOYO : growth 40%->{_g_toyo:.0%} ({_m_toyo['growth_source']}/{_m_toyo['growth_governor']}), "
      f"capex_basis={_m_toyo['capex_basis']!r}, IV=${_iv_toyo:.2f} "
      f"(vs an illustrative price ${_toyo_price:.2f}, {_iv_toyo / _toyo_price:.1f}x -> flagged)")
for _name in ("ADP", "CPRT", "AOS"):
    _iv, _g, _m = _results[_name]
    print(f"  {_name:<4} : growth {_g:.2%} ({_m['growth_source']}/{_m['growth_governor']}), "
          f"capex_basis={_m['capex_basis']!r}, IV=${_iv:.2f} (unchanged, no flag)")

print("\nAll outlier-guard checks passed.")
