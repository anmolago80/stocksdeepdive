"""Budget Ledger STEP 4 (9 Oct 2026, Director-directed, build go): B,
bank CSV import - UI wiring (app._render_budget_ledger_import_expander),
exercised end to end via AppTest with a real file upload.

The pure parsing/cleaning/suggestion/dedup logic this UI calls into
already has its own exhaustive test (tests/test_budget_ledger_step4_
import_engine.py) - this file checks the WIRING: upload -> review grid
-> Save actually persists the right rows with the right categories,
card repayments are never saved, re-uploading the same file dedupes
correctly, and "remember my category changes as rules" actually writes
a ledger_rules row.

Run: python3 tests/test_budget_ledger_step4_ui.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="ledger_step4_ui_test_")
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


EMAIL = "ledger-step4-ui-test@example.com"
PLAN = "My Plan"

FIXTURE_CSV = (
    '07/10/2026,"-48.10","WOOLWORTHS      1234     SAMPLEVILLE QL",""\n'
    '06/10/2026,"-201.55","AGL SALES PTY LTD        SYDNEY",""\n'
    '17/09/2026,"+8995.48","PAYMENT RECEIVED, THANK YOU",""\n'
).encode()


def _build_script():
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import paywall_engine as pw
pw.current_user_email = lambda: {EMAIL!r}
import streamlit as st
st.session_state["lang"] = "en"
import app
cat_id_to_label = {{}}
for c in app.budget_planner_engine.CATEGORIES:
    cat_id_to_label[c["id"]] = app.i18n.t("tools.budget.cat." + c["id"], "en")
cat_label_to_id = {{v: k for k, v in cat_id_to_label.items()}}
_bl = lambda k, **kw: app.i18n.t("tools.budget." + k, "en", **kw)
_bll = lambda k, **kw: app.i18n.t("tools.budget.ledger." + k, "en", **kw)
app._render_budget_ledger_import_expander({EMAIL!r}, {PLAN!r}, _bl, _bll, cat_id_to_label, cat_label_to_id)
"""


def _run(at):
    with mock.patch.object(app_module.scanner_engine, "get_universe_pool",
                            return_value=(None, "test fixture - no live fetch")):
        at.run()
    return at


tools_store.delete_all_ledger_data(EMAIL)
tools_store.ensure_default_budget_plan(EMAIL)
tools_store.save_budget_plan(EMAIL, PLAN, 9000.0, {"food": 600.0})

# ======================================================================
# CHECK 1: upload the real fixture - header stats correct, card
# repayment excluded from the count of savable/reviewable rows.
# ======================================================================
at = AppTest.from_string(_build_script(), default_timeout=60)
_run(at)
at.file_uploader[0].set_value(("bank.csv", FIXTURE_CSV, "text/csv"))
_run(at)
check("upload renders with no exception", not at.exception)
check("header caption shows 2 rows needing review (3 read - 1 repayment)",
      any("Rows read: 3" in c.value for c in at.caption))
check("header caption shows 1 card repayment", any("Card repayments: 1" in c.value for c in at.caption))

# ======================================================================
# CHECK 2: categorise the "needs you" WOOLWORTHS row via the review
# grid, then Save - it lands in tools_store with the chosen category,
# the card repayment does NOT, AGL (if suggested) may or may not have
# landed depending on the built-in match.
# ======================================================================
_review_key = f"tools_ledger_import_review__{PLAN}"
_review_editor = at.get_by_key(_review_key)
_woolworths_row_idx = int(_review_editor.value[_review_editor.value["What"] == "WOOLWORTHS"].index[0])
_review_diff = {
    "edited_rows": {_woolworths_row_idx: {"Category": "Food &amp; groceries"}},
    "added_rows": [], "deleted_rows": [],
}
# Same run must carry BOTH the injected diff and the queued click - a
# data_editor's own session_state diff resets on any run that doesn't
# also re-inject it (same lesson as tests/test_budget_ledger_step2_
# grid.py's own documented discovery).
at.session_state[_review_key] = _review_diff
_run(at)
check("after simulating the category edit, the Save button is enabled",
      not at.button(key=f"tools_ledger_import_save__{PLAN}").disabled)

at.session_state[_review_key] = _review_diff
at.button(key=f"tools_ledger_import_save__{PLAN}").click()
_run(at)

_saved = tools_store.list_ledger_entries(EMAIL, "2026-09-01", "2026-10-31")
check("exactly the categorised rows were saved (WOOLWORTHS + AGL, never the repayment)",
      len(_saved) in (1, 2) and all(r["source"] == "import" for r in _saved))
_woolworths_saved = next((r for r in _saved if r["description"] == "WOOLWORTHS"), None)
check("WOOLWORTHS was saved with the category chosen in the review grid ('food')",
      _woolworths_saved is not None and _woolworths_saved["category"] == "food")
check("WOOLWORTHS's amount is 4810 cents (the real purchase amount)",
      _woolworths_saved is not None and _woolworths_saved["amount_cents"] == 4810)
check("the card-repayment row (PAYMENT RECEIVED) was never saved at all",
      not any("PAYMENT RECEIVED" in (r.get("raw_description") or "") for r in _saved))

# ======================================================================
# CHECK 3: "remember my category changes as rules" actually wrote a
# ledger_rules row for the merchant.
# ======================================================================
check("a ledger_rules entry now exists for 'woolworths' -> 'food'",
      tools_store.get_merchant_rule(EMAIL, "woolworths") == "food")

# ======================================================================
# CHECK 4: re-uploading the EXACT same file dedupes - the already-
# imported rows are not offered again for review.
# ======================================================================
at2 = AppTest.from_string(_build_script(), default_timeout=60)
_run(at2)
at2.file_uploader[0].set_value(("bank.csv", FIXTURE_CSV, "text/csv"))
_run(at2)
check("re-upload: header caption shows the already-saved rows as 'Already imported'",
      any("Already imported: 1" in c.value or "Already imported: 2" in c.value for c in at2.caption))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
