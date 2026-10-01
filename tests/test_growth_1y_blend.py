"""
Growth 1y-blend fix (owner-directed, 1 Oct 2026 20:37 AEST, KNSL/
Kinsale Capital live case): fcf_valuation_engine.estimate_growth()'s
"ok_1y" (next-year Yahoo analyst consensus) branch. KNSL's Fair Value
tab (1 Oct 2026) showed "Base Case Growth: 2.5% for 5 yrs, then fades
to 5.0% by yr 10" - a bare next-year consensus used alone as a 10-year
DCF base rate, with the base rate BELOW the tier end rate (the fade
then runs uphill instead of down).

Fix: when yahoo_estimate_status == "ok_1y" AND clean FCF history is
available (>= MIN_HISTORY_POINTS_FOR_TREND points, CAGR > 0), the
next-year consensus is blended 50/50 with that history (history capped
at the tier ceiling BEFORE averaging), then the blended average itself
is floored at end_rate / capped at ceiling - source "analyst_1y_blend".
With no usable history, the bare consensus is used alone, floored at
end_rate - source stays "analyst_1y" (unchanged source name, new
governor text and floor behaviour).

Invariant asserted for BOTH new paths (by construction - each
explicitly floors at end_rate before returning): growth_used >=
end_rate for every input. This does NOT hold for the pre-existing,
untouched "analyst"(ok)/"history"/"history_volatile"/"info" paths -
each can return a raw signal below end_rate with no floor - that gap
is pre-existing and out of this task's scope (reported, not fixed).

Run: python3 tests/test_growth_1y_blend.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve

MEGA_CEILING, MEGA_END_RATE = 0.08, 0.02
SMALL_CEILING, SMALL_END_RATE = 0.20, 0.06

# ======================================================================
# Worked example 1 (KNSL-shaped): next-year consensus 2.5%, a clean
# 4-point FCF history whose own CAGR is ~35%, small-cap tier
# (ceiling=20%, end_rate=5%) -> blended average, history capped at the
# tier ceiling BEFORE averaging: (2.5% + 20%) / 2 = 11.25%.
# ======================================================================
_KNSL_ANALYST_1Y = 0.025
_KNSL_CEILING = 0.20
_KNSL_END_RATE = 0.05
# Reversed (oldest->newest) = [50, 60, 95, 105]: series[:2] avg=55,
# series[-2:] avg=100, years=2 -> cagr = sqrt(100/55)-1 ~= 34.8%,
# computed via growth_from_history() directly (not hand-derived), same
# convention as tests/test_analyst_1y_governor.py's own CHECK 4/5.
_knsl_hist = [105.0, 95.0, 60.0, 50.0]
_knsl_h = fve.growth_from_history(_knsl_hist)
assert _knsl_h is not None and _knsl_h > 0.30, _knsl_h  # confirms the "~35%" shape

_extra1 = {}
g1, src1, gov1, raw1 = fve.estimate_growth(
    {}, fcf_series=_knsl_hist, analyst_growth=_KNSL_ANALYST_1Y,
    ceiling=_KNSL_CEILING, end_rate=_KNSL_END_RATE, yahoo_estimate_status="ok_1y",
    extra_out=_extra1,
)
_expected_history_capped_1 = min(_knsl_h, _KNSL_CEILING)
_expected_base_1 = max(_KNSL_END_RATE, min(
    (_KNSL_ANALYST_1Y + _expected_history_capped_1) / 2.0, _KNSL_CEILING))
assert abs(g1 - _expected_base_1) < 1e-9, (g1, _expected_base_1)
assert abs(g1 - 0.1125) < 0.01, g1
assert src1 == "analyst_1y_blend", src1
assert gov1 == "next-year consensus blended with history (history capped at ceiling)", gov1
assert _extra1["growth_1y_consensus"] == _KNSL_ANALYST_1Y, _extra1
assert abs(_extra1["growth_history_capped"] - _expected_history_capped_1) < 1e-9, _extra1
print(f"[knsl_1y_blend_with_history] next-year consensus {_KNSL_ANALYST_1Y:.1%} blended "
      f"with ~35% history (capped at ceiling {_KNSL_CEILING:.0%}) -> {g1:.2%} "
      f"(expected ~=11.25%), source={src1!r} OK")

# ----------------------------------------------------------------------
# Worked example 2: same inputs, NO history -> bare consensus (2.5%)
# floored at end_rate (5%) -> 5.0%, source stays "analyst_1y".
# ----------------------------------------------------------------------
g2, src2, gov2, raw2 = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=_KNSL_ANALYST_1Y,
    ceiling=_KNSL_CEILING, end_rate=_KNSL_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g2 - _KNSL_END_RATE) < 1e-9, g2
assert src2 == "analyst_1y", src2
assert gov2 == "next-year consensus, floored at end rate", gov2
print(f"[knsl_1y_no_history_floored] same next-year consensus {_KNSL_ANALYST_1Y:.1%}, no "
      f"history -> floored at end_rate {_KNSL_END_RATE:.1%} -> {g2:.2%} OK")

# ======================================================================
# Worked example 3 (mega-cap): next-year consensus 12%, a clean 4-point
# history whose own CAGR is ~6%, mega-cap tier (ceiling=8%, end=2%) ->
# blended average (12% + 6%)/2 = 9%, capped at the 8% ceiling -> 8%.
# ======================================================================
_MEGA_ANALYST_1Y = 0.12
# Reversed (oldest->newest) = [100, 100, 110, 115]: avg(100,100)=100,
# avg(110,115)=112.5, years=2 -> cagr = sqrt(1.125)-1 ~= 6.07%.
_mega_hist = [115.0, 110.0, 100.0, 100.0]
_mega_h = fve.growth_from_history(_mega_hist)
assert _mega_h is not None and 0.04 < _mega_h < 0.08, _mega_h  # confirms the "~6%" shape

g3, src3, gov3, raw3 = fve.estimate_growth(
    {}, fcf_series=_mega_hist, analyst_growth=_MEGA_ANALYST_1Y,
    ceiling=MEGA_CEILING, end_rate=MEGA_END_RATE, yahoo_estimate_status="ok_1y",
)
_expected_history_capped_3 = min(_mega_h, MEGA_CEILING)
_expected_base_3 = max(MEGA_END_RATE, min(
    (_MEGA_ANALYST_1Y + _expected_history_capped_3) / 2.0, MEGA_CEILING))
assert abs(g3 - _expected_base_3) < 1e-9, (g3, _expected_base_3)
assert abs(g3 - MEGA_CEILING) < 1e-9, g3  # (12%+~6%)/2 ~= 9% > 8% ceiling -> clamped to 8%
assert src3 == "analyst_1y_blend", src3
print(f"[mega_1y_blend_clamped_to_ceiling] next-year consensus {_MEGA_ANALYST_1Y:.0%} "
      f"blended with ~6% history -> {g3:.2%} (clamped to the {MEGA_CEILING:.0%} ceiling) "
      f"source={src3!r} OK")

# ----------------------------------------------------------------------
# Worked example 4: same mega-cap inputs, NO history -> bare consensus
# (12%) capped at the plain tier ceiling (8%) -> 8%, source "analyst_1y"
# (same final number as example 3, different path - mirrors
# test_analyst_1y_governor.py's CHECK 3/3b).
# ----------------------------------------------------------------------
g4, src4, gov4, raw4 = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=_MEGA_ANALYST_1Y,
    ceiling=MEGA_CEILING, end_rate=MEGA_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g4 - MEGA_CEILING) < 1e-9, g4
assert src4 == "analyst_1y", src4
assert gov4 == "Cap", gov4
print(f"[mega_1y_no_history_capped] same next-year consensus {_MEGA_ANALYST_1Y:.0%}, no "
      f"history -> plain ceiling cap -> {g4:.2%}, source={src4!r} OK")

# ======================================================================
# No-uphill-fade invariant sweep: growth_used >= end_rate for every
# "ok_1y" input, across both the blend branch (enough clean history)
# and the bare-consensus branch (no/insufficient/volatile history) -
# holds BY CONSTRUCTION for these two paths (each explicitly floors at
# end_rate before returning). NOT asserted here for the untouched
# "analyst"(ok)/"history"/"history_volatile"/"info" paths, which can
# violate it (see this file's own module docstring + the final report).
# ======================================================================
random.seed(20261001)
_violations = []
for _ in range(500):
    _ceiling = random.choice([0.08, 0.12, 0.16, 0.20])
    _end_rate = random.choice([0.02, 0.03, 0.04, 0.05, 0.06])
    if _end_rate >= _ceiling:
        continue
    _analyst = random.uniform(-0.10, 0.60)
    _has_history = random.random() < 0.5
    if _has_history:
        _n = random.choice([4, 5, 6])
        _base_val = random.uniform(10.0, 500.0)
        _cagr_target = random.uniform(-0.30, 0.80)
        _series = [_base_val * ((1 + _cagr_target) ** (i / max(_n - 1, 1))) for i in range(_n)]
        _fcf_series = list(reversed(_series))  # most-recent-first, as yfinance orders it
    else:
        _fcf_series = None
    g, src, gov, raw = fve.estimate_growth(
        {}, fcf_series=_fcf_series, analyst_growth=_analyst,
        ceiling=_ceiling, end_rate=_end_rate, yahoo_estimate_status="ok_1y",
    )
    if src in ("analyst_1y", "analyst_1y_blend") and g < _end_rate - 1e-9:
        _violations.append((g, src, gov, _analyst, _ceiling, _end_rate, _has_history))

assert not _violations, _violations
print(f"[no_uphill_fade_invariant_holds] 500 random 'ok_1y' inputs (mixed with/without "
      f"history, mixed analyst sign/magnitude) -> growth_used never fell below end_rate "
      f"on the analyst_1y/analyst_1y_blend paths OK")

print("\nALL GROWTH 1Y-BLEND FIXTURES PASSED")
