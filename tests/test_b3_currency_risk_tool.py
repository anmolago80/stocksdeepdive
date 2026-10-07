"""
SECTION B, COMMIT B3 of instruction_top200_amendments_and_currency_view.md
(5 Oct 2026, Director-directed): Currency Risk tool additions - the From
picker defaults to a signed-in visitor's home currency, and an extra
"margin of safety in your currency" table appears under section C only
when arriving from a Deep Dive currency-risk note with a ticker.

Covers exactly the task's own listed tests:
  - the default From picker (home currency when set and visible;
    DEFAULT_BASE otherwise, including signed-out/non-owner-while-the-
    switch-is-off, "the page is exactly as today")
  - the table with and without a ticker
  - the page unchanged for a signed-out visitor

Plus: the table's own currency-effect numbers equal currency_view_
engine.mos_view()'s own (single source of truth, re-confirmed one
layer up from test_b2_currency_view_calc.py's own proof against
currency_risk_engine.position_impact() directly).

Fix history (full account in this session's own reports):
A live report (owner, home currency AUD, INTU) first showed the table
never appearing even though the jump reached the page correctly. Root
cause #1, confirmed from source: _seed_snapshot() in THIS file used to
write {"mos_pct":..., "currency":...} directly - the public, lowercase
key shape - but NO real production snapshot row is ever shaped that
way; both real producers (nightly_scan.analyze_ticker_lite(), app.py's
own _save_live_snapshot() live-Deep-Dive-view hook) write margin of
safety under "MOS %" (capitalised), never "mos_pct". _seed_snapshot()
now writes the real "MOS %" key.

A SECOND live report (same owner/ticker, after the first fix) showed
the table STILL not appearing. Root cause #2: NEITHER real producer
EVER writes a currency field to the stored row, under any key at all
- not a naming mismatch like MOS, a genuine storage gap. Per the
Director's instruction_portfolio_scoring_and_currency_table.md, PART A
STEP A1, this is fixed WITHOUT adding a stored field: the ticker's own
trading currency is now resolved LIVE, via the SAME function
(deep_dive_engine.analyze()) the Deep Dive currency note itself reads
its own currency from - app.py's _resolve_ticker_currency_live().
Every check below that needs a resolved currency now mocks deep_dive_
engine.analyze() (via _mock_live_currency(), see below) rather than
seeding a currency onto the stored snapshot row - matching exactly how
production now resolves it. _seed_snapshot()'s own `currency` kwarg
was removed entirely (it no longer does anything in production).

A THIRD follow-up (Director, 6 Oct 2026, "one fix to PART A"): mixing
that live currency with a still-stored-row margin of safety was itself
a one-source-of-truth bug - the two could disagree for the same
ticker. Margin of safety now comes from the SAME analyze() call as
currency (app.py's _resolve_ticker_currency_live() was extended into
_resolve_ticker_mos_currency_live(), returning both). _seed_snapshot()
and every snapshot_store usage were removed from this file entirely -
every check below controls mos AND currency purely through the one
deep_dive_engine.analyze() mock (_LIVE_RESULT_BY_TICKER/_fake_analyze()
below). This follow-up also added three gate-order tests (deep_dive_
engine.analyze() must not be called for a signed-out visitor, a
signed-in non-owner with CURRENCY_VIEW_LIVE unset, or a signed-in
visitor with no home currency set) and a one-source-of-truth test (the
table's margin of safety equals the Deep Dive note's for the same
analyze() result) - see test_part_a_followup_one_source_of_truth.py.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_b3_currency_risk_tool.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="b3_currency_risk_tool_test_")
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


# PART A STEP A1 (Director, 6 Oct 2026, instruction_portfolio_scoring_
# and_currency_table.md), amended by the Director's "one fix to PART A"
# follow-up (part b): BOTH the ticker's own trading currency AND its
# margin of safety are now resolved LIVE, from the SAME deep_dive_
# engine.analyze() call, by app.py's _resolve_ticker_mos_currency_live()
# - the SAME function the Deep Dive currency note itself reads its own
# currency from (one source of truth; see that function's own docstring
# for the full fix history). There is no longer any stored-row read on
# this path at all - _seed_snapshot()/snapshot_store are gone from this
# file entirely; every check below controls BOTH values purely through
# this one mock.
#
# Tests control this by mocking deep_dive_engine.analyze() directly
# (never a raw reassignment - mock.patch.object only, scoped, so it can
# never leak across AppTest scripts in this same process) to return a
# plain dict shaped exactly like that real function's own return value:
# {"error": ..., "mos": ..., "currency": ...}. A ticker absent from
# _LIVE_RESULT_BY_TICKER simulates deep_dive_engine.analyze() being
# unable to analyze it at all (bad symbol / fetch failure) - the same
# "error" key every real caller of analyze() already checks, giving
# back no mos and no currency. A ticker present with "currency": "-"
# simulates a ticker that DOES analyze successfully (a real mos comes
# back) but whose own currency info is unavailable - deep_dive_engine.
# py's own "no currency on file" sentinel.
_LIVE_RESULT_BY_TICKER = {}


def _fake_analyze(ticker, *_args, **_kwargs):
    if ticker in _LIVE_RESULT_BY_TICKER:
        r = _LIVE_RESULT_BY_TICKER[ticker]
        return {"error": None, "ticker": ticker, "mos": r.get("mos"), "currency": r.get("currency")}
    return {"error": f"No price history found for '{ticker}'.", "error_kind": "not_found",
            "mos": None, "currency": None}


def _mock_live_currency():
    """One mock.patch.object(...), reused by every helper below
    alongside the existing cre.get_fx_history/period_stats mocks."""
    return mock.patch.object(deep_dive_engine, "analyze", side_effect=_fake_analyze)


def _run_page(email, extra_env=None, jump=None):
    # `jump` carries a ticker (and, for backward-compatible callers in
    # this file, a now-unused base/quote) - fixed per the Director's 6
    # Oct 2026 live-bug report: only the ticker ever travels, via the
    # URL's own ?ticker= query param (set on `at` BEFORE the first
    # .run(), exactly like a browser landing fresh on that URL), never
    # a session key. base/quote are derived server-side from the
    # ticker's own cached currency and the visitor's home currency.
    # Fix (Director, 6 Oct 2026): reassigning paywall_engine.current_
    # user_email directly (rather than through mock.patch, or through
    # the real st.session_state["email_user"] mechanism) PERMANENTLY
    # overwrites it on the shared paywall_engine module for every later
    # AppTest script in this process (AppTest execs in-process - see
    # test_b2_deepdive_currency_note.py's own note on this exact class
    # of bug) - and it never drives paywall_engine.is_logged_in() at
    # all, which render_account_bar() branches on separately. Setting
    # the real session_state key, after `import app`'s own ambient
    # st.navigation().run() (so that ambient render stays harmlessly
    # signed-out rather than colliding on the signed-in branch's
    # hardcoded widget key), drives both functions together exactly
    # like production and leaves no cross-test residue.
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False
import app
{f'st.session_state["email_user"] = {email!r}' if email else ''}
app.page_currency_risk()
"""
    at = AppTest.from_string(script, default_timeout=60)
    if jump and jump.get("ticker"):
        at.query_params["ticker"] = jump["ticker"]
    for k, v in (extra_env or {}).items():
        os.environ[k] = v
    _patches = [
        mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY),
        mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS),
        _mock_live_currency(),
    ]
    for p in _patches:
        p.start()
    try:
        at.run()
    finally:
        for p in _patches:
            p.stop()
        for k in (extra_env or {}):
            os.environ.pop(k, None)
    assert not at.exception, f"page_currency_risk() raised: {at.exception}"
    return at


def _page_text(at):
    _df_text = []
    for df in at.dataframe:
        try:
            _df_text.append(df.value.to_csv(index=False))
        except Exception:
            _df_text.append(str(df.value))
    return "\n".join(
        [getattr(el, "value", "") or "" for el in at.caption] +
        [getattr(el, "value", "") or "" for el in at.markdown] +
        _df_text +
        _popover_labels(at._tree)
    )


def _popover_labels(node):
    """AppTest has no typed accessor for st.popover's own trigger label
    (unlike st.button/st.selectbox/...) - the label only lives on the
    raw element tree's protobuf (node.proto.popover.label), so this
    walks it directly. Needed to verify COMMIT B1's account-bar
    "Home currency: choose"/"Home currency: {currency}" control, which
    is never plain markdown/caption text."""
    labels = []
    children = getattr(node, "children", None)
    if children:
        for child in children.values():
            proto = getattr(child, "proto", None)
            if proto is not None:
                try:
                    if proto.HasField("popover"):
                        labels.append(proto.popover.label)
                except ValueError:
                    pass
            labels.extend(_popover_labels(child))
    return labels


_OWNER = "anmolago@hotmail.com"

# ======================================================================
# CHECK 1: default From picker - signed in + home currency set +
# visible (owner, switch OFF) -> starts on the home currency, not
# DEFAULT_BASE ("AUD").
# ======================================================================
os.environ.pop("CURRENCY_VIEW_LIVE", None)
acs.set_home_currency(_OWNER, "JPY")
_at1 = _run_page(_OWNER)
check("signed-in owner with home currency JPY: the From picker starts on JPY, not AUD",
      _at1.selectbox(key="cr_base").value == "JPY")
print("[b3_default_from_home_currency] the From picker starts on the signed-in visitor's "
      "own home currency when one is set and visible OK")

# ======================================================================
# CHECK 2: "for everyone else the page is exactly as today" - signed
# out, and signed in but NOT visible (non-owner, switch OFF) -> From
# picker still starts on DEFAULT_BASE ("AUD"), same as before this
# commit existed.
# ======================================================================
_at2a = _run_page(None)
check("signed out: From picker still starts on DEFAULT_BASE (AUD) - page unchanged",
      _at2a.selectbox(key="cr_base").value == cre.DEFAULT_BASE)

acs.set_home_currency("nonowner@example.com", "EUR")
_at2b = _run_page("nonowner@example.com")
check("signed in, non-owner, switch OFF (not visible): From picker still starts on "
      "DEFAULT_BASE, ignoring their own saved EUR - page unchanged",
      _at2b.selectbox(key="cr_base").value == cre.DEFAULT_BASE)
print("[b3_everyone_else_unchanged] a signed-out or non-owner-while-the-switch-is-off "
      "visitor's From picker is byte-identical to before this commit OK")

# ======================================================================
# CHECK 3: no ticker -> no extra table. Signed in + home currency set,
# but no currency_risk_jump this run.
# ======================================================================
check("no jump ticker: the 'Margin of safety, ... view' heading never appears",
      "Margin of safety," not in _page_text(_at1))

# ======================================================================
# CHECK 4: signed out, even WITH a jump dict somehow present -> no
# table (page_currency_risk() only honours the jump for a signed-in,
# visible visitor in the first place).
# ======================================================================
_LIVE_RESULT_BY_TICKER["AAPL"] = {"mos": 66.1, "currency": "USD"}
_at4 = _run_page(None, jump={"ticker": "AAPL"}, extra_env={"CURRENCY_VIEW_LIVE": "1"})
check("signed out, even with a jump present: no extra table",
      "Margin of safety," not in _page_text(_at4))
print("[b3_no_ticker_no_table] without a ticker, or signed out, no extra table ever "
      "appears OK")

# ======================================================================
# CHECK 5: WITH a ticker, signed in, home currency set, visible -> the
# extra table DOES appear, and its own numbers equal cve.mos_view()'s.
# AAPL's mos (66.1) and currency ("USD") both come from _fake_analyze()
# (deep_dive_engine.analyze()'s own real return shape), per _LIVE_
# RESULT_BY_TICKER set just above - the ONE live-resolution call PART A
# STEP A1 introduced and the "one fix to PART A" follow-up extended to
# cover mos too.
# ======================================================================
acs.set_home_currency("withticker@example.com", "AUD")
_at5 = _run_page("withticker@example.com", jump={"ticker": "AAPL"},
                 extra_env={"CURRENCY_VIEW_LIVE": "1"})
_text5 = _page_text(_at5)
check("with a ticker, signed in, home currency set: the heading appears",
      "Margin of safety," in _text5 and "AAPL" in _text5)
check("the From/To pickers landed on the jump's own pair (AUD/USD)",
      _at5.selectbox(key="cr_base").value == "AUD" and _at5.selectbox(key="cr_quote").value == "USD")

with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
    _expected = cve.mos_view(66.1, "AUD", "USD")
check("the table's own average-scenario margin equals cve.mos_view()'s own (single "
      "source of truth, one layer up from the engine-level proof)",
      f"{_expected['view_at_average']:+.1f}%" in _text5)
print("[b3_table_with_ticker] the extra table appears with a ticker, carries the pair "
      "over, and its own numbers equal currency_view_engine.mos_view()'s own OK")

# ======================================================================
# CHECK 6: the ticker's own currency equals the home currency (AUD) ->
# the PAGE's own base==quote collapse fires before the table is ever
# reached (jump sets both cr_base and cr_quote to AUD) - confirms that
# pre-existing "same currency -> warning, nothing past that point"
# path is untouched by this fix. The distinct "ticker currency known,
# matches base, base != quote" case inside _render_mos_view_table()
# itself (a correct non-failure outcome) is covered separately by
# CHECK 13 below, via presetting cr_base/cr_quote directly.
# ======================================================================
_LIVE_RESULT_BY_TICKER["CSL.AX"] = {"mos": 10.0, "currency": "AUD"}  # an AUD-priced ticker
_at6 = _run_page("withticker@example.com", jump={"ticker": "CSL.AX"},
                 extra_env={"CURRENCY_VIEW_LIVE": "1"})
# base == quote here -> the page itself warns and returns before any
# table could render - confirms the "same currency -> warning, nothing
# past that point" path is untouched by this commit.
check("ticker/quote pair collapses to the same-currency warning path, no table, no crash",
      "Margin of safety," not in _page_text(_at6))
print("[b3_mismatched_currency_no_table] a ticker whose own currency doesn't match the "
      "selected pair never gets a guessed table OK")

# ======================================================================
# CHECK 7 (Director's 6 Oct 2026 live-bug follow-up): the ticker now
# travels via the URL's own ?ticker= query param, not a one-shot
# session key - because a one-shot session key does not survive the
# dropped/reconnected WebSocket session a live click showed (Railway
# logging a full GET /currency-risk with no prior session state).
# Proves the exact bug scenario first - a FRESH session (no session_
# state seeded at all, only the URL) must show the table on its very
# first run - then re-proves everything CHECK 7 already covered
# (persistence across another widget's rerun, the real Close button,
# a fresh jump to a different ticker) under the new mechanism.
# ======================================================================
os.environ["CURRENCY_VIEW_LIVE"] = "1"
acs.set_home_currency("persist@example.com", "AUD")
_LIVE_RESULT_BY_TICKER["AAPL"] = {"mos": 66.1, "currency": "USD"}
_LIVE_RESULT_BY_TICKER["MSFT"] = {"mos": 20.0, "currency": "USD"}

_script7 = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False
import app
st.session_state["email_user"] = "persist@example.com"
app.page_currency_risk()
"""
_patches7 = [
    mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY),
    mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS),
    _mock_live_currency(),
]
for p in _patches7:
    p.start()
try:
    # THE BUG SCENARIO, reproduced directly: a brand-new AppTest instance
    # (no session_state seeded at all - this IS "a fresh session") whose
    # ONLY input is the URL's own query param, exactly as a real browser
    # landing fresh on https://.../currency-risk?ticker=AAPL would arrive.
    _at7 = AppTest.from_string(_script7, default_timeout=60)
    _at7.query_params["ticker"] = "AAPL"
    _at7.run()
    assert not _at7.exception, f"first run (fresh session, URL ticker only) raised: {_at7.exception}"
    check("FRESH SESSION, no prior session_state, only ?ticker=AAPL in the URL: "
          "the table appears on the very first run - this is the exact bug scenario "
          "the Director reported, now fixed",
          "Margin of safety," in _page_text(_at7))
    check("the From/To pickers also landed on the right pair (AUD/USD) from the URL "
          "ticker + the signed-in visitor's own home currency - no base/quote needed "
          "in the URL itself",
          _at7.selectbox(key="cr_base").value == "AUD" and _at7.selectbox(key="cr_quote").value == "USD")

    # Interact with a DIFFERENT widget on the page (the range chips, cr_range) -
    # the URL still says ?ticker=AAPL, but this must not be treated as a FRESH
    # jump a second time (it would otherwise re-fight the visitor's own choices).
    _at7.segmented_control(key="cr_range").set_value("20y")
    _at7.run()
    assert not _at7.exception, f"rerun after changing the range chip raised: {_at7.exception}"
    check("changing another widget (the range chips) on the page KEEPS the table",
          "Margin of safety," in _page_text(_at7))

    # The real Close button removes it AND drops the URL's own ?ticker=.
    _close_buttons = [b for b in _at7.button if b.label == "Close"]
    check("a real 'Close' button is offered", len(_close_buttons) == 1)
    _close_buttons[0].click()
    _at7.run()
    assert not _at7.exception, f"rerun after Close raised: {_at7.exception}"
    check("clicking Close removes the table", "Margin of safety," not in _page_text(_at7))
    check("clicking Close also clears the URL's own ?ticker= (so reloading this same "
          "URL, or a future reconnect, does not silently reopen it)",
          "ticker" not in dict(_at7.query_params))

    # A fresh jump to a DIFFERENT ticker (a new URL) overwrites the (now-closed)
    # active ticker, rather than requiring it to already be open.
    _at7.query_params["ticker"] = "MSFT"
    _at7.run()
    assert not _at7.exception, f"rerun after a fresh jump to MSFT raised: {_at7.exception}"
    _text7b = _page_text(_at7)
    check("a fresh jump to a different ticker (MSFT) opens the table again, for MSFT",
          "Margin of safety," in _text7b and "MSFT" in _text7b and "AAPL" not in _text7b)
finally:
    for p in _patches7:
        p.stop()
    os.environ.pop("CURRENCY_VIEW_LIVE", None)
print("[b3_table_persists_across_reruns] a fresh session landing on the URL alone "
      "(no session state) shows the table immediately - the exact bug the Director "
      "reported - and it still survives other widgets' reruns, closes via the real "
      "Close button (which also clears the URL), and reopens on a fresh jump to a "
      "different ticker OK")

# ======================================================================
# CHECK 8: the URL jump is ignored for a signed-out visitor, and for a
# signed-in visitor who isn't visible (non-owner, switch off) - "a
# ticker only, nothing personal" must never leak a table to someone who
# shouldn't see one just because the URL happens to carry one.
# ======================================================================
os.environ["CURRENCY_VIEW_LIVE"] = "1"
_at8a_script = _script7.replace('st.session_state["email_user"] = "persist@example.com"', '')
with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS), \
     _mock_live_currency():
    _at8a = AppTest.from_string(_at8a_script, default_timeout=60)
    _at8a.query_params["ticker"] = "AAPL"
    _at8a.run()
assert not _at8a.exception, f"signed-out URL-jump run raised: {_at8a.exception}"
check("signed out, URL carries ?ticker=AAPL: no table (the ticker is never applied)",
      "Margin of safety," not in _page_text(_at8a))
os.environ.pop("CURRENCY_VIEW_LIVE", None)

os.environ.pop("CURRENCY_VIEW_LIVE", None)
_at8b_script = _script7.replace('st.session_state["email_user"] = "persist@example.com"',
                                 'st.session_state["email_user"] = "nonowner2@example.com"')
acs.set_home_currency("nonowner2@example.com", "AUD")
with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS), \
     _mock_live_currency():
    _at8b = AppTest.from_string(_at8b_script, default_timeout=60)
    _at8b.query_params["ticker"] = "AAPL"
    _at8b.run()
assert not _at8b.exception, f"non-owner URL-jump run (switch off) raised: {_at8b.exception}"
check("signed in, non-owner, switch OFF, URL carries ?ticker=AAPL: still no table",
      "Margin of safety," not in _page_text(_at8b))
print("[b3_url_jump_never_leaks] a URL ticker is only ever applied for a visitor who "
      "already passes the same visible()+home-currency gate every other COMMIT B3 "
      "surface uses OK")

# ======================================================================
# CHECK 9 (Director's 6 Oct 2026 follow-up - "prove the gates still
# hold when the page is reached by URL alone"): the exact four cases,
# each its own completely fresh AppTest session (nothing seeded but
# the URL's own ?ticker=AAPL), hitting the real top-level
# app.page_currency_risk() entry point - no internal function called
# directly, no session_state pre-seeded beyond the single query param -
# matching a real GET /currency-risk?ticker=AAPL exactly.
#
# AAPL's currency is resolvable (set below), deliberately - these four
# cases are about the VISIBILITY gates (signed-out/non-owner/owner/no-
# home-currency), a question that is orthogonal to whether currency
# data happens to be resolvable for this one ticker today. See CHECK
# 10/11 right after this block for a ticker with no stored row at all,
# and for a ticker whose live currency resolution itself fails - the
# "never show nothing" fallback both now produce.
# ======================================================================
_LIVE_RESULT_BY_TICKER["AAPL"] = {"mos": 66.1, "currency": "USD"}


def _fresh_url_only(email, switch_on, ticker="AAPL"):
    # Real sign-in simulation, not a current_user_email() monkeypatch:
    # paywall_engine.is_logged_in() (which render_account_bar() itself
    # branches on, separately from current_user_email()) reads
    # st.session_state["email_user"] directly - the account bar's own
    # extra_widget4 (COMMIT B1's currency picker) is never even passed
    # through on the signed-out branch, so a monkeypatched current_
    # user_email() alone under-simulates a real signed-in visitor and
    # would make case (d) below look broken when it isn't.
    #
    # The session_state assignment happens INSIDE the script, AFTER
    # `import app` - not via at.session_state before the first .run().
    # `import app` itself runs app.py's own module-level st.navigation()
    # .run() as an import side effect (it renders whatever page the
    # CURRENT routing state resolves to, ambiently, before this script's
    # own explicit app.page_currency_risk() call even executes) - if
    # email_user were already set at that point, BOTH that ambient
    # render and the explicit call below would hit paywall_engine's
    # signed-in branch and its hardcoded st.container(key="pw_account_
    # name_box"), crashing on a duplicate element key. Setting it only
    # after the import keeps the ambient render signed-out (harmless -
    # it's not what this test is checking) and the explicit call below
    # is the only signed-in render in this script.
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
# Same test-environment gate every account-bar test in this repo needs
# (see test_b1_home_currency.py's own _run_account_bar()) - no Google/
# email-auth secrets exist in this sandbox, so render_account_bar()
# would otherwise take its "no sign-in method configured at all"
# branch, which never even looks at extra_widget4.
pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False
import app
{f'st.session_state["email_user"] = {email!r}' if email else ''}
app.page_currency_risk()
"""
    if switch_on:
        os.environ["CURRENCY_VIEW_LIVE"] = "1"
    else:
        os.environ.pop("CURRENCY_VIEW_LIVE", None)
    with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
         mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS), \
         _mock_live_currency():
        at = AppTest.from_string(script, default_timeout=60)
        at.query_params["ticker"] = ticker
        at.run()
    os.environ.pop("CURRENCY_VIEW_LIVE", None)
    assert not at.exception, f"_fresh_url_only({email!r}, switch_on={switch_on}) raised: {at.exception}"
    return at


# (a) Signed-out visitor: no stock table, page otherwise normal.
_at9a = _fresh_url_only(None, switch_on=False)
check("(a) signed out, /currency-risk?ticker=AAPL: no 'Margin of safety' table",
      "Margin of safety," not in _page_text(_at9a))
check("(a) page otherwise normal: the From/To selectors still render with their "
      "ordinary defaults (AUD/USD), no exception, and signed out means no account-bar "
      "currency picker is rendered at all (exactly 2 selectboxes, not 3)",
      len(_at9a.selectbox) == 2
      and _at9a.selectbox(key="cr_base").value == cre.DEFAULT_BASE
      and _at9a.selectbox(key="cr_quote").value == cre.DEFAULT_QUOTE)

# (b) Signed-in non-owner, CURRENCY_VIEW_LIVE unset: no stock table.
acs.set_home_currency("check9b@example.com", "AUD")  # even WITH a home currency saved
_at9b = _fresh_url_only("check9b@example.com", switch_on=False)
check("(b) signed in, non-owner, switch unset, /currency-risk?ticker=AAPL: no table",
      "Margin of safety," not in _page_text(_at9b))

# (c) Owner, CURRENCY_VIEW_LIVE unset, home currency set: table shown.
acs.set_home_currency(_OWNER, "AUD")
_at9c = _fresh_url_only(_OWNER, switch_on=False)
_text9c = _page_text(_at9c)
check("(c) owner, switch unset, home currency set, /currency-risk?ticker=AAPL: "
      "the table IS shown",
      "Margin of safety," in _text9c and "AAPL" in _text9c)

# (d) Signed-in visitor, no home currency set (switch ON so the gate itself is
# passable - otherwise there is no "choose currency" control to show at all,
# the same way COMMIT B1's own account-bar control is owner-only/switch-gated):
# no table, the account-bar "choose currency" prompt instead.
_at9d = _fresh_url_only("check9d@example.com", switch_on=True)
_text9d = _page_text(_at9d)
check("(d) signed in, no home currency set, /currency-risk?ticker=AAPL: no table",
      "Margin of safety," not in _text9d)
check("(d) the 'choose currency' prompt (COMMIT B1's own account-bar control) is "
      "shown instead",
      "Home currency: choose" in _text9d)
print("[b3_gates_hold_via_url_alone] all four cases (signed out / non-owner switch-"
      "off / owner switch-off-with-home-currency / no-home-currency) behave exactly "
      "right when the page is reached by URL alone, with nothing else seeded OK")

# ======================================================================
# CHECK 10 (Director's 6 Oct 2026 live-bug follow-up, "never show
# nothing"; amended by the "one fix to PART A" follow-up, part b): a
# ticker deep_dive_engine.analyze() can't analyze at all (bad symbol /
# fetch failure - absent from _LIVE_RESULT_BY_TICKER, so _fake_analyze()
# returns its own "error" branch, mos=None) - the table can't show, and
# must never silently show nothing either. A plain line explains why; a
# non-owner never sees the owner-only diagnostic.
# ======================================================================
acs.set_home_currency("check10@example.com", "AUD")
_at10 = _fresh_url_only("check10@example.com", switch_on=True, ticker="ZZZZ_NEVER_SCANNED")
_text10 = _page_text(_at10)
check("ticker analyze() can't resolve at all: no 'Margin of safety' table",
      "Margin of safety," not in _text10)
check("a plain line explains why (no confirmed valuation), not a silent gap",
      "We couldn't confirm a valuation for ZZZZ_NEVER_SCANNED right now" in _text10)
check("a non-owner never sees the owner-only diagnostic caption",
      "[Owner-only diagnostic]" not in _text10)
print("[b3_no_valuation_never_silent] a ticker analyze() can't resolve at all shows a "
      "plain explanatory line, never nothing OK")

# ======================================================================
# CHECK 11 (Director's 6 Oct 2026 PART A STEP A1, amended by the "one
# fix to PART A" follow-up, part b): a ticker whose SAME analyze() call
# comes back with a real margin of safety but no confirmed currency -
# deep_dive_engine.py's own "no currency on file" sentinel ("-"), via
# _LIVE_RESULT_BY_TICKER["NOCCY"]'s own "currency": "-" below. This is
# the live-resolution path's own "we got a valuation but can't confirm
# the currency" condition. The owner sees the exact failed condition; a
# non-owner (even though the switch is live, so they too reach this
# far) sees only the plain line.
# ======================================================================
_LIVE_RESULT_BY_TICKER["NOCCY"] = {"mos": 12.3, "currency": "-"}  # a real mos, no
                                                                   # confirmed currency
acs.set_home_currency(_OWNER, "AUD")
_at11_owner = _fresh_url_only(_OWNER, switch_on=True, ticker="NOCCY")
_text11_owner = _page_text(_at11_owner)
check("real mos, but no confirmed currency: no table",
      "Margin of safety," not in _text11_owner)
check("the plain line says the currency couldn't be confirmed right now",
      "We couldn't confirm NOCCY's trading currency right now" in _text11_owner)
check("the OWNER sees the exact failed condition as an extra diagnostic caption",
      "[Owner-only diagnostic]" in _text11_owner
      and "_resolve_ticker_mos_currency_live()" in _text11_owner)

acs.set_home_currency("check11b@example.com", "AUD")
_at11_nonowner = _fresh_url_only("check11b@example.com", switch_on=True, ticker="NOCCY")
_text11_nonowner = _page_text(_at11_nonowner)
check("a non-owner sees the same plain line...",
      "We couldn't confirm NOCCY's trading currency right now" in _text11_nonowner)
check("...but never the owner-only diagnostic caption",
      "[Owner-only diagnostic]" not in _text11_nonowner)
print("[b3_live_currency_resolution_fails] a real margin of safety with no confirmed "
      "currency shows a plain line for everyone and the exact failed condition for the "
      "owner only OK")

# ======================================================================
# CHECK 12/13 (Director's 6 Oct 2026 follow-up): the two remaining
# silent-miss branches inside _render_mos_view_table() itself - ticker
# currency known but doesn't match the current quote picker, and
# ticker currency known and equals the home/From currency (nothing to
# convert - a correct outcome, not a failure). Driven by presetting
# cr_base/cr_quote/cr_active_ticker directly in session_state (no URL
# jump involved), the only way to decouple the page's own base/quote
# resolution from these two specific branches inside the table itself.
# ======================================================================
_LIVE_RESULT_BY_TICKER["HSBA.L"] = {"mos": 8.0, "currency": "GBP"}  # matches neither AUD nor USD
_LIVE_RESULT_BY_TICKER["BHP.AX"] = {"mos": 15.0, "currency": "AUD"}  # the home-market currency


def _run_with_preset_state(email, cr_base, cr_quote, cr_active_ticker, switch_on=True):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False
import app
st.session_state["email_user"] = {email!r}
st.session_state["cr_base"] = {cr_base!r}
st.session_state["cr_quote"] = {cr_quote!r}
st.session_state["cr_active_ticker"] = {cr_active_ticker!r}
app.page_currency_risk()
"""
    if switch_on:
        os.environ["CURRENCY_VIEW_LIVE"] = "1"
    else:
        os.environ.pop("CURRENCY_VIEW_LIVE", None)
    with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
         mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS), \
         _mock_live_currency():
        at = AppTest.from_string(script, default_timeout=60)
        at.run()
    os.environ.pop("CURRENCY_VIEW_LIVE", None)
    assert not at.exception, f"_run_with_preset_state({email!r}) raised: {at.exception}"
    return at


acs.set_home_currency("check12@example.com", "AUD")
_at12 = _run_with_preset_state("check12@example.com", "AUD", "USD", "HSBA.L")
_text12 = _page_text(_at12)
check("ticker currency (GBP) known but matches neither base (AUD) nor quote (USD): no table",
      "Margin of safety," not in _text12)
check("the plain line names the ticker's own currency and says which To value shows it",
      "HSBA.L is priced in GBP - switch \"To\" to GBP to see its currency view." in _text12)

acs.set_home_currency("check13@example.com", "AUD")
_at13 = _run_with_preset_state("check13@example.com", "AUD", "USD", "BHP.AX")
_text13 = _page_text(_at13)
check("ticker currency (AUD) equals the home/From currency: no table (nothing to convert)",
      "Margin of safety," not in _text13)
check("the plain line says it's priced in the visitor's own home currency - a correct "
      "outcome, not an error",
      "BHP.AX is priced in your home currency." in _text13)
print("[b3_mismatch_and_same_currency_explained] a ticker whose currency doesn't match "
      "the current To picker, and one that matches the home currency exactly, both "
      "explain themselves in plain language instead of vanishing OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)
sys.exit(1 if failed else 0)
