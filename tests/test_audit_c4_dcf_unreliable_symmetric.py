"""Audit fix C4 (Fable finding V4, 10 Oct 2026, instruction_combined_
10oct.md PART C): resolver_engine.dcf_looks_unreliable() only ever
checked the HIGH side (intrinsic value > 3x price) - a DCF landing
far enough BELOW price (less than a third of it) is just as much a
sign of a bad-data artifact, but was never flagged at all.

New `symmetric=False` (default) parameter - False preserves the
EXACT original high-side-only behaviour for every existing caller,
in particular top100_engine.py's own pool-exclusion checks
(_is_dcf_unreliable()'s stored-flag read, and run_nightly()'s own
direct re-check against the row's CURRENT price) - this task's own
instruction is explicit: "Pool selection unchanged: don't extend the
Top 200 exclusion." True adds the symmetric low-side check.

nightly_scan.analyze_ticker_lite() now stores a SEPARATE, additive,
display-only field, "DCF Unreliable (Symmetric)" (symmetric=True) -
the pre-existing "DCF Unreliable" field top100_engine.py's pool
exclusion reads is untouched, still computed with the default
symmetric=False. The new field is exported in snapshot_store.
_PUBLIC_FIELD_MAP as "dcf_unreliable_symmetric" - the pre-existing
"DCF Unreliable" field was never exported and still isn't.

Expected effect (owner's own report): the flag only - no IV/MOS/
Long Score/pool-selection numbers change anywhere. A ticker whose IV
looks suspiciously low (e.g. an overstated share count elsewhere
depressing per-share IV - see C2's own RIO.AX-shaped fixture for a
real example of exactly that failure mode) now gets a visible,
exportable low-side warning it never had before.

Run: python3 tests/test_audit_c4_dcf_unreliable_symmetric.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolver_engine
import nightly_scan
import snapshot_store
import top100_engine

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
# CHECK 1: default behaviour (symmetric=False, every existing caller)
# is byte-identical to before this fix - high side still flags, low
# side does NOT (the exact gap this fix closes, but NOT by default).
# ======================================================================
check("high side (iv=400, price=100, 4x) still flags True by default - "
      "unchanged pre-existing behaviour",
      resolver_engine.dcf_looks_unreliable(400.0, 100.0) is True)
check("low side (iv=20, price=100, 0.2x, under a third) does NOT flag "
      "by default - regression: the default must stay high-side-only",
      resolver_engine.dcf_looks_unreliable(20.0, 100.0) is False)
check("a reasonable value (iv=150, price=100, 1.5x) never flags, "
      "default or symmetric",
      resolver_engine.dcf_looks_unreliable(150.0, 100.0) is False
      and resolver_engine.dcf_looks_unreliable(150.0, 100.0, symmetric=True) is False)


# ======================================================================
# CHECK 2: symmetric=True - the new low-side case.
# ======================================================================
check("low side (iv=20, price=100, 0.2x) DOES flag with symmetric=True "
      "- the new V4 check",
      resolver_engine.dcf_looks_unreliable(20.0, 100.0, symmetric=True) is True)
check("exactly a third (iv=33.33, price=100) does NOT flag (strict <, "
      "matching the existing strict > on the high side)",
      resolver_engine.dcf_looks_unreliable(100.0 / 3.0, 100.0, symmetric=True) is False)
check("just under a third (iv=33.0, price=100) DOES flag",
      resolver_engine.dcf_looks_unreliable(33.0, 100.0, symmetric=True) is True)
check("high side is UNCHANGED when symmetric=True too - still flags",
      resolver_engine.dcf_looks_unreliable(400.0, 100.0, symmetric=True) is True)
check("DCF_UNRELIABLE_LOW_LABEL is the exact text the owner specified",
      resolver_engine.DCF_UNRELIABLE_LOW_LABEL
      == "Model unreliable: value less than a third of price")


# ======================================================================
# CHECK 3: pool-selection call sites in top100_engine.py are completely
# unaffected - source inspection confirms neither call site passes
# symmetric=True, so a low-side-unreliable row is NOT excluded by
# either the stored-flag path or the current-price re-check path.
# ======================================================================
_te_src = inspect.getsource(top100_engine)
check("top100_engine.py's live re-check call site does NOT pass "
      "symmetric=True (2-arg call, unchanged)",
      "resolver_engine.dcf_looks_unreliable(\n                    row.get(\"Intrinsic Value\"), row.get(\"Price\"))" in _te_src)
check("_is_dcf_unreliable() still reads ONLY the pre-existing stored "
      "\"DCF Unreliable\" field (not the new Symmetric one)",
      'return bool(row.get("DCF Unreliable"))' in _te_src
      and 'row.get("DCF Unreliable (Symmetric)")' not in _te_src)

# Behavioural confirmation: _is_dcf_unreliable() itself, called on a
# row that is low-side-unreliable (would be flagged by the NEW
# symmetric check) but whose pre-existing stored flag is False.
_low_side_row = {"DCF Unreliable": False, "DCF Unreliable (Symmetric)": True,
                  "Intrinsic Value": 20.0, "Price": 100.0}
check("a row that's low-side-unreliable (would be flagged by the new "
      "symmetric check) is NOT excluded by _is_dcf_unreliable() - pool "
      "selection genuinely unextended",
      top100_engine._is_dcf_unreliable(_low_side_row) is False)


# ======================================================================
# CHECK 4: nightly_scan.py wiring - the new field is stored alongside
# the untouched pre-existing one. Source inspection (same pattern
# already used this session for similar wiring proofs) - driving the
# full analyze_ticker_lite() pipeline needs heavy yfinance mocking
# with no behavioural change of its own left to prove beyond "the new
# key is set", which source inspection answers directly.
# ======================================================================
_ns_src = inspect.getsource(nightly_scan.analyze_ticker_lite)
check('nightly_scan.py stores the pre-existing "DCF Unreliable" field '
      "with the function's default (symmetric=False) call, unchanged",
      '"DCF Unreliable": dcf_looks_unreliable(intrinsic, current_price),' in _ns_src)
check('nightly_scan.py now ALSO stores "DCF Unreliable (Symmetric)" '
      "via an explicit symmetric=True call",
      '"DCF Unreliable (Symmetric)": dcf_looks_unreliable(intrinsic, current_price, symmetric=True),'
      in _ns_src)


# ======================================================================
# CHECK 5: _PUBLIC_FIELD_MAP export - the new field is exposed via
# public_view(); the pre-existing "DCF Unreliable" field (which feeds
# pool selection) was never exported and still isn't.
# ======================================================================
check('"DCF Unreliable (Symmetric)" is in _PUBLIC_FIELD_MAP',
      snapshot_store._PUBLIC_FIELD_MAP.get("DCF Unreliable (Symmetric)") == "dcf_unreliable_symmetric")
check('the pre-existing "DCF Unreliable" field is still NOT exported '
      "(unchanged - this fix only adds the new symmetric key)",
      "DCF Unreliable" not in snapshot_store._PUBLIC_FIELD_MAP)

_row_low_side = {"Intrinsic Value": 20.0, "DCF Unreliable (Symmetric)": True}
_row_no_flag = {"Intrinsic Value": 150.0}
_out_low = snapshot_store.public_view(_row_low_side)
_out_none = snapshot_store.public_view(_row_no_flag)
check("public_view() exposes the symmetric flag as True when the row "
      "has it set",
      _out_low.get("dcf_unreliable_symmetric") is True)
check("public_view() omits the public key entirely when the row "
      "predates this fix (never fabricated) - same convention as "
      "every other optional field",
      "dcf_unreliable_symmetric" not in _out_none)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
