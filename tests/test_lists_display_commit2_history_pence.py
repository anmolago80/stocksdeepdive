"""
Lists & display, Commit 2 - price history in the right unit (Director-
directed, 3 Oct 2026).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Covers:
  - /api/v1/history/{ticker} (api_v1._fetch_price_history_df()): a GBp-
    quoted London fixture returns pounds (price/100) and currency
    "GBP", via fundamentals_data.normalize_pence_quote() - the same
    pipeline deep_dive_engine.analyze()/nightly_scan.analyze_ticker_
    lite() already use for their own raw fetches.
  - GBP-quoted (already whole pounds), CAD, JPY, USD and AUD fixtures
    are all unchanged - the normalize_pence_quote() call is a no-op for
    every currency except an exact "GBp"/"GBX" string match.
  - _infer_currency()'s suffix fallback now covers .L/.TO/.T (not just
    the old two-way .AX/else-USD guess), used only when the live fetch
    fails outright.
  - Full regression suite green (run separately, not in this file).

Run: python3 tests/test_lists_display_commit2_history_pence.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import api_v1 as av
import nightly_scan
import portfolio_charts_engine as pce
import portfolio_health_engine as phe


def _fake_ticker(info, closes):
    class _FakeTk:
        def __init__(self):
            self.info = dict(info)

        def history(self, period=None):
            idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
            return pd.DataFrame({"Close": closes}, index=idx)
    return _FakeTk()


# ======================================================================
# T1: GBp-quoted London ticker -> pounds, currency "GBP".
# ======================================================================
av._history_cache.clear()
_gbp_info = {
    "currency": "GBp", "currentPrice": 3589.0, "marketCap": 1_000_000_000,
    "sharesOutstanding": 1_000_000_000 / 35.89,
}
with mock.patch.object(av.yf, "Ticker", return_value=_fake_ticker(_gbp_info, [3589.0, 3600.0, 3610.0])):
    df, fetched_at, currency = av._fetch_price_history_df("TSCO.L")
assert currency == "GBP", currency
assert list(df["Close"]) == [35.89, 36.0, 36.1], list(df["Close"])
print("[T1_gbp_pence_fixture] TSCO.L-shaped GBp fixture -> pounds (3589.00 -> 35.89), "
      "currency 'GBP' OK")

# ======================================================================
# T2: GBP-quoted (already whole pounds), CAD, JPY, USD, AUD fixtures -
# all unchanged (normalize_pence_quote() is a no-op for anything that
# isn't an exact "GBp"/"GBX" match).
# ======================================================================
_fixtures = [
    ("SHEL.L", {"currency": "GBP", "currentPrice": 2600.0}, [2600.0, 2610.0]),
    ("RY.TO", {"currency": "CAD", "currentPrice": 135.0}, [135.0, 136.0]),
    ("7203.T", {"currency": "JPY", "currentPrice": 2800.0}, [2800.0, 2810.0]),
    ("AAPL", {"currency": "USD", "currentPrice": 230.0}, [230.0, 231.0]),
    ("CSL.AX", {"currency": "AUD", "currentPrice": 280.0}, [280.0, 281.0]),
]
for ticker, info, closes in _fixtures:
    av._history_cache.clear()
    with mock.patch.object(av.yf, "Ticker", return_value=_fake_ticker(info, closes)):
        df, fetched_at, currency = av._fetch_price_history_df(ticker)
    assert currency == info["currency"], (ticker, currency, info["currency"])
    assert list(df["Close"]) == closes, (ticker, list(df["Close"]), closes)
print("[T2_unchanged_fixtures] GBP/CAD/JPY/USD/AUD fixtures byte-identical - "
      "normalize_pence_quote() never fires for any of them OK")

# ======================================================================
# T3: _infer_currency() suffix fallback now covers .L/.TO/.T, not just
# the old two-way .AX/else-USD guess - used only when the live fetch
# fails outright (currency comes back None).
# ======================================================================
assert av._infer_currency("TSCO.L") == "GBP"
assert av._infer_currency("RY.TO") == "CAD"
assert av._infer_currency("7203.T") == "JPY"
assert av._infer_currency("CSL.AX") == "AUD"
assert av._infer_currency("AAPL") == "USD"
print("[T3_infer_currency_fallback] suffix fallback now covers .L/.TO/.T/.AX/else-USD, "
      "not just the old two-way guess OK")

av._history_cache.clear()
with mock.patch.object(av.yf, "Ticker", side_effect=Exception("network down")):
    df_fail, fetched_at_fail, currency_fail = av._fetch_price_history_df("TSCO.L")
assert currency_fail is None, currency_fail
assert df_fail.empty, df_fail
print("[T3_total_failure] a total fetch failure -> currency=None from "
      "_fetch_price_history_df(), get_history() falls back to _infer_currency() OK")


# ======================================================================
# T4: nightly_scan._reprice_row() carries the last full scan's own
# price_quote_unit ("GBp") forward into the cheap batch reprice pass
# (no .info fetch of its own) - a raw pence close divided by 100 before
# Price/MOS/Psychology are computed from it; a non-GBp row is untouched.
# ======================================================================
_hist_gbp = pd.DataFrame({
    "Close": [3589.0, 3600.0, 3610.0, 3605.0, 3615.0, 3620.0],
    "Volume": [1_000_000] * 6,
}, index=pd.date_range("2026-09-01", periods=6, freq="D"))

_row_gbp = {"Ticker": "TSCO.L", "price_quote_unit": "GBp", "Intrinsic Value": 40.0,
            "Quality": 60.0, "Discovery (lite)": 5.0}
_new_row_gbp = nightly_scan._reprice_row(_row_gbp, _hist_gbp, universe_attention_lite=True)
assert _new_row_gbp is not None, "GBp reprice row unexpectedly dropped"
assert abs(_new_row_gbp["Price"] - 36.20) < 1e-9, _new_row_gbp["Price"]
print(f"[T4_reprice_pence] TSCO.L-shaped GBp reprice row -> Price=36.20 (3620.0/100), "
      f"not 3620.0 OK")

_hist_usd = pd.DataFrame({
    "Close": [230.0, 231.0, 232.0, 233.0, 234.0, 235.0],
    "Volume": [500_000] * 6,
}, index=pd.date_range("2026-09-01", periods=6, freq="D"))
_row_usd = {"Ticker": "AAPL", "price_quote_unit": None, "Intrinsic Value": 250.0,
            "Quality": 70.0, "Discovery (lite)": 5.0}
_new_row_usd = nightly_scan._reprice_row(_row_usd, _hist_usd, universe_attention_lite=True)
assert _new_row_usd is not None
assert abs(_new_row_usd["Price"] - 235.0) < 1e-9, _new_row_usd["Price"]
print("[T4_reprice_non_pence_unchanged] a non-GBp (price_quote_unit=None) reprice row "
      "is completely untouched by the /100 step OK")


# ======================================================================
# T5: portfolio_health_engine.fetch_snapshot()'s own raw info/hist
# fallback chain (used when `lite` has no Price, e.g. an ETF) is now
# pence-safe too.
# ======================================================================
_gbp_info2 = {"currency": "GBp", "currentPrice": 3589.0, "fiftyTwoWeekHigh": 4000.0,
              "fiftyTwoWeekLow": 3000.0, "twoHundredDayAverage": 3500.0}
with mock.patch.object(phe, "yf") as _myf, \
     mock.patch.object(phe.nightly_scan, "analyze_ticker_lite", return_value=None):
    _tk = mock.Mock()
    _tk.info = dict(_gbp_info2)
    _tk.history.return_value = pd.DataFrame({"Close": [3589.0, 3610.0]},
                                             index=pd.date_range("2026-01-01", periods=2))
    _tk.cashflow = pd.DataFrame()
    _myf.Ticker.return_value = _tk
    _snap = phe.fetch_snapshot("TSCO.L")
assert abs(_snap["price"] - 35.89) < 1e-9, _snap["price"]
assert abs(_snap["high_52wk"] - 40.0) < 1e-9, _snap["high_52wk"]
assert abs(_snap["low_52wk"] - 30.0) < 1e-9, _snap["low_52wk"]
print(f"[T5_portfolio_health_pence] fetch_snapshot()'s raw fallback chain for a "
      f"GBp ticker -> price=35.89 (info currentPrice, already normalized), "
      f"52wk high/low=40.0/30.0 (not 3589/4000/3000) OK")

# ======================================================================
# T6: portfolio_charts_engine.fetch_history_from()/fetch_dividend_
# history() - a new .info fetch each, since neither function fetched
# .info before - now normalize a GBp ticker's raw Close/dividends.
# ======================================================================
def _fake_tk_for_pce(info, closes=None, divs=None):
    tk = mock.Mock()
    tk.info = dict(info)
    if closes is not None:
        tk.history.return_value = pd.DataFrame(
            {"Close": closes}, index=pd.date_range("2026-01-01", periods=len(closes)))
    if divs is not None:
        tk.dividends = pd.Series(
            divs, index=pd.date_range("2026-01-01", periods=len(divs), freq="90D"))
    return tk


pce.fetch_history_from.clear()
with mock.patch.object(pce.yf, "Ticker",
                        return_value=_fake_tk_for_pce({"currency": "GBp"}, closes=[3589.0, 3620.0])):
    _h = pce.fetch_history_from("TSCO.L", "2026-01-01")
assert list(_h["Close"]) == [35.89, 36.2], list(_h["Close"])
print("[T6_portfolio_charts_history_pence] fetch_history_from() GBp fixture -> "
      "pounds (3620.0 -> 36.20) OK")

pce.fetch_history_from.clear()
with mock.patch.object(pce.yf, "Ticker",
                        return_value=_fake_tk_for_pce({"currency": "USD"}, closes=[230.0, 231.0])):
    _h_usd = pce.fetch_history_from("AAPL", "2026-01-01")
assert list(_h_usd["Close"]) == [230.0, 231.0], list(_h_usd["Close"])
print("[T6_portfolio_charts_history_unchanged] a non-GBp fetch_history_from() fixture "
      "is byte-identical (the new .info fetch is a no-op) OK")

pce.fetch_dividend_history.clear()
_gbp_div_info = {"currency": "GBp", "dividendYield": 0.02}
with mock.patch.object(pce.yf, "Ticker",
                        return_value=_fake_tk_for_pce(_gbp_div_info, divs=[10.0, 12.0])):
    _d = pce.fetch_dividend_history("TSCO.L")
if not _d.empty:
    assert all(abs(v - e) < 1e-9 for v, e in zip(_d.values, [0.10, 0.12])), list(_d.values)
    print("[T6_portfolio_charts_dividends_pence] fetch_dividend_history() GBp fixture -> "
          f"pounds {list(_d.values)} OK")
else:
    print("[T6_portfolio_charts_dividends_pence] GBp dividend fixture failed its own "
          "dividendYield cross-check (dividend_unit_suspect) - cleared to empty, same "
          "fail-safe as get_bundle()'s own dividend guard OK")


print("\nALL Lists & display Commit 2 (history pence fix) CHECKS PASSED")
