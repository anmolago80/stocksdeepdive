"""
No-uphill-fade fix for the non-analyst growth paths (owner-directed,
1 Oct 2026, Commit 4 of instruction_dcf_unreliable_pool_step4_nightly.md,
acting on the growth/PE-forward task's own reported finding).

Fact (the growth/PE-forward task's own finding): the "analyst"(ok)/
"history"/"history_volatile"/"info"/"default" paths of estimate_growth()
carry no floor at end_rate - a genuine signal below end_rate on any of
them can resolve to a raw growth_rate below the market-cap-tiered end
rate. dcf_intrinsic_value()'s own stage-1 fade loop already refuses to
fade UPHILL in that case (`fade = growth_rate > end_rate` stays False,
so the path goes flat at growth_rate for the whole horizon - see that
loop's own comment, confirmed unchanged by this fix), but
meta["growth_end_rate_used"] kept reporting the ORIGINAL, higher tier
end rate regardless - misdescribing what the model actually did (the
live KNSL symptom: "2.5% for 5 yrs, then fades to 5.0% by yr 10" when
the path was actually flat at 2.5% the whole way).

Fix: for exactly these 5 sources, when growth_rate < end_rate,
dcf_intrinsic_value() itself lowers the REPORTED end_rate to
max(min(end_rate, growth_rate), perpetual_rate) and sets growth_governor
to "end rate lowered to base - no uphill fade". The two analyst_1y
paths (which already floor growth_rate AT end_rate, from the growth-1y-
blend task) and "manual" are explicitly excluded. No change to the
stage-1 fade math itself, DCF discount rates, tier tables, or any
scoring surface.

This file covers:
  - a 1%-history/6%-end fixture -> reported end_rate lowered to 1%,
    growth_governor names the rule, growth_path is flat at 1% for the
    whole horizon
  - a 12%-LTG(capped to 8% ceiling)/2%-end fixture -> UNCHANGED (growth
    already above end_rate, normal downhill fade, governor stays "Cap")
  - analyst_1y/analyst_1y_blend fixtures -> UNCHANGED even when
    (artificially, for the test) growth_rate is reported below end_rate -
    confirms the explicit source exclusion, not just "it never happens
    to fire for them"
  - "manual" growth override -> UNCHANGED (out of this task's stated
    scope)
  - invariant sweep: growth_used >= growth_end_rate_used now holds
    across 500 random (source, growth_rate, end_rate) combinations
    spanning every source, extending the growth-1y-blend task's own
    sweep (which covered only analyst_1y/analyst_1y_blend) to all of
    them

Run: python3 tests/test_no_uphill_fade_all_sources.py
"""
import os
import random
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve

_COMMON_DCF = dict(
    cashflow_df=None, manual_fcf=1.0, diluted_shares_override=1, discount_rate=0.10,
    perpetual_rate=0.02,
)


def _run_dcf(ticker, market_cap, currency, mocked_growth_return):
    """Calls the REAL dcf_intrinsic_value() end-to-end, with estimate_
    growth() itself mocked to return a controlled (growth_rate, source,
    governor, raw_rate) tuple - precisely isolates this fix's own logic
    (which reads gsrc/growth_rate/end_rate) from needing to hand-engineer
    a cashflow_df that happens to produce an exact CAGR, same reasoning
    tests/test_growth_never_zero.py's own FIXTURE 7/8 mock capm_engine.
    get_growth_estimates_5y() for the same purpose one layer up."""
    with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y",
                            return_value=(None, "no_coverage")), \
         mock.patch.object(fve, "estimate_growth", return_value=mocked_growth_return):
        return fve.dcf_intrinsic_value(
            ticker, info={"currentPrice": 10.0, "currency": currency, "marketCap": market_cap},
            currency=currency, **_COMMON_DCF,
        )


# ======================================================================
# CHECK 1: 1%-history / 6%-end (small-cap tier, marketCap=500M) -> the
# reported end_rate is lowered to 1%, governor names the rule, and the
# growth_path is flat at 1% for every single year (the stage-1 loop's
# own "fade never applies when g1 <= end_rate" behaviour, now correctly
# DESCRIBED by the lowered meta field too).
# ======================================================================
_iv1, _g1, _meta1 = _run_dcf(
    "HIST1PCT", 500_000_000, "USD", (0.01, "history", "History", 0.01))
assert _meta1["growth_source"] == "history", _meta1["growth_source"]
assert abs(_g1 - 0.01) < 1e-9, _g1
assert abs(_meta1["growth_end_rate_used"] - 0.01) < 1e-9, _meta1["growth_end_rate_used"]
assert _meta1["growth_governor"] == "end rate lowered to base - no uphill fade", _meta1["growth_governor"]
assert all(abs(g - 0.01) < 1e-9 for g in _meta1["growth_path"]), _meta1["growth_path"]
print(f"[one_pct_history_six_pct_end_lowers_to_one] growth=1%, tier end_rate=6% -> "
      f"growth_end_rate_used lowered to {_meta1['growth_end_rate_used']:.2%}, governor="
      f"{_meta1['growth_governor']!r}, growth_path flat at 1% for all "
      f"{len(_meta1['growth_path'])} years OK")

# ======================================================================
# CHECK 2: 12%-LTG capped to an 8%-ceiling mega-cap tier (end_rate=2%)
# -> UNCHANGED. growth_rate (8%, the capped/final figure) is already
# above end_rate (2%), so this fix's own condition (growth_rate <
# end_rate) never fires - normal downhill fade, governor stays "Cap"
# from estimate_growth()'s own cap logic, untouched by this task.
# ======================================================================
_iv2, _g2, _meta2 = _run_dcf(
    "LTG12CAP8", 300_000_000_000, "USD", (0.08, "analyst", "Cap", 0.12))
assert _meta2["growth_source"] == "analyst", _meta2["growth_source"]
assert abs(_g2 - 0.08) < 1e-9, _g2
assert abs(_meta2["growth_end_rate_used"] - 0.02) < 1e-9, _meta2["growth_end_rate_used"]
assert _meta2["growth_governor"] == "Cap", _meta2["growth_governor"]
# Normal downhill fade: year 10 should land near end_rate, not flat at 8%.
assert _meta2["growth_path"][0] > _meta2["growth_path"][-1] > _meta2["growth_end_rate_used"] - 1e-9
print(f"[twelve_pct_ltg_capped_eight_pct_unchanged] growth=8% (capped from 12%), "
      f"tier end_rate=2% -> growth_end_rate_used UNCHANGED at "
      f"{_meta2['growth_end_rate_used']:.2%}, governor={_meta2['growth_governor']!r} "
      "(untouched Cap logic), normal downhill fade OK")

# ======================================================================
# CHECK 3: analyst_1y / analyst_1y_blend sources are explicitly excluded
# from this fix - even when (artificially, for this test only) the
# mocked growth_rate is reported BELOW end_rate, the reported end_rate
# and governor stay UNCHANGED. Confirms the exclusion is a real source
# filter, not just "it happens to never fire for these two".
# ======================================================================
for _src, _gov in (("analyst_1y", "next-year consensus, floored at end rate"),
                    ("analyst_1y_blend", "next-year consensus blended with history (history capped at ceiling)")):
    _ivx, _gx, _metax = _run_dcf(
        f"EXCL_{_src}", 500_000_000, "USD", (0.01, _src, _gov, 0.01))
    assert abs(_metax["growth_end_rate_used"] - 0.06) < 1e-9, (_src, _metax["growth_end_rate_used"])
    assert _metax["growth_governor"] == _gov, (_src, _metax["growth_governor"])
print("[analyst_1y_paths_excluded] analyst_1y/analyst_1y_blend sources never trigger the "
      "end-rate-lowering rule (growth_end_rate_used and growth_governor both stay exactly "
      "as estimate_growth() reported them) even when artificially given a below-end_rate "
      "growth_rate for this test OK")

# ======================================================================
# CHECK 4: "manual" growth override is also excluded - out of this
# task's stated scope (analyst/history/history_volatile/info/default
# only). Uses the real growth_rate= override path (not a mocked
# estimate_growth(), since manual bypasses that function entirely).
# ======================================================================
with mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _ivm, _gm, _metam = fve.dcf_intrinsic_value(
        "MANUAL1PCT", info={"currentPrice": 10.0, "currency": "USD", "marketCap": 500_000_000},
        currency="USD", growth_rate=0.01, cashflow_df=None, manual_fcf=1.0,
        diluted_shares_override=1, discount_rate=0.10, perpetual_rate=0.02,
    )
assert _metam["growth_source"] == "manual", _metam["growth_source"]
assert abs(_gm - 0.01) < 1e-9, _gm
assert abs(_metam["growth_end_rate_used"] - 0.06) < 1e-9, _metam["growth_end_rate_used"]
print(f"[manual_override_excluded] a manual 1% growth override against a 6% tier end rate "
      f"-> growth_end_rate_used stays at the original {_metam['growth_end_rate_used']:.2%} "
      "(manual is out of this task's scope) OK")

# ======================================================================
# CHECK 5: invariant sweep - growth_used >= growth_end_rate_used now
# holds across every auto source, extending the growth-1y-blend task's
# own sweep (which only covered analyst_1y/analyst_1y_blend).
# ======================================================================
random.seed(20261001)
_sources = ["analyst", "history", "history_volatile", "info", "default",
            "analyst_1y", "analyst_1y_blend"]
_market_caps = [100_000_000, 500_000_000, 3_000_000_000, 20_000_000_000,
                80_000_000_000, 400_000_000_000]
_violations = []
for _ in range(500):
    _src = random.choice(_sources)
    _mcap = random.choice(_market_caps)
    _ccy = random.choice(["USD", "AUD"])
    _growth = random.uniform(0.0, 0.40)
    _governor = "History" if _src == "history" else "Info"
    if _src in ("analyst_1y", "analyst_1y_blend"):
        # These two sources already floor growth_rate AT end_rate inside
        # the REAL estimate_growth() (the previous growth-1y-blend task's
        # own fix) - this sweep mocks estimate_growth() directly, so it
        # must replicate that floor itself, otherwise it hands the mock a
        # (growth, end_rate) combination estimate_growth() could never
        # actually produce for these two sources, and wrongly blames
        # Commit 4 (which explicitly excludes them) for the "violation".
        _end_rate_for_floor = fve.growth_end_rate_for(
            {"marketCap": _mcap, "currency": _ccy}, _ccy)
        _growth = max(_growth, _end_rate_for_floor)
    _iv, _g, _meta = _run_dcf(
        "SWEEP", _mcap, _ccy, (_growth, _src, _governor, _growth))
    if _g < _meta["growth_end_rate_used"] - 1e-9:
        _violations.append((_src, _mcap, _ccy, _growth, _meta["growth_end_rate_used"]))

assert not _violations, _violations
print(f"[invariant_holds_on_every_source] 500 random (source, market-cap, currency, "
      f"growth) combinations across all {len(_sources)} sources -> growth_used never fell "
      "below growth_end_rate_used (the invariant now holds universally, not just for "
      "analyst_1y/analyst_1y_blend) OK")

print("\nALL NO-UPHILL-FADE (ALL SOURCES) FIXTURES PASSED")
