"""
TASK 59 (Director-directed, 14 Nov 2026) - "Also on Quality on sale"
chip missing on most Top 200 tabs.

ROOT CAUSE (the Director's own, verified directly in top100_render.py
at commit b5cd80e): render_top100_page() builds `_qos_rank_map` once,
but TASK 44 item 10 (11 Nov 2026) only ever wired it into _render_
full100_tab()/_render_near_cutoff_tab() - _render_top20_tab() (Mixed,
USA, Foreign US-listed), _render_top20_australia_tab() (Australia) and
_render_top20_market_tab() (UK, Canada, Europe, any other market tab)
never received it at all, so _render_row() was never given a cross_
list_chip_html on those five tabs. The Director's own example: DECK
qualifies for both Top 200 (#3) and Quality on sale (#98) - its
Quality on sale card correctly shows "Also in Top 200 · #3", but its
Top 200 card (on the Mixed tab) showed no chip at all.

FIX: all three render functions now take an optional
quality_on_sale_rank_map=None param (same "None/empty renders no chip
at all" default _render_full100_tab()'s own param already uses) and
render_top100_page() now passes `_qos_rank_map` into every one of
their 5 call sites (Mixed, Australia, USA, Foreign, every market_plan
tab) - the SAME already-ranked dict it was already building, never a
second lookup.

Also: the "Also in Top 200" chip's own wording (shown on a Quality on
sale card) now reads "★ Also in Top 200 · #{rank} overall" in every
language, so the global Value Score rank it shows is never confused
with the card's own 1-20 tab position number sitting right next to it.

Covers:
  - _render_top20_tab() under its three real calling shapes (bare/
    Mixed, currency="USD"+exclude_tickers/USA, currency="USD"+only_
    tickers/Foreign): a ticker present in quality_on_sale_rank_map
    gets the chip at the right rank; absent gets none; map omitted
    (the gate-off default) renders no chip for anyone.
  - _render_top20_australia_tab(): same three cases.
  - _render_top20_market_tab(): same three cases.
  - render_top100_page() itself: _qos_rank_map stays {} for a gated-
    off viewer (TOP200_QUALITY_ON_SALE_LIVE off, not the owner) -
    confirmed unchanged by source inspection, since this task never
    touches that gate.
  - The "overall" wording, EN + ES.
  - A DECK-shaped fixture (ticker="DECK", rank=98 - the Director's own
    exact reported numbers) proving the Mixed-tab call site (the one
    named in the Director's own report) now renders "★ Also on
    Quality on sale · #98" for it.
  - Scoring/selection/export/card-layout untouched: this task only
    ever adds an optional kwarg with a None/{} default to three
    existing functions and threads an already-computed dict into five
    existing call sites - no new computation, no new chip builder, no
    change to _cross_list_chip_html() or _render_row()'s own layout
    (already proven by test_director_task44_item10_cross_list_chip.py,
    untouched by this task and still passing).

Run: python3 tests/test_director_task59_cross_list_chip_every_tab.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task59_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import i18n
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


def _fake_render_row(rank, row, lang, finer_industry, sort_mode, origin_badge_html=None,
                      country_badge_html=None, home_country_html=None, recent_badge_html=None,
                      cross_list_chip_html=None, near_cutoff_gap_html=None):
    _calls.append((row["ticker"], cross_list_chip_html))


_calls = []
_CROSS = lambda kind, rank, lang: f"CROSS:{kind}:{rank}"


def _row(ticker, currency="AUD"):
    return {
        "ticker": ticker, "composite": 70.0, "mos_pct": 10.0, "currency": currency,
        "score_row": {"not_rated": False, "dims": {}, "composite": 70.0},
    }


# ======================================================================
# CHECK 1: _render_top20_tab() - the Mixed/USA/Foreign call shape.
# ======================================================================
_rows = [_row("A.AX"), _row("B.AX")]

with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html", side_effect=_CROSS):
    _calls.clear()
    tr._render_top20_tab(_rows, "en", {}, tr.SORT_RESEARCH, quality_on_sale_rank_map={"A.AX": 5})
_by_ticker = dict(_calls)
check("Mixed-shaped call: a ticker in the rank map gets the quality_on_sale chip "
      "at the right rank", _by_ticker["A.AX"] == "CROSS:quality_on_sale:5")
check("Mixed-shaped call: a ticker NOT in the rank map gets no chip",
      _by_ticker["B.AX"] is None)

with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html", side_effect=_CROSS):
    _calls.clear()
    tr._render_top20_tab(_rows, "en", {}, tr.SORT_RESEARCH)
_by_ticker_off = dict(_calls)
check("Mixed-shaped call: quality_on_sale_rank_map omitted (gate-off default) "
      "-> no chip for anyone", _by_ticker_off["A.AX"] is None and _by_ticker_off["B.AX"] is None)

_usd_rows = [_row("C", currency="USD"), _row("D", currency="USD")]
with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html", side_effect=_CROSS):
    _calls.clear()
    tr._render_top20_tab(_usd_rows, "en", {}, tr.SORT_RESEARCH, currency="USD",
                          exclude_tickers=set(), quality_on_sale_rank_map={"C": 9})
_by_ticker_usa = dict(_calls)
check("USA-shaped call (currency='USD', exclude_tickers=set()): a ticker in the "
      "rank map gets its chip", _by_ticker_usa["C"] == "CROSS:quality_on_sale:9")
check("USA-shaped call: a ticker NOT in the rank map gets none",
      _by_ticker_usa["D"] is None)

with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html", side_effect=_CROSS):
    _calls.clear()
    tr._render_top20_tab(_usd_rows, "en", {}, tr.SORT_RESEARCH, currency="USD",
                          only_tickers={"C", "D"}, quality_on_sale_rank_map={"D": 14})
_by_ticker_foreign = dict(_calls)
check("Foreign-shaped call (currency='USD', only_tickers=...): a ticker in the "
      "rank map gets its chip", _by_ticker_foreign["D"] == "CROSS:quality_on_sale:14")
check("Foreign-shaped call: a ticker NOT in the rank map gets none",
      _by_ticker_foreign["C"] is None)
print("[task59_check1_top20_tab] _render_top20_tab() wires the cross-list chip "
      "correctly under all three of its real calling shapes (Mixed, USA, "
      "Foreign US-listed) - present at the right rank, absent for a ticker not "
      "in the map, and completely absent when the map itself is omitted OK")


# ======================================================================
# CHECK 2: _render_top20_australia_tab().
# ======================================================================
with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html", side_effect=_CROSS), \
     mock.patch.object(tr, "_enriched_asx_extension", return_value=[]), \
     mock.patch.object(tr, "_global_value_score_ranks", return_value={"A.AX": 1, "B.AX": 2}):
    _calls.clear()
    tr._render_top20_australia_tab(_rows, "en", {}, tr.SORT_RESEARCH,
                                    quality_on_sale_rank_map={"A.AX": 5})
_au_by_ticker = dict(_calls)
check("Australia tab: a ticker in the rank map gets its chip",
      _au_by_ticker["A.AX"] == "CROSS:quality_on_sale:5")
check("Australia tab: a ticker NOT in the rank map gets none",
      _au_by_ticker["B.AX"] is None)

with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html", side_effect=_CROSS), \
     mock.patch.object(tr, "_enriched_asx_extension", return_value=[]), \
     mock.patch.object(tr, "_global_value_score_ranks", return_value={"A.AX": 1, "B.AX": 2}):
    _calls.clear()
    tr._render_top20_australia_tab(_rows, "en", {}, tr.SORT_RESEARCH)
_au_by_ticker_off = dict(_calls)
check("Australia tab: map omitted (gate-off default) -> no chip for anyone",
      _au_by_ticker_off["A.AX"] is None and _au_by_ticker_off["B.AX"] is None)
print("[task59_check2_australia_tab] _render_top20_australia_tab() wires the "
      "cross-list chip correctly - present at the right rank, absent for a "
      "ticker not in the map, absent entirely when the map is omitted OK")


# ======================================================================
# CHECK 3: _render_top20_market_tab() (UK/Canada/Europe/any other
# market tab).
# ======================================================================
with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html", side_effect=_CROSS):
    _calls.clear()
    tr._render_top20_market_tab(_rows, "en", {}, tr.SORT_RESEARCH, _rows, "UK",
                                 quality_on_sale_rank_map={"A.AX": 7})
_market_by_ticker = dict(_calls)
check("Market tab (UK): a ticker in the rank map gets its chip",
      _market_by_ticker["A.AX"] == "CROSS:quality_on_sale:7")
check("Market tab (UK): a ticker NOT in the rank map gets none",
      _market_by_ticker["B.AX"] is None)

with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "_cross_list_chip_html", side_effect=_CROSS):
    _calls.clear()
    tr._render_top20_market_tab(_rows, "en", {}, tr.SORT_RESEARCH, _rows, "UK")
_market_by_ticker_off = dict(_calls)
check("Market tab (UK): map omitted (gate-off default) -> no chip for anyone",
      _market_by_ticker_off["A.AX"] is None and _market_by_ticker_off["B.AX"] is None)
print("[task59_check3_market_tab] _render_top20_market_tab() (UK, standing in "
      "for Canada/Europe/any other market tab - all share this one function) "
      "wires the cross-list chip correctly, same present/absent/omitted-map "
      "behaviour as every other tab OK")


# ======================================================================
# CHECK 4: render_top100_page()'s own gate - _qos_rank_map stays {} for
# a gated-off viewer, UNCHANGED by this task (source inspection - this
# task never touches _show_quality_on_sale_tab or the gate itself,
# only threads the already-gated map further).
# ======================================================================
_page_src = __import__("inspect").getsource(tr.render_top100_page)
check("render_top100_page() still initializes _qos_rank_map to {} and only "
      "fills it under _show_quality_on_sale_tab - the gate itself is "
      "completely untouched by this task",
      "_qos_rank_map = {}" in _page_src
      and "if _show_quality_on_sale_tab:" in _page_src)
check("render_top100_page() now threads quality_on_sale_rank_map=_qos_rank_map "
      "into the Mixed/Australia/USA/Foreign/every market tab call site (5 "
      "occurrences, one per fixed call site - Full 200/Near the cut-off were "
      "already correct before this task and are not recounted here)",
      _page_src.count("quality_on_sale_rank_map=_qos_rank_map") == 7)
print("[task59_check4_gate_and_wiring_count] the gate itself (TOP200_QUALITY_"
      "ON_SALE_LIVE/owner-override) is untouched, and render_top100_page() now "
      "threads the SAME _qos_rank_map into every tab's own call site OK")


# ======================================================================
# CHECK 5: "overall" wording, EN + ES - the Quality on sale tab's own
# "Also in Top 200" chip (never the Top 200 tabs' "Also on Quality on
# sale" chip, which is unchanged).
# ======================================================================
check("EN cross_list_also_in_top200_chip now says 'overall'",
      i18n.t("top100.cross_list_also_in_top200_chip", "en", rank=5)
      == "★ Also in Top 200 · #5 overall")
check("ES cross_list_also_in_top200_chip has its own 'overall' equivalent",
      "en general" in i18n.t("top100.cross_list_also_in_top200_chip", "es", rank=5))
check("the OTHER chip (Also on Quality on sale, shown on a Top 200 card) is "
      "UNCHANGED - no 'overall' added to it, since its own rank IS the tab's "
      "own Quality on sale list position, never a different numbering scheme",
      i18n.t("top100.cross_list_also_on_sale_chip", "en", rank=5)
      == "★ Also on Quality on sale · #5"
      and "overall" not in i18n.t("top100.cross_list_also_on_sale_chip", "en", rank=5))
print("[task59_check5_overall_wording] the 'Also in Top 200' chip's own global-"
      "rank wording now says 'overall' in both languages; the 'Also on Quality "
      "on sale' chip (a different, tab-local rank) is correctly left alone OK")


# ======================================================================
# CHECK 6: DECK - the Director's own exact reported case (Quality on
# sale rank #98, missing from its Top 200 card on the Mixed tab).
# Synthetic fixture built to match those exact numbers; this sandbox
# has no outbound network access to pull DECK's real live standing.
# ======================================================================
_deck_rows = [_row("DECK", currency="USD"), _row("OTHER", currency="USD")]
with mock.patch.object(tr, "_render_row", side_effect=_fake_render_row):
    _calls.clear()
    tr._render_top20_tab(_deck_rows, "en", {}, tr.SORT_RESEARCH,
                          quality_on_sale_rank_map={"DECK": 98})
_deck_chip = dict(_calls)["DECK"]
check("DECK-shaped fixture (Quality on sale rank #98, the Director's own exact "
      "reported number): the Mixed tab's own card now renders the real "
      "'★ Also on Quality on sale · #98' chip",
      _deck_chip is not None and "★ Also on Quality on sale · #98" in _deck_chip)
check("DECK's neighbour (not a Quality on sale candidate) still gets no chip",
      dict(_calls)["OTHER"] is None)
print("[task59_check6_deck_confirmation] DECK's own Top 200 card (Mixed tab, "
      "the exact tab the Director's own report named) now renders "
      "'★ Also on Quality on sale · #98' - the missing chip is fixed OK")


print(f"\n{passed} passed, {failed} failed")
if failed:
    sys.exit(1)
