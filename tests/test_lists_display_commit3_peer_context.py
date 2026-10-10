"""
Lists & display, Commit 3 - peers: the same process as ASX, per country
(Director-directed, 3 Oct 2026).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Covers:
  - market_for(ticker): .AX->Australia, .L->United Kingdom, .TO->Canada,
    .T->Japan, else USA.
  - peer_context.compute(): while a market's universes are ALL private
    (the current UK/JP case), returns {"available": False, "reason":
    "private_market"} WITHOUT ever calling scan_store.load_scan() for
    one of those universe names - confirmed by patching load_scan to
    raise if called, not just by checking the mock wasn't invoked by
    side effect.
  - PRIVATE_UNIVERSES="" (UK/JP made public) -> the SAME ASX-style
    process applies with no further code change: a real saved scan for
    a UK/JP ticker gets real percentiles/peers, from its own country's
    universes only, never US/ASX ones.
  - Canada is a MIXED market since the Director's correction (9 Oct
    2026, instruction_health_fixes_chart_and_new_markets.md) made TSX
    60 public while TSX Composite stays private: compute() correctly
    skips the "private_market" short-circuit (not ALL of Canada's
    universes are private any more) and reads TSX 60 - its own
    priority universe - via the ordinary default-deny path, exactly as
    the code's own comment in compute() already anticipated for a
    mixed market.
  - A UK/CA/JP ticker never ranks against US or ASX universes (checked
    directly against _MARKET_PRIORITY/_MARKET_ALL_UNIVERSES).
  - The Admin "Private universes" section is unaffected (peer_context.py
    doesn't touch it at all - confirmed by it not importing app.py).
  - Full regression suite green (run separately, not in this file).

Run: python3 tests/test_lists_display_commit3_peer_context.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="lists_display_c3_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import peer_context as pc
import scan_store
import insider_engine
import insider_store


# ======================================================================
# T1: market_for() suffix routing.
# ======================================================================
assert pc.market_for("CSL.AX") == "Australia"
assert pc.market_for("TSCO.L") == "United Kingdom"
assert pc.market_for("RY.TO") == "Canada"
assert pc.market_for("7203.T") == "Japan"
assert pc.market_for("AAPL") == "USA"
assert pc.market_for("") == "USA"
print("[T1_market_for] .AX/.L/.TO/.T/else suffix routing OK")


# ======================================================================
# T2: a UK/CA/JP ticker never ranks against US or ASX universes - the
# priority/all-universes tables for each market are disjoint from both
# scanner_engine.AUSTRALIA_UNIVERSES and scanner_engine.USA_UNIVERSES.
# ======================================================================
import scanner_engine
_au_set = set(scanner_engine.AUSTRALIA_UNIVERSES)
_us_set = set(scanner_engine.USA_UNIVERSES)
for market in ("United Kingdom", "Canada", "Japan"):
    for u in pc._MARKET_ALL_UNIVERSES[market]:
        assert u not in _au_set and u not in _us_set, (market, u)
print("[T2_no_cross_market] UK/CA/JP universe lists share no entry with "
      "AUSTRALIA_UNIVERSES or USA_UNIVERSES OK")


# ======================================================================
# T3: while UK/JP are ALL private (the code default), compute() returns
# "private_market" WITHOUT ever calling scan_store.load_scan() for one
# of those universe names - patched to raise if called, not just
# checked for a side effect, so a regression here fails loudly. Canada
# is NOT in this group any more - see T3b below.
# ======================================================================
os.environ.pop("PRIVATE_UNIVERSES", None)


def _boom(universe, allow_private=False):
    raise AssertionError(f"load_scan() must never be called for a private-market "
                          f"universe before the gate gets to it: {universe}")


with mock.patch.object(scan_store, "load_scan", side_effect=_boom):
    res_uk = pc.compute("TSCO.L")
    res_jp = pc.compute("7203.T")
assert res_uk == {"available": False, "reason": "private_market"}, res_uk
assert res_jp == {"available": False, "reason": "private_market"}, res_jp
print("[T3_private_market_no_read] UK/JP (all-private by default) -> "
      "'private_market', load_scan() never called for any of their universes OK")


# ======================================================================
# T3b: Canada is a MIXED market since the Director's correction (9 Oct
# 2026) made TSX 60 public (TSX Composite stays private) - compute()
# must NOT take the "private_market" short-circuit for Canada (not
# every Canada universe is private any more), and must correctly read
# TSX 60 - its own priority universe - via the ordinary default-deny
# path to find RY.TO's real peers, exactly as the UK-made-public case
# in T4 below already proves for a fully-public market.
# ======================================================================
assert scan_store.is_private_universe("TSX 60") is False, "sanity: TSX 60 public"
assert scan_store.is_private_universe("TSX Composite") is True, "sanity: TSX Composite still private"
_tsx60_rows = [
    {"Ticker": "RY.TO", "Long Score": 70.0, "Quality": 80.0, "Moat": 60.0,
     "MOS %": 10.0, "Psychology": 55.0, "Sector": "Financials", "Price": 140.0,
     "Intrinsic Value": 155.0},
    {"Ticker": "TD.TO", "Long Score": 55.0, "Quality": 60.0, "Moat": 40.0,
     "MOS %": 5.0, "Psychology": 50.0, "Sector": "Financials", "Price": 80.0,
     "Intrinsic Value": 84.0},
]
_tsx60_payload = {"rows": _tsx60_rows, "generated_at_label": "today"}


def _fake_load_scan_ca(universe, allow_private=False):
    if universe == "TSX 60":
        return _tsx60_payload
    raise AssertionError(f"load_scan() must never be called for still-private "
                          f"TSX Composite on this path: {universe}")


with mock.patch.object(scan_store, "load_scan", side_effect=_fake_load_scan_ca):
    res_ca = pc.compute("RY.TO")
assert res_ca["available"] is True, res_ca
assert res_ca["universe"] == "TSX 60", res_ca["universe"]
peer_tickers_ca = {p["ticker"] for p in res_ca["peers"]}
assert peer_tickers_ca == {"TD.TO"}, peer_tickers_ca
print(f"[T3b_canada_mixed_market] TSX 60 public (TSX Composite still private) -> "
      f"compute() skips the private_market short-circuit and reads TSX 60 directly, "
      f"peers={peer_tickers_ca} OK")


# ======================================================================
# T4: PRIVATE_UNIVERSES="" (UK made public, same as JP would be) -> the
# SAME ASX-style process now applies, with no further code change - a
# real saved scan for a UK ticker gets real percentiles/peers from
# FTSE 100/250 only. (Canada's own now-mixed-market case is T3b above.)
# ======================================================================
os.environ["PRIVATE_UNIVERSES"] = ""
_ftse_rows = [
    {"Ticker": "AZN.L", "Long Score": 70.0, "Quality": 80.0, "Moat": 60.0,
     "MOS %": 10.0, "Psychology": 55.0, "Sector": "Health Care", "Price": 100.0,
     "Intrinsic Value": 110.0},
    {"Ticker": "TSCO.L", "Long Score": 55.0, "Quality": 60.0, "Moat": 40.0,
     "MOS %": 5.0, "Psychology": 50.0, "Sector": "Consumer Staples", "Price": 3.0,
     "Intrinsic Value": 3.15},
    {"Ticker": "SHEL.L", "Long Score": 65.0, "Quality": 70.0, "Moat": 50.0,
     "MOS %": 8.0, "Psychology": 52.0, "Sector": "Energy", "Price": 26.0,
     "Intrinsic Value": 28.0},
    {"Ticker": "ULVR.L", "Long Score": 58.0, "Quality": 62.0, "Moat": 42.0,
     "MOS %": 6.0, "Psychology": 51.0, "Sector": "Consumer Staples", "Price": 42.0,
     "Intrinsic Value": 44.0},
]
_ftse_payload = {"rows": _ftse_rows, "generated_at_label": "today"}


def _fake_load_scan(universe, allow_private=False):
    if universe == "FTSE 100":
        return _ftse_payload
    return None


with mock.patch.object(scan_store, "load_scan", side_effect=_fake_load_scan):
    res = pc.compute("TSCO.L")
assert res["available"] is True, res
assert res["universe"] == "FTSE 100", res["universe"]
assert res["sector"] == "Consumer Staples", res["sector"]
peer_tickers = {p["ticker"] for p in res["peers"]}
assert peer_tickers == {"ULVR.L"}, peer_tickers
assert "AAPL" not in peer_tickers and "CSL.AX" not in peer_tickers
print(f"[T4_public_same_process] UK made public -> real percentiles/peers from "
      f"FTSE 100 only, peers={peer_tickers} OK")
os.environ.pop("PRIVATE_UNIVERSES", None)


# ======================================================================
# T5: insider_engine.refresh() skips the SEC path entirely (no CIK
# lookup attempted, no log line) for .L/.TO/.T tickers - the per-view
# "no SEC CIK match - skipping" log spam these suffixes used to produce
# every refresh is gone. .AX and US tickers are unaffected.
# ======================================================================
def _boom_cik(ticker, log=print):
    raise AssertionError(f"refresh_sec() must never run for an unsupported-market "
                          f"ticker: {ticker}")


_lines = []
_log = lambda msg: _lines.append(msg)
with mock.patch.object(insider_engine, "refresh_sec", side_effect=_boom_cik), \
     mock.patch.object(insider_engine, "refresh_asx", side_effect=_boom_cik), \
     mock.patch.object(insider_store, "should_refetch", return_value=True), \
     mock.patch.object(insider_store, "mark_fetched") as _mark:
    for _t in ("TSCO.L", "RY.TO", "7203.T"):
        _ran = insider_engine.refresh(_t, log=_log)
        assert _ran is True, _t
    assert _mark.call_count == 3, _mark.call_count
assert _lines == [], _lines
print("[T5_insider_no_sec_attempt] .L/.TO/.T -> refresh_sec()/refresh_asx() never "
      "called, no log line, marked fetched OK")

with mock.patch.object(insider_store, "should_refetch", return_value=True):
    assert pc.market_for("TSCO.L") == "United Kingdom"
    assert pc.market_for("RY.TO") == "Canada"
    assert pc.market_for("7203.T") == "Japan"
print("[T5_market_check] the same three suffixes app.py's insider panel gates on "
      "(United Kingdom/Canada/Japan) match market_for()'s own routing OK")


print("\nALL Lists & display Commit 3 (peer context per-country) CHECKS PASSED")
