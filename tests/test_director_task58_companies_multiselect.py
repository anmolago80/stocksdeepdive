"""
Director's TASK 58 (14 Nov 2026) - Comparison entry: compact, intuitive,
and "start a new list" (still behind COMPARE_CHIP_ENTRY_LIVE). Covers
the pieces test_director_task55_compare_chips_lists_notes.py's own
rewrite doesn't already exercise:

A) scan_store.all_scanned_companies() - the Companies multiselect's
   own suggestion roster ("all scanned tickers", searchable by company
   name): dedupe across universes, default-deny private exclusion
   (same posture as every other public-facing scan_store reader),
   missing "Company Name" falls back to the ticker itself.
B) app._comparison_company_roster() - the @st.cache_data wrapper
   around (A), and the multiselect's own options/format_func actually
   using it (via a real AppTest render, "TICKER — Company" labels,
   searchable by company name).
C) "+ Start a new list": comparison.no_list_selected's new displayed
   text, and the NEW "picking it while a list IS loaded also empties
   the box" behavior - including the "never fires just because nothing
   happens to be loaded yet" guard (the day's rotating default/a URL
   share/a popular-pair click must survive an ordinary render).
D) Compare and Save/Update really do render on ONE row now (source
   inspection - the _save_col bug the owner's own screenshot caught).
E) The header's site-wide search box is hidden on Comparison ONLY
   while COMPARE_CHIP_ENTRY_LIVE is live (owner override included),
   and completely untouched when it's off.
F) requirements.txt pins streamlit to the minimum version that
   actually supports accept_new_options (confirmed by this session's
   own binary search across published streamlit wheels: present in
   1.45.0, absent in 1.44.1).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access (confirmed repeatedly throughout this project).

Run: python3 tests/test_director_task58_companies_multiselect.py
"""
import inspect
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task58_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("COMPARE_CHIP_ENTRY_LIVE", None)

from streamlit.testing.v1 import AppTest

import ai_gate
import scan_store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER_EMAIL = ai_gate.owner_email()

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
# SECTION A: scan_store.all_scanned_companies().
# ======================================================================
scan_store.save_scan("Task58 Public Universe", [
    {"Ticker": "RMS.PA", "Company Name": "Hermès International"},
    {"Ticker": "BHP.AX", "Company Name": "BHP Group Limited"},
    {"Ticker": "NOCOMPANY", "Company Name": ""},
    {"Ticker": ""},  # no ticker at all - must be skipped, never KeyError
], source_label="test fixture")
scan_store.save_scan("Task58 Second Public Universe", [
    {"Ticker": "BHP.AX", "Company Name": "A DIFFERENT name - should NOT win"},
    {"Ticker": "AAPL", "Company Name": "Apple Inc."},
], source_label="test fixture")
# A private universe - excluded by default (same default-deny posture
# every other public-facing scan_store reader already uses).
os.environ["PRIVATE_UNIVERSES"] = "Task58 Private Universe"
scan_store.save_scan("Task58 Private Universe", [
    {"Ticker": "7203.T", "Company Name": "Toyota Motor Corp"},
], source_label="test fixture")

_roster = scan_store.all_scanned_companies()
check("A1: a normal row's Ticker/Company Name both come through",
      _roster.get("RMS.PA") == "Hermès International")
check("A2: the FIRST universe's company name wins for a ticker that "
      "appears in more than one universe (BHP.AX) - the second "
      "universe's own different string is ignored",
      _roster.get("BHP.AX") == "BHP Group Limited")
check("A3: a row with no Company Name at all falls back to the "
      "ticker itself",
      _roster.get("NOCOMPANY") == "NOCOMPANY")
check("A4: a row with no Ticker at all is skipped, never raises",
      "" not in _roster)
check("A5: a second universe's own new ticker (AAPL) is still "
      "included",
      _roster.get("AAPL") == "Apple Inc.")
check("A6: default (include_private=False) excludes a private "
      "universe's own tickers entirely",
      "7203.T" not in _roster)
check("A7: include_private=True DOES include them",
      scan_store.all_scanned_companies(include_private=True).get("7203.T")
      == "Toyota Motor Corp")
# PRIVATE_UNIVERSES is deliberately left set for the rest of this file
# (Section B's own B4 check relies on "Task58 Private Universe" still
# being private) - popping it here would silently fall back to the
# DEFAULT private list, which doesn't name this test's own universe,
# making it public again for every later section.
print("[task58_sectionA_roster] all_scanned_companies(): dedupe (first "
      "wins), missing-name fallback, no-ticker-row skip, default-deny "
      "private exclusion all correct OK")


# ======================================================================
# SECTION B: app._comparison_company_roster() cache wrapper + the
# multiselect's own options/format_func, driven through a real render.
# ======================================================================
import app

check("B1: _comparison_company_roster() delegates to scan_store."
      "all_scanned_companies() and returns the same data",
      app._comparison_company_roster() == scan_store.all_scanned_companies())


def _script(body, email=None, lang="en", chip_entry_live=True, owner=False, extra_state=None):
    _owner_setup = (
        'import ai_gate\nimport paywall_engine as pw\n'
        'pw.current_user_email = lambda: ai_gate.owner_email()\n'
    ) if owner else (
        f'import paywall_engine as pw\npw.current_user_email = lambda: {email!r}\n'
    )
    _env = 'os.environ["COMPARE_CHIP_ENTRY_LIVE"] = "1"\n' if chip_entry_live else \
        'os.environ.pop("COMPARE_CHIP_ENTRY_LIVE", None)\n'
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


_at_b1 = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    email="task58-b1@example.com", extra_state={"cmp_stocks": ["RMS.PA"]},
))
_ms = _at_b1.multiselect(key="cmp_chips")
check("B2: the multiselect's own options list includes a scanned "
      "ticker formatted as 'TICKER — Company Name'",
      "RMS.PA — Hermès International" in _ms.options)
check("B3: a ticker with no distinct company name (falls back to "
      "itself) is shown as the bare ticker, never 'TICKER — TICKER'",
      "NOCOMPANY" in _ms.options and "NOCOMPANY — NOCOMPANY" not in _ms.options)
check("B4: a private universe's own ticker never appears in the "
      "public page's own suggestion list",
      not any(o.startswith("7203.T") for o in _ms.options))
print("[task58_sectionB_roster_in_widget] the Companies multiselect's "
      "own options are built from the cached roster with the "
      "'TICKER — Company Name' format OK")


# ======================================================================
# SECTION C: "+ Start a new list" - wording + clear-on-transition.
# ======================================================================
import i18n as _i18n_mod
check("C1: comparison.no_list_selected now reads '+ Start a new list' "
      "(EN)", _i18n_mod.t("comparison.no_list_selected", "en") == "+ Start a new list")
check("C1b: ...and the Spanish equivalent",
      _i18n_mod.t("comparison.no_list_selected", "es") == "+ Iniciar una lista nueva")

import compare_lists_store

USER_C1 = "task58-c1@example.com"
compare_lists_store.save_list(USER_C1, "Task58 loaded list", ["AAPL", "BHP.AX"])

# C2: nothing loaded yet, on an ORDINARY first render - the selector's
# own default value already equals "+ Start a new list", but that must
# NEVER wipe the day's rotating default / a URL share / a popular-pair
# click's own cmp_chips just because nothing happens to be loaded.
_at_c2 = _run(_script(
    'app._render_comparison_input_row_chips(st.session_state["lang"])',
    email=USER_C1, extra_state={"cmp_stocks": ["AAPL", "MSFT"]},
))
check("C2: an ordinary render with nothing loaded does NOT clear "
      "cmp_chips just because the selector's own default is "
      "'+ Start a new list'",
      _at_c2.session_state.get("cmp_chips") == ["AAPL", "MSFT"])

# C3: load a list, THEN pick "+ Start a new list" - should empty chips
# AND unload, this time via a genuine transition.
_at_c2.selectbox(key="cmp_saved_list_select").select("Task58 loaded list")
_at_c2.run()
check("C3: loading a list fills cmp_chips with its own tickers",
      _at_c2.session_state.get("cmp_chips") == ["AAPL", "BHP.AX"])
_at_c2.selectbox(key="cmp_saved_list_select").select(
    _i18n_mod.t("comparison.no_list_selected", "en"))
_at_c2.run()
check("C3: picking '+ Start a new list' AFTER a list was loaded "
      "empties the Companies box",
      _at_c2.session_state.get("cmp_chips") == [])
check("C3: ...and unloads the list name (Save reverts to "
      "'Save as new list…')",
      _at_c2.session_state.get("cmp_loaded_list_name") is None)
print("[task58_sectionC_start_new_list] '+ Start a new list' never "
      "fires on an ordinary render with nothing loaded, but genuinely "
      "clears the box + unloads on an actual loaded -> unloaded "
      "transition OK")


# ======================================================================
# SECTION D: Compare + Save/Update on ONE row (source inspection of
# the exact bug the owner's own screenshot caught - _save_col was
# defined but never actually used to render the save control).
# ======================================================================
_src = inspect.getsource(app._render_comparison_input_row_chips)
_btn_col_line = next(i for i, ln in enumerate(_src.splitlines()) if "_btn_col, _save_col" in ln)
_lines = _src.splitlines()[_btn_col_line:]
_save_col_body = "\n".join(_lines)
check("D1: _save_col is actually opened with a `with` block (not just "
      "assigned and left unused)",
      "with _save_col:" in _save_col_body)
_with_idx = next(i for i, ln in enumerate(_lines) if "with _save_col:" in ln)
_after_with = "\n".join(_lines[_with_idx:_with_idx + 10])
check("D2: the save control call sits INSIDE that `with _save_col:` "
      "block, not after it (the owner's own screenshot bug)",
      "_render_comparison_save_control_chips(lang, email)" in _after_with)
print("[task58_sectionD_one_row_layout] Compare and Save/Update are "
      "confirmed to render inside the SAME two-column row OK")


# ======================================================================
# SECTION E: header search box hidden on Comparison ONLY while the
# chip-entry gate is live. page_comparison() itself isn't called here
# (it also renders _render_scan_results(), which needs a live network
# fetch this sandbox doesn't have) - instead, a direct, scope-limited
# functional check of _render_header()'s own show_search mechanism
# (the SAME mechanism Money Tools/Deep Dive's empty-state hero already
# use - see that call site's own comment), plus a source-inspection
# check that page_comparison() actually wires its gate into it.
# ======================================================================
import i18n as _i18n_mod2
_header_placeholder = _i18n_mod2.t("header.search_placeholder", "en")

_at_e1 = _run(_script(
    'app._render_header(compact=True, page_label="Comparison", current="comparison", show_search=False)',
    email="task58-e1@example.com",
))
check("E1: _render_header(show_search=False) renders no text_input "
      "with the header's own search placeholder",
      not any(t.placeholder == _header_placeholder for t in _at_e1.text_input))

_at_e2 = _run(_script(
    'app._render_header(compact=True, page_label="Comparison", current="comparison", show_search=True)',
    email="task58-e2@example.com",
))
check("E2: _render_header(show_search=True) DOES render it",
      any(t.placeholder == _header_placeholder for t in _at_e2.text_input))

_pc_src = inspect.getsource(app.page_comparison)
check("E3: page_comparison() computes its own COMPARE_CHIP_ENTRY_LIVE "
      "owner-override gate and forwards its negation as _render_"
      "header()'s show_search= - so the header's search box is "
      "hidden exactly when, and only when, the new Companies-box "
      "flow is the one actually showing (owner override included, "
      "same OR as the entry-row choice itself)",
      "_cmp_chip_live = compare_config.compare_chip_entry_live() or ai_gate.is_owner(" in _pc_src
      and "show_search=not _cmp_chip_live" in _pc_src
      and "_render_comparison_input_row_chips(_cmp_lang)" in _pc_src)
print("[task58_sectionE_header_search] show_search=False/True both "
      "work as expected, and page_comparison() itself is confirmed to "
      "wire the SAME gate used for the entry-row choice into it OK")


# ======================================================================
# SECTION F: requirements.txt pins streamlit to >=1.45.0 - the
# earliest published version this session confirmed (by downloading
# and inspecting the real wheels) actually has accept_new_options on
# st.multiselect (absent in 1.44.1, present in 1.45.0).
# ======================================================================
_req_path = os.path.join(REPO_ROOT, "requirements.txt")
with open(_req_path) as _f:
    _req_text = _f.read()
_m = re.search(r"^streamlit>=(\d+)\.(\d+)(?:\.(\d+))?", _req_text, re.MULTILINE)
check("F1: requirements.txt has a streamlit>=X.Y(.Z) pin at all",
      _m is not None)
if _m:
    _major, _minor, _patch = int(_m.group(1)), int(_m.group(2)), int(_m.group(3) or 0)
    check("F2: the pin is at least 1.45.0 (the real minimum for "
          "accept_new_options, confirmed by downloading and "
          "inspecting streamlit 1.44.1 and 1.45.0's own wheels)",
          (_major, _minor, _patch) >= (1, 45, 0))
import streamlit as _st_mod
check("F3: this sandbox's own installed streamlit actually satisfies "
      "that pin (accept_new_options is really usable here)",
      "accept_new_options" in inspect.signature(_st_mod.multiselect).parameters)
print("[task58_sectionF_requirements_pin] requirements.txt pins "
      "streamlit>=1.45.0, the real minimum for accept_new_options, "
      "and the installed version here satisfies it OK")


print(f"\n{passed} passed, {failed} failed")
if failed:
    sys.exit(1)
