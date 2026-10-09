"""Proves git revert of e017807 actually restored TSX 60/TSX Composite to
private, ahead of pushing the revert to main (Director-directed, 9 Oct
2026). e017807 had removed TSX 60 from scan_store._DEFAULT_PRIVATE_
UNIVERSES; Andrew's separate go for that change never arrived, so the
Director ordered a revert-on-top (no history rewrite) rather than a
reorder. This test is the proof step the Director asked for before the
revert is pushed.

Run: python3 tests/test_revert_e017807_tsx60_private_again.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


with mock.patch.dict(os.environ, {}, clear=False):
    os.environ.pop("PRIVATE_UNIVERSES", None)
    check("TSX 60 is private again", scan_store.is_private_universe("TSX 60") is True)
    check("TSX Composite is private again", scan_store.is_private_universe("TSX Composite") is True)

check("_DEFAULT_PRIVATE_UNIVERSES contains TSX 60", "TSX 60" in scan_store._DEFAULT_PRIVATE_UNIVERSES)
check("_DEFAULT_PRIVATE_UNIVERSES contains TSX Composite", "TSX Composite" in scan_store._DEFAULT_PRIVATE_UNIVERSES)

_TESTVOL = tempfile.mkdtemp(prefix="revert_e017807_test_")
with mock.patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": _TESTVOL}):
    scan_store.save_scan("TSX 60", [{"Ticker": "RY.TO"}], "test fixture")
    check("non-owner cannot read TSX 60 (allow_private defaults to False)",
          scan_store.load_scan("TSX 60") is None)
    check("owner can still read TSX 60 (allow_private=True, per-read enforcement)",
          scan_store.load_scan("TSX 60", allow_private=True) is not None)

for _name in ("FTSE 100", "FTSE 250", "Nikkei 225", "TOPIX 500", "DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30"):
    check(f"{_name} still private, unchanged", scan_store.is_private_universe(_name) is True)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
