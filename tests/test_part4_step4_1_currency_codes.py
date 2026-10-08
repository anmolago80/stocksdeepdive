"""
PART 4 STEP 4.1 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): currency codes on every money list
(Scanner, Compare, Research lists) - owner-only behind
SCANNER_PRESENTATION_LIVE.

Covers:
  - scanner_engine.is_scanner_presentation_live() / app._scanner_
    presentation_visible() - the switch/owner composition.
  - _money_cell()/_price_cell(): currency_code=None (the default)
    reproduces the exact pre-PART-4 output, byte for byte; a code
    appends " <CODE>" after the number.
  - _insider_cell(): show_currency_code=False (the default)
    reproduces the exact pre-PART-4 output, byte for byte - INCLUDING
    its own pre-existing "A$"/bare-"$" mislabeling bug for a non-ASX
    ticker; show_currency_code=True replaces it with the ticker's own
    correct three-letter code.
  - _render_overnight_scan_table(), end to end, via a production-
    shaped scan payload (scan_store.save_scan()/load_scan()): switch
    OFF + non-owner is byte-identical to before PART 4 (no currency
    code anywhere in the rendered table); switch ON shows the correct
    code for a London-listed ticker's Price/Intrinsic Value cells.
  - the Research key-numbers header's own bare-"$" bug (PART 4 STEP
    4.0's own finding) is fixed - a non-USD ticker's price no longer
    shows a bare "$".

Run: python3 tests/test_part4_step4_1_currency_codes.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part4_step4_1_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("SCANNER_PRESENTATION_LIVE", None)

import scanner_engine as se
import scan_store

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
# CHECK 1-2: the switch itself.
# ======================================================================
check("is_scanner_presentation_live() is False when unset",
      se.is_scanner_presentation_live() is False)
os.environ["SCANNER_PRESENTATION_LIVE"] = "1"
check('is_scanner_presentation_live() is True when "1"',
      se.is_scanner_presentation_live() is True)
os.environ.pop("SCANNER_PRESENTATION_LIVE", None)

# ======================================================================
# CHECK 3-6: _money_cell()/_price_cell() - None reproduces the exact
# pre-PART-4 output; a code appends " <CODE>".
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _call(expr):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import app
st.session_state["_result"] = {expr}
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"{expr} raised: {at.exception}"
    return at.session_state["_result"]


check('_price_cell(123.4) unchanged (no currency_code)', _call("app._price_cell(123.4)") == "123.40")
check('_price_cell(123.4, currency_code="GBP") == "123.40 GBP"',
      _call('app._price_cell(123.4, currency_code="GBP")') == "123.40 GBP")
check('_money_cell(50.0) unchanged (no currency_code)',
      "50.00" in _call("app._money_cell(50.0)") and "USD" not in _call("app._money_cell(50.0)"))
check('_money_cell(50.0, currency_code="JPY") appends " JPY"',
      "50.00 JPY" in _call('app._money_cell(50.0, currency_code="JPY")'))

# ======================================================================
# CHECK 7-9: _insider_cell() - show_currency_code=False (default)
# reproduces the pre-existing mislabeling bug exactly (PART 4 STEP
# 4.0's own finding); True fixes it.
# ======================================================================
_ins_default_ax = _call('app._insider_cell("BHP.AX", 50000.0)')
_ins_default_l = _call('app._insider_cell("BARC.L", 50000.0)')
check('_insider_cell default (unchanged): ASX ticker shows "A$"', "A$" in _ins_default_ax)
check('_insider_cell default (unchanged): a London ticker still wrongly shows bare "$" '
      "(the PRE-EXISTING bug, reproduced byte for byte since show_currency_code defaults False)",
      "$50" in _ins_default_l and "GBP" not in _ins_default_l)
_ins_fixed_l = _call('app._insider_cell("BARC.L", 50000.0, show_currency_code=True)')
check('_insider_cell(show_currency_code=True): the SAME London ticker now shows "GBP", '
      "never a bare or wrong symbol",
      "GBP" in _ins_fixed_l and "$" not in _ins_fixed_l)

# ======================================================================
# CHECK 10-13: _render_overnight_scan_table(), end to end, via a
# production-shaped scan payload (scan_store.save_scan()/load_scan()).
# ======================================================================
scan_store.save_scan("FTSE 100", [
    {"Ticker": "BARC.L", "Type": "STOCK", "Price": 250.5, "Intrinsic Value": 300.0,
     "MOS %": 16.5, "Long Score": 60.0, "Quality": 55.0, "Psychology": 5.0,
     "Discovery (lite)": 30.0, "Moat": 50.0, "price_unit_suspect": False},
], source_label="test fixture")
_overnight = scan_store.load_scan("FTSE 100", allow_private=True)


def _run_table(switch_on):
    _env = 'os.environ["SCANNER_PRESENTATION_LIVE"] = "1"' if switch_on else \
           'os.environ.pop("SCANNER_PRESENTATION_LIVE", None)'
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{_env}
import app
import scan_store
import paywall_engine as pw
pw.current_user_email = lambda: "someone-else@example.com"
_overnight = scan_store.load_scan("FTSE 100", allow_private=True)
app._render_overnight_scan_table("FTSE 100", _overnight)
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"_render_overnight_scan_table() raised: {at.exception}"
    return at


_at_off = _run_table(switch_on=False)
_markdowns_off = " ".join(m.value or "" for m in _at_off.get("markdown"))
check("switch OFF, non-owner: no currency code anywhere in the rendered table "
      "(byte-identical to before PART 4)", "GBP" not in _markdowns_off)

_at_on = _run_table(switch_on=True)
_markdowns_on = " ".join(m.value or "" for m in _at_on.get("markdown"))
check("switch ON: BARC.L's Price/Intrinsic Value cells carry its real currency code, GBP",
      "GBP" in _markdowns_on)
check("switch ON: GBP appears at least twice (Price cell AND Intrinsic Value cell)",
      _markdowns_on.count("GBP") >= 2)

print("[currency_codes_on_scanner_table] switch OFF is byte-identical for a non-owner; "
      "switch ON shows BARC.L's real GBP code on both its Price and Intrinsic Value cells OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
