"""Compare Commit 3 (27 Sep 2026, owner-directed): unit fixtures for
compare_lists_store.py - save/load/rename/delete, the shared 15-ticker
cap, de-duplication, and the "never store anything for a signed-out
visitor" guarantee (every function is a no-op/False for email=None).
Run: python3 test_compare_lists_store.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="compare_lists_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import compare_lists_store as cls
import compare_config

# Fresh db for this run.
db_path = cls.DB_PATH
if os.path.exists(db_path):
    os.remove(db_path)

EMAIL = "owner@test.com"
OTHER_EMAIL = "other@test.com"

# ---- signed-out: every function is a safe no-op ----
assert cls.list_lists(None) == []
ok, err = cls.save_list(None, "My list", ["AAPL", "MSFT"])
assert ok is False and err == "not_signed_in"
assert cls.load_list(None, "My list") is None
ok, err = cls.rename_list(None, "a", "b")
assert ok is False and err == "not_signed_in"
cls.delete_list(None, "My list")  # must not raise
assert cls.list_lists(EMAIL) == []  # confirms nothing was stored under any email
print("[signed_out_noop] every function is a safe no-op with no email - nothing ever stored OK")

# ---- save + load ----
ok, err = cls.save_list(EMAIL, "Retail picks", ["aapl", "TGT", "wmt"])
assert ok is True and err is None
loaded = cls.load_list(EMAIL, "Retail picks")
assert loaded == ["AAPL", "TGT", "WMT"]  # upper-cased
print("[save_and_load] tickers upper-cased on save, round-trip via load OK")

# ---- de-dup + cap at COMPARE_MAX_TICKERS ----
big_list = [f"T{i}" for i in range(20)] + ["T0", "T1"]  # 20 unique + 2 dupes
ok, err = cls.save_list(EMAIL, "Big list", big_list)
assert ok is True
loaded_big = cls.load_list(EMAIL, "Big list")
assert len(loaded_big) == compare_config.COMPARE_MAX_TICKERS
assert loaded_big == [f"T{i}" for i in range(compare_config.COMPARE_MAX_TICKERS)]
print(f"[cap_and_dedupe] 20 tickers + 2 dupes -> saved list capped at "
      f"{compare_config.COMPARE_MAX_TICKERS} (the SAME shared cap every other Compare "
      f"entry point uses), de-duplicated, order preserved OK")

# ---- overwrite same name ----
ok, err = cls.save_list(EMAIL, "Retail picks", ["COST"])
assert ok is True
assert cls.load_list(EMAIL, "Retail picks") == ["COST"]
lists_now = cls.list_lists(EMAIL)
assert sum(1 for l in lists_now if l["name"] == "Retail picks") == 1
print("[overwrite_same_name] saving under an existing name overwrites it, not a duplicate row OK")

# ---- validation errors ----
ok, err = cls.save_list(EMAIL, "", ["AAPL"])
assert ok is False and err == "name_required"
ok, err = cls.save_list(EMAIL, "x" * 61, ["AAPL"])
assert ok is False and err == "name_too_long"
ok, err = cls.save_list(EMAIL, "Empty", [])
assert ok is False and err == "no_tickers"
ok, err = cls.save_list(EMAIL, "Empty", [None, "", "  "])
assert ok is False and err == "no_tickers"
print("[validation_errors] empty name / too-long name / no real tickers all rejected with the "
      "right error key OK")

# ---- rename ----
ok, err = cls.save_list(EMAIL, "Old name", ["AAPL"])
assert ok is True
ok, err = cls.rename_list(EMAIL, "Old name", "New name")
assert ok is True and err is None
assert cls.load_list(EMAIL, "Old name") is None
assert cls.load_list(EMAIL, "New name") == ["AAPL"]
print("[rename] old name gone, new name holds the same tickers OK")

# Rename collision: refuse if the target name already exists for this user.
cls.save_list(EMAIL, "Existing", ["MSFT"])
ok, err = cls.rename_list(EMAIL, "New name", "Existing")
assert ok is False and err == "name_taken"
assert cls.load_list(EMAIL, "New name") == ["AAPL"]  # untouched
print("[rename_collision] renaming onto an existing name refuses, original list untouched OK")

# ---- delete ----
cls.delete_list(EMAIL, "Existing")
assert cls.load_list(EMAIL, "Existing") is None
print("[delete] deleted list no longer loads OK")

# ---- per-account isolation ----
cls.save_list(OTHER_EMAIL, "Retail picks", ["NKE"])
assert cls.load_list(EMAIL, "Retail picks") == ["COST"]  # unaffected by the other account
assert cls.load_list(OTHER_EMAIL, "Retail picks") == ["NKE"]
own_names = {l["name"] for l in cls.list_lists(EMAIL)}
assert "Retail picks" in own_names
other_names = {l["name"] for l in cls.list_lists(OTHER_EMAIL)}
assert other_names == {"Retail picks"}
print("[account_isolation] two accounts can each have a list of the same name, fully "
      "independent - lists belong to the account OK")

print("\nALL COMPARE_LISTS_STORE FIXTURES PASSED")
