"""
SECTION B, COMMIT B1 of instruction_top200_amendments_and_currency_view.md
(5 Oct 2026, Director-directed): the home-currency account setting and
its account-bar control.

Covers exactly the task's own listed tests:
  - set, change and read back
  - unset for a new account
  - not shown when signed out
  - not shown to a non-owner while the switch (CURRENCY_VIEW_LIVE) is
    unset

Plus: an invalid currency is rejected (never silently stored), the
allowed list is exactly currency_risk_engine.CURRENCIES (no second,
invented list), and the real app._render_currency_picker widget itself
(options, default selection, persisting a change) under AppTest.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_b1_home_currency.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="b1_home_currency_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import account_currency_store as acs
import currency_risk_engine as cre
import currency_view_engine as cve
import ai_gate

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


# ======================================================================
# CHECK 1: store - set, change, read back; unset for a new account.
# ======================================================================
check("a brand-new account reads back None (never a guessed default)",
      acs.get_home_currency("new1@example.com") is None)

acs.set_home_currency("user1@example.com", "AUD")
check("set: read-back matches", acs.get_home_currency("user1@example.com") == "AUD")

acs.set_home_currency("user1@example.com", "usd")  # lower-case input, normalised
check("change: read-back matches the NEW value", acs.get_home_currency("user1@example.com") == "USD")
check("a different account is untouched by another account's change",
      acs.get_home_currency("new1@example.com") is None)

try:
    acs.set_home_currency("user1@example.com", "XYZ")
    check("an invalid currency raises ValueError", False)
except ValueError:
    check("an invalid currency raises ValueError", True)
check("the rejected write never overwrote the existing value",
      acs.get_home_currency("user1@example.com") == "USD")

try:
    acs.set_home_currency("", "AUD")
    check("a falsy email raises ValueError", False)
except ValueError:
    check("a falsy email raises ValueError", True)

check("CURRENCIES is the one and only allowed list (same object the picker/tool use)",
      set(cre.CURRENCIES) == {"AUD", "USD", "EUR", "GBP", "JPY", "NZD", "CAD", "SGD"})
print("[b1_store_set_change_read_back] set/change/read-back/unset/invalid-rejection all OK")


# ======================================================================
# CHECK 2: currency_view_engine.is_currency_view_live() / visible_to().
# ======================================================================
os.environ.pop("CURRENCY_VIEW_LIVE", None)
check("switch unset reads OFF", cve.is_currency_view_live() is False)
os.environ["CURRENCY_VIEW_LIVE"] = "1"
check("switch '1' reads ON", cve.is_currency_view_live() is True)
os.environ["CURRENCY_VIEW_LIVE"] = "on"
check("switch 'on' reads ON", cve.is_currency_view_live() is True)
os.environ.pop("CURRENCY_VIEW_LIVE", None)

_OWNER = "anmolago@hotmail.com"
check("switch OFF, owner -> visible", cve.visible_to(_OWNER, ai_gate.is_owner) is True)
check("switch OFF, non-owner -> NOT visible", cve.visible_to("someone@example.com", ai_gate.is_owner) is False)
os.environ["CURRENCY_VIEW_LIVE"] = "1"
check("switch ON, non-owner -> visible", cve.visible_to("someone@example.com", ai_gate.is_owner) is True)
check("switch ON, owner -> visible", cve.visible_to(_OWNER, ai_gate.is_owner) is True)
os.environ.pop("CURRENCY_VIEW_LIVE", None)
check("no email at all -> never visible, any switch state", cve.visible_to(None, ai_gate.is_owner) is False)
print("[b1_switch_and_visibility] is_currency_view_live()/visible_to() OK for every combination")


# ======================================================================
# CHECK 3: wiring - paywall_engine.render_account_bar() only ever calls
# extra_widget4 from a signed-in branch; never when signed out.
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_account_bar(signed_in, extra_env=None):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw

pw._email_auth_available = lambda: True
pw.PAYWALL_ENABLED = False

def _tracked():
    st.session_state["_fired"] = True
    st.caption("CURRENCY_WIDGET_RENDERED")

st.session_state.setdefault("_fired", False)
pw.render_account_bar(extra_widget4=_tracked, lang="en")
"""
    at = AppTest.from_string(script, default_timeout=30)
    if signed_in:
        at.session_state["email_user"] = signed_in
    for k, v in (extra_env or {}).items():
        os.environ[k] = v
    try:
        at.run()
    finally:
        for k in (extra_env or {}):
            os.environ.pop(k, None)
    assert not at.exception, f"render_account_bar() raised: {at.exception}"
    return bool(at.session_state.get("_fired"))


os.environ.pop("CURRENCY_VIEW_LIVE", None)
check("signed OUT: extra_widget4 is never invoked, regardless of the switch",
      _run_account_bar(signed_in=None) is False)
check("signed OUT, switch ON: still never invoked (sign-in gates it first)",
      _run_account_bar(signed_in=None, extra_env={"CURRENCY_VIEW_LIVE": "1"}) is False)
check("signed IN, switch OFF: extra_widget4 IS invoked (paywall_engine's own job is only "
      "routing-by-sign-in-state; the owner-vs-non-owner gate lives inside the widget itself, "
      "exercised separately in CHECK 2 and CHECK 4)",
      _run_account_bar(signed_in="whoever@example.com") is True)
print("[b1_signed_out_never_renders] render_account_bar() never calls the home-currency "
      "widget for a signed-out visitor, under either switch state OK")


# ======================================================================
# CHECK 4: the real app._render_currency_picker widget - options,
# default selection reflecting a saved value, owner-vs-non-owner gating
# while the switch is OFF, and persisting a change.
# ======================================================================
def _run_currency_picker(email, extra_env=None):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
import app
app._render_currency_picker(key_prefix="t")
"""
    at = AppTest.from_string(script, default_timeout=60)
    for k, v in (extra_env or {}).items():
        os.environ[k] = v
    try:
        at.run()
    finally:
        for k in (extra_env or {}):
            os.environ.pop(k, None)
    assert not at.exception, f"_render_currency_picker() raised: {at.exception}"
    return at


os.environ.pop("CURRENCY_VIEW_LIVE", None)
_at_nonowner_off = _run_currency_picker("someone-else@example.com")
check("switch OFF, non-owner: nothing rendered (no selectbox at all)",
      len(_at_nonowner_off.selectbox) == 0)

_at_owner_off = _run_currency_picker(_OWNER)
check("switch OFF, owner: the control DOES render", len(_at_owner_off.selectbox) == 1)
check("options are exactly currency_risk_engine.CURRENCIES, no second/invented list",
      _at_owner_off.selectbox[0].options == cre.CURRENCIES)

acs.set_home_currency(_OWNER, "AUD")
_at_owner_set = _run_currency_picker(_OWNER)
check("a previously-saved home currency is pre-selected as the widget's own value",
      _at_owner_set.selectbox[0].value == "AUD")

_at_owner_set.selectbox[0].select("JPY").run()
check("changing the selection persists to the store immediately",
      acs.get_home_currency(_OWNER) == "JPY")

acs.set_home_currency(_OWNER, "AUD")  # restore for determinism of any later re-run
os.environ["CURRENCY_VIEW_LIVE"] = "1"
_at_nonowner_on = _run_currency_picker("someone-else@example.com")
check("switch ON: a non-owner now sees the control too",
      len(_at_nonowner_on.selectbox) == 1)
os.environ.pop("CURRENCY_VIEW_LIVE", None)
print("[b1_picker_widget] app._render_currency_picker() OK - owner-only while the switch is "
      "OFF, CURRENCIES-only options, pre-selects a saved value, persists a change")


print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)
sys.exit(1 if failed else 0)
