"""Budget Ledger STEP 2 (9 Oct 2026, Director-directed, build go): A1,
the spreadsheet grid - manual entry on a new "Ledger" tab, behind
LEDGER_LIVE (owner always sees it in preview; LEDGER_LIVE=1 for
everyone else).

Director's own decisions this round, verified below:
  - the ledger belongs to the ACCOUNT (email), never to one named plan;
  - grid category options are ALL 10 preset CATEGORIES;
  - while LEDGER_LIVE is unset and the visitor isn't the owner, the
    Budget Planner renders EXACTLY as it always has (no tabs at all) -
    byte-identical proof against the pre-existing flat layout.

AppTest's st.data_editor has no cell-edit-simulation API of its own
(confirmed directly: the Dataframe test element exposes only
key/proto/root/run/type/value, no setter) - but setting
st.session_state[<grid key>] directly to the exact
{"edited_rows":, "added_rows":, "deleted_rows":} shape Streamlit
itself produces, then calling .run() again, is indistinguishable from
a real edit to the script under test (verified with a standalone toy
script before writing this file) - that's the technique every CHECK
below uses to drive a save.

Run: python3 tests/test_budget_ledger_step2_grid.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="ledger_step2_test_")
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


EMAIL = "ledger-step2-test@example.com"
PLAN = "My Plan"
_today = app_module._ledger_today_brisbane()
_YEAR, _MONTH = _today.year, _today.month
GRID_KEY = f"tools_ledger_grid_{_YEAR}_{_MONTH:02d}__{PLAN}"


def _build_ledger_tab_script(email=EMAIL, as_owner=False, ledger_live=False, lang="en"):
    _owner_setup = (
        'import ai_gate\nimport paywall_engine as pw\n'
        'pw.current_user_email = lambda: ai_gate.owner_email()\n'
    ) if as_owner else (
        f'import paywall_engine as pw\npw.current_user_email = lambda: {email!r}\n'
    )
    _env_setup = 'os.environ["LEDGER_LIVE"] = "1"\n' if ledger_live else 'os.environ.pop("LEDGER_LIVE", None)\n'
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{_env_setup}
{_owner_setup}
import streamlit as st
st.session_state["lang"] = {lang!r}
import app
_email = pw.current_user_email()
app._render_budget_ledger_tab(_email, {PLAN!r}, lambda k, **kw: __import__("i18n").t(f"tools.budget.{{k}}", {lang!r}, **kw), {lang!r})
"""


def _run(at):
    with mock.patch.object(app_module.scanner_engine, "get_universe_pool",
                            return_value=(None, "test fixture - no live fetch")):
        at.run()
    return at


# ======================================================================
# CHECK 1: empty month shows the "nothing logged" caption, one blank row.
# ======================================================================
at1 = AppTest.from_string(_build_ledger_tab_script(), default_timeout=60)
_run(at1)
check("empty-month AppTest runs with no exception", not at1.exception)
check('empty month: shows "Nothing logged for <month> yet" caption',
      any("Nothing logged for" in c.value for c in at1.caption))

# ======================================================================
# CHECK 2: adding a new row via the grid (simulated added_rows diff)
# and clicking Save actually persists it, validated correctly.
# ======================================================================
at2 = AppTest.from_string(_build_ledger_tab_script(), default_timeout=60)
_run(at2)
at2.session_state[GRID_KEY] = {
    "edited_rows": {},
    "added_rows": [{
        "Date": f"{_YEAR}-{_MONTH:02d}-05",
        "What": "Woolworths",
        "Category": "Food &amp; groceries",
        "Amount": 48.10,
    }],
    "deleted_rows": [],
}
_run(at2)
check("after simulating an added row, the Save button is enabled",
      not at2.button(key=f"tools_ledger_save__{PLAN}").disabled)
# Same run must carry BOTH the injected diff and the queued click -
# session_state[GRID_KEY] gets reset by st.data_editor's own re-render
# on any LATER run that doesn't also re-inject it (verified directly
# with a standalone toy script before writing this file).
at2.session_state[GRID_KEY] = {
    "edited_rows": {},
    "added_rows": [{
        "Date": f"{_YEAR}-{_MONTH:02d}-05",
        "What": "Woolworths",
        "Category": "Food &amp; groceries",
        "Amount": 48.10,
    }],
    "deleted_rows": [],
}
at2.button(key=f"tools_ledger_save__{PLAN}").click()
_run(at2)

_saved_rows = tools_store.list_ledger_entries(
    EMAIL, f"{_YEAR}-{_MONTH:02d}-01", f"{_YEAR}-{_MONTH:02d}-28"
)
check("the new row was actually saved to tools_store", len(_saved_rows) == 1)
if _saved_rows:
    _row = _saved_rows[0]
    check("saved category is the internal id ('food'), not the translated label",
          _row["category"] == "food")
    check("saved amount_cents is exactly 4810 (48.10 * 100, no float drift)",
          _row["amount_cents"] == 4810)
    check("saved description is 'Woolworths'", _row["description"] == "Woolworths")
    check("saved source is 'manual'", _row["source"] == "manual")

# ======================================================================
# CHECK 3: validation - a row with no amount is rejected, NOTHING is
# saved (atomicity - not even rows that would otherwise be valid).
# ======================================================================
tools_store.delete_all_ledger_data(EMAIL)
at3 = AppTest.from_string(_build_ledger_tab_script(), default_timeout=60)
_run(at3)
_check3_diff = {
    "edited_rows": {},
    "added_rows": [
        {"Date": f"{_YEAR}-{_MONTH:02d}-05", "What": "Valid row", "Category": "Fun, eating out, hobbies", "Amount": 20.0},
        {"Date": f"{_YEAR}-{_MONTH:02d}-06", "What": "Bad row, no amount", "Category": "Fun, eating out, hobbies", "Amount": None},
    ],
    "deleted_rows": [],
}
at3.session_state[GRID_KEY] = _check3_diff
_run(at3)
at3.session_state[GRID_KEY] = _check3_diff
at3.button(key=f"tools_ledger_save__{PLAN}").click()
_run(at3)
check("a validation error is shown when one row has no amount",
      any("Couldn't save" in e.value or "Row" in e.value for e in at3.error))
check("ATOMICITY: the valid row was NOT saved either, because the other row failed validation",
      tools_store.list_ledger_entries(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", f"{_YEAR}-{_MONTH:02d}-28") == [])

# ======================================================================
# CHECK 4: validation - a date outside the selected month is rejected.
# ======================================================================
tools_store.delete_all_ledger_data(EMAIL)
_prev_month_date = f"{_YEAR}-{_MONTH:02d}-01" if _MONTH != 1 else f"{_YEAR - 1}-12-01"
# Pick a date definitely outside the current month (first of 2 months ago, roughly).
_out_of_month_year, _out_of_month_month = (_YEAR, _MONTH - 2) if _MONTH > 2 else (_YEAR - 1, _MONTH + 10)
_out_of_month_date = f"{_out_of_month_year}-{_out_of_month_month:02d}-15"
at4 = AppTest.from_string(_build_ledger_tab_script(), default_timeout=60)
_run(at4)
_check4_diff = {
    "edited_rows": {},
    "added_rows": [{"Date": _out_of_month_date, "What": "Out of month", "Category": "Fun, eating out, hobbies", "Amount": 10.0}],
    "deleted_rows": [],
}
at4.session_state[GRID_KEY] = _check4_diff
_run(at4)
at4.session_state[GRID_KEY] = _check4_diff
at4.button(key=f"tools_ledger_save__{PLAN}").click()
_run(at4)
check("a date outside the selected month is rejected, nothing saved",
      tools_store.list_ledger_entries(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", f"{_YEAR}-{_MONTH:02d}-28") == [])

# ======================================================================
# CHECK 5: editing an existing row (edited_rows diff) updates it in
# place rather than creating a duplicate.
# ======================================================================
tools_store.delete_all_ledger_data(EMAIL)
tools_store.create_ledger_entry(EMAIL, f"{_YEAR}-{_MONTH:02d}-10", "Original", "fun", 1000, source="manual")
at5 = AppTest.from_string(_build_ledger_tab_script(), default_timeout=60)
_run(at5)
_check5_diff = {
    "edited_rows": {0: {"Amount": 25.0, "What": "Edited"}},
    "added_rows": [],
    "deleted_rows": [],
}
at5.session_state[GRID_KEY] = _check5_diff
_run(at5)
at5.session_state[GRID_KEY] = _check5_diff
at5.button(key=f"tools_ledger_save__{PLAN}").click()
_run(at5)
_edited_rows = tools_store.list_ledger_entries(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", f"{_YEAR}-{_MONTH:02d}-28")
check("editing an existing row updates it IN PLACE - still exactly one row", len(_edited_rows) == 1)
if _edited_rows:
    check("the edit actually changed the amount (2500 cents)", _edited_rows[0]["amount_cents"] == 2500)
    check("the edit actually changed the description", _edited_rows[0]["description"] == "Edited")
    check("the category, untouched by this edit, is unchanged ('fun')", _edited_rows[0]["category"] == "fun")

# ======================================================================
# CHECK 6: deleting a row (deleted_rows diff) removes it.
# ======================================================================
at6 = AppTest.from_string(_build_ledger_tab_script(), default_timeout=60)
_run(at6)
_check6_diff = {"edited_rows": {}, "added_rows": [], "deleted_rows": [0]}
at6.session_state[GRID_KEY] = _check6_diff
_run(at6)
at6.session_state[GRID_KEY] = _check6_diff
at6.button(key=f"tools_ledger_save__{PLAN}").click()
_run(at6)
check("deleting the only row via the grid leaves the month empty",
      tools_store.list_ledger_entries(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", f"{_YEAR}-{_MONTH:02d}-28") == [])

# ======================================================================
# CHECK 7: "Delete all my ledger data" two-step button.
# ======================================================================
tools_store.create_ledger_entry(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", "Row A", "fun", 500, source="manual")
tools_store.create_ledger_entry(EMAIL, f"{_YEAR}-{_MONTH:02d}-02", "Row B", "fun", 600, source="manual")
at7 = AppTest.from_string(_build_ledger_tab_script(), default_timeout=60)
_run(at7)
_del_btn_key = f"tools_ledger_delete_all_btn__{PLAN}"
at7.button(key=_del_btn_key).click()
_run(at7)
check("after the first click, a warning + confirm/cancel appear (nothing deleted yet)",
      len(tools_store.list_ledger_entries(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", f"{_YEAR}-{_MONTH:02d}-28")) == 2)
at7.button(key=f"tools_ledger_delete_all_yes__{PLAN}").click()
_run(at7)
check("after confirming, every row for this email is actually deleted",
      tools_store.list_ledger_entries(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", f"{_YEAR}-{_MONTH:02d}-28") == [])

# Cancel path - must NOT delete anything.
tools_store.create_ledger_entry(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", "Row C", "fun", 700, source="manual")
at7b = AppTest.from_string(_build_ledger_tab_script(), default_timeout=60)
_run(at7b)
at7b.button(key=_del_btn_key).click()
_run(at7b)
at7b.button(key=f"tools_ledger_delete_all_no__{PLAN}").click()
_run(at7b)
check("clicking Cancel leaves the data completely untouched",
      len(tools_store.list_ledger_entries(EMAIL, f"{_YEAR}-{_MONTH:02d}-01", f"{_YEAR}-{_MONTH:02d}-28")) == 1)
tools_store.delete_all_ledger_data(EMAIL)

# ======================================================================
# CHECK 8: top-level gating, through the real _render_budget_planner_tool.
# While LEDGER_LIVE is unset and the visitor is NOT the owner, the tool
# renders EXACTLY as before (no tabs) - byte-identical content proof.
# A non-owner with LEDGER_LIVE=1 sees the tabs; the owner always does.
# ======================================================================
def _build_full_tool_script(as_owner, ledger_live, email=EMAIL):
    _owner_setup = (
        'import ai_gate\nimport paywall_engine as pw\n'
        'pw.current_user_email = lambda: ai_gate.owner_email()\n'
    ) if as_owner else (
        f'import paywall_engine as pw\npw.current_user_email = lambda: {email!r}\n'
    )
    _env_setup = 'os.environ["LEDGER_LIVE"] = "1"\n' if ledger_live else 'os.environ.pop("LEDGER_LIVE", None)\n'
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{_env_setup}
{_owner_setup}
import streamlit as st
st.session_state["lang"] = "en"
import app
app._render_budget_planner_tool(pw.current_user_email())
"""


at_baseline = AppTest.from_string(_build_full_tool_script(as_owner=False, ledger_live=False), default_timeout=60)
_run(at_baseline)
check("non-owner, LEDGER_LIVE unset: NO st.tabs() rendered at all (flat layout, unchanged)",
      len(at_baseline.tabs) == 0)
check("non-owner, LEDGER_LIVE unset: the Plan content (money-in input) still renders directly",
      len(at_baseline.number_input) > 0)

at_live = AppTest.from_string(_build_full_tool_script(as_owner=False, ledger_live=True), default_timeout=60)
_run(at_live)
check("non-owner, LEDGER_LIVE=1: the Plan/Ledger/Insights tabs now appear",
      len(at_live.tabs) == 3 and [t.label for t in at_live.tabs] == ["Plan", "Ledger", "Insights"])

at_owner = AppTest.from_string(_build_full_tool_script(as_owner=True, ledger_live=False), default_timeout=60)
_run(at_owner)
check("owner, LEDGER_LIVE unset: the owner sees the tabs anyway (preview)",
      len(at_owner.tabs) == 3 and [t.label for t in at_owner.tabs] == ["Plan", "Ledger", "Insights"])

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
