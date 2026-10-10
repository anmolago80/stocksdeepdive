"""Budget Ledger STEP 3 (9 Oct 2026, Director-directed, build go): the
Insights tab - monthly spent for the last 6 months as bars, the plan
as a dashed line. Director's own decision: no month-by-month plan
history exists, so every month compares against the CURRENT plan's
total - verified by checking every month's bar is tested against the
SAME plan figure, and the caption says so.

Run: python3 tests/test_budget_ledger_step3_insights.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="ledger_insights_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

from streamlit.testing.v1 import AppTest
import tools_store
import app as app_module

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


EMAIL = "ledger-insights-test@example.com"
PLAN = "My Plan"
_today = app_module._ledger_today_brisbane()


def _build_script(lang="en"):
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import paywall_engine as pw
pw.current_user_email = lambda: {EMAIL!r}
import streamlit as st
st.session_state["lang"] = {lang!r}
import app
app._render_budget_insights_tab({EMAIL!r}, {PLAN!r}, lambda k, **kw: __import__("i18n").t(f"tools.budget.{{k}}", {lang!r}, **kw), {lang!r})
"""


def _run(at):
    with mock.patch.object(app_module.scanner_engine, "get_universe_pool",
                            return_value=(None, "test fixture - no live fetch")):
        at.run()
    return at


def _find_plotly_specs(node):
    """Every st.plotly_chart's own figure spec (as a parsed dict),
    walking the whole element tree - AppTest has no dedicated
    .plotly_chart accessor, but each node's raw proto carries the
    figure JSON directly in its own `spec` field (confirmed directly
    against a standalone toy script before writing this file)."""
    specs = []
    if type(node).__name__ == "UnknownElement" and getattr(node, "type", None) == "plotly_chart":
        specs.append(json.loads(node.proto.spec))
    children = getattr(node, "children", None)
    if children:
        for c in children.values():
            specs.extend(_find_plotly_specs(c))
    return specs


tools_store.delete_all_ledger_data(EMAIL)
tools_store.ensure_default_budget_plan(EMAIL)
tools_store.save_budget_plan(EMAIL, PLAN, 9000.0, {"food": 600.0})

# Current month: over plan. One month ago: under plan. Two months ago: no data.
_this_total = _today.year * 12 + (_today.month - 1)
_y0, _m0 = divmod(_this_total, 12)
_y1, _m1 = divmod(_this_total - 1, 12)
_y2, _m2 = divmod(_this_total - 2, 12)
tools_store.create_ledger_entry(EMAIL, f"{_y0}-{_m0 + 1:02d}-05", "Over", "food", 70000, source="manual")
tools_store.create_ledger_entry(EMAIL, f"{_y1}-{_m1 + 1:02d}-05", "Under", "food", 10000, source="manual")
# _y2/_m2: deliberately no entries at all.

at = AppTest.from_string(_build_script(), default_timeout=60)
_run(at)
check("Insights tab renders with no exception", not at.exception)
check("Insights caption says 'Compared with your current plan.'",
      any("Compared with your current plan" in c.value for c in at.caption))

_specs = _find_plotly_specs(at.main)
check("exactly one plotly chart renders on the Insights tab", len(_specs) == 1)
_spec = _specs[0] if _specs else {}
_bar = next((d for d in _spec.get("data", []) if d.get("type") == "bar"), None)
check("the bar trace has exactly 6 months", _bar is not None and len(_bar["x"]) == 6)

if _bar:
    # Months, oldest first: [2 months ago, 1 month ago, current, ...wait -
    # the function builds -5..0 so index 3 = 2 months ago, 4 = 1 month ago,
    # 5 = current month (the last one).
    check("the current (last) month's label says '(so far)'",
          "(so far)" in _bar["x"][-1])
    check("2-months-ago (no entries at all) shows 'no data', not $0",
          _bar["text"][3] == "no data")
    check("2-months-ago's bar color is the neutral grey, not teal/red",
          _bar["marker"]["color"][3] == "#5b7290")
    check("1-month-ago (under plan, $100 < $600 plan) is teal, not red",
          _bar["marker"]["color"][4] == "#2dd4bf")
    check("current month (over plan, $700 > $600 plan) is red", _bar["marker"]["color"][5] == "#fb7185")
    check("current month's hover text mentions 'over plan'",
          "over plan" in _bar["text"][5])
    check("1-month-ago's value is exactly 100.0 (the $100.00 entry)",
          _bar["y"][4] == 100.0)
    check("current month's value is exactly 700.0 (the $700.00 entry)",
          _bar["y"][5] == 700.0)
    check("2-months-ago's bar VALUE is 0 even though the text says 'no data' "
          "(no fabricated nonzero height)", _bar["y"][3] == 0.0)

_plan_line = next((d for d in _spec.get("data", []) if d.get("type") == "scatter"), None)
check("a plan dashed line trace exists", _plan_line is not None)
if _plan_line:
    check("the plan line is the SAME value (600) for every one of the 6 months - "
          "the current plan, never a per-month history",
          _plan_line["y"] == [600.0] * 6)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
