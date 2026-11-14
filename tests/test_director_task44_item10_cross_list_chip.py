"""
TASK 44 item 10 (Director-directed, 11 Nov 2026): cross-list chip -
"★ Also on Quality on sale · #<rank>" on a Top 200 card whose ticker
also qualifies for "Quality on sale", and "★ Also in Top 200 · #<rank>"
on a "Quality on sale" card whose ticker is a current Top 200 pool
member.

Covers:
  - top100_render._cross_list_chip_html(): correct text/rank per
    `kind`, and a colour distinct from both the support chip (purple)
    and the drop chip (red/rose).
  - top100_render._render_row(): the chip lands on row 3 (the
    support/drop row), strictly AFTER the drop chip, inside the SAME
    flex div - never a separate row, never touching rows 1-2 - and can
    appear on its own (no support/drop chip) without a stray empty
    row either way.
  - top100_render._render_full100_tab(): a ticker present in
    `quality_on_sale_rank_map` gets the "quality_on_sale" chip at the
    right rank; a ticker absent gets none; an empty/missing map (the
    gate-off default) renders no chip for anyone, byte-identical to
    before this task.
  - top100_render._render_quality_on_sale_tab(): the mirror wiring via
    `top_200_rank_map`, plus the `candidates_result` passthrough (so
    render_top100_page() never computes quality_on_sale_candidates()
    twice per page render).
  - i18n EN/ES "top100.cross_list_also_on_sale_chip"/"cross_list_
    also_in_top200_chip" keys render with the expected rank
    substitution.

Run: python3 tests/test_director_task44_item10_cross_list_chip.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task44_item10_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import i18n
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
# CHECK 1: _cross_list_chip_html() - correct text/rank per `kind`, and
# a colour distinct from both _SHELF_CHIP_SUPPORT (purple) and
# _SHELF_CHIP_DROP (red/rose).
# ======================================================================
_qos_chip = tr._cross_list_chip_html("quality_on_sale", 7, "en")
check("quality_on_sale chip: contains the rank", "#7" in _qos_chip)
check("quality_on_sale chip: wording matches the Director's own spec",
      "Also on Quality on sale" in _qos_chip)

_top200_chip = tr._cross_list_chip_html("top_200", 3, "en")
check("top_200 chip: contains the rank", "#3" in _top200_chip)
check("top_200 chip: wording matches the Director's own spec",
      "Also in Top 200" in _top200_chip)

check("cross-list chip colour is distinct from the support chip's own colour",
      tr._SHELF_CHIP_CROSS_LIST != tr._SHELF_CHIP_SUPPORT)
check("cross-list chip colour is distinct from the drop chip's own colour",
      tr._SHELF_CHIP_CROSS_LIST != tr._SHELF_CHIP_DROP)
print("[task44_item10_check1_chip_html] _cross_list_chip_html() renders the "
      "right text/rank for each kind, in a colour distinct from both the "
      "support and drop chips OK")


# ======================================================================
# CHECK 2: EN/ES i18n keys exist with the expected rank substitution.
# ======================================================================
check("EN cross_list_also_on_sale_chip substitutes {rank}",
      i18n.t("top100.cross_list_also_on_sale_chip", "en", rank=12) == "★ Also on Quality on sale · #12")
# TASK 59 (14 Nov 2026) added " overall" to this one wording - see that
# task's own test file for the full reasoning (disambiguates this
# global Value Score rank from the tab's own 1-20 position number).
check("EN cross_list_also_in_top200_chip substitutes {rank}",
      i18n.t("top100.cross_list_also_in_top200_chip", "en", rank=5) == "★ Also in Top 200 · #5 overall")
check("ES cross_list_also_on_sale_chip is present and substitutes {rank}",
      "12" in i18n.t("top100.cross_list_also_on_sale_chip", "es", rank=12))
check("ES cross_list_also_in_top200_chip is present and substitutes {rank}",
      "5" in i18n.t("top100.cross_list_also_in_top200_chip", "es", rank=5))
print("[task44_item10_check2_i18n] EN/ES keys exist and substitute the rank correctly OK")


# ======================================================================
# CHECK 3: _render_row() placement - the cross-list chip sits on row 3
# (the support/drop row), AFTER the drop chip, inside the SAME flex
# div as the support/drop chips (never a separate row of its own).
# ======================================================================
_captured_markdown = []
tr.st.markdown = lambda html, unsafe_allow_html=True: _captured_markdown.append(html)
_SUPPORT_MARKER = "##SUPPORT_MARKER##"
_DROP_MARKER = "##DROP_MARKER##"
_CROSS_MARKER = "##CROSS_MARKER##"
tr._support_chip_html = lambda row, lang: _SUPPORT_MARKER
tr._drop_chip_html = lambda row, lang: _DROP_MARKER
tr._drop_chart_svg_html = lambda row, lang: ""
tr._quality_exception_chip_html = lambda row, lang: ""
tr._dcf_unreliable_chip_html = lambda row, lang: ""
tr._stale_valuation_chip_html = lambda row, lang: ""
tr._tradability_spread_pct = lambda ticker: None
tr._sentiment_chip_html = lambda psychology, lang: ""

_dims = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
_row = {
    "ticker": "WMS", "company_name": "WMS Co", "sector": "Industrials",
    "value_score": 55.0, "mos_pct": 20.0, "price": 10.0, "intrinsic_value": 12.5,
    "currency": "AUD", "psychology": 5.0, "dividend_yield_pct": 2.0,
    "composite": 70.0,
    "score_row": {"composite": 70.0, "dims": _dims, "not_rated": False},
}
tr._render_row(1, _row, "en", {}, tr.SORT_RESEARCH, cross_list_chip_html=_CROSS_MARKER)

_html = _captured_markdown[0]
check("cross-list marker is present in the card", _CROSS_MARKER in _html)
_first_div_close = _html.index("</div>", _html.index(
    "display:flex;flex-wrap:wrap;align-items:center;gap:12px;'>"))
check("cross-list marker sits AFTER the row-1/row-2 div's own closing tag "
      "(never touches rows 1-2)",
      _first_div_close < _html.index(_CROSS_MARKER))
check("cross-list marker sits strictly AFTER the drop chip marker (per the "
      "Director's own 'after the drop chip' placement rule)",
      _html.index(_DROP_MARKER) < _html.index(_CROSS_MARKER))
check("cross-list marker sits strictly AFTER the support chip marker too",
      _html.index(_SUPPORT_MARKER) < _html.index(_CROSS_MARKER))
# All three (support, drop, cross-list) must be inside the SAME row-3 flex
# div, not three separate rows - i.e. no "</div>" between the drop marker
# and the cross-list marker.
_between = _html[_html.index(_DROP_MARKER):_html.index(_CROSS_MARKER)]
check("no div closes between the drop chip and the cross-list chip - all "
      "three chips share row 3's own single flex container",
      "</div>" not in _between)
print("[task44_item10_check3_row3_placement] the cross-list chip renders on "
      "row 3, strictly after the drop chip, inside the SAME flex container as "
      "the support/drop chips - rows 1-2 are completely untouched OK")

# The cross-list chip can appear ALONE (no support/drop chip for this
# ticker) without a stray empty row either way - its own TOP200_
# QUALITY_ON_SALE_LIVE gate is independent of TOP200_SUPPORT_CHIP_LIVE/
# TOP200_DROP_CHART_LIVE.
tr._support_chip_html = lambda row, lang: ""
tr._drop_chip_html = lambda row, lang: ""
_captured_markdown.clear()
tr._render_row(1, _row, "en", {}, tr.SORT_RESEARCH, cross_list_chip_html=_CROSS_MARKER)
check("cross-list chip alone (no support/drop) still renders its own row-3 div",
      "margin-top:6px;'>" + _CROSS_MARKER in _captured_markdown[0])

# And: with NOTHING at all (support/drop/cross-list all empty), no stray
# empty div is added - byte-identical to every row before TASK 36/39/44.
_captured_markdown.clear()
tr._render_row(1, _row, "en", {}, tr.SORT_RESEARCH, cross_list_chip_html=None)
check("no support/drop/cross-list chip at all -> no stray empty row-3 div "
      "added, exactly as before this task",
      "margin-top:6px;'></div>" not in _captured_markdown[0])
print("[task44_item10_check3b_chip_independent] the cross-list chip can "
      "appear alone, and omitting all three row-3 chips still renders no "
      "stray empty row OK")


# ======================================================================
# CHECK 4: _render_full100_tab() wiring - a ticker present in
# `quality_on_sale_rank_map` gets the "quality_on_sale" cross-list chip
# at the right rank; an absent ticker gets none; an empty/missing map
# renders no chip for anyone (the gate-off default, byte-identical to
# before this task).
# ======================================================================
_full100_rows = [
    {"ticker": "A.AX", "composite": 70.0, "mos_pct": 10.0,
     "score_row": {"not_rated": False, "dims": {}, "composite": 70.0}},
    {"ticker": "B.AX", "composite": 60.0, "mos_pct": 10.0,
     "score_row": {"not_rated": False, "dims": {}, "composite": 60.0}},
]
_render_calls = []


def _fake_render_row(rank, row, lang, finer_industry, sort_mode, recent_badge_html=None,
                      cross_list_chip_html=None):
    _render_calls.append((row["ticker"], cross_list_chip_html))


with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html",
                        side_effect=lambda kind, rank, lang: f"CROSS:{kind}:{rank}"), \
     mock.patch.object(te, "is_backfill_live", return_value=False):
    _render_calls.clear()
    tr._render_full100_tab(_full100_rows, "en", {}, tr.SORT_RESEARCH,
                            quality_on_sale_rank_map={"A.AX": 5})
_calls_by_ticker = dict(_render_calls)
check("Full 200 tab: a ticker in the rank map gets the quality_on_sale chip "
      "at the right rank",
      _calls_by_ticker["A.AX"] == "CROSS:quality_on_sale:5")
check("Full 200 tab: a ticker NOT in the rank map gets no cross-list chip",
      _calls_by_ticker["B.AX"] is None)

with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html",
                        side_effect=lambda kind, rank, lang: f"CROSS:{kind}:{rank}"), \
     mock.patch.object(te, "is_backfill_live", return_value=False):
    _render_calls.clear()
    tr._render_full100_tab(_full100_rows, "en", {}, tr.SORT_RESEARCH)
_calls_by_ticker_gate_off = dict(_render_calls)
check("Full 200 tab: no quality_on_sale_rank_map passed at all (the gate-off "
      "default) -> no cross-list chip for anyone, byte-identical to before "
      "this task",
      _calls_by_ticker_gate_off["A.AX"] is None and _calls_by_ticker_gate_off["B.AX"] is None)
print("[task44_item10_check4_full100_wiring] _render_full100_tab()'s own "
      "quality_on_sale_rank_map wiring adds the chip only for a ticker "
      "present in the map, at the right rank, and adds nothing at all when "
      "the map is empty/missing OK")


# ======================================================================
# CHECK 5: _render_quality_on_sale_tab() wiring - the mirror case via
# `top_200_rank_map`, plus the `candidates_result` passthrough (no
# second quality_on_sale_candidates() call when the caller already has
# one).
# ======================================================================
_qos_candidates = [
    {"ticker": "C.AX", "research_score": 90.0},
    {"ticker": "D.AX", "research_score": 80.0},
]
_qos_rows_by_ticker = {
    "C.AX": {"ticker": "C.AX", "score_row": {"not_rated": False, "dims": {}}, "composite": 90.0},
    "D.AX": {"ticker": "D.AX", "score_row": {"not_rated": False, "dims": {}}, "composite": 80.0},
}
_qos_render_calls = []


def _fake_render_row_qos(rank, row, lang, finer_industry, sort_mode, recent_badge_html=None,
                          cross_list_chip_html=None):
    _qos_render_calls.append((row["ticker"], cross_list_chip_html))


_candidates_calls = []


def _tracking_quality_on_sale_candidates(log=print):
    _candidates_calls.append(1)
    return {"candidates": _qos_candidates}


with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row_qos), \
     mock.patch.object(tr, "_cross_list_chip_html",
                        side_effect=lambda kind, rank, lang: f"CROSS:{kind}:{rank}"), \
     mock.patch.object(tr, "_quality_on_sale_candidate_rows", return_value=_qos_rows_by_ticker), \
     mock.patch.object(te, "quality_on_sale_candidates", side_effect=_tracking_quality_on_sale_candidates):
    _qos_render_calls.clear()
    _candidates_calls.clear()
    tr._render_quality_on_sale_tab(
        "en", {}, tr.SORT_RESEARCH, top_200_rank_map={"C.AX": 42},
        candidates_result={"candidates": _qos_candidates},
    )
_qos_calls_by_ticker = dict(_qos_render_calls)
check("Quality on sale tab: a ticker in the top_200_rank_map gets the "
      "top_200 chip at the right rank",
      _qos_calls_by_ticker["C.AX"] == "CROSS:top_200:42")
check("Quality on sale tab: a ticker NOT in the top_200_rank_map gets no "
      "cross-list chip",
      _qos_calls_by_ticker["D.AX"] is None)
check("Quality on sale tab: passing candidates_result means quality_on_sale_"
      "candidates() is never called a second time",
      len(_candidates_calls) == 0)

with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row_qos), \
     mock.patch.object(tr, "_cross_list_chip_html",
                        side_effect=lambda kind, rank, lang: f"CROSS:{kind}:{rank}"), \
     mock.patch.object(tr, "_quality_on_sale_candidate_rows", return_value=_qos_rows_by_ticker), \
     mock.patch.object(te, "quality_on_sale_candidates", side_effect=_tracking_quality_on_sale_candidates):
    _qos_render_calls.clear()
    _candidates_calls.clear()
    tr._render_quality_on_sale_tab("en", {}, tr.SORT_RESEARCH)
_qos_calls_gate_off = dict(_qos_render_calls)
check("Quality on sale tab: no top_200_rank_map/candidates_result passed at "
      "all -> no cross-list chip for anyone, AND quality_on_sale_candidates() "
      "is called itself exactly as before this task",
      _qos_calls_gate_off["C.AX"] is None and _qos_calls_gate_off["D.AX"] is None
      and len(_candidates_calls) == 1)
print("[task44_item10_check5_quality_on_sale_wiring] _render_quality_on_sale_"
      "tab()'s own top_200_rank_map wiring adds the chip only for a ticker "
      "present in the map, at the right rank, reuses a passed-in "
      "candidates_result instead of recomputing it, and falls back to "
      "computing everything itself (today's behaviour) when neither is "
      "passed OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
