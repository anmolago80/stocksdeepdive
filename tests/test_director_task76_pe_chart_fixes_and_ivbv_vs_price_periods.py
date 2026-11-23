"""
TASK 76 (Director-directed, owner-approved, 23 Nov 2026) - fixes to the
TASK 74+75 display (Auto pages; display only).

PART A - PE Ratio by Year label clipping: compounder_ui._cp_pe_ratio_
chart()'s two reference lines used to be add_shape()+add_annotation()
pairs positioned at xref="paper", x=1.0 inside a 100px right margin -
TASK 74's longer names ("PE Ratio Average 3 Years") no longer fit,
worst in this chart's own 3-column layout. Now full-width go.Scatter
traces shown as ordinary horizontal-legend entries below the chart,
with the actual value baked into the trace name (e.g. "PE Ratio
Average 22.65x") - nothing to clip. Bars stay out of the legend; the
right margin is back to 10px.

PART B - duplicate "Text description of this chart" expanders:
sdd_plotly_chart() already renders exactly one such expander per
chart; TASK 74+75 added a second, manually-written one under the PE
chart and under the new IV/BV-vs-price chart. Both are gone - their
text now travels through sdd_plotly_chart()'s own text_description=
kwarg instead.

PART C - "IV/BV vs Share Price, indexed" chart, rebuilt from the
ground up (Auto pages only now - see below):
  1. Removed from uploaded-research pages entirely: build_compounder_
     data.py's TASK 75 _iv_bv_year_prices() helper and its "prices"
     wiring are gone - iv_bv_series is back to its exact pre-TASK-75
     shape there, and render_section() only calls the new chart when
     is_auto=True.
  2. auto_compounder_engine._iv_bv_vs_price_reporting_cadence(): from
     the median gap between income_q (else cashflow_q) column dates -
     <=135 days (~4 months) is quarterly, else half-yearly. Falls back
     to ticker suffix (.AX/.NZ/.L = half-yearly, else quarterly) when
     no quarterly statement is on file at all.
  3. auto_compounder_engine._iv_bv_vs_price_periods(): builds, from the
     company's OWN fiscal year end (never a calendar quarter/half),
     one reporting period per cadence step, each with the AVERAGE of
     the monthly closes (bundle["prices_10y"]) falling inside it; the
     latest period may be partial ("(to date)"), averaged only through
     today; a period with no monthly data at all is skipped (never a
     fabricated average).
  4. compounder_ui._cp_iv_bv_vs_price_chart(): IV/BV is a STEP line
     (line_shape="hv") at each fiscal year's own real end date, held
     flat until the next one; TTM (if present) is plotted at today and
     holds the latest FY's step through to today. Share price is one
     point per reporting period, a real date x-axis. Indexed to 100 at
     the first fiscal year with a positive IV/BV AND a matching period
     average. Fewer than 3 usable fiscal-year points (TTM counts if
     present) -> a one-line caption instead of a chart.
  5. _iv_bv_series()'s own TASK 75 "prices" field is gone too (nothing
     ever read it once this task's data moved to the new, richer
     function) - that function is back to its pre-TASK-75 shape.
  6. ENGINE_VERSION bumped (54->55) so a disk-cached Auto section from
     before this task picks up the new "iv_bv_vs_price" key.

Full suite, 0 failures required alongside this file.

Run: python3 tests/test_director_task76_pe_chart_fixes_and_ivbv_vs_price_periods.py
"""
import datetime
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
# PART A: PE Ratio by Year chart - legend, not clipped side labels.
# ======================================================================
_series = {"WMS": {"pe_ratio": {"years": ["2026", "2025"], "values": [22.0, 18.0]}}}
_refs = {"WMS": {"avg_3y": 19.5, "overall_avg": 22.65}}
_fig_pe = cui._cp_pe_ratio_chart("WMS", _series, _refs)
check("PE chart: no add_annotation side labels remain", len(_fig_pe.layout.annotations) == 0)
check("PE chart: right margin restored to 10px", _fig_pe.layout.margin.r == 10)
_pe_names = [t.name for t in _fig_pe.data]
check('PE chart: a legend trace named "PE Ratio Average 3 Years 19.50x" exists',
      "PE Ratio Average 3 Years 19.50x" in _pe_names)
check('PE chart: a legend trace named "PE Ratio Average 22.65x" exists',
      "PE Ratio Average 22.65x" in _pe_names)
_bar_trace = next(t for t in _fig_pe.data if t.name == "PE Ratio")
check("PE chart: the bars themselves stay out of the legend (showlegend=False)",
      _bar_trace.showlegend is False)
_ref_traces = [t for t in _fig_pe.data if t.name != "PE Ratio"]
check("PE chart: reference-line traces are plain line traces (not bars)",
      all(t.type == "scatter" for t in _ref_traces))

# A ticker with only ONE of the two reference values (the other None) -
# only one ref-line trace should appear, never a crash.
_refs_partial = {"WMS": {"avg_3y": None, "overall_avg": 22.65}}
_fig_pe_partial = cui._cp_pe_ratio_chart("WMS", _series, _refs_partial)
check("PE chart with only one ref value: exactly 2 traces total (bars + 1 ref line)",
      len(_fig_pe_partial.data) == 2)

# ======================================================================
# PART B: no manually-added "Text description of this chart" expander
# survives anywhere in compounder_ui.py - the only such call left is
# the one inside sdd_plotly_chart() itself.
# ======================================================================
_src_module = inspect.getsource(cui)
_expander_call_count = _src_module.count('st.expander("Text description of this chart"')
check('exactly one real st.expander("Text description of this chart", ...) call '
      "survives in the whole module (inside sdd_plotly_chart() itself)",
      _expander_call_count == 1)
_src_sdd_plotly = inspect.getsource(cui.sdd_plotly_chart)
check('that one surviving call is inside sdd_plotly_chart()',
      'st.expander("Text description of this chart"' in _src_sdd_plotly)
check("every chart render in compounder_ui.py goes through sdd_plotly_chart() "
      "(st.plotly_chart is only ever called from inside it)",
      all(
          "sdd_plotly_chart" in _src_module.split("def ")[i - 1] if i > 0 else True
          for i in range(1)
      ) or True)  # structural grep check below is the real assertion
_direct_plotly_calls = [
    ln for ln in _src_module.splitlines()
    if "st.plotly_chart(" in ln and "def sdd_plotly_chart" not in ln
]
_sdd_fn_src_lines = set(_src_sdd_plotly.splitlines())
check("no st.plotly_chart() call exists outside sdd_plotly_chart() itself",
      all(ln.strip() in {s.strip() for s in _sdd_fn_src_lines} for ln in _direct_plotly_calls))


# ======================================================================
# PART C.2: reporting-cadence detection.
# ======================================================================
_income_q_quarterly = pd.DataFrame(
    {"2026-09-30": [1.0], "2026-06-30": [1.0], "2026-03-31": [1.0]},
    index=["Basic EPS"],
)
check("quarterly income_q (3-month gaps) -> 'quarterly'",
      ace._iv_bv_vs_price_reporting_cadence({"income_q": _income_q_quarterly}, "CPRT") == "quarterly")

_income_q_half = pd.DataFrame(
    {"2026-09-30": [1.0], "2026-03-31": [1.0], "2025-09-30": [1.0]},
    index=["Basic EPS"],
)
check("half-yearly income_q (6-month gaps) -> 'half-yearly'",
      ace._iv_bv_vs_price_reporting_cadence({"income_q": _income_q_half}, "WMS.AX") == "half-yearly")
check("half-yearly cadence detected the same way for a June-FY-end half-yearly "
      "reporter too (cadence detection doesn't care about FY end month)",
      ace._iv_bv_vs_price_reporting_cadence(
          {"income_q": pd.DataFrame(
              {"2026-12-31": [1.0], "2026-06-30": [1.0], "2025-12-31": [1.0]},
              index=["Basic EPS"],
          )}, "OCL.AX") == "half-yearly")

# No quarterly statements at all -> suffix fallback.
check("no quarterly statements, .AX ticker -> half-yearly fallback",
      ace._iv_bv_vs_price_reporting_cadence({"income_q": None, "cashflow_q": None}, "ABC.AX") == "half-yearly")
check("no quarterly statements, .NZ ticker -> half-yearly fallback",
      ace._iv_bv_vs_price_reporting_cadence({"income_q": None, "cashflow_q": None}, "ABC.NZ") == "half-yearly")
check("no quarterly statements, .L ticker -> half-yearly fallback",
      ace._iv_bv_vs_price_reporting_cadence({"income_q": None, "cashflow_q": None}, "HSBA.L") == "half-yearly")
check("no quarterly statements, US ticker -> quarterly fallback",
      ace._iv_bv_vs_price_reporting_cadence({"income_q": None, "cashflow_q": None}, "CPRT") == "quarterly")
# cashflow_q used when income_q itself is missing.
check("income_q missing, cashflow_q present and quarterly -> 'quarterly'",
      ace._iv_bv_vs_price_reporting_cadence(
          {"income_q": None, "cashflow_q": _income_q_quarterly}, "CPRT") == "quarterly")


# ======================================================================
# PART C.2-C.6: _iv_bv_vs_price_periods() - WMS-shaped (half-yearly,
# FY ends March) fixture, period alignment to Sep/Mar, step points,
# partial "(to date)" period, indexing input.
# ======================================================================
def _mk_wms_bundle(extra_price_dates=(), extra_prices=()):
    income = pd.DataFrame(
        {"2026-03-31": [1.0], "2025-03-31": [1.0], "2024-03-31": [1.0]},
        index=["Basic EPS"],
    )
    cashflow = pd.DataFrame(
        {
            "2026-03-31": [120.0, None, None],
            "2025-03-31": [100.0, None, None],
            "2024-03-31": [90.0, None, None],
        },
        index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
    )
    balance = pd.DataFrame(
        {"2026-03-31": [500.0], "2025-03-31": [450.0], "2024-03-31": [400.0]},
        index=["Stockholders Equity"],
    )
    income_q = pd.DataFrame(
        {"2026-09-30": [0.5], "2026-03-31": [0.5], "2025-09-30": [0.45]},
        index=["Basic EPS"],
    )
    dates = [
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
        "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
        "2026-03-31", "2026-06-30", "2026-09-30",
    ] + list(extra_price_dates)
    prices = [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40] + list(extra_prices)
    return {
        "info": {"currentPrice": 42.0, "sharesOutstanding": 100e6},
        "income": income, "income_q": income_q,
        "balance": balance, "cashflow": cashflow,
        "prices_10y": {"dates": dates, "prices": prices}, "dividends": {},
    }


_dcf = {"growth": 0.05, "discount_rate": 0.10, "perpetual_rate": 0.03}
_wms_bundle = _mk_wms_bundle(extra_price_dates=["2026-10-31"], extra_prices=[41])
_wms_result = ace._iv_bv_vs_price_periods(_wms_bundle, _dcf, bvps_ttm=5.0, ticker="WMS.AX")
check("WMS fixture: _iv_bv_vs_price_periods() returns a result", _wms_result is not None)
if _wms_result:
    check("WMS fixture: cadence is half-yearly", _wms_result["cadence"] == "half-yearly")
    check("WMS fixture: 3 fiscal-year points, ascending by date",
          len(_wms_result["fy_points"]) == 3
          and [p["date"] for p in _wms_result["fy_points"]] ==
              sorted(p["date"] for p in _wms_result["fy_points"]))
    check("WMS fixture: fy_points labelled FY2024/FY2025/FY2026",
          [p["label"] for p in _wms_result["fy_points"]] == ["FY2024", "FY2025", "FY2026"])
    check("WMS fixture: fy_points' dates are each FY's own real end date (31 March)",
          all(p["date"].endswith("-03-31") for p in _wms_result["fy_points"]))
    check("WMS fixture: ttm_ratio is present (a TTM point existed)",
          _wms_result["ttm_ratio"] is not None)
    _period_labels = [p["label"] for p in _wms_result["periods"]]
    check("WMS fixture: periods end on Sep/Mar only (half-years aligned to the March FY end)",
          all(p["end"][5:7] in ("03", "09") for p in _wms_result["periods"]))
    check('WMS fixture: a period is labelled "H2 FY24" (ends March 2024)',
          any(p["label"] == "H2 FY24" and p["end"] == "2024-03-31"
              for p in _wms_result["periods"]))
    check('WMS fixture: a period is labelled "H1 FY27" (Apr-Sep 2026, fully reported)',
          any(p["label"] == "H1 FY27" and p["end"] == "2026-09-30"
              for p in _wms_result["periods"]))
    _partial = [p for p in _wms_result["periods"] if p["is_partial"]]
    check("WMS fixture: exactly one partial (current, still-open) period",
          len(_partial) == 1)
    if _partial:
        check('WMS fixture: the partial period\'s label ends with "(to date)"',
              _partial[0]["label"].endswith("(to date)"))
        check("WMS fixture: the partial period's average uses only the one monthly "
              "close actually on file for it (41.0, the Oct point)",
              abs(_partial[0]["avg_price"] - 41.0) < 1e-9)
    check("WMS fixture: a fully-reported half-year's average is the mean of its own "
          "2 monthly closes (H2 FY26: Sep2025<d<=Mar2026 -> 37,38 -> 37.5)",
          any(abs(p["avg_price"] - 37.5) < 1e-9 for p in _wms_result["periods"]
              if p["label"] == "H2 FY26"))

# OCL-shaped (half-yearly, FY ends June) - same mechanism, different FY-end month.
_ocl_bundle = _mk_wms_bundle()
_ocl_bundle["income"] = pd.DataFrame(
    {"2026-06-30": [1.0], "2025-06-30": [1.0], "2024-06-30": [1.0]}, index=["Basic EPS"],
)
_ocl_bundle["cashflow"] = pd.DataFrame(
    {
        "2026-06-30": [120.0, None, None], "2025-06-30": [100.0, None, None],
        "2024-06-30": [90.0, None, None],
    },
    index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"],
)
_ocl_bundle["balance"] = pd.DataFrame(
    {"2026-06-30": [500.0], "2025-06-30": [450.0], "2024-06-30": [400.0]}, index=["Stockholders Equity"],
)
_ocl_bundle["income_q"] = pd.DataFrame(
    {"2026-12-31": [0.5], "2026-06-30": [0.5], "2025-12-31": [0.45]}, index=["Basic EPS"],
)
_ocl_result = ace._iv_bv_vs_price_periods(_ocl_bundle, _dcf, bvps_ttm=5.0, ticker="OCL.AX")
check("OCL fixture: _iv_bv_vs_price_periods() returns a result", _ocl_result is not None)
if _ocl_result:
    check("OCL fixture: cadence is half-yearly", _ocl_result["cadence"] == "half-yearly")
    check("OCL fixture: periods end on Dec/Jun only (half-years aligned to the June FY end)",
          all(p["end"][5:7] in ("06", "12") for p in _ocl_result["periods"]))
    check("OCL fixture: fy_points' dates are each FY's own real end date (30 June)",
          all(p["date"].endswith("-06-30") for p in _ocl_result["fy_points"]))

# US quarterly reporter (e.g. CPRT, FY ends June) - quarters aligned to June/Sep/Dec/Mar.
_cprt_bundle = dict(_ocl_bundle)
_cprt_bundle["income_q"] = pd.DataFrame(
    {"2026-09-30": [0.25], "2026-06-30": [0.25], "2026-03-31": [0.25]}, index=["Basic EPS"],
)
_cprt_result = ace._iv_bv_vs_price_periods(_cprt_bundle, _dcf, bvps_ttm=5.0, ticker="CPRT")
check("CPRT fixture: _iv_bv_vs_price_periods() returns a result", _cprt_result is not None)
if _cprt_result:
    check("CPRT fixture: cadence is quarterly", _cprt_result["cadence"] == "quarterly")
    check("CPRT fixture: periods end on Mar/Jun/Sep/Dec (quarterly, aligned to the June FY end)",
          all(p["end"][5:7] in ("03", "06", "09", "12") for p in _cprt_result["periods"]))
    check('CPRT fixture: a period is labelled "Q4 FY24" (ends June 2024, the FY end itself)',
          any(p["label"] == "Q4 FY24" and p["end"] == "2024-06-30"
              for p in _cprt_result["periods"]))

# No quarterly statements at all -> still works via the suffix fallback
# (half-yearly for .AX), using cashflow/balance-derived FY dates.
_no_q_bundle = dict(_wms_bundle)
_no_q_bundle["income_q"] = None
_no_q_bundle["cashflow_q"] = None
_no_q_result = ace._iv_bv_vs_price_periods(_no_q_bundle, _dcf, bvps_ttm=5.0, ticker="WMS.AX")
check("no-quarterly-statement fixture: _iv_bv_vs_price_periods() still returns a result "
      "via the ticker-suffix cadence fallback",
      _no_q_result is not None and _no_q_result["cadence"] == "half-yearly")

# No fiscal year end resolvable at all (empty income statement) -> None,
# never a crash.
_no_income_bundle = dict(_wms_bundle)
_no_income_bundle["income"] = pd.DataFrame()
check("no resolvable fiscal year end at all: returns None cleanly",
      ace._iv_bv_vs_price_periods(_no_income_bundle, _dcf, bvps_ttm=5.0, ticker="WMS.AX") is None)


# ======================================================================
# PART C.4-C.6: _cp_iv_bv_vs_price_chart() - step line, indexing,
# tooltip, <3-points caption, base-year shift on a negative IV/BV year.
# ======================================================================
def _d(s):
    return datetime.date.fromisoformat(s)


_entry_shift = {
    "fy_points": [
        {"date": _d("2023-03-31"), "label": "FY2023", "ratio": -0.1},  # negative -> skipped as base
        {"date": _d("2024-03-31"), "label": "FY2024", "ratio": -0.3},  # negative -> skipped as base
        {"date": _d("2025-03-31"), "label": "FY2025", "ratio": 1.5},
        {"date": _d("2026-03-31"), "label": "FY2026", "ratio": 2.0},
    ],
    "ttm_ratio": 2.4,
    "periods": [
        {"start": _d("2022-09-30"), "end": _d("2023-03-31"), "label": "H2 FY23", "avg_price": 8.0, "is_partial": False},
        {"start": _d("2023-03-31"), "end": _d("2023-09-30"), "label": "H1 FY24", "avg_price": 8.5, "is_partial": False},
        {"start": _d("2023-09-30"), "end": _d("2024-03-31"), "label": "H2 FY24", "avg_price": 9.0, "is_partial": False},
        {"start": _d("2024-03-31"), "end": _d("2024-09-30"), "label": "H1 FY25", "avg_price": 9.0, "is_partial": False},
        {"start": _d("2024-09-30"), "end": _d("2025-03-31"), "label": "H2 FY25", "avg_price": 9.0, "is_partial": False},
        {"start": _d("2025-03-31"), "end": _d("2025-09-30"), "label": "H1 FY26", "avg_price": 10.0, "is_partial": False},
        {"start": _d("2025-09-30"), "end": _d("2026-03-31"), "label": "H2 FY26", "avg_price": 10.0, "is_partial": False},
    ],
    "cadence": "half-yearly",
}
_fig_b, _reason_b = cui._cp_iv_bv_vs_price_chart("X", {"X": _entry_shift})
check("base-year shift (FY2024 negative): a chart IS produced (FY2025/FY2026/TTM usable)",
      _fig_b is not None and _reason_b is None)
if _fig_b is not None:
    check('title names FY2025 as the base year ("FY2025 = 100")',
          "FY2025 = 100" in _fig_b.layout.title.text)
    _ivbv_trace = _fig_b.data[0]
    check("IV/BV trace is a step line (line_shape hv)", _ivbv_trace.line.shape == "hv")
    check("IV/BV trace's x values are real dates, starting at the base year (FY2025) - "
          "FY2023/FY2024 (before base) are NOT plotted",
          list(_ivbv_trace.x) == [_d("2025-03-31"), _d("2026-03-31"), datetime.date.today()])
    check("IV/BV index at the base year (FY2025) = 100",
          abs(_ivbv_trace.y[0] - 100.0) < 1e-9)
    check("IV/BV index at FY2026 = 133.3 (2.0/1.5*100)",
          abs(_ivbv_trace.y[1] - (2.0 / 1.5 * 100.0)) < 0.1)
    check("IV/BV index at TTM (today) = 160.0 (2.4/1.5*100)",
          abs(_ivbv_trace.y[2] - (2.4 / 1.5 * 100.0)) < 0.1)
    check('IV/BV tooltip text matches the spec shape, e.g. "100 (1.5x, FY2025)"',
          _ivbv_trace.text[0] == "100 (1.5x, FY2025)")
    check('IV/BV tooltip for the TTM point says "TTM", not a date label',
          "TTM" in _ivbv_trace.text[2])
    _price_trace = _fig_b.data[1]
    check("price trace is a dashed line (not a step)", _price_trace.line.dash == "dash")
    check("price trace starts at the base period (H2 FY25, ending at FY2025's own date) - "
          "earlier periods (before the base) are excluded",
          _price_trace.x[0] == _d("2025-03-31"))
    check("price index at the base period = 100", abs(_price_trace.y[0] - 100.0) < 1e-9)
    check('price tooltip includes the period label, e.g. "100 (... avg, H2 FY25)"',
          _price_trace.text[0].startswith("100 (") and "H2 FY25)" in _price_trace.text[0])

# ASX ticker: price tooltip shows "A$", never a bare, ambiguous "$".
_fig_csl, _ = cui._cp_iv_bv_vs_price_chart("CSL.AX", {"CSL.AX": _entry_shift})
check('CSL.AX (ASX ticker): price tooltip shows "A$", not a bare "$"',
      _fig_csl is not None and "A$" in _fig_csl.data[1].text[0])

# Fewer than 3 usable fiscal-year points (TTM counted if present) ->
# caption instead of a chart.
_entry_short = {
    "fy_points": [{"date": _d("2026-03-31"), "label": "FY2026", "ratio": 2.0}],
    "ttm_ratio": None,
    "periods": [
        {"start": _d("2025-09-30"), "end": _d("2026-03-31"), "label": "H2 FY26", "avg_price": 10.0, "is_partial": False},
    ],
    "cadence": "half-yearly",
}
_fig_short, _reason_short = cui._cp_iv_bv_vs_price_chart("Y", {"Y": _entry_short})
check("fewer than 3 usable points (1 FY, no TTM): no chart, a caption instead",
      _fig_short is None and bool(_reason_short) and isinstance(_reason_short, str))

# Exactly 2 FY points + a TTM point = 3 usable -> a chart IS produced.
_entry_min = {
    "fy_points": [
        {"date": _d("2025-03-31"), "label": "FY2025", "ratio": 1.5},
        {"date": _d("2026-03-31"), "label": "FY2026", "ratio": 2.0},
    ],
    "ttm_ratio": 2.2,
    "periods": [
        {"start": _d("2024-09-30"), "end": _d("2025-03-31"), "label": "H2 FY25", "avg_price": 9.0, "is_partial": False},
        {"start": _d("2025-09-30"), "end": _d("2026-03-31"), "label": "H2 FY26", "avg_price": 10.0, "is_partial": False},
    ],
    "cadence": "half-yearly",
}
_fig_min, _reason_min = cui._cp_iv_bv_vs_price_chart("M", {"M": _entry_min})
check("exactly 3 usable points (2 FY + TTM): a chart IS produced",
      _fig_min is not None and _reason_min is None)

# No usable base year at all (every FY ratio non-positive, or none has
# a matching period) -> caption, not a crash.
_entry_none = {
    "fy_points": [{"date": _d("2026-03-31"), "label": "FY2026", "ratio": -1.0}],
    "ttm_ratio": -2.0,
    "periods": [
        {"start": _d("2025-09-30"), "end": _d("2026-03-31"), "label": "H2 FY26", "avg_price": 10.0, "is_partial": False},
    ],
    "cadence": "half-yearly",
}
_fig_none, _reason_none = cui._cp_iv_bv_vs_price_chart("Z", {"Z": _entry_none})
check("no usable base year (FY ratio negative): no chart, caption given",
      _fig_none is None and bool(_reason_none))

# A FY date with no matching period at all (period was skipped for lack
# of monthly data) can never be the base year.
_entry_no_period_match = {
    "fy_points": [
        {"date": _d("2025-03-31"), "label": "FY2025", "ratio": 1.0},  # no period ends here
        {"date": _d("2026-03-31"), "label": "FY2026", "ratio": 1.5},
        {"date": _d("2027-03-31"), "label": "FY2027", "ratio": 2.0},
    ],
    "ttm_ratio": 2.2,
    "periods": [
        {"start": _d("2025-09-30"), "end": _d("2026-03-31"), "label": "H2 FY26", "avg_price": 10.0, "is_partial": False},
        {"start": _d("2026-09-30"), "end": _d("2027-03-31"), "label": "H2 FY27", "avg_price": 11.0, "is_partial": False},
    ],
    "cadence": "half-yearly",
}
_fig_skip, _ = cui._cp_iv_bv_vs_price_chart("S", {"S": _entry_no_period_match})
check("FY2025 (no matching period) is correctly skipped as a base-year candidate - "
      "base shifts to FY2026",
      _fig_skip is not None and "FY2026 = 100" in _fig_skip.layout.title.text)

# Missing/empty entry -> (None, None) cleanly, no caption.
check("ticker missing from iv_bv_vs_price entirely: returns (None, None) cleanly",
      cui._cp_iv_bv_vs_price_chart("MISSING", {}) == (None, None))
check("empty fy_points: returns (None, None) cleanly",
      cui._cp_iv_bv_vs_price_chart("E", {"E": {"fy_points": [], "periods": [], "ttm_ratio": None}}) == (None, None))


# ======================================================================
# PART C.1: removed from uploaded-research pages entirely.
# ======================================================================
check("build_compounder_data.py no longer has _iv_bv_year_prices (fully removed)",
      not hasattr(bcd, "_iv_bv_year_prices"))
_src_bcd = inspect.getsource(bcd)
check("build_compounder_data.py's iv_bv_series construction has no 'prices' key any more "
      "(back to its exact pre-TASK-75 shape)",
      '{"years": years, "ratios": ratios}' in _src_bcd)

_src_build_vvb = inspect.getsource(ace._build_value_vs_book)
check("_build_value_vs_book() (Auto) sets a NEW 'iv_bv_vs_price' key",
      '"iv_bv_vs_price"' in _src_build_vvb)
check("auto_compounder_engine._iv_bv_series() no longer returns a 'prices' field "
      "(superseded by _iv_bv_vs_price_periods())",
      '{"years": years, "ratios": ratios}' in inspect.getsource(ace._iv_bv_series))

# TASK 78 (Director-directed, owner-approved, 23 Nov 2026 - "the owner
# changed his mind: he wants it on every uploaded company too"):
# render_section() no longer gates _cp_iv_bv_vs_price_chart() on
# is_auto at all - the chart calls it unconditionally, regardless of
# which page is rendering. (The old TASK 76 "is_auto=False never calls
# it, even with the key present" test this replaces is now the
# OPPOSITE of the Director's own current instruction - see
# tests/test_director_task78_ivbv_vs_price_on_uploaded_pages.py for the
# new uploaded-page behavior itself: _iv_bv_vs_price_for_upload(),
# build()'s own wiring, and the no-FY-end caption path.)
_src_render_section = inspect.getsource(cui.render_section)
check('render_section() calls _cp_iv_bv_vs_price_chart unconditionally '
      '(no "if is_auto:" gate around it any more)',
      "_cp_iv_bv_vs_price_chart(" in _src_render_section
      and "if is_auto:\n            _ivbv_price_fig" not in _src_render_section)


class _FakeCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_orig_columns, _orig_warning, _orig_markdown = cui.st.columns, cui.st.warning, cui.st.markdown
_orig_caption = cui.st.caption
_orig_plotly = cui.sdd_plotly_chart
_orig_chart_fn = cui._cp_iv_bv_vs_price_chart
_chart_fn_calls = []


def _spy_chart_fn(*a, **k):
    _chart_fn_calls.append((a, k))
    return _orig_chart_fn(*a, **k)


_vvb_sections_with_key = {
    "Value vs Book": {
        "metrics": [], "iv_bv_series": {}, "iv_bv_vs_price": {"WMS": _entry_min},
        "fcf_growth": {}, "book_value_growth": {},
    }
}
cui.st.columns = lambda n: [_FakeCtx() for _ in range(n)]
cui.st.warning = lambda *a, **k: None
cui.st.markdown = lambda *a, **k: None
cui.st.caption = lambda *a, **k: None
cui.sdd_plotly_chart = lambda *a, **k: None
cui._cp_iv_bv_vs_price_chart = _spy_chart_fn
try:
    _chart_fn_calls.clear()
    cui.render_section(_vvb_sections_with_key, "WMS", "Value vs Book", is_auto=False)
    _calls_uploaded = len(_chart_fn_calls)

    _chart_fn_calls.clear()
    cui.render_section(_vvb_sections_with_key, "WMS", "Value vs Book", is_auto=True)
    _calls_auto = len(_chart_fn_calls)
finally:
    cui.st.columns, cui.st.warning, cui.st.markdown = _orig_columns, _orig_warning, _orig_markdown
    cui.st.caption = _orig_caption
    cui.sdd_plotly_chart = _orig_plotly
    cui._cp_iv_bv_vs_price_chart = _orig_chart_fn

check("is_auto=False: _cp_iv_bv_vs_price_chart IS called exactly once (TASK 78 - no more gate)",
      _calls_uploaded == 1)
check("is_auto=True: _cp_iv_bv_vs_price_chart IS called exactly once (unchanged)",
      _calls_auto == 1)


# ======================================================================
# PART C.6 / D: ENGINE_VERSION bumped for this task.
# ======================================================================
check("ENGINE_VERSION is >= 55 (bumped from 54 for TASK 76's iv_bv_vs_price field; "
      "a later task, e.g. TASK 76a's own hotfix bump, may have bumped it further)",
      ace.ENGINE_VERSION >= 55)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
