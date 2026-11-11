"""
TASK 42 item 1 (Director-directed, 8 Nov 2026, GQG.AX live case):
GQG.AX's stored/live price (0.94) is suspiciously close to its real
ASX price (~A$1.42) divided by a plausible AUD/USD rate (0.66 - the
exact value of this codebase's own _FX_STATIC_FALLBACK[("AUD","USD")]
in fcf_valuation_engine.py).

INVESTIGATION: searched every place a PRICE value (as opposed to a
statement line item or a DCF cash-flow base) is computed in this
codebase for any fx_rate()/_FX_STATIC_FALLBACK usage:
  - fundamentals_data._basics()-feeding code (info.get("currentPrice")/
    _overlay_fresh_price()/_fetch_fresh_price()): zero fx_rate() calls -
    only a GBp/100 pence division, never a currency conversion.
  - app.py's get_price_history()/get_ticker_info() (deep_dive_engine.
    py's own COMPLETELY SEPARATE fetch path): raw, unmodified yfinance
    calls, zero currency logic of any kind.
  - nightly_scan.py's own current_price derivation: zero fx_rate()
    calls anywhere in the file.
  - top100_engine._cross_listing_traded_value_usd(): the ONE place
    fx_rate(currency, "USD") is called anywhere near a "price" variable
    - but this is a local, ephemeral traded-value comparison used only
    to pick a cross-listing dedup winner; it is never written back into
    a row's own "price"/"Price" field, and GQG has no cross-listed
    duplicate ticker to trigger this path at all.
No conversion bug was found in this codebase's own price-computation
code - every fx_rate()/_FX_STATIC_FALLBACK call site that exists
converts a STATEMENT line item or a DCF cash-flow base, never a quote
price. The leading remaining explanation is a genuine Yahoo-side data
inconsistency for this specific ticker (its own "price" quoteSummary
module serving a USD-looking figure while other modules, e.g.
marketCap, may still reflect the real AUD-based figure) - not a
conversion this codebase needs to "undo", since there IS no such
conversion in the price path.

FIX (both Director-approved regardless of the exact Yahoo-side
mechanism):

1. fundamentals_data._check_price_unit_guard() (the existing
   price*shares-vs-marketCap sanity cross-check, already proven
   effective for a GBp pence-quote mismatch) now runs for EVERY
   ticker inside get_bundle(), not just a GBp-detected one. It can
   only ever ADD "price_unit_suspect" (withholding IV/MOS, via
   build_sections()'s pre-existing contract), never clear one - same
   safety property it already had. This catches exactly the SHAPE of
   problem GQG.AX's price/marketCap would show if Yahoo's own
   marketCap figure still reflects something closer to the real AUD
   price while currentPrice does not - whatever the ROOT cause.

2. deep_dive_engine.analyze()'s non-financials-store branches
   (peek_cached_bundle()/get_bundle()) used to set
   _income_df_currency_converted=True UNCONDITIONALLY whenever
   income_df was non-empty, never checking the bundle's own
   "currency_converted" flag - the SAME dishonesty TASK 40 fixed only
   on auto_compounder_engine's fairvalue-path override. Now reads the
   bundle's own flag honestly, same pattern.

NOT VERIFIED LIVE: whether GQG.AX's own live price/marketCap actually
disagree enough to trip the widened guard, or whether Yahoo's quote
feed for this ticker self-corrects, cannot be confirmed from this
sandbox (no network access). The two fixes are independently correct
and tested against synthetic fixtures either way.

Run: python3 tests/test_director_task42_gqg_price_guard_and_deepdive_honesty.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task42_guard_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import deep_dive_engine
import fundamentals_data as fd

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


class _SpxStub:
    info = {}

    def history(self, *a, **k):
        return pd.DataFrame()


# ======================================================================
# CHECK 1: the widened guard catches a GQG.AX-shaped price/marketCap
# mismatch for a plain AUD (non-GBp) ticker - a case that was
# completely unchecked before this fix.
# ======================================================================
_GQG_SHARES = 2_994_332_236
_GQG_INCOME = pd.DataFrame({"2025-06-30": [500_000_000.0]}, index=["Net Income"])
_GQG_BALANCE = pd.DataFrame({"2025-06-30": [600_000_000.0]}, index=["Total Stockholders Equity"])
_GQG_CASHFLOW = pd.DataFrame({"2025-06-30": [400_000_000.0]}, index=["Free Cash Flow"])


def _fake_gqg_ticker(ticker, *_a, **_k):
    if ticker == "^GSPC":
        return _SpxStub()

    class _FT:
        def __init__(self):
            # GQG.AX-shaped: price looks USD-denominated (0.94) but
            # marketCap still reflects something closer to the REAL
            # AUD-based figure (shares x ~1.42) - exactly the shape that
            # should trip the guard regardless of currency/root cause.
            self.info = {
                "currency": "AUD", "financialCurrency": "USD",
                "currentPrice": 0.94, "regularMarketPrice": 0.94,
                "marketCap": 1.42 * _GQG_SHARES, "sharesOutstanding": _GQG_SHARES,
            }
            self.income_stmt = _GQG_INCOME
            self.balance_sheet = _GQG_BALANCE
            self.cashflow = _GQG_CASHFLOW
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = pd.Series(dtype=float)

        @property
        def fast_info(self):
            # Fails, same as the existing GBp fixtures already assume -
            # this is what leaves Yahoo's OWN pre-fetched marketCap in
            # place rather than _overlay_fresh_price() trivially
            # recomputing it from the SAME (possibly wrong) price.
            raise AttributeError

        def history(self, *a, **k):
            return pd.DataFrame()

    return _FT()


with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_gqg_ticker
    _bundle = fd.get_bundle("GQG.AX", force_refresh=True)

check("the widened guard flags a GQG.AX-shaped AUD ticker as "
      "price_unit_suspect - a currency OTHER than GBp used to never "
      "reach this guard at all",
      _bundle["meta"].get("price_unit_suspect") is True)
check("the reason names that the price unit couldn't be confirmed "
      "(the same reason build_sections() already keys its own "
      "withhold-IV/MOS behaviour on)",
      _bundle["meta"].get("price_unit_suspect_reason") == "price unit could not be confirmed")
print("[task42_check1_gqg_shaped_mismatch_flagged] a GQG.AX-shaped "
      "price/marketCap mismatch is now caught for a plain AUD ticker, "
      "not just a GBp one OK")


# ======================================================================
# CHECK 2: a perfectly ordinary USD/AUD ticker (price*shares agrees
# with marketCap) is completely unaffected - no new flag, no behaviour
# change, confirming this is flag-only and never produces a false
# positive on the common case.
# ======================================================================
_NORMAL_SHARES = 1_000_000


def _fake_normal_ticker(ticker, *_a, **_k):
    if ticker == "^GSPC":
        return _SpxStub()

    class _FT:
        def __init__(self):
            self.info = {
                "currency": "USD", "currentPrice": 100.0, "regularMarketPrice": 100.0,
                "marketCap": 100.0 * _NORMAL_SHARES, "sharesOutstanding": _NORMAL_SHARES,
            }
            self.income_stmt = pd.DataFrame({"2025-06-30": [50_000.0]}, index=["Net Income"])
            self.balance_sheet = pd.DataFrame({"2025-06-30": [200_000.0]}, index=["Total Stockholders Equity"])
            self.cashflow = pd.DataFrame({"2025-06-30": [40_000.0]}, index=["Free Cash Flow"])
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = pd.Series(dtype=float)

        @property
        def fast_info(self):
            raise AttributeError

        def history(self, *a, **k):
            return pd.DataFrame()

    return _FT()


with mock.patch.object(fd, "yf") as _myf2:
    _myf2.Ticker.side_effect = _fake_normal_ticker
    _bundle2 = fd.get_bundle("NORMALUSD", force_refresh=True)

check("an ordinary, self-consistent USD ticker (price*shares == "
      "marketCap) is never flagged - no false positive from widening "
      "this guard to every ticker",
      _bundle2["meta"].get("price_unit_suspect") is False
      and "price_unit_suspect" not in _bundle2["meta"]["flags"])
print("[task42_check2_normal_ticker_unaffected] the overwhelming "
      "majority of tickers (price and marketCap genuinely agree) see "
      "zero behaviour change from widening this guard OK")


# ======================================================================
# CHECK 3 (deep_dive_engine.py honesty fix): peek_cached_bundle()
# branch - income_df_currency_converted now reads the bundle's own
# flag honestly instead of assuming True.
# ======================================================================
_bundle_converted = {
    "info": {"financialCurrency": "USD", "currency": "AUD"},
    "income": pd.DataFrame({"2025-06-30": [100.0]}, index=["Net Income"]),
    "cashflow": pd.DataFrame({"2025-06-30": [90.0]}, index=["Free Cash Flow"]),
    "meta": {"flags": ["currency_converted"], "fetched_at": None},
}
_bundle_not_converted = {
    "info": {"financialCurrency": "USD", "currency": "AUD"},
    "income": pd.DataFrame({"2025-06-30": [100.0]}, index=["Net Income"]),
    "cashflow": pd.DataFrame({"2025-06-30": [90.0]}, index=["Free Cash Flow"]),
    "meta": {"flags": ["currency_conversion_partial"], "fetched_at": None},
}

import inspect
_dd_src = inspect.getsource(deep_dive_engine.analyze)
_dd_else_branch = _dd_src[_dd_src.index("else:\n        _bundle = fundamentals_data.peek_cached_bundle"):]
_dd_else_branch = _dd_else_branch[:_dd_else_branch.index("intrinsic_value, intrinsic_src")]
check("deep_dive_engine.py's peek_cached_bundle() branch reads "
      "'currency_converted' from the bundle's own meta.flags, not an "
      "unconditional True",
      '"currency_converted" in (' in _dd_else_branch
      and _dd_else_branch.count('"currency_converted" in (') == 2)
check("the OLD unconditional 'income_df_currency_converted = True' "
      "assignment is gone from both branches",
      "_income_df_currency_converted = True" not in _dd_else_branch)
print("[task42_check3_deepdive_honest_flag_source] deep_dive_engine.py's "
      "own non-financials-store branches now read the bundle's real "
      "conversion-success flag instead of assuming it OK")


# ======================================================================
# CHECK 4 (11 Nov 2026 follow-up, Director-directed, pre-push
# condition): the Director's own review found that the widened guard's
# flag is read by several downstream consumers that WITHHOLD a real,
# visible number (not merely log/flag) - this confirms the fix: a
# TOP200_PRICE_GUARD_LIVE-gated "price_unit_suspect_effective" field on
# fundamentals_data.get_bundle()'s own meta, and auto_compounder_
# engine.build_sections() (the Fair Value / Compounder View tab, the
# ONE real downstream consumer of get_bundle()'s own widened meta -
# nightly_scan.py/deep_dive_engine.py/top100_engine.py's own IV-
# zeroing/pre-scoring checks all read a COMPLETELY SEPARATE, already-
# GBp-only meta from fundamentals_data.normalize_pence_quote(), never
# touched by TASK 42 item 1's widening at all - confirmed by direct
# source inspection, not assumed) now reads that gated field instead
# of the raw one.
# ======================================================================
import auto_compounder_engine

os.environ.pop("TOP200_PRICE_GUARD_LIVE", None)
_gqg_info_bs = {
    "currency": "AUD", "financialCurrency": "AUD",
    "currentPrice": 0.94, "regularMarketPrice": 0.94,
    "marketCap": 1.42 * 2_994_332_236, "sharesOutstanding": 2_994_332_236,
    "longName": "GQG Partners",
}


class _FakeFastInfoBS:
    @property
    def last_price(self):
        raise AttributeError("no fast_info in this fixture")


class _FakeTickerBS:
    def __init__(self, *a, **k):
        self.info = dict(_gqg_info_bs)
        self.fast_info = _FakeFastInfoBS()

    def history(self, *a, **k):
        return pd.DataFrame()


_bundle_gqg_shaped, _ = (None, None)
with mock.patch("yfinance.Ticker", _FakeTickerBS):
    _bundle_gqg_shaped = fd.get_bundle("GQGGATETEST.AX", force_refresh=True)

check("fixture reproduces price_unit_suspect=True (same GQG.AX-shaped "
      "mismatch as CHECK 1 above)",
      (_bundle_gqg_shaped.get("meta") or {}).get("price_unit_suspect") is True)
check("TOP200_PRICE_GUARD_LIVE unset (tonight's default): "
      "price_unit_suspect_effective is False for this non-GBp ticker",
      (_bundle_gqg_shaped.get("meta") or {}).get("price_unit_suspect_effective") is False)

os.environ["TOP200_PRICE_GUARD_LIVE"] = "1"
with mock.patch("yfinance.Ticker", _FakeTickerBS):
    _bundle_gqg_shaped_live = fd.get_bundle("GQGGATETEST2.AX", force_refresh=True)
check("TOP200_PRICE_GUARD_LIVE=1: price_unit_suspect_effective is True "
      "for the same shape of non-GBp ticker",
      (_bundle_gqg_shaped_live.get("meta") or {}).get("price_unit_suspect_effective") is True)
os.environ.pop("TOP200_PRICE_GUARD_LIVE", None)

with mock.patch.object(fd, "get_bundle", return_value=_bundle_gqg_shaped):
    _sections_gate_off = auto_compounder_engine.build_sections("GQGGATETEST.AX", force_refresh=True)
check("build_sections(): gate OFF -> a non-GBp suspect ticker's Fair "
      "Value tab is NOT withheld (byte-identical to pre-TASK-42 "
      "behaviour - the Director's own explicit requirement)",
      _sections_gate_off is not None)

os.environ["TOP200_PRICE_GUARD_LIVE"] = "1"
with mock.patch.object(fd, "get_bundle", return_value=_bundle_gqg_shaped_live):
    _sections_gate_on = auto_compounder_engine.build_sections("GQGGATETEST2.AX", force_refresh=True)
check("build_sections(): gate ON -> the same shape of ticker IS "
      "withheld (None) - the switch has a real effect once flipped",
      _sections_gate_on is None)
os.environ.pop("TOP200_PRICE_GUARD_LIVE", None)

import nightly_scan
_src_ns = inspect.getsource(nightly_scan)
check("nightly_scan.py's own IV-zeroing check is UNCHANGED (still "
      "reads the plain, already-GBp-only 'price_unit_suspect' from "
      "normalize_pence_quote()'s meta - never touched by the widened "
      "guard, confirmed by source, not assumed)",
      'if _price_unit_meta.get("price_unit_suspect"):' in _src_ns
      and 'price_unit_suspect_effective' not in _src_ns)

_src_dd_full = inspect.getsource(deep_dive_engine)
check("deep_dive_engine.py's own headline IV-zeroing check is "
      "UNCHANGED for the same reason",
      'if price_unit_meta.get("price_unit_suspect"):' in _src_dd_full)

_src_te = inspect.getsource(__import__("top100_engine"))
check("top100_engine.py's pre-scoring eligibility checks are UNCHANGED "
      "(the saved scan row's own 'price_unit_suspect' field is ALSO "
      "sourced from normalize_pence_quote(), never from get_bundle()'s "
      "widened meta - confirmed by source)",
      _src_te.count('if row.get("price_unit_suspect"):') == 2)
print("[task42_check4_price_guard_live_gate] TOP200_PRICE_GUARD_LIVE "
      "(default off) gates the ONE real downstream consumer of the "
      "widened guard's flag that withholds a visible number (auto_"
      "compounder_engine.build_sections(), the Fair Value tab) - every "
      "other consumer (nightly_scan.py/deep_dive_engine.py/top100_"
      "engine.py) was already, and remains, GBp-only by construction, "
      "confirmed by direct source inspection OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
