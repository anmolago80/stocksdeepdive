"""
PART 4 STEP 4.3 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): "a withheld value shows 'n/a' with
its reason. A column that does not exist for a market shows 'not
available for this market'."

Covers:
  - _money_cell(): na_reason=None (the default) reproduces the bare
    "N/A" every existing caller already shows, byte for byte; a
    reason appends it alongside "N/A", and ONLY when the value is
    actually withheld (never alongside a real number).
  - _insider_cell(): show_currency_code=False (default, byte-
    identical) still shows the bare "-" for a ticker insider_engine.py
    genuinely has a source for, with nothing parsed yet; True shows
    "not available for this market" instead, for a ticker insider_
    engine.py has NO source for at all (.L/.TO/.T/.DE/.PA/.AS/.SW/.ST)
    - never confusing "not tracked for this market" with "tracked but
    nothing found yet".
  - end to end via _render_overnight_scan_table(), a production-shaped
    fixture (scan_store.save_scan()/load_scan()): a price-unit-guard-
    flagged London ticker's withheld Intrinsic Value shows its own
    stored reason, switch ON only; switch OFF is byte-identical.

Run: python3 tests/test_part4_step4_3_na_with_reason.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part4_step4_3_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("SCANNER_PRESENTATION_LIVE", None)

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


# ======================================================================
# CHECK 1-3: _money_cell()'s own na_reason.
# ======================================================================
check("_money_cell(None) unchanged: bare 'N/A', no reason",
      _call("app._money_cell(None)") == "<span style='color:#8aa0b8;'>N/A</span>")
_with_reason = _call('app._money_cell(None, na_reason="price unit unconfirmed")')
check('_money_cell(None, na_reason=...) shows "N/A" AND the reason',
      "N/A" in _with_reason and "price unit unconfirmed" in _with_reason)
check("_money_cell(50.0, na_reason=...) NEVER shows a reason when a real value renders "
      "(a reason only ever explains an ABSENT number)",
      "reason" not in _call('app._money_cell(50.0, na_reason="should never appear")').lower())

# ======================================================================
# CHECK 4-6: _insider_cell()'s "not available for this market".
# ======================================================================
check('default (unchanged): a London ticker with no value still shows bare "-", '
      "never the new market-gap text",
      _call('app._insider_cell("BARC.L", None)') == "<span style='color:#8aa0b8;'>-</span>")
check('show_currency_code=True: a London ticker (NO insider source at all) shows '
      '"not available for this market"',
      "not available for this market" in _call(
          'app._insider_cell("BARC.L", None, show_currency_code=True)'))
check('show_currency_code=True: an ASX ticker (insider_engine DOES cover .AX) with no '
      'value yet still shows the bare "-", never the market-gap text - "nothing parsed '
      'yet" is not "not tracked for this market"',
      _call('app._insider_cell("BHP.AX", None, show_currency_code=True)')
      == "<span style='color:#8aa0b8;'>-</span>")

# ======================================================================
# CHECK 7-9: end to end, via a production-shaped scan payload.
# ======================================================================
scan_store.save_scan("FTSE 100", [
    {"Ticker": "GUARDCO.L", "Type": "STOCK", "Price": 100.0, "Intrinsic Value": None,
     "MOS %": None, "Long Score": 40.0, "Quality": 50.0, "Psychology": 5.0,
     "Discovery (lite)": 30.0, "Moat": 50.0, "price_unit_suspect": True,
     "price_unit_suspect_reason": "[units] GUARDCO.L GBp->GBP ratio=0.01 SUSPECT"},
], source_label="test fixture")


def _run_table(switch_on, as_owner=False):
    _env = 'os.environ["SCANNER_PRESENTATION_LIVE"] = "1"' if switch_on else \
           'os.environ.pop("SCANNER_PRESENTATION_LIVE", None)'
    _owner_setup = (
        'import ai_gate\nimport paywall_engine as pw\n'
        'pw.current_user_email = lambda: ai_gate.owner_email()\n'
    ) if as_owner else (
        'import paywall_engine as pw\npw.current_user_email = lambda: "someone-else@example.com"\n'
    )
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{_env}
{_owner_setup}
import app
import scan_store
_overnight = scan_store.load_scan("FTSE 100", allow_private=True)
app._render_overnight_scan_table("FTSE 100", _overnight)
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"_render_overnight_scan_table() raised: {at.exception}"
    return " ".join(m.value or "" for m in at.get("markdown"))


_html_off = _run_table(switch_on=False)
check("switch OFF: no reason text anywhere (byte-identical to before PART 4)",
      "SUSPECT" not in _html_off and "ratio=" not in _html_off)

# Proposal 5 of the Director's numbered fix-proposal round (8 Oct 2026):
# "'n/a' reasons in plain words for visitors, keeping the technical
# reason for the owner only" - a NON-owner now sees the plain/generic
# translation, never the raw stored string (this fixture's own reason
# text is deliberately NOT one _NA_REASON_PLAIN_I18N_KEY recognises, so
# it must fall back to the GENERIC catch-all, never leak verbatim).
_html_on_visitor = _run_table(switch_on=True, as_owner=False)
check("switch ON, NON-owner: shows the plain generic catch-all phrase, never the raw "
      "stored reason text",
      "this figure could not be confirmed" in _html_on_visitor
      and "SUSPECT" not in _html_on_visitor and "ratio=" not in _html_on_visitor)

_html_on_owner = _run_table(switch_on=True, as_owner=True)
# html.escape() correctly turns the reason's own "->" into "-&gt;" for HTML
# safety - checking the escaped form, not the raw string, is the correct
# proof that this renders safely AND carries the real reason text.
check("switch ON, OWNER: GUARDCO.L's withheld Intrinsic Value shows its own EXACT stored "
      "price-unit-guard reason (HTML-escaped), unchanged by Proposal 5",
      "GBp-&gt;GBP ratio=0.01 SUSPECT" in _html_on_owner)

print("[na_with_reason_end_to_end] a price-unit-guard-flagged ticker's withheld value "
      "shows the OWNER its own stored reason; every other visitor sees a plain-words "
      "translation instead; switch OFF is byte-identical for everyone OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
