"""
Director's overnight queue, TASK 2 (14 Oct 2026): "Make DAX, CAC 40,
AEX, SMI and OMX Stockholm 30 public on the Scanner, the same way
5711c3c made FTSE 100/250 public. They stay Top-200-excluded. Prove
select_top100_pool() output byte-identical and that Nikkei 225 and
TOPIX 500 stay private."

Mechanism (identical to commit 5711c3c, which made FTSE 100/FTSE
250/TSX Composite public): the five European universe names removed
from scan_store._DEFAULT_PRIVATE_UNIVERSES. Nikkei 225/TOPIX 500 are
the only two names left in that default - the sole still-private pair.

This file proves:
  a. is_private_universe() is now False for all five European
     universes, and a real saved scan for each is readable by a non-
     owner via the default-deny load_scan() path (never leaked before
     this commit, never gated after it).
  b. Nikkei 225 and TOPIX 500 stay private - both the flag and a
     default-deny read of a real saved scan correctly returning None.
  c. is_top200_excluded() is UNCHANGED (still True) for all 11 non-US/
     non-AU universes, including the five now-public European ones -
     privacy and Top 200 eligibility are independent levers, and this
     commit pulls only the privacy one, exactly as 5711c3c's own
     commit message states for its own round.
  d. select_top100_pool()'s own output is BYTE-IDENTICAL whether the
     five European universes are private (the pre-TASK-2 state,
     simulated via an explicit PRIVATE_UNIVERSES override reproducing
     the OLD default) or public (today's actual code default) - a
     synthetic DAX scan row that would otherwise be eligible is
     checked explicitly to prove it's excluded in BOTH states, via the
     SEPARATE is_top200_excluded() lever, never via privacy.
  e. S&P 500/ASX 200 are unaffected (still public, never touched by
     this default).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_overnight_task2_european_universes_public.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task2_european_universes_public_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import scan_store
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


_EUROPEAN_FIVE = ("DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30")

# ======================================================================
# CHECK 1-15: all five European universes are now public - the flag
# AND a real saved scan readable via the default-deny load_scan() path
# (never leaked to a non-owner caller before this commit; readable now).
# ======================================================================
for _u in _EUROPEAN_FIVE:
    check(f"is_private_universe({_u!r}) is False (made public, TASK 2)",
          scan_store.is_private_universe(_u) is False)
    scan_store.save_scan(_u, [{"Ticker": f"{_u[:3].upper()}1"}], source_label="test fixture")
    check(f"load_scan({_u!r}) (default allow_private=False) returns the real scan - "
          f"readable by a non-owner now",
          scan_store.load_scan(_u) is not None)

# ======================================================================
# CHECK 16-19: Nikkei 225/TOPIX 500 stay private - unaffected.
# ======================================================================
for _u in ("Nikkei 225", "TOPIX 500"):
    check(f"is_private_universe({_u!r}) is True (unaffected by TASK 2)",
          scan_store.is_private_universe(_u) is True)
    scan_store.save_scan(_u, [{"Ticker": f"{_u[:3].upper()}1"}], source_label="test fixture")
    check(f"load_scan({_u!r}) (default allow_private=False) returns None - still never "
          f"leaked to a non-owner caller",
          scan_store.load_scan(_u) is None)

# ======================================================================
# CHECK 20-30: is_top200_excluded() is unchanged for all 11 non-US/
# non-AU universes - privacy and Top 200 eligibility are independent
# levers, this commit pulls only the privacy one.
# ======================================================================
for _u in ("FTSE 100", "FTSE 250", "TSX 60", "TSX Composite",
           "Nikkei 225", "TOPIX 500", "DAX", "CAC 40", "AEX", "SMI",
           "OMX Stockholm 30"):
    check(f"is_top200_excluded({_u!r}) is still True - unaffected by this commit",
          te.is_top200_excluded(_u) is True)
check('is_top200_excluded("S&P 500") is still False (US, unaffected)',
      te.is_top200_excluded("S&P 500") is False)
check('is_top200_excluded("ASX 200") is still False (Australian, unaffected)',
      te.is_top200_excluded("ASX 200") is False)

# ======================================================================
# CHECK 31-33: select_top100_pool()'s own output is BYTE-IDENTICAL
# whether the five European universes are private (the pre-TASK-2
# state, simulated via an explicit override) or public (today's actual
# code default) - a DAX candidate that would otherwise be eligible on
# every OTHER count is proven excluded in BOTH states, via the
# SEPARATE is_top200_excluded() lever, never via privacy.
# ======================================================================
scan_store.save_scan("DAX", [{
    "Ticker": "WOULDPOOL.DE", "Company Name": "Would-Be Pool Co", "Price": 50.0,
    "Intrinsic Value": 90.0, "MOS %": 44.4, "Long Score": 95.0,
    "Quality": 90.0, "Psychology": 8.0, "Discovery (lite)": 60.0, "Moat": 80.0,
    "DCF Unreliable": False, "Growth Source": "analyst",
}], source_label="test fixture")

_OLD_DEFAULT_PRIVATE = (
    "Nikkei 225, TOPIX 500, DAX, CAC 40, AEX, SMI, OMX Stockholm 30"
)
os.environ["PRIVATE_UNIVERSES"] = _OLD_DEFAULT_PRIVATE
_pool_pre_task2 = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("PRIVATE_UNIVERSES", None)
_pool_post_task2 = te.select_top100_pool(log=lambda *a, **k: None)

check("select_top100_pool()'s own output is byte-identical whether the five European "
      "universes are private (pre-TASK-2 state) or public (today's actual code default)",
      json.dumps(_pool_pre_task2, sort_keys=True, default=str) ==
      json.dumps(_pool_post_task2, sort_keys=True, default=str))
check("WOULDPOOL.DE (DAX) did NOT enter the pool in the pre-TASK-2 (DAX private) state",
      not any(r["ticker"] == "WOULDPOOL.DE" for r in _pool_pre_task2))
check("WOULDPOOL.DE (DAX) STILL does not enter the pool now that DAX is public - "
      "is_top200_excluded('DAX') alone is enough to keep it out, with no privacy help",
      not any(r["ticker"] == "WOULDPOOL.DE" for r in _pool_post_task2))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
