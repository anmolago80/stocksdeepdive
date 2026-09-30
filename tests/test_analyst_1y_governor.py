"""
Step 1d (owner-directed, 30 Sep 2026, from instruction_step1d_step4_
rewind.md): govern the next-year analyst tier.

Context: the Dow 30 rescan at 09:46 UTC (after Step 1c's LTG-to-1y
fallback shipped) logged "Yahoo coverage - Yahoo LTG 0, Yahoo 1y 28,
Yahoo none 2" and "growth source - Yahoo 5y 10, cap 18, other 2". Yahoo's
5-year figure is absent for essentially every Dow name; the +1y/0y
fallback now supplies most of estimate_growth()'s priority-1 hits. A
next-year consensus is a ONE-year figure driving a FIVE-year stage-1
rate - already contained by the market-cap-tiered ceiling, but a small/
mid-cap's recovery-year +45% estimate would still ride that full ceiling,
the same false-positive shape the reported-growth cap (priority 3)
already exists to close, just from a better (still analyst-sourced)
source.

fcf_valuation_engine.estimate_growth() now takes a new
`yahoo_estimate_status` parameter. When it's "ok_1y" (capm_engine's
next-year/current-year fallback, not a genuine LTG figure):
  - capped at max(end_rate, ceiling * REPORTED_GROWTH_CAP_FRACTION) -
    the SAME fractional cap the reported-growth path already uses -
    UNLESS a clean historical FCF CAGR (>= MIN_HISTORY_POINTS_FOR_TREND
    points, coefficient of variation <= 0.60) is itself at or above that
    cap, in which case the cap loosens to min(analyst_1y, history_cagr,
    ceiling) - history corroborating a higher rate.
  - source "analyst_1y" (distinct from plain "analyst"); governor
    "Cap1y" whenever the raw 1y figure was actually reduced (by either
    mechanism above), "Yahoo" when it passed through untouched.
  - a genuine "ok" status (real LTG/+5y figure) is completely UNCHANGED
    - still the plain tier-ceiling clamp only, no fractional cap.

NOTE on this file's own fixture numbers: instruction_step1d_step4_
rewind.md's own worked example for the "no corroboration" case reads
"mega-cap 1y 12% -> 8% (ceiling binds first)". Computed directly against
the formula above (mega ceiling=8%, end_rate=2%, cap_1y=max(2%,8%*0.5)=
4%), that arithmetic gives 4%, not 8% - a mega-cap's own fractional cap
is 4%, and there is no way to reach 8% from a 12% input under this
formula without ALSO exceeding the plain 8% ceiling first, which would
make the fractional 4% cap moot only if it were LOOSER than the ceiling
(it never is: cap_1y <= ceiling for every tier under today's constants,
since REPORTED_GROWTH_CAP_FRACTION=0.5<1 and end_rate << ceiling
everywhere). The task's OTHER three worked examples - small-cap 45%/3pt
->10%(Cap1y), 45%/4pt-clean->18%(unchanged upper bound), 45%/4pt-6%->10%
(Cap1y) - all match this formula exactly (small-cap cap_1y=max(6%,20%*
0.5)=10%, confirmed against three independent numeric checks). Given 3/4
worked examples validate the formula precisely and the 4th is
arithmetically unreachable from the formula as written, this file uses
the CORRECT computed value (4%, not 8%) for the "no corroboration,
mega-cap" fixture instead of reproducing the apparently-erroneous
example verbatim - flagged in the Step 1d report for owner review, per
this session's "do not guess, verify" standard (see also the 45%/4pt-
clean fixture below, which uses the real computed CAGR - 15.7%, not a
round "18%" - verified programmatically against growth_from_history()
itself rather than hand-derived, for the same reason).

Run: python3 tests/test_analyst_1y_governor.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve
import nightly_scan as ns

MEGA_CEILING, MEGA_END_RATE = 0.08, 0.02
SMALL_CEILING, SMALL_END_RATE = 0.20, 0.06
SMALL_CAP_1Y = max(SMALL_END_RATE, SMALL_CEILING * fve.REPORTED_GROWTH_CAP_FRACTION)  # 0.10
MEGA_CAP_1Y = max(MEGA_END_RATE, MEGA_CEILING * fve.REPORTED_GROWTH_CAP_FRACTION)     # 0.04
assert abs(SMALL_CAP_1Y - 0.10) < 1e-9 and abs(MEGA_CAP_1Y - 0.04) < 1e-9


# ======================================================================
# CHECK 1: mega-cap, no history, 1y=12% -> capped at the fractional cap
# (4%, not the raw 1y figure and not the plain 8% ceiling) - see this
# file's own module docstring for why 4% (not the task description's
# "8%") is the arithmetically correct answer here.
# ======================================================================
g1, src1, gov1, raw1 = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=0.12, ceiling=MEGA_CEILING,
    end_rate=MEGA_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g1 - MEGA_CAP_1Y) < 1e-9, g1
assert src1 == "analyst_1y", src1
assert gov1 == "Cap1y", gov1
assert abs(raw1 - 0.12) < 1e-9, raw1
print(f"[mega_no_history_capped] mega-cap 1y=12%, no history -> capped at "
      f"{g1*100:.1f}% (max(end_rate=2%, ceiling*0.5=4%)), source={src1!r} "
      f"governor={gov1!r}, raw={raw1*100:.1f}% OK")


# ======================================================================
# CHECK 2: small-cap, 1y=45%, only 3-point history (< MIN_HISTORY_
# POINTS_FOR_TREND=4) - insufficient regardless of its own CAGR, so no
# corroboration is even attempted -> capped at the plain cap_1y (10%).
# ======================================================================
g2, src2, gov2, raw2 = fve.estimate_growth(
    {}, fcf_series=[130.0, 115.0, 100.0], analyst_growth=0.45,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g2 - SMALL_CAP_1Y) < 1e-9, g2
assert src2 == "analyst_1y", src2
assert gov2 == "Cap1y", gov2
print(f"[small_3pt_history_insufficient] small-cap 1y=45%, 3-point history "
      f"(insufficient) -> capped at {g2*100:.1f}% (Cap1y) OK")


# ======================================================================
# CHECK 3: small-cap, 1y=45%, 4-point CLEAN history whose own CAGR is AT
# OR ABOVE cap_1y (10%) - history corroborates a higher rate, so the cap
# loosens to min(analyst_1y, history_cagr, ceiling).
# ======================================================================
_clean_hist = [200.0, 175.0, 150.0, 130.0]   # most-recent-first, growing 130->200
_g_hist_clean = fve.growth_from_history(_clean_hist)
_cv_clean = fve._coeff_of_variation(_clean_hist)
assert _g_hist_clean >= SMALL_CAP_1Y, (_g_hist_clean, SMALL_CAP_1Y)   # corroborates
assert _cv_clean <= 0.60, _cv_clean                                   # clean
_expected3 = min(0.45, _g_hist_clean, SMALL_CEILING)
g3, src3, gov3, raw3 = fve.estimate_growth(
    {}, fcf_series=_clean_hist, analyst_growth=0.45,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g3 - _expected3) < 1e-9, (g3, _expected3)
assert src3 == "analyst_1y", src3
assert gov3 == "Cap1y", gov3   # still capped (history's own 15.7% < analyst's 45%)
print(f"[small_4pt_clean_history_corroborates] small-cap 1y=45%, 4-point clean "
      f"history (CAGR={_g_hist_clean*100:.1f}%, CV={_cv_clean:.3f}, >= cap_1y "
      f"10%) -> allowed=min(45%,{_g_hist_clean*100:.1f}%,20%)={g3*100:.1f}% "
      f"(history corroborates a higher rate than the plain cap_1y) OK")


# ======================================================================
# CHECK 4: small-cap, 1y=45%, 4-point history whose own CAGR is BELOW
# cap_1y (10%) - does NOT corroborate a higher rate -> falls back to the
# plain cap_1y (10%), exactly like check 2.
# ======================================================================
_low_hist = [110.0, 106.0, 102.0, 100.0]   # most-recent-first, growing 100->110
_g_hist_low = fve.growth_from_history(_low_hist)
assert _g_hist_low < SMALL_CAP_1Y, (_g_hist_low, SMALL_CAP_1Y)   # does NOT corroborate
g4, src4, gov4, raw4 = fve.estimate_growth(
    {}, fcf_series=_low_hist, analyst_growth=0.45,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g4 - SMALL_CAP_1Y) < 1e-9, g4
assert src4 == "analyst_1y", src4
assert gov4 == "Cap1y", gov4
print(f"[small_4pt_history_below_cap_no_corroboration] small-cap 1y=45%, 4-point "
      f"history (CAGR={_g_hist_low*100:.1f}%, < cap_1y 10%) -> does NOT "
      f"corroborate, falls back to plain cap_1y={g4*100:.1f}% (Cap1y) OK")


# ======================================================================
# CHECK 5: a genuine LTG "ok" status is UNCHANGED - mega-cap 1y=12%
# (a real 5-year figure here, not a next-year fallback) still just gets
# the plain tier ceiling (8%), no fractional cap, governor "Cap" (not
# "Cap1y") - exactly the pre-Step-1d behaviour.
# ======================================================================
g5, src5, gov5, raw5 = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=0.12, ceiling=MEGA_CEILING,
    end_rate=MEGA_END_RATE, yahoo_estimate_status="ok",
)
assert abs(g5 - MEGA_CEILING) < 1e-9, g5
assert src5 == "analyst", src5
assert gov5 == "Cap", gov5
print(f"[ltg_ok_unchanged] a genuine LTG 'ok' 12% mega-cap figure is UNCHANGED - "
      f"still clamped to the plain tier ceiling ({g5*100:.1f}%), source={src5!r} "
      f"governor={gov5!r} (not the fractional 1y cap) OK")


# ======================================================================
# CHECK 6: yahoo_estimate_status="ok_1y" with analyst_growth <= 0 falls
# through to priority 2 exactly like before - the new branch lives
# entirely inside the `analyst_growth > 0` gate, so a non-positive value
# never even reaches it, regardless of status.
# ======================================================================
_hist_for_fallthrough = [130.0, 120.0, 110.0, 100.0]
g6a, src6a, gov6a, raw6a = fve.estimate_growth(
    {}, fcf_series=_hist_for_fallthrough, analyst_growth=-0.02,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status="ok_1y",
)
g6b, src6b, gov6b, raw6b = fve.estimate_growth(
    {}, fcf_series=_hist_for_fallthrough, analyst_growth=-0.02,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status=None,
)
assert src6a != "analyst_1y" and src6a == "history", src6a
assert (g6a, src6a, gov6a, raw6a) == (g6b, src6b, gov6b, raw6b)
print(f"[ok_1y_non_positive_falls_through] analyst_growth<=0 with status='ok_1y' "
      f"falls through to priority 2 (source={src6a!r}) identically to status=None "
      f"- the new branch never fires for a non-positive value OK")


# ======================================================================
# CHECK 7: nightly_scan bucket rename/split - "yahoo_5y" -> "yahoo_
# analyst_ltg"/"yahoo_analyst_1y", and the two summary lines reconcile
# (every ticker whose capm_engine coverage was "ok"/"ok_1y" lands in the
# matching growth-source bucket too, regardless of whether Cap1y fired).
# ======================================================================
assert ns._growth_source_bucket({"growth_source": "analyst", "growth_governor": "Yahoo"}) == "yahoo_analyst_ltg"
assert ns._growth_source_bucket({"growth_source": "analyst_1y", "growth_governor": "Cap1y"}) == "yahoo_analyst_1y"
assert ns._growth_source_bucket({"growth_source": "analyst_1y", "growth_governor": "Yahoo"}) == "yahoo_analyst_1y"
print("[bucket_rename_split] _growth_source_bucket() correctly maps "
      "'analyst'->yahoo_analyst_ltg and 'analyst_1y'->yahoo_analyst_1y "
      "(regardless of Cap1y) OK")

# Reconciliation: simulate a small universe of iv_meta dicts and confirm
# the growth-source bucket counts for LTG/1y match the coverage bucket
# counts for ok/ok_1y exactly - the whole point of the rename/split.
_sim_metas = [
    {"growth_source": "analyst", "growth_governor": "Yahoo", "yahoo_estimate_status": "ok"},
    {"growth_source": "analyst", "growth_governor": "Yahoo", "yahoo_estimate_status": "ok"},
    {"growth_source": "analyst_1y", "growth_governor": "Cap1y", "yahoo_estimate_status": "ok_1y"},
    {"growth_source": "analyst_1y", "growth_governor": "Yahoo", "yahoo_estimate_status": "ok_1y"},
    {"growth_source": "analyst_1y", "growth_governor": "Cap1y", "yahoo_estimate_status": "ok_1y"},
    {"growth_source": "history", "growth_governor": "History", "yahoo_estimate_status": "no_coverage"},
]
_summary = {}
for _m in _sim_metas:
    _b1 = ns._growth_source_bucket(_m)
    _summary[_b1] = _summary.get(_b1, 0) + 1
    _b2 = ns._growth_coverage_bucket(_m)
    _summary[_b2] = _summary.get(_b2, 0) + 1
assert _summary["yahoo_analyst_ltg"] == _summary["yahoo_ltg"] == 2, _summary
assert _summary["yahoo_analyst_1y"] == _summary["yahoo_1y"] == 3, _summary
print(f"[bucket_reconciliation] growth-source line's yahoo_analyst_ltg/1y counts "
      f"exactly match the coverage line's yahoo_ltg/1y counts: {_summary} OK")


print("\nSWEEP_DONE")
