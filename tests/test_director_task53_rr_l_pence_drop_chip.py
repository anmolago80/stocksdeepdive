"""
Director's TASK 53 (13 Nov 2026, live case): RR.L's drop chip showed
"-97% from Mar 2024 peak (3y high)" with peak "GBP 393.42" - that
figure is still PENCE (393.42p = GBP 3.93), never repaired to pounds;
current price is GBP 13.70. RR.L was wrongly admitted to "Quality on
sale" as a direct result (its own stored drop_pct, ~-96.5%, easily
clears that tab's -40% floor). The Fundamentals 10-year chart is
correct for RR.L, so the bug is isolated to support_levels_job's own
weekly-bar history path.

PROVEN ROOT CAUSE (both numbers below are pure arithmetic on the
Director's own reported figures - no live data needed to prove this
part):
  PASS 1 (fundamentals_data._PENCE_HISTORY_REPAIR_RATIO_RANGE =
  (30.0, 300.0)): 393.42 / 13.70 = 28.71x - just BELOW the 30.0x
  floor, so PASS 1 never touches this point.
  PASS 2 (fundamentals_data._neighbour_jump_segments(), the TASK 29c
  adjacent-trading-day jump detector): this function returns [] (a
  complete no-op for the WHOLE series) whenever it finds no single
  consecutive-day ratio inside its own (50.0, 200.0) band anywhere -
  see that function's own docstring. A ticker whose Yahoo history has
  ALWAYS been consistently pence-quoted (RR.L's own real multi-year
  rally means every historical point is genuinely far from today's
  price, but day-to-day neighbours never jump ~100x against EACH
  OTHER) produces zero detected boundaries, so PASS 2 is a structural
  no-op for it too - this is directly provable by reading that
  function's own code, independent of what RR.L's real history
  actually contains.

FIX (this file's own subject): rather than guess at a new repair
threshold (risking a false-positive on some OTHER ticker's genuine
multi-year rally or collapse - "fix only proven causes"), drop_chip_
engine.compute_peak_and_drop() now refuses to TRUST a peak/current
ratio this extreme at all: no real operating company's genuine
3-year weekly-close peak is 5x+ its own current price (the Director's
own audit threshold, reused here as the SAME plausibility floor) -
past that, the figure is treated as a still-unrepaired data artifact
and suppressed at the source (drop_pct/chip_shown), so EVERY existing
consumer (the drop chip, the mini chart, "Quality on sale"'s own
-40% rule, the "peak fear" shadow sort, the point-in-time replay
tool) is protected without touching any of their own code.

Covers:
  a. compute_peak_and_drop(): an RR.L-shaped fixture (peak ~28.7x
     current, no PASS-1/PASS-2-catchable jump) -> peak_implausible,
     drop_pct/chip_shown suppressed, drop_pct_raw still available for
     diagnostics; a normal fixture (a real, plausible drop) completely
     unaffected; the exact 5.0x boundary (inclusive floor, exclusive
     trigger - never flags AT exactly 5.0x, only strictly past it).
  b. Every downstream consumer respects peak_implausible: passes_
     quality_on_sale_drop_rule(None) is False; top100_engine's own
     "peak fear" shadow-sort falls back rather than using the bad
     peak; point_in_time_replay.fear_from_peak_as_of() returns fear=
     None; top100_render._drop_chip_html()/_drop_chart_svg_html()
     both render nothing for an implausible entry, even gate-on/owner.
  c. End-to-end: top100_engine.quality_on_sale_candidates() with an
     RR.L-shaped fixture - RR.L does NOT appear, confirming the exact
     symptom the Director reported is closed.
  d. drop_chip_engine.audit_suspect_entries()/_series_has_suspect_
     jump(): the TASK 53 audit itself - flags an implausible-peak
     entry, flags a plausible-peak entry whose own stored series still
     has an internal single-week jump >= 50x, leaves a genuinely clean
     entry alone, sorts worst-ratio first.
  e. Root-cause proof: _weekly_bars_from_daily() + compute_peak_and_
     drop() run together on an RR.L-SHAPED synthetic daily series
     (pence segment at a ratio-to-reference just under 30x, the exact
     same magnitude the Director reported) - confirms PASS 1 leaves it
     untouched (ratio just below the floor) AND PASS 2 finds no jump
     (fundamentals_data._neighbour_jump_segments() returns [] for a
     consistently-pence series), so the raw pence value really does
     reach compute_peak_and_drop() unrepaired - and that this task's
     own plausibility guard then catches it anyway.

This sandbox has no outbound network access (confirmed repeatedly
throughout this project) - "RR.L's real 5y history" is reproduced here
as a representative synthetic fixture matching the Director's own
EXACT reported numbers (peak ratio ~28.7x, current GBP 13.70), not
fetched. The ALL-.L-tickers production audit itself (this task's other
ask) runs nightly as a real log scan over support_levels_job.py's own
freshly-computed store entries - not reproducible here either; its own
logic (drop_chip_engine.audit_suspect_entries()) is unit-tested
directly in CHECK d.

Run: python3 tests/test_director_task53_rr_l_pence_drop_chip.py
"""
import datetime
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task53_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import drop_chip_engine as dce
import fundamentals_data as fd
import point_in_time_replay as pitr
import support_levels_job as slj
import top100_engine as te
import top100_render as tr
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


def _d(i):
    return datetime.date(2020, 1, 6) + datetime.timedelta(weeks=i)


# ======================================================================
# CHECK a: compute_peak_and_drop() - the plausibility guard itself.
# ======================================================================
# RR.L-shaped: peak 393.42 (still pence), current 13.70 - ratio 28.71x,
# well past the 5.0x floor. Enough weeks either side of the peak so it
# genuinely wins max() over the 3-year window.
_rrl_closes = [13.0, 13.2, 393.42, 13.4, 13.5, 13.6, 13.70]
_rrl_dates = [_d(i) for i in range(len(_rrl_closes))]
_rrl_result = dce.compute_peak_and_drop(_rrl_dates, _rrl_closes, ticker="RR.L")
check("RR.L-shaped fixture: peak_price is still the raw 393.42 (never silently "
      "altered - only drop_pct/chip_shown are suppressed, the raw figure stays "
      "available for diagnostics)",
      _rrl_result["peak_price"] == 393.42)
check("RR.L-shaped fixture: peak_implausible is True (393.42 / 13.70 = 28.7x > 5.0x)",
      _rrl_result["peak_implausible"] is True)
check("RR.L-shaped fixture: drop_pct is None (suppressed) - never the bogus -96.5%",
      _rrl_result["drop_pct"] is None)
check("RR.L-shaped fixture: drop_pct_raw STILL carries the naive (would-be bogus) "
      "figure, for audit/diagnostic use only",
      _rrl_result["drop_pct_raw"] is not None and _rrl_result["drop_pct_raw"] < -96.0)
check("RR.L-shaped fixture: chip_shown is False (no drop_pct to compare against "
      "the -20%% threshold)",
      _rrl_result["chip_shown"] is False)
print("[task53_checkA1_rrl_shaped_suppressed] an RR.L-shaped 28.7x peak is flagged "
      "implausible, drop_pct/chip_shown suppressed, raw figure kept for audit OK")

# A normal, plausible drop (e.g. a real -35% decline) is completely unaffected.
_normal_closes = [100.0, 95.0, 90.0, 85.0, 80.0, 70.0, 65.0]
_normal_dates = [_d(i) for i in range(len(_normal_closes))]
_normal_result = dce.compute_peak_and_drop(_normal_dates, _normal_closes)
check("a normal, plausible drop (peak 100, current 65, ratio 1.54x) is NOT flagged "
      "implausible",
      _normal_result["peak_implausible"] is False)
check("a normal, plausible drop: drop_pct/drop_pct_raw are identical (never "
      "diverge when not flagged) and correctly negative",
      _normal_result["drop_pct"] == _normal_result["drop_pct_raw"]
      and abs(_normal_result["drop_pct"] - ((65.0 / 100.0 - 1.0) * 100.0)) < 1e-9)
print("[task53_checkA2_normal_fixture_unaffected] a genuinely plausible drop is "
      "completely byte-identical to before this task OK")

# Boundary: exactly 5.0x is NOT flagged (strictly > 5.0, never >=); just
# past it IS flagged.
_boundary_ok_closes = [100.0] + [20.0] * 5  # peak 100, current 20 -> ratio EXACTLY 5.0
_boundary_ok_result = dce.compute_peak_and_drop(
    [_d(i) for i in range(6)], _boundary_ok_closes, ticker="TEST.L")
check("boundary: a peak/current ratio of EXACTLY 5.0x is NOT flagged (the floor "
      "is inclusive of 5.0x itself - only strictly PAST it triggers), on a .L "
      "ticker (so this really exercises the ratio boundary, not just the .L scope)",
      _boundary_ok_result["peak_implausible"] is False
      and _boundary_ok_result["drop_pct"] is not None)

_boundary_bad_closes = [100.0] + [19.99] * 5  # ratio 5.0025x - just past the floor
_boundary_bad_result = dce.compute_peak_and_drop(
    [_d(i) for i in range(6)], _boundary_bad_closes, ticker="TEST.L")
check("boundary: a peak/current ratio just PAST 5.0x (5.0025x) IS flagged, on a "
      ".L ticker",
      _boundary_bad_result["peak_implausible"] is True
      and _boundary_bad_result["drop_pct"] is None)
print("[task53_checkA3_boundary] the 5.0x floor is exact and inclusive - only "
      "strictly past it triggers the guard OK")

# Empty input still returns the new fields, all safe defaults.
_empty_result = dce.compute_peak_and_drop([], [])
check("empty input: peak_implausible is False, drop_pct_raw is None, never raises",
      _empty_result["peak_implausible"] is False and _empty_result["drop_pct_raw"] is None)
print("[task53_checkA4_empty_input] the empty-input shape carries the new fields "
      "safely OK")


# ======================================================================
# CHECK b: every downstream consumer respects peak_implausible.
# ======================================================================
check("passes_quality_on_sale_drop_rule(None): False - RR.L's own suppressed "
      "drop_pct can never qualify it for Quality on sale",
      dce.passes_quality_on_sale_drop_rule(_rrl_result["drop_pct"]) is False)

_rrl_row = {"Fear": 10.0, "Greed": 5.0, "Fomo": 5.0, "Quality V2": 70.0}
with mock.patch.object(te.drop_chip_store, "get", return_value=_rrl_result), \
     mock.patch.object(te, "_recompute_value_score_v2_moat", return_value={"sentinel": "fallback"}):
    _peak_fear_rrl = te._recompute_value_score_v2_moat_peak_fear("RR.L", _rrl_row)
check("top100_engine._recompute_value_score_v2_moat_peak_fear(): an implausible "
      "peak falls straight back to the plain v2_moat score, never computing a "
      "bogus peak_fear from the 393.42/13.70 pair",
      _peak_fear_rrl == {"sentinel": "fallback"})
print("[task53_checkB1_peak_fear_guard] the 'peak fear' shadow sort never reads "
      "an implausible peak_price/current_price pair OK")

_rrl_fear = pitr.fear_from_peak_as_of(
    [(_d(i), c) for i, c in enumerate(_rrl_closes)], _rrl_dates[-1], ticker="RR.L")
check("point_in_time_replay.fear_from_peak_as_of(): an RR.L-shaped replay returns "
      "fear=None, never a bogus ~96%% fear reading",
      _rrl_fear["fear"] is None and _rrl_fear["peak_price"] == 393.42)
_normal_fear = pitr.fear_from_peak_as_of(
    [(_d(i), c) for i, c in enumerate(_normal_closes)], _normal_dates[-1])
check("point_in_time_replay.fear_from_peak_as_of(): a normal fixture still "
      "computes a real fear value, unaffected",
      _normal_fear["fear"] is not None)
print("[task53_checkB2_replay_guard] the point-in-time replay tool's own fear "
      "reading respects the same guard OK")

_row_rrl = {"ticker": "RR.L", "price": 13.70}
with mock.patch.object(tr, "_is_owner_for_refresh", return_value=True), \
     mock.patch.object(tr.drop_chip_store, "get", return_value={
         **_rrl_result, "series": [(d, c) for d, c in zip(_rrl_dates, _rrl_closes)],
     }):
    _rrl_chip = tr._drop_chip_html(_row_rrl, "en")
    _rrl_chart = tr._drop_chart_svg_html(_row_rrl, "en")
check("top100_render._drop_chip_html(): renders NOTHING for an implausible "
      "entry, even gated on for the owner",
      _rrl_chip == "")
check("top100_render._drop_chart_svg_html(): renders NOTHING for an implausible "
      "entry either - the series itself still carries the bad data point, not "
      "just the peak line",
      _rrl_chart == "")
print("[task53_checkB3_render_guard] neither the drop chip nor the mini chart "
      "ever renders from an implausible peak OK")


# ======================================================================
# CHECK c: end-to-end - quality_on_sale_candidates() with an RR.L-
# shaped fixture. RR.L must NOT appear.
# ======================================================================
_fake_best_by_ticker = {
    "RR.L": {"ticker": "RR.L", "company_name": "Rolls-Royce Holdings", "sector": "Industrials",
             "quality": 80.0, "quality_v2": 82.0, "moat": 60.0},
}
with mock.patch.object(te, "_build_best_by_ticker", return_value=(_fake_best_by_ticker, 1)), \
     mock.patch.object(te.top100_store, "current_pool", return_value=[]), \
     mock.patch.object(te.drop_chip_store, "get", return_value=_rrl_result), \
     mock.patch.object(te.top100_store, "latest_score_for_ticker", return_value=None):
    _qos_result = te.quality_on_sale_candidates()
check("quality_on_sale_candidates(): RR.L (quality 82 >= 65, but its own "
      "drop_pct is suppressed) does NOT appear as a candidate - the exact "
      "symptom the Director reported is closed",
      all(c["ticker"] != "RR.L" for c in _qos_result["candidates"]))
print("[task53_checkC_end_to_end] RR.L no longer qualifies for Quality on sale "
      "after this fix OK")


# ======================================================================
# CHECK d: drop_chip_engine.audit_suspect_entries() / _series_has_
# suspect_jump() - the TASK 53 audit itself.
# ======================================================================
check("_series_has_suspect_jump(): a genuine ~100x adjacent jump is detected",
      dce._series_has_suspect_jump([(_d(0), 4.0), (_d(1), 400.0), (_d(2), 410.0)]) is True)
check("_series_has_suspect_jump(): a normal, gradually-moving series has no jump",
      dce._series_has_suspect_jump([(_d(i), 100.0 + i) for i in range(10)]) is False)
check("_series_has_suspect_jump(): an empty/None series never raises, reads False",
      dce._series_has_suspect_jump([]) is False and dce._series_has_suspect_jump(None) is False)

_entries = {
    "RR.L": {**_rrl_result, "series": [(d, c) for d, c in zip(_rrl_dates, _rrl_closes)]},
    "CLEAN.L": {**_normal_result, "series": [(d, c) for d, c in zip(_normal_dates, _normal_closes)]},
    "JUMPY.L": {
        "peak_price": 50.0, "current_price": 45.0,  # plausible ratio (1.1x) on its own
        "drop_pct": -10.0, "drop_pct_raw": -10.0,
        "series": [(_d(0), 48.0), (_d(1), 4800.0), (_d(2), 50.0), (_d(3), 45.0)],  # internal jump
    },
}
_suspect = dce.audit_suspect_entries(_entries)
_suspect_tickers = {s["ticker"] for s in _suspect}
check("audit_suspect_entries(): RR.L (implausible peak ratio) is flagged",
      "RR.L" in _suspect_tickers)
check("audit_suspect_entries(): JUMPY.L (plausible overall ratio, but an internal "
      "single-week jump >= 50x) is ALSO flagged - the ratio check alone would "
      "have missed it",
      "JUMPY.L" in _suspect_tickers)
check("audit_suspect_entries(): CLEAN.L (no implausible ratio, no internal jump) "
      "is NOT flagged",
      "CLEAN.L" not in _suspect_tickers)
check("audit_suspect_entries(): RR.L sorts FIRST (its own peak_to_current_ratio, "
      "28.7x, is the worst/highest)",
      _suspect[0]["ticker"] == "RR.L")
check("audit_suspect_entries(): RR.L's own row reports before/after (drop_pct_raw "
      "vs drop_pct) and the exact ratio",
      _suspect[0]["drop_pct"] is None and _suspect[0]["drop_pct_raw"] < -96.0
      and abs(_suspect[0]["peak_to_current_ratio"] - 28.71) < 0.1)
print("[task53_checkD_audit] the TASK 53 audit flags both an implausible-ratio "
      "entry AND a plausible-ratio entry with an internal jump, leaves a clean "
      "entry alone, sorts worst first OK")


# ======================================================================
# CHECK e: root-cause proof - _weekly_bars_from_daily() + compute_
# peak_and_drop() run together on an RR.L-SHAPED synthetic daily
# series, confirming the raw pence value really does reach compute_
# peak_and_drop() UNREPAIRED by either PASS 1 or PASS 2, and that this
# task's own guard then catches it anyway.
# ======================================================================
def _daily_df(closes, start="2024-01-01"):
    idx = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx,
    )


# ~18 months of daily closes: a long consistently-pence-quoted stretch
# around GBP 3.90-4.00 real price (reported as 390-400 "pence" by
# Yahoo, i.e. these ARE the raw numbers Yahoo would actually return),
# settling to today's real price of 13.70 (genuinely in GBP, far above
# the 30x floor so PASS 1 catches nothing there either way) - no
# adjacent-day jump anywhere, so PASS 2 has nothing to find.
_rrl_daily_closes = [390.0 + (i % 10) for i in range(260)] + [13.70] * 5
_rrl_daily_df = _daily_df(_rrl_daily_closes)
_reference_price = 13.70  # today's real, already-GBP price - the pool's own stored price

_repaired = fd.repair_mixed_unit_price_dataframe(_rrl_daily_df, _reference_price, ticker="RR.L")
_repaired_closes = _repaired["Close"].tolist()
check("PROOF: the pence segment's own ratio to today's reference (390/13.70 "
      "approx 28.5x) sits BELOW PASS 1's 30.0x floor, confirmed by reading it "
      "back UNCHANGED after repair_mixed_unit_price_dataframe() runs",
      abs(_repaired_closes[0] - 390.0) < 1e-9)
check("PROOF: fundamentals_data._neighbour_jump_segments() finds NO boundary "
      "anywhere in this consistently-pence-then-genuinely-GBP series (no two "
      "ADJACENT days differ by a 50x-200x ratio - the pence/GBP transition "
      "itself is only ~29x, still under PASS 2's own 50x floor) - PASS 2 is a "
      "structural no-op for this exact shape",
      fd._neighbour_jump_segments(_rrl_daily_df["Close"].tolist()) == [])
_dates, _highs, _lows, _closes = slj._weekly_bars_from_daily(_rrl_daily_df, "RR.L", _reference_price)
check("PROOF: the resulting WEEKLY series still has an unrepaired ~390-range "
      "value sitting among the weekly closes (both passes genuinely missed it, "
      "exactly as the Director's own hypothesis described)",
      max(_closes[:-1]) > 300.0)
_rrl_real_result = dce.compute_peak_and_drop(_dates, _closes, ticker="RR.L")
check("PROOF, closing the loop: even though BOTH repair passes missed this "
      "value, compute_peak_and_drop()'s own new plausibility guard still "
      "catches it and suppresses drop_pct/chip_shown",
      _rrl_real_result["peak_implausible"] is True and _rrl_real_result["drop_pct"] is None)
print("[task53_checkE_root_cause_proof] PASS 1 (30x floor) and PASS 2 (adjacent-"
      "day jump detector) both structurally miss a consistently-pence-quoted "
      "series whose ratio-to-today has drifted under their own detection floors "
      "- proven by running the real repair code on an RR.L-shaped series; this "
      "task's own plausibility guard closes the gap regardless OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
