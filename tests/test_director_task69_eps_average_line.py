"""
TASK 69 (Director-directed, owner-approved, 20 Nov 2026) - Add an average
line to "EPS by Year".

WHERE: compounder_ui._cp_year_bar_chart(), whose only caller is "EPS by
Year" on the Earnings Trends tab (series key "eps").

DO:
  1. A dashed horizontal "Average" line, same style as the Share Price
     Growth chart (fig.add_hline, dash, #e6edf5, width 1.5, annotation
     top right): "Average $X.XX" using the chart's own currency format
     (_cp_format(..., fmt, ticker=ticker)).
  2. The average uses the fiscal-year bars ONLY - excludes any "TTM"-
     labeled point (it overlaps the latest FY and would double-count
     it). Annotation text: "Average $X.XX (FY only)".
  3. Fewer than 2 fiscal-year values -> no line at all.
  4. Negative EPS years are included in the average as-is.
  5. No other chart, data or calculation changes - confirmed via grep,
     _cp_year_bar_chart() has exactly one caller in the whole file.

Covers:
  - WMS-shaped fixture (FY 6.16, 6.52, 5.81, 5.48 + TTM 5.29) -> average
    line at 5.99, TTM excluded from both the line's value and the count.
  - Fewer than 2 FY values (0 and 1) -> no hline/annotation drawn.
  - A negative-EPS year is included in the average as reported (not
    dropped, not floored at zero).
  - The chart still renders (bars + average line) with hand-built
    workbook-shaped data (build_compounder_data.py's own {"years",
    "values"} dict, literal "TTM" first entry, same as the real
    pipeline's own _build_earnings_trends() shape).
  - _cp_year_bar_chart() still has exactly one call site in the file
    (no new/duplicated caller introduced, no other chart touched).

Run: python3 tests/test_director_task69_eps_average_line.py
"""
import os
import subprocess
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


import compounder_ui as cui


def _avg_annotations(fig):
    return [a.text for a in fig.layout.annotations if "Average" in (a.text or "")]


# ======================================================================
# CHECK 1-4: WMS-shaped fixture - FY 6.16, 6.52, 5.81, 5.48 (newest
# first) plus a TTM point of 5.29. Average = (6.16+6.52+5.81+5.48)/4 =
# 5.9925 -> "$5.99", TTM excluded from both the value and the count.
# ======================================================================
_wms_entry = {
    "years": ["TTM", "2026", "2025", "2024", "2023"],
    "values": [5.29, 6.16, 6.52, 5.81, 5.48],
}
_wms_fig = cui._cp_year_bar_chart("WMS", {"WMS": {"eps": _wms_entry}}, "eps", "EPS by Year", "EPS", fmt="cur")
check("WMS fixture: chart builds", _wms_fig is not None)

_wms_fy_values = [6.16, 6.52, 5.81, 5.48]
_wms_expected_avg = sum(_wms_fy_values) / len(_wms_fy_values)
check("WMS fixture: expected average is 5.9925 (-> displays as $5.99)",
      abs(_wms_expected_avg - 5.9925) < 1e-9)

_wms_annotations = _avg_annotations(_wms_fig)
check("WMS fixture: exactly one Average annotation, excluding TTM from the "
      "line's own value (5.99, not a TTM-including average)",
      _wms_fig is not None and len(_wms_annotations) == 1
      and "$5.99" in _wms_annotations[0] and "(FY only)" in _wms_annotations[0])
check("WMS fixture: the TTM bar itself is still plotted (only excluded from "
      "the average, not dropped from the chart)",
      _wms_fig is not None and "TTM" in list(_wms_fig.data[0].x)
      and list(_wms_fig.data[0].y)[list(_wms_fig.data[0].x).index("TTM")] == 5.29)


# ======================================================================
# CHECK 5-6: fewer than 2 fiscal-year values -> no line drawn.
# ======================================================================
_one_fy_entry = {"years": ["TTM", "2026"], "values": [5.29, 6.16]}
_one_fy_fig = cui._cp_year_bar_chart("WMS", {"WMS": {"eps": _one_fy_entry}}, "eps", "EPS by Year", "EPS", fmt="cur")
check("exactly 1 FY value (plus TTM): chart still builds, but no Average line",
      _one_fy_fig is not None and len(_avg_annotations(_one_fy_fig)) == 0)

_zero_fy_entry = {"years": ["TTM"], "values": [5.29]}
_zero_fy_fig = cui._cp_year_bar_chart("WMS", {"WMS": {"eps": _zero_fy_entry}}, "eps", "EPS by Year", "EPS", fmt="cur")
check("0 FY values (TTM only): chart still builds, but no Average line",
      _zero_fy_fig is not None and len(_avg_annotations(_zero_fy_fig)) == 0)


# ======================================================================
# CHECK 7: a negative-EPS year is included in the average as-is.
# ======================================================================
_neg_entry = {
    "years": ["TTM", "2026", "2025", "2024"],
    "values": [1.0, 2.0, -3.0, 4.0],
}
_neg_fig = cui._cp_year_bar_chart("WMS", {"WMS": {"eps": _neg_entry}}, "eps", "EPS by Year", "EPS", fmt="cur")
_neg_expected_avg = (2.0 + -3.0 + 4.0) / 3  # = 1.0
_neg_annotations = _avg_annotations(_neg_fig)
check("a negative-EPS fiscal year is included in the average, not dropped "
      "or floored at zero (expected average = 1.00)",
      _neg_fig is not None and len(_neg_annotations) == 1
      and "$1.00" in _neg_annotations[0])


# ======================================================================
# CHECK 8: hand-built workbook-shaped data (build_compounder_data.py's
# own {"years", "values"} dict, literal "TTM" first entry) still
# renders the chart, bars + Average line, unaffected.
# ======================================================================
_handbuilt_entry = {
    "years": ["TTM", "2025", "2024", "2023"],
    "values": [4.50, 4.20, 3.90, 3.60],
}
_handbuilt_fig = cui._cp_year_bar_chart(
    "WMS", {"WMS": {"eps": _handbuilt_entry}}, "eps", "EPS by Year", "EPS", fmt="cur",
)
_handbuilt_expected_avg = (4.20 + 3.90 + 3.60) / 3
check("hand-built-shaped data still renders the chart with no error",
      _handbuilt_fig is not None
      and list(_handbuilt_fig.data[0].x) == ["2023", "2024", "2025", "TTM"])
check("hand-built-shaped data: Average line reflects the FY-only average, "
      "TTM excluded, same as the real pipeline's own series shape",
      _handbuilt_fig is not None and len(_avg_annotations(_handbuilt_fig)) == 1
      and f"${_handbuilt_expected_avg:.2f}" in _avg_annotations(_handbuilt_fig)[0])


# ======================================================================
# CHECK 9: no other chart/caller touched - _cp_year_bar_chart() still
# has exactly one call site in the whole file (its own definition's
# "def" line plus the single "EPS by Year" caller).
# ======================================================================
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_grep = subprocess.run(
    ["grep", "-n", r"_cp_year_bar_chart(", "compounder_ui.py"],
    cwd=_repo_root, capture_output=True, text=True,
)
_hits = [l for l in _grep.stdout.splitlines() if l]
check("_cp_year_bar_chart() still has exactly one call site (plus its own "
      "def) - no other chart was wired into the new Average-line logic",
      len(_hits) == 2 and "def _cp_year_bar_chart(" in _hits[0]
      and '"eps", "EPS by Year"' in _hits[1])


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
