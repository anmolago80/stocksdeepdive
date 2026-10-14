"""
Director's overnight queue, TASK 1 (14 Oct 2026): "Two European
symbols (root cause verified in production logs)." Rescan 2026-10-14
04:17-04:18 UTC logged:
  "[symbols] CAC 40: no Yahoo price for MT-AS.PA (wiki \"MT-AS.PA\")"
  "[symbols] DAX: no Yahoo price for AIR-PA.DE (wiki \"AIR-PA.DE\")"

Root cause: CAC 40's own Wikipedia constituent table lists
ArcelorMittal by its REAL Yahoo symbol, "MT.AS" (Euronext Amsterdam -
its actual primary listing), not a bare CAC-40-local code; DAX's own
table lists Airbus by its real Yahoo symbol, "AIR.PA" (Euronext
Paris). The OLD to_yahoo_symbol() only recognised the CURRENT
exchange's own suffix as "already Yahoo-shaped" - so "MT.AS" under
EURONEXT_PARIS fell into the ordinary stem transform: hyphenate the
inner dot ("MT-AS"), then append EURONEXT_PARIS's own ".PA" suffix on
top - "MT-AS.PA", not a real Yahoo symbol. Same mechanism for
"AIR.PA" under XETRA -> "AIR-PA.DE".

Fix (symbol_mapping.py, general rule, no SYMBOL_OVERRIDES entry
needed): the idempotency check now covers EVERY suffix this module
ever appends (_ALL_YAHOO_SUFFIXES), not just the current exchange's
own - a raw symbol already ending in any known Yahoo suffix is
already a complete, valid symbol for whatever market it actually
trades on, and is returned unchanged regardless of which exchange's
universe it was scraped from.

This file proves:
  a. Both real bug symbols now map to themselves, unchanged.
  b. Every symbol in to_yahoo_symbol()'s own pre-existing worked
     examples (already-documented, real tickers from before this
     task - Rolls-Royce, BT Group, Brookfield, Rogers, CT REIT, Toyota,
     SAP, LVMH, ASML, Nestle, Volvo) maps BYTE-IDENTICALLY to before
     this fix - the general rule only changes behaviour for a symbol
     that already carries a FOREIGN exchange's suffix; every ordinary
     symbol is completely unaffected.
  c. Every existing idempotent-same-exchange case (a raw symbol
     already suffixed for its OWN exchange) still returns unchanged,
     same as before - the general rule subsumes, never narrows, the
     old per-exchange check.
  d. SYMBOL_OVERRIDES and the unknown-exchange ValueError are
     untouched.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC OR THE FUNCTION'S OWN PRE-
EXISTING DOCUMENTED EXAMPLES - no ticker list was scraped or invented
for this file; this sandbox has no outbound network access.

Run: python3 tests/test_director_overnight_task1_cross_exchange_symbol.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import symbol_mapping as sm

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
# CHECK 1-2: the two real bug symbols from the rescan's own log lines.
# ======================================================================
check('to_yahoo_symbol("MT.AS", "EURONEXT_PARIS") == "MT.AS" (ArcelorMittal\'s '
      "real symbol is Euronext Amsterdam's, not CAC 40's - no longer MT-AS.PA)",
      sm.to_yahoo_symbol("MT.AS", "EURONEXT_PARIS") == "MT.AS")
check('to_yahoo_symbol("AIR.PA", "XETRA") == "AIR.PA" (Airbus\'s real symbol is '
      "Euronext Paris's, not DAX's - no longer AIR-PA.DE)",
      sm.to_yahoo_symbol("AIR.PA", "XETRA") == "AIR.PA")
# Case-insensitivity and lowercase suffixes behave the same way.
check('to_yahoo_symbol("mt.as", "EURONEXT_PARIS") == "mt.as" (case preserved, '
      "still recognised)",
      sm.to_yahoo_symbol("mt.as", "EURONEXT_PARIS") == "mt.as")


# ======================================================================
# CHECK 3-16: every symbol in the function's own pre-existing worked
# examples maps BYTE-IDENTICALLY to before this fix - none of these
# carry a foreign exchange's suffix, so the general rule changes
# nothing about them.
# ======================================================================
_PRE_EXISTING_EXAMPLES = [
    (("RR.", "LSE"), "RR.L"),
    (("BT.A", "LSE"), "BT-A.L"),
    (("BAM.A", "TSX"), "BAM-A.TO"),
    (("RCI.B", "TSX"), "RCI-B.TO"),
    (("CAR.UN", "TSX"), "CAR-UN.TO"),
    (("RR.L", "LSE"), "RR.L"),              # idempotent, own exchange
    (("BAM-A.TO", "TSX"), "BAM-A.TO"),      # idempotent, own exchange
    (("7203", "TSE"), "7203.T"),
    (("130A", "TSE"), "130A.T"),
    (("7203.T", "TSE"), "7203.T"),          # idempotent, own exchange
    (("7203.t", "TSE"), "7203.t"),          # idempotent, case-insensitive
    (("SAP", "XETRA"), "SAP.DE"),
    (("SAP.DE", "XETRA"), "SAP.DE"),        # idempotent, own exchange
    (("MC", "EURONEXT_PARIS"), "MC.PA"),
    (("ASML", "EURONEXT_AMSTERDAM"), "ASML.AS"),
    (("NESN", "SIX"), "NESN.SW"),
    (("VOLV.B", "NASDAQ_STOCKHOLM"), "VOLV-B.ST"),
]
for (raw, exchange), expected in _PRE_EXISTING_EXAMPLES:
    actual = sm.to_yahoo_symbol(raw, exchange)
    check(f"byte-identical: to_yahoo_symbol({raw!r}, {exchange!r}) == {expected!r} "
          "(pre-existing documented example, unaffected by this fix)",
          actual == expected)


# ======================================================================
# CHECK 17: overrides and the unknown-exchange ValueError are untouched
# by this fix (both checked/raised exactly as before).
# ======================================================================
check('overrides still checked first, before any transform',
      sm.to_yahoo_symbol("WEIRD", "LSE", overrides={"WEIRD": "WEIRD2.L"}) == "WEIRD2.L")
try:
    sm.to_yahoo_symbol("X", "NYSE")
    check("unknown exchange still raises ValueError", False)
except ValueError:
    check("unknown exchange still raises ValueError", True)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
