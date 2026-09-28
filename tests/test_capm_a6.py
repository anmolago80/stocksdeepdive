"""A6 (27 Sep 2026, owner-directed, FINAL SPEC; owner-approved LIVE 28
Sep 2026): unit fixtures for capm_engine.py's market-cap-discount-tier
machinery - resolve_discount_rate_by_market_cap() and
get_au_risk_free_rate_live() - PLUS confirmation this is now the live
formula every valuation on the site actually uses (resolve_discount_
rate(), which every one of fcf_valuation_engine.py's/moat_engine.py's/
auto_compounder_engine.py's call sites goes through, unchanged at every
one of THEIR call sites - see the live_path_confirmed section at the
bottom).
Run: python3 test_capm_a6.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capm_engine as ce


# ---- tier boundaries + FX bucketing ----
_FAKE_RF = {"USD": (0.050, "default"), "AUD": (0.053, "default")}


def _rf(ccy):
    return _FAKE_RF[ccy]


with mock.patch.object(ce, "get_risk_free_rate", side_effect=_rf), \
     mock.patch.object(ce, "get_au_risk_free_rate_live", return_value=(0.053, "default")):

    # Mega-cap USD: rf(0.050) + 0.02 = 0.070, floored to MIN_DISCOUNT_RATE (0.075).
    rate, meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 3_000_000_000_000, "currency": "USD"}, "USD")
    assert rate == 0.075 and meta["discount_floored"] is True
    assert meta["tier_label"].startswith("mega-cap")
    print("[tier_mega_floored] mega-cap rf+2% floors at MIN_DISCOUNT_RATE (7.5%), as the owner "
          "explicitly expects at today's rates OK")

    # Large-cap USD: rf(0.050) + 0.03 = 0.080, no floor.
    rate, meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 100_000_000_000, "currency": "USD"}, "USD")
    assert rate == 0.080 and meta["discount_floored"] is False
    assert meta["tier_label"].startswith("large-cap")
    print("[tier_large] US$100B -> large-cap tier, rf+3% = 8.0% OK")

    # Mid-cap boundary: exactly US$10B clears the mid-cap threshold (>= comparison).
    rate, meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 10_000_000_000, "currency": "USD"}, "USD")
    assert meta["tier_label"].startswith("mid-cap") and rate == 0.090
    print("[tier_boundary_inclusive] exactly US$10B lands in mid-cap (>=), not small-cap OK")

    # Just under the mid-cap threshold falls to small-cap.
    rate, meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 9_999_999_999, "currency": "USD"}, "USD")
    assert meta["tier_label"].startswith("small-cap") and rate == 0.100
    print("[tier_boundary_below] one dollar under US$10B lands in small-cap OK")

    # Micro-cap, the effective ceiling (no DISCOUNT_CEIL applied).
    rate, meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 100_000_000, "currency": "USD"}, "USD")
    assert meta["tier_label"].startswith("micro-cap") and rate == 0.110
    assert rate > ce.DISCOUNT_CEIL - 0.04  # sanity: nowhere near the old flat 15% ceiling logic
    print("[tier_micro_no_ceiling] micro-cap rf+6% = 11.0%, not clamped by the old DISCOUNT_CEIL "
          "(this tiered path deliberately has no ceiling clamp) OK")

    # Missing market cap -> conservative fallback to the strictest (micro-cap) tier, flagged.
    rate, meta = ce.resolve_discount_rate_by_market_cap({"currency": "USD"}, "USD")
    assert meta["market_cap_missing"] is True and meta["tier_label"].startswith("micro-cap")
    print("[tier_missing_mcap] missing marketCap falls back to the strictest tier, flagged "
          "market_cap_missing=True for the caller to disclose OK")

    # AUD bucketing: AU$250B * FX(0.65) = US$162.5B -> large-cap tier (50B-200B).
    rate, meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 250_000_000_000, "currency": "AUD"}, "AUD")
    assert meta["tier_label"].startswith("large-cap")
    assert abs(rate - (0.053 + 0.03)) < 1e-9
    print("[tier_aud_fx_bucketing] AU$250B correctly FX-bucketed to US$162.5B -> large-cap tier, "
          "AUD risk-free (0.053) + 3% premium OK")


# ---- AUD routes through get_au_risk_free_rate_live(), not get_risk_free_rate() ----
with mock.patch.object(ce, "get_risk_free_rate", side_effect=AssertionError(
        "get_risk_free_rate() must NOT be called for AUD in the tiered path")), \
     mock.patch.object(ce, "get_au_risk_free_rate_live", return_value=(0.06, "live")):
    rate, meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 100_000_000_000, "currency": "AUD"}, "AUD")
    assert meta["rf_source"] == "live" and rate == 0.09
print("[aud_uses_live_rba_path] AUD calls get_au_risk_free_rate_live(), never the old "
      "get_risk_free_rate() (which still hand-defaults AUD) OK")

with mock.patch.object(ce, "get_au_risk_free_rate_live", return_value=(0.053, "default")):
    rate, meta = ce.resolve_discount_rate_by_market_cap(
        {"marketCap": 100_000_000_000, "currency": "AUD"}, "AUD")
    assert meta["defaulted"] is True and meta["rf_source"] == "default"
print("[aud_default_flagged] a defaulted AU rate is flagged in meta the same way "
      "resolve_discount_rate()'s beta-based path already flags a default OK")


# ---- get_au_risk_free_rate_live(): CSV parsing + fallback behaviour ----
class _FakeResp:
    def __init__(self, text, status_ok=True):
        self.text = text
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise Exception("HTTP error")


_GOOD_CSV = (
    "Title,Yields on 2 year Treasury Bond,Yields on 10 year Treasury Bond\n"
    "Series ID,FCMYGBAG2,FCMYGBAG10\n"
    "Units,Per cent per annum,Per cent per annum\n"
    "2026-09-24,5.10,5.35\n"
    "2026-09-25,5.12,5.3850\n"
    "2026-09-26,5.14,\n"  # most recent date has no 10-year quote yet (e.g. a public holiday)
)

ce.get_au_risk_free_rate_live.clear()  # bypass the 24h st.cache_data memo between fixtures
with mock.patch.object(ce, "requests") as mock_requests:
    mock_requests.get.return_value = _FakeResp(_GOOD_CSV)
    rate, source = ce.get_au_risk_free_rate_live()
assert source == "live" and abs(rate - 0.05385) < 1e-9
print("[rba_parse_good_csv] 10-year COLUMN found via tolerant label match in the metadata row, "
      "most recent date row's value in that column used (skipping the blank cell one row up, a "
      "no-quote holiday), 5.3850 -> 0.05385 OK")

ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests:
    mock_requests.get.side_effect = Exception("connection refused")
    rate, source = ce.get_au_risk_free_rate_live()
assert source == "default" and rate == ce.RISK_FREE_FALLBACK["AUD"]
print("[rba_network_error_falls_back] a request exception degrades to the flagged fallback "
      "constant, never raises OK")

ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests:
    mock_requests.get.return_value = _FakeResp("Title,nothing useful here\nNo yields at all\n")
    rate, source = ce.get_au_risk_free_rate_live()
assert source == "default"
print("[rba_no_matching_row_falls_back] a CSV with no 10-year row found degrades to the "
      "fallback rather than crashing or returning garbage OK")

ce.get_au_risk_free_rate_live.clear()
_GARBAGE_CSV = "Series ID,10-year\n2026-09-26,0.0004\n"  # a divisor-bug-shaped value (0.04%)
with mock.patch.object(ce, "requests") as mock_requests:
    mock_requests.get.return_value = _FakeResp(_GARBAGE_CSV)
    rate, source = ce.get_au_risk_free_rate_live()
assert source == "default"
print("[rba_sanity_band_rejects_garbage] a value outside RISK_FREE_MIN/MAX (e.g. from a wrong "
      "divisor) is rejected, not accepted as 'live' OK")

ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests:
    mock_requests.get.return_value = _FakeResp("bad csv", status_ok=False)
    rate, source = ce.get_au_risk_free_rate_live()
assert source == "default"
print("[rba_http_error_falls_back] a non-OK HTTP status degrades to the fallback OK")


# ---- Live-path confirmation (owner-approved LIVE, 28 Sep 2026): the ----
# ---- SAME resolve_discount_rate() every live caller already used     ----
# ---- now produces the tiered rate, with beta read nowhere at all.    ----
import inspect

# resolve_discount_rate() no longer reads info["beta"] - the beta CAPM
# formula's own historic magic numbers are gone from the module entirely
# (not just unused: genuinely removed, so nothing can silently re-wire
# them back in), and identical market cap/currency with wildly different
# beta values must give the EXACT same discount rate.
for _dead_name in ("EQUITY_RISK_PREMIUM", "DEFAULT_BETA", "MIN_BETA"):
    assert not hasattr(ce, _dead_name), f"{_dead_name} should have been removed, not just unused"
print("[beta_constants_removed] EQUITY_RISK_PREMIUM/DEFAULT_BETA/MIN_BETA no longer exist on "
      "capm_engine - not just unused dead code OK")

with mock.patch.object(ce, "get_risk_free_rate", side_effect=_rf):
    _rate_lo_beta, _meta_lo_beta = ce.resolve_discount_rate(
        {"marketCap": 100_000_000_000, "currency": "USD", "beta": 0.05}, "USD")
    _rate_hi_beta, _meta_hi_beta = ce.resolve_discount_rate(
        {"marketCap": 100_000_000_000, "currency": "USD", "beta": 5.0}, "USD")
    _rate_no_beta, _meta_no_beta = ce.resolve_discount_rate(
        {"marketCap": 100_000_000_000, "currency": "USD"}, "USD")
assert _rate_lo_beta == _rate_hi_beta == _rate_no_beta == 0.080, (
    _rate_lo_beta, _rate_hi_beta, _rate_no_beta)
assert _meta_lo_beta["tier_label"].startswith("large-cap")
print(f"[resolve_discount_rate_ignores_beta] resolve_discount_rate() - the function EVERY live "
      f"caller (fcf_valuation_engine.dcf_intrinsic_value, moat_engine.py's ~5 cost-of-equity call "
      f"sites, auto_compounder_engine._build_cost_of_capital) already goes through - gives the "
      f"IDENTICAL {_rate_lo_beta:.3f} for beta=0.05, beta=5.0, and no beta at all: the market-cap "
      f"tier (large-cap, rf+3%), not beta, decides it now OK")

# meta shape every existing caller reads is preserved (defaulted/floored/
# discount_floored/rf_source) - beta_source/beta_floored are gone (no
# caller outside this module ever read them).
for _key in ("defaulted", "floored", "discount_floored", "rf_source", "tier_label", "premium_used"):
    assert _key in _meta_lo_beta, (_key, _meta_lo_beta)
for _dead_key in ("beta_source", "beta_floored"):
    assert _dead_key not in _meta_lo_beta, _meta_lo_beta
print("[meta_shape_preserved] resolve_discount_rate()'s meta keeps defaulted/floored/"
      "discount_floored/rf_source (every existing caller's own contract) and adds tier_label/"
      "premium_used - beta_source/beta_floored are gone OK")

# Integration: fcf_valuation_engine.dcf_intrinsic_value()'s own auto path
# (discount_rate=None) now reports "tiered"/"tiered-default", not the old
# "capm"/"capm-default" - confirms the swap reaches the real DCF, not
# just this module in isolation.
import fcf_valuation_engine as fve

with mock.patch.object(ce, "get_risk_free_rate", side_effect=_rf):
    _iv, _g, _dcf_meta = fve.dcf_intrinsic_value(
        "TEST", info={"marketCap": 100_000_000_000, "currency": "USD", "beta": 0.05},
        cashflow_df=None, currency="USD", manual_fcf=10.0, diluted_shares_override=1,
        growth_rate=0.05, perpetual_rate=0.02,
    )
assert _dcf_meta["discount_source"] in ("tiered", "tiered-default"), _dcf_meta["discount_source"]
assert abs(_dcf_meta["discount_rate_used"] - 0.080) < 1e-9, _dcf_meta["discount_rate_used"]
assert _dcf_meta["discount_tier_label"].startswith("large-cap"), _dcf_meta["discount_tier_label"]
print(f"[live_dcf_uses_tiered_rate] fcf_valuation_engine.dcf_intrinsic_value()'s own auto-CAPM "
      f"path (discount_rate=None) now reports discount_source={_dcf_meta['discount_source']!r} "
      f"and discount_tier_label={_dcf_meta['discount_tier_label']!r} - the live DCF genuinely "
      "uses the tiered rate, not just capm_engine.py tested in isolation OK")

_fcf_src = inspect.getsource(fve)
assert "info.get(\"beta\")" not in _fcf_src and "info[\"beta\"]" not in _fcf_src
print("[fcf_valuation_engine_never_reads_beta] fcf_valuation_engine.py itself never reads "
      "info[\"beta\"] anywhere (it never did directly - always through capm_engine - but this "
      "confirms no new beta read was introduced either) OK")


print("\nALL CAPM A6 FIXTURES PASSED")
