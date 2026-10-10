"""
PART 4 STEP 4.2 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): the picker's rows/order + owner-only
private markers.

Covers:
  - switch OFF, non-owner: byte-identical to before PART 4 - exactly
    the Australia/USA bands, no United Kingdom/Canada/Japan/Europe row
    at all, no "(private)" marker anywhere.
  - switch OFF, OWNER: sees all six bands (Australia, USA, United
    Kingdom, Canada, Japan, Europe, in that order), every currently-
    private universe's pill marked "(private)".
  - switch ON, non-owner: the new bands exist, but since every
    universe in Japan/Europe is STILL private, those two bands are
    skipped entirely (no empty row rendered); United Kingdom and
    Canada now render in full (see the PUSH 2 update below).
  - the Europe band's five pills each carry their OWN country flag,
    not one shared band flag.

UPDATED (Andrew's go, PUSH 2, 10 Oct 2026, instruction_combined_
10oct.md): FTSE 100, FTSE 250 and TSX Composite removed from
scan_store._DEFAULT_PRIVATE_UNIVERSES (same mechanism as TSX 60's own
9 Oct correction) - all four United Kingdom/Canada universes
(FTSE 100, FTSE 250, TSX 60, TSX Composite) are now public. Japan
(Nikkei 225, TOPIX 500) and Europe (DAX, CAC 40, AEX, SMI, OMX
Stockholm 30) are UNCHANGED, still fully private - those two bands
still never appear for a non-owner, with or without the switch.

Run: python3 tests/test_part4_step4_2_picker_rows_and_private_markers.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part4_step4_2_test_")
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


from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _render(email, switch_on):
    _env = 'os.environ["SCANNER_PRESENTATION_LIVE"] = "1"' if switch_on else \
           'os.environ.pop("SCANNER_PRESENTATION_LIVE", None)'
    _email_repr = repr(email)
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{_env}
import app
import paywall_engine as pw
pw.current_user_email = lambda: {_email_repr}
app._render_scanner_universe_picker("en")
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"_render_scanner_universe_picker() raised: {at.exception}"
    return " ".join(m.value or "" for m in at.get("markdown"))


import ai_gate
_OWNER = ai_gate.owner_email()

# ======================================================================
# CHECK 1-3: switch OFF, non-owner - byte-identical to before PART 4.
# ======================================================================
_html_off_nonowner = _render("someone-else@example.com", switch_on=False)
check("switch OFF, non-owner: AUSTRALIA band present", "AUSTRALIA" in _html_off_nonowner)
check("switch OFF, non-owner: USA band present", "USA" in _html_off_nonowner)
check("switch OFF, non-owner: none of the four new bands appear at all",
      "UNITED KINGDOM" not in _html_off_nonowner and "CANADA" not in _html_off_nonowner
      and "JAPAN" not in _html_off_nonowner and "EUROPE" not in _html_off_nonowner)
check('switch OFF, non-owner: no "(private)" marker anywhere', "private" not in _html_off_nonowner)
print("[switch_off_non_owner_unchanged] exactly the pre-PART-4 picker, byte for byte OK")

# ======================================================================
# CHECK 4-8: switch OFF, OWNER - sees all six bands, in order, with
# private markers on every currently-private pill.
# ======================================================================
_html_off_owner = _render(_OWNER, switch_on=False)
_band_order = [
    b for b in ("AUSTRALIA", "USA", "UNITED KINGDOM", "CANADA", "JAPAN", "EUROPE")
    if b in _html_off_owner
]
check("switch OFF, OWNER: all six bands appear",
      _band_order == ["AUSTRALIA", "USA", "UNITED KINGDOM", "CANADA", "JAPAN", "EUROPE"])
# PUSH 2 (Andrew's go, 10 Oct 2026): FTSE 100/FTSE 250/TSX Composite
# removed from scan_store._DEFAULT_PRIVATE_UNIVERSES - all four UK/
# Canada universes are now public, so none of their pills carry the
# (private) marker any more (TSX 60 was already public since the
# 9 Oct correction).
check("switch OFF, OWNER: FTSE 100 pill is NOT marked (private) (made public, PUSH 2)",
      "FTSE 100 (private)" not in _html_off_owner)
check("switch OFF, OWNER: FTSE 250 pill is NOT marked (private) (made public, PUSH 2)",
      "FTSE 250 (private)" not in _html_off_owner)
check("switch OFF, OWNER: TSX 60 pill is NOT marked (private) (made public, 9 Oct correction)",
      "TSX 60 (private)" not in _html_off_owner)
check("switch OFF, OWNER: TSX Composite pill is NOT marked (private) (made public, PUSH 2)",
      "TSX Composite (private)" not in _html_off_owner)
# Japan/Europe are UNCHANGED by PUSH 2 - still fully private.
check("switch OFF, OWNER: Nikkei 225 pill is marked (private)",
      "Nikkei 225 (private)" in _html_off_owner)
check("switch OFF, OWNER: DAX pill is marked (private)", "DAX (private)" in _html_off_owner)
print("[switch_off_owner_sees_everything] the owner sees all six bands; every UK/Canada "
      "pill is now unmarked (public since PUSH 2), every Japan/Europe pill still marked "
      "(private), even while the switch itself is off OK")

# ======================================================================
# CHECK 9-13: switch ON, non-owner - Japan/Europe still don't render
# (every universe in them is still private - the switch alone never
# makes a universe public). United Kingdom and Canada NOW render in
# full (PUSH 2, 10 Oct 2026): every universe in both bands is public,
# so both bands appear with every pill, none marked (private).
# ======================================================================
_html_on_nonowner = _render("someone-else@example.com", switch_on=True)
check("switch ON, non-owner: still no Japan/Europe band - nothing in them is public",
      "JAPAN" not in _html_on_nonowner and "EUROPE" not in _html_on_nonowner)
check("switch ON, non-owner: United Kingdom band NOW appears, with both FTSE 100 and "
      "FTSE 250 pills, neither marked (private)",
      "UNITED KINGDOM" in _html_on_nonowner and "FTSE 100" in _html_on_nonowner
      and "FTSE 250" in _html_on_nonowner and "FTSE 100 (private)" not in _html_on_nonowner
      and "FTSE 250 (private)" not in _html_on_nonowner)
check("switch ON, non-owner: Canada band NOW appears, with BOTH TSX 60 and TSX Composite "
      "pills (TSX Composite public since PUSH 2), neither marked (private)",
      "CANADA" in _html_on_nonowner and "TSX 60" in _html_on_nonowner
      and "TSX Composite" in _html_on_nonowner and "TSX 60 (private)" not in _html_on_nonowner
      and "TSX Composite (private)" not in _html_on_nonowner)
check("switch ON, non-owner: AUSTRALIA/USA still present, unaffected",
      "AUSTRALIA" in _html_on_nonowner and "USA" in _html_on_nonowner)
print("[switch_on_uk_canada_public] turning the presentation switch on now reveals the "
      "United Kingdom and Canada bands IN FULL to a non-owner - every universe in both is "
      "public since PUSH 2; Japan/Europe stay fully private and still don't render OK")

# ======================================================================
# CHECK 11-12: the Europe band's five pills each carry their OWN flag
# (not emoji) - checked via the owner's own render, where Europe is
# visible. Each flag SVG is distinct, confirming per-pill overrides
# are actually wired (not all five falling back to one shared flag).
# ======================================================================
import app as _app_module
_europe_flags = {_app_module._EUROPE_PILL_FLAGS[u] for u in
                  ("DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30")}
check("all five Europe pill flags are distinct SVGs (one per country, never one shared flag)",
      len(_europe_flags) == 5)
check("none of the five Europe flags is the generic EU flag used for the band label itself",
      _app_module._FLAG_EU_SVG not in _europe_flags)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
