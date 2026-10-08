"""
Proposal 1 of the Director's numbered fix-proposal round (8 Oct 2026,
instruction_health_fixes_chart_and_new_markets.md PART 3 STEP 3.2):
"store 'Growth Governor' on the row; the readiness panel reads it
instead of the proxy."

Covers:
  - nightly_scan.py now persists fcf_valuation_engine's real
    growth_governor value on every scanned row as "Growth Governor" -
    pure passthrough, never read by scoring/selection.
  - market_readiness_engine._is_at_growth_cap()/valuation_breakdown():
    a row carrying the new "Growth Governor" key reads it DIRECTLY
    ("Cap" -> counted, anything else, including None, -> not counted)
    - even a row whose "Growth Used"=="Growth Ceiling Used" by sheer
      coincidence is correctly NOT counted when its real governor says
      otherwise (the proxy's own false-positive risk, now fixed).
  - a row saved by an OLDER scan (the "Growth Governor" KEY itself
    absent, not just None) falls back to the ORIGINAL proxy - "Growth
    Used" == "Growth Ceiling Used" - so an old stored scan still gets
    a best-effort count, never silently zero.

Run: python3 tests/test_fixproposal1_growth_governor_stored.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_readiness_engine as mre

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
# CHECK 1-4: _is_at_growth_cap() - the new field, read directly.
# ======================================================================
check('new row, Growth Governor == "Cap" -> counted',
      mre._is_at_growth_cap({"Growth Governor": "Cap"}) is True)
check('new row, Growth Governor == "Yahoo" -> NOT counted',
      mre._is_at_growth_cap({"Growth Governor": "Yahoo"}) is False)
check('new row, Growth Governor is None (key present, genuinely uncapped) -> NOT counted '
      '(never falls through to the proxy just because the value happens to be None)',
      mre._is_at_growth_cap({"Growth Governor": None, "Growth Used": 0.05,
                              "Growth Ceiling Used": 0.05}) is False)
check('new row FALSE-POSITIVE FIX: Growth Used == Growth Ceiling Used by coincidence, but '
      'the real governor says "Manual" -> correctly NOT counted (the old proxy would have '
      'wrongly counted this)',
      mre._is_at_growth_cap({"Growth Governor": "Manual", "Growth Used": 0.08,
                              "Growth Ceiling Used": 0.08}) is False)

# ======================================================================
# CHECK 5-6: old row (no "Growth Governor" key at all) - falls back to
# the original proxy, never silently reads as zero.
# ======================================================================
check("old row, no Growth Governor key, Growth Used == Growth Ceiling Used -> "
      "falls back to the proxy, counted",
      mre._is_at_growth_cap({"Growth Used": 0.06, "Growth Ceiling Used": 0.06}) is True)
check("old row, no Growth Governor key, Growth Used != Growth Ceiling Used -> "
      "falls back to the proxy, NOT counted",
      mre._is_at_growth_cap({"Growth Used": 0.06, "Growth Ceiling Used": 0.09}) is False)

# ======================================================================
# CHECK 7-8: valuation_breakdown() end to end - a mixed set of old-
# shaped and new-shaped rows, counted correctly together.
# ======================================================================
_rows = [
    {"Ticker": "NEWCAP", "Growth Governor": "Cap", "Growth Used": 0.08, "Growth Ceiling Used": 0.08,
     "DCF Unreliable": False, "Growth Source": "analyst", "MOS %": 10.0},
    {"Ticker": "NEWNOTCAP", "Growth Governor": "Manual", "Growth Used": 0.08, "Growth Ceiling Used": 0.08,
     "DCF Unreliable": False, "Growth Source": "history", "MOS %": 5.0},
    {"Ticker": "OLDCAP", "Growth Used": 0.07, "Growth Ceiling Used": 0.07,
     "DCF Unreliable": False, "Growth Source": "info", "MOS %": 0.0},
]
_breakdown = mre.valuation_breakdown(_rows)
check("mixed old/new rows: at_growth_cap_count == 2 (NEWCAP via the real field, OLDCAP via "
      "the proxy fallback; NEWNOTCAP correctly excluded despite matching the proxy's own "
      "numeric coincidence)",
      _breakdown["at_growth_cap_count"] == 2)
check("valuation_breakdown()'s other fields are untouched by this change "
      "(dcf_unreliable_count/analyst_estimate_count/median_mos_pct still compute normally)",
      _breakdown["dcf_unreliable_count"] == 0 and _breakdown["analyst_estimate_count"] == 1
      and _breakdown["median_mos_pct"] == 5.0)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
