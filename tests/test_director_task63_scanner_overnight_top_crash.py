"""
TASK 63 (Director-directed, 15 Nov 2026) - Scanner crash for signed-out
visitors with the sign-in wall on.

BUG (owner screenshot, Director verified in app.py at cc1b2b5):
page_scanner() set `_overnight_top` only in the else-branch of the
TASK 60 wall check (scanner_universe_is_public()/_signin_wall_blocked()).
When a signed-out visitor's universe is blocked (the default "ASX 200"
while the wall is live), the if-branch renders the sign-in prompt and
`_overnight_top` was never assigned at all - the "Change universe"
expander's own `expanded=not bool(_overnight_top)` a few lines down
then raised UnboundLocalError.

FIX (app.py, page_scanner()):
  1. `_overnight_top = None` initialised BEFORE the wall check, so both
     branches always leave it a real (possibly None/falsy) value.
  2. While the wall is live and the visitor is signed out (not the
     owner), the FIRST-EVER load (st.session_state.setdefault() itself
     only ever fires once, before anything - incl. a ?universe= deep
     link - has had a chance to set a value) now defaults scanner_
     universe to "S&P 500" (signin_wall.PUBLIC_SCANNER_UNIVERSE) - the
     one universe that stays public - instead of "ASX 200", so a
     signed-out visitor's very first screen is a universe they can
     actually see. Picking any OTHER universe (via the picker, a deep
     link, or the "Change universe" expander) still shows the prompt,
     with no exception either way, thanks to fix 1. Signed-in visitors,
     the owner, and every visitor while the wall is off all keep
     today's ASX 200 default, unchanged.
  3. Audited every other TASK 60/60b sign-in-wall gate in app.py,
     server.py, api_v1.py and mcp_server.py for the same "variable set
     only inside the allowed branch, read outside it" shape - every
     other site uses an early `return` (app.py's other nine
     _signin_wall_blocked() call sites), a `raise` (api_v1.py's
     _require_signin_or_share(), mcp_server.py's _require_signin()), a
     loop `continue`, or a redirect (server.py's universe_snapshot_
     page()/research_snapshot_page()) - none of those shapes can ever
     leave a later statement reading an unassigned local, so
     page_scanner()'s own _overnight_top was the only real instance of
     this bug.

Run: python3 tests/test_director_task63_scanner_overnight_top_crash.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task63_scanner_crash_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("SIGNIN_WALL_LIVE", None)

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

scan_store.save_scan("S&P 500", [
    {"Ticker": "WMSFAKE", "Type": "STOCK", "Company Name": "WMS Fake Co", "Price": 100.0,
     "Intrinsic Value": 150.0, "MOS %": 49.0, "Long Score": 55.5, "Quality": 61.0,
     "Psychology": 13.0, "Discovery (lite)": 40.0, "Moat": None,
     "Growth Used": 0.05, "FCF Source": "reported", "Dividend Yield %": 1.0},
], source_label="test fixture")
scan_store.save_scan("ASX 200", [
    {"Ticker": "AUFAKE", "Type": "STOCK", "Company Name": "AU Fake Co", "Price": 20.0,
     "Intrinsic Value": 25.0, "MOS %": 20.0, "Long Score": 58.0, "Quality": 64.0,
     "Psychology": 6.0, "Discovery (lite)": 25.0, "Moat": None,
     "Growth Used": 0.04, "FCF Source": "reported", "Dividend Yield %": 2.0},
], source_label="test fixture")

from streamlit.testing.v1 import AppTest
import ai_gate
import scanner_engine
import app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER_EMAIL = ai_gate.owner_email()


def _build(email, is_logged_in, wall_on, qp_universe=None):
    """A fresh AppTest instance for page_scanner(), NOT yet run - every
    call gets a brand-new Streamlit session (no prior session_state),
    exactly the "first-ever visit" shape the setdefault() default fix
    depends on. Same env/mock shape as test_director_task60_signin_
    wall.py's own _blocked_script() and test_task1_scanner_private_
    universe_deeplink.py's own _build()."""
    _env = 'os.environ["SIGNIN_WALL_LIVE"] = "1"' if wall_on else \
           'os.environ.pop("SIGNIN_WALL_LIVE", None)'
    _qp_line = (
        f'st.query_params["universe"] = {qp_universe!r}' if qp_universe else ""
    )
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ["SCHEDULER_ENABLED"] = "false"
{_env}
import streamlit as st
import paywall_engine
paywall_engine.current_user_email = lambda: {email!r}
paywall_engine.is_logged_in = lambda: {is_logged_in!r}
import app
{_qp_line}
app.page_scanner()
"""
    return AppTest.from_string(script, default_timeout=60)


def _run(at):
    """Same get_universe_pool() mock test_task1_scanner_private_
    universe_deeplink.py's own _run() already uses - only ever reached
    inside the "Change universe" expander's sector-heat decoration,
    nothing this task touches, and its real implementation would try a
    live scrape every run otherwise."""
    with mock.patch.object(scanner_engine, "get_universe_pool",
                            return_value=(None, "test fixture - no live fetch")):
        at.run()
    return at


# ======================================================================
# CHECK 1 (the real crash, reproduced): wall ON, signed out, FIRST load
# (no ?universe= deep link, fresh session) -> no exception, and the
# page actually lands on S&P 500 (the public universe), not the
# previously-defaulted ASX 200 (which would have been wall-blocked).
# ======================================================================
_at1 = _build(None, False, wall_on=True)
_run(_at1)
check("[1a] wall ON, signed out, first load -> NO exception (the real "
      "UnboundLocalError this task fixes)", not _at1.exception)
check("[1b] ...and scanner_universe defaulted to S&P 500 (the public "
      "universe), not ASX 200",
      _at1.session_state.get("scanner_universe") == "S&P 500")
_at1_text = " ".join(m.value or "" for m in _at1.get("markdown"))
check("[1c] the S&P 500 overnight table actually rendered (ticker "
      "WMSFAKE visible) - the signed-out visitor's first screen is "
      "real content, not the sign-in prompt",
      "WMSFAKE" in _at1_text)
check("[1d] the sign-in-wall prompt copy does NOT appear on this first "
      "load", "Sign in to see this." not in _at1_text)


# ======================================================================
# CHECK 2 (the real crash, reproduced via an explicit pick): wall ON,
# signed out, deep-links to ASX 200 (a wall-blocked universe) -> no
# exception, sign-in prompt shown instead of the table.
# ======================================================================
_at2 = _build(None, False, wall_on=True, qp_universe="ASX 200")
_run(_at2)
check("[2a] wall ON, signed out, picks ASX 200 -> NO exception", not _at2.exception)
check("[2b] scanner_universe is ASX 200 (the pick was honoured)",
      _at2.session_state.get("scanner_universe") == "ASX 200")
_at2_text = " ".join(m.value or "" for m in _at2.get("markdown"))
check("[2c] the sign-in-wall prompt renders instead of the ASX 200 table",
      "Sign in to see this." in _at2_text)
check("[2d] the ASX 200 overnight table itself does NOT render",
      "AUFAKE" not in _at2_text)


# ======================================================================
# CHECK 3: wall OFF, signed out -> unchanged (today's ASX 200 default,
# no exception, no sign-in prompt - the wall never applies at all).
# ======================================================================
_at3 = _build(None, False, wall_on=False)
_run(_at3)
check("[3a] wall OFF, signed out -> no exception", not _at3.exception)
check("[3b] scanner_universe still defaults to ASX 200 (unchanged)",
      _at3.session_state.get("scanner_universe") == "ASX 200")
_at3_text = " ".join(m.value or "" for m in _at3.get("markdown"))
check("[3c] the ASX 200 overnight table renders (AUFAKE visible), "
      "wall OFF behaves exactly as before this task",
      "AUFAKE" in _at3_text)
check("[3d] no sign-in prompt at all with the wall off",
      "Sign in to see this." not in _at3_text)


# ======================================================================
# CHECK 4: wall ON, SIGNED IN (non-owner) -> unchanged (ASX 200
# default, real table, no prompt - a signed-in visitor sees everything
# the wall itself permits without the S&P 500-only carve-out).
# ======================================================================
_at4 = _build("someone@example.com", True, wall_on=True)
_run(_at4)
check("[4a] wall ON, signed in -> no exception", not _at4.exception)
check("[4b] scanner_universe still defaults to ASX 200 for a signed-in "
      "visitor (unchanged)", _at4.session_state.get("scanner_universe") == "ASX 200")
_at4_text = " ".join(m.value or "" for m in _at4.get("markdown"))
check("[4c] the ASX 200 overnight table renders for a signed-in visitor",
      "AUFAKE" in _at4_text)


# ======================================================================
# CHECK 5: wall ON, the OWNER -> unchanged (ASX 200 default, real
# table, no prompt - the owner always sees the real, current site).
# ======================================================================
_at5 = _build(OWNER_EMAIL, False, wall_on=True)
_run(_at5)
check("[5a] wall ON, owner -> no exception", not _at5.exception)
check("[5b] scanner_universe still defaults to ASX 200 for the owner "
      "(unchanged)", _at5.session_state.get("scanner_universe") == "ASX 200")
_at5_text = " ".join(m.value or "" for m in _at5.get("markdown"))
check("[5c] the ASX 200 overnight table renders for the owner",
      "AUFAKE" in _at5_text)


# ======================================================================
# CHECK 6: source-level confirmation of fix 1 - _overnight_top is
# initialised before the wall-check branch, so a later read of it can
# never raise UnboundLocalError regardless of which branch ran.
# ======================================================================
import inspect

_src = inspect.getsource(app.page_scanner)
_init_idx = _src.find("_overnight_top = None")
_branch_idx = _src.find('_signin_wall_blocked(\n            "scanner", _top_universe)')
check("[6] '_overnight_top = None' appears in page_scanner()'s source "
      "BEFORE the wall-check branch that used to leave it unassigned",
      _init_idx != -1 and _branch_idx != -1 and _init_idx < _branch_idx)


print(f"\nTASK 63 RESULTS: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("\nALL TASK 63 (SCANNER OVERNIGHT-TOP CRASH) FIXTURES PASSED")
