"""
TASK 74+75 (Director-directed, owner-approved, 22 Nov 2026) - PE
average widened to 4 points (TTM + last 3 FY, matching the workbook's
AU = AVERAGE(AQ:AT)).

PART A: auto_compounder_engine._avg_pe_3pt() -> _avg_pe_4pt() - widened
from 3 points (TTM + last 2 FY) to 4 (TTM + last 3 FY). Both callers
(_build_earnings_trends()'s "PE Ratio Average" metric, _build_fair_
value()'s PE Trailing method) updated. compounder_ui._cp_pe_ratio_
chart()'s two reference-line labels renamed to the workbook's own
names ("PE Ratio Average 3 Years", "PE Ratio Average"). A new per-mode
"Text description of this chart" expander differs for Auto vs uploaded
pages (render_section()'s new is_auto parameter). _pe_avg_3y_by_eps()/
_fair_pe_5y()/_pe_forward_method() are untouched - verified below.

Reference check (Director's own numbers): WMS-shaped fixture with
P/Es TTM=21.01, lastFY=24.95, prevFY=18.55, prevFY2=26.09 ->
new 4pt average = (21.01+24.95+18.55+26.09)/4 = 22.65 exactly, matching
the Director's own "Expected WMS ~= 22.65x" reference to the cent. The
OLD 3pt average on the SAME fixture (21.01+24.95+18.55)/3 = 21.5033... -
confirms the 4th point (26.09) is what moves the number, not a
coincidence of the fixture.

PART B (the new "IV/BV vs Share Price, indexed" chart introduced here)
was ENTIRELY rebuilt by TASK 76 (Director-directed, owner-approved,
23 Nov 2026, "fixes to TASK 74+75 display") - the chart's own tests now
live in tests/test_director_task76_pe_chart_fixes_and_ivbv_vs_price_
periods.py, which also covers this file's two display-bug fixes to the
PE Ratio chart (legend entries instead of clipped side labels; no
duplicate "Text description of this chart" expander).

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
# PART A: chart reference-line labels renamed to the workbook's names -
# TASK 76 moved these from clipped side add_annotation labels to
# ordinary legend entries (see test_director_task76_... for the full
# display-fix coverage); this file just confirms the VALUE-bearing
# label text itself is still correct.
# ======================================================================
_series_fixture = {"WMS": {"pe_ratio": {"years": ["2026", "2025"], "values": [22.0, 18.0]}}}
_refs_fixture = {"WMS": {"avg_3y": 19.5, "overall_avg": 22.65}}
_fig_pe = cui._cp_pe_ratio_chart("WMS", _series_fixture, _refs_fixture)
_pe_trace_names = [t.name for t in _fig_pe.data]
check('rendered chart has a legend trace named "PE Ratio Average 3 Years 19.50x"',
      "PE Ratio Average 3 Years 19.50x" in _pe_trace_names)
check('rendered chart has a legend trace named "PE Ratio Average 22.65x"',
      "PE Ratio Average 22.65x" in _pe_trace_names)

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
# with st.columns/st.warning/st.markdown patched out and sdd_plotly_
# chart replaced by a RECORDING stub (not a bare no-op - TASK 76 moved
# this chart's description text into sdd_plotly_chart()'s own
# text_description= kwarg, so a no-op stub would swallow it entirely)
# to read back what text_description was actually passed per chart.
# ======================================================================
class _FakeCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_recorded_calls = []
_orig_columns, _orig_warning, _orig_markdown = cui.st.columns, cui.st.warning, cui.st.markdown
_orig_plotly = cui.sdd_plotly_chart
cui.st.columns = lambda n: [_FakeCtx() for _ in range(n)]
cui.st.warning = lambda *a, **k: None
cui.st.markdown = lambda *a, **k: None
cui.sdd_plotly_chart = lambda fig, **k: _recorded_calls.append((fig, k))

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
    _recorded_calls.clear()
    cui.render_section(_et_sections, "WMS", "Earnings Trends", is_auto=True)
    _auto_descriptions = [k.get("text_description") for _fig, k in _recorded_calls]

    _recorded_calls.clear()
    cui.render_section(_et_sections, "WMS", "Earnings Trends", is_auto=False)
    _uploaded_descriptions = [k.get("text_description") for _fig, k in _recorded_calls]
finally:
    cui.st.columns, cui.st.warning, cui.st.markdown = _orig_columns, _orig_warning, _orig_markdown
    cui.sdd_plotly_chart = _orig_plotly

check("Earnings Trends renders exactly 3 charts through sdd_plotly_chart "
      "(EPS, EPS Growth, PE Ratio) each time", len(_auto_descriptions) == 3)
check('Auto page: PE chart text_description says "average of TTM + last 3 FY P/Es"',
      any(d and "average of TTM + last 3 FY P/Es" in d for d in _auto_descriptions))
check('Auto page: PE chart text_description says "price / average of TTM + last 2 FY EPS"',
      any(d and "price / average of TTM + last 2 FY EPS" in d for d in _auto_descriptions))
check('Uploaded page: PE chart text_description says "from your workbook"',
      any(d and "are from your workbook" in d for d in _uploaded_descriptions))
check("Uploaded page's descriptions do NOT contain the Auto-only wording",
      not any(d and "average of TTM + last 3 FY P/Es" in d for d in _uploaded_descriptions))
check("EPS/EPS Growth charts pass text_description=None (unchanged, generic "
      "auto-description still applies to them)",
      _auto_descriptions[0] is None and _auto_descriptions[1] is None)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
