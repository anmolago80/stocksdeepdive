"""
TASK 29b (Director-directed, 3 Nov 2026).

PART A - BCG.L still broken after TASK 29's own deploy.
EVIDENCE (live, after TASK 29 deployed 05:46 UTC 3 Nov): PE by year
still 3,431x/3,516x/3,616x, PE Trailing GBP 167.95, Retained Earnings
-2,700. Railway logs show NO "[units] BCG.L" line at all (TSCO.L, a
pence-quoted ticker, does log). Deep-dive "Last 6 months" chart: ~200
until late Aug 2026, then ~2.3, axis labelled "EUR".
ROOT CAUSE (verified in code): every TASK 29 repair call sat inside
`if price_quote_unit == "GBp"`. BCG.L's own info["currency"] is a
plain "GBP" (never GBp/GBX) - it was NEVER pence-flagged in the first
place - so TASK 29's own repair never ran for it at all. Separately,
Yahoo's own info["currency"] for BCG.L is "EUR" (its financial
REPORTING currency, not its real LSE LISTING currency), which is why
the chart axis showed "EUR".
FIX: every ".L" ticker now gets the SAME per-point/per-row repair
regardless of price_quote_unit (fallback_divide_all=False - never
blanket-divide a series that was never pence-CONFIRMED), at all three
TASK 29 sites PLUS the Deep Dive "Last 6 months" chart's own
independent re-fetch (app.py) TASK 29 missed entirely. fcf_valuation_
engine.trading_currency_for() now returns "GBP" for any ".L" ticker
BEFORE consulting info["currency"] at all, fixing the currency label
wherever it's read via that shared helper; fundamentals_data.
normalize_pence_quote() additionally forces info["currency"]="GBP" for
a ".L" ticker in its own (deep_dive_engine.py-reached) pipeeline.
BUNDLE_VERSION bumped 7->8.

PART B - record real bid/ask for the Top 200.
FINDING (verified, quote_recorder.py): MARKET_UK/TSX/JP ARE actually
recording real rows daily in production (confirmed via Railway's own
live deploy logs, this task's own B1 investigation) - the bug was
roster_tickers(None) (the Trading Cost tab's own per-ticker honesty
check, via is_in_roster()) unioning only MARKET_ASX + MARKET_US, so a
genuinely-recorded UK/TSX/JP ticker like HSBA.L was wrongly told
"isn't in the daily bid/ask recording set". Separately, no European
market existed in quote_recorder.py at all, and Top 200 pool members
(incl. "Foreign US-listed" names like BBVA) were never added to any
market's roster regardless of suffix.
FIX: roster_tickers()/tickers_for_market() widened to every market
(_ALL_MARKETS, now including four new ones - MARKET_EURONEXT/XETRA/
SIX/STOCKHOLM); top200_roster_tickers() (top100_store.current_pool()
+ current_asx_extension()) is bucketed into each market by suffix
(_market_for_ticker()) and unioned into that market's own roster AND
its own daily tier (daily_tier_tickers()).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. The live B1 numbers (per-market captured/rejected counts
over the last 7 days, confirming UK/TSX/JP already record real rows)
came from Railway's own deploy logs via the Railway MCP tool, not from
a fixture - see this task's own report.

Run: python3 tests/test_director_task29b_london_pence_v2_and_top200_roster.py
"""
import contextlib
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task29b_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import pandas as pd

import auto_compounder_engine as ace
import deep_dive_engine
import fcf_valuation_engine as fcf
import fundamentals_data as fd
import nightly_scan
import quote_recorder as qr
import top100_store

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


def _mk_cashflow_df(ocf, capex):
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


# ======================================================================
# Shared BCG.L-shaped mixed-unit fixture: 30 months in pence (~230),
# 6 months already in pounds (~2.30) - the real incident shape, but
# with info["currency"] == "GBP" (NEVER "GBp"/"GBX") this time, and
# Yahoo's OWN info["currency"] separately set to "EUR" where a test
# needs to prove the currency-LABEL bug specifically.
# ======================================================================
_BCG_REFERENCE = 2.30
_BCG_PENCE_MONTHS = [230.0, 228.5, 231.0, 229.0, 232.0, 230.5, 227.0, 233.0,
                     229.5, 231.5, 230.0, 228.0, 232.5, 229.0, 230.5, 231.0,
                     228.5, 230.0, 233.0, 229.0, 231.5, 230.0, 228.0, 232.0,
                     229.5, 230.5, 231.0, 229.0, 230.0, 228.5]
_BCG_POUND_MONTHS = [2.29, 2.31, 2.28, 2.32, 2.30, 2.30]
_BCG_MIXED_SERIES = _BCG_PENCE_MONTHS + _BCG_POUND_MONTHS  # 36 points


# ======================================================================
# PART A, T1 (item A1, end-to-end): fundamentals_data.get_bundle() for
# a BCG.L-shaped ticker whose info["currency"] is a plain "GBP" (never
# GBp/GBX - it was NEVER pence-flagged) - prices_10y still repairs flat
# in GBP, and the "[units] BCG.L history repaired..." line is printed,
# proving this no longer depends on price_quote_unit == "GBp" at all.
# ======================================================================
def _fake_ticker(info, income=None, balance=None, cashflow=None,
                  dividends=None, history_fn=None):
    income = income if income is not None else pd.DataFrame()
    balance = balance if balance is not None else pd.DataFrame()
    cashflow = cashflow if cashflow is not None else pd.DataFrame()
    dividends = dividends if dividends is not None else pd.Series(dtype=float)

    class _FT:
        def __init__(self, *_a, **_k):
            self.info = dict(info)
            self.income_stmt = income
            self.balance_sheet = balance
            self.cashflow = cashflow
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = dividends

        @property
        def fast_info(self):
            raise AttributeError

        def history(self, *a, **k):
            return history_fn() if history_fn else pd.DataFrame()

    return _FT


_idx = pd.date_range("2023-09-01", periods=36, freq="MS")
_bcg_info_t1 = {
    "currency": "GBP", "currentPrice": 2.30, "regularMarketPrice": 2.30,
    "marketCap": 2.30 * 1_000_000, "sharesOutstanding": 1_000_000,
    "trailingEps": 0.12,
}
_bcg_hist_t1 = pd.DataFrame({"Close": _BCG_MIXED_SERIES}, index=_idx)

_stdout1 = io.StringIO()
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_bcg_info_t1, history_fn=lambda: _bcg_hist_t1)
    with contextlib.redirect_stdout(_stdout1):
        _bundle1 = fd.get_bundle("BCG.L", force_refresh=True)
_printed1 = _stdout1.getvalue()

check("FIX: get_bundle() repairs prices_10y flat in GBP for a .L ticker "
      "that was NEVER pence-flagged (info currency plain 'GBP', "
      "price_quote_unit None)",
      _bundle1["meta"]["price_quote_unit"] is None
      and (max(_bundle1["prices_10y"]["prices"]) / min(_bundle1["prices_10y"]["prices"])) < 1.3)
check("FIX: every point now sits near the real current price (GBP "
      "2.30), not the old ~100-360 broken range",
      all(1.8 <= p <= 2.8 for p in _bundle1["prices_10y"]["prices"]))
check('FIX: the "[units] BCG.L history repaired..." line is printed '
      "even though price_quote_unit is None",
      "[units] BCG.L history repaired: 30 of 36 points rescaled "
      "pence->GBP" in _printed1)
print("[task29b_a_t1_get_bundle_never_pence_flagged] get_bundle() repairs "
      "a never-pence-flagged .L ticker's mixed history OK")

# Downstream PE sanity, same pattern as TASK 29's own test.
_eps_income = pd.DataFrame(
    {"2026-06-30": [0.12], "2025-06-30": [0.11]}, index=["Basic EPS"]
)
_bundle1_ace = {
    "info": {"currentPrice": 2.30, "trailingEps": 0.12},
    "income": _eps_income, "income_q": pd.DataFrame(),
    "prices_10y": _bundle1["prices_10y"], "meta": {},
}
_avg_pe1 = ace._avg_pe_3pt(_bundle1_ace)
check("FIX: PE Ratio Average computed from the repaired series is sane "
      "(tens, not the broken 3,431x/.../1,807x-scale garbage)",
      _avg_pe1 is not None and 5.0 < _avg_pe1 < 50.0)
print(f"[task29b_a_t1b_pe_sanity] avg P/E={_avg_pe1:.2f}x (sane) OK")


# ======================================================================
# PART A, T2 (item A1/A3, end-to-end): fundamentals_data.normalize_
# pence_quote() for a BCG.L-shaped ticker whose RAW info["currency"] is
# "EUR" (the real Yahoo quirk - financial reporting currency, not
# listing currency) - history_df still repairs flat in GBP, AND
# info["currency"] is forced to "GBP" (never left as "EUR") so the
# Deep Dive headline's own "currency" field - and every page that
# reads it - shows GBP, never EUR.
# ======================================================================
_bcg_mixed_df = pd.DataFrame({
    "Open": [p * 0.995 for p in _BCG_MIXED_SERIES],
    "High": [p * 1.01 for p in _BCG_MIXED_SERIES],
    "Low": [p * 0.99 for p in _BCG_MIXED_SERIES],
    "Close": _BCG_MIXED_SERIES,
}, index=_idx)

_info_t2 = {"currency": "EUR", "currentPrice": 2.30}
_log2 = []
_info_out2, _hist_out2, _div_out2, _meta2 = fd.normalize_pence_quote(
    "BCG.L", dict(_info_t2), history_df=_bcg_mixed_df.copy(), log=_log2.append
)
check("FIX: normalize_pence_quote() repairs a mixed-unit history_df "
      "flat in GBP even though info['currency'] is 'EUR' (never GBp/"
      "GBX - never pence-flagged)",
      (_hist_out2["Close"].max() / _hist_out2["Close"].min()) < 1.3)
check("FIX: normalize_pence_quote() forces info['currency'] to 'GBP' "
      "for a .L ticker - the LISTING currency, never left as Yahoo's "
      "own 'EUR' financial-REPORTING-currency value",
      _info_out2["currency"] == "GBP")
check('FIX: the "[units] BCG.L history repaired..." log line fires via '
      "normalize_pence_quote()'s own log= parameter too",
      any("[units] BCG.L history repaired" in l for l in _log2))
print("[task29b_a_t2_normalize_pence_quote_never_flagged] normalize_pence_"
      "quote() repairs history + forces listing currency to GBP for a "
      "never-pence-flagged .L ticker reporting in EUR OK")


# ======================================================================
# PART A, T3 (items A1-A3, full end-to-end): deep_dive_engine.analyze()
# on a BCG.L-shaped fixture - info["currency"]="EUR" (the real Yahoo
# quirk), 6-month OHLC history mixed pence/pounds. Price/MA50/currency
# must all come out correct - this is the exact Deep Dive headline
# path the live incident report described.
# ======================================================================
_bcg_6mo_dates = pd.date_range("2026-05-01", periods=130, freq="D")
_bcg_6mo_closes = [230.0] * 100 + [2.30] * 30  # pence era, then pounds era
_bcg_6mo_hist = pd.DataFrame({
    "Open": _bcg_6mo_closes, "High": [c * 1.01 for c in _bcg_6mo_closes],
    "Low": [c * 0.99 for c in _bcg_6mo_closes], "Close": _bcg_6mo_closes,
    "Volume": [500_000] * 130,
}, index=_bcg_6mo_dates)
_bcg_dd_info = {
    "currency": "EUR", "financialCurrency": "EUR", "currentPrice": 2.30,
    "marketCap": 2.30 * 10_000_000, "sharesOutstanding": 10_000_000,
    "longName": "BCG-shaped",
}
_bcg_dd_cf = _mk_cashflow_df([5_000_000.0] * 5, [-500_000.0] * 5)

_dd_log = io.StringIO()
with contextlib.redirect_stdout(_dd_log):
    _dd = deep_dive_engine.analyze(
        "BCG.L",
        get_price_history=lambda t: _bcg_6mo_hist,
        get_ticker_info=lambda t: dict(_bcg_dd_info),
        get_cashflow_df=lambda t: _bcg_dd_cf,
        live_data=False, enable_social=False,
    )
check("FIX: deep_dive_engine.analyze() on a BCG.L-shaped fixture "
      "returns no error", _dd.get("error") is None)
check("FIX: the headline price is sane GBP (~2.30), not a false '99% "
      "crash'-shaped figure derived from the raw pence-era closes",
      _dd is not None and 1.8 <= _dd.get("price", 0) <= 2.8)
check("FIX: MA50 is also computed from the REPAIRED series - sane "
      "GBP scale, not a mix of pence and pounds",
      1.8 <= _dd.get("ma50", 0) <= 2.8)
check("FIX: the headline currency is 'GBP' - the real LSE LISTING "
      "currency - NEVER Yahoo's own 'EUR' financial-reporting-currency "
      "value (the live incident's own 'axis labelled EUR' symptom)",
      _dd.get("currency") == "GBP")
print(f"[task29b_a_t3_deep_dive_e2e] deep_dive_engine.analyze('BCG.L'): "
      f"price={_dd.get('price')}, ma50={_dd.get('ma50')}, "
      f"currency={_dd.get('currency')!r} (never 'EUR') OK")


# ======================================================================
# PART A, T4 (item A3): fcf_valuation_engine.trading_currency_for() -
# a ".L" ticker ALWAYS returns "GBP", even when info["currency"] says
# something else entirely (the BCG.L "EUR" quirk); a non-".L" ticker's
# existing behaviour (info["currency"] wins) is completely unaffected -
# CSL.AX's own legitimate USD-financialCurrency/AUD-info["currency"]
# case must not be disturbed by this .L-specific fix.
# ======================================================================
check("FIX: trading_currency_for('BCG.L', info with currency='EUR') "
      "-> 'GBP'", fcf.trading_currency_for("BCG.L", {"currency": "EUR"}) == "GBP")
check("REGRESSION GUARD: trading_currency_for('SHEL.L', info with "
      "currency='GBP') -> 'GBP' (no-op, already correct)",
      fcf.trading_currency_for("SHEL.L", {"currency": "GBP"}) == "GBP")
check("REGRESSION GUARD: trading_currency_for('TSCO.L', no info at "
      "all) -> 'GBP' via the suffix fallback, unchanged",
      fcf.trading_currency_for("TSCO.L", None) == "GBP")
check("REGRESSION GUARD: trading_currency_for('CSL.AX', info with "
      "currency='AUD') -> 'AUD', completely unaffected by the .L-only "
      "fix (CSL.AX's own financialCurrency=USD/currency=AUD split "
      "must keep working exactly as before)",
      fcf.trading_currency_for("CSL.AX", {"currency": "AUD"}) == "AUD")
check("REGRESSION GUARD: trading_currency_for('AAPL', info with "
      "currency='USD') -> 'USD', unaffected",
      fcf.trading_currency_for("AAPL", {"currency": "USD"}) == "USD")
print("[task29b_a_t4_trading_currency_for] .L always GBP regardless of "
      "info['currency']; every other suffix's existing behaviour "
      "(including CSL.AX's own legitimate USD/AUD split) unchanged OK")


# ======================================================================
# PART A, T5 (item A2): the Deep Dive "Last 6 months" chart's own
# SEPARATE re-fetch (app.py's _px_hist) - repaired the exact same way
# app.py now does it: fundamentals_data.repair_mixed_unit_price_
# dataframe(_px_hist, _dd["price"], ticker=_dd["ticker"],
# fallback_divide_all=False). Proves the wiring app.py uses, not just
# the underlying function in isolation.
# ======================================================================
_px_hist_raw = _bcg_6mo_hist.copy()  # the SAME raw, unrepaired re-fetch
_px_hist_repaired = fd.repair_mixed_unit_price_dataframe(
    _px_hist_raw, _dd.get("price"), ticker="BCG.L", fallback_divide_all=False,
)
check("FIX: the chart's own independent re-fetch, repaired against "
      "_dd['price'] exactly as app.py now does, is flat in GBP - no "
      "false '99% crash' on the chart itself",
      (_px_hist_repaired["Close"].max() / _px_hist_repaired["Close"].min()) < 1.3)
print("[task29b_a_t5_chart_refetch_repair] the Deep Dive chart's own "
      "separate re-fetch repairs flat in GBP using the exact app.py call "
      "shape OK")


# ======================================================================
# PART A, T6 (regression guard, item 5 of TASK 29/29b): a GBp-FLAGGED
# .L ticker (already fixed by TASK 29) is completely unaffected by this
# widening - same outcome as before.
# ======================================================================
_shel_info_t6 = {
    "currency": "GBp", "currentPrice": 2600.0,
    "marketCap": 26.0 * 1_000_000, "sharesOutstanding": 1_000_000,
}
_shel_closes_t6 = [2590.0, 2605.0, 2615.0, 2600.0]
_shel_hist_t6 = pd.DataFrame({"Close": _shel_closes_t6},
                              index=pd.date_range("2026-08-01", periods=4, freq="MS"))
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_shel_info_t6, history_fn=lambda: _shel_hist_t6)
    _bundle_shel_t6 = fd.get_bundle("SHEL.L", force_refresh=True)
check("REGRESSION GUARD: a GBp-flagged, consistently-pence ticker "
      "still ends up fully divided by 100 - unchanged by this widening",
      [round(p, 2) for p in _bundle_shel_t6["prices_10y"]["prices"]]
      == [25.9, 26.05, 26.15, 26.0])
print("[task29b_a_t6_gbp_flagged_unchanged] a GBp-flagged .L ticker's "
      "outcome is byte-identical to before this widening OK")


# ======================================================================
# PART A, T7 (regression guard): a plain-GBP .L ticker whose history is
# ALREADY consistent (never switches units) is left completely
# untouched - the new .L-wide repair is a safe no-op here.
# ======================================================================
_glen_closes_t7 = [12.0, 12.2, 12.4, 12.5]
_glen_info_t7 = {"currency": "GBP", "currentPrice": 12.5,
                 "marketCap": 12.5 * 1_000_000, "sharesOutstanding": 1_000_000}
_glen_hist_t7 = pd.DataFrame({"Close": _glen_closes_t7},
                             index=pd.date_range("2026-07-01", periods=4, freq="MS"))
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_glen_info_t7, history_fn=lambda: _glen_hist_t7)
    _bundle_glen_t7 = fd.get_bundle("GLEN.L", force_refresh=True)
check("REGRESSION GUARD: a plain-GBP .L ticker whose history never "
      "switches units is left byte-identical",
      _bundle_glen_t7["prices_10y"]["prices"] == _glen_closes_t7)
print("[task29b_a_t7_already_consistent_gbp_unchanged] an already-"
      "consistent-GBP .L ticker's history is completely unaffected OK")


# ======================================================================
# PART A, T8 (regression guard): a non-.L ticker is completely
# unaffected by every change in this task.
# ======================================================================
_aapl_closes_t8 = [225.0, 228.0, 230.0]
_aapl_info_t8 = {"currency": "USD", "currentPrice": 230.0,
                 "marketCap": 230.0 * 1_000_000, "sharesOutstanding": 1_000_000}
_aapl_hist_t8 = pd.DataFrame({"Close": _aapl_closes_t8},
                             index=pd.date_range("2026-08-01", periods=3, freq="MS"))
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_aapl_info_t8, history_fn=lambda: _aapl_hist_t8)
    _bundle_aapl_t8 = fd.get_bundle("AAPL", force_refresh=True)
check("REGRESSION GUARD: a non-.L ticker's prices_10y is byte-"
      "identical", _bundle_aapl_t8["prices_10y"]["prices"] == _aapl_closes_t8)
print("[task29b_a_t8_non_london_unchanged] a non-.L ticker is completely "
      "unaffected by this task OK")


# ======================================================================
# PART A, T9 (item A2, end-to-end): nightly_scan.analyze_ticker_lite()
# on a BCG.L-shaped fixture (currency "GBP", never pence-flagged,
# mixed 6mo history) - this module's own standalone raw fetch, shares
# normalize_pence_quote() with deep_dive_engine.py.
# ======================================================================
class _BcgNightlyTicker:
    def __init__(self, *_a, **_k):
        self.info = {"currency": "GBP", "currentPrice": 2.30,
                     "marketCap": 2.30 * 5_000_000, "sharesOutstanding": 5_000_000}
        self.cashflow = _mk_cashflow_df([2_000_000.0] * 5, [-200_000.0] * 5)
        self.dividends = pd.Series(dtype=float)

    def history(self, *a, **k):
        return _bcg_6mo_hist


_nightly_log = io.StringIO()
with mock.patch.object(nightly_scan, "yf") as _myf, contextlib.redirect_stdout(_nightly_log):
    _myf.Ticker.side_effect = _BcgNightlyTicker
    _bcg_row = nightly_scan.analyze_ticker_lite("BCG.L")
check("FIX: analyze_ticker_lite() on a never-pence-flagged .L ticker "
      "with mixed-unit history still lands on a sane GBP price",
      _bcg_row is not None and 1.8 <= _bcg_row["Price"] <= 2.8)
print(f"[task29b_a_t9_analyze_ticker_lite] BCG.L-shaped scan row -> "
      f"Price={_bcg_row['Price']:.2f} (sane GBP) OK")


# ======================================================================
# PART A, T10 (item A2, end-to-end): nightly_scan._reprice_row() for a
# BCG.L-shaped row that was NEVER pence-flagged (price_quote_unit is
# None) - repaired against row["Price"] regardless.
# ======================================================================
_reprice_mixed = [230.0, 229.0, 231.0, 2.29, 2.31, 2.30]
_hist_df_t10 = pd.DataFrame(
    {"Close": _reprice_mixed, "Volume": [500_000] * 6},
    index=pd.date_range("2026-09-01", periods=6, freq="D"),
)
_row_t10 = {"Ticker": "BCG.L", "price_quote_unit": None, "Price": 2.30,
            "Intrinsic Value": 3.00, "Quality": 60.0, "Discovery (lite)": 5.0}
_log_t10 = []
_new_row_t10 = nightly_scan._reprice_row(_row_t10, _hist_df_t10, universe_attention_lite=True,
                                          log=_log_t10.append)
check("FIX: _reprice_row() repairs a mixed-unit window for a .L row "
      "that was never pence-flagged",
      _new_row_t10 is not None and 1.8 <= _new_row_t10["Price"] <= 2.8)
check('FIX: the "[units] BCG.L history repaired..." log line fires '
      "even with price_quote_unit=None, purely from the .L suffix",
      any("[units] BCG.L history repaired" in l for l in _log_t10))
print(f"[task29b_a_t10_reprice_row_never_flagged] _reprice_row() BCG.L "
      f"never-pence-flagged row -> Price={_new_row_t10['Price']:.2f} OK")


# ======================================================================
# PART A, T11: BUNDLE_VERSION bumped 7->8.
# ======================================================================
check("BUNDLE_VERSION bumped to 8", fd.BUNDLE_VERSION == 8)
print("[task29b_a_t11_bundle_version] BUNDLE_VERSION == 8 OK")


# ========================================================================
# PART B, T12 (item B2): suffix -> market mapping.
# ========================================================================
_suffix_cases = [
    ("BBVA", qr.MARKET_US), ("AAPL", qr.MARKET_US),
    ("CBA.AX", qr.MARKET_ASX), ("HSBA.L", qr.MARKET_UK),
    ("RY.TO", qr.MARKET_TSX), ("7203.T", qr.MARKET_JP),
    ("MC.PA", qr.MARKET_EURONEXT), ("ASML.AS", qr.MARKET_EURONEXT),
    ("SAP.DE", qr.MARKET_XETRA), ("NESN.SW", qr.MARKET_SIX),
    ("VOLV-B.ST", qr.MARKET_STOCKHOLM),
]
_suffix_ok = all(qr._market_for_ticker(t) == m for t, m in _suffix_cases)
check("FIX: suffix -> market mapping correct for every case (no suffix "
      "incl. Foreign US-listed -> US; .AX/.L/.TO/.T unchanged; .PA/.AS "
      "-> EURONEXT; .DE -> XETRA; .SW -> SIX; .ST -> STOCKHOLM)",
      _suffix_ok)
print(f"[task29b_b_t12_suffix_market_mapping] {len(_suffix_cases)} suffix "
      f"cases all map correctly OK")


# ========================================================================
# PART B, T13 (item B2): top200_roster_tickers() - current_pool() UNION
# current_asx_extension(), fail-open on any failure.
# ========================================================================
_mock_pool = [
    {"ticker": "BBVA"}, {"ticker": "MC.PA"}, {"ticker": "SAP.DE"},
    {"ticker": "NESN.SW"}, {"ticker": "VOLV-B.ST"}, {"ticker": "HSBA.L"},
]
_mock_ext = [{"ticker": "XRO.AX"}]
with mock.patch.object(top100_store, "current_pool", return_value=_mock_pool), \
     mock.patch.object(top100_store, "current_asx_extension", return_value=_mock_ext):
    _t200 = qr.top200_roster_tickers()
check("FIX: top200_roster_tickers() unions current_pool() with "
      "current_asx_extension() (the ASX extension rows current_pool() "
      "alone never carries)",
      _t200 == {"BBVA", "MC.PA", "SAP.DE", "NESN.SW", "VOLV-B.ST", "HSBA.L", "XRO.AX"})

with mock.patch.object(top100_store, "current_pool", side_effect=Exception("db down")):
    _t200_fail = qr.top200_roster_tickers()
check("REGRESSION GUARD: top200_roster_tickers() fails open to an "
      "empty set on any error (never raises, never blocks the rest of "
      "the roster)", _t200_fail == set())
print("[task29b_b_t13_top200_roster_tickers] union + ASX extension + "
      "fail-open to empty set OK")


# ========================================================================
# PART B, T14 (items B2/B3): roster_tickers()/_top200_tickers_for_
# market() correctly slice the Top 200 pool per market, including the
# four brand-new European markets.
# ========================================================================
with mock.patch.object(top100_store, "current_pool", return_value=_mock_pool), \
     mock.patch.object(top100_store, "current_asx_extension", return_value=_mock_ext):
    check("FIX: MARKET_EURONEXT's own Top 200 slice is exactly MC.PA",
          qr._top200_tickers_for_market(qr.MARKET_EURONEXT) == {"MC.PA"})
    check("FIX: MARKET_XETRA's own Top 200 slice is exactly SAP.DE",
          qr._top200_tickers_for_market(qr.MARKET_XETRA) == {"SAP.DE"})
    check("FIX: MARKET_SIX's own Top 200 slice is exactly NESN.SW",
          qr._top200_tickers_for_market(qr.MARKET_SIX) == {"NESN.SW"})
    check("FIX: MARKET_STOCKHOLM's own Top 200 slice is exactly VOLV-B.ST",
          qr._top200_tickers_for_market(qr.MARKET_STOCKHOLM) == {"VOLV-B.ST"})
    check("FIX: MARKET_US's own Top 200 slice includes BBVA (Foreign "
          "US-listed, no suffix of its own)",
          "BBVA" in qr._top200_tickers_for_market(qr.MARKET_US))
    check("FIX: a brand-new European market's roster_tickers() is its "
          "Top 200 slice alone (no scanned-universe anchor exists for "
          "it at all)",
          qr.roster_tickers(qr.MARKET_EURONEXT) == {"MC.PA"})
print("[task29b_b_t14_per_market_top200_slices] every market's own Top "
      "200 slice (incl. the four brand-new European ones) is correct OK")


# ========================================================================
# PART B, T15 (item B3, THE director's own exact complaint): roster_
# tickers(None)/is_in_roster() now cover EVERY market, not just ASX+US -
# a genuinely-recorded UK ticker like HSBA.L is no longer wrongly
# reported as outside the recording set.
# ========================================================================
with mock.patch.object(top100_store, "current_pool", return_value=[]), \
     mock.patch.object(top100_store, "current_asx_extension", return_value=[]), \
     mock.patch.object(qr, "tickers_for_market",
                        side_effect=lambda m: ["HSBA.L", "TSCO.L"] if m == qr.MARKET_UK else []):
    check("FIX: roster_tickers(None) now includes a UK-only ticker "
          "(HSBA.L) - the Director's own exact complaint",
          "HSBA.L" in qr.roster_tickers(None))
    check("FIX: is_in_roster('HSBA.L') is True (was False before this "
          "task - the Trading Cost tab's own wrong 'not in roster' "
          "message)", qr.is_in_roster("HSBA.L"))
print("[task29b_b_t15_roster_tickers_none_all_markets] roster_tickers"
      "(None)/is_in_roster() now cover UK (and, by the same mechanism, "
      "TSX/JP/the four new European markets) - HSBA.L no longer wrongly "
      "flagged as unrecorded OK")


# ========================================================================
# PART B, T16 (item B4): Top 200 members land in the DAILY tier, even
# for a brand-new market with no scanned-universe roster at all.
# ========================================================================
with mock.patch.object(top100_store, "current_pool", return_value=_mock_pool), \
     mock.patch.object(top100_store, "current_asx_extension", return_value=_mock_ext), \
     mock.patch.object(qr.quote_snapshot_store, "latest_snapshot", return_value=None):
    _daily_euronext = qr.daily_tier_tickers(qr.MARKET_EURONEXT)
    check("FIX: MC.PA (a Top 200 member, MARKET_EURONEXT) is in the "
          "DAILY tier, not left to a 1/5th weekly rotation",
          "MC.PA" in _daily_euronext)
    _weekly_euronext = qr.weekly_tier_tickers(qr.MARKET_EURONEXT)
    check("FIX: MC.PA is NOT in the weekly tier (every roster ticker "
          "not in the daily tier) - it's fully covered by the daily "
          "tier instead", "MC.PA" not in _weekly_euronext)
print("[task29b_b_t16_top200_daily_tier] Top 200 pool members land in "
      "the daily tier (every trading day), never the weekly rotation OK")


# ========================================================================
# PART B, T17 (item B2): the four new European markets' TZ/window/
# currency-fallback config is complete, and is_due_now() uses it
# correctly (09:00-17:30 Europe/Paris for MARKET_EURONEXT).
# ========================================================================
_new_markets = (qr.MARKET_EURONEXT, qr.MARKET_XETRA, qr.MARKET_SIX, qr.MARKET_STOCKHOLM)
check("FIX: every new European market has a _MARKET_TZ/_MARKET_WINDOW/"
      "_MARKET_CURRENCY_FALLBACK entry",
      all(m in qr._MARKET_TZ and m in qr._MARKET_WINDOW and m in qr._MARKET_CURRENCY_FALLBACK
          for m in _new_markets))
check("FIX: is_due_now(MARKET_EURONEXT) is True at 10:00 Europe/Paris "
      "(09:00 UTC in winter CET) on a weekday",
      qr.is_due_now(qr.MARKET_EURONEXT, now=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)))
check("FIX: is_due_now(MARKET_EURONEXT) is False before the 09:00 "
      "local open",
      not qr.is_due_now(qr.MARKET_EURONEXT, now=datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)))
check("FIX: is_due_now(MARKET_EURONEXT) is False after the 17:30 "
      "local safety-bound close",
      not qr.is_due_now(qr.MARKET_EURONEXT, now=datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)))
check("FIX: is_due_now(MARKET_EURONEXT) is False on a Saturday",
      not qr.is_due_now(qr.MARKET_EURONEXT, now=datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)))
check("item B5: none of the four new European windows (07:00-16:30 "
      "UTC worst case, winter CET) can ever overlap the ~20:00-21:30 "
      "UTC nightly scan",
      all(qr._MARKET_WINDOW[m][2] <= 17 for m in _new_markets))
print("[task29b_b_t17_european_window_config] four new markets' TZ/"
      "window config complete and correct; no overlap with the nightly "
      "scan's own ~20:00-21:30 UTC window OK")


# ========================================================================
# PART B, T18 (item B2, regression guard): tickers_for_market() for a
# market with no _MARKET_UNIVERSE entry returns [] without raising -
# never a KeyError, unlike before this fix.
# ========================================================================
check("REGRESSION GUARD: tickers_for_market(MARKET_EURONEXT) returns "
      "[] (no _MARKET_UNIVERSE entry), never raises",
      qr.tickers_for_market(qr.MARKET_EURONEXT) == [])
print("[task29b_b_t18_tickers_for_market_no_universe] a market with no "
      "scanned-universe anchor returns [] cleanly, never KeyErrors OK")


# ========================================================================
# PART B, T19 (regression guard): with an EMPTY Top 200 pool (no
# selection has ever run), every existing market's roster is BYTE-
# IDENTICAL to tickers_for_market() alone - this task's own addition is
# a pure no-op until a Top 200 selection actually exists.
# ========================================================================
with mock.patch.object(top100_store, "current_pool", return_value=[]), \
     mock.patch.object(top100_store, "current_asx_extension", return_value=[]), \
     mock.patch.object(qr, "tickers_for_market",
                        side_effect=lambda m: ["AAPL", "MSFT"] if m == qr.MARKET_US else []):
    check("REGRESSION GUARD: roster_tickers(MARKET_US) with an empty "
          "Top 200 pool is byte-identical to tickers_for_market(US) "
          "alone", qr.roster_tickers(qr.MARKET_US) == {"AAPL", "MSFT"})
print("[task29b_b_t19_empty_top200_pool_noop] an empty Top 200 pool "
      "(no selection has run yet) makes this task's own roster addition "
      "a complete no-op OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
