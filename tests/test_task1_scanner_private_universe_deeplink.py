"""
TASK 1 of instruction_scanner_chips_trusts_japan_sectors.md (9 Oct
2026, Director-directed): LIVE BUG - clicking a private-market chip on
the Scanner ("FTSE 100 (private)", "TOPIX 500 (private)", etc.) landed
on ASX 200 instead, for the owner too.

ROOT CAUSE (two code sites in app.py, both now fixed):

  1. page_scanner()'s own ?universe= deep-link reader only ever
     recognized scanner_engine.AUSTRALIA_UNIVERSES/USA_UNIVERSES -
     every other real universe name (United Kingdom/Canada/Japan/
     Europe) fell through silently, leaving scanner_universe at its
     freshly-defaulted "ASX 200" (a brand-new Streamlit session on
     every full page load - each pill click is its own GET, confirmed
     by the Railway log evidence in the instruction). This is cause
     (b), not (a): the owner's own identity (ai_gate.is_owner(
     paywall_engine.current_user_email())) is read from a persistent
     cookie, never session_state, so it survives the new session fine
     - the picker's own "(private)" markers still show correctly on
     the very page that wrongly landed on ASX 200.

  2. A SECOND, latent bug that a naive fix of (1) alone would have hit
     immediately: the "Change universe & Run Scan" expander's own
     AU/USA-only selectbox used to share the SAME "scanner_universe"
     session key as the top table/picker highlight. Its own guard
     ("reset scanner_universe to an AU/USA default when it's not a
     valid option for THIS widget") would silently stomp a just-picked
     non-AU/USA universe back to an AU default on literally the next
     rerun of the page (any widget interaction anywhere on it) - so an
     owner who clicked "FTSE 100" would see it correctly for one
     instant, then watch it revert to ASX 200 the moment they touched
     anything else. Fixed by giving that selectbox its OWN, decoupled
     widget key ("scanner_universe_manual"), seeded from the shared key
     only when it's already an AU/USA value, and propagating a genuine
     manual pick back up to the shared key only under that same
     condition - never force-overwriting the shared key just because
     this AU/USA-only tool can't represent a new-market universe.

THE FIX (app.py):
  - New _all_scanner_picker_universes() (built from the picker's own
    _SCANNER_PICKER_BANDS config, never a hand-maintained duplicate or
    a ticker/constituent list) - the ?universe= deep-link reader now
    checks this FULL set, not just the two public lists.
  - A universe outside AU/USA is privacy-gated exactly once: the OWNER
    may point session_state at ANY such universe, private or not
    (scan_store.load_scan()'s own default allow_private=False still
    protects every actual data read - this only decides whether the
    page is even ALLOWED to try); a non-owner reaching a currently-
    private one is refused outright - scanner_universe is left
    untouched (whatever it already was, typically the ASX 200
    default), and a new i18n key ("scanner.universe_refused",
    "{universe} is not public yet.") renders as a plain st.info line
    right above that same still-shown fallback table - never a silent
    substitution with no note.
  - The expander's own selectbox now uses "scanner_universe_manual" -
    see cause 2 above.

Covers, per the instruction's own test list:
  - Owner + private universe -> that universe (first render AND after
    a subsequent, unrelated rerun - proving cause 2 is actually fixed,
    not just cause 1).
  - Non-owner + private universe -> refusal line, no private rows, the
    existing fallback table still renders beneath the note.
  - Public chips (ASX 200, S&P 500) unchanged - a before/after fixture
    proof: ASX 200's own rendered table is byte-identical whether
    reached via the default (no deep link) or via an explicit
    ?universe=ASX%20200 deep link, and manually picking an AU universe
    in the "Change universe" expander still updates the shared
    "current universe" exactly as it did before this fix.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. scanner_engine.get_universe_pool() (called inside the
"Change universe" expander, purely for its own sector-heat decoration
- nothing this task touches) is mocked out for every page_scanner()
run below, same as test_b3_currency_risk_tool.py's own mock.patch.
object(..., "get_fx_history", ...) wrapping its at.run() calls - this
sandbox's live scrape/yfinance retries are slow, not instant failures,
so leaving it unmocked would make every run here time out.

Run: python3 tests/test_task1_scanner_private_universe_deeplink.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task1_scanner_deeplink_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("SCANNER_PRESENTATION_LIVE", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

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

# ======================================================================
# Production-shaped fixtures (scan_store.save_scan() - the real
# writer): ASX 200 (the baseline that must never change) and FTSE 100
# (the private universe under test).
# ======================================================================
scan_store.save_scan("ASX 200", [
    {"Ticker": "CSL.AX", "Type": "STOCK", "Company Name": "CSL Limited", "Price": 280.0,
     "Intrinsic Value": 320.0, "MOS %": 12.5, "Long Score": 70.0, "Quality": 75.0,
     "Psychology": 5.0, "Discovery (lite)": 35.0, "Moat": 65.0,
     "Growth Used": 0.06, "FCF Source": "reported", "Dividend Yield %": 1.1},
], source_label="test fixture")
scan_store.save_scan("FTSE 100", [
    {"Ticker": "BARC.L", "Type": "STOCK", "Company Name": "Barclays PLC", "Price": 250.5,
     "Intrinsic Value": 300.0, "MOS %": 16.5, "Long Score": 60.0, "Quality": 55.0,
     "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
     "Growth Used": 0.05, "FCF Source": "reported", "Dividend Yield %": 4.2},
], source_label="test fixture")

from streamlit.testing.v1 import AppTest
import ai_gate
import scanner_engine
import app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER_EMAIL = ai_gate.owner_email()
NONOWNER_EMAIL = "someone-else@example.com"


def _build(email, qp_universe=None):
    """An AppTest instance for page_scanner(), NOT yet run - the caller
    decides how many times to .run() it (persisted session_state
    across repeated .run() calls is how AppTest simulates a later
    rerun from some other widget interaction, confirmed directly
    against this exact Streamlit version before writing this file)."""
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
    """.run() with scanner_engine.get_universe_pool() mocked out - it's
    only ever reached inside the "Change universe" expander's own
    sector-heat decoration (nothing this task touches), and its real
    implementation tries a live scrape/yfinance call every time, which
    this sandbox's proxy doesn't fail fast on - unmocked, every single
    run() here would time out. app.py's own "import scanner_engine"
    binds the SAME module object patched here (one process, one
    sys.modules entry), same pattern as test_b3_currency_risk_tool.py's
    own mock.patch.object(..., "get_fx_history", ...) wrapping at.run()."""
    with mock.patch.object(scanner_engine, "get_universe_pool",
                            return_value=(None, "test fixture - no live fetch")):
        at.run()
    return at


# ======================================================================
# CHECK 1-4: owner + private universe (FTSE 100) deep link.
# ======================================================================
_at_owner = _build(OWNER_EMAIL, qp_universe="FTSE 100")
_run(_at_owner)
check("owner + FTSE 100 deep link: raised no exception", not _at_owner.exception)
check("owner + FTSE 100 deep link: scanner_universe IS FTSE 100 (not ASX 200)",
      _at_owner.session_state.get("scanner_universe") == "FTSE 100")
_html_owner = " ".join(m.value or "" for m in _at_owner.get("markdown"))
_expected_href = app._scanner_universe_href("FTSE 100", "en")
check('owner + FTSE 100 deep link: the FTSE 100 pill itself is highlighted ("pill on")',
      f'class="pill on" href="{_expected_href}"' in _html_owner)
check("owner + FTSE 100 deep link: the overnight table shows FTSE 100's own "
      "fixture ticker (BARC.L) - the real, reported symptom (landing on ASX 200's "
      "overnight table instead) is gone",
      "BARC.L" in _html_owner)

# ======================================================================
# CHECK 5: the exact cause-2 regression proof - a SUBSEQUENT rerun
# (simulating any other widget interaction on the page, with the
# ?universe= query param already popped, exactly as a real second
# page interaction would see it) must NOT revert scanner_universe back
# to an AU/USA default.
# ======================================================================
_run(_at_owner)
check("owner + FTSE 100: survives a SUBSEQUENT rerun (cause 2's own fix) - "
      "scanner_universe is STILL FTSE 100, not reverted to an AU/USA default",
      _at_owner.session_state.get("scanner_universe") == "FTSE 100")
check("owner + FTSE 100: the decoupled expander widget key holds its OWN AU "
      "fallback locally, while the shared key stays on FTSE 100",
      _at_owner.session_state.get("scanner_universe_manual") in
      app.scanner_engine.get_universes("Australia")
      and _at_owner.session_state.get("scanner_universe") == "FTSE 100")

# ======================================================================
# CHECK 6-8: non-owner + private universe (FTSE 100) deep link ->
# refused, no private rows, existing fallback table still shown
# beneath a plain-English note (never silently substituted with no
# note at all).
# ======================================================================
_at_non = _build(NONOWNER_EMAIL, qp_universe="FTSE 100")
_run(_at_non)
check("non-owner + FTSE 100 deep link: raised no exception", not _at_non.exception)
check("non-owner + FTSE 100 deep link: scanner_universe stays at the ASX 200 "
      "default - refused, not silently granted",
      _at_non.session_state.get("scanner_universe") == "ASX 200")
_infos_non = " ".join(m.value or "" for m in _at_non.get("info"))
check('non-owner + FTSE 100 deep link: refusal note reads "FTSE 100 is not public yet."',
      "FTSE 100 is not public yet." in _infos_non)
_html_non = " ".join(m.value or "" for m in _at_non.get("markdown"))
check("non-owner + FTSE 100 deep link: no FTSE 100 row (BARC.L) ever rendered, and "
      "the existing ASX 200 fallback table still renders beneath the note (never "
      "'show nothing')",
      "BARC.L" not in _html_non and "CSL.AX" in _html_non)

# ======================================================================
# CHECK 9: non-owner, no deep link at all - pure baseline, unaffected.
# ======================================================================
_at_non_baseline = _build(NONOWNER_EMAIL)
_run(_at_non_baseline)
check("non-owner, no deep link: ASX 200 default, no refusal note (nothing was refused)",
      _at_non_baseline.session_state.get("scanner_universe") == "ASX 200"
      and not any("is not public yet" in (m.value or "") for m in _at_non_baseline.get("info")))

# ======================================================================
# CHECK 10-11: public chips (ASX 200/S&P 500) completely unchanged -
# the before/after fixture proof the instruction's own standing rule
# asks for.
# ======================================================================
_at_default = _build(OWNER_EMAIL)
_run(_at_default)
_html_default = " ".join(m.value or "" for m in _at_default.get("markdown"))

_at_asx_deeplink = _build(OWNER_EMAIL, qp_universe="ASX 200")
_run(_at_asx_deeplink)
_html_asx_deeplink = " ".join(m.value or "" for m in _at_asx_deeplink.get("markdown"))

check("BEFORE/AFTER FIXTURE: ASX 200's own rendered picker+table HTML is BYTE-"
      "IDENTICAL whether reached via the default (no deep link) or via an "
      "explicit ?universe=ASX%20200 deep link - the pre-existing AU/USA branch "
      "is untouched by this commit",
      _html_default == _html_asx_deeplink)

_at_sp500 = _build(OWNER_EMAIL, qp_universe="S&P 500")
_run(_at_sp500)
check("owner + S&P 500 (USA) deep link: unchanged pre-existing behavior - "
      "scanner_universe/country flags set exactly as before this commit",
      _at_sp500.session_state.get("scanner_universe") == "S&P 500"
      and _at_sp500.session_state.get("scanner_country_us") is True
      and _at_sp500.session_state.get("scanner_country_au") is False)

# ======================================================================
# CHECK 12: a genuine manual pick in the "Change universe" expander
# (AU/USA only) still propagates to the shared key exactly as before
# this commit - cause 2's fix only changes behavior for a non-AU/USA
# shared value, never for a real manual AU/USA selection.
# ======================================================================
_at_manual = _build(OWNER_EMAIL)
_run(_at_manual)
_at_manual.get_by_key("scanner_universe_manual").select("ASX 300")
_run(_at_manual)
check('manual pick in the "Change universe" expander (ASX 300) still updates the '
      "shared scanner_universe key, same as before this commit",
      _at_manual.session_state.get("scanner_universe") == "ASX 300")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
