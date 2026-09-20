"""
market_cap_engine.py

Per-ticker market cap lookups, backed by yfinance. Two entry points:

- get_market_cap(ticker, info=None): back-compat, always returns a number
  (0 on any failure), never raises - existing callers (get_market_cap_
  bucket, app.py's cache-warming pass) keep working unchanged.
- get_market_cap_checked(ticker, info=None): (market_cap, ok) - lets a
  caller that prices many tickers at once (scanner_engine's nightly
  market-cap ranking) tell a genuine "this real security has no market
  cap" from a failed/throttled lookup, so it can retry the latter instead
  of silently recording a wrong zero.

Commit D (20 Sep 2026): both now try yf.Ticker(ticker).fast_info first -
a lightweight endpoint (last price, market cap, previous close, day
range) that avoids the full .info scrape (dozens of fields: officers,
description, every valuation ratio, etc.) - falling back to .info only
when fast_info doesn't yield a usable market cap. This matters most for
scanner_engine._rebuild_market_cap_ranking(), which prices on the order
of a thousand tickers a night; a cheaper per-ticker call directly lowers
both wall-clock time and the odds of tripping Yahoo's throttling. This
sandbox has no live network route to Yahoo Finance (every fetch attempt
here gets a 403 from the outbound proxy - a standing constraint for this
whole engagement, same as every other yfinance-touching commit), so the
before/after timing this change is meant to produce could not be measured
directly; see Commit D's own report for what was verified instead.
"""

import yfinance as yf

# A real, populated yfinance .info response for an active ticker normally
# carries dozens of keys; when yfinance recognises a ticker is real but
# has no useful data left to serve (throttled, or the request otherwise
# came back empty rather than raising), it's been observed returning a
# near-empty dict instead of an exception. This floor is how
# get_market_cap_checked() tells that failure shape apart from a real,
# fully-populated response that simply has no marketCap field (some ASX-
# listed LICs/stapled trusts/hybrids genuinely don't report one).
_MIN_INFO_KEYS_FOR_REAL_RESPONSE = 5


def _fast_info_market_cap(ticker):
    """The lightweight fast_info market cap for `ticker`, or None if
    fast_info itself raised or came back without a usable value - never
    raises. fast_info is dict-like (supports __getitem__) rather than a
    plain dict, so this indexes it rather than calling .get()."""
    try:
        fast = yf.Ticker(ticker).fast_info
    except Exception:
        return None
    for key in ("market_cap", "marketCap"):
        try:
            value = fast[key]
        except Exception:
            continue
        if value:
            return value
    return None


def get_market_cap_checked(ticker, info=None):
    """(market_cap, ok). ok=False means the lookup itself failed or came
    back looking throttled/empty - the caller should retry rather than
    trust a silent 0 as a real answer. ok=True with market_cap=0 means a
    real, populated response was returned for `ticker` that genuinely
    carries no market cap.

    `info` (optional): an already-fetched yfinance .info dict for this
    ticker (a caller that fetched it for other fields too can pass it in
    to avoid a second network round trip) - skips fast_info entirely in
    that case, exactly like the pre-existing behaviour this preserves.
    """
    if info is not None:
        ok = len(info) >= _MIN_INFO_KEYS_FOR_REAL_RESPONSE
        return (info.get("marketCap", 0) or 0), ok

    fast_cap = _fast_info_market_cap(ticker)
    if fast_cap:
        return fast_cap, True

    try:
        fetched_info = yf.Ticker(ticker).info
    except Exception:
        return 0, False

    if not fetched_info or len(fetched_info) < _MIN_INFO_KEYS_FOR_REAL_RESPONSE:
        return 0, False

    # yfinance frequently returns an explicit None (not a missing key)
    # for marketCap, so `.get(key, 0)` alone isn't enough - `or 0` catches
    # that case too.
    return (fetched_info.get("marketCap", 0) or 0), True


def get_market_cap(ticker, info=None):
    market_cap, _ok = get_market_cap_checked(ticker, info=info)
    return market_cap


def get_market_cap_bucket(ticker, info=None):

    market_cap = get_market_cap(ticker, info=info)

    if market_cap > 100_000_000_000:
        return "LARGE"

    elif market_cap > 10_000_000_000:
        return "MID"

    else:
        return "SMALL"
