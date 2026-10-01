"""
Step 1d (owner-directed, 30 Sep 2026, from instruction_step1d_step4_
rewind.md) governed the next-year analyst tier with its own "Cap1y"
fractional cap/history-corroboration branch, distinct from the plain
market-cap tier ceiling.

REVISED the same day, 20:55 AEST (instruction_analyst_1y_ceiling_
only.md, owner decision): "the market-cap tier ceiling (8/12/16/20%)
is the safety limit and was built for exactly this; a second per-
source cap under it for the next-year analyst figure adds a rule
without adding safety." The Cap1y governor, the history-corroboration
branch, and the fractional cap are REMOVED - fcf_valuation_engine.
estimate_growth() now governs "ok_1y" (the next-year analyst tier)
EXACTLY like a genuine "ok" (LTG) value: the plain market-cap tier
ceiling only. The ½-ceiling fractional cap (REPORTED_GROWTH_CAP_
FRACTION) stays exactly as it was for single-quarter REPORTED growth
(info["revenueGrowth"]/["earningsGrowth"]) - that figure isn't a
forecast at all, unlike an analyst consensus.

Source stays "analyst_1y" (distinct from plain "analyst") purely for
DISPLAY - so a Deep Dive/Fair Value label still reads "Yahoo analyst
(next year)" rather than implying a genuine 5-year figure - it has no
effect on how the number is governed any more.

Priority order is UNCHANGED: Yahoo LTG/1y -> history (>= MIN_HISTORY_
POINTS_FOR_TREND=4 points; volatile -> ½-ceiling cap) -> reported
(revenue then earnings, ½-ceiling cap) -> tier end rate (flagged,
never 0%).

REVISED AGAIN (growth 1y-blend fix, owner-directed, 1 Oct 2026 20:37
AEST, KNSL/Kinsale Capital live case): a BARE "ok_1y" next-year
consensus is cycle-dominated, so it's now blended with clean FCF
history (>= MIN_HISTORY_POINTS_FOR_TREND points, history capped at
the tier ceiling BEFORE averaging) whenever enough history exists;
with no usable history the consensus is used alone, floored at
end_rate. This changes CHECK 2 (governor text; the 6% consensus no
longer reads "Yahoo" since it's now floored-not-passthrough) and
CHECK 3 (history is no longer inert for "ok_1y" - the blend fires,
changing source/governor/raw even though the final number happens to
land on the same ceiling-capped value either way). See
fcf_valuation_engine.estimate_growth()'s own docstring for the exact
blend formula.

Run: python3 tests/test_analyst_1y_governor.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve
import nightly_scan as ns

MEGA_CEILING, MEGA_END_RATE = 0.08, 0.02
SMALL_CEILING, SMALL_END_RATE = 0.20, 0.06
REPORTED_CAP_SMALL = max(SMALL_END_RATE, SMALL_CEILING * fve.REPORTED_GROWTH_CAP_FRACTION)  # 0.10
assert abs(REPORTED_CAP_SMALL - 0.10) < 1e-9


# ======================================================================
# CHECK 1: mega-cap, next-year analyst 12% -> capped at the PLAIN tier
# ceiling (8%) - no fractional cap any more, no history involvement.
# ======================================================================
g1, src1, gov1, raw1 = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=0.12, ceiling=MEGA_CEILING,
    end_rate=MEGA_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g1 - MEGA_CEILING) < 1e-9, g1
assert src1 == "analyst_1y", src1
assert gov1 == "Cap", gov1
assert abs(raw1 - 0.12) < 1e-9, raw1
print(f"[mega_1y_12pct_capped_at_ceiling] mega-cap next-year analyst 12% -> "
      f"{g1*100:.1f}% (plain tier ceiling, not a fractional cap), source={src1!r} "
      f"governor={gov1!r} OK")


# ======================================================================
# CHECK 2: mega-cap, next-year analyst 6%, no history -> floored at
# end_rate (not relevant here since 6% > 2% end_rate already) and
# passed through under the ceiling (8%) - governor is now the blend
# fix's "floored at end rate" text (no history -> no blend branch),
# not "Yahoo" (that governor string belongs to the untouched plain
# "analyst"/"ok" LTG path, not "ok_1y" any more).
# ======================================================================
g2, src2, gov2, raw2 = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=0.06, ceiling=MEGA_CEILING,
    end_rate=MEGA_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g2 - 0.06) < 1e-9, g2
assert src2 == "analyst_1y", src2
assert gov2 == "next-year consensus, floored at end rate", gov2
print(f"[mega_1y_6pct_uncapped] mega-cap next-year analyst 6% (under the 8% "
      f"ceiling), no history -> passes through unchanged at {g2*100:.1f}%, "
      f"governor={gov2!r} OK")


# ======================================================================
# CHECK 3: small-cap, next-year analyst 45% -> the FINAL number still
# lands on the plain 20% ceiling regardless of history (unchanged),
# but history is NO LONGER inert for "ok_1y" after the growth 1y-blend
# fix (1 Oct 2026): with >= MIN_HISTORY_POINTS_FOR_TREND clean history
# present, the blend branch fires (source becomes "analyst_1y_blend",
# governor becomes the blend text, raw becomes the pre-clamp blended
# average) even though min(blend, ceiling) and min(consensus, ceiling)
# both happen to resolve to the same 20% here. Computed via
# growth_from_history() directly (not hand-derived) for the same
# reason CHECK 4/5 below do.
# ======================================================================
_strong_hist = [200.0, 175.0, 150.0, 130.0]  # would have corroborated under the old Cap1y logic
_g_strong_hist = fve.growth_from_history(_strong_hist)
assert _g_strong_hist is not None and _g_strong_hist > 0, _g_strong_hist
_history_capped_3 = min(_g_strong_hist, SMALL_CEILING)
_raw3_expected = max(SMALL_END_RATE, min((0.45 + _history_capped_3) / 2.0, SMALL_CEILING))
g3, src3, gov3, raw3 = fve.estimate_growth(
    {}, fcf_series=_strong_hist, analyst_growth=0.45,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g3 - SMALL_CEILING) < 1e-9, g3
assert src3 == "analyst_1y_blend", src3
assert gov3 == "next-year consensus blended with history (history capped at ceiling)", gov3
assert abs(raw3 - _raw3_expected) < 1e-9, (raw3, _raw3_expected)
# With history ABSENT, the blend branch can't fire at all - falls back
# to the bare-consensus-floored-at-end-rate branch (CHECK 2's branch),
# which the plain ceiling clamp still brings to the identical FINAL
# number here, but via a different source/governor/raw - no longer
# "identical regardless of history" (that was the pre-blend-fix
# behaviour this task deliberately changed).
g3b, src3b, gov3b, raw3b = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=0.45,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status="ok_1y",
)
assert abs(g3b - SMALL_CEILING) < 1e-9, g3b
assert src3b == "analyst_1y", src3b
assert gov3b == "Cap", gov3b
assert abs(raw3b - 0.45) < 1e-9, raw3b
assert g3 == g3b and (src3, gov3) != (src3b, gov3b)
print(f"[small_1y_45pct_capped_regardless_of_history] small-cap next-year analyst "
      f"45% -> {g3*100:.1f}% (plain 20% ceiling) whether a strong corroborating "
      f"history is present (source={src3!r}) or absent (source={src3b!r}) - same "
      f"final number, different path through the new blend logic OK")


# ======================================================================
# CHECK 4: small-cap, no analyst estimate, 4-point CLEAN history with a
# real CAGR (~14%, computed programmatically, not hand-derived - see
# this module's own comment on verifying against growth_from_history()
# directly) -> priority 2 fires and passes through UNCAPPED (14% < the
# 20% ceiling) - history still comes before reported growth.
# ======================================================================
_hist_14pct = [130.0, 130.0, 100.0, 100.0]
_g_hist_14 = fve.growth_from_history(_hist_14pct)
_cv_14 = fve._coeff_of_variation(_hist_14pct)
assert _cv_14 <= 0.60, _cv_14  # clean, not volatile
g4, src4, gov4, raw4 = fve.estimate_growth(
    {"revenueGrowth": 0.40}, fcf_series=_hist_14pct, analyst_growth=None,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status=None,
)
assert abs(g4 - _g_hist_14) < 1e-9, (g4, _g_hist_14)
assert src4 == "history", src4
assert gov4 == "History", gov4
print(f"[no_analyst_4pt_clean_history_before_reported] no analyst estimate, 4-point "
      f"clean history (CAGR={_g_hist_14*100:.2f}%, CV={_cv_14:.3f}) with a 40% "
      f"reported-growth figure ALSO present -> history wins (priority 2 before "
      f"priority 3), passes through uncapped at {g4*100:.2f}% OK")


# ======================================================================
# CHECK 5: 4-point history with a NEGATIVE CAGR (does not satisfy g>0)
# and a 40% reported-growth figure -> falls through to reported growth,
# capped at max(end_rate=6%, ceiling*0.5=10%) = 10% - the REPORTED-
# growth cap (REPORTED_GROWTH_CAP_FRACTION) is UNCHANGED by this task.
# ======================================================================
_hist_negative = [95.0, 95.0, 100.0, 100.0]
_g_hist_neg = fve.growth_from_history(_hist_negative)
assert _g_hist_neg is not None and _g_hist_neg < 0, _g_hist_neg
g5, src5, gov5, raw5 = fve.estimate_growth(
    {"revenueGrowth": 0.40}, fcf_series=_hist_negative, analyst_growth=None,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status=None,
)
assert abs(g5 - REPORTED_CAP_SMALL) < 1e-9, g5
assert src5 == "info", src5
assert gov5 == "Cap", gov5  # raw (40%) exceeds its own cap (10%) - "Cap", not "Info"
assert abs(raw5 - 0.40) < 1e-9, raw5
print(f"[negative_history_falls_to_reported_capped] 4-point history with a negative "
      f"CAGR ({_g_hist_neg*100:.2f}%) fails the g>0 test -> falls through to the 40% "
      f"reported-growth figure, capped at {g5*100:.1f}% (unchanged ½-ceiling cap) OK")


# ======================================================================
# CHECK 6: only 2-point history (< MIN_HISTORY_POINTS_FOR_TREND=4) -
# insufficient regardless of its own CAGR - with a 40% reported-growth
# figure -> same 10% cap as check 5, via the same reported-growth path.
# ======================================================================
_hist_2pt = [110.0, 100.0]
g6, src6, gov6, raw6 = fve.estimate_growth(
    {"revenueGrowth": 0.40}, fcf_series=_hist_2pt, analyst_growth=None,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status=None,
)
assert abs(g6 - REPORTED_CAP_SMALL) < 1e-9, g6
assert src6 == "info", src6
assert gov6 == "Cap", gov6  # same reasoning as check 5 - raw exceeds its own cap
print(f"[2pt_history_insufficient_falls_to_reported] only 2-point history "
      f"(< MIN_HISTORY_POINTS_FOR_TREND=4, insufficient regardless of its own CAGR) "
      f"with a 40% reported-growth figure -> falls through identically to check 5, "
      f"capped at {g6*100:.1f}% OK")


# ======================================================================
# CHECK 7: a genuine LTG "ok" status - mega-cap 12% -> the plain tier
# ceiling (8%), governor "Cap", source "analyst" - completely unchanged
# by this task (it was already ceiling-only before Step 1d ever
# existed, and remains so).
# ======================================================================
g7, src7, gov7, raw7 = fve.estimate_growth(
    {}, fcf_series=None, analyst_growth=0.12, ceiling=MEGA_CEILING,
    end_rate=MEGA_END_RATE, yahoo_estimate_status="ok",
)
assert abs(g7 - MEGA_CEILING) < 1e-9, g7
assert src7 == "analyst", src7
assert gov7 == "Cap", gov7
print(f"[ltg_ok_unchanged] a genuine LTG 'ok' mega-cap 12% figure -> the plain tier "
      f"ceiling ({g7*100:.1f}%), source={src7!r} governor={gov7!r} - identical "
      f"treatment to the now-ceiling-only 'ok_1y' path OK")


# ======================================================================
# Supporting check: yahoo_estimate_status="ok_1y" with analyst_growth
# <= 0 still falls through to priority 2 exactly like status=None - the
# analyst branch lives entirely inside the `analyst_growth > 0` gate.
# ======================================================================
_hist_for_fallthrough = [130.0, 120.0, 110.0, 100.0]
g8a, src8a, gov8a, raw8a = fve.estimate_growth(
    {}, fcf_series=_hist_for_fallthrough, analyst_growth=-0.02,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status="ok_1y",
)
g8b, src8b, gov8b, raw8b = fve.estimate_growth(
    {}, fcf_series=_hist_for_fallthrough, analyst_growth=-0.02,
    ceiling=SMALL_CEILING, end_rate=SMALL_END_RATE, yahoo_estimate_status=None,
)
assert src8a != "analyst_1y" and src8a == "history", src8a
assert (g8a, src8a, gov8a, raw8a) == (g8b, src8b, gov8b, raw8b)
print(f"[ok_1y_non_positive_falls_through] analyst_growth<=0 with status='ok_1y' "
      f"falls through to priority 2 (source={src8a!r}) identically to status=None OK")


# ======================================================================
# Supporting check: nightly_scan bucketing needs NO code change (source
# == "analyst_1y" is checked before governor == "Cap" in _growth_
# source_bucket() - see that function's own docstring) - confirmed here
# with the new governor values ("Cap"/"Yahoo" instead of "Cap1y") to
# make sure the bucket/reconciliation behaviour survives the removal.
# ======================================================================
assert ns._growth_source_bucket({"growth_source": "analyst", "growth_governor": "Yahoo"}) == "yahoo_analyst_ltg"
assert ns._growth_source_bucket({"growth_source": "analyst_1y", "growth_governor": "Cap"}) == "yahoo_analyst_1y"
assert ns._growth_source_bucket({"growth_source": "analyst_1y", "growth_governor": "Yahoo"}) == "yahoo_analyst_1y"
print("[bucket_rename_split_survives_governor_change] _growth_source_bucket() still "
      "correctly maps 'analyst'->yahoo_analyst_ltg and 'analyst_1y'->yahoo_analyst_1y "
      "(regardless of the now-plain 'Cap'/'Yahoo' governor) OK")

_sim_metas = [
    {"growth_source": "analyst", "growth_governor": "Yahoo", "yahoo_estimate_status": "ok"},
    {"growth_source": "analyst", "growth_governor": "Yahoo", "yahoo_estimate_status": "ok"},
    {"growth_source": "analyst_1y", "growth_governor": "Cap", "yahoo_estimate_status": "ok_1y"},
    {"growth_source": "analyst_1y", "growth_governor": "Yahoo", "yahoo_estimate_status": "ok_1y"},
    {"growth_source": "analyst_1y", "growth_governor": "Cap", "yahoo_estimate_status": "ok_1y"},
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
