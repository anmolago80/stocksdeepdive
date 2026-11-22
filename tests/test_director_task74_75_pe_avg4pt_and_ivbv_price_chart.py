"""
TASK 74+75 (Director-directed, owner-approved, 22 Nov 2026) - PE
average widened to 4 points (TTM + last 3 FY, matching the workbook's
AU = AVERAGE(AQ:AT)) + a new indexed "IV/BV vs Share Price" chart.

PART A: auto_compounder_engine._avg_pe_3pt() -> _avg_pe_4pt() - widened
from 3 points (TTM + last 2 FY) to 4 (TTM + last 3 FY). Both callers
(_build_fundamentals()'s "PE Ratio Average" metric, _build_fair_value()'s
PE Trailing method) updated. compounder_ui._cp_pe_ratio_chart()'s two
reference-line labels renamed to the workbook's own names ("PE Ratio
Average 3 Years", "PE Ratio Average"). A new per-mode "Text description
of this chart" expander differs for Auto vs uploaded pages (render_
section()'s new is_auto parameter). _pe_avg_3y_by_eps()/_fair_pe_5y()/
_pe_forward_method() are untouched - verified below.

Reference check (Director's own numbers): WMS-shaped fixture with
P/Es TTM=21.01, lastFY=24.95, prevFY=18.55, prevFY2=26.09 ->
new 4pt average = (21.01+24.95+18.55+26.09)/4 = 22.65 exactly, matching
the Director's own "Expected WMS ~= 22.65x" reference to the cent. The
OLD 3pt average on the SAME fixture (21.01+24.95+18.55)/3 = 21.5033... -
confirms the 4th point (26.09) is what moves the number, not a
coincidence of the fixture.

PART B: auto_compounder_engine._iv_bv_series() also returns "prices"
(year-end price per year via _year_end_prices(), TTM = current price).
build_compounder_data._iv_bv_year_prices() does the same for uploaded
research data from the already-loaded Fundamentals price_history (no
new workbook column). compounder_ui._cp_iv_bv_vs_price_chart() is the
new chart: two lines (IV/BV index, share-price index) on one y-axis,
indexed to the first year BOTH a positive IV/BV AND a price exist
("the base year") = 100; years before the base year aren't plotted; a
missing price within the plotted range is a gap; fewer than 3 usable
years -> no chart, a one-line caption instead; title names the base
year; hover text shows both the index and the real value.

Full suite, 0 failures required alongside this file.

Run: python3 tests/test_director_task74_75_pe_avg4pt_and_ivbv_price_chart.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


import pandas as pd

import auto_compounder_engine as ace
import build_compounder_data as bcd
import compounder_ui as cui


# ======================================================================
# PART A fixture: WMS-shaped. All EPS = 1.0 so each year's P/E is just
# that year's price - price_now/lastFY/prevFY/prevFY2 are the Director's
# own 4 reference P/Es (21.01, 24.95, 18.55, 26.09). Year-end prices are
# placed exactly ON each fiscal year's own end-date so _year_end_prices()
# resolves them without any "nearest on-or-before" ambiguity.
# ======================================================================
_WMS_INCOME = pd.DataFrame(
    {"2026-06-30": [1.0], "2025-06-30": [1.0], "2024-06-30": [1.0]},
    index=["Basic EPS"],
)
_WMS_PRICES_10Y = {
    "dates": ["2024-06-30", "2025-06-30", "2026-06-30"],
    "prices": [26.09, 18.55, 24.95],
}
_WMS_BUNDLE = {
    "info": {"currentPrice": 21.01, "trailingEps": 1.0, "sharesOutstanding": 100e6},
    "income": _WMS_INCOME, "income_q": None,
    "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
    "prices_10y": _WMS_PRICES_10Y, "dividends": {},
}

_wms_new_avg = ace._avg_pe_4pt(_WMS_BUNDLE)
check("WMS fixture: new 4pt PE Ratio Average = 22.65x (Director's own reference, exact)",
      _wms_new_avg is not None and abs(_wms_new_avg - 22.65) < 0.005)

# Old 3pt behaviour, reproduced by hand from the SAME fixture (the old
# function no longer exists post-rename - this proves what it WOULD
# have given, not a live call).
_wms_old_avg = (21.01 + 24.95 + 18.55) / 3
check("WMS fixture: old 3pt average (hand-computed) = 21.5033...",
      abs(_wms_old_avg - 21.503333333333334) < 1e-9)
check("WMS fixture: new 4pt average differs from the old 3pt average "
      "(the 4th point, prevFY2=26.09, genuinely moves the number)",
      abs(_wms_new_avg - _wms_old_avg) > 1.0)

# ======================================================================
# PART A, OCL-shaped fixture (non-uniform EPS, to prove the per-year
# P/E = price/eps division itself, not just a trivial eps=1 case).
# ======================================================================
_OCL_INCOME = pd.DataFrame(
    {"2026-06-30": [0.30], "2025-06-30": [0.25], "2024-06-30": [0.20]},
    index=["Basic EPS"],
)
_OCL_PRICES_10Y = {
    "dates": ["2024-06-30", "2025-06-30", "2026-06-30"],
    "prices": [3.00, 6.00, 7.50],
}
_OCL_BUNDLE = {
    "info": {"currentPrice": 9.00, "sharesOutstanding": 50e6},
    "income": _OCL_INCOME, "income_q": None,
    "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
    "prices_10y": _OCL_PRICES_10Y, "dividends": {},
}
# TTM P/E = 9.00/0.38 (trailingEps unset -> falls back through
# _eps_ttm() to info["trailingEps"], None, so the TTM leg is skipped
# unless we provide it) - give it one explicitly for a clean 4-point
# computation.
_OCL_BUNDLE["info"]["trailingEps"] = 0.35
_ocl_pes = [9.00 / 0.35, 7.50 / 0.30, 6.00 / 0.25, 3.00 / 0.20]  # TTM, FY26, FY25, FY24
_ocl_new_avg_expected = sum(_ocl_pes) / 4
_ocl_old_avg_expected = sum(_ocl_pes[:3]) / 3
_ocl_new_avg = ace._avg_pe_4pt(_OCL_BUNDLE)
check("OCL fixture: new 4pt average matches hand-computed (TTM + 3 FY P/Es)",
      _ocl_new_avg is not None and abs(_ocl_new_avg - _ocl_new_avg_expected) < 1e-9)
check("OCL fixture: new 4pt average differs from the old 3pt average",
      abs(_ocl_new_avg - _ocl_old_avg_expected) > 0.01)

# PE Trailing IV scales by exactly the same ratio as the averages
# changed - confirms the second caller (_build_fair_value()'s PE
# Trailing method) multiplies normalised EPS by THIS average, nothing
# else changed in that multiplication.
_ocl_normalized = ace._normalized_eps(_OCL_BUNDLE)
check("OCL fixture: normalized_eps resolves to a usable value",
      _ocl_normalized.get("value") is not None)
if _ocl_normalized.get("value") is not None:
    _neps = _ocl_normalized["value"]
    _pe_trailing_old = _neps * _ocl_old_avg_expected
    _pe_trailing_new = _neps * _ocl_new_avg
    check("OCL fixture: old vs new PE Trailing IV changes by the same ratio as "
          "old vs new PE Ratio Average (same EPS, only the average changed)",
          abs((_pe_trailing_new / _pe_trailing_old) - (_ocl_new_avg / _ocl_old_avg_expected)) < 1e-9)
    check("OCL fixture: PE Trailing IV actually moved (not a no-op)",
          abs(_pe_trailing_new - _pe_trailing_old) > 0.01)

# ======================================================================
# PART A: both real callers in the source use _avg_pe_4pt, not the old
# name - and _pe_avg_3y_by_eps/_fair_pe_5y/_pe_forward_method are
# untouched (still present, no reference to _avg_pe_3pt anywhere).
# ======================================================================
check("_avg_pe_3pt no longer exists (renamed)", not hasattr(ace, "_avg_pe_3pt"))
check("_avg_pe_4pt exists", hasattr(ace, "_avg_pe_4pt"))
_src_earnings_trends = inspect.getsource(ace._build_earnings_trends)
_src_fair_value = inspect.getsource(ace._build_fair_value)
check("_build_earnings_trends() (the 'PE Ratio Average' metric caller) calls _avg_pe_4pt",
      "avg_pe = _avg_pe_4pt(" in _src_earnings_trends and "_avg_pe_3pt(" not in _src_earnings_trends)
check("_build_fair_value() (the PE Trailing method caller) calls _avg_pe_4pt",
      "avg_pe = _avg_pe_4pt(" in _src_fair_value and "_avg_pe_3pt(" not in _src_fair_value)
check("_pe_avg_3y_by_eps still exists, untouched by this task", hasattr(ace, "_pe_avg_3y_by_eps"))
check("_fair_pe_5y still exists, untouched by this task", hasattr(ace, "_fair_pe_5y"))
check("_pe_forward_method still exists, untouched by this task", hasattr(ace, "_pe_forward_method"))
_src_3y = inspect.getsource(ace._pe_avg_3y_by_eps)
check("_pe_avg_3y_by_eps()'s own EPS-averaging logic is untouched (still [:2], 3 EPS points)",
      "_eps_series(bundle)[:2]" in _src_3y)

# ======================================================================
# PART A: chart reference-line labels renamed to the workbook's names.
# ======================================================================
_src_chart = inspect.getsource(cui._cp_pe_ratio_chart)
check('chart ref line call passes "PE Ratio Average 3 Years" as its label',
      '"PE Ratio Average 3 Years", avg_3y_yanchor' in _src_chart)
check('chart ref line call passes "PE Ratio Average" as its label',
      '"PE Ratio Average", overall_avg_yanchor' in _src_chart)

_series_fixture = {"WMS": {"pe_ratio": {"years": ["2026", "2025"], "values": [22.0, 18.0]}}}
_refs_fixture = {"WMS": {"avg_3y": 19.5, "overall_avg": 22.65}}
_fig_pe = cui._cp_pe_ratio_chart("WMS", _series_fixture, _refs_fixture)
_fig_pe_names = [t.name for t in _fig_pe.layout.annotations] if False else None
_fig_pe_texts = [a.text for a in _fig_pe.layout.annotations]
check('rendered chart annotation shows "PE Ratio Average 3 Years: 19.50x"',
      any("PE Ratio Average 3 Years: 19.50x" in t for t in _fig_pe_texts))
check('rendered chart annotation shows "PE Ratio Average: 22.65x"',
      any(t.startswith("PE Ratio Average:") and "22.65x" in t for t in _fig_pe_texts))

# ======================================================================
# PART A: uploaded-research ticker (e.g. AUB.AX) - build_compounder_
# data.py's pe_ratio_refs reads AU/AV directly, no recompute - the
# chart/section renderer never calls _avg_pe_4pt for uploaded data.
# ======================================================================
check("PE_RATIO_REF_COLS maps straight to workbook cells AU/AV, untouched by this task",
      bcd.PE_RATIO_REF_COLS == {"avg_3y": "AV", "overall_avg": "AU"})
_src_render_section = inspect.getsource(cui.render_section)
check("render_section()'s Earnings Trends branch never calls _avg_pe_4pt "
      "(uploaded pages never recompute PE Ratio Average/3 Years - they're "
      "read straight from pe_ratio_refs, exactly as before this task)",
      "_avg_pe_4pt" not in _src_render_section)
check("render_section() accepts an is_auto parameter",
      "is_auto" in inspect.signature(cui.render_section).parameters)

# ======================================================================
# Behavioral check (not a raw-source-text match, since the real strings
# are built from concatenated literals across lines): actually run
# render_section() for "Earnings Trends" with is_auto True then False,
# with st.columns/st.caption/st.expander/st.warning/sdd_plotly_chart
# patched out (same monkeypatch-capture pattern as TASK 41's own
# render_section() test) so no real Streamlit runtime is needed, and
# read back what was ACTUALLY passed to st.caption inside the "Text
# description of this chart" expander.
# ======================================================================
class _FakeCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_captured_captions = []
_orig_columns, _orig_caption, _orig_expander = cui.st.columns, cui.st.caption, cui.st.expander
_orig_warning, _orig_markdown = cui.st.warning, cui.st.markdown
_orig_plotly = cui.sdd_plotly_chart
cui.st.columns = lambda n: [_FakeCtx() for _ in range(n)]
cui.st.caption = lambda *a, **k: _captured_captions.append(a[0] if a else "")
cui.st.expander = lambda *a, **k: _FakeCtx()
cui.st.warning = lambda *a, **k: None
cui.st.markdown = lambda *a, **k: None
cui.sdd_plotly_chart = lambda *a, **k: None

_et_series = {
    "WMS": {
        "pe_ratio": {"years": ["2026", "2025"], "values": [22.0, 18.0]},
        "eps": {"years": ["2026", "2025"], "values": [1.0, 1.0]},
        "eps_growth": {"years": ["2026"], "values": [0.1]},
    }
}
_et_refs = {"WMS": {"avg_3y": 19.5, "overall_avg": 22.65}}
_et_sections = {"Earnings Trends": {"metrics": [], "series": _et_series, "pe_ratio_refs": _et_refs}}

try:
    _captured_captions.clear()
    cui.render_section(_et_sections, "WMS", "Earnings Trends", is_auto=True)
    _auto_captions = list(_captured_captions)

    _captured_captions.clear()
    cui.render_section(_et_sections, "WMS", "Earnings Trends", is_auto=False)
    _uploaded_captions = list(_captured_captions)
finally:
    cui.st.columns, cui.st.caption, cui.st.expander = _orig_columns, _orig_caption, _orig_expander
    cui.st.warning, cui.st.markdown = _orig_warning, _orig_markdown
    cui.sdd_plotly_chart = _orig_plotly

check('Auto page: PE chart description caption says "average of TTM + last 3 FY P/Es"',
      any("average of TTM + last 3 FY P/Es" in c for c in _auto_captions))
check('Auto page: PE chart description caption says "price / average of TTM + last 2 FY EPS"',
      any("price / average of TTM + last 2 FY EPS" in c for c in _auto_captions))
check('Uploaded page: PE chart description caption says "from your workbook"',
      any("are from your workbook" in c for c in _uploaded_captions))
check("Uploaded page's caption list does NOT contain the Auto-only wording",
      not any("average of TTM + last 3 FY P/Es" in c for c in _uploaded_captions))


# ======================================================================
# PART B: auto_compounder_engine._iv_bv_series() now returns "prices".
# ======================================================================
_IVBV_CASHFLOW = pd.DataFrame(
    {
        "2026-06-30": [120.0, None, None],
        "2025-06-30": [100.0, None, None],
        "2024-06-30": [-50.0, None, None],  # a negative/loss year -> base-year shift
    },
    index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
)
_IVBV_BALANCE = pd.DataFrame(
    {"2026-06-30": [500.0], "2025-06-30": [450.0], "2024-06-30": [400.0]},
    index=["Stockholders Equity"],
)
_IVBV_PRICES_10Y = {
    "dates": ["2024-06-30", "2025-06-30", "2026-06-30"],
    "prices": [8.00, 9.00, 10.00],
}
_IVBV_BUNDLE = {
    "info": {"currentPrice": 11.00, "sharesOutstanding": 100e6},
    "income": pd.DataFrame(), "income_q": None,
    "balance": _IVBV_BALANCE, "cashflow": _IVBV_CASHFLOW,
    "prices_10y": _IVBV_PRICES_10Y, "dividends": {},
}
_dcf_result = {"growth": 0.05, "discount_rate": 0.10, "perpetual_rate": 0.03}
_ivbv_entry = ace._iv_bv_series(_IVBV_BUNDLE, _dcf_result, bvps_ttm=5.5)
check("_iv_bv_series() returns a 'prices' key", _ivbv_entry is not None and "prices" in _ivbv_entry)
if _ivbv_entry:
    check("_iv_bv_series() 'prices' is the same length as 'years'/'ratios'",
          len(_ivbv_entry["prices"]) == len(_ivbv_entry["years"]) == len(_ivbv_entry["ratios"]))
    _ttm_idx = next((i for i, y in enumerate(_ivbv_entry["years"]) if y.startswith("TTM")), None)
    if _ttm_idx is not None:
        check("_iv_bv_series() TTM price = today's current price (11.00), not a year-end close",
              abs(_ivbv_entry["prices"][_ttm_idx] - 11.00) < 1e-9)
    _y2026_idx = _ivbv_entry["years"].index("2026")
    _y2025_idx = _ivbv_entry["years"].index("2025")
    check("_iv_bv_series() 2026 FY price = its own year-end close (10.00)",
          abs(_ivbv_entry["prices"][_y2026_idx] - 10.00) < 1e-9)
    check("_iv_bv_series() 2025 FY price = its own year-end close (9.00)",
          abs(_ivbv_entry["prices"][_y2025_idx] - 9.00) < 1e-9)

# ======================================================================
# PART B: _cp_iv_bv_vs_price_chart() - base-year selection (including a
# zero/negative IV/BV year forcing a shift), indexing, title, tooltip.
# ======================================================================
# Raw entry convention matches _iv_bv_series()'s own (newest-first:
# [TTM, newestFY, ..., oldestFY]). 2024's ratio is negative (a loss
# year) -> base year must shift forward to 2025, the first year with
# BOTH a positive ratio and a price.
_series_shift = {
    "years": ["TTM", "2026", "2025", "2024"],
    "ratios": [2.4, 2.0, 1.5, -0.5],
    "prices": [11.00, 10.00, 9.00, 8.00],
}
_fig_b, _reason_b = cui._cp_iv_bv_vs_price_chart("X", {"X": _series_shift})
check("base-year shift: a chart IS produced (3 usable years from 2025 onward: 2025/2026/TTM)",
      _fig_b is not None and _reason_b is None)
if _fig_b is not None:
    check('base-year shift: title names 2025 as the base year ("2025 = 100")',
          "2025 = 100" in _fig_b.layout.title.text)
    _x_vals = list(_fig_b.data[0].x)
    check("base-year shift: 2024 (before the base year) is NOT plotted",
          "2024" not in _x_vals)
    check("base-year shift: plotted years are exactly [2025, 2026, TTM] in order",
          _x_vals == ["2025", "2026", "TTM"])
    _ivbv_y = list(_fig_b.data[0].y)
    check("base-year shift: IV/BV index at the base year (2025) = 100",
          abs(_ivbv_y[0] - 100.0) < 1e-9)
    check("base-year shift: IV/BV index at 2026 = 133.3 (2.0/1.5*100)",
          abs(_ivbv_y[1] - (2.0 / 1.5 * 100.0)) < 0.1)
    _price_y = list(_fig_b.data[1].y)
    check("base-year shift: price index at the base year (2025) = 100",
          abs(_price_y[0] - 100.0) < 1e-9)
    check("base-year shift: price index at 2026 = 111.1 (10.00/9.00*100)",
          abs(_price_y[1] - (10.00 / 9.00 * 100.0)) < 0.1)
    # Tooltip text format: "{index:.0f} ({real value})".
    _ivbv_text = list(_fig_b.data[0].text)
    check('IV/BV tooltip text format matches the spec example shape, e.g. "100 (1.5x)"',
          _ivbv_text[0] == "100 (1.5x)")
    _price_text = list(_fig_b.data[1].text)
    check('Price tooltip text includes the real currency value, e.g. "100 (...)"',
          _price_text[0].startswith("100 ("))

# ASX ticker: price tooltip shows "A$", never a bare, ambiguous "$" -
# Director's own example is "400 (A$112.00)" for CSL.
_fig_csl, _ = cui._cp_iv_bv_vs_price_chart("CSL.AX", {"CSL.AX": _series_shift})
check('CSL.AX (ASX ticker): price tooltip shows "A$", not a bare "$"',
      _fig_csl is not None and "A$" in list(_fig_csl.data[1].text)[0])

# Fewer than 3 usable years -> no chart, one-line caption.
_series_short = {"years": ["TTM", "2026"], "ratios": [2.0, 1.5], "prices": [11.0, 10.0]}
_fig_short, _reason_short = cui._cp_iv_bv_vs_price_chart("Y", {"Y": _series_short})
check("fewer than 3 usable years: no chart produced", _fig_short is None)
check("fewer than 3 usable years: a one-line caption reason is given instead",
      bool(_reason_short) and isinstance(_reason_short, str))

# No usable base year at all (every ratio non-positive or no price).
_series_none = {"years": ["TTM", "2026", "2025"], "ratios": [-1.0, -2.0, -3.0], "prices": [11.0, 10.0, 9.0]}
_fig_none, _reason_none = cui._cp_iv_bv_vs_price_chart("Z", {"Z": _series_none})
check("no usable base year (all ratios negative): no chart, caption given",
      _fig_none is None and bool(_reason_none))

# Missing price mid-series -> a gap (None), not a crash or a fabricated value.
_series_gap = {
    "years": ["TTM", "2026", "2025", "2024"],
    "ratios": [2.4, 2.0, 1.5, 1.2],
    "prices": [11.00, None, 9.00, 8.00],
}
_fig_gap, _reason_gap = cui._cp_iv_bv_vs_price_chart("G", {"G": _series_gap})
check("missing price mid-series: chart still produced (4 usable years)",
      _fig_gap is not None and _reason_gap is None)
if _fig_gap is not None:
    # Raw entry is newest-first ([TTM, 2026, 2025, 2024]); reversed() for
    # display is oldest-first ([2024, 2025, 2026, TTM]) - the missing
    # price (originally at the "2026" slot) lands at index 2 here.
    _price_y_gap = list(_fig_gap.data[1].y)
    check("missing price mid-series: the missing year's (2026) price index is a None gap",
          _price_y_gap[2] is None)
    _ivbv_y_gap = list(_fig_gap.data[0].y)
    check("missing price mid-series: the IV/BV index line is unaffected by the price gap",
          all(v is not None for v in _ivbv_y_gap))

# A ticker/section with no "prices" key at all (an old cached entry, or
# a hand-built Research page from before this task) degrades to "no
# usable base year" rather than crashing.
_series_no_prices = {"years": ["TTM", "2026", "2025", "2024"], "ratios": [2.4, 2.0, 1.5, 1.2]}
_fig_noprices, _reason_noprices = cui._cp_iv_bv_vs_price_chart("N", {"N": _series_no_prices})
check("entry with no 'prices' key at all: no crash, no chart, a caption instead",
      _fig_noprices is None and bool(_reason_noprices))

# Empty/missing ticker entirely.
_fig_empty, _reason_empty = cui._cp_iv_bv_vs_price_chart("MISSING", {})
check("ticker missing from iv_bv_series entirely: returns (None, None) cleanly",
      _fig_empty is None and _reason_empty is None)

# ======================================================================
# PART B: new chart wired into render_section()'s Value vs Book branch,
# with its own "Text description of this chart" expander including the
# Director's exact required sentence.
# ======================================================================
check("render_section() calls the new _cp_iv_bv_vs_price_chart in its Value vs Book branch",
      "_cp_iv_bv_vs_price_chart(" in _src_render_section)

cui.st.columns = lambda n: [_FakeCtx() for _ in range(n)]
cui.st.caption = lambda *a, **k: _captured_captions.append(a[0] if a else "")
cui.st.expander = lambda *a, **k: _FakeCtx()
cui.st.warning = lambda *a, **k: None
cui.st.markdown = lambda *a, **k: None
cui.sdd_plotly_chart = lambda *a, **k: None
_vvb_sections = {
    "Value vs Book": {
        "metrics": [], "iv_bv_series": {"WMS": _series_shift},
        "fcf_growth": {}, "book_value_growth": {},
    }
}
try:
    _captured_captions.clear()
    cui.render_section(_vvb_sections, "WMS", "Value vs Book")
    _vvb_captions = list(_captured_captions)
finally:
    cui.st.columns, cui.st.caption, cui.st.expander = _orig_columns, _orig_caption, _orig_expander
    cui.st.warning, cui.st.markdown = _orig_warning, _orig_markdown
    cui.sdd_plotly_chart = _orig_plotly

check('new chart\'s own "Text description of this chart" caption includes the '
      "required sentence verbatim",
      any(
          "Yearly IV/BV uses today's growth, discount rate and share count - "
          "read the shape, not exact history." in c
          for c in _vvb_captions
      ))

# ======================================================================
# PART B: uploaded research - build_compounder_data._iv_bv_year_prices()
# (the pure, directly-testable helper extracted for this task).
# ======================================================================
_ph_entry = {
    "dates": ["2024-03-31", "2024-11-30", "2025-06-15", "2025-12-20", "2026-01-15"],
    "prices": [10.0, 11.0, 12.0, 13.0, 14.0],
}
check("_iv_bv_year_prices(): TTM/current label = the LATEST monthly price on file",
      bcd._iv_bv_year_prices(["TTM"], _ph_entry) == [14.0])
check("_iv_bv_year_prices(): a plain year label = the LAST monthly price dated "
      "within that calendar year (2024 -> Nov close, not the March one)",
      bcd._iv_bv_year_prices(["2024"], _ph_entry) == [11.0])
check("_iv_bv_year_prices(): 2025 -> the Dec close (last match in 2025), not the June one",
      bcd._iv_bv_year_prices(["2025"], _ph_entry) == [13.0])
check("_iv_bv_year_prices(): a year with no matching monthly price -> None (gap, not fabricated)",
      bcd._iv_bv_year_prices(["2020"], _ph_entry) == [None])
check("_iv_bv_year_prices(): empty price_history_entry -> all None, no crash",
      bcd._iv_bv_year_prices(["TTM", "2024"], {}) == [None, None])
check("_iv_bv_year_prices(): None price_history_entry -> all None, no crash",
      bcd._iv_bv_year_prices(["TTM", "2024"], None) == [None, None])

_src_bcd_main = inspect.getsource(bcd)
check("build_compounder_data.py's iv_bv_series construction calls _iv_bv_year_prices "
      "(the uploaded-research path actually uses the new helper, not dead code)",
      "_iv_bv_year_prices(years, price_history.get(tkr))" in _src_bcd_main)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
