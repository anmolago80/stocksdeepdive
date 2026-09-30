"""
Push 2 Part 1 (owner-directed, 30 Sep 2026, from instruction_final_
tonight.md): capm_engine.py's market-cap size premium is now a
continuous log-linear interpolation between five FIXED anchors
(SIZE_PREMIUM_ANCHORS_USD - the exact same five values the old step
table used) instead of a step-table lookup. Live symptom this fixes:
OCL.AX (~US$0.9B, an established mid-sized industrial) got the SAME
harshest premium as a genuine micro-cap purely because it sat one side
of a hard US$2B cliff - a US$1.99B company and a US$2.01B company got a
full percentage point of discount-rate difference for a rounding error
in market cap.

Seven checks, per the instruction's own spec:
  1. Each of the five anchors reproduces today's exact step-table value.
  2. US$0.9B -> ~5.43% (+-0.02 percentage points).
  3. US$1.99B vs US$2.01B differ by < 0.01 (1 percentage point) - no cliff.
  4. US$250M -> 6.0% flat (below the bottom anchor).
  5. US$1T -> 2.0% flat (above the top anchor).
  6. AUD market cap is FX-converted to USD BEFORE the interpolation lookup.
  7. Missing/zero market cap -> 6.0% (bottom) + defaulted=True.
  Plus: MIN_DISCOUNT_RATE (7.5%) floor still binds for a mega-cap on a
  low risk-free rate (unaffected by the interpolation, same floor logic
  as before).

Run: python3 tests/test_size_premium_continuous.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capm_engine as ce

_FAKE_RF = {"USD": (0.050, "default"), "AUD": (0.053, "default")}


def _rf(ccy):
    return _FAKE_RF[ccy]


# ======================================================================
# CHECK 1: each of the five fixed anchors reproduces today's EXACT old
# step-table premium - the boundary-exactness guarantee that lets any
# company sitting exactly on an old threshold see no change at all.
# ======================================================================
_ANCHOR_EXPECTATIONS = [
    (200_000_000_000, 0.02, "mega-cap"),
    (50_000_000_000, 0.03, "large-cap"),
    (10_000_000_000, 0.04, "mid-cap"),
    (2_000_000_000, 0.05, "small-mid-cap"),
    (300_000_000, 0.06, "small-cap"),
]
with mock.patch.object(ce, "get_risk_free_rate", side_effect=_rf):
    for _cap, _expected_premium, _band_prefix in _ANCHOR_EXPECTATIONS:
        _premium = ce._interpolate_size_premium(_cap)
        assert abs(_premium - _expected_premium) < 1e-9, (_cap, _premium, _expected_premium)
        _label = ce._size_band_label(_cap)
        assert _label.startswith(_band_prefix), (_cap, _label)
print("[anchor_exactness] all five fixed anchors (US$200B/50B/10B/2B/300M) reproduce the exact "
      "old step-table premium (2%/3%/4%/5%/6%) through the new interpolation OK")


# ======================================================================
# CHECK 2: US$0.9B (the OCL.AX live symptom) -> ~5.43% (+-0.02pp), NOT
# the old flat 6% micro-cap premium it used to get purely for sitting
# under the old US$2B cliff.
# ======================================================================
_premium_09b = ce._interpolate_size_premium(900_000_000)
assert abs(_premium_09b - 0.0543) < 0.0002, _premium_09b
print(f"[ocl_09b_not_micro_cap] US$0.9B interpolates to {_premium_09b:.4%} (owner's own expected "
      "~5.43%, +-0.02pp) - NOT the old flat 6% micro-cap premium OCL.AX incorrectly got under the "
      "old US$2B cliff OK")


# ======================================================================
# CHECK 3: US$1.99B vs US$2.01B - the exact old cliff point - now differ
# by less than 0.01 (1 percentage point), confirming the cliff is gone.
# The old step table gave these a FULL percentage point apart (6% vs 5%).
# ======================================================================
_premium_199b = ce._interpolate_size_premium(1_990_000_000)
_premium_201b = ce._interpolate_size_premium(2_010_000_000)
assert abs(_premium_199b - _premium_201b) < 0.01, (_premium_199b, _premium_201b)
print(f"[no_cliff_at_old_2b_threshold] US$1.99B ({_premium_199b:.4%}) vs US$2.01B "
      f"({_premium_201b:.4%}) differ by {abs(_premium_199b - _premium_201b):.4%} - well under the "
      "1pp threshold, confirming the old cliff (a full percentage point apart) is gone OK")


# ======================================================================
# CHECK 4: US$250M (below the bottom US$300M anchor) -> flat 6.0%, same
# as the old step table's bottom tier.
# ======================================================================
_premium_250m = ce._interpolate_size_premium(250_000_000)
assert abs(_premium_250m - 0.06) < 1e-9, _premium_250m
print("[below_bottom_anchor_flat] US$250M (below the US$300M bottom anchor) -> flat 6.0%, same "
      "as the old step table's bottom (micro-cap) tier OK")


# ======================================================================
# CHECK 5: US$1T (above the top US$200B anchor) -> flat 2.0%, same as
# the old step table's top tier.
# ======================================================================
_premium_1t = ce._interpolate_size_premium(1_000_000_000_000)
assert abs(_premium_1t - 0.02) < 1e-9, _premium_1t
print("[above_top_anchor_flat] US$1T (above the US$200B top anchor) -> flat 2.0%, same as the "
      "old step table's top (mega-cap) tier OK")


# ======================================================================
# CHECK 6: AUD market cap is FX-converted to USD BEFORE the interpolation
# lookup - AU$1.5B * FX(0.65) = US$975M, interpolates the same as a
# genuinely-US$975M company would (NOT the raw AU$1.5B figure).
# ======================================================================
with mock.patch.object(ce, "get_au_risk_free_rate_live", return_value=(0.053, "default")):
    _rate_aud, _meta_aud = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 1_500_000_000, "currency": "AUD"}, "AUD")
_expected_cap_usd = 1_500_000_000 * ce._DISCOUNT_TIER_FX_TO_USD_APPROX["AUD"]
assert abs(_meta_aud["market_cap_usd"] - _expected_cap_usd) < 1e-6, _meta_aud["market_cap_usd"]
_expected_premium_aud = ce._interpolate_size_premium(_expected_cap_usd)
assert abs(_meta_aud["premium_used"] - _expected_premium_aud) < 1e-9, _meta_aud["premium_used"]
print(f"[aud_converted_before_lookup] AU$1.5B FX-converts to US${_expected_cap_usd / 1e9:.3f}B "
      f"BEFORE the interpolation lookup (premium {_meta_aud['premium_used']:.4%}) - not looked up "
      "against the raw, unconverted AUD figure OK")


# ======================================================================
# CHECK 7: missing/zero market cap -> 6.0% (bottom premium) + defaulted.
# ======================================================================
with mock.patch.object(ce, "get_risk_free_rate", side_effect=_rf):
    _rate_missing, _meta_missing = ce.resolve_discount_rate_by_market_cap(
        {"currency": "USD"}, "USD")
assert _meta_missing["market_cap_missing"] is True
assert _meta_missing["defaulted"] is True
assert abs(_meta_missing["premium_used"] - 0.06) < 1e-9, _meta_missing["premium_used"]
print("[missing_market_cap_bottom_premium] a missing market cap resolves to the bottom (6.0%) "
      "premium and is flagged market_cap_missing=True/defaulted=True for the caller to disclose OK")


# ======================================================================
# BONUS: MIN_DISCOUNT_RATE (7.5%) floor still binds for a mega-cap on a
# low risk-free rate - the floor logic is unaffected by the switch from
# a step-table lookup to a continuous interpolation.
# ======================================================================
with mock.patch.object(ce, "get_risk_free_rate", return_value=(0.03, "live")):
    _rate_floored, _meta_floored = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 3_000_000_000_000, "currency": "USD"}, "USD")
assert _rate_floored == ce.MIN_DISCOUNT_RATE, _rate_floored
assert _meta_floored["discount_floored"] is True
print(f"[floor_still_binds] a mega-cap (2.0% premium) on a low 3.0% risk-free rate (raw 5.0%) "
      f"still floors at MIN_DISCOUNT_RATE ({ce.MIN_DISCOUNT_RATE:.1%}) - the floor logic is "
      "unaffected by the step-table-to-interpolation switch OK")


print("\nALL SIZE-PREMIUM-CONTINUOUS FIXTURES PASSED")
