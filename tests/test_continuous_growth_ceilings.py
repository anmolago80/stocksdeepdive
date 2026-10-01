"""
Continuous-in-market-cap growth ceiling and end rate (owner-directed,
1 Oct 2026 23:09 AEST, "all you are doing is interpolating").

Fact: fcf_valuation_engine.growth_ceiling_for()/growth_end_rate_for()
were step functions - a company at $9.9B got a 16% ceiling, at $10.1B
12%; at $199B 12%, at $201B 8% - with nothing about the business
changing across that boundary. Tuesday's size-premium fix (capm_engine.
_interpolate_size_premium(), log-linear through SIZE_PREMIUM_ANCHORS_USD)
solved the identical problem for the discount rate; this applies the
same mechanism here.

Fix: GROWTH_CEILING_ANCHORS_USD / GROWTH_END_RATE_ANCHORS_USD, anchors
at the GEOMETRIC MIDPOINT of each old tier (owner choice: "midpoints,
not tier floors") - the typical company already sitting mid-tier keeps
today's value exactly; a company near an old boundary now sees a smooth
slope instead of a cliff. _log_interpolate() is a local copy of capm_
engine._interpolate_size_premium()'s own log-linear-in-log10(market_cap)
approach (not a shared import - see fcf_valuation_engine.py's own
comment for why).

This file covers the instruction's own test list:
  - anchor points return exactly the anchor values
  - the owner's own expected sweep, within 0.1 point
  - monotonic non-increasing in market cap for both functions over a
    dense log sweep $100M-$2T
  - flat beyond both ends
  - AUD/other-currency conversion identical to today's path (same
    FX_TO_USD_APPROX bucketing, unchanged)
  - missing/zero market cap -> unchanged fallbacks (GROWTH_CEIL,
    DEFAULT_PERPETUAL_RATE)
  - growth_used <= growth_ceiling_used and growth_used >=
    growth_end_rate_used still hold across the existing fixture sweep
  - rounded to 4 decimals

Run: python3 tests/test_continuous_growth_ceilings.py
"""
import math
import os
import random
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve

# ======================================================================
# CHECK 1: anchor points return EXACTLY the anchor values (boundary-
# exactness, same guarantee capm_engine._interpolate_size_premium() has).
# ======================================================================
for _cap, _expected in fve.GROWTH_CEILING_ANCHORS_USD:
    _got = fve.growth_ceiling_for({"marketCap": _cap, "currency": "USD"})
    assert abs(_got - _expected) < 1e-9, (_cap, _got, _expected)
for _cap, _expected in fve.GROWTH_END_RATE_ANCHORS_USD:
    _got = fve.growth_end_rate_for({"marketCap": _cap, "currency": "USD"})
    assert abs(_got - _expected) < 1e-9, (_cap, _got, _expected)
print("[anchor_points_exact] every GROWTH_CEILING_ANCHORS_USD / GROWTH_END_RATE_ANCHORS_USD "
      "anchor's own market cap returns that anchor's exact value OK")


# ======================================================================
# CHECK 2: the owner's own expected sweep (log-linear through the
# anchors above), within 0.1 point. Any mismatch > 0.1pp is reported (an
# assertion failure) rather than silently accepted - "the figures are
# mine, the formula is the authority."
# ======================================================================
_CEILING_SWEEP = [
    (300_000_000,     20.0), (775_000_000,      20.0), (1_900_000_000,   18.0),
    (4_470_000_000,   16.0), (9_000_000_000,    14.8), (20_000_000_000,  13.4),
    (44_700_000_000,  12.0), (50_000_000_000,   11.7), (100_000_000_000,  9.9),
    (190_000_000_000,  8.1), (200_000_000_000,   8.0), (300_000_000_000,  8.0),
]
for _cap, _expected_pct in _CEILING_SWEEP:
    _got_pct = fve.growth_ceiling_for({"marketCap": _cap, "currency": "USD"}) * 100
    assert abs(_got_pct - _expected_pct) <= 0.1, (_cap, _got_pct, _expected_pct)
print(f"[ceiling_sweep_matches_owner] all {len(_CEILING_SWEEP)} owner-supplied ceiling points "
      "match the interpolated formula within 0.1pp OK")

_END_RATE_SWEEP = [
    (775_000_000,      6.0), (500_000_000,       6.0), (1_900_000_000,   5.5),
    (4_470_000_000,    5.0), (9_000_000_000,     4.6), (22_400_000_000,  4.0),
    (50_000_000_000,   3.5), (100_000_000_000,   3.0), (190_000_000_000, 2.1),
    (200_000_000_000,  2.0),
]
for _cap, _expected_pct in _END_RATE_SWEEP:
    _got_pct = fve.growth_end_rate_for({"marketCap": _cap, "currency": "USD"}) * 100
    assert abs(_got_pct - _expected_pct) <= 0.1, (_cap, _got_pct, _expected_pct)
print(f"[end_rate_sweep_matches_owner] all {len(_END_RATE_SWEEP)} owner-supplied end-rate points "
      "match the interpolated formula within 0.1pp OK")


# ======================================================================
# CHECK 3: monotonic NON-INCREASING in market cap, over a dense log
# sweep $100M-$2T, for both functions.
# ======================================================================
_dense_caps = [10 ** (8 + i * 0.02) for i in range(201)]  # $100M .. ~$2.5T, fine-grained
_ceiling_series = [fve.growth_ceiling_for({"marketCap": c, "currency": "USD"}) for c in _dense_caps]
_end_rate_series = [fve.growth_end_rate_for({"marketCap": c, "currency": "USD"}) for c in _dense_caps]
assert all(_ceiling_series[i] >= _ceiling_series[i + 1] - 1e-12 for i in range(len(_ceiling_series) - 1)), \
    "growth_ceiling_for() must be monotonic non-increasing in market cap"
assert all(_end_rate_series[i] >= _end_rate_series[i + 1] - 1e-12 for i in range(len(_end_rate_series) - 1)), \
    "growth_end_rate_for() must be monotonic non-increasing in market cap"
print(f"[monotonic_non_increasing] both functions are monotonic non-increasing across a dense "
      f"{len(_dense_caps)}-point log sweep from $100M to ~$2.5T OK")


# ======================================================================
# CHECK 4: flat beyond both ends.
# ======================================================================
assert fve.growth_ceiling_for({"marketCap": 500_000_000_000_000, "currency": "USD"}) == 0.08
assert fve.growth_ceiling_for({"marketCap": 1_000, "currency": "USD"}) == 0.20
assert fve.growth_end_rate_for({"marketCap": 500_000_000_000_000, "currency": "USD"}) == 0.02
assert fve.growth_end_rate_for({"marketCap": 1_000, "currency": "USD"}) == 0.06
print("[flat_beyond_ends] a $500T market cap reads the top anchor's value, a $1,000 market cap "
      "reads the bottom anchor's value, for both functions OK")


# ======================================================================
# CHECK 5: AUD/other-currency conversion identical to today's path -
# same FX_TO_USD_APPROX bucketing, unchanged by this fix. An AUD company
# whose USD-equivalent cap lands exactly on a USD anchor gets that
# anchor's exact value.
# ======================================================================
_aud_cap_at_usd_anchor = 4_470_000_000 / fve.FX_TO_USD_APPROX["AUD"]  # -> exactly $4.47B USD-equiv
_got_aud_ceiling = fve.growth_ceiling_for({"marketCap": _aud_cap_at_usd_anchor, "currency": "AUD"}, "AUD")
assert abs(_got_aud_ceiling - 0.16) < 1e-9, _got_aud_ceiling
_got_aud_end_rate = fve.growth_end_rate_for({"marketCap": _aud_cap_at_usd_anchor, "currency": "AUD"}, "AUD")
assert abs(_got_aud_end_rate - 0.05) < 1e-9, _got_aud_end_rate
print("[aud_fx_bucketing_unchanged] an AUD market cap converted via the unchanged FX_TO_USD_"
      "APPROX snapshot to exactly $4.47B USD-equivalent lands exactly on that anchor for both "
      "functions OK")


# ======================================================================
# CHECK 6: missing/zero market cap -> unchanged fallbacks.
# ======================================================================
assert fve.growth_ceiling_for({}) == fve.GROWTH_CEIL
assert fve.growth_ceiling_for({"marketCap": 0, "currency": "USD"}) == fve.GROWTH_CEIL
assert fve.growth_ceiling_for(None) == fve.GROWTH_CEIL
assert fve.growth_end_rate_for({}) == fve.DEFAULT_PERPETUAL_RATE
assert fve.growth_end_rate_for({"marketCap": -5, "currency": "USD"}) == fve.DEFAULT_PERPETUAL_RATE
assert fve.growth_end_rate_for(None) == fve.DEFAULT_PERPETUAL_RATE
print("[missing_market_cap_fallbacks] a missing, zero, negative or absent-info market cap falls "
      "back to the original flat GROWTH_CEIL / DEFAULT_PERPETUAL_RATE, unchanged by this fix OK")


# ======================================================================
# CHECK 7: rounded to 4 decimals.
# ======================================================================
_odd_cap = 7_123_456_789
_ceiling_val = fve.growth_ceiling_for({"marketCap": _odd_cap, "currency": "USD"})
_end_rate_val = fve.growth_end_rate_for({"marketCap": _odd_cap, "currency": "USD"})
assert round(_ceiling_val, 4) == _ceiling_val, _ceiling_val
assert round(_end_rate_val, 4) == _end_rate_val, _end_rate_val
print(f"[rounded_to_4_decimals] an off-anchor market cap (${_odd_cap:,}) returns ceiling="
      f"{_ceiling_val} and end_rate={_end_rate_val}, both already rounded to 4 decimals OK")


# ======================================================================
# CHECK 8: growth_used <= growth_ceiling_used and growth_used >=
# growth_end_rate_used still hold across a random sweep of (source,
# market cap, currency) combinations, extending the no-uphill-fade
# task's own invariant sweep to the now-continuous ceiling/end rate.
# ======================================================================
_COMMON_DCF = dict(
    cashflow_df=None, manual_fcf=1.0, diluted_shares_override=1, discount_rate=0.10,
    perpetual_rate=0.02,
)


def _run_dcf(ticker, market_cap, currency, mocked_growth_return):
    with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y",
                            return_value=(None, "no_coverage")), \
         mock.patch.object(fve, "estimate_growth", return_value=mocked_growth_return):
        return fve.dcf_intrinsic_value(
            ticker, info={"currentPrice": 10.0, "currency": currency, "marketCap": market_cap},
            currency=currency, **_COMMON_DCF,
        )


random.seed(20261001)
_sources = ["analyst", "history", "history_volatile", "info", "default"]
_market_caps = [100_000_000, 500_000_000, 1_900_000_000, 3_000_000_000, 9_000_000_000,
                20_000_000_000, 44_700_000_000, 80_000_000_000, 190_000_000_000, 400_000_000_000]
_violations = []
for _ in range(500):
    _src = random.choice(_sources)
    _mcap = random.choice(_market_caps)
    _ccy = random.choice(["USD", "AUD"])
    _governor = "History" if _src == "history" else "Info"
    # _run_dcf() mocks estimate_growth() directly, bypassing its own
    # real ceiling clamp - replicate that clamp here (same reasoning as
    # the no-uphill-fade task's own invariant sweep for analyst_1y/
    # analyst_1y_blend) so this sweep never hands the mock a growth
    # value the real function could never actually produce.
    _info_for_ceiling = {"marketCap": _mcap, "currency": _ccy}
    _ceiling_for_draw = fve.growth_ceiling_for(_info_for_ceiling, _ccy)
    _growth = min(random.uniform(0.0, 0.40), _ceiling_for_draw)
    _iv, _g, _meta = _run_dcf("SWEEP", _mcap, _ccy, (_growth, _src, _governor, _growth))
    _ceiling_used = _meta.get("growth_ceiling_used")
    _end_rate_used = _meta["growth_end_rate_used"]
    if _ceiling_used is not None and _g > _ceiling_used + 1e-9:
        _violations.append(("ceiling", _src, _mcap, _ccy, _growth, _g, _ceiling_used))
    if _g < _end_rate_used - 1e-9:
        _violations.append(("end_rate", _src, _mcap, _ccy, _growth, _g, _end_rate_used))

assert not _violations, _violations
print(f"[invariant_holds_with_continuous_tables] 500 random (source, market-cap, currency, "
      f"growth) combinations -> growth_used never exceeded growth_ceiling_used nor fell below "
      "growth_end_rate_used, now that both are continuous OK")


print("\nALL CONTINUOUS GROWTH CEILING/END RATE FIXTURES PASSED")
