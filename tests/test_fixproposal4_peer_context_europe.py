"""
Proposal 4 of the Director's numbered fix-proposal round (8 Oct 2026,
instruction_health_fixes_chart_and_new_markets.md PART 3 STEP 3.2):
"peer_context.market_for(): add .DE Germany, .PA France, .AS
Netherlands, .SW Switzerland, .ST Sweden, with their own peer groups;
a European ticker must never be ranked against USA peers."

Covers:
  - market_for(): each of the 5 new suffixes resolves to its own,
    correctly-named country - never falling through to the old "else
    USA" default (the real bug this closes: a German/French/Dutch/
    Swiss/Swedish ticker used to be peer-ranked against S&P 500/
    Nasdaq 100/... - all PUBLIC universes).
  - each new country gets its OWN one-universe peer group (_MARKET_
    PRIORITY/_MARKET_ALL_UNIVERSES) - never lumped into one shared
    "Europe" bucket (a German company's own peers are DAX members,
    not a Swedish one).
  - compute() end to end: since DAX/CAC 40/AEX/SMI/OMX Stockholm 30
    are ALL still private today, the existing "every universe for
    this market is private -> no peer context" check already covers
    these five automatically - a German ticker gets {"available":
    False, "reason": "private_market"}, never a bogus USA-peer
    comparison and never a crash (proves the fix is safe even before
    any European universe ever goes public).
  - pre-existing markets (Australia/UK/Canada/Japan/USA) are
    byte-for-byte unaffected.

Run: python3 tests/test_fixproposal4_peer_context_europe.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="fixproposal4_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import peer_context

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
# CHECK 1-5: market_for() - each new suffix, its own correct country.
# ======================================================================
check('market_for("SAP.DE") == "Germany"', peer_context.market_for("SAP.DE") == "Germany")
check('market_for("MC.PA") == "France"', peer_context.market_for("MC.PA") == "France")
check('market_for("ASML.AS") == "Netherlands"', peer_context.market_for("ASML.AS") == "Netherlands")
check('market_for("NESN.SW") == "Switzerland"', peer_context.market_for("NESN.SW") == "Switzerland")
check('market_for("VOLV-B.ST") == "Sweden"', peer_context.market_for("VOLV-B.ST") == "Sweden")

# ======================================================================
# CHECK 6-10: never falls through to USA any more for these suffixes
# (the real bug this fix closes).
# ======================================================================
for _suffix, _ticker in [(".DE", "SAP.DE"), (".PA", "MC.PA"), (".AS", "ASML.AS"),
                          (".SW", "NESN.SW"), (".ST", "VOLV-B.ST")]:
    check(f'{_ticker} no longer resolves to "USA" (the old bug)',
          peer_context.market_for(_ticker) != "USA")

# ======================================================================
# CHECK 11-13: each country has its OWN one-universe peer group, never
# a shared "Europe" bucket.
# ======================================================================
_de_pool = set(peer_context._MARKET_PRIORITY["Germany"])
_fr_pool = set(peer_context._MARKET_PRIORITY["France"])
_se_pool = set(peer_context._MARKET_PRIORITY["Sweden"])
check("Germany's own peer pool is exactly {'DAX'}, not shared with France/Sweden",
      _de_pool == {"DAX"} and _de_pool.isdisjoint(_fr_pool) and _de_pool.isdisjoint(_se_pool))
check("every one of the 5 new countries appears in BOTH _MARKET_PRIORITY and "
      "_MARKET_ALL_UNIVERSES (compute()'s own two lookups)",
      all(m in peer_context._MARKET_PRIORITY and m in peer_context._MARKET_ALL_UNIVERSES
          for m in ("Germany", "France", "Netherlands", "Switzerland", "Sweden")))
check("all 5 new peer pools are mutually exclusive (one country, one universe each)",
      len({u for pool in (peer_context._MARKET_PRIORITY[m] for m in
                           ("Germany", "France", "Netherlands", "Switzerland", "Sweden"))
           for u in pool}) == 5)

# ======================================================================
# CHECK 14-15: compute() end to end - still-private markets correctly
# short-circuit to "private_market", never a bogus USA comparison,
# never a crash (KeyError on _MARKET_PRIORITY/_MARKET_ALL_UNIVERSES
# would be the failure mode if this fix were only half-wired).
# ======================================================================
_result = peer_context.compute("SAP.DE")
check('compute("SAP.DE") == {"available": False, "reason": "private_market"} - DAX is '
      "still private today, so this is correctly withheld, never a crash and never a "
      "bogus comparison against S&P 500",
      _result == {"available": False, "reason": "private_market"})
_result2 = peer_context.compute("VOLV-B.ST")
check('compute("VOLV-B.ST") == {"available": False, "reason": "private_market"} too '
      "(OMX Stockholm 30 is also still private)",
      _result2 == {"available": False, "reason": "private_market"})

# ======================================================================
# CHECK 16: pre-existing markets are byte-for-byte unaffected.
# ======================================================================
check("pre-existing markets unaffected: BHP.AX->Australia, BARC.L->United Kingdom, "
      "RY.TO->Canada, 7203.T->Japan, AAPL->USA",
      peer_context.market_for("BHP.AX") == "Australia"
      and peer_context.market_for("BARC.L") == "United Kingdom"
      and peer_context.market_for("RY.TO") == "Canada"
      and peer_context.market_for("7203.T") == "Japan"
      and peer_context.market_for("AAPL") == "USA")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
