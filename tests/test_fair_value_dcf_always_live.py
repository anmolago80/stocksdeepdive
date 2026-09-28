"""
Owner-directed (28 Sep 2026, "APPROVED - build now, push automatically
later"): the Fair Value tab's DCF row must ALWAYS show the LIVE canonical
value - resolver_engine.resolve_intrinsic_value(), the same call the Deep
Dive gauge makes on every render - never a value read out of auto_
compounder_engine.py's 24h-TTL disk-persisted section cache
(_read_cache()/_write_cache()). The other three valuation methods on that
same tab (PE Forward, PE Trailing, Rational Compounder/Equity 10y) are
explicitly allowed to keep coming from the cache unchanged.

Root cause this closes: build_sections()'s cache-hit path used to
`return cached` wholesale - the ENTIRE previously-computed "Fair Value"
section (including its "dcf" row) rode along with everything else,
unrefreshed, for up to 24h (or until a >3% intraday price move happened
to trigger a full rebuild). A visitor's Deep Dive gauge (which recomputes
resolve_intrinsic_value() fresh on every page render, off 30-min-cached
fetchers) and the SAME ticker's Fair Value tab (serving a stale 24h
cache) could show two different Intrinsic Value numbers for the same
stock at the same moment.

The fix (auto_compounder_engine.py):
- _dcf_valuation_and_inputs(info, price, canonical_dcf_result): the
  "dcf" row's value + every display input, factored out of
  _build_fair_value() into its own pure function (no behavior change on
  a cold/full build - same exact output as before this refactor).
- _refresh_live_dcf_in_cached_sections(cached, ticker, ...): re-fetches
  the bundle (itself already 24h-cached but with a fresh live-price
  overlay on every call) and re-runs _run_canonical_dcf(), then splices
  ONLY the "dcf" row (via the helper above) into an already-cached
  `sections` dict in place - PE Forward/PE Trailing/Equity 10y are left
  completely untouched. Fails open (returns `cached` unmodified) if the
  live bundle fetch or DCF recompute fails.
- build_sections()'s two cache-hit `return cached` branches now both
  return _refresh_live_dcf_in_cached_sections(cached, ...) instead.

Run: python3 tests/test_fair_value_dcf_always_live.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace
import resolver_engine
import fundamentals_data as fd


# ADP-shaped fixture (stable capex, clean positive history) reused from
# tests/test_dcf_outlier_guards.py's own fixture pattern - a real,
# non-trivial, network-free DCF: every rate (discount/perpetual/growth)
# is passed explicitly, so dcf_intrinsic_value() never touches capm_
# engine's live bond-yield/growth-estimate fetches.
TICKER = "LIVEDCFTEST"
_INFO = {
    "currentPrice": 100.0, "currency": "USD", "financialCurrency": "USD",
    "marketCap": 105_000_000_000, "sharesOutstanding": 100,
}
_CASHFLOW = pd.DataFrame(
    {
        "2026-06-30": [500.0, -40.0], "2025-06-30": [470.0, -38.0],
        "2024-06-30": [450.0, -36.0], "2023-06-30": [430.0, -34.0],
    },
    index=["Operating Cash Flow", "Capital Expenditure"],
)
_BUNDLE = {"info": dict(_INFO), "cashflow": _CASHFLOW, "income": None}
# A fully-shaped bundle (per fundamentals_data.get_bundle()'s own module
# docstring) - needed only for Check 4's full/cold build below, which
# runs EVERY section builder (not just the DCF-only path the cache-hit
# checks exercise) - the other five builders each read bundle["income"]/
# ["balance"] etc. as real DataFrames, not None.
_FULL_BUNDLE = dict(
    _BUNDLE, income=pd.DataFrame(), balance=pd.DataFrame(),
    prices_10y={"dates": [], "prices": []}, dividends={"dates": [], "amounts": []},
    spx_prices_10y={"dates": [], "prices": []},
    meta={"source": "yfinance", "statement_years": 4,
          "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "flags": []},
)
_DR, _PR, _GR = 0.08, 0.02, 0.0544  # matches the ADP-shaped fixture's own history growth
_OVERRIDES = (_DR, _PR, _GR, None)

# Ground truth: exactly what "the Deep Dive gauge" computes for this same
# ticker/inputs - a direct resolve_intrinsic_value() call, same function
# _run_canonical_dcf() itself is a thin wrapper around (see that
# function's own docstring). This IS the fixture: "gauge IV == Fair
# Value DCF IV for the same ticker at the same moment".
_gauge_iv, _gauge_src, _gauge_growth, _gauge_meta = resolver_engine.resolve_intrinsic_value(
    TICKER, None, info=dict(_INFO), cashflow_df=_CASHFLOW, currency="USD",
    discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR, manual_fcf=None,
)
assert _gauge_src == "dcf" and _gauge_iv > 0, (_gauge_src, _gauge_iv)
print(f"[gauge_reference_value] the Deep Dive gauge's own resolve_intrinsic_value() call -> "
      f"IV=${_gauge_iv:.2f} (source={_gauge_src!r}) - this is the value the Fair Value tab's "
      f"DCF row must always match OK")


def _stale_cache_meta(price_at_build=100.0, include_price=True):
    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "engine_version": ace.ENGINE_VERSION,
        "bundle_version": fd.BUNDLE_VERSION,
        "valuation_source_hash": ace.VALUATION_SOURCE_HASH,
    }
    if include_price:
        meta["price_at_build"] = price_at_build
    return meta


def _stale_sections(dcf_value=999999.0):
    """A fabricated cache entry whose "dcf" row is an obviously-WRONG,
    deliberately-stale value (999999) - any test that still sees this
    number after build_sections() has proved the fix did nothing."""
    return {
        "_meta": _stale_cache_meta(),
        "Fundamentals": {"metrics": []},
        "Value vs Book": {"metrics": []},
        "Retained Earnings": {"metrics": []},
        "Earnings Trends": {"metrics": []},
        "Cost of Capital": {"metrics": []},
        "Fair Value": {
            "metrics": [],
            "valuation_methods": {
                TICKER: {
                    "price": 100.0, "pe_forward": 111.0, "pe_trailing": 222.0,
                    "dcf": dcf_value, "equity_10y": 333.0,
                },
            },
            "valuation_inputs": {
                TICKER: {
                    "pe_forward": [{"label": "STALE PE FORWARD INPUT", "value": 1, "format": "raw"}],
                    "pe_trailing": [{"label": "STALE PE TRAILING INPUT", "value": 1, "format": "raw"}],
                    "dcf": [{"label": "STALE DCF INPUT - should be replaced", "value": 1, "format": "raw"}],
                    "equity_10y": [{"label": "STALE EQUITY 10Y INPUT", "value": 1, "format": "raw"}],
                },
            },
        },
    }


# ======================================================================
# CHECK 1: cache hit, live price within 3% of price_at_build (the
# "everything matches, serve the fast path" branch) - the DCF row must
# STILL be freshly recomputed live, matching the gauge's own value
# exactly; PE Forward/PE Trailing/Equity 10y must stay the STALE cached
# numbers, completely untouched.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp), \
         mock.patch.object(ace.fundamentals_data, "get_bundle", return_value=_BUNDLE), \
         mock.patch.object(ace.fundamentals_data, "get_live_price", return_value=100.0):
        _path = ace._cache_path(TICKER, _OVERRIDES)
        with open(_path, "w") as f:
            json.dump(_stale_sections(dcf_value=999999.0), f)

        _sections = ace.build_sections(
            TICKER, discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR, manual_fcf=None)

assert _sections is not None
_fv = _sections["Fair Value"]
_live_dcf = _fv["valuation_methods"][TICKER]["dcf"]
assert abs(_live_dcf - _gauge_iv) < 1e-6, (_live_dcf, _gauge_iv)
assert _live_dcf != 999999.0
print(f"[dcf_row_always_live_price_matched] cache hit (live price matched price_at_build) - "
      f"DCF row = ${_live_dcf:.2f}, exactly matches the gauge's ${_gauge_iv:.2f} (NOT the stale "
      f"cached $999999.00) OK")

assert _fv["valuation_methods"][TICKER]["pe_forward"] == 111.0
assert _fv["valuation_methods"][TICKER]["pe_trailing"] == 222.0
assert _fv["valuation_methods"][TICKER]["equity_10y"] == 333.0
assert _fv["valuation_inputs"][TICKER]["pe_forward"][0]["label"] == "STALE PE FORWARD INPUT"
assert _fv["valuation_inputs"][TICKER]["pe_trailing"][0]["label"] == "STALE PE TRAILING INPUT"
assert _fv["valuation_inputs"][TICKER]["equity_10y"][0]["label"] == "STALE EQUITY 10Y INPUT"
print("[other_methods_stay_cached] PE Forward ($111, stale), PE Trailing ($222, stale), and "
      "Equity 10y/Rational Compounder ($333, stale) all come through UNTOUCHED from the cache - "
      "only the dcf row was refreshed OK")

# The dcf row's own INPUTS (discount/perpetual/growth/etc.) were also
# refreshed, not just the headline number - proves the whole row is live,
# not just a value silently swapped in on top of stale supporting text.
_dcf_inputs = _fv["valuation_inputs"][TICKER]["dcf"]
assert _dcf_inputs != [{"label": "STALE DCF INPUT - should be replaced", "value": 1, "format": "raw"}]
_discount_row = next(r for r in _dcf_inputs if r["label"] == "Discount Rate")
assert abs(_discount_row["value"] - _gauge_meta["discount_rate_used"]) < 1e-6
print(f"[dcf_inputs_also_refreshed] the dcf row's own inputs (Discount Rate = "
      f"{_discount_row['value']:.2%}) were rebuilt from the SAME live call, matching the gauge's "
      f"discount_rate_used exactly - not just the headline value OK")


# ======================================================================
# CHECK 2: cache hit, pre-fix entry with NO price_at_build recorded at
# all (the "nothing to compare against" branch) - the DCF row must still
# be refreshed live, same as Check 1.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp), \
         mock.patch.object(ace.fundamentals_data, "get_bundle", return_value=_BUNDLE), \
         mock.patch.object(ace.fundamentals_data, "get_live_price", return_value=100.0):
        _stale = _stale_sections(dcf_value=888888.0)
        _stale["_meta"] = _stale_cache_meta(include_price=False)
        _path = ace._cache_path(TICKER, _OVERRIDES)
        with open(_path, "w") as f:
            json.dump(_stale, f)

        _sections2 = ace.build_sections(
            TICKER, discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR, manual_fcf=None)

_live_dcf2 = _sections2["Fair Value"]["valuation_methods"][TICKER]["dcf"]
assert abs(_live_dcf2 - _gauge_iv) < 1e-6, (_live_dcf2, _gauge_iv)
assert _sections2["Fair Value"]["valuation_methods"][TICKER]["pe_forward"] == 111.0
print(f"[dcf_row_always_live_no_baseline_price] cache hit (no price_at_build recorded - a pre-"
      f"fix entry) - DCF row = ${_live_dcf2:.2f}, still matches the gauge exactly, PE Forward "
      f"still stale/cached at $111.00 OK")


# ======================================================================
# CHECK 3: fail-open - if the live bundle fetch fails (e.g. a transient
# network error), the cached "dcf" row is returned UNCHANGED rather than
# blanked out or the whole render crashing.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp), \
         mock.patch.object(ace.fundamentals_data, "get_bundle", return_value=None), \
         mock.patch.object(ace.fundamentals_data, "get_live_price", return_value=100.0):
        _path = ace._cache_path(TICKER, _OVERRIDES)
        with open(_path, "w") as f:
            json.dump(_stale_sections(dcf_value=777777.0), f)

        _sections3 = ace.build_sections(
            TICKER, discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR, manual_fcf=None)

assert _sections3["Fair Value"]["valuation_methods"][TICKER]["dcf"] == 777777.0
print("[fails_open_on_bundle_fetch_failure] a failed live bundle fetch leaves the previously-"
      "cached dcf row exactly as it was ($777777.00, stale but present) rather than blanking it "
      "or raising OK")


# ======================================================================
# CHECK 4: _dcf_valuation_and_inputs() is a pure refactor - a cold/full
# build (no cache at all) produces the IDENTICAL "dcf" row it always
# did, proving the extraction out of _build_fair_value() changed nothing
# about the normal, uncached path.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp), \
         mock.patch.object(ace.fundamentals_data, "get_bundle", return_value=_FULL_BUNDLE), \
         mock.patch.object(ace.capm_engine, "get_risk_free_rate", return_value=(0.045, "live")):
        # Cost of Capital (a DIFFERENT section builder, unrelated to this
        # fix) independently wants a live risk-free rate when this test's
        # full/cold rebuild runs EVERY section - mocked purely to keep
        # this test network-free and fast; its own result plays no part
        # in any assertion below.
        _sections4 = ace.build_sections(
            TICKER, discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR, manual_fcf=None)
assert _sections4["Fair Value"].get("valuation_methods"), _sections4["Fair Value"]
_cold_dcf = _sections4["Fair Value"]["valuation_methods"][TICKER]["dcf"]
assert abs(_cold_dcf - _gauge_iv) < 1e-6, (_cold_dcf, _gauge_iv)
print(f"[cold_build_unchanged] a full/cold build (no pre-existing cache) still produces the same "
      f"${_cold_dcf:.2f} dcf row via the refactored _dcf_valuation_and_inputs() helper - the "
      "extraction is behavior-neutral on the normal path OK")


print("\nAll Fair Value always-live-DCF checks passed.")
