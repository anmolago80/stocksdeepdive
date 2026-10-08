"""
Proposal 5 of the Director's numbered fix-proposal round (8 Oct 2026,
instruction_health_fixes_chart_and_new_markets.md PART 3 STEP 3.2):
"'n/a' reasons in plain words for visitors (e.g. 'price units could
not be confirmed'), keeping the technical reason for the owner only."

Covers:
  - app._na_reason_for_viewer(): None through unchanged; the real
    OWNER sees the exact stored technical string, unchanged; every
    other visitor sees the plain-words translation; the REAL
    production reason text (fundamentals_data._check_price_unit_
    guard()'s own stored string, "price unit could not be confirmed")
    maps to the Director's own exact suggested phrase; an unrecognised
    reason string falls back to the generic catch-all, never leaking
    verbatim to a non-owner.
  - i18n EN/ES strings exist and are genuinely distinct per language.

Run: python3 tests/test_fixproposal5_plain_na_reasons.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
TESTVOL = tempfile.mkdtemp(prefix="fixproposal5_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


def _call(expr, as_owner, lang="en"):
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
{_owner_setup}
import streamlit as st
st.session_state["lang"] = {lang!r}
import app
st.session_state["_result"] = {expr}
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"{expr} raised: {at.exception}"
    return at.session_state["_result"]


# ======================================================================
# CHECK 1: None through unchanged.
# ======================================================================
check("None -> None, for either owner or visitor",
      _call("app._na_reason_for_viewer(None)", as_owner=False) is None
      and _call("app._na_reason_for_viewer(None)", as_owner=True) is None)

# ======================================================================
# CHECK 2-3: the REAL production reason string.
# ======================================================================
_real_reason = "price unit could not be confirmed"
check("OWNER sees the exact stored technical string, unchanged",
      _call(f'app._na_reason_for_viewer({_real_reason!r})', as_owner=True) == _real_reason)
check('NON-owner sees the Director\'s own exact suggested phrase, '
      '"price units could not be confirmed" (plural "units", the known-reason mapping)',
      _call(f'app._na_reason_for_viewer({_real_reason!r})', as_owner=False)
      == "price units could not be confirmed")

# ======================================================================
# CHECK 4-5: an UNRECOGNISED reason string - falls back to the generic
# catch-all for a visitor, never leaks verbatim; the owner still sees
# it exactly as stored (whatever it is).
# ======================================================================
_unknown_reason = "some future technical reason nobody has mapped yet"
check("OWNER sees an unrecognised reason exactly as stored too (owner always sees the "
      "real thing, mapped or not)",
      _call(f'app._na_reason_for_viewer({_unknown_reason!r})', as_owner=True) == _unknown_reason)
check("NON-owner sees the GENERIC catch-all for an unrecognised reason - never the raw "
      "internal wording verbatim",
      _call(f'app._na_reason_for_viewer({_unknown_reason!r})', as_owner=False)
      == "this figure could not be confirmed")

# ======================================================================
# CHECK 6-7: Spanish, for a non-owner visitor.
# ======================================================================
check('NON-owner, lang="es": the real reason -> the Spanish plain phrase',
      _call(f'app._na_reason_for_viewer({_real_reason!r})', as_owner=False, lang="es")
      == "no se pudo confirmar la unidad del precio")
check('NON-owner, lang="es": an unrecognised reason -> the Spanish generic catch-all',
      _call(f'app._na_reason_for_viewer({_unknown_reason!r})', as_owner=False, lang="es")
      == "no se pudo confirmar esta cifra")

# ======================================================================
# CHECK 8: the EN/ES strings are genuinely distinct (never the same
# literal text copy-pasted across languages).
# ======================================================================
import i18n
check("EN/ES price-unit phrases are genuinely distinct strings",
      i18n.t("scanner.na_reason_price_unit", "en") != i18n.t("scanner.na_reason_price_unit", "es"))
check("EN/ES generic-catch-all phrases are genuinely distinct strings",
      i18n.t("scanner.na_reason_generic", "en") != i18n.t("scanner.na_reason_generic", "es"))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
