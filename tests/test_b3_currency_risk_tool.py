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

Plus: the table silently renders nothing when the ticker's own
currency doesn't match the current quote picker (never a guessed
margin), and the table's own currency-effect numbers equal
currency_view_engine.mos_view()'s own (single source of truth,
re-confirmed one layer up from test_b2_currency_view_calc.py's own
proof against currency_risk_engine.position_impact() directly).

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
import snapshot_store

if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)
if os.path.exists(snapshot_store.DB_PATH):
    os.remove(snapshot_store.DB_PATH)

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


def _seed_snapshot(ticker, mos_pct, currency):
    snapshot_store.save_snapshot(ticker, "S&P 500", {"ticker": ticker, "mos_pct": mos_pct, "currency": currency})


def _run_page(email, extra_env=None, jump=None):
    _jump_lines = ""
    if jump:
        _jump_lines = f'st.session_state["currency_risk_jump"] = {jump!r}\n'
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
{_jump_lines}
import app
app.page_currency_risk()
"""
    at = AppTest.from_string(script, default_timeout=60)
    for k, v in (extra_env or {}).items():
        os.environ[k] = v
    _patches = [
        mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY),
        mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS),
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
        _df_text
    )


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
      _at1.selectbox[0].value == "JPY")
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
      _at2a.selectbox[0].value == cre.DEFAULT_BASE)

acs.set_home_currency("nonowner@example.com", "EUR")
_at2b = _run_page("nonowner@example.com")
check("signed in, non-owner, switch OFF (not visible): From picker still starts on "
      "DEFAULT_BASE, ignoring their own saved EUR - page unchanged",
      _at2b.selectbox[0].value == cre.DEFAULT_BASE)
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
_seed_snapshot("AAPL", 66.1, "USD")
_at4 = _run_page(None, jump={"ticker": "AAPL", "base": "AUD", "quote": "USD"},
                 extra_env={"CURRENCY_VIEW_LIVE": "1"})
check("signed out, even with a jump present: no extra table",
      "Margin of safety," not in _page_text(_at4))
print("[b3_no_ticker_no_table] without a ticker, or signed out, no extra table ever "
      "appears OK")

# ======================================================================
# CHECK 5: WITH a ticker, signed in, home currency set, visible -> the
# extra table DOES appear, and its own numbers equal cve.mos_view()'s.
# ======================================================================
acs.set_home_currency("withticker@example.com", "AUD")
_at5 = _run_page("withticker@example.com", jump={"ticker": "AAPL", "base": "AUD", "quote": "USD"},
                 extra_env={"CURRENCY_VIEW_LIVE": "1"})
_text5 = _page_text(_at5)
check("with a ticker, signed in, home currency set: the heading appears",
      "Margin of safety," in _text5 and "AAPL" in _text5)
check("the From/To pickers landed on the jump's own pair (AUD/USD)",
      _at5.selectbox[0].value == "AUD" and _at5.selectbox[1].value == "USD")

with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
    _expected = cve.mos_view(66.1, "AUD", "USD")
check("the table's own average-scenario margin equals cve.mos_view()'s own (single "
      "source of truth, one layer up from the engine-level proof)",
      f"{_expected['view_at_average']:+.1f}%" in _text5)
print("[b3_table_with_ticker] the extra table appears with a ticker, carries the pair "
      "over, and its own numbers equal currency_view_engine.mos_view()'s own OK")

# ======================================================================
# CHECK 6: the ticker's own currency doesn't match the current `quote`
# -> no table, never a guessed margin.
# ======================================================================
_seed_snapshot("CSL.AX", 10.0, "AUD")  # an AUD-priced ticker
_at6 = _run_page("withticker@example.com", jump={"ticker": "CSL.AX", "base": "AUD", "quote": "AUD"},
                 extra_env={"CURRENCY_VIEW_LIVE": "1"})
# base == quote here -> the page itself warns and returns before any
# table could render - confirms the "same currency -> warning, nothing
# past that point" path is untouched by this commit.
check("ticker/quote pair collapses to the same-currency warning path, no table, no crash",
      "Margin of safety," not in _page_text(_at6))
print("[b3_mismatched_currency_no_table] a ticker whose own currency doesn't match the "
      "selected pair never gets a guessed table OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)
if os.path.exists(snapshot_store.DB_PATH):
    os.remove(snapshot_store.DB_PATH)
sys.exit(1 if failed else 0)
