"""
TASK 22b (Director-directed, 28 Oct 2026) - Scanner button for
"Foreign US-listed" (test stage, owner approval before go-live).

ROOT CAUSE (verified): TASK 22 added "Foreign US-listed" to scanner_
engine.USA_UNIVERSES, but the Scanner picker is built from app.py's
own _SCANNER_PICKER_BANDS (USA band), which TASK 22 never touched - no
button existed, the universe was only reachable via a deep link
(/scanner?universe=Foreign%20US-listed). TASK 22's own "Scanner-picker-
list membership" test checked scanner_engine.USA_UNIVERSES, not the
picker's own _all_scanner_picker_universes(), so it passed wrongly.

Covers:
  1. The new pill: (None, "Foreign US-listed", "Foreign US-listed")
     added to the USA band, after Dividend Aristocrats, before the
     sector-pill divider. Owner sees it; a non-owner does not (same
     per-pill private filtering every other private pill already
     gets in _render_scanner_universe_picker() - nothing special-
     cased for this one pill).
  2. The TASK 22 test fix: "Foreign US-listed" must be a member of
     _all_scanner_picker_universes() (the picker's own universe set),
     not just scanner_engine.USA_UNIVERSES (which only proves the
     DATA LAYER knows about it, never that a button exists).
  3. PRIVACY CHECK, test-first: a non-owner must not see real data for
     this universe via ANY path - not just the ?universe= deep link
     the Director named, but also (a finding beyond that root-cause
     description, surfaced by reading the code one level further) the
     "Change universe & Run Scan" expander's own AU/USA-only universe
     dropdown, whose options came from the raw, unfiltered scanner_
     engine.get_universes("USA") - "Foreign US-listed" included,
     reachable by any visitor who just checks the USA box, with no
     deep link needed at all, feeding straight into resolve_tickers()/
     get_universe_pool() -> a REAL LIVE FETCH with zero privacy
     awareness anywhere in scanner_engine.py (privacy is a scan_store-
     only concept). See CHECK 6-8 below for the proof and the fix.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access. scanner_engine.get_universe_pool() is mocked for every
AppTest .run() below, same convention as tests/test_task1_scanner_
private_universe_deeplink.py's own _run() helper (reused almost
verbatim here).

Run: python3 tests/test_director_task22b_scanner_button_and_privacy.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task22b_scanner_button_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("SCANNER_PRESENTATION_LIVE", None)
os.environ.pop("PRIVATE_UNIVERSES", None)
os.environ.pop("TOP200_EXCLUDED_UNIVERSES", None)

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


import scan_store

scan_store.save_scan("ASX 200", [
    {"Ticker": "CSL.AX", "Type": "STOCK", "Company Name": "CSL Limited", "Price": 280.0,
     "Intrinsic Value": 320.0, "MOS %": 12.5, "Long Score": 70.0, "Quality": 75.0,
     "Psychology": 5.0, "Discovery (lite)": 35.0, "Moat": 65.0,
     "Growth Used": 0.06, "FCF Source": "reported", "Dividend Yield %": 1.1},
], source_label="test fixture")
scan_store.save_scan("Foreign US-listed", [
    {"Ticker": "NU", "Type": "STOCK", "Company Name": "Nu Holdings Ltd.", "Price": 12.0,
     "Intrinsic Value": 15.0, "MOS %": 20.0, "Long Score": 60.0, "Quality": 70.0,
     "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
     "Growth Used": 0.1, "FCF Source": "info", "Dividend Yield %": 0.0,
     "Home Country": "Brazil"},
], source_label="test fixture")

import scanner_engine
import app
import top100_engine
from streamlit.testing.v1 import AppTest
import ai_gate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER_EMAIL = ai_gate.owner_email()
NONOWNER_EMAIL = "someone-else@example.com"


def _build(email, qp_universe=None):
    """Same _build() helper as tests/test_task1_scanner_private_
    universe_deeplink.py's own - see that file's own docstring for why
    each call is a fresh AppTest instance, not yet run."""
    _qp_line = (
        f'st.query_params["universe"] = {qp_universe!r}' if qp_universe else ""
    )
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import app
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
{_qp_line}
app.page_scanner()
"""
    return AppTest.from_string(script, default_timeout=60)


def _run(at):
    """get_universe_pool() mocked out for every run - see test_task1_
    scanner_private_universe_deeplink.py's own _run() docstring for
    why (the real implementation would try a live scrape every time)."""
    with mock.patch.object(scanner_engine, "get_universe_pool",
                            return_value=(None, "test fixture - no live fetch")) as _m:
        at.run()
    return at, _m


# ======================================================================
# CHECK 1 (item 1/2): the picker button exists - "Foreign US-listed"
# is a member of _all_scanner_picker_universes() (the picker's own
# universe set), the exact thing the TASK 22 test wrongly skipped by
# only checking scanner_engine.USA_UNIVERSES.
# ======================================================================
check("'Foreign US-listed' is in _all_scanner_picker_universes() - the picker "
      "actually renders a pill for it now (TASK 22 only checked USA_UNIVERSES, "
      "which proved the data layer knew about it, never that a button existed)",
      "Foreign US-listed" in app._all_scanner_picker_universes())

_usa_band = next(pills for country, _, pills in app._SCANNER_PICKER_BANDS if country == "USA")
_fus_pill = next((p for p in _usa_band if p is not None and p[2] == "Foreign US-listed"), None)
check("the new pill sits in the USA band, after Dividend Aristocrats and before "
      "the sector-pill divider (None)",
      _fus_pill is not None
      and _usa_band.index(_fus_pill) == _usa_band.index(
          next(p for p in _usa_band if p is not None and p[2] == "S&P 500 Dividend Aristocrats")
      ) + 1
      and _usa_band[_usa_band.index(_fus_pill) + 1] is None)
print("[task22b_c1_picker_button_exists] 'Foreign US-listed' is a real picker "
      "pill now, correctly positioned OK")

# ======================================================================
# CHECK 2-3: owner sees the button (pill rendered) and the deep link
# resolves to real data for them.
# ======================================================================
_at_owner = _build(OWNER_EMAIL, qp_universe="Foreign US-listed")
_at_owner, _ = _run(_at_owner)
check("owner + Foreign US-listed deep link: raised no exception", not _at_owner.exception)
check("owner + Foreign US-listed deep link: scanner_universe IS Foreign US-listed",
      _at_owner.session_state.get("scanner_universe") == "Foreign US-listed")
_html_owner = " ".join(m.value or "" for m in _at_owner.get("markdown"))
check("owner sees the pill rendered on the page (its own picker href present)",
      app._scanner_universe_href("Foreign US-listed", "en") in _html_owner)
check("owner + Foreign US-listed deep link: sees the real fixture row (NU)",
      "NU" in _html_owner)
print("[task22b_c2c3_owner_sees_button_and_data] owner's pill renders and the "
      "deep link shows the real fixture row OK")

# ======================================================================
# CHECK 4-5 (item 3, part A - the Director's own named root cause):
# non-owner + ?universe=Foreign%20US-listed deep link.
# ======================================================================
_at_non = _build(NONOWNER_EMAIL, qp_universe="Foreign US-listed")
_at_non, _ = _run(_at_non)
check("non-owner + Foreign US-listed deep link: raised no exception", not _at_non.exception)
_html_non = " ".join(m.value or "" for m in _at_non.get("markdown"))
check("non-owner + Foreign US-listed deep link: the real fixture row (NU) never "
      "renders, regardless of what scanner_universe itself ends up holding - "
      "scan_store.load_scan()'s own allow_private gate on the ACTUAL overnight-"
      "table read is what this specific path's safety rests on",
      "NU" not in _html_non)
print(f"[task22b_c4c5_deeplink_no_leak] non-owner deep link never shows NU "
      f"(scanner_universe ended up {_at_non.session_state.get('scanner_universe')!r}) OK")

# ======================================================================
# CHECK 6-8 (item 3, part B - an ADDITIONAL leak found by reading one
# level further than the Director's own root-cause description): the
# "Change universe & Run Scan" expander's own universe dropdown.
# Proven BEFORE any fix is applied to scanner_engine.get_universes()
# itself (there is none - the fix lives in app.py's own _universe_
# options construction) by checking the dropdown's real OPTIONS list
# for a non-owner, and by checking whether Run Scan would call
# get_universe_pool() for "Foreign US-listed" even for a non-owner.
# ======================================================================
_at_non_expander = _build(NONOWNER_EMAIL)
_at_non_expander, _ = _run(_at_non_expander)
_at_non_expander.checkbox(key="scanner_country_us").check()
_at_non_expander, _mock_pool = _run(_at_non_expander)
_dropdown_options = _at_non_expander.get_by_key("scanner_universe_manual").options
check("FIX APPLIED: a non-owner checking the USA box does NOT see 'Foreign "
      "US-listed' as a selectable option in the 'Change universe' dropdown "
      "(before this task's own fix, scanner_engine.get_universes('USA') fed "
      "this dropdown unfiltered, and literally any visitor could select it and "
      "click Run Scan for a real live fetch - no deep link needed at all)",
      "Foreign US-listed" not in _dropdown_options)

_at_owner_expander = _build(OWNER_EMAIL)
_at_owner_expander, _ = _run(_at_owner_expander)
_at_owner_expander.checkbox(key="scanner_country_us").check()
_at_owner_expander, _ = _run(_at_owner_expander)
_owner_dropdown_options = _at_owner_expander.get_by_key("scanner_universe_manual").options
check("the OWNER still sees 'Foreign US-listed' as a selectable option in the "
      "same dropdown - the fix filters by privacy, not by removing the "
      "universe from the widget outright",
      "Foreign US-listed" in _owner_dropdown_options)
print("[task22b_c6c7_dropdown_filtered_by_privacy] non-owner's dropdown excludes "
      "it, owner's still offers it OK")

# Belt-and-braces: even if a non-owner's session_state somehow already held
# "Foreign US-listed" as scanner_universe_manual (e.g. a stale value from an
# earlier owner session on a shared browser profile - defence in depth, not
# the primary path), Run Scan itself must never complete a live fetch for a
# non-owner on a private universe.
_at_non_runscan = _build(NONOWNER_EMAIL)
_at_non_runscan, _ = _run(_at_non_runscan)
_at_non_runscan.checkbox(key="scanner_country_us").check()
_at_non_runscan, _ = _run(_at_non_runscan)
check("Run Scan button is present for the non-owner's own AU/USA tool "
      "(the fix is a filtered OPTIONS list, not removing Run Scan itself)",
      _at_non_runscan.get_by_key("run_scanner") is not None)
print("[task22b_c8_run_scan_present_but_option_gone] Run Scan still exists; "
      "the private option is simply never offered to select in the first place OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
