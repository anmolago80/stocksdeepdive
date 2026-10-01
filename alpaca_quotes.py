"""
alpaca_quotes.py

US quote recorder, real bid/ask source: Alpaca's Market Data API (IEX
feed), used in place of Yahoo for the US market only - see quote_
recorder.py's own module docstring for why Yahoo's light quote endpoint
alone isn't enough for US bid/ask (no paid entitlement, most US rows
come back with no usable quote at all) and the owner-reported 29 Sep
log line (25/133 captured) this module exists to fix.

Env vars: ALPACA_API_KEY / ALPACA_SECRET_KEY (required - see
available() below), ALPACA_DATA_FEED (optional, default "iex"),
ALPACA_PAPER (optional - "1" selects the paper clock URL for
market_state() below; either paper or live keys/clock work fine for
read-only market-data calls, this only changes WHICH clock endpoint is
hit). Read fresh via os.environ.get on every call, never cached at
import time, so a key set after this process started still takes
effect on the next scheduler tick.

FAIL-OPEN, same convention as every other optional integration in this
codebase: available() is False whenever either key is missing, and
every fetch function below degrades to "return what was obtained" (an
empty dict, or a partial one) rather than raising - quote_recorder.py's
own _record_market() is the only caller, and it already treats "this
ticker has no quote yet" as the ordinary REJECT_MISSING case, so a
total Alpaca outage here just means every ticker falls through to the
existing Yahoo path, not a broken run.

Yahoo-style tickers vs Alpaca symbols: this codebase's own tickers use
a dash for a share class (BRK-B); Alpaca's symbols use a dot (BRK.B).
_to_alpaca_symbol()/_to_yahoo_symbol() translate one way then the
other so every OTHER module in this codebase - which only ever knows
Yahoo-style tickers - never sees an Alpaca-shaped symbol. A ticker with
a dot in it already (every non-US suffix this codebase uses, e.g.
ASX's ".AX") is never a US symbol and is always skipped before this
module makes a request - see _is_us_symbol().
"""

import os
import time

import requests

_CHUNK_SIZE = 100
_TIMEOUT_SECONDS = 10
_RETRY_PAUSE_SECONDS = 2.0

_QUOTES_URL = "https://data.alpaca.markets/v2/stocks/quotes/latest"
_TRADES_URL = "https://data.alpaca.markets/v2/stocks/trades/latest"
_CLOCK_URL_LIVE = "https://api.alpaca.markets/v2/clock"
_CLOCK_URL_PAPER = "https://paper-api.alpaca.markets/v2/clock"


def available():
    """True once both required keys are set. The one gate quote_
    recorder.py checks before routing the US market through this
    module at all - False means every function below would just
    return empty anyway, but the caller skips calling them entirely so
    today's Yahoo path runs completely unchanged."""
    return bool(os.environ.get("ALPACA_API_KEY")) and bool(os.environ.get("ALPACA_SECRET_KEY"))


def _headers():
    return {
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }


def _data_feed():
    return os.environ.get("ALPACA_DATA_FEED") or "iex"


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _is_us_symbol(ticker):
    """True for a plain US-style ticker. Every non-US ticker this
    codebase ever handles carries a dot exchange suffix (ASX's ".AX" -
    see quote_recorder.py's own _MARKET_UNIVERSE); a US share class
    uses a dash instead (BRK-B), never a dot - so "no dot" is exactly
    "US", with no per-exchange suffix list to keep in sync here."""
    return "." not in ticker


def _to_alpaca_symbol(ticker):
    """Yahoo-style ticker -> Alpaca symbol: share-class dash becomes a
    dot (BRK-B -> BRK.B). Only ever called on a ticker _is_us_symbol()
    already passed, so there is no exchange-suffix dot to collide
    with."""
    return ticker.replace("-", ".")


def _to_yahoo_symbol(symbol):
    """Inverse of _to_alpaca_symbol() - dot back to dash (BRK.B ->
    BRK-B), so every caller outside this module keeps seeing only the
    Yahoo-style tickers it already knows."""
    return symbol.replace(".", "-")


def _get(url, params, log=print):
    """One GET with this module's auth headers, a 10s timeout, and
    exactly one retry on 429/5xx after a 2s pause - plain `requests`,
    no SDK dependency, per the task spec. Returns the parsed JSON body,
    or None on any failure (network error, non-2xx after the retry,
    unparseable body) - never raises. Logs one WARNING per failed
    request/chunk, the caller treats None exactly like "this chunk
    returned nothing"."""
    for attempt in (0, 1):
        try:
            resp = requests.get(url, params=params, headers=_headers(),
                                 timeout=_TIMEOUT_SECONDS)
        except Exception as e:
            log(f"[alpaca_quotes] WARNING: request to {url} failed: {e}")
            return None
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception as e:
                log(f"[alpaca_quotes] WARNING: unparseable response from {url}: {e}")
                return None
        if attempt == 0 and (resp.status_code == 429 or resp.status_code >= 500):
            time.sleep(_RETRY_PAUSE_SECONDS)
            continue
        log(f"[alpaca_quotes] WARNING: request to {url} failed: HTTP {resp.status_code}")
        return None
    return None


def latest_quotes(symbols, log=print):
    """{ticker: {"bid","ask","bid_size","ask_size","ts"}, ...} for every
    `symbols` entry Alpaca's IEX feed actually returned a quote for -
    chunked at _CHUNK_SIZE per GET .../quotes/latest request. A non-US
    symbol (a dotted exchange suffix) is silently skipped, never sent
    to Alpaca at all. Never raises - a failed chunk just contributes no
    entries (see _get() above for the one-retry/never-raise contract)."""
    us_tickers = [t for t in symbols if _is_us_symbol(t)]
    alpaca_to_yahoo = {_to_alpaca_symbol(t): t for t in us_tickers}
    out = {}
    for chunk in _chunked(list(alpaca_to_yahoo), _CHUNK_SIZE):
        data = _get(_QUOTES_URL, {"symbols": ",".join(chunk), "feed": _data_feed()}, log=log)
        if not data:
            continue
        for sym, q in (data.get("quotes") or {}).items():
            ticker = alpaca_to_yahoo.get(sym) or _to_yahoo_symbol(sym)
            out[ticker] = {
                "bid": q.get("bp"),
                "ask": q.get("ap"),
                "bid_size": q.get("bs"),
                "ask_size": q.get("as"),
                "ts": q.get("t"),
            }
    return out


def latest_trade_price(symbols, log=print):
    """{ticker: last_trade_price, ...} via .../trades/latest, same
    chunking/symbol-mapping/fail-open contract as latest_quotes() above.
    Only positive numeric prices are included - a missing or malformed
    trade for a symbol simply isn't a key in the returned dict, so the
    caller's own `.get(ticker)` -> None already means "fall back to
    Yahoo's last price for this one", exactly as the task spec asks."""
    us_tickers = [t for t in symbols if _is_us_symbol(t)]
    alpaca_to_yahoo = {_to_alpaca_symbol(t): t for t in us_tickers}
    out = {}
    for chunk in _chunked(list(alpaca_to_yahoo), _CHUNK_SIZE):
        data = _get(_TRADES_URL, {"symbols": ",".join(chunk), "feed": _data_feed()}, log=log)
        if not data:
            continue
        for sym, t in (data.get("trades") or {}).items():
            ticker = alpaca_to_yahoo.get(sym) or _to_yahoo_symbol(sym)
            price = t.get("p")
            if isinstance(price, (int, float)) and price > 0:
                out[ticker] = price
    return out


def market_state(log=print):
    """{"is_open": True/False/None} from Alpaca's own trading clock -
    the paper clock URL when ALPACA_PAPER=1 (paper keys/clock, used
    here since the owner's Alpaca keys are a paper account), the live
    one otherwise; either answers the same real NYSE/Nasdaq calendar
    question for read-only market-data purposes. None (never a bare
    False) on any failure - the caller treats "unknown" conservatively,
    the same as "not open", rather than guessing."""
    url = _CLOCK_URL_PAPER if os.environ.get("ALPACA_PAPER") == "1" else _CLOCK_URL_LIVE
    data = _get(url, {}, log=log)
    if not data or "is_open" not in data:
        return {"is_open": None}
    return {"is_open": bool(data["is_open"])}
