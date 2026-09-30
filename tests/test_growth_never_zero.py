"""
Growth-never-zero rewrite (owner-directed, 30 Sep 2026). Root cause (see
the task's own instruction file, section "Why"): Yahoo switched its
growth_estimates row label from "+5y"/"5y" to "LTG" - every nightly scan
logged "growth source - Yahoo 5y 0" and every ticker fell through to
estimate_growth()'s OLD three ways of landing on exactly 0%: a negative
earningsGrowth clamped to 0 with revenueGrowth never tried; a 2-point FCF
history CAGR of 0.0 by construction; a negative Yahoo estimate floored to
0. None of these set the `defaulted` flag, so a real 0% growth DCF looked
identical to a healthy one everywhere downstream.

This file covers the NEW estimate_growth() priority order end to end -
see fcf_valuation_engine.estimate_growth()'s own docstring for the full
list. Every fixture below asserts estimate_growth()'s result is a real,
non-degenerate rate; the closing property test asserts it unconditionally
over 500 random combinations.

Cap values used throughout (all fcf_valuation_engine.py constants):
  REPORTED_GROWTH_CAP_FRACTION = 0.5   (max(end_rate, ceiling * 0.5))
  HISTORY_VOLATILE_CAP_FRACTION = 0.5  (max(end_rate, ceiling * 0.5))
  MIN_HISTORY_POINTS_FOR_TREND = 4     (fewer points = "no history")

KNOWN, EXPLICITLY-FLAGGED SPEC SELF-CONTRADICTION (see the reported-
growth-micro-cap fixture below): the instruction file's own section 1.5
says a reported-40%-micro-cap fixture must be "capped 10% (ceiling 20% x
0.5)" AND, in the same sentence, that the TOYO-shaped fixture from
b958fb0 "must produce a LOWER IV than the current 8%-cap code does or
equal, never higher". A 10% cap is mathematically HIGHER than the old
flat 8% cap, so it produces a HIGHER growth rate and a HIGHER IV, not a
lower or equal one - these two clauses cannot both hold. This file
implements the cap value literally as specified (10%, matching "capped
10%", stated twice, consistently) and asserts that value; it does NOT
attempt to silently pick some other unstated number to satisfy the IV-
comparison clause instead. See tests/test_dcf_outlier_guards.py's own
TOYO fixture for the (now higher, not lower) resulting IV.

Run: python3 tests/test_growth_never_zero.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve

MICRO_CEILING = 0.20   # micro-cap tier ceiling
SMALL_CEILING = 0.16
MID_CEILING = 0.12
MEGA_CEILING = 0.08


# ======================================================================
# FIXTURE 1: negative Yahoo estimate -> falls through to history (NOT 0%)
# ======================================================================
_clean_series = [130.0, 120.0, 110.0, 100.0]  # most-recent-first, growing
_g1, _src1, _gov1, _raw1 = fve.estimate_growth(
    {}, fcf_series=_clean_series, analyst_growth=-0.03, ceiling=MID_CEILING, end_rate=0.04,
)
assert _g1 > 0, _g1
assert _src1 == "history", _src1
print(f"[negative_yahoo_falls_to_history] analyst_growth=-3% (a real, non-positive Yahoo "
      f"estimate) -> falls through to clean history -> {_g1:.4f} ({_src1}), never 0% OK")


# ======================================================================
# FIXTURE 2: negative earningsGrowth, positive revenueGrowth -> revenue-
# Growth used (revenueGrowth is tried FIRST in the priority-3 loop).
# ======================================================================
_g2, _src2, _gov2, _raw2 = fve.estimate_growth(
    {"earningsGrowth": -0.15, "revenueGrowth": 0.06}, fcf_series=None,
    analyst_growth=None, ceiling=MID_CEILING, end_rate=0.04,
)
assert _src2 == "info", _src2
assert abs(_raw2 - 0.06) < 1e-9, _raw2  # revenueGrowth's OWN value, not earningsGrowth's
assert _g2 > 0, _g2
print(f"[negative_earnings_positive_revenue_uses_revenue] earningsGrowth=-15% (negative, "
      f"never used), revenueGrowth=6% (tried first, positive) -> raw={_raw2:.2f}, "
      f"resolved={_g2:.4f} OK")


# ======================================================================
# FIXTURE 3: 2-point and 3-point history -> treated as "no history"
# (MIN_HISTORY_POINTS_FOR_TREND=4), falls through past the history branch
# entirely (no reported/analyst signal either here -> lands on default).
# ======================================================================
for _n, _series in ((2, [120.0, 100.0]), (3, [130.0, 115.0, 100.0])):
    _g3, _src3, _gov3, _raw3 = fve.estimate_growth(
        {}, fcf_series=_series, analyst_growth=None, ceiling=MID_CEILING, end_rate=0.04,
    )
    assert _src3 == "default", (_n, _src3)
    assert abs(_g3 - 0.04) < 1e-9, (_n, _g3)
    print(f"[history_{_n}pt_treated_as_no_history] {_n}-point series ({_series}) has fewer than "
          f"MIN_HISTORY_POINTS_FOR_TREND={fve.MIN_HISTORY_POINTS_FOR_TREND} points -> skipped, "
          f"falls to end_rate default -> {_g3:.4f} ({_src3}) OK")


# ======================================================================
# FIXTURE 4: 4-point CLEAN history -> "history", plain tier ceiling
# (uncapped since the raw CAGR here is well under the tier ceiling).
# ======================================================================
_g4, _src4, _gov4, _raw4 = fve.estimate_growth(
    {}, fcf_series=_clean_series, analyst_growth=None, ceiling=MID_CEILING, end_rate=0.04,
)
_cv4 = fve._coeff_of_variation(_clean_series)
assert _cv4 <= 0.60, _cv4
assert _src4 == "history" and _gov4 == "History", (_src4, _gov4)
assert _g4 > 0, _g4
print(f"[history_4pt_clean] {_clean_series} (CV={_cv4:.3f} <= 0.60) -> {_g4:.4f} "
      f"({_src4}/{_gov4}, plain tier ceiling, uncapped) OK")


# ======================================================================
# FIXTURE 5: 4-point VOLATILE history -> "history_volatile", capped at
# max(end_rate, ceiling * HISTORY_VOLATILE_CAP_FRACTION) = max(0.04, 0.06)
# = 6% for a 12% mid-cap ceiling.
# ======================================================================
_volatile_series = [150.0, 30.0, 140.0, 20.0]  # most-recent-first
_cv5 = fve._coeff_of_variation(_volatile_series)
assert _cv5 > 0.60, _cv5
_raw_g5 = fve.growth_from_history(_volatile_series)
assert _raw_g5 > 0, _raw_g5
_g5, _src5, _gov5, _raw5 = fve.estimate_growth(
    {}, fcf_series=_volatile_series, analyst_growth=None, ceiling=MID_CEILING, end_rate=0.04,
)
_expected_cap5 = max(0.04, MID_CEILING * fve.HISTORY_VOLATILE_CAP_FRACTION)
assert _src5 == "history_volatile", _src5
assert abs(_g5 - min(_raw_g5, _expected_cap5)) < 1e-9, (_g5, _expected_cap5, _raw_g5)
assert abs(_raw5 - _raw_g5) < 1e-9, (_raw5, _raw_g5)
print(f"[history_4pt_volatile_capped] {_volatile_series} (CV={_cv5:.3f} > 0.60, raw CAGR "
      f"{_raw_g5:.4f}) -> capped at max(end_rate=4%, 12%*0.5=6%)={_expected_cap5:.2%} -> "
      f"{_g5:.4f} ({_src5}) OK")


# ======================================================================
# FIXTURE 6: reported 40% micro-cap -> capped at 10% (ceiling 20% * 0.5),
# NOT the old flat 8%. See this file's own module docstring for the
# explicitly-flagged spec self-contradiction this fixture's own expected
# value exposes (a 10% cap is HIGHER than the old 8%, not lower).
# ======================================================================
_g6, _src6, _gov6, _raw6 = fve.estimate_growth(
    {"earningsGrowth": 0.40}, fcf_series=None, analyst_growth=None,
    ceiling=MICRO_CEILING, end_rate=0.025,
)
_expected_cap6 = max(0.025, MICRO_CEILING * fve.REPORTED_GROWTH_CAP_FRACTION)
assert abs(_expected_cap6 - 0.10) < 1e-9, _expected_cap6
assert abs(_g6 - 0.10) < 1e-9, _g6
assert _src6 == "info" and _gov6 == "Cap", (_src6, _gov6)
assert abs(_raw6 - 0.40) < 1e-9, _raw6
print(f"[reported_growth_micro_cap_capped_10pct] earningsGrowth=40% against a 20% micro-cap "
      f"tier ceiling -> capped at max(end_rate=2.5%, 20%*0.5=10%)=10% (NOT the old flat 8%) -> "
      f"{_g6:.2f} ({_src6}/{_gov6}), raw={_raw6:.2f} - see module docstring for the flagged "
      f"spec contradiction this number exposes OK")


# ======================================================================
# FIXTURE 7: nothing usable anywhere -> end_rate itself, defaulted=True
# (checked at the dcf_intrinsic_value() level, where the defaulted flag
# actually lives - estimate_growth() itself has no such flag, only a
# source/governor pair the caller uses to set it).
# ======================================================================
_g7, _src7, _gov7, _raw7 = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=None, ceiling=MID_CEILING, end_rate=0.06,
)
assert _g7 == 0.06, _g7
assert _src7 == "default" and _gov7 == "Default", (_src7, _gov7)
assert abs(_raw7 - 0.06) < 1e-9, _raw7

from unittest import mock

with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _iv7, _g7_full, _meta7 = fve.dcf_intrinsic_value(
        "NOTHING", info={"currentPrice": 10.0, "currency": "USD", "marketCap": 500_000_000},
        cashflow_df=None, currency="USD",
        discount_rate=0.10, perpetual_rate=0.02, manual_fcf=1.0, diluted_shares_override=1,
    )
assert _meta7["growth_source"] == "default", _meta7["growth_source"]
assert _meta7.get("growth_default") or _meta7.get("defaulted"), _meta7
print(f"[nothing_usable_defaults_to_end_rate] no analyst/history/reported signal anywhere -> "
      f"resolves to end_rate itself ({_g7:.4f}), source='default', and the full DCF pipeline's "
      f"defaulted/growth_default flag fires (never a silent, unflagged 0%) OK")


# ======================================================================
# FIXTURE 8: AUD mega-cap with no signal -> 2.5% (AUD's own perpetual
# rate), NOT 2.0% (the mega tier's OWN end rate, unfloored) - the max()
# floor against the currency's own perpetual rate must still apply on
# the "nothing usable" path, same as the growth-path fade target.
# ======================================================================
_end_rate_aud_alone = fve.growth_end_rate_for(
    {"marketCap": 400_000_000_000, "currency": "AUD"}, "AUD")
assert abs(_end_rate_aud_alone - 0.02) < 1e-9, _end_rate_aud_alone  # mega tier's own end rate
_floored_aud = max(_end_rate_aud_alone, 0.025)  # AUD's own perpetual rate
assert abs(_floored_aud - 0.025) < 1e-9, _floored_aud

with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _iv8, _g8, _meta8 = fve.dcf_intrinsic_value(
        "AUDMEGA", info={"currentPrice": 10.0, "currency": "AUD", "marketCap": 400_000_000_000},
        cashflow_df=None, currency="AUD",
        discount_rate=0.10, manual_fcf=1.0, diluted_shares_override=1,
    )
assert _meta8["growth_source"] == "default", _meta8["growth_source"]
assert abs(_meta8["growth_end_rate_used"] - 0.025) < 1e-9, _meta8["growth_end_rate_used"]
assert abs(_g8 - 0.025) < 1e-9, _g8
print(f"[aud_mega_no_signal_floors_to_2_5pct] AUD mega-cap, no signal anywhere: "
      f"growth_end_rate_for() alone = {_end_rate_aud_alone:.1%} (mega tier), floored by "
      f"max(tier_end_rate, AUD perpetual 2.5%) to {_floored_aud:.1%} -> resolved growth "
      f"{_g8:.4f} (2.5%, not 2.0%) OK")


# ======================================================================
# PROPERTY TEST: 500 random info/fcf_series/analyst_growth/ceiling/
# end_rate combinations - estimate_growth()'s result is ALWAYS > 0.
# Covers the full input space: negative/positive/None analyst estimates,
# short/long/clean/volatile/all-negative fcf series, negative/positive/
# missing reported growth fields, varying ceilings and end rates.
# ======================================================================
_rng = random.Random(20260930)
_property_failures = []
for _i in range(500):
    _analyst = _rng.choice([None, _rng.uniform(-0.5, 0.5)])
    _n_pts = _rng.choice([0, 1, 2, 3, 4, 5, 6])
    if _n_pts == 0:
        _series = None
    else:
        _series = [_rng.uniform(-500, 500) for _ in range(_n_pts)]
    _earnings = _rng.choice([None, _rng.uniform(-0.5, 0.5)])
    _revenue = _rng.choice([None, _rng.uniform(-0.5, 0.5)])
    _info = {}
    if _earnings is not None:
        _info["earningsGrowth"] = _earnings
    if _revenue is not None:
        _info["revenueGrowth"] = _revenue
    _ceiling = _rng.choice([0.08, 0.12, 0.16, 0.20])
    _end_rate = _rng.choice([0.02, 0.025, 0.03, 0.04, 0.05, 0.06])
    _g, _src, _gov, _raw = fve.estimate_growth(
        _info, fcf_series=_series, analyst_growth=_analyst,
        ceiling=_ceiling, end_rate=_end_rate,
    )
    if not (_g > 0):
        _property_failures.append((_i, _info, _series, _analyst, _ceiling, _end_rate, _g, _src))

assert not _property_failures, _property_failures
print(f"[property_test_500_combinations_always_positive] 500 random info/fcf_series/"
      f"analyst_growth/ceiling/end_rate combinations - estimate_growth() result is > 0 in "
      f"every single case (0 failures) OK")


print("\nALL GROWTH-NEVER-ZERO FIXTURES PASSED")
