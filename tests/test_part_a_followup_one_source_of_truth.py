"""
Director's 6 Oct 2026 "one fix to PART A" follow-up to PART A of
instruction_portfolio_scoring_and_currency_table.md - own commit, no
push. Three sub-requirements:

  a. Gate order: on /currency-risk?ticker=X, deep_dive_engine.analyze()
     must not be called unless the visitor is signed in, visible_to()
     is true, and a home currency is set. Tests assert analyze() is
     NOT called for: a signed-out visitor; a signed-in non-owner with
     CURRENCY_VIEW_LIVE unset; a signed-in visitor with no home
     currency set. Per the Director's own words, if the code already
     does this, the tests alone are the commit for this sub-part - and
     tracing app.py's page_currency_risk() confirms it already does:
     _jump_ticker is only ever set inside the
     "if _email and _home_currency and _url_ticker and not-already-
     applied" block, and the ungated second resolution
     (_active_ticker_now = _jump_ticker or st.session_state.get(
     "cr_active_ticker")) can never see a pre-existing "cr_active_
     ticker" in a FRESH session for any of these three cases - so
     app._resolve_ticker_mos_currency_live() (the sole caller of
     deep_dive_engine.analyze() on this page) is never reached.

  b. One source of truth: the table must take BOTH margin of safety
     AND currency from the SAME analyze() result - the same fields
     the Deep Dive note uses (_dd["mos"]/_dd["currency"]). The stored-
     row margin-of-safety read is removed from the table path entirely
     (see app._resolve_ticker_mos_currency_live() and currency_risk_
     render.py's _render_mos_view_table(), which no longer imports
     snapshot_store at all). Test: the table's margin of safety (its
     "average" scenario row) equals the Deep Dive note's own displayed
     figure, for the SAME mocked analyze() result - both go through
     currency_view_engine.mos_view(mos, home, stock_currency) with the
     SAME home/stock_currency/FX-history inputs (app.py's own
     _render_currency_note() and currency_risk_render.py's
     _render_mos_view_table(), read from source, both call cve.
     mos_view() the same way), so a single shared `mos` value can only
     ever produce one view.

  c. While analyze() runs, show a spinner with plain text naming the
     ticker. On failure, keep the existing reason line (already
     covered for every failure branch by test_b3_currency_risk_tool.py
     - this file only adds the spinner-itself proof, scoped so it
     can't leak: mock.patch.object(st, "spinner", side_effect=...)
     wraps the REAL spinner, so the UI behaviour is unchanged, only
     observed).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_part_a_followup_one_source_of_truth.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part_a_followup_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import account_currency_store as acs
import currency_risk_engine as cre
import currency_view_engine as cve
import deep_dive_engine

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

_OWNER = "anmolago@hotmail.com"

# Safe, ticker-specific mock (same shape as test_b3_currency_risk_tool.
# py's own _fake_analyze()): only a ticker explicitly listed here comes
# back as a resolvable analyze() - anything else (notably whatever
# ticker the landing page's own ambient featured-stock card queries,
# via `import app`'s own module-level st.navigation().run() side effect
# that happens on every AppTest script below regardless of which page
# function it goes on to call explicitly) gets the same "couldn't
# analyze it" shape a real, unmocked analyze() call would give for an
# unknown/unfetchable ticker. A blanket mock.patch.object(..., return_
# value=...) - unconditionally "successful" for every ticker - was
# tried first and crashed that ambient render (it expects a full real
# analyze() dict, "long_score" included, for whatever ticker it is
# given).
#
# Fix (8 Oct 2026 regression-sweep flake): mirroring test_b3's pattern
# alone is NOT actually safe against this - it only avoids the crash
# when the ambient featured-stock pick (date/day_key-based, outside
# this test's control) happens to differ from every ticker registered
# below. It is EXACTLY as likely to land on "AAPL" as any other large-
# cap ticker, and when it does, the registered "success" branch used
# to return a dict missing fields app.py's own _featured_card_html()
# reads UNCONDITIONALLY (plain dd["key"], never dd.get("key")):
# "ticker", "price", "currency", "long_score", "quality_score" - read
# straight from that function's own source to get the exhaustive set,
# rather than letting each one surface one crash at a time. Filler
# values only - no check in this file asserts on any of them - so
# this is safe no matter which ticker the ambient pick turns out to
# be, not just safe by the luck of which ticker that happened to be on
# a given run.
_LIVE_RESULT_BY_TICKER = {}


def _fake_analyze(ticker, *_args, **_kwargs):
    if ticker in _LIVE_RESULT_BY_TICKER:
        r = _LIVE_RESULT_BY_TICKER[ticker]
        return {"error": None, "ticker": ticker, "mos": r.get("mos"), "currency": r.get("currency"),
                "price": 100.0, "long_score": 50.0, "quality_score": 50}
    return {"error": f"No price history found for '{ticker}'.", "error_kind": "not_found",
            "mos": None, "currency": None}


_LIVE_RESULT_BY_TICKER["AAPL"] = {"mos": 10.0, "currency": "USD"}


# ======================================================================
# PART (a): gate-order tests - deep_dive_engine.analyze() must NOT be
# called for these three scenarios, hitting the real top-level
# app.page_currency_risk() entry point with ?ticker=AAPL in the URL,
# exactly like a real GET /currency-risk?ticker=AAPL. mock.patch.
# object(deep_dive_engine, "analyze") (never a raw reassignment, so it
# can never leak across AppTest scripts in this same process) lets
# each check assert on the mock's own call_count rather than relying on
# an exception, since app._resolve_ticker_mos_currency_live() already
# swallows any exception analyze() could raise.
#
# `import app` ITSELF (its own module-level st.navigation().run(),
# ambiently rendering whatever page - here, the landing page's own
# unrelated featured-stock card) is confirmed (by direct probe) to
# call deep_dive_engine.analyze() once, for its own purposes, with NO
# relation to page_currency_risk() at all - this would otherwise
# falsely inflate every call count below by exactly 1, regardless of
# this page's own gating. The script resets the mock's call count
# right after `import app` and before the explicit app.page_currency_
# risk() call below, so the count this function returns reflects ONLY
# calls made by page_currency_risk() itself.
# ======================================================================
def _run_gate_check(email, switch_on, home_currency_set):
    if home_currency_set and email:
        acs.set_home_currency(email, "AUD")
    if switch_on:
        os.environ["CURRENCY_VIEW_LIVE"] = "1"
    else:
        os.environ.pop("CURRENCY_VIEW_LIVE", None)
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False
import app
import deep_dive_engine as _dde
_dde.analyze.reset_mock()
{f'st.session_state["email_user"] = {email!r}' if email else ''}
app.page_currency_risk()
"""
    with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
         mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS), \
         mock.patch.object(deep_dive_engine, "analyze", side_effect=_fake_analyze) as _mock_analyze:
        at = AppTest.from_string(script, default_timeout=60)
        at.query_params["ticker"] = "AAPL"
        at.run()
        _call_count = _mock_analyze.call_count
    os.environ.pop("CURRENCY_VIEW_LIVE", None)
    assert not at.exception, f"_run_gate_check({email!r}) raised: {at.exception}"
    return _call_count


# Sanity check FIRST, deliberately: `import app` only ever re-executes
# app.py's own module-level code (including its ambient st.navigation().
# run(), which renders whatever page - the landing page's own unrelated
# featured-stock card - on the VERY FIRST `import app` of this process;
# every later AppTest script's `import app` is an ordinary cached Python
# import, a no-op that does not re-render anything). That ambient
# landing-page render is itself SIGNED OUT; pairing it with an explicit
# page_currency_risk() call that is ALSO signed out collides on
# paywall_engine's own hardcoded "account_bar_signin_pop" widget key
# (both would render that same signed-out control once each, in the
# SAME script execution) - confirmed directly against this exact repo's
# own code, not a theoretical concern. A signed-IN explicit call never
# collides (it takes render_account_bar()'s signed-in branch instead,
# which never touches that key), so running this sanity check - which
# also proves _run_gate_check() itself is capable of registering a real
# call, i.e. the zero counts below are a real gate holding, not a blind
# spot in how the call is counted - FIRST safely "spends" that one-time
# ambient render before any signed-out script runs.
acs.set_home_currency(_OWNER, "AUD")
_cc_owner = _run_gate_check(_OWNER, switch_on=False, home_currency_set=False)
check("(sanity) owner, home currency set, switch off (owner is always visible): "
      "deep_dive_engine.analyze() IS called at least once",
      _cc_owner >= 1)
print("[followup_a_sanity] the harness itself does observe a real analyze() call when every "
      "gate is satisfied, confirming the zero counts below are the gate holding, not a blind "
      "spot in how the call is counted OK")

_cc_signed_out = _run_gate_check(None, switch_on=False, home_currency_set=False)
check("(a.1) signed-out visitor on /currency-risk?ticker=AAPL: "
      "deep_dive_engine.analyze() is NEVER called",
      _cc_signed_out == 0)

acs.set_home_currency("a2_nonowner@example.com", "AUD")  # even WITH a home currency saved
_cc_nonowner_switch_off = _run_gate_check(
    "a2_nonowner@example.com", switch_on=False, home_currency_set=False)
check("(a.2) signed-in non-owner, CURRENCY_VIEW_LIVE unset, /currency-risk?ticker=AAPL: "
      "deep_dive_engine.analyze() is NEVER called",
      _cc_nonowner_switch_off == 0)

_cc_no_home_currency = _run_gate_check(
    "a3_nohome@example.com", switch_on=True, home_currency_set=False)
check("(a.3) signed-in visitor with no home currency set, /currency-risk?ticker=AAPL: "
      "deep_dive_engine.analyze() is NEVER called",
      _cc_no_home_currency == 0)
print("[followup_a_gate_order] deep_dive_engine.analyze() is never called for a signed-out "
      "visitor, a signed-in non-owner with the switch unset, or a signed-in visitor with no "
      "home currency set - the code already enforced this gate order before this follow-up; "
      "per the Director's own words, these tests alone are the commit for part (a) OK")


# ======================================================================
# PART (b): one source of truth - the table's margin of safety (its
# "average" scenario row) equals the Deep Dive note's own displayed
# figure, for the SAME analyze() result. Both app._render_currency_
# note() and currency_risk_render.py's _render_mos_view_table() are
# driven by the exact same mocked deep_dive_engine.analyze() return
# value and the same cre.get_fx_history()/period_stats() mocks, so if
# either read a second, different source for margin of safety, the two
# figures would disagree.
# ======================================================================
_SHARED_MOS = 66.1
_SHARED_CURRENCY = "USD"
_SHARED_HOME = "AUD"
_SHARED_TICKER = "AAPL"

with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
    _expected_view = cve.mos_view(_SHARED_MOS, _SHARED_HOME, _SHARED_CURRENCY)
_expected_avg = _expected_view["view_at_average"]

# The Deep Dive note, called directly with the SAME _dd shape
# deep_dive_engine.analyze() would return (app._render_currency_note()
# reads ONLY _dd["mos"]/_dd["currency"] plus the caller's email/home
# currency - it has no deep_dive_engine coupling of its own, so this is
# exactly how app.py's real Deep Dive page calls it). CURRENCY_VIEW_
# LIVE must be on (or the caller must be the owner) - currency_view_
# engine.visible_to() gates both the note and the table identically,
# and neither reader below is the owner.
os.environ["CURRENCY_VIEW_LIVE"] = "1"
acs.set_home_currency("note_reader@example.com", _SHARED_HOME)
_note_script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False
import app
st.session_state["email_user"] = "note_reader@example.com"
app._render_currency_note(
    {{"mos": {_SHARED_MOS!r}, "currency": {_SHARED_CURRENCY!r}, "ticker": {_SHARED_TICKER!r}}}
)
"""
with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
    _at_note = AppTest.from_string(_note_script, default_timeout=60)
    _at_note.run()
assert not _at_note.exception, f"_render_currency_note() raised: {_at_note.exception}"
_note_text = " ".join(c.value or "" for c in _at_note.caption)
check("the Deep Dive note shows the average-scenario margin computed from mos=66.1",
      f"{_expected_avg:.1f}" in _note_text)

# The Currency Risk table, reached via the real page_currency_risk()
# entry point, with deep_dive_engine.analyze() mocked to return the
# EXACT SAME _dd shape (mos=66.1, currency=USD) the note above used.
acs.set_home_currency("table_reader@example.com", _SHARED_HOME)
_table_script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False
import app
st.session_state["email_user"] = "table_reader@example.com"
app.page_currency_risk()
"""
with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS), \
     mock.patch.object(deep_dive_engine, "analyze") as _mock_analyze_table:
    _mock_analyze_table.return_value = {
        "error": None, "ticker": _SHARED_TICKER, "mos": _SHARED_MOS, "currency": _SHARED_CURRENCY,
    }
    _at_table = AppTest.from_string(_table_script, default_timeout=60)
    _at_table.query_params["ticker"] = _SHARED_TICKER
    _at_table.run()
assert not _at_table.exception, f"page_currency_risk() raised: {_at_table.exception}"
_table_rows = []
for df in _at_table.dataframe:
    try:
        _table_rows.extend(df.value.to_dict("records"))
    except Exception:
        pass
_margin_col = None
for _row in _table_rows:
    for k in _row:
        if "margin" in k.lower():
            _margin_col = k
            break
    if _margin_col:
        break
check("the Currency Risk table rendered its margin-of-safety rows",
      _margin_col is not None and len(_table_rows) > 0)
_average_row_margin = _table_rows[0][_margin_col] if _table_rows and _margin_col else None
check("the table's average-scenario margin equals the Deep Dive note's own figure, for "
      "the SAME analyze() result (one source of truth - no stored-row read in the way)",
      _average_row_margin == f"{_expected_avg:+.1f}%")
print("[followup_b_one_source_of_truth] the table's margin of safety and the Deep Dive "
      "note's own figure are bit-for-bit the same number for the same analyze() result - "
      "both come from the one mos value, never a separate stored-row read OK")
os.environ.pop("CURRENCY_VIEW_LIVE", None)


# ======================================================================
# PART (c): while analyze() runs, a spinner shows plain text naming the
# ticker. mock.patch.object(st, "spinner", side_effect=...) wraps the
# REAL st.spinner (so the UI behaviour this proves is unchanged, only
# observed) and records the text each call was made with. Also proves
# the session cache: a second call for the same ticker is a cache hit
# (no second analyze() call, no second spinner).
# ======================================================================
_spinner_script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
from unittest import mock
import app

_spinner_calls = []
_orig_spinner = st.spinner

def _spy_spinner(text="In progress...", *a, **kw):
    _spinner_calls.append(text)
    return _orig_spinner(text, *a, **kw)

with mock.patch.object(st, "spinner", side_effect=_spy_spinner):
    _first = app._resolve_ticker_mos_currency_live("SPINCO")
    _second = app._resolve_ticker_mos_currency_live("SPINCO")  # same ticker, same session

st.session_state["_test_spinner_calls"] = list(_spinner_calls)
st.session_state["_test_first_result"] = _first
st.session_state["_test_second_result"] = _second
"""
with mock.patch.object(deep_dive_engine, "analyze") as _mock_analyze_spinner:
    _mock_analyze_spinner.return_value = {
        "error": None, "ticker": "SPINCO", "mos": 5.0, "currency": "EUR", "price": 42.5,
    }
    _at_spin = AppTest.from_string(_spinner_script, default_timeout=60)
    _at_spin.run()
    _spinner_analyze_call_count = _mock_analyze_spinner.call_count
assert not _at_spin.exception, f"spinner script raised: {_at_spin.exception}"
_spinner_calls = _at_spin.session_state["_test_spinner_calls"]
check("(c) exactly one spinner call for the ticker's first resolution this session",
      len(_spinner_calls) == 1)
check("(c) the spinner's own text names the ticker in plain language",
      len(_spinner_calls) == 1 and "SPINCO" in _spinner_calls[0])
check("(c) the second call for the SAME ticker this session shows NO second spinner "
      "(a session-cache hit, not a second analyze() call)",
      len(_spinner_calls) == 1)
check("one analyze() call per ticker per session: deep_dive_engine.analyze() itself was "
      "only invoked once despite _resolve_ticker_mos_currency_live() being called twice "
      "for the same ticker in the same session",
      _spinner_analyze_call_count == 1)
check("both calls this session return the identical resolved result",
      _at_spin.session_state["_test_first_result"] == _at_spin.session_state["_test_second_result"]
      # Director, 8 Oct 2026, currency-adjusted-value addition:
      # _resolve_ticker_mos_currency_live() now also carries "price",
      # the same _dd["price"] app.py's currency-adjusted-value feature
      # anchors to - from the SAME one analyze() call, never a second.
      == {"mos": 5.0, "currency": "EUR", "price": 42.5})
print("[followup_c_spinner] a plain-text spinner naming the ticker shows while analyze() "
      "runs, never a second time for a session-cached ticker, and the session cache means "
      "exactly one real analyze() call per ticker per session OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)
sys.exit(1 if failed else 0)
