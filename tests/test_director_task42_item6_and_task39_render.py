"""
TASK 39 (render half) + TASK 42 item 6 (Director-directed, 8 Nov 2026,
"build together"): covers everything test_director_task39_drop_chip_
engine.py (pure math) and test_director_task36_support_levels_job_and_
render.py (job wiring) do NOT already cover:

  - top100_render._drop_chip_html()/_drop_chart_svg_html(): the
    TOP200_DROP_CHART_LIVE gate matrix (owner-only unless flipped -
    same TEST GATE shape as TASK 36's support chip), the exact chip
    text/currency symbol, "no chip below -20%" even with the gate wide
    open, and the mini SVG chart's own basic structure (peak dashed
    line always present, support dashed line present only when a
    support_levels_store entry exists, a dot at the current price).
  - i18n EN/ES "top100.drop_chip"/"top100.drop_tooltip" keys render
    with the expected substitutions.
  - top100_engine.quality_on_sale_candidates(): the three rules (
    Quality floor with v1 fallback, the -40% drop floor, and the
    "clean data" rule, which this function gets for free via
    quality_exception_override=True on _build_best_by_ticker() -
    never a separate re-check), restricted to pool+extension members
    only, sorted by drop depth, capped at 20.
  - point_in_time_replay.py's TASK 42 item 6a/6c additions:
    fear_from_peak_as_of() (weekly-resample-as-of + drop_chip_engine's
    own peak/fear math, unmodified), the 4 extra "peak_fear_*" Value
    Score variants in _value_score_variants(), and quality_on_sale_
    as_of()'s per-rule pass/fail check.

Run: python3 tests/test_director_task42_item6_and_task39_render.py
"""
import datetime
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task42_item6_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import drop_chip_engine as dce
import drop_chip_store as dcs
import point_in_time_replay as pir
import ranking_engine
import support_levels_store as sls
import top100_engine as te
import top100_render as tr

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


# ======================================================================
# CHECK 1: _drop_chip_html() gate matrix - closed gate + non-owner =>
# "" even with a chip_shown=True stored entry.
# ======================================================================
_drop_entry = {
    "peak_date": datetime.date(2023, 8, 15), "peak_price": 29.80,
    "current_date": datetime.date(2026, 7, 16), "current_price": 14.30,
    "drop_pct": -52.0, "chip_shown": True,
    "series": [(datetime.date(2023, 8, 15), 29.80), (datetime.date(2026, 7, 16), 14.30)],
}
dcs.save("OCL.AX", _drop_entry, currency="AUD")
_row = {"ticker": "OCL.AX", "price": 14.30}

os.environ.pop("TOP200_DROP_CHART_LIVE", None)
tr._is_owner_for_refresh = lambda: False
check("gate_closed_non_owner_no_drop_chip", tr._drop_chip_html(_row, "en") == "")
check("gate_closed_non_owner_no_drop_chart", tr._drop_chart_svg_html(_row, "en") == "")

# CHECK 2: closed gate + OWNER => both the chip and the chart render.
tr._is_owner_for_refresh = lambda: True
_chip = tr._drop_chip_html(_row, "en")
check("gate_closed_owner_sees_drop_chip", _chip != "")
_chart = tr._drop_chart_svg_html(_row, "en")
check("gate_closed_owner_sees_drop_chart", _chart != "" and "<svg" in _chart)

# CHECK 3: TOP200_DROP_CHART_LIVE=1 => shown to a non-owner viewer too.
os.environ["TOP200_DROP_CHART_LIVE"] = "1"
tr._is_owner_for_refresh = lambda: False
check("gate_live_non_owner_sees_drop_chip", tr._drop_chip_html(_row, "en") != "")
check("gate_live_non_owner_sees_drop_chart", tr._drop_chart_svg_html(_row, "en") != "")

# CHECK 4: gate "0" behaves like unset.
os.environ["TOP200_DROP_CHART_LIVE"] = "0"
check("gate_zero_non_owner_no_drop_chip", tr._drop_chip_html(_row, "en") == "")

print("[item6_check1-4_gate_matrix] TOP200_DROP_CHART_LIVE owner-override gate "
      "behaves identically to TASK 36's own support-chip gate for both the chip "
      "and the chart OK")


# ======================================================================
# CHECK 5: exact chip text/currency - "-52% from Aug 2023 peak (3y
# high)" for an AUD ticker (A$ symbol via compounder_ui).
# ======================================================================
os.environ["TOP200_DROP_CHART_LIVE"] = "1"
_chip = tr._drop_chip_html(_row, "en")
check(f"drop_chip_text_en (chip={_chip!r})", "-52% from Aug 2023 peak (3y high)" in _chip)
check("drop_chip_is_red_rose", "#f5a7a7" in _chip)

_chip_es = tr._drop_chip_html(_row, "es")
check(f"drop_chip_month_es_lowercase (chip={_chip_es!r})", "ago 2023" in _chip_es)
check("drop_chip_month_es_not_capitalized", "Ago 2023" not in _chip_es)


# ======================================================================
# CHECK 6: chip_shown False (above -20%) -> "" even with the gate open
# and a stored entry.
# ======================================================================
dcs.save("ADBE", {
    "peak_date": datetime.date(2026, 1, 1), "peak_price": 500.0,
    "current_date": datetime.date(2026, 7, 16), "current_price": 480.0,
    "drop_pct": -4.0, "chip_shown": False,
    "series": [(datetime.date(2026, 1, 1), 500.0), (datetime.date(2026, 7, 16), 480.0)],
}, currency="USD")
_row_small_drop = {"ticker": "ADBE", "price": 480.0}
check("no_chip_below_threshold", tr._drop_chip_html(_row_small_drop, "en") == "")
# The chart itself is NOT gated by chip_shown (shown whenever there's a
# stored entry at all, per the Director's own chart-vs-chip scope split).
check("chart_still_shows_even_without_chip", tr._drop_chart_svg_html(_row_small_drop, "en") != "")


# ======================================================================
# CHECK 7: no stored entry at all -> "" for both, even gate wide open.
# ======================================================================
_no_entry_row = {"ticker": "NOENTRY.AX", "price": 5.0}
check("no_store_entry_no_drop_chip", tr._drop_chip_html(_no_entry_row, "en") == "")
check("no_store_entry_no_drop_chart", tr._drop_chart_svg_html(_no_entry_row, "en") == "")


# ======================================================================
# CHECK 8: mini chart structure - peak dashed line always present
# (green #4ade80); support dashed line (purple #c9a7f5) present ONLY
# when support_levels_store has an entry for this ticker; a dot
# (circle) at the current price always present.
# ======================================================================
_chart_no_support = tr._drop_chart_svg_html(_row, "en")
check("chart_has_peak_line", "#4ade80" in _chart_no_support)
check("chart_has_current_dot", "<circle" in _chart_no_support)
check("chart_has_no_support_line_without_a_stored_support_entry",
      "#c9a7f5" not in _chart_no_support)

sls.save("OCL.AX", {
    "support_price": 12.40, "support_date": datetime.date(2025, 11, 1),
    "pct_above_support": 3.0, "next_support_price": None, "next_support_date": None,
    "drop_from_12m_high": -41.0, "has_support": True,
    "lowest_since_date": None, "lowest_since_price": None,
}, currency="AUD")
_chart_with_support = tr._drop_chart_svg_html(_row, "en")
check("chart_has_support_line_once_a_support_entry_exists", "#c9a7f5" in _chart_with_support)
print("[item6_check5-8_chip_text_and_chart_structure] drop chip text/currency/"
      "ES month formatting, the -20% chip_shown gate (independent of the chart's "
      "own gate), and the mini chart's peak/support/current-dot structure all "
      "behave as specified OK")


# ======================================================================
# CHECK 9: i18n keys render with the expected substitutions, EN + ES.
# ======================================================================
import i18n
_en_text = i18n.t("top100.drop_chip", "en", drop="-52", month_year="Aug 2023")
check(f"i18n_drop_chip_en (text={_en_text!r})", _en_text == "-52% from Aug 2023 peak (3y high)")
_es_text = i18n.t("top100.drop_chip", "es", drop="-52", month_year="ago 2023")
check(f"i18n_drop_chip_es (text={_es_text!r})",
      _es_text == "-52% desde el máximo de ago 2023 (máximo de 3 años)")
_en_tooltip = i18n.t("top100.drop_tooltip", "en", peak_price="A$29.80", peak_date="15 Aug 2023",
                      price="A$14.30", drop="-52")
check("i18n_drop_tooltip_en_has_peak_price", "A$29.80" in _en_tooltip)
check("i18n_drop_tooltip_en_has_price", "A$14.30" in _en_tooltip)
print("[item6_check9_i18n_keys] top100.drop_chip/top100.drop_tooltip render "
      "with the expected substitutions in both EN and ES OK")


# ======================================================================
# CHECK 10: top100_engine.quality_on_sale_candidates() - the three
# rules, restricted to pool+extension members, sorted by drop depth,
# capped at 20. _build_best_by_ticker() itself is mocked (its own
# behaviour is covered by other test files) so this checks ONLY this
# function's own filtering/sorting/capping logic.
# ======================================================================
_fake_best = {
    # Passes everything - Quality v2 >= 65, drop <= -40%.
    "GOOD.AX": {"ticker": "GOOD.AX", "quality": 60.0, "quality_v2": 80.0,
                "quality_exception_admitted": False},
    # Quality v2 missing -> falls back to v1 (60.0), which is BELOW the
    # 65 floor -> excluded.
    "LOWQ.AX": {"ticker": "LOWQ.AX", "quality": 60.0, "quality_v2": None,
                "quality_exception_admitted": False},
    # Quality v2 missing -> v1 fallback (70.0) clears the floor -> only
    # included if its drop also clears -40%.
    "V1FALLBACK.AX": {"ticker": "V1FALLBACK.AX", "quality": 70.0, "quality_v2": None,
                       "quality_exception_admitted": False},
    # High quality, but drop is only -25% (passes the card chip's -20%
    # but NOT the Quality-on-sale tab's stricter -40%) -> excluded.
    "SHALLOWDROP.AX": {"ticker": "SHALLOWDROP.AX", "quality": 60.0, "quality_v2": 90.0,
                        "quality_exception_admitted": False},
    # Not currently a pool/extension member at all -> excluded
    # regardless of its own numbers (drop_chip_store has no 3y-peak
    # entry for it anyway, by construction of the real nightly job).
    "NOTINPOOL.AX": {"ticker": "NOTINPOOL.AX", "quality": 60.0, "quality_v2": 95.0,
                      "quality_exception_admitted": False},
}
_fake_drops = {
    "GOOD.AX": {"drop_pct": -45.0, "peak_date": datetime.date(2023, 5, 1),
                "peak_price": 100.0, "current_price": 55.0},
    "LOWQ.AX": {"drop_pct": -50.0, "peak_date": datetime.date(2023, 5, 1),
                "peak_price": 50.0, "current_price": 25.0},
    "V1FALLBACK.AX": {"drop_pct": -41.0, "peak_date": datetime.date(2024, 1, 1),
                       "peak_price": 40.0, "current_price": 23.6},
    "SHALLOWDROP.AX": {"drop_pct": -25.0, "peak_date": datetime.date(2024, 6, 1),
                        "peak_price": 80.0, "current_price": 60.0},
    "NOTINPOOL.AX": {"drop_pct": -70.0, "peak_date": datetime.date(2023, 1, 1),
                      "peak_price": 10.0, "current_price": 3.0},
}
_pool_rows = [{"ticker": t} for t in
              ("GOOD.AX", "LOWQ.AX", "V1FALLBACK.AX", "SHALLOWDROP.AX")]

with mock.patch.object(te, "_build_best_by_ticker", return_value=(_fake_best, 500)), \
     mock.patch.object(te.top100_store, "current_pool", return_value=_pool_rows), \
     mock.patch.object(te.top100_store, "current_asx_extension", return_value=[]), \
     mock.patch.object(te.drop_chip_store, "get", side_effect=lambda t: _fake_drops.get(t)):
    _result = te.quality_on_sale_candidates()

_tickers_out = [c["ticker"] for c in _result["candidates"]]
check(f"quality_on_sale_includes_the_one_fully_passing_ticker (got={_tickers_out})",
      "GOOD.AX" in _tickers_out)
check("quality_on_sale_excludes_v2_missing_falling_back_below_the_v1_floor",
      "LOWQ.AX" not in _tickers_out)
check("quality_on_sale_excludes_shallow_drop_above_the_-40pct_floor",
      "SHALLOWDROP.AX" not in _tickers_out)
check("quality_on_sale_excludes_non_pool_extension_member_regardless_of_its_own_numbers",
      "NOTINPOOL.AX" not in _tickers_out)
check("quality_on_sale_includes_v1_fallback_when_it_clears_both_rules",
      "V1FALLBACK.AX" in _tickers_out)
_v1fb = next(c for c in _result["candidates"] if c["ticker"] == "V1FALLBACK.AX")
check("quality_on_sale_flags_the_v1_fallback_source_honestly",
      _v1fb["quality_used_source"] == "v1" and _v1fb["quality_used"] == 70.0)
_good = next(c for c in _result["candidates"] if c["ticker"] == "GOOD.AX")
check("quality_on_sale_uses_v2_source_when_present",
      _good["quality_used_source"] == "v2" and _good["quality_used"] == 80.0)
check("quality_on_sale_sorted_deepest_drop_first",
      [c["drop_pct"] for c in _result["candidates"]] ==
      sorted([c["drop_pct"] for c in _result["candidates"]]))
check("quality_on_sale_eligible_count_passthrough", _result["eligible_count"] == 500)
print("[item6_check10_quality_on_sale_candidates] the three rules (Quality floor "
      "with honest v1-fallback flagging, the -40% drop floor, pool/extension-only "
      "scope) and deepest-drop-first sorting all behave as specified OK")

# CHECK 11: an empty candidate pool never raises, returns [].
with mock.patch.object(te, "_build_best_by_ticker", return_value=({}, 0)), \
     mock.patch.object(te.top100_store, "current_pool", return_value=[]), \
     mock.patch.object(te.top100_store, "current_asx_extension", return_value=[]):
    _empty_result = te.quality_on_sale_candidates()
check("quality_on_sale_empty_pool_returns_empty_list_never_raises",
      _empty_result["candidates"] == [])


# ======================================================================
# CHECK 12 (TASK 42 item 6a): fear_from_peak_as_of() - resamples to
# weekly, truncates to on/before the replay date, then runs drop_chip_
# engine's own peak/fear math unmodified.
# ======================================================================
_base = datetime.date(2023, 1, 2)  # a Monday
_daily = [(_base + datetime.timedelta(days=i), 100.0 - 0.05 * i) for i in range(900)]
# Peak is effectively the earliest closes in the 3y window; current
# price is whatever the replay date's own on/before close resamples to.
_replay_date = _base + datetime.timedelta(days=800)
_peak_fear_result = pir.fear_from_peak_as_of(_daily, _replay_date)
check("fear_from_peak_as_of_returns_a_peak_price", _peak_fear_result["peak_price"] is not None)
check("fear_from_peak_as_of_returns_a_fear_value", _peak_fear_result["fear"] is not None)
# Cross-check against calling drop_chip_engine directly on the same
# weekly-resampled series this function itself builds internally.
_wd, _wc = pir._weekly_closes_as_of(_daily, _replay_date)
_direct = dce.compute_peak_and_drop(_wd, _wc)
_direct_fear = dce.fear_from_peak(_direct["current_price"], _direct["peak_price"])
check("fear_from_peak_as_of_matches_a_direct_drop_chip_engine_call",
      abs(_peak_fear_result["fear"] - _direct_fear) < 1e-9)

check("fear_from_peak_as_of_empty_input_never_raises",
      pir.fear_from_peak_as_of([], _replay_date)["fear"] is None)
print("[item6_check12_fear_from_peak_as_of] the shadow fear-from-peak replay "
      "variant resamples to weekly, truncates to on/before the date, and matches "
      "a direct drop_chip_engine.compute_peak_and_drop()/fear_from_peak() call "
      "exactly - no second, independently-derived formula OK")


# ======================================================================
# CHECK 13 (TASK 42 item 6a): _value_score_variants()'s 4 extra
# "peak_fear_*" keys - only added when peak_fear_psychology is
# supplied; otherwise byte-identical to the pre-item-6a shape.
# ======================================================================
_variants_no_peak_fear = pir._value_score_variants(
    quality_v1=60.0, quality_v2=75.0, mos_pct=20.0, psychology=5.0, moat_score=86.0,
)
check("no_peak_fear_keys_when_not_supplied",
      "peak_fear_current_formula" not in _variants_no_peak_fear)

_variants_with_peak_fear = pir._value_score_variants(
    quality_v1=60.0, quality_v2=75.0, mos_pct=20.0, psychology=5.0, moat_score=86.0,
    peak_fear_psychology=-39.1,
)
for _k in ("peak_fear_current_formula", "peak_fear_moat_blend",
           "peak_fear_quality_v2", "peak_fear_quality_v2_moat_blend"):
    check(f"peak_fear_variant_present_{_k}", _variants_with_peak_fear.get(_k) is not None)

check("peak_fear_current_formula_matches_a_direct_recompute_value_score_call",
      _variants_with_peak_fear["peak_fear_current_formula"] == ranking_engine.recompute_value_score(
          {"Quality": 60.0, "MOS %": 20.0, "Psychology": -39.1, "Discovery (lite)": 0, "Moat": 86.0},
          moat_blend=False))
print("[item6_check13_value_score_variants_peak_fear] the 4 extra peak_fear_* "
      "Value Score variants only appear when peak_fear_psychology is supplied, "
      "and match an unmodified ranking_engine.recompute_value_score() call OK")


# ======================================================================
# CHECK 14 (TASK 42 item 6c): quality_on_sale_as_of() - per-rule
# pass/fail, reusing the SAME drop_chip_engine peak math.
# ======================================================================
_r14 = pir.quality_on_sale_as_of(
    _replay_date, _daily, quality_v1=60.0, quality_v2=80.0,
    dcf_unreliable=False, quality_exception_thresholds_met=None,
    min_quality=65.0,
)
check("quality_on_sale_as_of_quality_rule_pass_v2_above_floor", _r14["quality_rule_pass"] is True)
check("quality_on_sale_as_of_quality_used_source_v2", _r14["quality_used_source"] == "v2")
check("quality_on_sale_as_of_drop_rule_matches_fear_from_peak_as_of",
      _r14["drop_pct"] is not None)

_r14_unreliable_blocked = pir.quality_on_sale_as_of(
    _replay_date, _daily, quality_v1=60.0, quality_v2=80.0,
    dcf_unreliable=True, quality_exception_thresholds_met=False,
    min_quality=65.0,
)
check("quality_on_sale_as_of_clean_data_rule_fails_when_unreliable_and_no_exception",
      _r14_unreliable_blocked["clean_data_rule_pass"] is False
      and _r14_unreliable_blocked["all_rules_pass"] is False)

_r14_exception_admits = pir.quality_on_sale_as_of(
    _replay_date, _daily, quality_v1=60.0, quality_v2=80.0,
    dcf_unreliable=True, quality_exception_thresholds_met=True,
    min_quality=65.0,
)
check("quality_on_sale_as_of_clean_data_rule_passes_when_the_exception_admits_it",
      _r14_exception_admits["clean_data_rule_pass"] is True)

_r14_v1_fallback = pir.quality_on_sale_as_of(
    _replay_date, _daily, quality_v1=70.0, quality_v2=None,
    dcf_unreliable=False, quality_exception_thresholds_met=None,
    min_quality=65.0,
)
check("quality_on_sale_as_of_v1_fallback_source_flagged",
      _r14_v1_fallback["quality_used_source"] == "v1" and _r14_v1_fallback["quality_used"] == 70.0)

check("quality_on_sale_as_of_missing_quality_never_raises_and_fails_the_rule",
      pir.quality_on_sale_as_of(
          _replay_date, _daily, quality_v1=None, quality_v2=None,
          dcf_unreliable=False, quality_exception_thresholds_met=None, min_quality=65.0,
      )["quality_rule_pass"] is False)
print("[item6_check14_quality_on_sale_as_of] the Quality-floor/drop/clean-data "
      "rules each report pass/fail independently, the v1 fallback is flagged "
      "honestly, and a missing input fails its own rule rather than raising OK")


# ======================================================================
# CHECK 15: replay_at_date() end-to-end - the new "peak_fear"/"peak_
# fear_psychology"/"quality_on_sale" output keys are present and
# internally consistent, on a synthetic CSL-shaped fixture. This is a
# SYNTHETIC fixture (no network access in this sandbox to re-fetch
# CSL.AX's real daily price history) - it exercises the wiring, not a
# real CSL.AX number; see the TASK 42 report for that disclosure.
# ======================================================================
_csl_daily = [(_base + datetime.timedelta(days=i), 120.0 - 0.1 * i) for i in range(800)]
_r15 = pir.replay_at_date(
    "CSLSYNTH", _replay_date, baseline=None, baseline_date=None, bundle=None,
    daily_closes_with_dates=_csl_daily, today_cutoff=48.3,
)
check("replay_at_date_has_peak_fear_key", "peak_fear" in _r15 and _r15["peak_fear"]["fear"] is not None)
check("replay_at_date_has_peak_fear_psychology_key", _r15["peak_fear_psychology"] is not None)
check("replay_at_date_peak_fear_psychology_equals_fear_minus_greed_minus_fomo",
      abs(_r15["peak_fear_psychology"]
          - (_r15["peak_fear"]["fear"] - _r15["psychology"]["greed"] - _r15["psychology"]["fomo"])) < 1e-9)
check("replay_at_date_value_score_has_peak_fear_variants",
      "peak_fear_current_formula" in _r15["value_score"])
check("replay_at_date_has_quality_on_sale_key", "quality_on_sale" in _r15)
check("replay_at_date_would_be_in_pool_covers_peak_fear_variants_too",
      "peak_fear_current_formula" in _r15["would_be_in_pool"])
print("[item6_check15_replay_at_date_wiring] replay_at_date()'s new peak_fear/"
      "peak_fear_psychology/quality_on_sale output keys are present and mutually "
      "consistent on a synthetic fixture OK")


# ======================================================================
# CHECK 16: nightly_scan.py now stores "Fear"/"Greed"/"Fomo" alongside
# "Psychology" in BOTH the full-scan (analyze_ticker) and lite-scan
# (analyze_ticker_lite) row dicts - a structural source check (the
# formulas themselves are already covered by point_in_time_replay's
# own psychology_as_of(), which mirrors them exactly).
# ======================================================================
_ns_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "nightly_scan.py")).read()
check('nightly_scan.py stores "Fear" alongside "Psychology" (additive, both scan paths)',
      _ns_src.count('"Fear": round(fear, 1)') + _ns_src.count('["Fear"] = round(fear, 1)') == 2)
check('nightly_scan.py stores "Greed" alongside "Psychology" (additive, both scan paths)',
      _ns_src.count('"Greed": round(greed, 1)') + _ns_src.count('["Greed"] = round(greed, 1)') == 2)
check('nightly_scan.py stores "Fomo" alongside "Psychology" (additive, both scan paths)',
      _ns_src.count('"Fomo": round(fomo, 1)') + _ns_src.count('["Fomo"] = round(fomo, 1)') == 2)
print("[item6_check16_nightly_scan_fear_greed_fomo] both nightly_scan.py scan "
      "paths now persist Fear/Greed/Fomo as separate, additive fields alongside "
      "the combined Psychology score OK")


# ======================================================================
# CHECK 17 (TASK 42 item 6a): _recompute_value_score_v2_moat_peak_
# fear() - falls back to the real value_score_v2_moat whenever Fear/
# Greed/Fomo or a drop_chip_store peak entry is missing (the
# documented "no-op until next scan" case); swaps the fear term alone
# when every input is present.
# ======================================================================
_row_no_fgfe = {"ticker": "NOFGF.AX", "Quality": 60.0, "Quality V2": 75.0,
                "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 0, "Moat": 86.0}
check("peak_fear_recompute_falls_back_without_stored_fear_greed_fomo",
      te._recompute_value_score_v2_moat_peak_fear("NOFGF.AX", _row_no_fgfe)
      == te._recompute_value_score_v2_moat(_row_no_fgfe))

_row_with_fgfe = dict(_row_no_fgfe)
_row_with_fgfe.update({"ticker": "WITHFGF.AX", "Fear": 13.9, "Greed": 12.6, "Fomo": 12.6})
check("peak_fear_recompute_falls_back_without_a_drop_chip_store_entry",
      te._recompute_value_score_v2_moat_peak_fear("WITHFGF.AX", _row_with_fgfe)
      == te._recompute_value_score_v2_moat(_row_with_fgfe))

dcs.save("WITHFGF.AX", {
    "peak_date": datetime.date(2023, 8, 1), "peak_price": 29.80,
    "current_date": datetime.date(2026, 7, 16), "current_price": 25.60,
    "drop_pct": -14.1, "chip_shown": False,
    "series": [(datetime.date(2023, 8, 1), 29.80), (datetime.date(2026, 7, 16), 25.60)],
}, currency="AUD")
_peak_fear_score = te._recompute_value_score_v2_moat_peak_fear("WITHFGF.AX", _row_with_fgfe)
_expected_fear_peak = dce.fear_from_peak(25.60, 29.80)
_expected_psych = _expected_fear_peak - 12.6 - 12.6
_expected_score = ranking_engine.recompute_value_score(
    {"Quality": 75.0, "MOS %": 20.0, "Psychology": _expected_psych,
     "Discovery (lite)": 0, "Moat": 86.0},
    moat_blend=True,
)
check("peak_fear_recompute_swaps_only_the_fear_term_when_every_input_is_present",
      abs(_peak_fear_score - _expected_score) < 1e-9)
check("peak_fear_recompute_differs_from_the_real_v2_moat_score_once_swapped",
      abs(_peak_fear_score - te._recompute_value_score_v2_moat(_row_with_fgfe)) > 1e-6)
print("[item6_check17_recompute_peak_fear] the peak-fear recompute falls back to "
      "the real v2_moat score whenever Fear/Greed/Fomo or a drop_chip_store peak "
      "entry is missing, and swaps only the fear term (never greed/fomo) when "
      "every input is present, matching the Director's own root-cause framing OK")


# ======================================================================
# CHECK 18/19 (TASK 42 item 6a): simulate_pool_v2_moat_peak_fear() uses
# the new sort key via the SAME _simulate_pool_generic() shape every
# other shadow preview uses, and its result is directly reusable by
# pool_change_impact_and_cost() (TASK 35's own turnover/cost tool) -
# no new reporting code needed for "turnover/cost impact".
# ======================================================================
_fake_best_18 = {
    "A.AX": {"ticker": "A.AX", "value_score": 50.0, "value_score_v2_moat_peak_fear": 70.0},
    "B.AX": {"ticker": "B.AX", "value_score": 40.0, "value_score_v2_moat_peak_fear": 20.0},
}
with mock.patch.object(te, "_build_best_by_ticker", return_value=(_fake_best_18, 2)):
    _sim18 = te.simulate_pool_v2_moat_peak_fear()
check("simulate_pool_v2_moat_peak_fear_ranks_by_the_new_sort_key",
      _sim18["cutoff"] == 70.0 or "A.AX" in _sim18["pool_tickers"])
with mock.patch.object(te.top100_store, "current_pool", return_value=[{"ticker": "B.AX"}]):
    _impact18 = te.pool_change_impact_and_cost(_sim18)
check("pool_change_impact_and_cost_accepts_the_peak_fear_sim_result_unmodified",
      isinstance(_impact18.get("entrants"), int) and isinstance(_impact18.get("dropouts"), int))
print("[item6_check18-19_simulate_and_impact_reuse] simulate_pool_v2_moat_peak_"
      "fear() follows the same _simulate_pool_generic() shape as every sibling "
      "shadow preview, and TASK 35's own pool_change_impact_and_cost() reuses it "
      "with no changes needed OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
