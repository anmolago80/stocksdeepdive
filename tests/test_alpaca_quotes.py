"""
Alpaca US quotes task (1 Oct 2026): the US quote recorder's bid/ask now
comes from Alpaca's Market Data API (IEX feed) when ALPACA_API_KEY/
ALPACA_SECRET_KEY are set, instead of Yahoo's light quote endpoint,
which doesn't return a usable real-time US bid/ask without a paid
entitlement (owner-reported: 25/133 captured on 29 Sep). ASX is
untouched - always Yahoo.

Covers: alpaca_quotes.py's own chunking/symbol-mapping/retry/fail-open
contract, and quote_recorder.py's _fetch_quotes_for_market() wiring
(Yahoo fallback for Alpaca-missing symbols, fail-open to the pre-
existing Yahoo path with no keys, source tagging through to
quote_snapshot_store.record_snapshot(), and the ASX path staying
byte-identical).

Run: python3 tests/test_alpaca_quotes.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alpaca_quotes
import quote_recorder


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


_KEYS = {"ALPACA_API_KEY": "test-key-id", "ALPACA_SECRET_KEY": "test-secret"}

# ======================================================================
# alpaca_quotes.py unit tests
# ======================================================================

# available() tracks the two required env vars, nothing else.
with mock.patch.dict(os.environ, _KEYS, clear=False):
    assert alpaca_quotes.available() is True
with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}, clear=False):
    assert alpaca_quotes.available() is False
print("[available] True only when both keys are set")

# Symbol mapping both ways.
assert alpaca_quotes._to_alpaca_symbol("BRK-B") == "BRK.B"
assert alpaca_quotes._to_yahoo_symbol("BRK.B") == "BRK-B"
assert alpaca_quotes._to_yahoo_symbol(alpaca_quotes._to_alpaca_symbol("AAPL")) == "AAPL"
assert alpaca_quotes._is_us_symbol("AAPL") is True
assert alpaca_quotes._is_us_symbol("BRK-B") is True
assert alpaca_quotes._is_us_symbol("BHP.AX") is False
print("[symbol_mapping] BRK-B <-> BRK.B, .AX skipped as non-US")

# Chunking at 100: 150 symbols -> two requests, sized 100 then 50.
_symbols_150 = [f"T{i}" for i in range(150)]
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests:
    _mock_requests.get.return_value = _FakeResp(200, {"quotes": {}})
    alpaca_quotes.latest_quotes(_symbols_150, log=lambda *a, **k: None)
    _calls = _mock_requests.get.call_args_list
    assert len(_calls) == 2, len(_calls)
    _first_symbols = _calls[0].kwargs["params"]["symbols"].split(",")
    _second_symbols = _calls[1].kwargs["params"]["symbols"].split(",")
    assert len(_first_symbols) == 100, len(_first_symbols)
    assert len(_second_symbols) == 50, len(_second_symbols)
print("[chunking] 150 symbols -> two requests (100, 50)")

# latest_quotes() end to end: Alpaca's dotted symbol maps back to the
# dashed Yahoo ticker, bp/ap/bs/as/t parsed into bid/ask/bid_size/
# ask_size/ts.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests:
    _mock_requests.get.return_value = _FakeResp(200, {
        "quotes": {"BRK.B": {"bp": 410.1, "ap": 410.3, "bs": 2, "as": 5, "t": "2026-10-01T17:00:00Z"}}
    })
    _out = alpaca_quotes.latest_quotes(["BRK-B"], log=lambda *a, **k: None)
    assert set(_out) == {"BRK-B"}, _out
    assert _out["BRK-B"]["bid"] == 410.1 and _out["BRK-B"]["ask"] == 410.3
    assert _out["BRK-B"]["bid_size"] == 2 and _out["BRK-B"]["ask_size"] == 5
    assert _out["BRK-B"]["ts"] == "2026-10-01T17:00:00Z"
print("[latest_quotes] BRK.B response parsed back onto Yahoo-style BRK-B")

# A non-US (dotted-suffix) ticker is never sent to Alpaca at all.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests:
    _mock_requests.get.return_value = _FakeResp(200, {"quotes": {}})
    alpaca_quotes.latest_quotes(["AAPL", "BHP.AX"], log=lambda *a, **k: None)
    _sent = _mock_requests.get.call_args.kwargs["params"]["symbols"]
    assert "BHP" not in _sent and "AX" not in _sent, _sent
    assert _sent == "AAPL", _sent
print("[latest_quotes] non-US suffixed ticker excluded from the request")

# 429 -> exactly one retry, then success.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests, \
     mock.patch.object(alpaca_quotes.time, "sleep") as _mock_sleep:
    _mock_requests.get.side_effect = [
        _FakeResp(429), _FakeResp(200, {"quotes": {"AAPL": {"bp": 1.0, "ap": 1.1}}}),
    ]
    _out = alpaca_quotes.latest_quotes(["AAPL"], log=lambda *a, **k: None)
    assert _mock_requests.get.call_count == 2
    assert _mock_sleep.call_count == 1
    assert _out["AAPL"]["bid"] == 1.0
print("[retry_429] one retry after a 429, then the retried response is used")

# A SECOND consecutive failure gives up - never an unbounded retry loop.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests, \
     mock.patch.object(alpaca_quotes.time, "sleep"):
    _mock_requests.get.side_effect = [_FakeResp(503), _FakeResp(503)]
    _out = alpaca_quotes.latest_quotes(["AAPL"], log=lambda *a, **k: None)
    assert _mock_requests.get.call_count == 2
    assert _out == {}
print("[retry_exhausted] a second 5xx after the one retry gives up, returns nothing")

# latest_trade_price(): only positive numeric prices are returned.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests:
    _mock_requests.get.return_value = _FakeResp(200, {
        "trades": {"AAPL": {"p": 227.5}, "MSFT": {"p": -1}}
    })
    _out = alpaca_quotes.latest_trade_price(["AAPL", "MSFT"], log=lambda *a, **k: None)
    assert _out == {"AAPL": 227.5}, _out
print("[latest_trade_price] non-positive/missing trade price excluded")

# market_state(): paper clock URL when ALPACA_PAPER=1, live otherwise.
with mock.patch.dict(os.environ, dict(_KEYS, ALPACA_PAPER="1"), clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests:
    _mock_requests.get.return_value = _FakeResp(200, {"is_open": True})
    _state = alpaca_quotes.market_state(log=lambda *a, **k: None)
    assert _state == {"is_open": True}
    assert _mock_requests.get.call_args.args[0] == alpaca_quotes._CLOCK_URL_PAPER
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests:
    _mock_requests.get.return_value = _FakeResp(200, {"is_open": False})
    assert alpaca_quotes.market_state(log=lambda *a, **k: None) == {"is_open": False}
    assert _mock_requests.get.call_args.args[0] == alpaca_quotes._CLOCK_URL_LIVE
print("[market_state] paper clock URL under ALPACA_PAPER=1, live URL otherwise")

# A total failure never raises - returns None/empty, not an exception.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "requests") as _mock_requests:
    _mock_requests.get.side_effect = Exception("network down")
    assert alpaca_quotes.latest_quotes(["AAPL"], log=lambda *a, **k: None) == {}
    assert alpaca_quotes.market_state(log=lambda *a, **k: None) == {"is_open": None}
print("[fail_open] a network exception never raises, degrades to empty/None")


# ======================================================================
# quote_recorder.py wiring: _fetch_quotes_for_market()
# ======================================================================

# Missing keys -> available() False -> straight to the existing Yahoo
# path; Alpaca's own fetch functions are never called.
with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}, clear=False), \
     mock.patch.object(quote_recorder, "_fetch_quotes_with_bid_ask_fallback") as _mock_yahoo, \
     mock.patch.object(alpaca_quotes, "latest_quotes") as _mock_alpaca_quotes:
    _mock_yahoo.return_value = {"AAPL": {"bid": 1, "ask": 2}}
    _result = quote_recorder._fetch_quotes_for_market(["AAPL"], quote_recorder.MARKET_US, log=lambda *a, **k: None)
    assert _mock_yahoo.called
    assert not _mock_alpaca_quotes.called
    assert _result == {"AAPL": {"bid": 1, "ask": 2}}
print("[no_keys_fail_open] _record_market's US path takes Yahoo unchanged when Alpaca is unavailable")

# ASX always takes the Yahoo path, even with Alpaca keys set and
# available() true - Alpaca's own functions are never touched for ASX.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(quote_recorder, "_fetch_quotes_with_bid_ask_fallback") as _mock_yahoo, \
     mock.patch.object(alpaca_quotes, "latest_quotes") as _mock_alpaca_quotes, \
     mock.patch.object(alpaca_quotes, "market_state") as _mock_alpaca_clock:
    _mock_yahoo.return_value = {"BHP.AX": {"bid": 40.0, "ask": 40.1}}
    _result = quote_recorder._fetch_quotes_for_market(["BHP.AX"], quote_recorder.MARKET_ASX, log=lambda *a, **k: None)
    _mock_yahoo.assert_called_once_with(["BHP.AX"], quote_recorder.MARKET_ASX, log=mock.ANY)
    assert not _mock_alpaca_quotes.called
    assert not _mock_alpaca_clock.called
    assert _result == {"BHP.AX": {"bid": 40.0, "ask": 40.1}}
print("[asx_unchanged] ASX stays on the Yahoo path byte-identically, Alpaca never touched")

# Mixed response: Alpaca covers some tickers, Yahoo fallback runs only
# for the ones it didn't.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "available", return_value=True), \
     mock.patch.object(alpaca_quotes, "latest_quotes") as _mock_quotes, \
     mock.patch.object(alpaca_quotes, "latest_trade_price") as _mock_trades, \
     mock.patch.object(alpaca_quotes, "market_state") as _mock_clock, \
     mock.patch.object(quote_recorder, "_fetch_quotes_with_bid_ask_fallback") as _mock_yahoo:
    _mock_quotes.return_value = {"AAPL": {"bid": 227.0, "ask": 227.2, "bid_size": 1, "ask_size": 1}}
    _mock_trades.return_value = {"AAPL": 227.1}
    _mock_clock.return_value = {"is_open": True}
    _mock_yahoo.return_value = {"MSFT": {"bid": 420.0, "ask": 420.3, "_source": "yfinance.quote_bulk"}}
    _result = quote_recorder._fetch_quotes_for_market(
        ["AAPL", "MSFT"], quote_recorder.MARKET_US, log=lambda *a, **k: None,
    )
    _mock_yahoo.assert_called_once_with(["MSFT"], quote_recorder.MARKET_US, log=mock.ANY)
    assert _result["AAPL"]["_source"] == "alpaca-iex"
    assert _result["AAPL"]["bid"] == 227.0 and _result["AAPL"]["regularMarketPrice"] == 227.1
    assert _result["AAPL"]["marketState"] == "REGULAR"
    assert _result["MSFT"]["_source"] == "yfinance.quote_bulk"
print("[mixed_response] Alpaca-covered ticker tagged alpaca-iex, Yahoo fallback only for the rest")

# Alpaca quote present but no trade price -> falls back to Yahoo's
# single-ticker light endpoint for the price only, bid/ask stay Alpaca's.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "available", return_value=True), \
     mock.patch.object(alpaca_quotes, "latest_quotes") as _mock_quotes, \
     mock.patch.object(alpaca_quotes, "latest_trade_price") as _mock_trades, \
     mock.patch.object(alpaca_quotes, "market_state") as _mock_clock, \
     mock.patch.object(quote_recorder, "_fetch_quote_single") as _mock_single:
    _mock_quotes.return_value = {"AAPL": {"bid": 227.0, "ask": 227.2}}
    _mock_trades.return_value = {}
    _mock_clock.return_value = {"is_open": True}
    _mock_single.return_value = {"regularMarketPrice": 227.15}
    _result = quote_recorder._fetch_quotes_for_market(["AAPL"], quote_recorder.MARKET_US, log=lambda *a, **k: None)
    assert _mock_single.called
    assert _result["AAPL"]["bid"] == 227.0
    assert _result["AAPL"]["regularMarketPrice"] == 227.15
    assert _result["AAPL"]["_source"] == "alpaca-iex"
print("[price_fallback] missing Alpaca trade price falls back to Yahoo's last price alone")

# Market closed per Alpaca's clock -> marketState=CLOSED, so the
# existing _classify_market_state() gate rejects it exactly like a
# Yahoo holiday response, with no change to that function at all.
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(alpaca_quotes, "available", return_value=True), \
     mock.patch.object(alpaca_quotes, "latest_quotes") as _mock_quotes, \
     mock.patch.object(alpaca_quotes, "latest_trade_price") as _mock_trades, \
     mock.patch.object(alpaca_quotes, "market_state") as _mock_clock:
    _mock_quotes.return_value = {"AAPL": {"bid": 227.0, "ask": 227.2}}
    _mock_trades.return_value = {"AAPL": 227.1}
    _mock_clock.return_value = {"is_open": False}
    _result = quote_recorder._fetch_quotes_for_market(["AAPL"], quote_recorder.MARKET_US, log=lambda *a, **k: None)
    assert _result["AAPL"]["marketState"] == "CLOSED"
    _reason = quote_recorder._classify_market_state(_result["AAPL"]["marketState"], _result["AAPL"]["regularMarketVolume"])
    assert _reason == quote_recorder.REJECT_MARKET_NOT_REGULAR, _reason
print("[market_closed] Alpaca clock closed -> marketState=CLOSED -> existing gate rejects, unchanged")


# ======================================================================
# quote_recorder.py wiring: _record_market() end to end (US, mocked
# roster/store - never touches the real DB or makes a live request)
# ======================================================================

with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(quote_recorder, "today_roster_tickers", return_value={"AAPL"}), \
     mock.patch.object(alpaca_quotes, "available", return_value=True), \
     mock.patch.object(alpaca_quotes, "latest_quotes") as _mock_quotes, \
     mock.patch.object(alpaca_quotes, "latest_trade_price") as _mock_trades, \
     mock.patch.object(alpaca_quotes, "market_state") as _mock_clock, \
     mock.patch.object(quote_recorder.quote_snapshot_store, "latest_snapshot", return_value=None), \
     mock.patch.object(quote_recorder.quote_snapshot_store, "record_snapshot") as _mock_record, \
     mock.patch.object(quote_recorder.quote_snapshot_store, "record_rejection") as _mock_reject, \
     mock.patch.object(quote_recorder.quote_snapshot_store, "record_run_summary") as _mock_summary:
    _mock_quotes.return_value = {"AAPL": {"bid": 227.0, "ask": 227.2, "bid_size": 1, "ask_size": 1}}
    _mock_trades.return_value = {"AAPL": 227.1}
    _mock_clock.return_value = {"is_open": True}
    _captured, _counts = quote_recorder._record_market(quote_recorder.MARKET_US, log=lambda *a, **k: None)
    assert _captured == 1, _captured
    assert not _mock_reject.called
    _mock_record.assert_called_once()
    assert _mock_record.call_args.kwargs["source"] == "alpaca-iex"
    assert _mock_record.call_args.kwargs["ticker"] == "AAPL"
print("[record_market_us_alpaca] a captured US quote is stored with source='alpaca-iex'")

# Same _record_market(), but for ASX - the Yahoo path runs exactly as
# it did before this task, Alpaca's functions are never called, and
# the summary log line keeps its original (no per-source breakdown)
# shape.
_asx_log_lines = []
with mock.patch.dict(os.environ, _KEYS, clear=False), \
     mock.patch.object(quote_recorder, "today_roster_tickers", return_value={"BHP.AX"}), \
     mock.patch.object(quote_recorder, "_fetch_quotes_with_bid_ask_fallback") as _mock_yahoo, \
     mock.patch.object(alpaca_quotes, "latest_quotes") as _mock_alpaca_quotes, \
     mock.patch.object(quote_recorder.quote_snapshot_store, "latest_snapshot", return_value=None), \
     mock.patch.object(quote_recorder.quote_snapshot_store, "record_snapshot") as _mock_record, \
     mock.patch.object(quote_recorder.quote_snapshot_store, "record_run_summary"):
    _mock_yahoo.return_value = {"BHP.AX": {"bid": 40.0, "ask": 40.1, "regularMarketPrice": 40.05,
                                            "marketState": "REGULAR", "regularMarketVolume": 1000,
                                            "bidSize": 1, "askSize": 1, "currency": "AUD"}}
    _captured, _ = quote_recorder._record_market(
        quote_recorder.MARKET_ASX, log=lambda line, *_a, **_k: _asx_log_lines.append(line),
    )
    assert _captured == 1, _captured
    assert not _mock_alpaca_quotes.called
    assert _mock_record.call_args.kwargs["source"] == "yfinance.quote_bulk"
    assert any("captured 1, rejected 0" in line and "alpaca" not in line for line in _asx_log_lines), _asx_log_lines
print("[record_market_asx_unchanged] ASX: Yahoo path, source stays yfinance.quote_bulk, summary line unchanged")

print("\nAll alpaca_quotes tests passed.")
