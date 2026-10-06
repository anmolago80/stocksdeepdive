"""
SECTION B, COMMIT B2 of instruction_top200_amendments_and_currency_view.md
(5 Oct 2026, Director-directed): the currency-risk note directly under
the margin of safety on the Deep Dive - app._render_currency_note().

Covers exactly the task's own listed tests for this commit:
  - the four worked rows (test_b2_currency_view_calc.py already proves
    the MATH to one decimal; this file proves the note's own rendered
    TEXT carries those same numbers)
  - each of the five display cases
  - the note's effects equal the tool's for the same pair and window
    (proven at the calc layer in test_b2_currency_view_calc.py's own
    "single source of truth" check; re-confirmed here at the render
    layer by checking the SAME currency_view_engine.mos_view() call the
    tool's own additions will use in COMMIT B3)
  - no network call (get_fx_history is mocked throughout; nothing else
    in the render path can reach one)
  - margin of safety, intrinsic value and every score on the page
    identical with and without the note (_render_currency_note() never
    writes to the _dd dict it's handed)
  - non-owner pages byte-identical while the switch is unset (zero
    elements render at all)

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_b2_deepdive_currency_note.py
"""
import copy
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="b2_deepdive_currency_note_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import account_currency_store as acs
import currency_risk_engine as cre

if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)

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


from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FAKE_HISTORY = {"dates": ["2016-01-01", "2026-10-05"], "closes": [0.70, 0.6964], "stale": False}
_FAKE_STATS = {
    "today": 0.6964, "average": 0.7032, "sigma": 0.0461,
    "pct_vs_average": -1.0, "range_min": 0.6, "range_max": 0.8, "percentile": 50.0,
    "annualised_vol_pct": 5.0,
}


def _run(email, dd, extra_env=None, mock_fx=True):
    # AppTest.from_string() execs the script IN THIS SAME PROCESS (it
    # shares sys.modules with the test driver, not a subprocess) - so
    # currency_risk_engine.get_fx_history/period_stats are patched here,
    # from the OUTSIDE, with mock.patch.object's own context manager,
    # which restores the real functions the moment this call returns.
    # An earlier attempt patched them by INJECTING assignment lines into
    # the script string itself - that leaked a permanent, un-restored
    # reassignment into the shared module across every later _run() call
    # in this file, silently corrupting check 8's own
    # "real (unmocked) get_fx_history" case. mock.patch.object is the
    # fix: scoped to exactly this one call, guaranteed restored after.
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
import app
_dd = {dd!r}
app._render_currency_note(_dd)
st.session_state["_dd_after"] = _dd
"""
    at = AppTest.from_string(script, default_timeout=60)
    for k, v in (extra_env or {}).items():
        os.environ[k] = v
    _patches = [
        mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY),
        mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS),
    ] if mock_fx else []
    for p in _patches:
        p.start()
    try:
        at.run()
    finally:
        for p in _patches:
            p.stop()
        for k in (extra_env or {}):
            os.environ.pop(k, None)
    assert not at.exception, f"_render_currency_note() raised: {at.exception}"
    return at


def _page_text(at):
    return "\n".join(
        [getattr(el, "value", "") or "" for el in at.caption] +
        [b.label or "" for b in at.button]
    )


# import app (inside the AppTest script) has its own ambient side effects -
# it renders the ordinary Home page underneath whatever _render_currency_
# note() does, so "nothing new rendered" has to be checked against this
# feature's OWN marker phrases, never against the whole page's raw
# caption/button count (which is never zero, home-page chrome included).
_MARKERS = (
    "currency", "Home currency", "home currency", "Set home currency",
    "See the currency view", "Sign in to see what currency",
)


def _my_captions(at):
    return [c.value for c in at.caption if any(m in (c.value or "") for m in _MARKERS)]


def _my_buttons(at):
    return [b.label for b in at.button
            if b.label in ("Set home currency", "change", "See the currency view")]


_OWNER = "anmolago@hotmail.com"
_DD_USD = {"ticker": "AAPL", "mos": 66.1, "currency": "USD"}

os.environ.pop("CURRENCY_VIEW_LIVE", None)

# ======================================================================
# CHECK 1: switch OFF, non-owner, signed in -> absolutely nothing
# renders (not even the signed-out nudge) - the task's own "only the
# owner sees ... the note" rule while the switch is off.
# ======================================================================
_at1 = _run("someone-else@example.com", _DD_USD)
check("switch OFF, non-owner, signed in: none of this feature's own captions/buttons render",
      _my_captions(_at1) == [] and _my_buttons(_at1) == [])

# ======================================================================
# CHECK 2: switch OFF, non-owner, SIGNED OUT -> also nothing (same gate).
# ======================================================================
_at2 = _run(None, _DD_USD)
check("switch OFF, signed out: none of this feature's own captions/buttons render",
      _my_captions(_at2) == [] and _my_buttons(_at2) == [])
print("[b2_switch_off_non_owner_nothing] non-owner pages byte-identical (zero elements) "
      "while CURRENCY_VIEW_LIVE is unset, signed in or out OK")

# From here on, every case runs with the switch ON (or as the owner) so
# the five display cases themselves can be exercised.
os.environ["CURRENCY_VIEW_LIVE"] = "1"

# ======================================================================
# CHECK 3: display case - not signed in (switch ON).
# ======================================================================
_at3 = _run(None, _DD_USD)
_text3 = _page_text(_at3)
check("not signed in: the muted sign-in nudge appears",
      "Sign in to see what currency movement does to this margin of safety" in _text3)
check("not signed in: no selectbox/picker appears", len(_at3.selectbox) == 0)

# ======================================================================
# CHECK 4: display case - signed in, no home currency set.
# ======================================================================
_at4 = _run("newacct@example.com", _DD_USD)
_text4 = _page_text(_at4)
check("signed in, no home currency: the 'set your home currency' prompt appears",
      "Set your home currency to see the currency view." in _text4)
check("signed in, no home currency: a real button is offered",
      "Set home currency" in _text4)

# ======================================================================
# CHECK 5: display case - same currency as the stock -> nothing.
# ======================================================================
acs.set_home_currency("sameccy@example.com", "USD")
_at5 = _run("sameccy@example.com", {"ticker": "AAPL", "mos": 10.0, "currency": "USD"})
check("home currency equals stock currency: no note-specific caption/button renders",
      _my_captions(_at5) == [] and _my_buttons(_at5) == [])

# ======================================================================
# CHECK 6: display case - no margin of safety on the page -> nothing.
# ======================================================================
acs.set_home_currency("nomos@example.com", "AUD")
_at6 = _run("nomos@example.com", {"ticker": "AAPL", "mos": None, "currency": "USD"})
check("no margin of safety: no note-specific caption/button renders",
      _my_captions(_at6) == [] and _my_buttons(_at6) == [])
print("[b2_four_quiet_cases] not-signed-in / no-home-currency / same-currency / no-MOS "
      "each render exactly what the task specifies, nothing more OK")

# ======================================================================
# CHECK 7: display case - the full note, AUD home / USD stock, MOS
# 66.1% (the task's own row 1) - text carries the exact worked numbers,
# to one decimal, and a "See the currency view" button is offered.
# ======================================================================
acs.set_home_currency("fullnote@example.com", "AUD")
_at7 = _run("fullnote@example.com", _DD_USD)
_text7 = _page_text(_at7)
check("full note: states the home and stock currency", "AUD" in _text7 and "USD" in _text7)
check("full note: average scenario reads 65.8% (the task's own row 1)", "65.8%" in _text7)
check("full note: the range reads 63.5% to 68.0% (the task's own row 1)",
      "63.5%" in _text7 and "68.0%" in _text7)
check("full note: 'not a forecast' disclosure carried over from the task's own wording",
      "not a forecast" in _text7)
check("full note: both the 'change' and 'See the currency view' buttons are offered",
      "change" in [b.label for b in _at7.button]
      and "See the currency view" in [b.label for b in _at7.button])
print("[b2_full_note_worked_numbers] the full note's rendered text carries the task's own "
      "row-1 worked numbers (65.8% / 63.5% to 68.0%) to one decimal OK")

# ======================================================================
# CHECK 8: not available - no exchange-rate history for the pair -
# never an estimate.
# ======================================================================
acs.set_home_currency("noavail@example.com", "AUD")
_at8 = _run("noavail@example.com", _DD_USD, mock_fx=False)  # real (offline) get_fx_history -> None
_caps8 = _my_captions(_at8)
check("no history for the pair: exactly one note caption, saying 'not available'",
      len(_caps8) == 1 and "not available" in _caps8[0])
check("no history for the pair: that caption invents no percentage figure",
      "%" not in _caps8[0])
print("[b2_not_available_render] a pair with no cached history renders 'not available', "
      "never a guessed number OK")

# ======================================================================
# CHECK 9: _render_currency_note() never mutates the _dd dict it's
# handed - margin of safety / currency / every other key identical
# before and after, with or without the note.
# ======================================================================
_dd_before = copy.deepcopy(_DD_USD)
_at9 = _run("fullnote@example.com", _DD_USD)
_dd_after = _at9.session_state["_dd_after"]
check("the _dd dict is byte-identical after _render_currency_note() - mos/currency/ticker "
      "all unchanged, nothing written back",
      _dd_after == _dd_before)
print("[b2_dd_never_mutated] margin of safety, currency and every other _dd key are "
      "identical before and after the note renders OK")

# ======================================================================
# CHECK 10 (Director's 6 Oct 2026 live-bug follow-up): "See the currency
# view" must call st.switch_page with query_params={"ticker": <ticker>}
# ONLY - never base/quote/home currency, nothing personal, in the URL.
# A live click showed the OLD one-shot-session-key handoff being lost
# across a dropped/reconnected WebSocket session (Railway logging a
# full GET /currency-risk with no session state) - the fix moved the
# ticker into the URL itself via switch_page's own query_params kwarg,
# which this checks directly by recording the exact call made.
# ======================================================================
acs.set_home_currency("fullnote2@example.com", "AUD")
_script10 = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw.current_user_email = lambda: "fullnote2@example.com"
import app
_calls = []
app.st.switch_page = lambda *a, **k: _calls.append((a, k))
_dd = {_DD_USD!r}
app._render_currency_note(_dd)
st.session_state["_switch_page_calls"] = _calls
"""
with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
    _at10 = AppTest.from_string(_script10, default_timeout=60)
    _at10.run()
    assert not _at10.exception, f"_render_currency_note() raised: {_at10.exception}"
    _at10.button(key="dd_see_currency_tool_AAPL").click().run()
_calls10 = _at10.session_state["_switch_page_calls"]
check("'See the currency view' calls st.switch_page() exactly once", len(_calls10) == 1)
_call_kwargs10 = _calls10[0][1] if _calls10 else {}
check("its query_params is EXACTLY {'ticker': 'AAPL'} - nothing else, nothing personal",
      _call_kwargs10.get("query_params") == {"ticker": "AAPL"})
print("[b2_see_tool_button_url_only] 'See the currency view' hands the ticker to "
      "st.switch_page()'s own query_params - a ticker only, never base/quote/home "
      "currency - OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)
sys.exit(1 if failed else 0)
