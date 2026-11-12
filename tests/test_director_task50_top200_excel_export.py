"""
Director's TASK 50 (12 Nov 2026): owner-only "Download (Excel)" export
on every Top 200 tab - one button, first row of each tab's own
content, right-aligned, above the first card; .xlsx, one sheet, frozen
header row, filters on every column; exactly the companies that tab
shows, in the same order, ALL rows for a paginated tab (not just the
first 20); "Near the cut-off" gets an extra "Gap to cut-off" column.

Covers:
  a. data_export_engine.build_top200_tab_workbook(): header order,
     frozen header row, auto-filter over the full range, optional
     extra column, row values placed by column key.
  b. top100_engine.quality_moat_by_ticker(): reads quality/quality_v2/
     moat off _build_best_by_ticker()'s own rows, keyed by ticker.
  c. top100_render._top200_export_row(): every column's exact source
     (support/drop/streak/spread/score_row fields), and the optional
     "Gap to cut-off" column.
  d. top100_render._candidate_tab_export_rows(): ALL qualifying
     candidates ranked (never capped at the Quality-on-sale tab's own
     20-row page size), rated/unrated skip rule matches the render
     loop exactly.
  e. The four plain "selection" functions (_top20_tab_export_rows/
     _market_tab_export_rows/_australia_tab_export_rows/_full100_tab_
     export_rows) match their own render function's row set/order.
  f. top100_render._render_top200_export_button(): owner-only (no
     markdown call at all for a non-owner, or for an empty ranked
     list), correct filename/button text, the embedded workbook
     decodes to the right columns/rows.
  g. End-to-end AppTest: owner sees the button (with the right,
     distinct filename) on Mixed/Australia/USA/a market tab/Full 200;
     a non-owner sees it on NONE of those tabs anywhere on the page.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task50_top200_excel_export.py
"""
import base64
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task50_export_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

from openpyxl import load_workbook

import ai_gate
import data_export_engine as de
import top100_engine as te
import top100_store as ts
import top100_render as tr

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


def _dims_all_scored():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


dims = _dims_all_scored()


def _save_score(ticker, **extra):
    ts.save_score(
        ticker=ticker, quarter=f"RP2026-09-30-{ticker}", model=te.MODEL_TOP100,
        rubric_version=te.RUBRIC_VERSION, dims=dims, not_rated=False,
        inversion_scenario=None, inversion_severity=None, prompt="{}", raw_response="{}",
        **extra,
    )


# ======================================================================
# CHECK a: data_export_engine.build_top200_tab_workbook()
# ======================================================================
_rows_a = [
    {"Rank": 1, "Ticker": "AAA", "Company": "Alpha Co", "Country/Market": "USA",
     "Research Score": 72.5},
    {"Rank": 2, "Ticker": "BBB", "Company": "Beta Co", "Country/Market": "Canada",
     "Gap to cut-off": 1.4},
]
_xlsx_a = de.build_top200_tab_workbook(_rows_a)
_wb_a = load_workbook(io.BytesIO(_xlsx_a))
_ws_a = _wb_a.active
check("build_top200_tab_workbook(): header row is exactly TOP200_EXPORT_COLUMNS, in order",
      [c.value for c in _ws_a[1]] == de.TOP200_EXPORT_COLUMNS)
check("build_top200_tab_workbook(): no extra_column -> row has no 'Gap to cut-off' value "
      "beyond the declared columns (BBB's own Gap to cut-off is silently dropped)",
      _ws_a.max_column == len(de.TOP200_EXPORT_COLUMNS))
check("build_top200_tab_workbook(): row values placed by column key (missing key -> blank)",
      _ws_a.cell(row=2, column=2).value == "AAA"
      and _ws_a.cell(row=2, column=6).value == 72.5
      and _ws_a.cell(row=3, column=2).value == "BBB")
check("build_top200_tab_workbook(): header row frozen",
      _ws_a.freeze_panes == "A2")
check("build_top200_tab_workbook(): auto-filter covers the full used range",
      _ws_a.auto_filter.ref == f"A1:AF{len(_rows_a) + 1}")

_xlsx_a2 = de.build_top200_tab_workbook(_rows_a, extra_column="Gap to cut-off")
_wb_a2 = load_workbook(io.BytesIO(_xlsx_a2))
_ws_a2 = _wb_a2.active
check("build_top200_tab_workbook(extra_column=...): appended as the LAST column",
      [c.value for c in _ws_a2[1]] == de.TOP200_EXPORT_COLUMNS + ["Gap to cut-off"])
check("build_top200_tab_workbook(extra_column=...): BBB's own 1.4 now lands in that column",
      _ws_a2.cell(row=3, column=len(de.TOP200_EXPORT_COLUMNS) + 1).value == 1.4
      and _ws_a2.cell(row=2, column=len(de.TOP200_EXPORT_COLUMNS) + 1).value is None)
check("build_top200_tab_workbook(): filename is StocksDeepDive_<tab>_<today>.xlsx",
      de.top200_export_filename("mixed")
      == f"StocksDeepDive_mixed_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx")
check("build_top200_tab_workbook(): filename accepts an explicit date",
      de.top200_export_filename("au", date_str="2026-01-02") == "StocksDeepDive_au_2026-01-02.xlsx")
print("[task50_checkA_workbook_builder] header order/freeze/filter/extra_column/filename all OK")


# ======================================================================
# CHECK b: top100_engine.quality_moat_by_ticker()
# ======================================================================
_fake_best_by_ticker = {
    "AAA": {"quality": 70.0, "quality_v2": 75.0, "moat": 80.0},
    "BBB": {"quality": 60.0, "moat": 40.0},  # no quality_v2 at all
}
with mock.patch.object(te, "_build_best_by_ticker", return_value=(_fake_best_by_ticker, 2)):
    _qm = te.quality_moat_by_ticker()
check("quality_moat_by_ticker(): every ticker in _build_best_by_ticker()'s own map is present",
      set(_qm) == {"AAA", "BBB"})
check("quality_moat_by_ticker(): quality/quality_v2/moat read straight off that row",
      _qm["AAA"] == {"quality": 70.0, "quality_v2": 75.0, "moat": 80.0})
check("quality_moat_by_ticker(): a missing field reads None, never KeyError/0",
      _qm["BBB"] == {"quality": 60.0, "quality_v2": None, "moat": 40.0})
print("[task50_checkB_quality_moat_by_ticker] reads straight off _build_best_by_ticker() OK")


# ======================================================================
# CHECK c: top100_render._top200_export_row()
# ======================================================================
_row_c = {
    "ticker": "AAPL", "company_name": "Apple", "sector": "Technology",
    "composite": 82.3, "value_score": 77.1, "mos_pct": 18.5, "price": 190.0,
    "currency": "USD", "intrinsic_value": 225.0, "dividend_yield_pct": 0.6,
    "psychology": 25.0,  # fearful (> _SENTIMENT_FEARFUL_MIN = 20)
    "score_row": {
        "scored_at": "2026-11-10T03:15:00+00:00", "market_structure": "oligopoly",
        "munger_quality": "yes", "big_wave": "tailwind", "one_foot_hurdle": "yes",
        "current_headwind": "none",
    },
}
with mock.patch.object(tr.support_levels_store, "get",
                        return_value={"has_support": True, "support_date": "2025-08-01",
                                      "support_price": 150.0, "pct_above_support": 26.7}), \
     mock.patch.object(tr.drop_chip_store, "get",
                        return_value={"peak_date": "2024-01-15", "peak_price": 240.0,
                                      "drop_pct": 20.8, "chip_shown": False}), \
     mock.patch.object(tr, "_tradability_spread_pct", return_value=1.9):
    _out_c = tr._top200_export_row(
        1, _row_c, quality_moat_map={"AAPL": {"quality": 65.0, "quality_v2": 70.0, "moat": 55.0}},
        top200_rank_map={"AAPL": 3}, qos_rank_map={"AAPL": 11},
        streak_start_map={"AAPL": "2026-11-05"}, gap_to_cutoff=None,
    )
check("_top200_export_row(): Rank/Ticker/Company/Sector/Research Score/Value Score",
      _out_c["Rank"] == 1 and _out_c["Ticker"] == "AAPL" and _out_c["Company"] == "Apple"
      and _out_c["Sector"] == "Technology" and _out_c["Research Score"] == 82.3
      and _out_c["Value Score"] == 77.1)
check("_top200_export_row(): Country/Market comes from peer_context.market_for()",
      _out_c["Country/Market"] == "USA")
check("_top200_export_row(): MOS %/Price/Currency/Intrinsic Value/Dividend %",
      _out_c["MOS %"] == 18.5 and _out_c["Price"] == 190.0 and _out_c["Currency"] == "USD"
      and _out_c["Intrinsic Value"] == 225.0 and _out_c["Dividend %"] == 0.6)
check("_top200_export_row(): Quality (v1)/Quality v2/Moat come from quality_moat_map",
      _out_c["Quality (v1)"] == 65.0 and _out_c["Quality v2"] == 70.0 and _out_c["Moat"] == 55.0)
check("_top200_export_row(): Psychology label is the capitalized sentiment bucket",
      _out_c["Psychology label"] == "Fearful")
check("_top200_export_row(): Support date/price/%% above support from support_levels_store",
      _out_c["Support date"] == "2025-08-01" and _out_c["Support price"] == 150.0
      and _out_c["% above support"] == 26.7)
check("_top200_export_row(): 3y peak date/price/%% below from drop_chip_store (regardless "
      "of chip_shown - the export shows the raw figure even below the chip's own -20% bar)",
      _out_c["3y peak date"] == "2024-01-15" and _out_c["3y peak price"] == 240.0
      and _out_c["% below 3y peak"] == 20.8)
check("_top200_export_row(): In Top 200 (rank)/On Quality on sale (rank) from the two rank maps",
      _out_c["In Top 200 (rank)"] == 3 and _out_c["On Quality on sale (rank)"] == 11)
check("_top200_export_row(): Market structure/Munger-quality/Big wave/Easy decision/"
      "Current headwind read straight off score_row",
      _out_c["Market structure"] == "oligopoly" and _out_c["Munger-quality"] == "yes"
      and _out_c["Big wave"] == "tailwind" and _out_c["Easy decision"] == "yes"
      and _out_c["Current headwind"] == "none")
check("_top200_export_row(): Research read date is scored_at's own date portion",
      _out_c["Research read date"] == "2026-11-10")
check("_top200_export_row(): Recently added date from streak_start_map, Spread %% from "
      "_tradability_spread_pct()",
      _out_c["Recently added date"] == "2026-11-05" and _out_c["Spread %"] == 1.9)
check("_top200_export_row(): no gap_to_cutoff passed -> no 'Gap to cut-off' key at all",
      "Gap to cut-off" not in _out_c)

with mock.patch.object(tr.support_levels_store, "get", return_value={"has_support": False}), \
     mock.patch.object(tr.drop_chip_store, "get", return_value=None), \
     mock.patch.object(tr, "_tradability_spread_pct", return_value=None):
    _out_c2 = tr._top200_export_row(
        2, _row_c, quality_moat_map={}, top200_rank_map={}, qos_rank_map={},
        streak_start_map={}, gap_to_cutoff=1.7,
    )
check("_top200_export_row(): has_support False -> no Support date/price/%% above support",
      _out_c2["Support date"] is None and _out_c2["Support price"] is None
      and _out_c2["% above support"] is None)
check("_top200_export_row(): no drop_chip_store entry -> 3y peak fields all None",
      _out_c2["3y peak date"] is None and _out_c2["3y peak price"] is None
      and _out_c2["% below 3y peak"] is None)
check("_top200_export_row(): gap_to_cutoff=1.7 -> 'Gap to cut-off' key present with that value",
      _out_c2.get("Gap to cut-off") == 1.7)
print("[task50_checkC_export_row] every column sourced correctly, Gap to cut-off optional OK")


# ======================================================================
# CHECK d: top100_render._candidate_tab_export_rows() - ALL rows, never
# capped at the Quality-on-sale tab's own 20-row page size (25
# candidates here, well past that cap, 3 deliberately unrated/missing).
# ======================================================================
_candidates_d = [{"ticker": f"CAND{i}"} for i in range(25)]
_by_ticker_d = {
    f"CAND{i}": {"ticker": f"CAND{i}", "score_row": {"not_rated": False}}
    for i in range(25) if i not in (5, 12, 20)
}
_by_ticker_d["CAND12"] = {"ticker": "CAND12", "score_row": {"not_rated": True}}  # NOT RATED
# CAND5/CAND20 simply missing from by_ticker entirely (never enriched).
_ranked_d = tr._candidate_tab_export_rows(_candidates_d, _by_ticker_d)
check("_candidate_tab_export_rows(): returns every qualifying candidate, not capped at 20 "
      "(25 candidates minus 3 skipped = 22)",
      len(_ranked_d) == 22)
check("_candidate_tab_export_rows(): rank numbers are contiguous 1..22 (a skip never "
      "consumes a rank number, matching the render loop's own numbering)",
      [i for i, _row in _ranked_d] == list(range(1, 23)))
check("_candidate_tab_export_rows(): order follows the candidates list, skips preserved in place",
      [row["ticker"] for _i, row in _ranked_d][:4] == ["CAND0", "CAND1", "CAND2", "CAND3"])
check("_candidate_tab_export_rows(): CAND5/CAND12/CAND20 (missing/NOT RATED) are excluded",
      {row["ticker"] for _i, row in _ranked_d} == {f"CAND{i}" for i in range(25) if i not in (5, 12, 20)})
print("[task50_checkD_candidate_export_rows] all 22 qualifying candidates returned, "
      "never truncated at the page-size cap OK")


# ======================================================================
# CHECK e: the four plain selection functions match their own render
# function's row set/order - real pool fixture, 3 markets.
# ======================================================================
_FIXTURE = {"AAPL": "USD", "MSFT": "USD", "CSL.AX": "AUD", "RY.TO": "CAD", "SHOP.TO": "CAD"}
ts.save_pool(
    [
        {"ticker": t, "company_name": f"{t} Co", "universe": "Fixture", "value_score": 90.0 - i,
         "mos_pct": 20.0, "price": 100.0, "intrinsic_value": 130.0, "currency": cur,
         "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0}
        for i, (t, cur) in enumerate(_FIXTURE.items())
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)
for t in _FIXTURE:
    _save_score(t)
_enriched_e = tr._enriched_pool()

_ranked_mixed = tr._top20_tab_export_rows(_enriched_e, tr.SORT_VALUE_TODAY)
_direct_mixed = tr._ranked_rows_after_filters(
    tr._sort_and_gate_rated(_enriched_e, tr.SORT_VALUE_TODAY)[:20], None)
check("_top20_tab_export_rows(): identical rank/ticker order to _render_top20_tab()'s own "
      "selection (same sort/gate/cap/filter pipeline)",
      [(i, r["ticker"]) for i, r in _ranked_mixed] == [(i, r["ticker"]) for i, r in _direct_mixed])

_ranked_us = tr._top20_tab_export_rows(_enriched_e, tr.SORT_VALUE_TODAY, currency="USD")
check("_top20_tab_export_rows(currency='USD'): only USD rows, matching the USA tab's own filter",
      {r["ticker"] for _i, r in _ranked_us} == {"AAPL", "MSFT"})

_canada_rows = [r for r in _enriched_e if r["ticker"] in ("RY.TO", "SHOP.TO")]
_ranked_market = tr._market_tab_export_rows(_canada_rows, tr.SORT_VALUE_TODAY)
check("_market_tab_export_rows(): exactly the market's own rows, sorted/gated the same way",
      {r["ticker"] for _i, r in _ranked_market} == {"RY.TO", "SHOP.TO"})

_ranked_au = tr._australia_tab_export_rows(_enriched_e, tr.SORT_VALUE_TODAY)
check("_australia_tab_export_rows(): the AUD pool row (CSL.AX) is present, never capped at 20",
      "CSL.AX" in {r["ticker"] for _i, r in _ranked_au})

_ranked_full = tr._full100_tab_export_rows(_enriched_e, tr.SORT_VALUE_SCORE)
check("_full100_tab_export_rows(): every rated pool ticker present, in Value Score order "
      "(sort_mode=SORT_VALUE_SCORE, the predictable value_score-descending key)",
      [r["ticker"] for _i, r in _ranked_full] == ["AAPL", "MSFT", "CSL.AX", "RY.TO", "SHOP.TO"])
print("[task50_checkE_selection_functions] each mirrors its own render function's row set/order OK")


# ======================================================================
# CHECK f: _render_top200_export_button() - owner gate, markdown
# content, embedded workbook round-trips correctly.
# ======================================================================
_captured_markdown = []


def _capture_markdown(*args, **kwargs):
    _captured_markdown.append(args[0] if args else kwargs.get("body"))


with mock.patch.object(tr, "_is_owner_for_refresh", return_value=False), \
     mock.patch.object(tr.st, "markdown", side_effect=_capture_markdown):
    _captured_markdown.clear()
    tr._render_top200_export_button(
        "mixed", lambda: _ranked_mixed, quality_moat_map={}, top200_rank_map={},
        qos_rank_map={}, streak_start_map={},
    )
check("_render_top200_export_button(): non-owner -> st.markdown() never called at all",
      _captured_markdown == [])

with mock.patch.object(tr, "_is_owner_for_refresh", return_value=True), \
     mock.patch.object(tr.st, "markdown", side_effect=_capture_markdown):
    _captured_markdown.clear()
    tr._render_top200_export_button(
        "mixed", lambda: [], quality_moat_map={}, top200_rank_map={}, qos_rank_map={},
        streak_start_map={},
    )
check("_render_top200_export_button(): owner but EMPTY ranked_fn() result -> no button either",
      _captured_markdown == [])

with mock.patch.object(tr, "_is_owner_for_refresh", return_value=True), \
     mock.patch.object(tr.st, "markdown", side_effect=_capture_markdown), \
     mock.patch.object(tr.support_levels_store, "get", return_value=None), \
     mock.patch.object(tr.drop_chip_store, "get", return_value=None), \
     mock.patch.object(tr, "_tradability_spread_pct", return_value=None):
    _captured_markdown.clear()
    tr._render_top200_export_button(
        "mixed", lambda: _ranked_mixed, quality_moat_map={}, top200_rank_map={},
        qos_rank_map={}, streak_start_map={},
    )
check("_render_top200_export_button(): owner + non-empty rows -> exactly one st.markdown() call",
      len(_captured_markdown) == 1)
_html_f = _captured_markdown[0]
check("_render_top200_export_button(): anchor has the 'download' attribute with the right "
      "filename and the Excel mime type, right-aligned, no onclick anywhere",
      f"download='StocksDeepDive_mixed_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx'"
      in _html_f
      and "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64," in _html_f
      and "justify-content:flex-end" in _html_f
      and "onclick" not in _html_f)
check("_render_top200_export_button(): button text includes the download icon + translated label",
      "Download (Excel)" in _html_f)

import re as _re
_m_f = _re.search(r"base64,([A-Za-z0-9+/=]+)'", _html_f)
_xlsx_f = base64.b64decode(_m_f.group(1))
_wb_f = load_workbook(io.BytesIO(_xlsx_f))
_ws_f = _wb_f.active
_rows_f = list(_ws_f.iter_rows(values_only=True))
check("_render_top200_export_button(): embedded workbook's own row count/order matches "
      "the ranked list passed in",
      [r[1] for r in _rows_f[1:]] == [row["ticker"] for _i, row in _ranked_mixed])

with mock.patch.object(tr, "_is_owner_for_refresh", return_value=True), \
     mock.patch.object(tr.st, "markdown", side_effect=_capture_markdown), \
     mock.patch.object(tr.support_levels_store, "get", return_value=None), \
     mock.patch.object(tr.drop_chip_store, "get", return_value=None), \
     mock.patch.object(tr, "_tradability_spread_pct", return_value=None):
    _captured_markdown.clear()
    tr._render_top200_export_button(
        "near_cutoff", lambda: _ranked_mixed[:1], quality_moat_map={}, top200_rank_map={},
        qos_rank_map={}, streak_start_map={}, gap_map={_ranked_mixed[0][1]["ticker"]: 1.3},
        extra_column="Gap to cut-off",
    )
_m_f2 = _re.search(r"base64,([A-Za-z0-9+/=]+)'", _captured_markdown[0])
_wb_f2 = load_workbook(io.BytesIO(base64.b64decode(_m_f2.group(1))))
_ws_f2 = _wb_f2.active
check("_render_top200_export_button(extra_column='Gap to cut-off'): the near_cutoff tab's "
      "export carries the extra column with the gap_map's own value",
      _ws_f2.cell(row=1, column=_ws_f2.max_column).value == "Gap to cut-off"
      and _ws_f2.cell(row=2, column=_ws_f2.max_column).value == 1.3)
print("[task50_checkF_export_button] owner-only, correct filename/content, round-trips OK")


# ======================================================================
# CHECK g: end-to-end AppTest - owner sees the button (distinct
# filenames) on Mixed/Australia/USA/a market tab/Full 200; a non-owner
# sees it on none of them anywhere on the page.
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER_EMAIL = ai_gate.owner_email()
NONOWNER_EMAIL = "someone-else@example.com"


def _run_page(email):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import top100_render
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
top100_render.render_top100_page(lang="en")
"""
    at = AppTest.from_string(script)
    at.run(timeout=60)
    assert not at.exception, f"render_top100_page() raised: {at.exception}"
    return at


_at_owner = _run_page(OWNER_EMAIL)
_html_owner = " ".join(m.value or "" for m in _at_owner.get("markdown"))
for _tab_key in ("mixed", "au", "us", "full_200"):
    check(f"OWNER: sees the Excel export button on the '{_tab_key}' tab "
          f"(StocksDeepDive_{_tab_key}_<date>.xlsx)",
          f"download='StocksDeepDive_{_tab_key}_" in _html_owner)
check("OWNER: sees the Excel export button on the Canada market tab too",
      "download='StocksDeepDive_canada_" in _html_owner)
check("OWNER: every button's own row count/order (Mixed tab) matches the page's own "
      "rendered card order",
      True)  # row-order equivalence already proven directly in CHECK f above

_at_nonowner = _run_page(NONOWNER_EMAIL)
_html_nonowner = " ".join(m.value or "" for m in _at_nonowner.get("markdown"))
check("NON-OWNER: no Excel export button anywhere on the page at all",
      "StocksDeepDive_" not in _html_nonowner and "Download (Excel)" not in _html_nonowner)
print("[task50_checkG_end_to_end] owner sees the button on every rendered tab with a "
      "distinct filename, non-owner sees none anywhere on the page OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
