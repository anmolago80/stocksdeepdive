"""
Director TASK 9 (17 Oct 2026): Property vs Index year-by-year table
crash.

Verified in production logs (17 Oct 01:44 UTC): pyarrow's own
ArrowInvalid, "Could not convert 'Total' with type str: ... Conversion
failed for column Year". app.py's _render_property_vs_index_tool()
builds the year-by-year table's "Year" column from row["year"] (an
int) for every per-year row, then appends a totals row whose own
"Year" cell is the STRING label _sl("table_totals_row_label")
("Total") - pyarrow infers one dtype for the whole column, and a
column that is ints everywhere except one string cell can't convert,
crashing the ENTIRE table (every row, not just the totals one).

Fix: str(row["year"]) for every per-year row, so the whole column is
text, matching the totals row's own "Total" string. Display only -
_style_pvi_table()'s own highlight logic is purely positional (an
.iloc lookup keyed off _rental["first_positive_year"], a row INDEX),
never reads the Year column's value, so this changes nothing about
what the table calculates or highlights.

Run: python3 tests/test_director_task9_property_vs_index_year_column_crash.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task9_pvi_year_crash_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd
import pyarrow as pa

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


# ======================================================================
# CHECK 1-2: reproduce the EXACT production bug first, to prove this
# is a genuine fix, not a no-op - a Year column that is ints for every
# per-year row plus one string "Total" cell fails pyarrow conversion.
# ======================================================================
_old_style_rows = [
    {"Year": 1, "Rent": "$500"},
    {"Year": 2, "Rent": "$520"},
    {"Year": "Total", "Rent": "$1,020"},
]
_old_df = pd.DataFrame(_old_style_rows)
_reproduced_crash = False
try:
    pa.Table.from_pandas(_old_df)
except pa.lib.ArrowInvalid as e:
    _reproduced_crash = True
    _crash_msg = str(e)
check("reproduces the original bug: a Year column with ints for every "
      "per-year row plus a string 'Total' totals-row cell genuinely "
      "crashes pyarrow's Arrow conversion (proving this is a real fix, "
      "not a no-op)",
      _reproduced_crash)
if _reproduced_crash:
    check("the reproduced crash is the SAME ArrowInvalid/'Could not "
          "convert' error seen in production logs",
          "Could not convert" in _crash_msg or "convert" in _crash_msg.lower())

# ======================================================================
# CHECK 3-4: the fix - str(row["year"]) for every per-year row - never
# crashes, and the whole Year column is text (every value a str,
# including the totals row's own "Total" label).
# ======================================================================
_new_style_rows = [
    {"Year": str(1), "Rent": "$500"},
    {"Year": str(2), "Rent": "$520"},
    {"Year": "Total", "Rent": "$1,020"},
]
_new_df = pd.DataFrame(_new_style_rows)
_no_crash = True
try:
    pa.Table.from_pandas(_new_df)
except Exception:
    _no_crash = False
check("the fix (str(row['year']) for every per-year row) converts to "
      "Arrow cleanly, never crashing",
      _no_crash)
check("every value in the fixed Year column is a str, including the "
      "totals row's own label",
      all(isinstance(v, str) for v in _new_df["Year"]))


# ======================================================================
# CHECK 5-8: end to end through the REAL app.py render function, via
# the real ?tool=property_vs_index deep link (same AppTest pattern as
# tests/test_part_a_followup_one_source_of_truth.py) - no mocking of
# property_vs_index_engine itself, since run() has no network calls.
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False
import app
st.session_state["email_user"] = "task9_pvi_reader@example.com"
app.page_tools()
"""
_at = AppTest.from_string(_script, default_timeout=60)
_at.query_params["tool"] = "property_vs_index"
_at.run()
check("app.page_tools() with ?tool=property_vs_index renders without "
      "raising (the crash this task fixes would surface here)",
      not _at.exception)
if _at.exception:
    print(f"    exception: {_at.exception}")

_year_by_year_table = None
for df in _at.dataframe:
    try:
        _cols = list(df.value.columns)
    except Exception:
        continue
    if any("year" in str(c).lower() for c in _cols):
        _year_by_year_table = df.value
        break

check("the year-by-year rental position table rendered at all (not "
      "silently skipped)", _year_by_year_table is not None)

if _year_by_year_table is not None:
    _year_col = next(c for c in _year_by_year_table.columns if "year" in str(c).lower())
    _year_values = list(_year_by_year_table[_year_col])
    check("every rendered Year cell is a string (the per-year rows AND "
          "the totals row, same column, same dtype)",
          all(isinstance(v, str) for v in _year_values))
    check("the totals row's own label ('Total'-shaped text, not a "
          "number) is present among the rendered Year values",
          any(not v.strip().isdigit() for v in _year_values))
    check("at least one per-year numeric year is still present and "
          "readable as a plain year string",
          any(v.strip().isdigit() for v in _year_values))


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
