"""instruction_budget_ledger.md STEP 1 (9 Oct 2026, Director-directed,
behind LEDGER_LIVE): data layer for the spending ledger - ledger_entries/
ledger_rules/ledger_imports/ledger_category_flags in tools_store.py.

Tests required by the instruction itself: create/read/update/delete per
user, user isolation, cents round-trip, and that deleting a category
from the Plan tab never deletes its ledger rows.

Uses a temp sqlite file (tools_store.DB_PATH patched) so this never
touches the real stocksdeepdive.db - same isolation pattern every other
*_store.py test in this repo uses.

Run: python3 tests/test_budget_ledger_step1_data_layer.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools_store

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


_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

with mock.patch.object(tools_store, "DB_PATH", _TMP_DB):
    # -------------------------------------------------------------
    # Create / read
    # -------------------------------------------------------------
    _id = tools_store.create_ledger_entry(
        "alice@example.com", "2026-10-07", "Woolworths", "Groceries", 4810,
        source="manual",
    )
    check("create_ledger_entry returns a new id", isinstance(_id, int))

    _row = tools_store.get_ledger_entry("alice@example.com", _id)
    check("get_ledger_entry returns the row just created", _row is not None)
    check("amount_cents round-trips exactly (4810, not a float-drifted value)",
          _row["amount_cents"] == 4810 and isinstance(_row["amount_cents"], int))
    check("category round-trips", _row["category"] == "Groceries")
    check("description round-trips", _row["description"] == "Woolworths")
    check("source round-trips", _row["source"] == "manual")

    # Cents round-trip with a value that would show float drift if this
    # were ever stored as a float (e.g. 0.1 + 0.2 != 0.3 in binary float).
    _id_cents = tools_store.create_ledger_entry(
        "alice@example.com", "2026-10-08", "Tricky amount", "Fun", 1234, source="manual",
    )
    _row_cents = tools_store.get_ledger_entry("alice@example.com", _id_cents)
    check("a second, different cents value round-trips exactly too (1234)",
          _row_cents["amount_cents"] == 1234)

    # -------------------------------------------------------------
    # list_ledger_entries (month range)
    # -------------------------------------------------------------
    _month_rows = tools_store.list_ledger_entries("alice@example.com", "2026-10-01", "2026-10-31")
    check("list_ledger_entries returns both October rows", len(_month_rows) == 2)
    _sept_rows = tools_store.list_ledger_entries("alice@example.com", "2026-09-01", "2026-09-30")
    check("list_ledger_entries returns nothing for a month with no entries", _sept_rows == [])

    # -------------------------------------------------------------
    # Update
    # -------------------------------------------------------------
    _ok = tools_store.update_ledger_entry("alice@example.com", _id, amount_cents=5000, category="Shopping")
    check("update_ledger_entry reports success", _ok is True)
    _row_after = tools_store.get_ledger_entry("alice@example.com", _id)
    check("update_ledger_entry actually changed amount_cents", _row_after["amount_cents"] == 5000)
    check("update_ledger_entry actually changed category", _row_after["category"] == "Shopping")
    check("update_ledger_entry left description untouched (not passed)",
          _row_after["description"] == "Woolworths")

    # -------------------------------------------------------------
    # Delete
    # -------------------------------------------------------------
    _del_ok = tools_store.delete_ledger_entry("alice@example.com", _id)
    check("delete_ledger_entry reports success", _del_ok is True)
    check("the row is actually gone", tools_store.get_ledger_entry("alice@example.com", _id) is None)
    _del_again = tools_store.delete_ledger_entry("alice@example.com", _id)
    check("deleting an already-deleted row reports False, doesn't raise", _del_again is False)

    # -------------------------------------------------------------
    # User isolation - the core privacy requirement (section 0: "Write
    # a test proving user A can never read user B's rows").
    # -------------------------------------------------------------
    _bob_id = tools_store.create_ledger_entry(
        "bob@example.com", "2026-10-07", "Bob's secret purchase", "Fun", 9999, source="manual",
    )
    check("alice cannot read bob's row by id", tools_store.get_ledger_entry("alice@example.com", _bob_id) is None)
    check("bob can read his own row by id", tools_store.get_ledger_entry("bob@example.com", _bob_id) is not None)
    check("alice's monthly list never contains bob's row",
          not any(r["id"] == _bob_id for r in tools_store.list_ledger_entries("alice@example.com", "2026-10-01", "2026-10-31")))
    check("bob's monthly list never contains alice's remaining row",
          not any(r["id"] == _id_cents for r in tools_store.list_ledger_entries("bob@example.com", "2026-10-01", "2026-10-31")))
    check("alice cannot update bob's row even by his exact id",
          tools_store.update_ledger_entry("alice@example.com", _bob_id, amount_cents=1) is False)
    _bob_row_untouched = tools_store.get_ledger_entry("bob@example.com", _bob_id)
    check("...and bob's row is provably untouched by alice's attempted update",
          _bob_row_untouched["amount_cents"] == 9999)
    check("alice cannot delete bob's row even by his exact id",
          tools_store.delete_ledger_entry("alice@example.com", _bob_id) is False)
    check("...and bob's row still exists", tools_store.get_ledger_entry("bob@example.com", _bob_id) is not None)

    # -------------------------------------------------------------
    # Deleting a category from the Plan tab never deletes/corrupts its
    # ledger rows (no foreign key - rows keep their category text
    # forever regardless of what the current plan contains).
    # -------------------------------------------------------------
    tools_store.ensure_default_budget_plan("alice@example.com")
    tools_store.save_budget_plan("alice@example.com", "My Plan", 9000.0, {"mortgage": 1800.0})
    _ledger_row_mortgage = tools_store.create_ledger_entry(
        "alice@example.com", "2026-10-09", "Rent payment", "mortgage", 180000, source="manual",
    )
    # "Delete" the category from the plan - save a new version with it gone.
    tools_store.save_budget_plan("alice@example.com", "My Plan", 9000.0, {})
    _plan_after = tools_store.get_budget_plan("alice@example.com", "My Plan")
    check("the plan itself no longer has 'mortgage' in its categories",
          "mortgage" not in (_plan_after.get("categories") or {}))
    _row_still_there = tools_store.get_ledger_entry("alice@example.com", _ledger_row_mortgage)
    check("the ledger row that used 'mortgage' is completely unaffected - still exists",
          _row_still_there is not None)
    check("...with its category text unchanged ('mortgage', ready to show '(category removed)' in the UI)",
          _row_still_there["category"] == "mortgage")

    # Deleting the WHOLE plan (not just a category) must still never
    # touch ledger_entries - no cascade exists by construction.
    tools_store.delete_budget_plan("alice@example.com", "My Plan")
    check("get_budget_plan returns nothing after the plan is deleted",
          tools_store.get_budget_plan("alice@example.com", "My Plan") is None)
    check("the ledger row survives even a full plan deletion",
          tools_store.get_ledger_entry("alice@example.com", _ledger_row_mortgage) is not None)

    # -------------------------------------------------------------
    # ledger_rules
    # -------------------------------------------------------------
    check("get_merchant_rule returns None before any rule exists",
          tools_store.get_merchant_rule("alice@example.com", "woolworths") is None)
    tools_store.upsert_merchant_rule("alice@example.com", "WOOLWORTHS", "Groceries")
    check("a rule saved with a mixed-case key is readable lower-cased",
          tools_store.get_merchant_rule("alice@example.com", "woolworths") == "Groceries")
    tools_store.upsert_merchant_rule("alice@example.com", "woolworths", "Food")
    check("upserting the same merchant_key again updates it, not duplicates it",
          tools_store.get_merchant_rule("alice@example.com", "woolworths") == "Food")
    check("bob has no rule for alice's merchant key (user isolation on rules too)",
          tools_store.get_merchant_rule("bob@example.com", "woolworths") is None)

    # -------------------------------------------------------------
    # ledger_imports (counts only)
    # -------------------------------------------------------------
    _imp_id = tools_store.record_ledger_import(
        "alice@example.com", rows_read=11, rows_saved=9, rows_skipped=2,
        date_from="2026-09-20", date_to="2026-10-07",
    )
    check("record_ledger_import returns a new id", isinstance(_imp_id, int))

    # -------------------------------------------------------------
    # ledger_category_flags (is_bill)
    # -------------------------------------------------------------
    check("is_category_bill defaults to False for an un-flagged category",
          tools_store.is_category_bill("alice@example.com", "mortgage") is False)
    tools_store.set_category_is_bill("alice@example.com", "mortgage", True)
    check("is_category_bill is True after being set", tools_store.is_category_bill("alice@example.com", "mortgage") is True)
    check("bob's flags are independent of alice's (user isolation on flags too)",
          tools_store.is_category_bill("bob@example.com", "mortgage") is False)
    tools_store.set_category_is_bill("alice@example.com", "mortgage", False)
    check("is_category_bill can be un-set back to False", tools_store.is_category_bill("alice@example.com", "mortgage") is False)

os.remove(_TMP_DB)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
