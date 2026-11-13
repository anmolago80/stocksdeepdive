"""
Director's TASK 55 (13 Nov 2026) - Comparison page: compact chip
entry, editable saved lists, private notes. Behind new env var
COMPARE_CHIP_ENTRY_LIVE (default off; owner always sees the new
version for review, same "owner-override OR *_LIVE" pattern as every
other such gate in this codebase). When off (and not the owner), the
page renders exactly as it did before this task - proven below.

A) Compact entry: compare_config.compare_chip_entry_live() swaps
   app._render_comparison_input_row() (old stacked boxes, untouched)
   for app._render_comparison_input_row_chips() (new). Chips live in
   st.session_state["cmp_chips"]; typing + Enter (a one-input st.form
   with clear_on_submit) or pasting a comma/space/newline list (via
   the SAME app._parse_ticker_tokens() app._dispatch_search() itself
   now uses, pulled out as a shared helper for this task) adds
   tickers; a real st.button labelled "TICKER ✕" per chip (no
   raw-HTML onclick - CLAUDE.md's own documented Streamlit trap)
   removes it. Capped via the SAME app._cap_comparison_tickers() every
   other entry point already enforces.

B) Saved lists (signed-in only): a "Saved list" st.selectbox above the
   chip box loads a list's tickers into cmp_chips on selection. A
   second button next to Compare reads 'Update "<name>"' when a list
   is loaded (overwrites it via the SAME compare_lists_store.
   save_list() the old popover-based flow already used - "update" is
   simply a same-name save) with a "More" popover for Save as new
   list / Rename / Delete; with no list loaded it reads "Save this
   comparison" and opens a name-entry popover.

C) "My notes" column (signed-in only): a new compare_notes_store.py
   (same DB/WAL pattern as compare_lists_store.py, a new table since a
   note is per (user, ticker), independent of any one saved list - see
   that module's own docstring) backs an st.data_editor column
   (Comparison only) right after the free preview table (the one with
   the Long Score bar and Investment Signal LONG/WATCHLIST/AVOID
   wording the task named) and before the paywall gate. Every rerun
   diffs the editor's returned value against what's stored and
   persists only changed cells - a cell edit already triggers that
   rerun, so this IS the "saves automatically on blur/Enter" the task
   asked for, with no separate Save button. The same column is added
   to the `results` DataFrame the CSV download already serializes, so
   it flows into the CSV with no change to that code at all.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access (confirmed repeatedly throughout this project). AAPL/
MSFT/GOOGL/NVDA are used only as representative tickers; their scores
are pre-seeded into scan_store as a fresh "saved scan" row (the SAME
mechanism app._comparison_row_from_saved_scan()/_need_fresh_scan
already use to let Comparison skip a live fetch for a ticker with a
recent overnight-scan row - see that function's own docstring), never
a live computation.

Run: python3 tests/test_director_task55_compare_chips_lists_notes.py
"""
import inspect
import os
import sqlite3
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task55_compare_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("COMPARE_CHIP_ENTRY_LIVE", None)

from streamlit.testing.v1 import AppTest

import ai_gate
import compare_config
import compare_lists_store
import compare_notes_store
import data_export_engine
import scan_store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER_EMAIL = ai_gate.owner_email()
USER_A = "task55-user-a@example.com"
USER_B = "task55-user-b@example.com"

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


scan_store.save_scan("S&P 500", [
    {"Ticker": "AAPL", "Type": "STOCK", "Company Name": "Apple Inc.", "Price": 180.0,
     "Intrinsic Value": 210.0, "MOS %": 14.3, "Long Score": 65.0, "Quality": 75.0,
     "Psychology": 5.0, "Discovery (lite)": 32.0, "Moat": 55.0,
     "Growth Used": 0.08, "FCF Source": "reported", "Dividend Yield %": 0.5,
     "Signal": "LONG", "Valuation": "FAIR"},
    {"Ticker": "MSFT", "Type": "STOCK", "Company Name": "Microsoft Corp.", "Price": 410.0,
     "Intrinsic Value": 500.0, "MOS %": 18.0, "Long Score": 72.0, "Quality": 82.0,
     "Psychology": 4.0, "Discovery (lite)": 30.0, "Moat": 60.0,
     "Growth Used": 0.09, "FCF Source": "reported", "Dividend Yield %": 0.8,
     "Signal": "STRONG LONG", "Valuation": "CHEAP"},
], source_label="test fixture")


def _script(body, email=None, lang="en", chip_entry_live=False, owner=False, extra_state=None):
    _owner_setup = (
        f'import ai_gate\nimport paywall_engine as pw\n'
        f'pw.current_user_email = lambda: ai_gate.owner_email()\n'
    ) if owner else (
        f'import paywall_engine as pw\npw.current_user_email = lambda: {email!r}\n'
    )
    _env = 'os.environ["COMPARE_CHIP_ENTRY_LIVE"] = "1"\n' if chip_entry_live else \
        'os.environ.pop("COMPARE_CHIP_ENTRY_LIVE", None)\n'
    # setdefault(), NOT a bare assignment: a widget inside the function
    # under test can call st.rerun() (chip removal, the save/rename/
    # delete flows below all do), which re-executes this ENTIRE script
    # from the top - a bare `st.session_state[k] = v` line would re-
    # fire on that internal rerun too and clobber whatever the widget
    # code just wrote, before the test ever gets to inspect it (proven
    # directly with a standalone toy script before fixing this).
    _state_lines = "\n".join(
        f"st.session_state.setdefault({k!r}, {v!r})" for k, v in (extra_state or {}).items()
    )
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{_env}
{_owner_setup}
import streamlit as st
st.session_state["lang"] = {lang!r}
{_state_lines}
import app
{body}
"""


def _run(script):
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"AppTest raised: {at.exception}"
    return at


# ======================================================================
# SECTION A: gate - old row (byte-identical off/non-owner) vs new row.
# ======================================================================
_at_a1 = _run(_script(
    'app._render_comparison_input_row(st.session_state["lang"])',
    email="someone-else@example.com", chip_entry_live=False, owner=False,
))
check("gate OFF, non-owner: old stacked-box row renders its OWN compare "
      "button key ('cmp_compare_btn')",
      any(b.key == "cmp_compare_btn" for b in _at_a1.button))
check("gate OFF, non-owner: old row never renders the NEW chip row's "
      "compare button key",
      not any(b.key == "cmp_chips_compare_btn" for b in _at_a1.button))

_at_a2 = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    email="someone-else@example.com", chip_entry_live=True, owner=False,
    extra_state={"cmp_stocks": ["AAPL"]},
))
check("gate ON: the new chip row renders its OWN compare button key",
      any(b.key == "cmp_chips_compare_btn" for b in _at_a2.button))

_at_a3 = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    owner=True, chip_entry_live=False,
    extra_state={"cmp_stocks": ["AAPL"]},
))
check("gate OFF, OWNER: owner-override still reaches the new chip row "
      "when called directly (page_comparison()'s own OR is what "
      "selects it live - proven by source inspection below)",
      any(b.key == "cmp_chips_compare_btn" for b in _at_a3.button))

_page_src = inspect.getsource(__import__("app").page_comparison)
check("page_comparison() itself branches on compare_config."
      "compare_chip_entry_live() OR ai_gate.is_owner(...) - the SAME "
      "owner-override shape as every other *_LIVE gate in this codebase",
      "compare_config.compare_chip_entry_live() or ai_gate.is_owner(" in _page_src
      and "_render_comparison_input_row_chips(_cmp_lang)" in _page_src
      and "_render_comparison_input_row(_cmp_lang)" in _page_src)


# ======================================================================
# SECTION B: chip add / remove / paste / max.
# ======================================================================
_at_b = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    email=USER_A, chip_entry_live=True,
    extra_state={"cmp_stocks": ["AAPL"]},
))
check("B1: chips seeded from cmp_stocks - one removable chip button "
      "for AAPL",
      any(b.key == "cmp_chip_rm_AAPL" for b in _at_b.button))
check("B1: the '1 companies' count caption is shown",
      any("1" in c.value for c in _at_b.caption if "companies" in c.value.lower()
          or "empresas" in c.value.lower()))

# Add a single ticker by typing + submitting the form.
_at_b.text_input(key="cmp_chip_add_input").input("GOOGL")
_at_b.button(key="cmp_chip_add_submit").click()
_at_b.run()
check("B2: typing one ticker + submit adds it to cmp_chips",
      "GOOGL" in _at_b.session_state["cmp_chips"])

# Paste several at once, comma+space mixed - all added, deduped.
_at_b.text_input(key="cmp_chip_add_input").input("MSFT, NVDA AAPL")
_at_b.button(key="cmp_chip_add_submit").click()
_at_b.run()
check("B3: pasting a comma/space-separated list adds every new ticker",
      "MSFT" in _at_b.session_state["cmp_chips"]
      and "NVDA" in _at_b.session_state["cmp_chips"])
check("B3: a ticker already present (AAPL, pasted again) is not "
      "duplicated",
      _at_b.session_state["cmp_chips"].count("AAPL") == 1)
check("B3: chips are now AAPL, GOOGL, MSFT, NVDA (order-preserved, "
      "insertion order)",
      _at_b.session_state["cmp_chips"] == ["AAPL", "GOOGL", "MSFT", "NVDA"])

# Max cap: push well past COMPARE_MAX_TICKERS (15) in one paste.
_at_b.text_input(key="cmp_chip_add_input").input(
    " ".join(f"TICK{i}" for i in range(20))
)
_at_b.button(key="cmp_chip_add_submit").click()
_at_b.run()
check(f"B4: capped at compare_config.COMPARE_MAX_TICKERS "
      f"({compare_config.COMPARE_MAX_TICKERS}) even after a huge paste",
      len(_at_b.session_state["cmp_chips"]) == compare_config.COMPARE_MAX_TICKERS)
check("B4: the existing max-tickers warning fires (reused, not a new "
      "message)",
      any("15" in w.value or "capped" in w.value.lower() or "máximo" in w.value.lower()
          for w in _at_b.warning))

# Remove a chip by clicking its own "TICKER ✕" button.
_at_b2 = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    email=USER_A, chip_entry_live=True,
    extra_state={"cmp_chips": ["AAPL", "MSFT", "GOOGL"], "cmp_stocks": ["AAPL", "MSFT", "GOOGL"]},
))
_at_b2.button(key="cmp_chip_rm_MSFT").click()
_at_b2.run()
check("B5: clicking a chip's own remove button removes exactly that "
      "ticker, keeping the others in order",
      _at_b2.session_state["cmp_chips"] == ["AAPL", "GOOGL"])


# ======================================================================
# SECTION C: saved lists (signed-in only) - selector, update, save-as-
# new, rename, delete.
# ======================================================================
_at_c1 = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    email=None, chip_entry_live=True, extra_state={"cmp_stocks": ["AAPL"]},
))
check("C1: signed OUT - no 'Saved list' selectbox at all",
      not any(s.key == "cmp_saved_list_select" for s in _at_c1.selectbox))
check("C1: signed OUT - no Update/Save button either (the sign-in "
      "prompt shows instead)",
      not any(b.key in ("cmp_update_list_btn",) for b in _at_c1.button))

_LIST_NAME = "Task55 test list"
compare_lists_store.save_list(USER_A, _LIST_NAME, ["AAPL", "MSFT"])

_at_c2 = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    email=USER_A, chip_entry_live=True, extra_state={"cmp_stocks": ["GOOGL"]},
))
import i18n as _i18n_mod
check("C2: signed in - the 'Saved list' selectbox IS present, "
      "defaulting to 'No list selected'",
      any(s.key == "cmp_saved_list_select" for s in _at_c2.selectbox)
      and _at_c2.selectbox(key="cmp_saved_list_select").value
      == _i18n_mod.t("comparison.no_list_selected", "en"))
check("C2: with no list loaded yet, the 'Save this comparison' "
      "popover is present (the pre-existing key) and its own name-"
      "entry field is reachable - never the Update button",
      _at_c2.get_by_key("cmp_save_new_popover") is not None
      and any(t.key == "cmp_save_new_name_input" for t in _at_c2.text_input)
      and not any(b.key == "cmp_update_list_btn" for b in _at_c2.button))

# Selecting the saved list fills the chips and sets cmp_loaded_list_name.
_at_c2.selectbox(key="cmp_saved_list_select").select(_LIST_NAME)
_at_c2.run()
check("C3: selecting a saved list loads ITS tickers into cmp_chips",
      _at_c2.session_state.get("cmp_chips") == ["AAPL", "MSFT"])
check("C3: cmp_loaded_list_name now tracks the loaded list's name",
      _at_c2.session_state.get("cmp_loaded_list_name") == _LIST_NAME)

check("C4: the Update button is now present, labelled with the "
      "loaded list's name",
      any(b.key == "cmp_update_list_btn" for b in _at_c2.button)
      and any(_LIST_NAME in (b.label or "") for b in _at_c2.button
              if b.key == "cmp_update_list_btn"))

# C5: clicking Update overwrites the saved list with the CURRENT chips
# (which may have changed since loading - simulate that here).
_at_c2.session_state["cmp_chips"] = ["AAPL", "MSFT", "GOOGL"]
_at_c2.button(key="cmp_update_list_btn").click()
_at_c2.run()
_updated = compare_lists_store.load_list(USER_A, _LIST_NAME)
check("C5: clicking Update persists the CURRENT (edited) chip set "
      "under the SAME list name - 'update' really is save_list() by "
      "the same name",
      _updated == ["AAPL", "MSFT", "GOOGL"])

# C6: "More" popover -> Save as new list.
_at_c2.button(key="cmp_save_as_new_btn").click()
_at_c2.run()
_at_c2.text_input(key="cmp_save_as_new_name_input").input("Task55 copy")
_at_c2.button(key="cmp_save_as_new_confirm_btn").click()
_at_c2.run()
check("C6: Save as new list creates a SECOND, independent list",
      sorted(_l["name"] for _l in compare_lists_store.list_lists(USER_A))
      == sorted([_LIST_NAME, "Task55 copy"]))
check("C6: after Save as new, the loaded list switches to the NEW name",
      _at_c2.session_state.get("cmp_loaded_list_name") == "Task55 copy")

# C7: Rename (the now-loaded "Task55 copy" -> "Task55 renamed").
_at_c2.button(key="cmp_rename_loaded_btn").click()
_at_c2.run()
_at_c2.text_input(key="cmp_rename_loaded_input").input("Task55 renamed")
_at_c2.button(key="cmp_rename_loaded_confirm_btn").click()
_at_c2.run()
check("C7: Rename changes the stored list's name",
      any(_l["name"] == "Task55 renamed" for _l in compare_lists_store.list_lists(USER_A))
      and not any(_l["name"] == "Task55 copy" for _l in compare_lists_store.list_lists(USER_A)))
check("C7: cmp_loaded_list_name tracks the rename",
      _at_c2.session_state.get("cmp_loaded_list_name") == "Task55 renamed")

# C8: Delete the now-loaded "Task55 renamed" list.
_at_c2.button(key="cmp_delete_loaded_btn").click()
_at_c2.run()
_at_c2.button(key="cmp_delete_loaded_yes_btn").click()
_at_c2.run()
check("C8: Delete removes the list from storage",
      not any(_l["name"] == "Task55 renamed" for _l in compare_lists_store.list_lists(USER_A)))
check("C8: cmp_loaded_list_name is cleared after delete",
      _at_c2.session_state.get("cmp_loaded_list_name") is None)

# C9: with no list loaded, "Save this comparison" saves a brand-new
# list and the loaded-name pointer picks it up immediately.
_at_c3 = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    email=USER_B, chip_entry_live=True, extra_state={"cmp_stocks": ["AAPL", "MSFT"]},
))
_at_c3.text_input(key="cmp_save_new_name_input").input("Fresh save")
_at_c3.button(key="cmp_save_new_confirm_btn").click()
_at_c3.run()
check("C9: 'Save this comparison' (no list loaded yet) creates a "
      "list under USER_B's own account",
      compare_lists_store.load_list(USER_B, "Fresh save") == ["AAPL", "MSFT"])
check("C9: saving also sets cmp_loaded_list_name, so the SAME button "
      "immediately becomes 'Update' for what was just saved",
      _at_c3.session_state.get("cmp_loaded_list_name") == "Fresh save")


# ======================================================================
# SECTION D: "My notes" - store unit tests, then wiring into the page.
# ======================================================================
check("D1: no note yet -> get_note returns '' (never None)",
      compare_notes_store.get_note(USER_A, "AAPL") == "")
_ok_d1, _err_d1 = compare_notes_store.save_note(USER_A, "AAPL", "Great moat, watch debt")
check("D1: save_note() ok for a normal note", _ok_d1 and _err_d1 is None)
check("D1: get_note() round-trips exactly what was saved",
      compare_notes_store.get_note(USER_A, "AAPL") == "Great moat, watch debt")
check("D1: get_notes() bulk-reads the same note alongside a ticker "
      "with none",
      compare_notes_store.get_notes(USER_A, ["AAPL", "MSFT"])
      == {"AAPL": "Great moat, watch debt"})

check("D2: PRIVATE per user - USER_B has no note for the SAME ticker "
      "USER_A just wrote one for",
      compare_notes_store.get_note(USER_B, "AAPL") == "")
compare_notes_store.save_note(USER_B, "AAPL", "Different opinion entirely")
check("D2: USER_A's own note is unaffected by USER_B's independent "
      "note on the same ticker",
      compare_notes_store.get_note(USER_A, "AAPL") == "Great moat, watch debt"
      and compare_notes_store.get_note(USER_B, "AAPL") == "Different opinion entirely")

_ok_d3, _err_d3 = compare_notes_store.save_note(USER_A, "MSFT", "x" * 281)
check("D3: a note over 280 chars is rejected with 'note_too_long', "
      "nothing stored", not _ok_d3 and _err_d3 == "note_too_long"
      and compare_notes_store.get_note(USER_A, "MSFT") == "")
_ok_d3b, _err_d3b = compare_notes_store.save_note(USER_A, "MSFT", "x" * 280)
check("D3: exactly 280 chars is accepted (inclusive boundary)",
      _ok_d3b and compare_notes_store.get_note(USER_A, "MSFT") == "x" * 280)

compare_notes_store.save_note(USER_A, "GOOGL", "temporary note")
compare_notes_store.save_note(USER_A, "GOOGL", "   ")
check("D4: saving a blank/whitespace-only note DELETES the row "
      "entirely (not an empty string left behind)",
      compare_notes_store.get_note(USER_A, "GOOGL") == "")
with sqlite3.connect(compare_notes_store.DB_PATH) as _raw_conn:
    _row_count = _raw_conn.execute(
        "SELECT COUNT(*) FROM compare_notes WHERE email = ? AND ticker = ?",
        (USER_A, "GOOGL"),
    ).fetchone()[0]
check("D4: ...confirmed directly against the table - zero rows, not "
      "one row holding ''", _row_count == 0)

# D5-D6: wired into the real page render (saved-scan fixture, no live
# fetch) - the data_editor appears with a pre-filled note, and editing
# a cell persists it.
_at_d = _run(_script(
    'st.query_params["tickers"] = "AAPL,MSFT"\n'
    'app.page_comparison()',
    email=USER_A, chip_entry_live=True,
    extra_state={"cmp_stocks": ["AAPL", "MSFT"], "cmp_fresh": True,
                 "cmp_universe_source": "test fixture"},
))
_notes_editor_key = "cmp_notes_editor_cmp"
check("D5: the notes st.data_editor renders on the Comparison page "
      "(gate on, signed in)",
      any(d.key == _notes_editor_key for d in _at_d.dataframe))
_notes_df = next(d for d in _at_d.dataframe if d.key == _notes_editor_key).value
check("D5: AAPL's pre-existing note ('Great moat, watch debt') shows "
      "up pre-filled in the editor",
      "Great moat, watch debt" in list(_notes_df.iloc[:, 1]))

# Simulate editing MSFT's note cell the same way this codebase's own
# established AppTest technique for st.data_editor does elsewhere
# (test_budget_ledger_step2_grid.py) - no cell-edit-simulation API
# exists, but setting session_state[<editor key>] to the exact
# {"edited_rows":, "added_rows":, "deleted_rows":} shape Streamlit
# itself produces, then re-running, is indistinguishable from a real
# edit. `results` is sorted by Long Score descending (MSFT=72.0 >
# AAPL=65.0 in this fixture), so MSFT is row 0, never assumed by
# position - found by its actual Ticker value in the editor's own
# rendered rows, so this can't silently target the wrong row if the
# fixture or the sort ever changes.
_notes_col_label = _notes_df.columns[1]
_msft_row_idx = int(_notes_df.index[_notes_df["Ticker"] == "MSFT"][0])
_at_d.session_state[_notes_editor_key] = {
    "edited_rows": {_msft_row_idx: {_notes_col_label: "Overweight after Q3 beat"}},
    "added_rows": [], "deleted_rows": [],
}
_at_d.run()
check("D6: editing MSFT's note cell persists it immediately (no "
      "explicit Save button - the rerun itself is the autosave)",
      compare_notes_store.get_note(USER_A, "MSFT") == "Overweight after Q3 beat")

# D7: the CSV column - proven two ways (the mechanism works on real
# bytes; the render code adds the column BEFORE the CSV is built).
import pandas as pd
_csv_bytes = data_export_engine.table_to_csv_bytes(
    pd.DataFrame({"Ticker": ["AAPL"], "My notes": ["Great moat, watch debt"]})
)
check("D7: data_export_engine.table_to_csv_bytes() carries a 'My "
      "notes' column and its value through to the real CSV bytes",
      b"My notes" in _csv_bytes and b"Great moat" in _csv_bytes)
_render_src = inspect.getsource(__import__("app")._render_scan_results)
check("D7: _render_scan_results() adds results['My notes'] BEFORE "
      "building the CSV download bytes, so the download automatically "
      "carries it with no change to the CSV-building code itself",
      _render_src.index('results["My notes"] =') < _render_src.index("_dl_csv ="))

# Gate off / signed out: no notes column at all - byte-identical to
# before this task for everyone else.
_at_d_off = _run(_script(
    'st.query_params["tickers"] = "AAPL,MSFT"\n'
    'app.page_comparison()',
    email="someone-else@example.com", chip_entry_live=False, owner=False,
    extra_state={"cmp_stocks": ["AAPL", "MSFT"], "cmp_fresh": True,
                 "cmp_universe_source": "test fixture"},
))
check("gate OFF, non-owner: no 'My notes' data_editor at all",
      not any(d.key == _notes_editor_key for d in _at_d_off.dataframe))


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
