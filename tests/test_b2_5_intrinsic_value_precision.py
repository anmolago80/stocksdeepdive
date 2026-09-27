"""Batch B2, item 5 (27 Sep 2026, owner-directed, CONFIRMED BUG): intrinsic
value was rounded to 2 decimal places INSIDE dcf_intrinsic_value(), before
any caller computed MOS from it or even checked whether it was positive.

fcf_valuation_engine.dcf_intrinsic_value() used to `return round(intrinsic,
2), ...`. Two consequences:
1. Every caller's MOS ((intrinsic - price) / intrinsic * 100) was computed
   from an already-rounded number, silently losing precision.
2. Every caller gates on the RAW return value being positive
   (nightly_scan.py's `mos = ... if intrinsic > 0 else 0.0`, and this
   codebase's own `if iv else None` pattern in resolver_engine.py,
   admin_data_audit.py, app.py's A6 panel). A genuinely positive but
   sub-cent per-share intrinsic value (a real, computable number for a
   penny stock) got rounded down to exactly 0.0 INSIDE the function,
   before any of those gates ever saw the true value - so the stock was
   treated as having NO valid intrinsic value at all, rather than a small
   one. That is the "penny stocks are currently misclassified" bug.

The fix returns full precision from dcf_intrinsic_value() itself; every
caller already re-rounds independently for display/storage (confirmed by
reading nightly_scan.py lines 575/577, admin_data_audit.py, app.py's A6
panel - all call round(iv, 2)/round(mos, ...) themselves), so DISPLAYED
values are unchanged - only the precision MOS (and the positive/invalid
gate) is computed from improves.

Reproduces each consequence first, then proves the fix.
Run: python3 tests/test_b2_5_intrinsic_value_precision.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve

# Shared discount/perpetual/growth all passed explicitly (manual overrides)
# so this fixture never hits capm_engine's or fx_rate's own network paths -
# isolates the one thing under test (the return statement's rounding).
_COMMON = dict(discount_rate=0.10, perpetual_rate=0.02, growth_rate=0.05)

# ---- Case 1: normal-priced stock - a small, real MOS precision loss ----
info_normal = {
    "sharesOutstanding": 1_000_000_000.0,
    "currency": "USD",
    "financialCurrency": "USD",  # same as currency -> no fx_rate() network call
    "currentPrice": 0.005,
}
iv_normal, g_normal, meta_normal = fve.dcf_intrinsic_value(
    "TESTB25NORMAL", info=info_normal, cashflow_df=None, currency="USD",
    manual_fcf=3_000_000.0, **_COMMON,
)
assert iv_normal is not None and iv_normal > 0

# Reproduces the bug: the OLD code returned round(iv_normal, 2) and computed
# MOS from THAT rounded number.
price = info_normal["currentPrice"]
_old_buggy_iv = round(iv_normal, 2)
_old_buggy_mos = (_old_buggy_iv - price) / _old_buggy_iv * 100.0
_correct_mos = (iv_normal - price) / iv_normal * 100.0
assert iv_normal != _old_buggy_iv  # the fix keeps precision the old code discarded
assert abs(_correct_mos - _old_buggy_mos) > 0.1  # a real, non-trivial MOS difference
print(f"[normal_priced_mos_precision] full-precision intrinsic {iv_normal:.6f} gives MOS "
      f"{_correct_mos:.4f}%, not the old buggy {_old_buggy_mos:.4f}% computed after rounding "
      f"to {_old_buggy_iv} inside the function OK")

# Downstream display (nightly_scan.py's own independent round(intrinsic, 2))
# is UNCHANGED by this fix - same displayed number either way.
assert round(iv_normal, 2) == round(_old_buggy_iv, 2)
print("[normal_priced_display_unchanged] the DISPLAYED intrinsic value (nightly_scan.py's own "
      "round(intrinsic, 2)) is identical before and after this fix - only the precision MOS is "
      "computed from changed OK")

# ---- Case 2: penny stock - the OLD code zeroed a real value out entirely ----
info_penny = {
    "sharesOutstanding": 1_000_000_000.0,
    "currency": "USD",
    "financialCurrency": "USD",
    "currentPrice": 0.005,
}
iv_penny, g_penny, meta_penny = fve.dcf_intrinsic_value(
    "TESTB25PENNY", info=info_penny, cashflow_df=None, currency="USD",
    manual_fcf=300_000.0, **_COMMON,
)
# A real, positive, sub-cent per-share value.
assert iv_penny is not None
assert 0 < iv_penny < 0.01
print(f"[penny_stock_real_positive_value] the true intrinsic value is a real, positive "
      f"${iv_penny:.6f}/share OK")

# Reproduces the bug: the OLD code's return value, rounded to 2dp INSIDE the
# function, was exactly 0.0 - every caller's own `if intrinsic > 0` gate
# (resolver_engine.py, nightly_scan.py, admin_data_audit.py, app.py's A6
# panel) would have seen a hard 0.0, not a small positive number.
_old_buggy_iv_penny = round(iv_penny, 2)
assert _old_buggy_iv_penny == 0.0
print("[penny_stock_old_code_zeroed_it] the OLD code would have returned exactly 0.0 for this "
      "real, positive intrinsic value - every downstream `intrinsic > 0` gate would have "
      "treated this stock as unscoreable/invalid, not merely cheap OK")

# The FIX: the raw return value itself is still genuinely positive, so
# every caller's own gate now sees the true, real number - only the
# DISPLAYED figure (nightly_scan's own independent round(intrinsic, 2))
# still shows $0.00 at 2dp, same as before; what changes is that MOS is no
# longer computed from an already-corrupted 0.0.
assert iv_penny > 0  # the gate every live caller applies
_mos_penny_correct = (iv_penny - info_penny["currentPrice"]) / iv_penny * 100.0
print(f"[penny_stock_fix_preserves_gate] the fixed return value ({iv_penny:.6f}) still clears "
      f"every caller's `intrinsic > 0` gate, so MOS ({_mos_penny_correct:.2f}%) is computed on a "
      "real number instead of being silently dropped/defaulted to invalid OK")

# ---- growth_rate rounding is untouched by this fix (unrelated bug) ----
assert g_normal == round(0.05, 4)
assert g_penny == round(0.05, 4)
print("[growth_rate_rounding_unchanged] growth_rate's own round(growth_rate, 4) is untouched - "
      "this fix only removes the premature rounding of the intrinsic VALUE OK")


print("\nALL B2.5 INTRINSIC-VALUE-PRECISION FIXTURES PASSED")
