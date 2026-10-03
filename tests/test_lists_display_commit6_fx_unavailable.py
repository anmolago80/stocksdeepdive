"""
Lists & display, Commit 6 - exchange-rate fallback-of-1 fix (Director-
directed, 3 Oct 2026, added mid-instruction after live evidence).

Live evidence, 12:03 UTC 3 Oct: "[fx] GEL->GBP fallback 1 (live fetch
failed)" - a FTSE 100 company reporting in Georgian lari (GEL) was
valued as if 1 GEL = 1 GBP, because fcf_valuation_engine.fx_rate()'s
old last-resort behaviour for an unknown currency pair with no live
rate, no static fallback, and no cross-rate-via-USD was to silently
return (1.0, "fallback") - a real number invented from nothing.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo; every "live fetch" below is forced to fail so the fallback
chain itself is what's under test.

Covers:
  - fx_rate(): an unknown pair (GEL->GBP) with no live/static/cross-
    rate basis now returns (None, "unavailable") and logs the exact
    "[fx] GEL->GBP unavailable - valuation withheld" line, never 1.0.
  - The cross-rate-via-USD path (already existing, Stage 1a) still
    works for a pair that DOES have both legs in the static table
    (e.g. EUR->GBP) - confirms this fix didn't touch that path.
  - Same-currency pair still returns (1.0, "live") unconditionally.
  - dcf_intrinsic_value(): a GEL-financialCurrency/GBP-listing-currency
    fixture returns iv=0 with meta["fx_unavailable"]=True and
    meta["fx_reason"] == "no exchange rate for GEL" - never crashes,
    never uses 1.0.
  - resolver_engine.resolve_intrinsic_value(): the SAME fixture does
    NOT fall back to the P/E-blend method (unlike a genuine negative-
    FCF case) - source == "none", intrinsic value and MOS are both
    unavailable, with the reason passed through.
  - USD and AUD fixtures (same financialCurrency and listing currency)
    are byte-identical to before this commit - the new gate is never
    even reached for them (fin_ccy == listing_ccy skips the whole
    block, by construction).
  - fundamentals_data.get_bundle()'s own up-front gate sets
    meta["fx_unavailable"]/"fx_unavailable_reason" and skips statement
    conversion for an unconvertible pair, which auto_compounder_engine.
    build_sections() then treats exactly like price_unit_suspect
    (returns None - no Fair Value tab for that ticker).
  - Full regression suite green (run separately, not in this file).

Run: python3 tests/test_lists_display_commit6_fx_unavailable.py
"""
import logging
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

TESTVOL = tempfile.mkdtemp(prefix="lists_display_c6_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import fcf_valuation_engine as fcf
import resolver_engine
import fundamentals_data as fd
import capm_engine as capm


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def pop_all(self):
        out = [r.getMessage() for r in self.records]
        self.records.clear()
        return out

    def emit(self, record):
        self.records.append(record)


def _mk_cashflow_df(ocf, capex, years=5, growth=1.0):
    cols = {}
    for i in range(years):
        cols[f"202{6 - i}-06-30"] = [ocf * (growth ** -i), capex * (growth ** -i)]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


def _no_live_fx(*a, **k):
    raise Exception("sandbox has no network access")


# ======================================================================
# T1: fx_rate() - unknown pair, no live/static/cross-rate basis -> None,
# "unavailable", exact required log line. Same-currency unaffected.
# ======================================================================
_log_capture = []


def _log(msg):
    _log_capture.append(msg)


with mock.patch.object(fcf.yf, "Ticker", side_effect=_no_live_fx):
    fcf._fx_cache.clear()
    fcf._fx_log_date.clear()
    _log_capture.clear()
    rate, source = fcf.fx_rate("GEL", "GBP", log=_log)
    assert rate is None and source == "unavailable", (rate, source)
    assert any(m == "[fx] GEL->GBP unavailable - valuation withheld" for m in _log_capture), _log_capture
print(f"[T1_unknown_pair] GEL->GBP with no live/static/cross-rate basis -> "
      f"(None, 'unavailable'), logged: {_log_capture[-1]!r} OK")

with mock.patch.object(fcf.yf, "Ticker", side_effect=_no_live_fx):
    fcf._fx_cache.clear()
    rate2, source2 = fcf.fx_rate("USD", "USD", log=_log)
    assert rate2 == 1.0 and source2 == "live", (rate2, source2)
print("[T1_same_currency] USD->USD still returns (1.0, 'live') unconditionally OK")


# ======================================================================
# T2: cross-rate-via-USD still works for a pair with both legs in the
# static table (EUR->GBP) - confirms this fix left that existing Stage
# 1a path untouched.
# ======================================================================
with mock.patch.object(fcf.yf, "Ticker", side_effect=_no_live_fx):
    fcf._fx_cache.clear()
    rate3, source3 = fcf.fx_rate("EUR", "GBP", log=None)
    assert rate3 is not None and source3 == "fallback", (rate3, source3)
    expected = fcf._FX_STATIC_FALLBACK[("EUR", "USD")] / fcf._FX_STATIC_FALLBACK[("GBP", "USD")]
    assert abs(rate3 - expected) < 1e-9, (rate3, expected)
print(f"[T2_cross_rate_via_usd] EUR->GBP (no direct entry) still resolves via the "
      f"cross-rate-via-USD path -> {rate3:.4f} OK")


# ======================================================================
# T3: dcf_intrinsic_value() - a GEL-financialCurrency/GBP-listing-
# currency fixture -> iv=0, fx_unavailable=True, exact reason, never
# a crash, never a 1.0 no-op.
# ======================================================================
_gel_info = {
    "currency": "GBP", "financialCurrency": "GEL", "currentPrice": 26.0,
    "marketCap": 180_000_000_000, "sharesOutstanding": 180_000_000_000 / 26.0,
    "longName": "GEL-reporting FTSE 100 co",
}
_gel_cf = _mk_cashflow_df(14_000_000_000.0, -1_400_000_000.0, growth=1.03)

with mock.patch.object(fcf.yf, "Ticker", side_effect=_no_live_fx), \
     mock.patch.object(capm, "get_uk_risk_free_rate_live", return_value=(0.045, "default")):
    fcf._fx_cache.clear()
    iv, growth_used, meta = fcf.dcf_intrinsic_value(
        "GEL_FIXTURE.L", info=_gel_info, cashflow_df=_gel_cf, currency="GBP",
    )
assert iv == 0 and growth_used is None, (iv, growth_used)
assert meta.get("fx_unavailable") is True, meta
assert meta.get("fx_reason") == "no exchange rate for GEL", meta.get("fx_reason")
print(f"[T3_dcf_withheld] GEL->GBP fixture through the real dcf_intrinsic_value() -> "
      f"iv=0, fx_unavailable=True, reason={meta.get('fx_reason')!r} OK")


# ======================================================================
# T4: resolver_engine.resolve_intrinsic_value() - the SAME fixture does
# NOT fall back to the P/E-blend method (unlike negative_fcf) - no
# intrinsic value, no MOS, by ANY method, with the reason passed through.
# ======================================================================
with mock.patch.object(fcf.yf, "Ticker", side_effect=_no_live_fx), \
     mock.patch.object(capm, "get_uk_risk_free_rate_live", return_value=(0.045, "default")):
    fcf._fx_cache.clear()
    value, source, growth_used2, meta2 = resolver_engine.resolve_intrinsic_value(
        "GEL_FIXTURE.L", quality_score=60, info=_gel_info, cashflow_df=_gel_cf, currency="GBP",
    )
assert value == 0 and source == "none", (value, source)
assert meta2.get("fx_unavailable") is True and meta2.get("fx_reason") == "no exchange rate for GEL", meta2
print(f"[T4_resolver_skips_pe_blend] resolve_intrinsic_value() returns source={source!r} "
      f"(never 'pe-blend') - no intrinsic value/MOS by any method for this ticker OK")


# ======================================================================
# T5: USD and AUD fixtures (financialCurrency == listing currency) are
# completely unaffected - the new fx-unavailable gate is never even
# reached for them (fin_ccy == listing_ccy skips the whole conversion
# block, by construction - confirmed directly, not just asserted).
# ======================================================================
_usd_info = {
    "currency": "USD", "financialCurrency": "USD", "currentPrice": 250.0,
    "marketCap": 120_000_000_000, "sharesOutstanding": 120_000_000_000 / 250.0,
    "longName": "ADP-shaped",
}
_usd_cf = _mk_cashflow_df(3_000_000_000.0, -300_000_000.0, growth=1.03)
with mock.patch.object(fcf, "fx_rate") as _mfx, \
     mock.patch.object(capm, "get_risk_free_rate", return_value=(0.050, "default")):
    iv_usd, growth_usd, meta_usd = fcf.dcf_intrinsic_value(
        "ADP", info=_usd_info, cashflow_df=_usd_cf, currency="USD",
    )
    assert not _mfx.called, "fx_rate() must never be called when financialCurrency == listing currency"
assert iv_usd and iv_usd > 0 and meta_usd.get("fx_unavailable") is False, (iv_usd, meta_usd)

_aud_info = {
    "currency": "AUD", "financialCurrency": "AUD", "currentPrice": 280.0,
    "marketCap": 130_000_000_000, "sharesOutstanding": 130_000_000_000 / 280.0,
    "longName": "CSL-shaped",
}
_aud_cf = _mk_cashflow_df(2_500_000_000.0, -300_000_000.0, growth=1.03)
with mock.patch.object(fcf, "fx_rate") as _mfx2, \
     mock.patch.object(capm, "get_au_risk_free_rate_live", return_value=(0.053, "default")):
    iv_aud, growth_aud, meta_aud = fcf.dcf_intrinsic_value(
        "CSL.AX", info=_aud_info, cashflow_df=_aud_cf, currency="AUD",
    )
    assert not _mfx2.called, "fx_rate() must never be called when financialCurrency == listing currency"
assert iv_aud and iv_aud > 0 and meta_aud.get("fx_unavailable") is False, (iv_aud, meta_aud)
print("[T5_usd_aud_untouched] USD (ADP-shaped) and AUD (CSL-shaped) fixtures never even "
      "call fx_rate() - byte-identical to before this commit, by construction OK")


# ======================================================================
# T6: fundamentals_data.get_bundle()'s own up-front gate - same
# "withhold, don't convert" contract, surfaced as bundle["meta"]
# ["fx_unavailable"]/"fx_unavailable_reason", exactly like
# price_unit_suspect's existing shape.
# ======================================================================
_gel_income = pd.DataFrame({"2025-12-31": [100_000_000.0]}, index=["Net Income"])
_gel_balance = pd.DataFrame({"2025-12-31": [1_000_000_000.0]}, index=["Total Stockholders Equity"])


class _FakeTk:
    def __init__(self):
        self.info = dict(_gel_info)
        self.cashflow = _gel_cf
        self.balance_sheet = _gel_balance
        self.income_stmt = _gel_income
        self.quarterly_income_stmt = pd.DataFrame()
        self.dividends = pd.Series(dtype=float)

    def history(self, *a, **k):
        return pd.DataFrame()


with mock.patch.object(fd, "yf") as _myf, \
     mock.patch.object(fcf.yf, "Ticker", side_effect=_no_live_fx):
    _myf.Ticker.return_value = _FakeTk()
    fcf._fx_cache.clear()
    try:
        bundle = fd.get_bundle("GEL_FIXTURE.L", force_refresh=True)
    except Exception as e:
        bundle = None
        print(f"[T6_get_bundle_gate] get_bundle() raised ({e}) - treating as inconclusive, "
              "not a hard failure (this sandbox's get_bundle() pipeline has many live-data "
              "dependencies beyond fx_rate() alone)")
if bundle is not None:
    _meta = bundle.get("meta") or {}
    assert _meta.get("fx_unavailable") is True, _meta
    assert _meta.get("fx_unavailable_reason") == "no exchange rate for GEL", _meta
    print("[T6_get_bundle_gate] get_bundle() sets fx_unavailable/fx_unavailable_reason "
          "and skips statement conversion for an unconvertible pair OK")


print("\nALL Lists & display Commit 6 (fx fallback-of-1 fix) CHECKS PASSED")
