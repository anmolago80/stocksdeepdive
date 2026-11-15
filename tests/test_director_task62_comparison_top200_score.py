"""
TASK 62 (Director-directed, 15 Nov 2026) - Comparison shows the steady
Top 200 score.

PROBLEM (Director verified in code at cc1b2b5 + Railway logs): the
Comparison page computed its own live Long Score in _render_scan_
results() via calculate_long_score(), including a live Discovery (news/
Trends/social attention) reading taken at that moment - while Deep Dive
computes its own live headline independently, at a different moment.
Example: WMS showed Comparison 45.2 vs Deep Dive 55.5 for the same
Quality/MOS/Psychology - almost the whole gap was the attention reading.
The steady number is Deep Dive's own subline, "Scanner & Top 200 score"
(no live Discovery), from ranking_engine.recompute_value_score().

FIX, covered here:
  1. New shared helper, ranking_engine.top200_score_with_quality() - the
     SAME Quality V2 swap/fallback + moat_blend switch + recompute_
     value_score() call deep_dive_engine.py's own subline already uses,
     factored out so the Comparison page can call the identical formula
     without a second, independently-written copy (never a third
     formula) and WITHOUT deep_dive_engine.py itself being touched at
     all (the Director's own explicit instruction).
  2. app.py's _render_scan_results(): on Comparison only (state_prefix
     == "cmp") a new "Value Score (Top 200)" / "_top200_quality" pair of
     columns carries this steady score + the Quality it was computed
     from. Scanner (state_prefix == "scan") is byte-identical to before
     this task - these two columns are always None there and nothing
     reads them on that branch.
  3. The main results table, the Head-to-head score row, and the CSV
     download all read "Value Score (Top 200)" instead of "Long Score"
     on Comparison; "Long Score" itself (and everything keyed off it -
     sort order, Investment Signal, the thesis, Trade Setup) is
     completely UNCHANGED - this task touches DISPLAY only.
  4. Head-to-head's "Quality / Moat" row reads "_top200_quality" (the
     SAME Quality the score above used) instead of the live "Quality",
     so the two rows always agree.
  5. Label "Value Score (Top 200)" (EN+ES, i18n keys comparison.h2h_
     score_top200 / h2h_top200_caption) + a one-line caption under
     Head-to-head.
  6. The Valuation pill stays MOS-driven - untouched.

Run: python3 tests/test_director_task62_comparison_top200_score.py
"""
import inspect
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SCHEDULER_ENABLED", "false")

import i18n
import ranking_engine

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


def _reset_gates():
    os.environ.pop("TOP200_QUALITY_V2_LIVE", None)
    os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)


# ======================================================================
# PART A - ranking_engine.top200_score_with_quality(): the formula
# itself, and its exact agreement with Deep Dive's own "Scanner & Top
# 200 score" subline formula (same recompute_value_score() call, same
# row shape) for identical inputs - the WMS-shaped case.
# ======================================================================

# CHECK A1: plain formula (V2 off, moat_blend off) - the WMS-shaped
# fixture. quality_used must be the live Quality passed in (no V2
# swap), and the score must equal calling ranking_engine.
# recompute_value_score() DIRECTLY on the exact row shape deep_dive_
# engine.py's own subline builds (this IS the Deep Dive subline
# formula - same function, same row, same result, by construction).
_reset_gates()
_wms_quality, _wms_mos, _wms_psych, _wms_moat = 61.0, 49.0, 13.0, None
_result = ranking_engine.top200_score_with_quality(
    "WMS", _wms_quality, _wms_mos, _wms_psych, moat_score=_wms_moat, moat_blend=False,
)
_dd_style_row = {
    "Quality": _wms_quality, "MOS %": _wms_mos, "Psychology": _wms_psych,
    "Discovery (lite)": 0, "Moat": _wms_moat,
}
_dd_subline_value = ranking_engine.recompute_value_score(_dd_style_row, moat_blend=False)
check("[A1] quality_used is the live Quality (no V2 swap, gate off)",
      _result["quality_used"] == _wms_quality)
check(f"[A1] WMS-shaped comparison score ({_result['score']}) EXACTLY equals "
      f"the Deep Dive subline formula's own value ({_dd_subline_value}) for the "
      f"same Quality/MOS/Psychology", _result["score"] == _dd_subline_value)

# CHECK A2: live attention/Discovery never moves the comparison score -
# structurally guaranteed (the function takes no discovery_score
# parameter at all), confirmed by signature inspection.
_sig_params = set(inspect.signature(ranking_engine.top200_score_with_quality).parameters)
check("[A2] top200_score_with_quality() has no discovery/attention "
      "parameter at all - today's live news/Trends/social reading "
      "structurally cannot move this score",
      not ({"discovery_score", "discovery", "news_score", "trend_score",
            "social_score"} & _sig_params))

# CHECK A3: calling it twice with identical Quality/MOS/Psychology
# always returns the identical score, regardless of anything else
# happening "live" elsewhere in the process.
_r1 = ranking_engine.top200_score_with_quality("WMS", 61.0, 49.0, 13.0, moat_score=None)
_r2 = ranking_engine.top200_score_with_quality("WMS", 61.0, 49.0, 13.0, moat_score=None)
check("[A3] two calls with identical inputs return the identical score "
      "(no hidden live-attention state leaking in)", _r1["score"] == _r2["score"])


# ======================================================================
# PART B - Quality V2 swap + fallback.
# ======================================================================

# CHECK B1: TOP200_QUALITY_V2_LIVE on, V2 lookup succeeds -> quality_used
# swaps to the V2 figure, and the score is computed FROM that V2 figure
# (never a blend of both).
_reset_gates()
os.environ["TOP200_QUALITY_V2_LIVE"] = "1"
with mock.patch.object(ranking_engine.fundamentals_data, "get_bundle", return_value={"fake": "bundle"}), \
     mock.patch.object(ranking_engine.quality_v2_engine, "get_quality_score_v2",
                        return_value=(78.0, {"fiscal_years_used": []})):
    _r = ranking_engine.top200_score_with_quality("WMS", 61.0, 49.0, 13.0, moat_score=None)
_expected = ranking_engine.recompute_value_score(
    {"Quality": 78.0, "MOS %": 49.0, "Psychology": 13.0, "Discovery (lite)": 0, "Moat": None},
    moat_blend=False,
)
check("[B1] V2 live + available -> quality_used is the V2 figure (78.0), "
      "not the live Quality (61.0)", _r["quality_used"] == 78.0)
check("[B1] ...and the score is computed from THAT quality_used, matching "
      "recompute_value_score() on the V2-swapped row", _r["score"] == _expected)

# CHECK B2: V2 live but this ticker has no stored V2 score at all
# (returns None) -> falls back to the live Quality (v1), never a crash,
# never a silent zero.
_reset_gates()
os.environ["TOP200_QUALITY_V2_LIVE"] = "1"
with mock.patch.object(ranking_engine.fundamentals_data, "get_bundle", return_value={"fake": "bundle"}), \
     mock.patch.object(ranking_engine.quality_v2_engine, "get_quality_score_v2",
                        return_value=(None, {"fiscal_years_used": []})):
    _r = ranking_engine.top200_score_with_quality("WMS", 61.0, 49.0, 13.0, moat_score=None)
check("[B2] V2 live but this ticker has no V2 score -> falls back to the "
      "live Quality (v1)", _r["quality_used"] == 61.0)

# CHECK B3: V2 live but the lookup itself raises -> fails open to the
# live Quality, never propagates the exception to the Comparison page.
_reset_gates()
os.environ["TOP200_QUALITY_V2_LIVE"] = "1"
with mock.patch.object(ranking_engine.fundamentals_data, "get_bundle",
                        side_effect=RuntimeError("network down")):
    _r = ranking_engine.top200_score_with_quality("WMS", 61.0, 49.0, 13.0, moat_score=None)
check("[B3] V2 lookup raises -> fails open to the live Quality, no "
      "exception escapes", _r["quality_used"] == 61.0)

# CHECK B4: V2 gate OFF (unset) -> never even attempts a V2 lookup -
# quality_used is always the live Quality, same as today.
_reset_gates()
with mock.patch.object(ranking_engine.quality_v2_engine, "get_quality_score_v2") as _mock_v2:
    _r = ranking_engine.top200_score_with_quality("WMS", 61.0, 49.0, 13.0, moat_score=None)
    check("[B4] V2 gate off -> get_quality_score_v2() is never called",
          _mock_v2.call_count == 0)
check("[B4] ...and quality_used is the live Quality", _r["quality_used"] == 61.0)


# ======================================================================
# PART C - moat_blend switch on/off.
# ======================================================================
_reset_gates()

# CHECK C1: moat_blend=False (default/switch off) -> identical to the
# plain formula regardless of a Moat score being present; Moat never
# enters the computation at all.
_r_no_moat_blend = ranking_engine.top200_score_with_quality(
    "WMS", 61.0, 49.0, 13.0, moat_score=80.0, moat_blend=False,
)
_r_no_moat_score = ranking_engine.top200_score_with_quality(
    "WMS", 61.0, 49.0, 13.0, moat_score=None, moat_blend=False,
)
check("[C1] moat_blend=False -> a present Moat score changes nothing "
      "(never enters the plain formula)",
      _r_no_moat_blend["score"] == _r_no_moat_score["score"])

# CHECK C2: moat_blend=True with a real Moat score -> matches
# recompute_value_score(row, moat_blend=True) directly, and DIFFERS from
# the moat_blend=False figure (the switch genuinely changes the number).
_r_moat_blend = ranking_engine.top200_score_with_quality(
    "WMS", 61.0, 49.0, 13.0, moat_score=80.0, moat_blend=True,
)
_expected_moat_blend = ranking_engine.recompute_value_score(
    {"Quality": 61.0, "MOS %": 49.0, "Psychology": 13.0, "Discovery (lite)": 0, "Moat": 80.0},
    moat_blend=True,
)
check("[C2] moat_blend=True matches recompute_value_score(row, "
      "moat_blend=True) exactly", _r_moat_blend["score"] == _expected_moat_blend)
check("[C2] ...and differs from the moat_blend=False figure for the "
      "same inputs (the switch has real effect)",
      _r_moat_blend["score"] != _r_no_moat_blend["score"])

# CHECK C3: moat_blend=True with NO stored Moat score (None) -> falls
# through to calculate_long_score()'s own missing-moat reweighting
# (never scored as if Moat were zero), matching recompute_value_score()
# directly.
_r_moat_blend_missing = ranking_engine.top200_score_with_quality(
    "WMS", 61.0, 49.0, 13.0, moat_score=None, moat_blend=True,
)
_expected_missing = ranking_engine.recompute_value_score(
    {"Quality": 61.0, "MOS %": 49.0, "Psychology": 13.0, "Discovery (lite)": 0, "Moat": None},
    moat_blend=True,
)
check("[C3] moat_blend=True with no stored Moat score -> matches "
      "recompute_value_score()'s own missing-moat reweighting",
      _r_moat_blend_missing["score"] == _expected_missing)

_reset_gates()


# ======================================================================
# PART D - app.py wiring (source inspection - _render_scan_results() is
# a 1000+ line function shared with the Scanner page, deeply entangled
# with live yfinance/trade-filter/swing-engine calls that make a full
# AppTest-level run impractical here; the same source-inspection
# approach already used for TASK 59/61's own render-function wiring
# checks in this test suite).
# ======================================================================
os.environ.setdefault("AUTH_COOKIE_SECRET", "test-secret-task62")
import app

_src = inspect.getsource(app._render_scan_results)

check("[D1] a new 'Value Score (Top 200)' column is added to the row "
      "dict, gated to Comparison only (None on Scanner)",
      '"Value Score (Top 200)": (' in _src and 'if _top200_result else None' in _src)
check("[D2] a new '_top200_quality' column (internal, never exported) "
      "carries the Quality the steady score used",
      '"_top200_quality": (' in _src)
check("[D3] the Top 200 score/quality computation is gated by "
      "state_prefix == \"cmp\" (None on the Scanner page)",
      'if state_prefix == "cmp":\n                    _top200_moat_blend' in _src)
check("[D4] the Comparison-only computation reuses ranking_engine."
      "top200_score_with_quality() - never a third, independent formula",
      "ranking_engine.top200_score_with_quality(" in _src)
check("[D5] the CSV download drops 'Long Score' from the exported "
      "columns on Comparison, so only the Top 200 score is exported",
      'if state_prefix == "cmp" and "Long Score" in _dl_cols:' in _src)
check("[D6] the Head-to-head score row reads 'Value Score (Top 200)', "
      "not 'Long Score'",
      '_score_nums = [r["Value Score (Top 200)"] for _, r in results.iterrows()]' in _src)
check("[D7] the Head-to-head Quality/Moat row reads '_top200_quality', "
      "not the live 'Quality', so the two rows agree",
      '_q = r["_top200_quality"]' in _src)
check("[D8] the Head-to-head score label uses the new i18n key "
      "(comparison.h2h_score_top200), EN 'Value Score (Top 200)'",
      'i18n.t("comparison.h2h_score_top200", _h2h_lang)' in _src
      and i18n.t("comparison.h2h_score_top200", "en") == "Value Score (Top 200)")
check("[D9] the new one-line caption (comparison.h2h_top200_caption) is "
      "rendered under Head-to-head",
      'st.caption(i18n.t("comparison.h2h_top200_caption", _h2h_lang))' in _src)
check("[D10] EN caption text matches the Director's own exact wording",
      i18n.t("comparison.h2h_top200_caption", "en")
      == "Same score the Top 200 ranks on - excludes today's live news/attention.")
check("[D11] ES label/caption both exist (non-empty, different from EN)",
      bool(i18n.t("comparison.h2h_score_top200", "es"))
      and i18n.t("comparison.h2h_score_top200", "es") != i18n.t("comparison.h2h_score_top200", "en")
      and bool(i18n.t("comparison.h2h_top200_caption", "es")))
check("[D12] the main Side-by-side table's score column is conditional "
      "on state_prefix (the Top 200 column for cmp, 'Long Score' "
      "unchanged for scan)",
      '_cmp_score_col = "Value Score (Top 200)" if state_prefix == "cmp" else "Long Score"' in _src)
check("[D13] the Valuation pill/column is untouched - 'Valuation' is "
      "still driven straight off valuation (MOS), no new computation "
      "introduced for it by this task",
      _src.count('valuation = "N/A"') == 1 and _src.count('valuation = "UNDERVALUED"') == 1)

# CHECK D14: "Long Score" itself (the live figure) is computed
# IDENTICALLY to before this task - the long_score assignment block is
# untouched, confirmed by the exact original two-branch shape still
# being present with no Top 200 logic interleaved into it.
_long_score_block = (
    'if moat_engine.MOAT_IN_VALUE_SCORE:\n'
    '                    long_score = calculate_long_score(\n'
    '                        quality_score, margin_of_safety, psychology_score, discovery_score,\n'
    '                        mode="moat_blend",\n'
    '                        moat_score=_moat_cached["score"] if _moat_cached else None,\n'
    '                    )\n'
    '                else:\n'
    '                    long_score = calculate_long_score(\n'
    '                        quality_score, margin_of_safety, psychology_score, discovery_score\n'
    '                    )'
)
check("[D14] the live 'long_score' computation itself is byte-identical "
      "to before this task (untouched scoring formula)",
      _long_score_block in _src)

# CHECK D15: the Scanner page's own call site is unchanged (same
# page_label/state_prefix args as always).
_page_scanner_src = inspect.getsource(app.page_scanner)
check("[D16] Scanner's own call site still passes state_prefix=\"scan\" "
      "(unchanged)", '_render_scan_results(\n            page_label="Scanner",\n            state_prefix="scan",' in _page_scanner_src)

# CHECK D17/D18: _comparison_row_from_saved_scan() - the OTHER place a
# Comparison row gets built (a fresh (<=72h) saved overnight-scan row,
# served without a live fetch - ONLY ever called when state_prefix ==
# "cmp", per that function's own docstring/caller) must ALSO carry the
# "Value Score (Top 200)"/"_top200_quality" pair - missed on the first
# pass, caught by the full regression sweep (test_director_task26_
# home_country_nan_crash.py's own real AppTest run hit a bare KeyError:
# 'Value Score (Top 200)' here) before this task's own commit.
_saved_scan_src = inspect.getsource(app._comparison_row_from_saved_scan)
check("[D17] _comparison_row_from_saved_scan() also computes and "
      "returns 'Value Score (Top 200)'/'_top200_quality', reusing the "
      "SAME ranking_engine.top200_score_with_quality() helper",
      '"Value Score (Top 200)": (' in _saved_scan_src
      and '"_top200_quality": (' in _saved_scan_src
      and "ranking_engine.top200_score_with_quality(" in _saved_scan_src)


def _run_cmp_saved_scan_row():
    """Exercises the real saved-scan fast path end to end (scan_store ->
    _comparison_row_from_saved_scan()) - the exact path the sweep's own
    AppTest run caught the KeyError on - and proves it no longer raises
    and carries a real Top 200 score."""
    import tempfile
    import scan_store
    _tmpvol = tempfile.mkdtemp(prefix="task62_saved_scan_test_")
    os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = _tmpvol
    scan_store.save_scan("TASK62 FIXTURE", [
        {"Ticker": "WMSFAKE", "Type": "STOCK", "Company Name": "WMS Fake Co",
         "Price": 100.0, "Intrinsic Value": 150.0, "MOS %": 49.0, "Long Score": 55.5,
         "Quality": 61.0, "Psychology": 13.0, "Discovery (lite)": 40.0, "Moat": None},
    ], source_label="test fixture")
    return app._comparison_row_from_saved_scan("WMSFAKE")


_saved_row = _run_cmp_saved_scan_row()
check("[D18] the saved-scan fast path returns a real 'Value Score (Top "
      "200)' (no exception, no None) for a WMS-shaped fixture",
      isinstance(_saved_row["Value Score (Top 200)"], (int, float)))
check("[D18b] ...and it matches the SAME formula directly (Quality 61, "
      "MOS 49, Psychology 13, no live Discovery) - 45.25, same as the "
      "live-loop WMS case in Part A above",
      _saved_row["Value Score (Top 200)"] == 45.25)
check("[D18c] the saved-scan row's own 'Long Score' (55.5, the saved "
      "scan's own figure) is completely untouched by this task",
      _saved_row["Long Score"] == 55.5)


# ======================================================================
# PART E - Deep Dive is completely untouched (the Director's own
# explicit instruction 5).
# ======================================================================
import deep_dive_engine

_dd_src = inspect.getsource(deep_dive_engine)
check("[E1] deep_dive_engine.py's own 'Scanner & Top 200 score' subline "
      "block is present verbatim, unchanged by this task",
      'long_score_lite = ranking_engine.recompute_value_score(_dd_value_score_row, '
      'moat_blend=_dd_moat_blend_live)' in _dd_src)
check("[E2] deep_dive_engine.py never references the new "
      "top200_score_with_quality() helper at all - it keeps its own "
      "existing inline copy of the logic, completely untouched",
      "top200_score_with_quality" not in _dd_src)


print(f"\nTASK 62 RESULTS: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("\nALL TASK 62 (COMPARISON TOP 200 SCORE) FIXTURES PASSED")
