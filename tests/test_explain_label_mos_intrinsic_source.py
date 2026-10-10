"""Explain-label fix (Director-directed, 9 Oct 2026, Director's review
item 3): the MOS Explain popover's context text
(app._explain_context_text, metric_key == "margin_of_safety") hard-coded
"Intrinsic Value (DCF base case)" regardless of the real
dd["intrinsic_source"] - found while answering Task 4 Q1 of
instruction_scanner_chips_trusts_japan_sectors.md (a P/E-blend name like
BN.TO would misleadingly show "DCF base case"). Elsewhere in the same
file (_dd_copy_text, ~line 5762) the same concept is already correctly
conditional: `method = dd.get("intrinsic_source") or "DCF"`.

FIX (wording only, no number changes): the MOS line now reads
"Intrinsic Value (DCF base case)" only when dd["intrinsic_source"] ==
"dcf", and "Intrinsic Value (earnings-based (P/E) estimate)" otherwise
(pe-blend, or any other non-dcf source/missing source - same
not-DCF bucket).

PROOF OF "WORDING ONLY, NO NUMBER CHANGE": for a DCF-valued name
(intrinsic_source == "dcf", the case for every S&P 500/ASX 200 row),
the rendered text is byte-identical to the pre-fix hard-coded line.

Run: python3 tests/test_explain_label_mos_intrinsic_source.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

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


def _dd(intrinsic_source):
    return {
        "ticker": "TEST",
        "name": "Test Co",
        "price": 100.0,
        "currency": "USD",
        "intrinsic_value": 123.45,
        "intrinsic_source": intrinsic_source,
        "mos": 12.3,
        "valuation": "Undervalued",
    }


# DCF case - byte-identical to the old hard-coded line (S&P 500/ASX 200
# are always DCF-valued, so their Explain text must not change at all).
_text_dcf = app._explain_context_text(_dd("dcf"), "margin_of_safety")
_old_hardcoded_line = "Intrinsic Value (DCF base case): 123.45 USD"
check("DCF case: line byte-identical to the pre-fix hard-coded text", _old_hardcoded_line in _text_dcf)

# pe-blend case - must now say earnings-based (P/E) estimate, not DCF base case.
_text_pe = app._explain_context_text(_dd("pe-blend"), "margin_of_safety")
check("pe-blend case: says 'earnings-based (P/E) estimate'",
      "Intrinsic Value (earnings-based (P/E) estimate): 123.45 USD" in _text_pe)
check("pe-blend case: does NOT say 'DCF base case'", "DCF base case" not in _text_pe)

# Missing/None intrinsic_source - falls into the same not-DCF bucket, never DCF base case.
_text_none = app._explain_context_text(_dd(None), "margin_of_safety")
check("missing intrinsic_source: does NOT say 'DCF base case'", "DCF base case" not in _text_none)
check("missing intrinsic_source: falls back to earnings-based (P/E) estimate wording",
      "earnings-based (P/E) estimate" in _text_none)

# The number itself never changes regardless of source - wording only.
check("intrinsic_value number is identical across dcf/pe-blend/None",
      "123.45 USD" in _text_dcf and "123.45 USD" in _text_pe and "123.45 USD" in _text_none)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
