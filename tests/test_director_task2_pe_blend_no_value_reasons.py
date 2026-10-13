"""
Director's work queue, TASK 2 (13 Oct 2026): "pe-blend rows with no
Intrinsic Value still render a bare '-' on /s/<ticker> because no
reason string exists for that case." Enumerates EVERY condition under
which a pe-blend row's own Intrinsic Value lands as None, mapped to
one plain sentence each, in snapshot_render._intrinsic_value_hint()'s
own documented order (matching resolver_engine.resolve_intrinsic_
value()/nightly_scan.py's REAL execution order, not an independently
invented one):

  1. is_fund (pre-existing, overnight queue TASK 2, 12 Oct 2026) -
     unchanged, the stored "Fund Reason" string.
  2. price_unit_suspect - nightly_scan.py zeroes "Intrinsic Value"
     AFTER resolve_intrinsic_value() already ran, so this can fire on
     a row whose own "Intrinsic Source" still reads "pe-blend" (or
     even "dcf") from before that override - checked BEFORE the
     pe-blend-specific case below, matching that real order. Reuses
     the EXISTING stored price_unit_suspect_reason text.
  3. intrinsic_source == "pe-blend" and still no value, price_unit_
     suspect False - the only way auto_intrinsic_value_engine.
     get_intrinsic_value() returns (0, False) with a real `info` dict
     is "no positive trailing EPS" (that function's own docstring).
     Trailing EPS is never persisted to the stored row, so there is
     exactly ONE honest, named sentence for this case, not several
     differently-worded ones pretending to a precision the stored row
     doesn't support.
  4. Out of scope, both left completely unchanged: intrinsic_source ==
     "none" (the SEPARATE fx_unavailable short-circuit in resolver_
     engine.resolve_intrinsic_value() - never reaches the pe-blend
     method at all, so "pe-blend rows" doesn't cover it) and any row
     whose Intrinsic Value is NOT None (nothing to explain).

BEFORE/AFTER for the three tickers the Director named (STLAP.PA,
7201.T, 6963.T): this sandbox has no outbound network access and no
real stored scan/snapshot data for these tickers (same disclosed
limitation as every other live-data question this session) - these
fixtures use each ticker's real symbol with SYNTHETIC field values
illustrating the three enumerated causes (STLAP.PA: no positive EPS;
7201.T: price_unit_suspect; 6963.T: a normal DCF row, included to
prove the fix never touches a row that already has a real value) -
clearly labelled synthetic, never presented as a real looked-up number
for these companies.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task2_pe_blend_no_value_reasons.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshot_render
import snapshot_store

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


def _snap(ticker, data, universe="CAC 40"):
    return {
        "ticker": ticker, "universe": universe, "data": data, "moat": None,
        "generated_at": "2026-10-13T03:00:00",
    }


# ======================================================================
# CHECK 1-3: BEFORE/AFTER, STLAP.PA (SYNTHETIC) - pe-blend, no value,
# not price-unit-suspect -> "no positive per-share earnings" reason.
# This is the exact scenario the Director's own bug report named.
# ======================================================================
_stlap_data = {
    "Company Name": "STLAP.PA (synthetic fixture)", "Price": 20.0, "Intrinsic Value": None,
    "MOS %": None, "Quality": 50.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
    "Long Score": 35.0, "Valuation": None, "Type": "Operating company",
    "Is Fund": False, "Intrinsic Source": "pe-blend", "price_unit_suspect": False,
}
_stlap_html = snapshot_render.render_snapshot(_snap("STLAP.PA", _stlap_data), "https://stocksdeepdive.com")
check("BEFORE/AFTER STLAP.PA (synthetic): the bare '-' is still there (value genuinely withheld)",
      '<div class="val">-</div>' in _stlap_html.replace("&#x27;", "'"))
check("BEFORE/AFTER STLAP.PA (synthetic): AFTER this fix, the cell now carries the named reason "
      "'no positive per-share earnings' - BEFORE this fix, nothing followed the bare dash at all",
      "no positive per-share earnings" in _stlap_html)
check("snapshot_render._intrinsic_value_hint() returns this exact sentence directly, for the "
      "underlying pub dict (not just a substring match on the rendered page)",
      snapshot_render._intrinsic_value_hint(snapshot_store.public_view(_stlap_data))
      == "no positive per-share earnings - the P/E-blend method needs a profit to value")


# ======================================================================
# CHECK 4-6: BEFORE/AFTER, 7201.T (SYNTHETIC) - price_unit_suspect
# fires FIRST, even though Intrinsic Source still reads "pe-blend"
# from before nightly_scan.py's own override - proves the priority
# order matches the real code's execution order, not an assumed one.
# ======================================================================
_nissan_data = {
    "Company Name": "7201.T (synthetic fixture)", "Price": 500.0, "Intrinsic Value": None,
    "MOS %": None, "Quality": 45.0, "Psychology": 5.0, "Discovery (lite)": 25.0,
    "Long Score": 30.0, "Valuation": None, "Type": "Operating company",
    "Is Fund": False, "Intrinsic Source": "pe-blend",
    "price_unit_suspect": True, "price_unit_suspect_reason": "price unit could not be confirmed",
}
_nissan_html = snapshot_render.render_snapshot(_snap("7201.T", _nissan_data, universe="TOPIX 500"),
                                                "https://stocksdeepdive.com")
check("BEFORE/AFTER 7201.T (synthetic): AFTER this fix, the cell carries the EXISTING "
      "price_unit_suspect_reason text, reused verbatim (never a new string invented for it)",
      "price unit could not be confirmed" in _nissan_html)
check("price_unit_suspect takes priority over the pe-blend 'no positive earnings' sentence, "
      "even though Intrinsic Source on this row still says 'pe-blend' - matches the real "
      "execution order (nightly_scan.py's override runs AFTER resolve_intrinsic_value())",
      "no positive per-share earnings" not in _nissan_html)
check("snapshot_store.public_view() exposes price_unit_suspect/price_unit_suspect_reason "
      "under their public names (newly whitelisted this task)",
      snapshot_store.public_view(_nissan_data).get("price_unit_suspect") is True
      and snapshot_store.public_view(_nissan_data).get("price_unit_suspect_reason")
      == "price unit could not be confirmed")


# ======================================================================
# CHECK 7-8: BEFORE/AFTER, 6963.T (SYNTHETIC) - a normal row with a
# real DCF-sourced Intrinsic Value. Included to prove the fix is
# display-only and never adds a stray hint next to a real number.
# ======================================================================
_rohm_data = {
    "Company Name": "6963.T (synthetic fixture)", "Price": 1200.0, "Intrinsic Value": 1450.0,
    "MOS %": 20.9, "Quality": 68.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
    "Long Score": 62.0, "Valuation": "Undervalued", "Type": "Operating company",
    "Is Fund": False, "Intrinsic Source": "dcf",
}
_rohm_html = snapshot_render.render_snapshot(_snap("6963.T", _rohm_data, universe="TOPIX 500"),
                                              "https://stocksdeepdive.com")
check("BEFORE/AFTER 6963.T (synthetic): unaffected before and after - the real number renders "
      "with no hint next to it, exactly as it already did",
      '<div class="lbl">Intrinsic value</div><div class="val">1450</div></div>' in _rohm_html)
check("_intrinsic_value_hint() returns None outright for a row with a real Intrinsic Value",
      snapshot_render._intrinsic_value_hint(snapshot_store.public_view(_rohm_data)) is None)


# ======================================================================
# CHECK 9-10: out-of-scope case left unchanged - intrinsic_source ==
# "none" (the fx_unavailable short-circuit, never reaches pe-blend at
# all) gets no hint, same bare "-" as before this task.
# ======================================================================
_fx_unavailable_data = {
    "Company Name": "Example SA (fx_unavailable, synthetic)", "Price": 30.0, "Intrinsic Value": None,
    "MOS %": None, "Quality": 55.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
    "Long Score": None, "Valuation": None, "Type": "Operating company",
    "Is Fund": False, "Intrinsic Source": "none",
}
check("out of scope: intrinsic_source == 'none' (fx_unavailable, never reaches pe-blend) gets "
      "no hint - 'pe-blend rows' doesn't cover this case, correctly left unchanged",
      snapshot_render._intrinsic_value_hint(snapshot_store.public_view(_fx_unavailable_data)) is None)

# ======================================================================
# CHECK 11: priority order sanity - is_fund wins over EVERY other
# check, exactly as the pre-existing overnight-queue fix already
# established (this task's own new checks must not shadow it).
# ======================================================================
_fund_data = {
    "Company Name": "Example Fund Co", "Price": 10.0, "Intrinsic Value": None,
    "Is Fund": True, "Fund Reason": "Fund — company valuation does not apply",
    "Intrinsic Source": "pe-blend", "price_unit_suspect": True,
    "price_unit_suspect_reason": "price unit could not be confirmed",
}
check("is_fund still wins over price_unit_suspect AND the pe-blend reason, even when a row "
      "(implausibly) carries all three flags at once - priority order unchanged by this task",
      snapshot_render._intrinsic_value_hint(snapshot_store.public_view(_fund_data))
      == "Fund — company valuation does not apply")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
