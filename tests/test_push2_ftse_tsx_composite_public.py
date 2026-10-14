"""
PUSH 2 (Andrew's go, 10 Oct 2026, instruction_combined_10oct.md):
"FTSE 100, FTSE 250 and TSX Composite become public" - same single-
lever mechanism as TSX 60's own 9 Oct correction (scan_store.
_DEFAULT_PRIVATE_UNIVERSES). All four United Kingdom/Canada universes
(FTSE 100, FTSE 250, TSX 60, TSX Composite) are now public by the
code default; Japan (Nikkei 225, TOPIX 500) and the five European
universes (DAX, CAC 40, AEX, SMI, OMX Stockholm 30) stay private,
unchanged.

UPDATED (Director's overnight queue TASK 2, 14 Oct 2026): DAX, CAC 40,
AEX, SMI and OMX Stockholm 30 themselves went public that day too,
same single lever - CHECK 2 and CHECK 4 below are updated for that
(Japan is now the only still-private band); CHECK 3's Top 200
exclusion requirement is untouched (it was always privacy-independent)
and still covers all 11 universes including the now-public five.

Covers the Director's own six numbered proof requirements (as they
stood at PUSH 2 - item 2's own five European names are now covered by
the TASK 2 update note above instead):
  1. Non-owners can read FTSE 100, FTSE 250, TSX Composite and TSX 60.
  2. Nikkei 225, TOPIX 500 stay private.
  3. The Top 200 exclusion is unchanged (every non-US/non-AU universe
     still excluded from the pool), and the scoring-request fingerprint
     (f00c69f5...) passes.
  4. With SCANNER_PRESENTATION_LIVE=1, a non-owner sees the United
     Kingdom band (FTSE 100, FTSE 250) and the Canada band (TSX 60,
     TSX Composite), with no "(private)" labels and no Japan band
     (Europe now also appears in full, since TASK 2 - see the updated
     CHECK 4 below).
  5. Fund rows (Is Fund) show the "Fund — company valuation does not
     apply" reason to non-owners, never a bare N/A.
  6. S&P 500 / ASX 200 output is byte-identical.

Checks 3/4 are also exercised, in far more depth, by the existing
tests/test_nextround_item3_top200_exclusion.py and tests/
test_part4_step4_2_picker_rows_and_private_markers.py (both updated
alongside this commit for the same PUSH 2 change) - this file's own
versions are a self-contained, minimal restatement so this single
file proves every one of the Director's six points on its own,
without depending on another file's fixtures.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_push2_ftse_tsx_composite_public.py
"""
import hashlib
import inspect
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="push2_ftse_tsx_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)
os.environ.pop("SCANNER_PRESENTATION_LIVE", None)

import scan_store
import scanner_engine
import top100_engine as te
import nightly_scan

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
# CHECK 1: non-owners can read FTSE 100, FTSE 250, TSX Composite and
# TSX 60 - both the boolean flag AND an actual default-deny read
# (scan_store.load_scan(universe) - allow_private=False, the exact
# call every public reader uses) of a real saved scan.
# ======================================================================
for _u in ("FTSE 100", "FTSE 250", "TSX 60", "TSX Composite",
           # Director's overnight queue TASK 2 (14 Oct 2026): these five
           # European universes went public the same single way - added
           # to this loop rather than duplicating it, since CHECK 1's own
           # proof (flag + a default-deny read of a real saved scan
           # returning the row) is exactly the proof TASK 2 needs too.
           "DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30"):
    check(f"is_private_universe({_u!r}) is False", scan_store.is_private_universe(_u) is False)
    scan_store.save_scan(_u, [{"Ticker": f"{_u[:3].upper()}1"}], source_label="test fixture")
    check(f"load_scan({_u!r}) (default allow_private=False) returns the real scan - "
          f"readable by a non-owner",
          scan_store.load_scan(_u) is not None)

# ======================================================================
# CHECK 2: Nikkei 225, TOPIX 500 stay private - both the flag AND a
# default-deny read of a real saved scan correctly returning None
# (never leaking rows to a non-owner caller). UPDATED (Director's
# overnight queue TASK 2, 14 Oct 2026): DAX/CAC 40/AEX/SMI/OMX
# Stockholm 30 moved up into CHECK 1 above - they're public now too -
# leaving Japan as the only still-private pair.
# ======================================================================
for _u in ("Nikkei 225", "TOPIX 500"):
    check(f"is_private_universe({_u!r}) is True", scan_store.is_private_universe(_u) is True)
    scan_store.save_scan(_u, [{"Ticker": f"{_u[:3].upper()}1"}], source_label="test fixture")
    check(f"load_scan({_u!r}) (default allow_private=False) returns None - "
          f"never leaked to a non-owner caller",
          scan_store.load_scan(_u) is None)
    check(f"load_scan({_u!r}, allow_private=True) (the owner-only path) still finds it",
          scan_store.load_scan(_u, allow_private=True) is not None)

# ======================================================================
# CHECK 3: Top 200 exclusion is unchanged - every non-US/non-AU
# universe (all 11 named above, public or private) is still excluded
# from the pool via top100_engine.is_top200_excluded()'s own SEPARATE,
# privacy-independent derivation (scanner_engine.AUSTRALIA_UNIVERSES/
# USA_UNIVERSES) - plus the scoring-request fingerprint, proving
# _request_params()/the batch entrant-building path is untouched.
# ======================================================================
for _u in ("FTSE 100", "FTSE 250", "TSX 60", "TSX Composite",
           "Nikkei 225", "TOPIX 500", "DAX", "CAC 40", "AEX", "SMI",
           "OMX Stockholm 30"):
    check(f"is_top200_excluded({_u!r}) is still True - privacy and Top 200 "
          f"eligibility are independent levers, this commit only pulls privacy",
          te.is_top200_excluded(_u) is True)
for _u in ("S&P 500", "ASX 200"):
    check(f"is_top200_excluded({_u!r}) is still False (US/Australian, unaffected)",
          te.is_top200_excluded(_u) is False)

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
check(f"scoring-request fingerprint unchanged by this PUSH 2 diff (still {EXPECTED_FINGERPRINT})",
      _fp == EXPECTED_FINGERPRINT)

# ======================================================================
# CHECK 4: with SCANNER_PRESENTATION_LIVE=1, a non-owner sees the
# United Kingdom band (FTSE 100, FTSE 250) and the Canada band (TSX
# 60, TSX Composite), with no "(private)" labels and no Japan/Europe
# bands. Full end-to-end render via AppTest, same technique as
# tests/test_part4_step4_2_picker_rows_and_private_markers.py.
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ["SCANNER_PRESENTATION_LIVE"] = "1"
import app
import paywall_engine as pw
pw.current_user_email = lambda: "someone-else@example.com"
app._render_scanner_universe_picker("en")
"""
_at = AppTest.from_string(script, default_timeout=60)
_at.run()
assert not _at.exception, f"_render_scanner_universe_picker() raised: {_at.exception}"
_html = " ".join(m.value or "" for m in _at.get("markdown"))

check("United Kingdom band appears for a non-owner", "UNITED KINGDOM" in _html)
check("FTSE 100 pill appears, not marked (private)",
      "FTSE 100" in _html and "FTSE 100 (private)" not in _html)
check("FTSE 250 pill appears, not marked (private)",
      "FTSE 250" in _html and "FTSE 250 (private)" not in _html)
check("Canada band appears for a non-owner", "CANADA" in _html)
check("TSX 60 pill appears, not marked (private)",
      "TSX 60" in _html and "TSX 60 (private)" not in _html)
check("TSX Composite pill appears, not marked (private)",
      "TSX Composite" in _html and "TSX Composite (private)" not in _html)
check("no Japan band - Nikkei 225/TOPIX 500 are both still private",
      "JAPAN" not in _html)
# UPDATED (Director's overnight queue TASK 2, 14 Oct 2026): the five
# European universes went public the same day - the Europe band now
# appears for a non-owner too, in full, exactly like United Kingdom/
# Canada above (no (private) marker on any of its five pills).
check("Europe band NOW appears for a non-owner (made public, TASK 2)",
      "EUROPE" in _html)
check("no (private) marker appears anywhere in this non-owner render "
      "(a non-owner's own visible-pill filter never lets a private universe's pill "
      "reach the labeling step at all, regardless of which bands exist)",
      "private" not in _html)
for _eu_universe in ("DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30"):
    check(f"{_eu_universe} pill appears, not marked (private)",
          _eu_universe in _html and f"{_eu_universe} (private)" not in _html)

# ======================================================================
# CHECK 5: fund rows (Is Fund) show the exact "Fund — company
# valuation does not apply" reason to non-owners, never a bare N/A.
# First, source-inspection proof that nightly_scan.py's own fund logic
# still sets this exact pair of fields (the data side, unchanged by
# this commit); then an end-to-end render proof that app.py's Scanner
# table now surfaces it (the PUSH 2 follow-up fix in this same
# commit - app._render_overnight_scan_table()'s own Intrinsic Value
# cell).
# ======================================================================
_ns_src = inspect.getsource(nightly_scan)
check('nightly_scan.py still sets row["Fund Reason"] = "Fund — company valuation '
      'does not apply" on the Is Fund branch',
      '"Fund Reason"] = "Fund — company valuation does not apply"' in _ns_src)

scan_store.save_scan("FTSE 100", [
    {"Ticker": "FUND1.L", "Type": "FUND", "Company Name": "Some Trust PLC",
     "Price": 100.0, "Intrinsic Value": None, "MOS %": None, "Long Score": 50.0,
     "Quality": 50.0, "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
     "Is Fund": True, "Fund Reason": "Fund — company valuation does not apply",
     "Valuation": "N/A", "Signal": "WATCHLIST", "DCF Unreliable": False,
     "price_unit_suspect": False},
], source_label="test fixture")
_fund_script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ.pop("SCANNER_PRESENTATION_LIVE", None)
import app
import scan_store
import paywall_engine as pw
pw.current_user_email = lambda: "someone-else@example.com"
_overnight_fund = scan_store.load_scan("FTSE 100")
app._render_overnight_scan_table("FTSE 100", _overnight_fund)
"""
_at_fund = AppTest.from_string(_fund_script, default_timeout=60)
_at_fund.run()
assert not _at_fund.exception, f"_render_overnight_scan_table() raised: {_at_fund.exception}"
_fund_html = " ".join(m.value or "" for m in _at_fund.get("markdown"))
check("non-owner sees the exact Fund Reason text next to N/A, never a bare N/A",
      ">N/A <span" in _fund_html
      and "(Fund — company valuation does not apply)" in _fund_html)

# ======================================================================
# CHECK 6: S&P 500 / ASX 200 output is byte-identical - source-
# inspection proof that the only two places in the production code
# path that call scan_store.is_private_universe() key it on the
# SPECIFIC universe being processed in that loop iteration/call
# (nightly_scan.py's reprice path, scheduler_engine.py's snapshot-
# build gate) - never on the full private SET - so is_private_
# universe("S&P 500")/is_private_universe("ASX 200") (always False,
# unaffected by this commit) is the only thing that can matter for
# those two universes; nothing about this commit's change to FTSE
# 100/FTSE 250/TSX Composite can cross-affect a different universe's
# own scan output.
# ======================================================================
check('is_private_universe("S&P 500") is still False (always public, untouched)',
      scan_store.is_private_universe("S&P 500") is False)
check('is_private_universe("ASX 200") is still False (always public, untouched)',
      scan_store.is_private_universe("ASX 200") is False)
check("nightly_scan.py's own is_private_universe() call site is keyed on the "
      "per-call `universe` argument, never the full private set",
      "not scan_store.is_private_universe(universe)" in _ns_src)
import scheduler_engine as _se_module
_se_src = inspect.getsource(_se_module)
check("scheduler_engine.py's own is_private_universe() call site is likewise keyed "
      "on the per-iteration `universe` variable",
      "scan_store.is_private_universe(universe)" in _se_src)
check("analyze_ticker_lite()/run_universe_scan() (the functions that actually "
      "compute S&P 500/ASX 200 scan rows) never reference is_private_universe "
      "or _DEFAULT_PRIVATE_UNIVERSES at all",
      "is_private_universe" not in inspect.getsource(nightly_scan.analyze_ticker_lite)
      and "is_private_universe" not in inspect.getsource(nightly_scan.run_universe_scan))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
