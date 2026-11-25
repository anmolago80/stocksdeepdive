"""
TASK 79 (Director-directed, owner-approved, 24 Nov 2026) - "IV/BV vs
Share Price" chart fixes from the owner's own WMS screenshot (Auto +
uploaded pages).

A. No stepped line (owner decision): the IV/BV index is now a plain
   connected line (line_shape="linear", markers at each point) - one
   point per fiscal year end plus the TTM point at today, straight
   segments between them. The previous "hv" step shape is gone.
B. Partial period plotted in the future (CONFIRMED BUG): a reporting
   period's NOMINAL end date can be in the future before it's fully
   elapsed - the chart must plot that (to date) point at TODAY, never
   its own nominal end, and the x-axis itself must never show a date
   past today (an explicit `xaxis.range` upper bound, on top of the
   per-point clamp, since Plotly's own default autorange padding on a
   sparse half-yearly/quarterly date axis can visually extend past the
   last real point otherwise - the owner's own "ran to ~Jan 2027").
C. Reporting-cadence diagnostics (diagnose only, no rule change):
   auto_compounder_engine._iv_bv_vs_price_reporting_cadence() now logs
   one "[ivbv_cadence] <TICKER> cadence=<x> source=<income_q|cashflow_q
   |suffix> cols=<ISO dates> nonnull_revenue=<per column True/False>"
   line every time it decides a cadence.
D. Text description updated to the Director's own exact wording.
E. Tooltip CONTENT is unchanged; no valuation/scoring/cache-logic
   change; dates stay ISO strings (TASK 76a); no ENGINE_VERSION bump
   needed (chart rendering + a log line only - the cached section data
   SHAPE is unchanged, so nothing needs a forced recompute).

Run: python3 tests/test_director_task79_ivbv_vs_price_line_fixes.py
"""
import datetime
import io
import os
import sys
from contextlib import redirect_stdout
from unittest import mock

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


def _d(s):
    return datetime.date.fromisoformat(s)


TODAY = datetime.date.today()


# ======================================================================
# PART A + B: a WMS-shaped scenario, reproducing the Director's own
# reported bug - cadence wrongly "quarterly" (Part C's own subject),
# a partial quarter whose NOMINAL end is well past today.
# ======================================================================
_anchor = _d("2026-03-31")  # latest FY end on file (31 March, like WMS)
_fy_points = [
    {"date": _d("2024-03-31"), "label": "FY2024", "ratio": 2.0},
    {"date": _d("2025-03-31"), "label": "FY2025", "ratio": 2.2},
    {"date": _d("2026-03-31"), "label": "FY2026", "ratio": 2.5},
]
_ttm_ratio = 2.6

_monthly_dates, _monthly_prices = [], []
_dd = _d("2023-12-31")
_price = 30.0
while _dd <= TODAY:
    _monthly_dates.append(_dd)
    _monthly_prices.append(_price)
    _price += 0.3
    _month, _year = _dd.month + 1, _dd.year
    if _month > 12:
        _month, _year = 1, _year + 1
    import calendar
    _last_day = calendar.monthrange(_year, _month)[1]
    _dd = datetime.date(_year, _month, _last_day)

_wms_result = ace._iv_bv_vs_price_core(
    _fy_points, _ttm_ratio, _anchor, "quarterly", _monthly_dates, _monthly_prices)
check("WMS-shaped fixture: _iv_bv_vs_price_core() returns a result", _wms_result is not None)

_last_period = _wms_result["periods"][-1]
check("WMS-shaped fixture: the last period really is partial (its own nominal end IS in the future)",
      _last_period["is_partial"] and _d(_last_period["end"]) > TODAY)

_fig, _reason = cui._cp_iv_bv_vs_price_chart("WMS.AX", {"WMS.AX": _wms_result})
check("WMS-shaped fixture: a chart renders (no caption)", _fig is not None and _reason is None)

if _fig is not None:
    _ivbv_trace, _price_trace = _fig.data[0], _fig.data[1]

    # ---- PART A ----
    check('PART A: IV/BV trace line_shape is "linear" (no step/"hv" any more)',
          _ivbv_trace.line.shape == "linear")
    check("PART A: IV/BV trace has exactly one point per fiscal year plus one TTM point at today "
          "(3 FY + 1 TTM = 4 points, nothing extra/interpolated)",
          len(_ivbv_trace.x) == 4)
    check("PART A: every FY point sits at that year's own real end date",
          list(_ivbv_trace.x[:3]) == [_d("2024-03-31"), _d("2025-03-31"), _d("2026-03-31")])
    check("PART A: the TTM point sits at TODAY, not some other date",
          _ivbv_trace.x[-1] == TODAY)

    # ---- PART B ----
    check("PART B (CONFIRMED BUG FIX): the partial period's x is TODAY, "
          "never its own (future) nominal end date",
          _price_trace.x[-1] == TODAY)
    check("PART B: no point on EITHER trace is ever after today",
          all(x <= TODAY for x in list(_ivbv_trace.x) + list(_price_trace.x)))
    check("PART B: the chart's own x-axis range never extends past today "
          "(an explicit upper bound, not just relying on the last data point)",
          _fig.layout.xaxis.range is not None and _fig.layout.xaxis.range[1] == TODAY)

    # ---- PART E: tooltip CONTENT unchanged ----
    check('PART E: IV/BV tooltip still matches the spec shape, e.g. "100 (2.0x, FY2024)" - '
          "base year = FY2024 here",
          _ivbv_trace.text[0] == "100 (2.0x, FY2024)")
    check('PART E: IV/BV TTM tooltip still says "TTM"',
          "TTM" in _ivbv_trace.text[-1])
    check('PART E: price tooltip for the partial period still ends with the period\'s own '
          '"(to date)" label, not a date',
          _price_trace.text[-1].endswith(")") and "(to date)" in _price_trace.text[-1])


# ======================================================================
# PART D: the exact new text_description wording, via render_section().
# ======================================================================
class _FakeCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_captured_text_descriptions = []
_orig_sdd_plotly_chart = cui.sdd_plotly_chart


def _spy_sdd_plotly_chart(fig, *a, **k):
    _captured_text_descriptions.append(k.get("text_description"))
    return None


_orig_columns, _orig_warning, _orig_markdown = cui.st.columns, cui.st.warning, cui.st.markdown
_orig_caption = cui.st.caption
cui.st.columns = lambda n: [_FakeCtx() for _ in range(n)]
cui.st.warning = lambda *a, **k: None
cui.st.markdown = lambda *a, **k: None
cui.st.caption = lambda *a, **k: None
cui.sdd_plotly_chart = _spy_sdd_plotly_chart
try:
    _sections_with_chart = {
        "Value vs Book": {
            "metrics": [], "iv_bv_series": {}, "iv_bv_vs_price": {"WMS.AX": _wms_result},
            "fcf_growth": {}, "book_value_growth": {},
        }
    }
    _captured_text_descriptions.clear()
    cui.render_section(_sections_with_chart, "WMS.AX", "Value vs Book", is_auto=True)
finally:
    cui.st.columns, cui.st.warning, cui.st.markdown = _orig_columns, _orig_warning, _orig_markdown
    cui.st.caption = _orig_caption
    cui.sdd_plotly_chart = _orig_sdd_plotly_chart

_ivbv_price_descriptions = [d for d in _captured_text_descriptions if d and "Share price" in d]
check("PART D: exactly one text_description was captured for this chart",
      len(_ivbv_price_descriptions) == 1)
if _ivbv_price_descriptions:
    check(
        "PART D: the text_description matches the Director's exact new wording",
        _ivbv_price_descriptions[0] == (
            "Share price = average price in each reporting period. "
            "IV/BV is known only at each annual result and TTM; the "
            "line between those points is drawn straight, not "
            "measured. Yearly IV/BV uses today's growth, discount "
            "rate and share count - read the shape, not exact "
            "history."
        ),
    )
    check('PART D: the old "drawn as steps" wording is gone',
          "steps" not in _ivbv_price_descriptions[0])


# ======================================================================
# PART C: cadence diagnostic logging - exact format, both real-source
# branches (income_q/cashflow_q) and the suffix fallback, with and
# without a resolvable revenue row (for the nonnull_revenue flags).
# ======================================================================
_income_q_with_revenue = pd.DataFrame(
    {
        "2026-09-30": [0.25, 1000.0], "2026-06-30": [0.25, None], "2026-03-31": [0.25, 900.0],
    },
    index=["Basic EPS", "Total Revenue"],
)
_buf = io.StringIO()
with redirect_stdout(_buf):
    _cadence = ace._iv_bv_vs_price_reporting_cadence({"income_q": _income_q_with_revenue}, "CPRT")
_log_line = next((ln for ln in _buf.getvalue().splitlines() if ln.startswith("[ivbv_cadence]")), None)
check("PART C: a [ivbv_cadence] log line is emitted for an income_q-sourced cadence decision",
      _log_line is not None)
if _log_line:
    check("PART C: the log line names the ticker, cadence, and source=income_q",
          "CPRT" in _log_line and f"cadence={_cadence}" in _log_line and "source=income_q" in _log_line)
    check("PART C: the log line's cols= lists the exact column ISO dates used",
          "cols=2026-09-30,2026-06-30,2026-03-31" in _log_line)
    check("PART C: the log line's nonnull_revenue= correctly flags the one null revenue column "
          "(True,False,True - middle column's Total Revenue is None)",
          "nonnull_revenue=True,False,True" in _log_line)

_buf2 = io.StringIO()
with redirect_stdout(_buf2):
    ace._iv_bv_vs_price_reporting_cadence({"income_q": None, "cashflow_q": None}, "ABC.AX")
_log_line2 = next((ln for ln in _buf2.getvalue().splitlines() if ln.startswith("[ivbv_cadence]")), None)
check("PART C: the suffix-fallback path also logs, with source=suffix and empty cols/nonnull_revenue",
      _log_line2 is not None and "source=suffix" in _log_line2
      and "cols=" in _log_line2 and "nonnull_revenue=" in _log_line2)

_buf3 = io.StringIO()
_cashflow_q = pd.DataFrame({"2026-06-30": [1.0], "2026-03-31": [1.0]}, index=["Free Cash Flow"])
with redirect_stdout(_buf3):
    ace._iv_bv_vs_price_reporting_cadence({"income_q": None, "cashflow_q": _cashflow_q}, "NVO")
_log_line3 = next((ln for ln in _buf3.getvalue().splitlines() if ln.startswith("[ivbv_cadence]")), None)
check("PART C: cashflow_q is logged as the source when income_q is unavailable but cashflow_q isn't",
      _log_line3 is not None and "source=cashflow_q" in _log_line3)

check("PART C: the detection RULE itself is unchanged (median gap <=135 days = quarterly, "
      "same threshold as before this task)",
      "<= 135" in __import__("inspect").getsource(ace._iv_bv_vs_price_reporting_cadence))


# ======================================================================
# Uploaded-page path (TASK 78's own helper) - same Part A/B fixes apply
# identically, since both share _iv_bv_vs_price_core().
# ======================================================================
_bundle_upload = {
    "income": pd.DataFrame({"2026-03-31": [1.0], "2025-03-31": [1.0]}, index=["Basic EPS"]),
    "income_q": pd.DataFrame(
        {"2026-09-30": [0.25], "2026-06-30": [0.25], "2026-03-31": [0.25]}, index=["Basic EPS"]),
}
_years_upload = ["TTM", "2026", "2025", "2024"]
_ratios_upload = [2.6, 2.5, 2.2, 2.0]
_price_hist_upload = {
    "dates": [d.isoformat() for d in _monthly_dates],
    "prices": _monthly_prices,
}
with mock.patch.object(bcd.fundamentals_data, "get_bundle", return_value=_bundle_upload):
    _upload_result = bcd._iv_bv_vs_price_for_upload(
        "AUB.AX", _years_upload, _ratios_upload, _price_hist_upload)
check("uploaded-page fixture: _iv_bv_vs_price_for_upload() returns real chart data",
      _upload_result is not None and "no_chart_reason" not in _upload_result)

_fig_up, _reason_up = cui._cp_iv_bv_vs_price_chart("AUB.AX", {"AUB.AX": _upload_result})
check("uploaded-page fixture: a chart renders", _fig_up is not None and _reason_up is None)
if _fig_up is not None:
    check("uploaded-page fixture: IV/BV trace is also linear (shared core/chart code - "
          "TASK 78's whole point was no second copy of this logic)",
          _fig_up.data[0].line.shape == "linear")
    check("uploaded-page fixture: no point on either trace is after today",
          all(x <= TODAY for x in list(_fig_up.data[0].x) + list(_fig_up.data[1].x)))
    check("uploaded-page fixture: the x-axis range also never extends past today",
          _fig_up.layout.xaxis.range[1] == TODAY)


# ======================================================================
# JSON round trip - confirm dates are still plain ISO strings (TASK
# 76a's own rule, unchanged) and the chart built from a round-tripped
# copy is pixel-identical to the direct one, with the new Part A/B
# fixes surviving the round trip too.
# ======================================================================
import json

_round_tripped = json.loads(json.dumps({"WMS.AX": _wms_result}))
check("JSON round trip: every date field is still a plain string, never a raw date object",
      all(isinstance(pt["date"], str) for pt in _round_tripped["WMS.AX"]["fy_points"])
      and all(isinstance(pr["end"], str) and isinstance(pr["start"], str)
              for pr in _round_tripped["WMS.AX"]["periods"]))

_fig_direct, _ = cui._cp_iv_bv_vs_price_chart("WMS.AX", {"WMS.AX": _wms_result})
_fig_rt, _ = cui._cp_iv_bv_vs_price_chart("WMS.AX", _round_tripped)
check("JSON round trip: the chart rendered from the round-tripped copy is identical to the direct one",
      _fig_direct is not None and _fig_rt is not None and _fig_direct.to_json() == _fig_rt.to_json())


# ======================================================================
# PART E: no ENGINE_VERSION bump for this task - the cached section
# data SHAPE is unchanged (only chart rendering + a log line changed),
# so nothing needs a forced recompute.
# ======================================================================
check("PART E: ENGINE_VERSION is >= 56 (TASK 76a's own bump, not bumped further by this task) - "
      "TASK 79 is a chart-rendering + logging change only, no cached data shape changed",
      ace.ENGINE_VERSION >= 56)


print()
print(f"PASS={passed} FAIL={failed}")
