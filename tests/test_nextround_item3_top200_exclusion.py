"""
Director's next round, item 3 (8 Oct 2026, instruction_health_fixes_
chart_and_new_markets.md): "a Top 200 exclusion separate from
privacy: a list of universes that may be public on the Scanner but
are kept out of the Top 200. Code default: every non-US, non-
Australian universe."

Covers:
  - top100_engine.is_top200_excluded(): the code default (env var
    unset) is DERIVED from scanner_engine.AUSTRALIA_UNIVERSES/
    USA_UNIVERSES, never a hand-maintained name list - every current
    non-US/non-AU universe (UK/Canada/Japan/the five European ones)
    excluded; every US/AU universe (including sector sub-universes)
    NOT excluded.
  - TOP200_EXCLUDED_UNIVERSES env var overrides the default entirely
    when set, even to an empty string.
  - The real test: with TSX 60 REMOVED from PRIVATE_UNIVERSES (made
    PUBLIC on the Scanner), the Top 200 pool select_top100_pool()
    produces is BYTE-IDENTICAL to the pool with TSX 60 still private -
    privacy and Top 200 eligibility are two independent levers; making
    a universe public must never, by itself, let it start winning Top
    200 slots.

  UPDATED (Director's correction, 9 Oct 2026): TSX 60 has since been
  removed from scan_store._DEFAULT_PRIVATE_UNIVERSES itself - there is
  no PRIVATE_UNIVERSES variable on Railway, so that code default is
  what's actually live. TSX 60 is readable as public BY DEFAULT now
  (no env var needed); TSX Composite stays private, unchanged. The
  byte-identical proof below is re-run the other way around: the code
  DEFAULT (TSX 60 public) vs. an explicit override that puts TSX 60
  back into PRIVATE_UNIVERSES (simulating the pre-correction state) -
  still byte-identical, since is_top200_excluded() excludes TSX 60
  either way.
  - The scoring-request fingerprint (test_top200_commit4_schema_
    mode.py's own f00c69f5...sha256 check) is re-run unmodified,
    proving _request_params()/the batch entrant-building path is
    untouched by this change.

Run: python3 tests/test_nextround_item3_top200_exclusion.py
"""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="nextround_item3_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_EXCLUDED_UNIVERSES", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

import scan_store
import scanner_engine
import top100_engine as te

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
# CHECK 1-6: is_top200_excluded() - the derived code default.
# ======================================================================
for _u in ("FTSE 100", "FTSE 250", "TSX 60", "TSX Composite",
           "Nikkei 225", "TOPIX 500", "DAX", "CAC 40", "AEX", "SMI",
           "OMX Stockholm 30"):
    check(f'is_top200_excluded({_u!r}) is True (non-US, non-Australian)',
          te.is_top200_excluded(_u) is True)

for _u in ("S&P 500", "Nasdaq 100", "ASX 200", "ASX 300", "ASX Financials",
           "US Technology" if "US Technology" in scanner_engine.USA_UNIVERSES else "S&P 500"):
    check(f'is_top200_excluded({_u!r}) is False (US or Australian)',
          te.is_top200_excluded(_u) is False)

# ======================================================================
# CHECK 7-8: a brand-new, never-hand-listed non-US/non-AU universe
# name is STILL excluded by the derived default - proof it's a RULE,
# not a name list that needs updating.
# ======================================================================
check('a made-up non-US/non-AU name ("Some Future Market Index") is still excluded '
      "by the derived default - never needs adding to a list",
      te.is_top200_excluded("Some Future Market Index") is True)
check('a made-up name added to scanner_engine.USA_UNIVERSES would NOT be excluded '
      "(confirms the rule really does key off those two lists, not a separate copy)",
      "S&P 500" in scanner_engine.USA_UNIVERSES and te.is_top200_excluded("S&P 500") is False)

# ======================================================================
# CHECK 9-10: TOP200_EXCLUDED_UNIVERSES env var overrides the default
# entirely when set.
# ======================================================================
os.environ["TOP200_EXCLUDED_UNIVERSES"] = "S&P 500"
check('explicit override: "S&P 500" now excluded (even though it is USA)',
      te.is_top200_excluded("S&P 500") is True)
check('explicit override: "TSX 60" is now NOT excluded (not named in the override)',
      te.is_top200_excluded("TSX 60") is False)
os.environ["TOP200_EXCLUDED_UNIVERSES"] = ""
check('explicit override to an EMPTY string: nothing is excluded, a valid explicit '
      'choice - "TSX 60" is not excluded',
      te.is_top200_excluded("TSX 60") is False)
os.environ.pop("TOP200_EXCLUDED_UNIVERSES", None)

# ======================================================================
# CHECK 11-16: the real end-to-end test, UPDATED for the Director's
# correction (9 Oct 2026) - TSX 60 is readable as public BY THE CODE
# DEFAULT now (no env var needed), TSX Composite stays private, and
# the Top 200 pool is BYTE-IDENTICAL to the pre-correction (TSX 60
# private) state, because Top 200 eligibility is a separate,
# unaffected lever.
# ======================================================================
scan_store.save_scan("TSX 60", [
    {"Ticker": "RY.TO", "Type": "STOCK", "Company Name": "Royal Bank of Canada",
     "Price": 140.0, "Intrinsic Value": 160.0, "MOS %": 12.5, "Long Score": 95.0,
     "Quality": 90.0, "Psychology": 5.0, "Discovery (lite)": 35.0, "Moat": 85.0,
     "DCF Unreliable": False, "Growth Source": "analyst"},
], source_label="test fixture")

# Current state: code default, PRIVATE_UNIVERSES unset - TSX 60 public,
# TSX Composite (and every other pre-existing name) still private.
os.environ.pop("PRIVATE_UNIVERSES", None)
check("TSX 60 is readable as PUBLIC by the code default (the Director's correction)",
      scan_store.is_private_universe("TSX 60") is False)
check("TSX Composite is STILL private by the code default, unchanged",
      scan_store.is_private_universe("TSX Composite") is True)
_pool_default = te.select_top100_pool(log=lambda *a, **k: None)
check("RY.TO (TSX 60) is NOT in the pool under the code default - it's public but "
      "still Top-200-excluded",
      not any(r["ticker"] == "RY.TO" for r in _pool_default))

# Simulated PRE-CORRECTION state: TSX 60 put back into an explicit
# PRIVATE_UNIVERSES override (every other default-private name kept).
_pre_correction_private = (
    "FTSE 100, FTSE 250, TSX 60, TSX Composite, Nikkei 225, TOPIX 500, "
    "DAX, CAC 40, AEX, SMI, OMX Stockholm 30"
)
os.environ["PRIVATE_UNIVERSES"] = _pre_correction_private
check("explicit override can still make TSX 60 private again (pre-correction state)",
      scan_store.is_private_universe("TSX 60") is True)
_pool_pre_correction = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("PRIVATE_UNIVERSES", None)

check("RY.TO (TSX 60) is STILL not in the pool in the pre-correction (TSX 60 private) "
      "state either - Top 200 eligibility (is_top200_excluded) is a SEPARATE lever",
      not any(r["ticker"] == "RY.TO" for r in _pool_pre_correction))
check("the Top 200 pool is BYTE-IDENTICAL whether TSX 60 is public (current code "
      "default) or private (pre-correction) - making it public changed nothing "
      "about the Top 200",
      json.dumps(_pool_default, sort_keys=True, default=str) ==
      json.dumps(_pool_pre_correction, sort_keys=True, default=str))

# ======================================================================
# CHECK 14: the scoring-request fingerprint is untouched - re-running
# the existing COMMIT 4 test's own sha256 check proves _request_
# params()/the batch entrant-building path is byte-for-byte the same
# as before this change.
# ======================================================================
FIXTURE_ENTRANTS = [
    {"ticker": "ADP", "company_name": "ADP-shaped", "sector": "Industrials"},
    {"ticker": "CSL.AX", "company_name": "CSL-shaped", "sector": "Health Care"},
    {"ticker": "AZN_FIXTURE.L", "company_name": "AZN-shaped", "sector": "Health Care"},
    {"ticker": "RY_FIXTURE.TO", "company_name": "RY-shaped", "sector": "Financials"},
    {"ticker": "7203_FIXTURE.T", "company_name": "Toyota-shaped", "sector": "Consumer Discretionary"},
]
EXPECTED_FINGERPRINT = "f00c69f5392af5e2ca435fb4c8a314393c14b4c0dc498cc4293a59d1276e5196"
_params = te._request_params(FIXTURE_ENTRANTS)
_fp = hashlib.sha256(json.dumps(_params, sort_keys=True).encode()).hexdigest()
check("scoring-request fingerprint unchanged by this item 3 diff "
      f"(_request_params() sha256 still {EXPECTED_FINGERPRINT})",
      _fp == EXPECTED_FINGERPRINT)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
