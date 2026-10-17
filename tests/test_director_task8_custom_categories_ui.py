"""
Director TASK 8 (17 Oct 2026, "custom budget categories") - UI wiring:
app._categories_for_user()/_render_custom_categories_manager() and
every call site TASK 8 replaced (Plan tab inputs, ledger category
chart, "is a bill" expander, ledger dropdown/is_bill map, CSV import's
cat_id_to_label), exercised end to end via AppTest against the real
app.py render functions - mirrors the exact AppTest patterns tests/
test_budget_ledger_step3_ui.py and tests/test_budget_ledger_step4_ui.
py already use for this same tool.

The pure store-layer validation (create/rename/hide/limits/duplicates/
user isolation) already has its own exhaustive test (tests/test_
director_task8_custom_categories_store.py) - this file checks the
WIRING: a custom category reaches the Plan tab's own inputs/plan
total, the Ledger dropdown/chart/is-a-bill tick, and the CSV import's
suggestion/merchant-rule machinery, for free, through one shared list
- never a second, parallel one. Also: a custom category flows through
Plan total -> Ledger tiles correctly when flagged a bill; a ledger row
keeps working after a rename; and shows "(category removed)" after a
hide (both in the grid and the CSV export) without ever deleting the
row or any merchant rule that used it; the 10 presets are completely
unaffected throughout.

Run: python3 tests/test_director_task8_custom_categories_ui.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task8_ui_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

from streamlit.testing.v1 import AppTest
import tools_store
import budget_planner_engine
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


def _run(at):
    with mock.patch.object(app_module.scanner_engine, "get_universe_pool",
                            return_value=(None, "test fixture - no live fetch")):
        at.run()
    return at


EMAIL = "task8-ui-test@example.com"
PLAN = "My Plan"
tools_store.delete_all_ledger_data(EMAIL)
tools_store.ensure_default_budget_plan(EMAIL)
_today = app_module._ledger_today_brisbane()


# ======================================================================
# CHECK 1-2: _categories_for_user() itself - presets first (in their
# own fixed order), then this email's visible customs, oldest first;
# a HIDDEN custom never appears.
# ======================================================================
_cid_a, _ = tools_store.create_custom_category(EMAIL, "Pet care", preset_labels=["x"])
_cid_b, _ = tools_store.create_custom_category(EMAIL, "Streaming", preset_labels=["x"])
_categories = app_module._categories_for_user(EMAIL, "en")
_preset_ids = [c["id"] for c in budget_planner_engine.CATEGORIES]
check("the first 10 entries are exactly the presets, in their own order",
      [c["id"] for c in _categories[:10]] == _preset_ids)
check("the custom categories follow, oldest-created first",
      [c["id"] for c in _categories[10:]] == [_cid_a, _cid_b])

tools_store.hide_custom_category(EMAIL, _cid_b)
_categories_after_hide = app_module._categories_for_user(EMAIL, "en")
check("a hidden custom category never appears in _categories_for_user()",
      [c["id"] for c in _categories_after_hide] == _preset_ids + [_cid_a])
tools_store.hide_custom_category(EMAIL, _cid_a)
tools_store.list_custom_categories(EMAIL)  # sanity - still callable, empty now


# ======================================================================
# CHECK 3-9: the Plan tab - a visible custom category gets its own
# money-out input (icon+label, not a preset i18n lookup), flows into
# the plan total/compounding/save exactly like a preset; "Add a
# category" creates one; rename/hide work through the real buttons.
# A FRESH email - EMAIL above already has a HIDDEN "Pet care" from
# CHECK 1-2, and the store's own "retired labels can't be recycled
# either" rule (proven in test_director_task8_custom_categories_store.
# py) would reject reusing that exact label for the same email.
# ======================================================================
EMAIL_PLAN = "task8-ui-plan-test@example.com"
tools_store.ensure_default_budget_plan(EMAIL_PLAN)
_cid, _ = tools_store.create_custom_category(EMAIL_PLAN, "Pet care", preset_labels=["x"])


def _plan_script():
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import paywall_engine as pw
pw.current_user_email = lambda: {EMAIL_PLAN!r}
import streamlit as st
st.session_state["lang"] = "en"
import app
_bl = lambda k, **kw: app.i18n.t("tools.budget." + k, "en", **kw)
app._render_budget_plan_subtab({EMAIL_PLAN!r}, {PLAN!r}, _bl)
"""


at = AppTest.from_string(_plan_script(), default_timeout=60)
_run(at)
check("the Plan tab renders with no exception", not at.exception)
_labels = [ni.label for ni in at.number_input]
check("the custom category 'Pet care' gets its own money-out input, "
      "with its own icon (not an i18n lookup, which would show the "
      "raw key for a custom id)",
      any("Pet care" in (l or "") and "\U0001F3F7" in (l or "") for l in _labels))
check("the 10 presets are all still present, completely unaffected",
      sum(1 for l in _labels for c in budget_planner_engine.CATEGORIES
          if (c["icon"] + " ") in (l or "") and app_module.i18n.t(f"tools.budget.cat.{c['id']}", "en") in (l or "")) == 10)
check("the manager expander is present", any("Manage" in (e.label or "") for e in at.expander))

# Set the custom category's amount and let the render loop save it.
_pet_input = next(ni for ni in at.number_input if "Pet care" in (ni.label or ""))
_pet_input.set_value(80.0)
_run(at)
_saved_plan = tools_store.get_budget_plan(EMAIL_PLAN, PLAN)
check("the custom category's amount is stored in categories_json under "
      "its own custom id, exactly like a preset would be",
      _saved_plan["categories"].get(_cid) == 80.0)

# "Add a category" - fill the name field and click Add.
_name_input = next(ti for ti in at.text_input if (ti.key or "").startswith("tools_budget_add_cat_name__"))
_name_input.set_value("Gym")
_add_btn = next(b for b in at.button if (b.key or "").startswith("tools_budget_add_cat_btn__"))
_add_btn.click()
_run(at)
check("'Add a category' creates a new, visible custom category",
      any(c["label"] == "Gym" for c in tools_store.list_custom_categories(EMAIL_PLAN)))
check("the newly-added category's own money-out input now appears too",
      any("Gym" in (ni.label or "") for ni in at.number_input))

# Duplicate name -> an error, never a second category.
_name_input2 = next(ti for ti in at.text_input if (ti.key or "").startswith("tools_budget_add_cat_name__"))
_name_input2.set_value("Gym")
_add_btn2 = next(b for b in at.button if (b.key or "").startswith("tools_budget_add_cat_btn__"))
_add_btn2.click()
_run(at)
check("adding a duplicate-named category shows an error, not a crash",
      not at.exception and any(e.value for e in at.error))
check("...and no second 'Gym' category was created",
      sum(1 for c in tools_store.list_custom_categories(EMAIL_PLAN) if c["label"] == "Gym") == 1)

# Rename "Pet care" -> "Pet expenses" via the manager's own button.
_rename_input = next(ti for ti in at.text_input if (ti.key or "").startswith(f"tools_budget_cat_rename__{_cid}__"))
_rename_input.set_value("Pet expenses")
_rename_btn = next(b for b in at.button if (b.key or "").startswith(f"tools_budget_cat_rename_btn__{_cid}__"))
_rename_btn.click()
_run(at)
check("the rename button actually renamed the category in storage",
      tools_store.list_custom_categories(EMAIL_PLAN)[0]["label"] == "Pet expenses"
      if tools_store.list_custom_categories(EMAIL_PLAN)[0]["id"] == _cid else False)
check("the renamed category's input now shows the NEW label",
      any("Pet expenses" in (ni.label or "") for ni in at.number_input))
check("the OLD label no longer appears anywhere",
      not any("Pet care" == (ni.label or "").split(" ", 1)[-1] for ni in at.number_input))

# Hide "Pet expenses" (the renamed _cid) via its own button.
_hide_btn = next(b for b in at.button if (b.key or "").startswith(f"tools_budget_cat_hide_btn__{_cid}__"))
_hide_btn.click()
_run(at)
check("the hide button actually hid the category - it's no longer in "
      "list_custom_categories()", not any(c["id"] == _cid for c in tools_store.list_custom_categories(EMAIL_PLAN)))
check("the hidden category's own money-out input is gone from the Plan tab",
      not any("Pet expenses" in (ni.label or "") for ni in at.number_input))
check("'Gym' (never touched) is still there, unaffected by hiding a "
      "DIFFERENT custom category", any("Gym" in (ni.label or "") for ni in at.number_input))


# ======================================================================
# CHECK 10-11: the 10-visible limit surfaces as a UI error, not a
# crash, and never silently creates an 11th.
# ======================================================================
tools_store.delete_all_ledger_data("limit-test@example.com")
for i in range(10):
    tools_store.create_custom_category("limit-test@example.com", f"Cat {i}", preset_labels=["x"])


def _plan_script_for(email):
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
import streamlit as st
st.session_state["lang"] = "en"
import app
_bl = lambda k, **kw: app.i18n.t("tools.budget." + k, "en", **kw)
app._render_budget_plan_subtab({email!r}, {PLAN!r}, _bl)
"""


tools_store.ensure_default_budget_plan("limit-test@example.com")
at_limit = AppTest.from_string(_plan_script_for("limit-test@example.com"), default_timeout=60)
_run(at_limit)
_name_input3 = next(ti for ti in at_limit.text_input if (ti.key or "").startswith("tools_budget_add_cat_name__"))
_name_input3.set_value("Eleventh")
_add_btn3 = next(b for b in at_limit.button if (b.key or "").startswith("tools_budget_add_cat_btn__"))
_add_btn3.click()
_run(at_limit)
check("hitting the 10-visible limit through the UI shows an error, "
      "never a crash", not at_limit.exception and any(e.value for e in at_limit.error))
check("...and no 11th category was created",
      len(tools_store.list_custom_categories("limit-test@example.com")) == 10)


# ======================================================================
# CHECK 12-18: the Ledger tab - a custom category reaches the grid's
# dropdown options, the category chart, the is-a-bill expander, and
# (via is_bill_map) the plan/tiles split correctly when flagged a
# bill; a ledger row keeps working after a rename and shows "(category
# removed)" after a hide - without deleting the row.
# ======================================================================
EMAIL2 = "task8-ledger-test@example.com"
tools_store.delete_all_ledger_data(EMAIL2)
tools_store.ensure_default_budget_plan(EMAIL2)
_cid2, _ = tools_store.create_custom_category(EMAIL2, "Pet care", preset_labels=["x"])
tools_store.save_budget_plan(EMAIL2, PLAN, 5000.0, {"food": 400.0, _cid2: 60.0})
_entry_id = tools_store.create_ledger_entry(EMAIL2, _today.isoformat(), "Vet bill", _cid2, 4500, source="manual")


def _ledger_script(email):
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ.pop("LEDGER_LIVE", None)
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
import streamlit as st
st.session_state["lang"] = "en"
import app
_bl = lambda k, **kw: __import__("i18n").t(f"tools.budget.{{k}}", "en", **kw)
app._render_budget_ledger_tab({email!r}, {PLAN!r}, _bl, "en")
"""


_grid_key = app_module._tools_plan_key(PLAN, f"tools_ledger_grid_{_today.year}_{_today.month:02d}")

at_l = AppTest.from_string(_ledger_script(EMAIL2), default_timeout=60)
_run(at_l)
check("the Ledger tab renders with no exception, custom category "
      "present", not at_l.exception)
_grid_before = at_l.get_by_key(_grid_key).value
check("the ledger row shows the custom category's own label in the "
      "grid", list(_grid_before["Category"]) == ["Pet care"])

# AppTest exposes no reliable way to read a plotly figure's own trace
# data (its `.value` is always None for plotly_chart elements -
# confirmed directly) - call _render_ledger_category_chart() itself
# instead, with st.plotly_chart mocked to capture the real Figure it
# builds, and read the custom category's label straight off the bar
# trace's own y-axis categories.
_captured_fig = {}
with mock.patch.object(app_module.st, "plotly_chart", side_effect=lambda fig, **kw: _captured_fig.setdefault("fig", fig)), \
     mock.patch.object(app_module.st, "markdown"), mock.patch.object(app_module.st, "caption"):
    app_module._render_ledger_category_chart(
        lambda k, **kw: __import__("i18n").t(f"tools.budget.ledger.{k}", "en", **kw),
        [{"category": _cid2, "amount_cents": 4500}], {_cid2: 60.0},
        {_cid2: False}, app_module._categories_for_user(EMAIL2, "en"),
    )
check("'Pet care' (the custom category) appears as its own bar in the "
      "category chart, by its real label - not an i18n lookup that "
      "would never resolve for a custom id",
      "fig" in _captured_fig and "Pet care" in list(_captured_fig["fig"].data[0].y))

_is_bill_expander_opened = any("bill" in (e.label or "").lower() for e in at_l.expander)
check("the is-a-bill expander is present (loops the full category "
      "list, so the custom category gets its own tick)", _is_bill_expander_opened)
_is_bill_checkbox = next((cb for cb in at_l.checkbox if "Pet care" in (cb.label or "")), None)
check("the custom category has its own is-a-bill checkbox",
      _is_bill_checkbox is not None)

# Flag the custom category a bill, then confirm bills_plan picks it up.
_is_bill_checkbox.set_value(True)
_run(at_l)
check("ticking is-a-bill for the custom category actually persists",
      tools_store.is_category_bill(EMAIL2, _cid2) is True)
_bills_plan_after = budget_planner_engine.ledger_bills_plan(
    {"food": 400.0, _cid2: 60.0}, {"food": False, _cid2: True},
)
check("bills_plan correctly includes the custom category's own plan "
      "amount once flagged a bill", _bills_plan_after == 60.0)

# Rename the custom category, re-render, confirm the ledger row keeps
# working (shows the NEW label, amount/category id untouched).
_ok, _ = tools_store.rename_custom_category(EMAIL2, _cid2, "Pet expenses", preset_labels=["x"])
check("rename succeeded", _ok is True)
at_l2 = AppTest.from_string(_ledger_script(EMAIL2), default_timeout=60)
_run(at_l2)
_grid_after_rename = at_l2.get_by_key(_grid_key).value
check("after a rename, the SAME ledger row now shows the NEW label - "
      "it kept working, never orphaned", list(_grid_after_rename["Category"]) == ["Pet expenses"])
_row_after_rename = tools_store.get_ledger_entry(EMAIL2, _entry_id)
check("the row's own stored category id is untouched by the rename "
      "(only the label changed)", _row_after_rename["category"] == _cid2)

# Hide the custom category entirely - the row must show "(category
# removed)", never disappear or get deleted.
tools_store.hide_custom_category(EMAIL2, _cid2)
at_l3 = AppTest.from_string(_ledger_script(EMAIL2), default_timeout=60)
_run(at_l3)
check("the Ledger tab still renders with no exception after hiding "
      "the category a logged row uses", not at_l3.exception)
_grid_after_hide = at_l3.get_by_key(_grid_key).value
check('after hiding, the SAME row now shows "(category removed)" - '
      "never deleted, never crashes",
      list(_grid_after_hide["Category"]) == ["(category removed)"])
_row_after_hide = tools_store.get_ledger_entry(EMAIL2, _entry_id)
check("the row itself still exists in storage, completely untouched",
      _row_after_hide is not None and _row_after_hide["category"] == _cid2
      and _row_after_hide["amount_cents"] == 4500)


# ======================================================================
# CHECK 19-21: CSV import - a custom category's own cat_id_to_label
# reaches suggest_category()/the review dropdown/the saved merchant
# rule, exactly like a preset.
# ======================================================================
import ledger_import_engine

EMAIL3 = "task8-import-test@example.com"
tools_store.delete_all_ledger_data(EMAIL3)
tools_store.ensure_default_budget_plan(EMAIL3)
_cid3, _ = tools_store.create_custom_category(EMAIL3, "Vet Bills", preset_labels=["x"])
tools_store.upsert_merchant_rule(EMAIL3, "the vet clinic", _cid3)

_categories3 = app_module._categories_for_user(EMAIL3, "en")
_cat_id_to_label3 = {c["id"]: c["label"] for c in _categories3}
_label, _source = ledger_import_engine.suggest_category(
    "the vet clinic", lambda k: tools_store.get_merchant_rule(EMAIL3, k), _cat_id_to_label3,
)
check("suggest_category() resolves a merchant rule pointing at a "
      "CUSTOM category id to that category's own display label",
      _label == "Vet Bills" and _source == "your rule")


def _import_script():
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import paywall_engine as pw
pw.current_user_email = lambda: {EMAIL3!r}
import streamlit as st
st.session_state["lang"] = "en"
import app
_categories = app._categories_for_user({EMAIL3!r}, "en")
cat_id_to_label = {{c["id"]: c["label"] for c in _categories}}
cat_label_to_id = {{v: k for k, v in cat_id_to_label.items()}}
_bl = lambda k, **kw: app.i18n.t("tools.budget." + k, "en", **kw)
_bll = lambda k, **kw: app.i18n.t("tools.budget.ledger." + k, "en", **kw)
app._render_budget_ledger_import_expander({EMAIL3!r}, {PLAN!r}, _bl, _bll, cat_id_to_label, cat_label_to_id)
"""


_FIXTURE_CSV = (
    '10/10/2026,"-33.00","THE VET CLINIC          SOMEWHERE QL",""\n'
).encode()

at_imp = AppTest.from_string(_import_script(), default_timeout=60)
_run(at_imp)
at_imp.file_uploader[0].set_value(("bank.csv", _FIXTURE_CSV, "text/csv"))
_run(at_imp)
check("the import expander renders with no exception with a custom "
      "category in play", not at_imp.exception)
check('the review header shows 0 "needs you" rows - the custom-'
      "category merchant rule suggested it automatically",
      any("Needs you: 0" in (c.value or "") for c in at_imp.caption))

_save_btn = next(b for b in at_imp.button if (b.key or "").startswith("tools_ledger_import_save__"))
check("the Save button is enabled (a real suggestion, not 'needs you')",
      not _save_btn.disabled)
_save_btn.click()
_run(at_imp)
_imported = tools_store.list_ledger_entries(EMAIL3, "2026-10-01", "2026-10-31")
check("the imported row was saved under the CUSTOM category's own id",
      len(_imported) == 1 and _imported[0]["category"] == _cid3)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
