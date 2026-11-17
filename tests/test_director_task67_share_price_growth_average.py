"""
TASK 67 Part A (Director-directed, owner-approved, 17 Nov 2026) - Share
Price Growth by Year uses average-vs-average for EVERY year, including
the current/latest one.

CONTEXT: auto_compounder_engine._share_price_growth_entry() used to
special-case the latest in-progress year (fewer than 12 monthly
points), using a plain start-of-year-close vs latest-close YTD return
instead of the normal average-vs-average formula every completed year
uses - added for CPU.AX in Aug 2026, whose 2026 bar read -7.7%
(average-vs-average, dragged down by a Jan-Mar trough) while the
actual price was +17-22% since the 2025 close by August.

OWNER DECISION (17 Nov 2026): remove that exception. Every year,
including the current one, uses average-vs-average for consistency.
The current year's own label gets " (to date)" appended instead (e.g.
"2026 (to date)") so the chart still signals it's a partial year - the
only thing that changes between a completed and in-progress year now
is the LABEL, never the formula.

No pre-existing test covered this function at all (confirmed via grep
for "ytd_year"/"_share_price_growth_entry"/"CPU.AX" across tests/ -
zero hits before this file), so this is a new test, not an update to
one - it covers both the new behaviour and a CPU.AX-shaped regression
check (the exact scenario that justified the now-removed exception).

Covers:
  - Completed years: formula unchanged (avg this year vs avg prior
    year).
  - Current/latest partial year: avg(available points this year) vs
    avg(full prior year) - same formula as a completed year, not a
    separate YTD calculation.
  - The current year's label carries " (to date)"; every completed
    year's label is the plain "YYYY" string.
  - _cp_share_price_growth_chart()'s own dashed Average line includes
    the current year's value (no filtering/exclusion).
  - The CPU.AX-shaped scenario (an early-year trough that would have
    produced a very different YTD-vs-average-vs-average result) now
    gets the SAME average-vs-average treatment as every other year -
    no more special case.
  - "ytd_year" is gone from the codebase entirely (no stray leftover
    reference to the removed field) and the stale "this bar is a plain
    start-of-year vs latest-price return" caption in compounder_ui.py
    is removed.
  - Hand-built workbook data (build_compounder_data.py's own
    {"years", "values"} dicts, no "ytd_year"/partial-year concept at
    all) renders completely unaffected.

Run: python3 tests/test_director_task67_share_price_growth_average.py
"""
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


import auto_compounder_engine as ace
import compounder_ui as cui


def _monthly_fixture(year_prices):
    """year_prices: {year_str: [monthly closes...]} (already in calendar
    order within the year, oldest year first) -> a prices_10y-shaped
    {"dates", "prices"} dict, one point per month, 1st of the month."""
    dates, prices = [], []
    for year in sorted(year_prices):
        for i, p in enumerate(year_prices[year], start=1):
            dates.append(f"{year}-{i:02d}-01")
            prices.append(p)
    return {"dates": dates, "prices": prices}


# ======================================================================
# CHECK 1-4: a WMS-shaped fixture - two completed years plus a current,
# partial (3-month) year.
# ======================================================================
_wms_fixture = _monthly_fixture({
    "2024": [100.0] * 12,
    "2025": [120.0] * 12,
    "2026": [90.0, 95.0, 140.0],  # 3 months so far, avg = 108.333...
})
_wms_entry = ace._share_price_growth_entry(_wms_fixture)
check("WMS fixture: entry built (>=24 points)", _wms_entry is not None)

_avg_2025 = 120.0
_avg_2026_partial = (90.0 + 95.0 + 140.0) / 3
_expected_2026 = (_avg_2026_partial - _avg_2025) / _avg_2025
check("current-year bar = avg(current-year points)/avg(prior year) - 1, "
      "NOT a start-of-year-vs-latest-close YTD return",
      _wms_entry is not None and abs(_wms_entry["values"][0] - _expected_2026) < 1e-9)

_avg_2024 = 100.0
_expected_2025 = (_avg_2025 - _avg_2024) / _avg_2024
check("the completed year (2025) is unchanged - still avg-vs-avg",
      _wms_entry is not None and abs(_wms_entry["values"][1] - _expected_2025) < 1e-9)

check("the current year's label carries ' (to date)'",
      _wms_entry is not None and _wms_entry["years"][0] == "2026 (to date)")
check("the completed year's label is the plain 'YYYY' string (no suffix)",
      _wms_entry is not None and _wms_entry["years"][1] == "2025")


# ======================================================================
# CHECK 5-6: the dashed Average line (via _cp_share_price_growth_chart)
# includes the current/partial year's value - never excluded.
# ======================================================================
_fig = cui._cp_share_price_growth_chart("WMS", {"WMS": _wms_entry})
check("the chart builds successfully for a fixture with a partial current year",
      _fig is not None)
_bar_values = list(_fig.data[0].y)
_expected_avg = sum(_bar_values) / len(_bar_values)
_hline_annotations = [a.text for a in _fig.layout.annotations if "Average" in (a.text or "")]
check("the dashed Average line's own annotation reflects an average that "
      "INCLUDES the current (partial) year's value - not just the completed years",
      len(_hline_annotations) == 1
      and f"{_expected_avg * 100:+.1f}%" in _hline_annotations[0]
      and abs(_expected_avg - (sum(_wms_entry['values']) / len(_wms_entry['values']))) < 1e-9)
check("the x-axis category list passed to the bar trace carries the "
      "'(to date)' label for the current year, in chronological order",
      list(_fig.data[0].x) == ["2025", "2026 (to date)"])


# ======================================================================
# CHECK 7-9: CPU.AX-shaped regression - the exact scenario that
# justified the now-removed exception (an early-year trough, since-
# recovered) now gets the SAME average-vs-average formula as every
# other year, no special-cased YTD number.
# ======================================================================
_cpu_fixture = _monthly_fixture({
    "2024": [50.0] * 12,
    "2025": [60.0] * 12,
    # Jan-Mar trough (40/38/42), already recovered to 75 by the time
    # this fixture's "latest" point is read - a start-of-year-close
    # (40) vs latest-close (75) YTD return would read +87.5%, while
    # avg-vs-avg (avg(40,38,42,75)=48.75 vs 60) reads a DIFFERENT
    # number entirely - proving the two formulas really do diverge,
    # and that this fixture now uses the avg-vs-avg one.
    "2026": [40.0, 38.0, 42.0, 75.0],
})
_cpu_entry = ace._share_price_growth_entry(_cpu_fixture)
_cpu_avg_2026 = (40.0 + 38.0 + 42.0 + 75.0) / 4
_cpu_expected = (_cpu_avg_2026 - 60.0) / 60.0
_cpu_ytd_would_have_been = (75.0 - 40.0) / 40.0
check("CPU.AX-shaped scenario: the two formulas genuinely diverge in this "
      "fixture (sanity check on the fixture itself)",
      abs(_cpu_expected - _cpu_ytd_would_have_been) > 0.1)
check("CPU.AX-shaped scenario: the current year's bar is the avg-vs-avg "
      "number, NOT the old YTD number the removed exception would have used",
      abs(_cpu_entry["values"][0] - _cpu_expected) < 1e-9
      and abs(_cpu_entry["values"][0] - _cpu_ytd_would_have_been) > 0.1)
check("CPU.AX-shaped scenario: the current year's label is still '2026 (to date)'",
      _cpu_entry["years"][0] == "2026 (to date)")


# ======================================================================
# CHECK 10-11: completed-years-only fixture (no partial year at all) -
# byte-identical to before this task, no " (to date)" anywhere.
# ======================================================================
_closed_fixture = _monthly_fixture({
    "2023": [100.0] * 12, "2024": [110.0] * 12, "2025": [121.0] * 12,
})
_closed_entry = ace._share_price_growth_entry(_closed_fixture)
check("an all-completed-years fixture produces no ' (to date)' label anywhere",
      _closed_entry is not None and all("(to date)" not in y for y in _closed_entry["years"]))
check("an all-completed-years fixture: both bars are the expected avg-vs-avg values",
      _closed_entry is not None
      and abs(_closed_entry["values"][0] - 0.1) < 1e-9  # 2025 vs 2024: 121/110-1
      and abs(_closed_entry["values"][1] - 0.1) < 1e-9)  # 2024 vs 2023: 110/100-1


# ======================================================================
# CHECK 12-13: "ytd_year" is gone from the codebase entirely, and the
# stale caption explaining a now-nonexistent calculation difference is
# removed from compounder_ui.py.
# ======================================================================
import subprocess
_this_file = os.path.abspath(__file__)
_grep = subprocess.run(
    ["grep", "-rln", "ytd_year", "--include=*.py", "."],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    capture_output=True, text=True,
)
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_files_with_refs = {
    os.path.abspath(os.path.join(_repo_root, line.lstrip("./")))
    for line in _grep.stdout.splitlines() if line
}
_files_with_refs.discard(_this_file)
check("no CODE file other than this test's own prose references the removed "
      "'ytd_year' field anywhere else in the codebase",
      _files_with_refs == {os.path.abspath(os.path.join(_repo_root, "auto_compounder_engine.py"))})
check("the stale 'is still in progress, so that bar is a plain start-of-year "
      "vs latest-price return' caption is gone from compounder_ui.py",
      "is still in progress, so that bar is a plain" not in open(
          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "compounder_ui.py")).read())


# ======================================================================
# CHECK 14: hand-built workbook data (build_compounder_data.py's own
# {"years", "values"} shape, no partial-year concept at all) still
# renders completely unaffected - _cp_share_price_growth_chart() never
# assumes a "(to date)" label is present.
# ======================================================================
_handbuilt_entry = {"years": ["2025", "2024", "2023"], "values": [0.12, -0.05, 0.08]}
_handbuilt_fig = cui._cp_share_price_growth_chart("WMS", {"WMS": _handbuilt_entry})
check("hand-built-shaped data (no partial-year label ever present) still "
      "renders the chart with no error",
      _handbuilt_fig is not None and list(_handbuilt_fig.data[0].x) == ["2023", "2024", "2025"])


# ======================================================================
# CHECK 15: fewer than 24 points still returns None (unchanged guard).
# ======================================================================
check("fewer than 24 points still returns None (the pre-existing guard, untouched)",
      ace._share_price_growth_entry({"dates": ["2025-01-01"], "prices": [100.0]}) is None)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
