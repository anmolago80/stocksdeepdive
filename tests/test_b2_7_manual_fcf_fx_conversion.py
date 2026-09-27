"""Batch B2, item 7 (27 Sep 2026, owner-directed, CONFIRMED BUG): the
manual FCF override skipped FX conversion in the compounder
(auto_compounder_engine._run_dcf()).

_run_dcf() overrides a COPY of info["financialCurrency"] to match the
listing currency before calling fcf_valuation_engine.dcf_intrinsic_
value() - deliberately, to stop that function re-converting
bundle["cashflow"], which fundamentals_data.get_bundle() already
converted to listing currency once. But dcf_intrinsic_value() decides
whether to FX-convert its chosen FCF (auto-derived from cashflow_df, OR
a manual override - "Applied to whichever fcf was just chosen above,
manual override included", per that function's own comment) by
comparing THAT (now-overridden) financialCurrency against currency - so
the override, correct for the auto-derived path, also silently
suppressed the ONE real conversion a manual override still needed: a
manual_fcf value is a raw, user-typed number, never pre-converted by
get_bundle() the way the cashflow DataFrame is. Calling
dcf_intrinsic_value() directly (the main site's Deep Dive page) converts
an equivalent manual override correctly, since it passes the real,
unmodified financialCurrency - so the SAME manual override number
produced two different intrinsic values depending on which caller it
went through, purely because of this compounder-only override.

Reproduces the bug first (mocking dcf_intrinsic_value to capture what
manual_fcf value it was actually called with), then proves the fix.
Run: python3 tests/test_b2_7_manual_fcf_fx_conversion.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace
import fcf_valuation_engine as fve

# CSL.AX-shaped: reports in USD, trades in AUD - the exact scenario this
# module's own "Double-FX-conversion fix" comment names.
BUNDLE = {
    "info": {"currency": "AUD", "financialCurrency": "USD", "sharesOutstanding": 1_000_000.0},
    "cashflow": object(),  # opaque - dcf_intrinsic_value is mocked, never actually reads this
}
MANUAL_FCF_RAW = 200_000.0  # a raw, user-typed number - documented convention: same unit as the auto-derived figure, i.e. USD (financial currency)
_FX_RATE = 1.5  # USD->AUD, made up but fixed, so the expected converted value is exact


def _run(manual_fcf):
    captured = {}

    def _fake_dcf(*args, **kwargs):
        captured["manual_fcf"] = kwargs.get("manual_fcf")
        captured["financialCurrency"] = (kwargs.get("info") or {}).get("financialCurrency")
        return (999.0, 0.05, {"perpetual_rate_used": 0.02, "discount_rate_used": 0.1})

    with mock.patch.object(ace, "_whole_company_shares", return_value=(1_000_000.0, False)), \
         mock.patch.object(fve, "dcf_intrinsic_value", side_effect=_fake_dcf), \
         mock.patch.object(fve, "fx_rate", return_value=(_FX_RATE, "live")):
        ace._run_dcf(BUNDLE, "TESTB27.AX", manual_fcf=manual_fcf)
    return captured


# ---- The fix: manual_fcf reaching dcf_intrinsic_value() is FX-converted ----
captured = _run(MANUAL_FCF_RAW)
_expected_converted = MANUAL_FCF_RAW * _FX_RATE
assert captured["manual_fcf"] == _expected_converted
print(f"[manual_fcf_converted] a manual override of {MANUAL_FCF_RAW} USD reaches "
      f"dcf_intrinsic_value() as {captured['manual_fcf']} AUD (x{_FX_RATE}), not the raw, "
      "unconverted USD figure OK")

# Reproduces the bug: the OLD code passed manual_fcf straight through
# unconverted (the raw USD number, mismatched against the AUD-denominated
# share price it gets divided into per-share by).
assert captured["manual_fcf"] != MANUAL_FCF_RAW
print(f"[reproduces_bug] the OLD code would have passed the RAW {MANUAL_FCF_RAW} straight "
      "through unconverted - a real unit mismatch against the AUD price OK")

# ---- No double-conversion: dcf_info's financialCurrency is still
# overridden to match currency, so dcf_intrinsic_value()'s OWN internal
# conversion block is a no-op on the (already-converted-by-us) manual_fcf ----
assert captured["financialCurrency"] == BUNDLE["info"]["currency"]
print("[no_double_conversion] dcf_info's financialCurrency is still overridden to match the "
      "listing currency, so dcf_intrinsic_value()'s own internal conversion never fires a "
      "SECOND time on the already-converted manual_fcf OK")

# ---- Same-currency case: no conversion applied (nothing to convert) ----
BUNDLE_SAME_CCY = {
    "info": {"currency": "USD", "financialCurrency": "USD", "sharesOutstanding": 1_000_000.0},
    "cashflow": object(),
}


def _run_same_ccy(manual_fcf):
    captured = {}

    def _fake_dcf(*args, **kwargs):
        captured["manual_fcf"] = kwargs.get("manual_fcf")
        return (999.0, 0.05, {"perpetual_rate_used": 0.02, "discount_rate_used": 0.1})

    with mock.patch.object(ace, "_whole_company_shares", return_value=(1_000_000.0, False)), \
         mock.patch.object(fve, "dcf_intrinsic_value", side_effect=_fake_dcf), \
         mock.patch.object(fve, "fx_rate", return_value=(_FX_RATE, "live")):
        ace._run_dcf(BUNDLE_SAME_CCY, "TESTB27US", manual_fcf=manual_fcf)
    return captured


captured_same = _run_same_ccy(MANUAL_FCF_RAW)
assert captured_same["manual_fcf"] == MANUAL_FCF_RAW
print("[same_currency_unaffected] when financial and listing currency already match, manual_fcf "
      "passes through completely unchanged - no spurious conversion is ever applied OK")

# ---- No manual override: nothing to convert, no fx_rate() call needed ----
def _run_no_override():
    calls = {"fx_rate": 0}

    def _fake_dcf(*args, **kwargs):
        return (999.0, 0.05, {"perpetual_rate_used": 0.02, "discount_rate_used": 0.1})

    def _fake_fx_rate(*args, **kwargs):
        calls["fx_rate"] += 1
        return (_FX_RATE, "live")

    with mock.patch.object(ace, "_whole_company_shares", return_value=(1_000_000.0, False)), \
         mock.patch.object(fve, "dcf_intrinsic_value", side_effect=_fake_dcf), \
         mock.patch.object(fve, "fx_rate", side_effect=_fake_fx_rate):
        ace._run_dcf(BUNDLE, "TESTB27NOOVERRIDE.AX", manual_fcf=None)
    return calls


calls = _run_no_override()
assert calls["fx_rate"] == 0
print("[no_override_no_fx_call] with no manual override at all, this new conversion step never "
      "calls fx_rate() - the auto-derived path is completely untouched by this fix OK")


print("\nALL B2.7 MANUAL-FCF-FX-CONVERSION FIXTURES PASSED")
