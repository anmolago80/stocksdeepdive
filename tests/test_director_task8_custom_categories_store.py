"""
Director TASK 8 (17 Oct 2026, "custom budget categories") - tools_store.
py's own data layer: custom_categories (list/create/rename/hide), each
re-checking ownership, same IDENTITY rule as every other table in this
module. Limits: max 10 visible per user, label 1-30 chars, unique
case-insensitively against presets and the user's other customs.

Uses a temp sqlite file (tools_store.DB_PATH patched), same isolation
pattern every other *_store.py test in this repo uses.

Run: python3 tests/test_director_task8_custom_categories_store.py
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
PRESETS = [
    "Mortgage / rent", "Transport (car, petrol, fares)", "Food & groceries",
]

with mock.patch.object(tools_store, "DB_PATH", _TMP_DB):

    # ------------------------------------------------------------------
    # CHECK 1-6: create - happy path, label trimming, default icon.
    # ------------------------------------------------------------------
    check("a brand-new email has no custom categories",
          tools_store.list_custom_categories("alice@example.com") == [])

    cid, err = tools_store.create_custom_category(
        "alice@example.com", "  Pet care  ", preset_labels=PRESETS,
    )
    check("create returns a new 'custom_1' id and no error",
          cid == "custom_1" and err is None)
    _listed = tools_store.list_custom_categories("alice@example.com")
    check("the label is stored trimmed", _listed[0]["label"] == "Pet care")
    check("a blank icon gets the default tag emoji",
          _listed[0]["icon"] == tools_store.DEFAULT_CUSTOM_CATEGORY_ICON)

    cid2, err2 = tools_store.create_custom_category(
        "alice@example.com", "Gym", icon="\U0001F3CB", preset_labels=PRESETS,
    )
    check("a second category gets id 'custom_2' and keeps its own icon",
          cid2 == "custom_2" and err2 is None
          and tools_store.list_custom_categories("alice@example.com")[1]["icon"] == "\U0001F3CB")

    # ------------------------------------------------------------------
    # CHECK 7-11: validation - empty label, too long, duplicate vs
    # preset (case-insensitive), duplicate vs own custom (case-
    # insensitive), no email.
    # ------------------------------------------------------------------
    _cid_empty, _err_empty = tools_store.create_custom_category(
        "alice@example.com", "   ", preset_labels=PRESETS,
    )
    check("an empty/whitespace-only label is rejected",
          _cid_empty is None and _err_empty == "label_required")

    _cid_long, _err_long = tools_store.create_custom_category(
        "alice@example.com", "x" * 31, preset_labels=PRESETS,
    )
    check("a label over 30 chars is rejected",
          _cid_long is None and _err_long == "label_too_long")
    _cid_exactly30, _err_exactly30 = tools_store.create_custom_category(
        "alice@example.com", "y" * 30, preset_labels=PRESETS,
    )
    check("a label of exactly 30 chars is accepted (boundary)",
          _cid_exactly30 is not None and _err_exactly30 is None)

    _cid_dup_preset, _err_dup_preset = tools_store.create_custom_category(
        "alice@example.com", "mortgage / rent", preset_labels=PRESETS,
    )
    check("a label matching a PRESET label case-insensitively is rejected",
          _cid_dup_preset is None and _err_dup_preset == "duplicate")

    _cid_dup_own, _err_dup_own = tools_store.create_custom_category(
        "alice@example.com", "PET CARE", preset_labels=PRESETS,
    )
    check("a label matching the user's OWN existing custom label "
          "case-insensitively is rejected",
          _cid_dup_own is None and _err_dup_own == "duplicate")

    _cid_noemail, _err_noemail = tools_store.create_custom_category(
        None, "Something", preset_labels=PRESETS,
    )
    check("no email is rejected", _cid_noemail is None and _err_noemail == "no_email")

    # ------------------------------------------------------------------
    # CHECK 12-13: the 10-visible limit, and that hiding one frees a
    # slot without ever reusing a retired id.
    # ------------------------------------------------------------------
    tools_store.delete_all_ledger_data("bob@example.com")
    for i in range(10):
        _bid, _berr = tools_store.create_custom_category(
            "bob@example.com", f"Bob cat {i}", preset_labels=PRESETS,
        )
        assert _bid is not None, (_bid, _berr)
    check("bob now has exactly 10 visible custom categories",
          len(tools_store.list_custom_categories("bob@example.com")) == 10)
    _cid_11th, _err_11th = tools_store.create_custom_category(
        "bob@example.com", "One too many", preset_labels=PRESETS,
    )
    check("an 11th custom category is rejected with 'limit_reached'",
          _cid_11th is None and _err_11th == "limit_reached")

    _hidden_ok = tools_store.hide_custom_category("bob@example.com", "custom_1")
    check("hiding bob's first custom category succeeds",
          _hidden_ok is True)
    check("bob now has only 9 VISIBLE custom categories",
          len(tools_store.list_custom_categories("bob@example.com")) == 9)

    _cid_after_hide, _err_after_hide = tools_store.create_custom_category(
        "bob@example.com", "Fresh slot", preset_labels=PRESETS,
    )
    check("hiding one frees a slot - bob can create an 11th-ever category now",
          _cid_after_hide is not None and _err_after_hide is None)
    check("the new category's id is 'custom_11' - NEVER reuses the hidden "
          "'custom_1', even though a visible slot was free",
          _cid_after_hide == "custom_11")

    # A label matching a HIDDEN custom category's own (retired) label
    # is still rejected - "the user's other customs" includes hidden
    # ones, so a retired label can't be silently recycled either. Bob
    # is back at 10 visible (the "Fresh slot" create above refilled
    # the slot "custom_1" freed) - hide one more first so this check
    # actually exercises the DUPLICATE rule, not the limit one.
    tools_store.hide_custom_category("bob@example.com", "custom_2")
    _cid_reuse_hidden_label, _err_reuse_hidden_label = tools_store.create_custom_category(
        "bob@example.com", "Bob cat 0", preset_labels=PRESETS,
    )
    check("a label matching a HIDDEN custom category's own label is "
          "still rejected as a duplicate",
          _cid_reuse_hidden_label is None and _err_reuse_hidden_label == "duplicate")

    # ------------------------------------------------------------------
    # CHECK 14-18: rename - happy path, re-validates the same rules,
    # excludes its OWN current label from the duplicate check (so
    # renaming "Pet care" to "Pet care" again, or to itself with
    # different case, doesn't false-positive as a duplicate).
    # ------------------------------------------------------------------
    _ok_rename, _err_rename = tools_store.rename_custom_category(
        "alice@example.com", "custom_1", "Pet expenses", preset_labels=PRESETS,
    )
    check("renaming succeeds", _ok_rename is True and _err_rename is None)
    check("the rename actually changed the stored label",
          tools_store.list_custom_categories("alice@example.com")[0]["label"] == "Pet expenses")

    _ok_rename_self, _err_rename_self = tools_store.rename_custom_category(
        "alice@example.com", "custom_1", "pet EXPENSES", preset_labels=PRESETS,
    )
    check("renaming a category to a different-case version of its OWN "
          "current label succeeds (excludes itself from the duplicate "
          "check)", _ok_rename_self is True and _err_rename_self is None)

    _ok_rename_dup, _err_rename_dup = tools_store.rename_custom_category(
        "alice@example.com", "custom_1", "Gym", preset_labels=PRESETS,
    )
    check("renaming to match a DIFFERENT existing custom category's "
          "label is still rejected as a duplicate",
          _ok_rename_dup is False and _err_rename_dup == "duplicate")

    _ok_rename_missing, _err_rename_missing = tools_store.rename_custom_category(
        "alice@example.com", "custom_999", "Whatever", preset_labels=PRESETS,
    )
    check("renaming a non-existent category id reports 'not_found'",
          _ok_rename_missing is False and _err_rename_missing == "not_found")

    _ok_rename_empty, _err_rename_empty = tools_store.rename_custom_category(
        "alice@example.com", "custom_1", "   ", preset_labels=PRESETS,
    )
    check("renaming to an empty label is rejected the same as create",
          _ok_rename_empty is False and _err_rename_empty == "label_required")

    # ------------------------------------------------------------------
    # CHECK 19-22: hide - never deletes, re-checks ownership.
    # ------------------------------------------------------------------
    _hide_missing = tools_store.hide_custom_category("alice@example.com", "custom_999")
    check("hiding a non-existent category id returns False, never raises",
          _hide_missing is False)

    # ------------------------------------------------------------------
    # CHECK 23-27: USER ISOLATION - the core privacy requirement,
    # mirrored exactly from the ledger's own data-layer test. Two BRAND
    # NEW users with zero prior categories (ids are PER-USER, scoped
    # by (user_id, id) - alice/bob above already have their OWN
    # "custom_1" rows, so reusing them here would just exercise each
    # user's own row, not prove isolation at all; a user with no row
    # for that id makes the "not_found" unambiguous).
    _carol_cid, _carol_err = tools_store.create_custom_category(
        "carol@example.com", "Carol's secret category", preset_labels=PRESETS,
    )
    check("carol's create succeeded (fresh user, fresh id)",
          _carol_cid is not None and _carol_err is None)
    check("dave (a different, brand-new user with no categories at all) "
          "sees an empty list, never carol's category",
          tools_store.list_custom_categories("dave@example.com") == [])
    _rename_cross = tools_store.rename_custom_category(
        "dave@example.com", _carol_cid, "Hacked by dave", preset_labels=PRESETS,
    )
    check("dave cannot rename carol's category even by its exact id - "
          "he has no row at all for that id, so it's 'not_found' "
          "(same (user_id, id) WHERE clause as every other table here)",
          _rename_cross == (False, "not_found"))
    check("...and carol's category is provably untouched",
          tools_store.list_custom_categories("carol@example.com")[0]["label"]
          == "Carol's secret category")
    _hide_cross = tools_store.hide_custom_category("dave@example.com", _carol_cid)
    check("dave cannot hide carol's category even by its exact id",
          _hide_cross is False)
    check("...and carol's category is still visible to carol",
          len(tools_store.list_custom_categories("carol@example.com")) == 1)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
