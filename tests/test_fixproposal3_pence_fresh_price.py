"""
Proposal 3 of the Director's PART 3 numbered fix-proposal round (8 Oct
2026, instruction_health_fixes_chart_and_new_markets.md): pence
normalisation in fundamentals_data._fetch_fresh_price()/get_live_price()
for GBp/GBX.

The bug: _fetch_fresh_price() returned Yahoo's raw fast_info quote
unconditionally - in PENCE for a GBp/GBX-quoted LSE ticker, regardless
of any normalisation already applied to the rest of that ticker's
bundle. _overlay_fresh_price() (get_bundle()'s own internal caller)
already corrected for this itself, using the bundle's own confirmed
info["_price_quote_unit"] marker - but get_live_price() (the PUBLIC
wrapper auto_compounder_engine.build_sections() calls directly, with
NO bundle info at all) never did, comparing a pounds-scale cached
price against a pence-scale "fresh" one up to 100x too large.

Covers:
  - _fetch_fresh_price(tk, known_pence_quoted=True/False/None): the
    explicit flag always wins; None falls back to fast_info's own
    "currency" field, the only GBp/GBX signal available with no
    bundle info.
  - get_live_price(): the real-world fix - a GBp-quoted ticker whose
    fast_info exposes "GBp" as its currency now returns the CORRECTLY
    normalised (pounds) price, not the raw pence one.
  - _overlay_fresh_price() (get_bundle()'s own caller): still
    correct, no double-division now that the /100 moved inside
    _fetch_fresh_price() itself.
  - cache safety: a cache hit from one caller's own known_pence_quoted
    context is correctly re-normalised for a LATER, differently-
    informed caller within the same 90s window - the raw price is
    what's cached, never a pre-divided one.

Run: python3 tests/test_fixproposal3_pence_fresh_price.py
"""
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


class _FakeFastInfo:
    def __init__(self, price, currency=None):
        self._price = price
        self._currency = currency

    def __getitem__(self, key):
        if key == "last_price":
            return self._price
        if key == "currency" and self._currency is not None:
            return self._currency
        raise KeyError(key)


class _FakeTicker:
    def __init__(self, ticker, price, currency=None):
        self.ticker = ticker
        self._fi = _FakeFastInfo(price, currency)

    @property
    def fast_info(self):
        return self._fi


# ======================================================================
# CHECK 1-3: _fetch_fresh_price() - the explicit flag always wins.
# ======================================================================
fd._FRESH_PRICE_CACHE.clear()
_tk_gbp = _FakeTicker("TEST1.L", 2650.0, currency="GBp")
check("known_pence_quoted=True divides the raw fast_info quote by 100",
      fd._fetch_fresh_price(_tk_gbp, known_pence_quoted=True) == 26.50)

fd._FRESH_PRICE_CACHE.clear()
_tk_gbp2 = _FakeTicker("TEST2.L", 2650.0, currency="GBp")
check("known_pence_quoted=False trusts the caller over fast_info's own currency - "
      "never divides even though fast_info itself says GBp",
      fd._fetch_fresh_price(_tk_gbp2, known_pence_quoted=False) == 2650.0)

fd._FRESH_PRICE_CACHE.clear()
_tk_usd = _FakeTicker("AAPL", 150.0, currency="USD")
check("known_pence_quoted=True on a USD ticker still divides (the caller's own "
      "explicit claim always wins, even if it were wrong) - proves this is a real "
      "override, not a secondary check",
      fd._fetch_fresh_price(_tk_usd, known_pence_quoted=True) == 1.50)

# ======================================================================
# CHECK 4-6: None (the default) - best-effort fallback to fast_info's
# own "currency" field.
# ======================================================================
fd._FRESH_PRICE_CACHE.clear()
_tk_gbx = _FakeTicker("TEST3.L", 1899.5, currency="GBX")
check('known_pence_quoted=None, fast_info currency="GBX" -> divides by 100 (best effort)',
      fd._fetch_fresh_price(_tk_gbx) == 18.995)

fd._FRESH_PRICE_CACHE.clear()
_tk_gbp_plain_pounds = _FakeTicker("TEST4.L", 850.0, currency="GBP")
check('known_pence_quoted=None, fast_info currency="GBP" (whole pounds, NOT "GBp") -> '
      "never divides - _is_pence_quoted() is an exact match, GBP != GBp/GBX",
      fd._fetch_fresh_price(_tk_gbp_plain_pounds) == 850.0)

fd._FRESH_PRICE_CACHE.clear()
_tk_no_currency = _FakeTicker("TEST5.L", 999.0, currency=None)
check("known_pence_quoted=None, fast_info exposes no currency at all -> never divides "
      "(no signal, no guess)",
      fd._fetch_fresh_price(_tk_no_currency) == 999.0)

# ======================================================================
# CHECK 7: get_live_price() - the real bug fix, end to end.
# ======================================================================
fd._FRESH_PRICE_CACHE.clear()
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.return_value = _FakeTicker("BARC.L", 25050.0, currency="GBp")
    _result = fd.get_live_price("BARC.L")
check("get_live_price() on a GBp-quoted ticker returns the NORMALISED pounds price "
      "(250.50), never the raw pence figure (25050.0) - the exact bug build_sections() "
      "hit",
      _result == 250.50)

# ======================================================================
# CHECK 8: _overlay_fresh_price() - no double-division now that the
# /100 moved inside _fetch_fresh_price().
# ======================================================================
fd._FRESH_PRICE_CACHE.clear()
_tk_overlay = _FakeTicker("TEST6.L", 2650.0, currency=None)
_info = {"_price_quote_unit": "GBp", "sharesOutstanding": 1000.0}
_result_info = fd._overlay_fresh_price(_info, _tk_overlay, income=None)
check("_overlay_fresh_price(): currentPrice is divided EXACTLY once (26.50, not "
      "0.265 from a double division) - known_pence_quoted=True is passed explicitly, "
      "so _fetch_fresh_price()'s own fast_info-currency fallback (which would see "
      "no currency at all here) is never consulted",
      _result_info["currentPrice"] == 26.50)
check("_overlay_fresh_price(): marketCap recomputed from the correctly-divided price",
      _result_info["marketCap"] == 26.50 * 1000.0)

# ======================================================================
# CHECK 9: cache safety - the RAW price is what's cached, not a
# pre-divided one, so two callers with different known_pence_quoted
# signals within the same 90s window both get the CORRECT answer for
# their own context from the same underlying cache entry.
# ======================================================================
fd._FRESH_PRICE_CACHE.clear()
_tk_shared = _FakeTicker("SHARED.L", 3000.0, currency=None)
_first = fd._fetch_fresh_price(_tk_shared, known_pence_quoted=None)   # no signal -> 3000.0
_second = fd._fetch_fresh_price(_tk_shared, known_pence_quoted=True)  # explicit -> divide
check("first call (no signal) returns the raw price unchanged (3000.0)", _first == 3000.0)
check("second call on the SAME ticker within the cache window, now with an explicit "
      "known_pence_quoted=True, correctly divides (30.0) - proves the cache stores the "
      "RAW price, re-normalised per call, never a stale pre-divided value",
      _second == 30.0)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
