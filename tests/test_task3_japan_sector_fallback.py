"""
TASK 3 of instruction_scanner_chips_trusts_japan_sectors.md (9 Oct
2026, Director-directed): Sector is blank on all TOPIX 500/Nikkei 225
rows.

CAUSE (found, with the exact file/line): neither universe's own
constituent source carries sector text at all - a deliberate choice,
not a bug, already documented in each fetcher's own docstring:
  - scanner_engine.fetch_topix500() (~line 2767): JPX's own
    topixweight_j.csv has columns code/name/size-classification
    (Core30/Large70/Mid400) only - no sector column - so it sets
    `out["Sector"] = None` explicitly.
  - scanner_engine.fetch_nikkei225() (~line 2662): Wikipedia's Nikkei
    225 page is a bulleted name list ("Honda Motor Co., Ltd. (TYO:
    7267)") with no sector text either - `Sector: [None] * len(tickers)`.
nightly_scan.run_universe_scan()'s own per-ticker loop then set
row["Sector"] = _sector_by_ticker.get(t) (the pool's own column) with
no fallback at all, so every TOPIX/Nikkei row landed with Sector=None.

FIX: Yahoo's own sector field for the ticker - already fetched into
`info` inside analyze_ticker_lite() for every other purpose in that
row (quality, intrinsic value, stock-type classification), so this is
zero extra network calls. analyze_ticker_lite() now returns a scratch
key, "_fallback_sector" = (info.get("sector") or "").strip() or None.
run_universe_scan()'s own per-ticker loop pops it unconditionally (so
it never reaches scan_store/the saved JSON either way) and only uses
it when the constituent POOL itself has no sector for that ticker -
never overriding a real pool-sourced one (FTSE/ASX/US/TSX are
unaffected, since their own pool already carries a real "Sector").
This is universe-agnostic by construction (keyed on "does the pool
have one", not on "is this Japan"), so it covers Nikkei 225 the exact
same way as TOPIX 500, and automatically covers any future universe
whose own source also lacks sector text.

UNVERIFIED FROM THIS SANDBOX (no outbound network access here to
confirm Yahoo actually returns a sector for a .T ticker) - same
disclosure as every other live-data assumption this session. If it
comes back empty in production too, nothing regresses: Sector stays
None exactly as it already does today - never invented, never a
hand-made mapping.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_task3_japan_sector_fallback.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task3_japan_sector_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

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


import pandas as pd
import nightly_scan as ns
import scan_store
import scanner_engine
import fundamentals_data

# ======================================================================
# CHECK 1-2: analyze_ticker_lite()'s own "_fallback_sector" key, via
# the exact mocking harness tests/test_financials_income_store_
# commit2.py's own C4 check already proved works for this function -
# Yahoo's info carries "sector": "Consumer Cyclical" (a real field this
# row already fetches for every other purpose, no extra call), and
# this fix threads it through as a new scratch key.
# ======================================================================
_dates = pd.date_range("2026-01-01", periods=90, freq="D")
_hist_df = pd.DataFrame({"Close": [2800.0 + i for i in range(90)],
                          "Volume": [500_000] * 90}, index=_dates)
TOYOTA_CF = pd.DataFrame(
    {"2026-03-31": [4_000_000.0, -1_500_000.0], "2025-03-31": [3_800_000.0, -1_400_000.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)


def _ytw_side_effect(fn, log, ticker, label, **kw):
    if label == "history":
        return _hist_df
    if label == "info":
        return {"currency": "JPY", "sector": "Consumer Cyclical", "marketCap": 30_000_000_000_000}
    if label == "cashflow":
        return TOYOTA_CF
    return None


with mock.patch.object(ns, "_yf_call_with_retry") as _ytw, \
     mock.patch.object(ns, "resolve_quality_score", return_value=(70, "auto", False)), \
     mock.patch.object(ns, "resolve_intrinsic_value") as _riv, \
     mock.patch.object(ns, "resolve_stock_type", return_value=("COMPOUNDER", "auto", False)), \
     mock.patch.object(ns.reverse_dcf_engine, "compute", return_value={"ok": False}), \
     mock.patch.object(ns.trade_filter_engine, "calc_support_resistance",
                        return_value={"support20": 2700, "resistance20": 2900,
                                      "support60": 2600, "resistance60": 3000}), \
     mock.patch.object(ns.indicators_engine, "compute_indicators", return_value={"trend": "up"}), \
     mock.patch.object(ns.trade_filter_engine, "evaluate_trade", return_value={"signal": "-"}), \
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None):
    _ytw.side_effect = _ytw_side_effect
    _riv.return_value = (3200.0, "dcf", 0.08, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "ok", "value_default": False,
        "fcf_distorted_years": [],
    })
    _row = ns.analyze_ticker_lite("7203.T", log=lambda *a, **k: None)

check("analyze_ticker_lite() row is not None", _row is not None)
check('analyze_ticker_lite() returns "_fallback_sector" from Yahoo\'s own info.get("sector") - '
      "zero extra network call", _row.get("_fallback_sector") == "Consumer Cyclical")

with mock.patch.object(ns, "_yf_call_with_retry") as _ytw2, \
     mock.patch.object(ns, "resolve_quality_score", return_value=(70, "auto", False)), \
     mock.patch.object(ns, "resolve_intrinsic_value") as _riv2, \
     mock.patch.object(ns, "resolve_stock_type", return_value=("COMPOUNDER", "auto", False)), \
     mock.patch.object(ns.reverse_dcf_engine, "compute", return_value={"ok": False}), \
     mock.patch.object(ns.trade_filter_engine, "calc_support_resistance",
                        return_value={"support20": 2700, "resistance20": 2900,
                                      "support60": 2600, "resistance60": 3000}), \
     mock.patch.object(ns.indicators_engine, "compute_indicators", return_value={"trend": "up"}), \
     mock.patch.object(ns.trade_filter_engine, "evaluate_trade", return_value={"signal": "-"}), \
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None):
    _ytw2.side_effect = lambda fn, log, ticker, label, **kw: (
        {"currency": "JPY"} if label == "info" else _ytw_side_effect(fn, log, ticker, label, **kw)
    )
    _riv2.return_value = (3200.0, "dcf", 0.08, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "ok", "value_default": False,
        "fcf_distorted_years": [],
    })
    _row_no_sector = ns.analyze_ticker_lite("9999.T", log=lambda *a, **k: None)
check('analyze_ticker_lite(): "_fallback_sector" is None (not "Unknown"/a guess) when Yahoo '
      "itself has no sector for this ticker either",
      _row_no_sector.get("_fallback_sector") is None)

# ======================================================================
# CHECK 3-6: run_universe_scan() end to end, TOPIX-500-shaped (pool has
# NO "Sector" column at all, exactly as fetch_topix500() produces) -
# the saved row's own Sector comes from the Yahoo fallback, and the
# scratch key never reaches scan_store.
# ======================================================================
_topix_pool_df = pd.DataFrame({"Ticker": ["7203.T"]})  # no "Sector" column - TOPIX/Nikkei shape


def _fake_analyze_with_fallback(ticker, attention_lite=True, discount_rate=None, log=print,
                                 rate_limited_out=None, growth_summary_out=None,
                                 oneoff_summary_out=None, shadow_out=None):
    return {
        "Ticker": ticker, "Type": "COMPOUNDER", "Company Name": "Toyota Motor Corp",
        "Price": 2850.0, "Quality": 70, "Quality Default": False,
        "Intrinsic Value": 3200.0, "Intrinsic Default": False,
        "MOS %": 10.9, "Long Score": 65.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
        "Moat": 55.0, "Growth Used": 0.08, "FCF Source": "reported",
        "_fallback_sector": "Consumer Cyclical",
    }


with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_topix_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze_with_fallback):
    ns.run_universe_scan("TOPIX 500", log=lambda *a, **k: None, run_night="2026-10-09")

_topix_saved = scan_store.load_scan("TOPIX 500", allow_private=True)
check("run_universe_scan() saved a TOPIX 500 payload", _topix_saved is not None)
_topix_row = next(r for r in _topix_saved["rows"] if r["Ticker"] == "7203.T")
check('TOPIX 500 row: Sector is "Consumer Cyclical" (the Yahoo fallback), not None - '
      "the real bug, now fixed",
      _topix_row.get("Sector") == "Consumer Cyclical")
check('TOPIX 500 row: the scratch key "_fallback_sector" never reaches the saved scan_store row',
      "_fallback_sector" not in _topix_row)

# ======================================================================
# CHECK 7-8: FTSE-100-shaped (pool DOES carry a real Sector) - the
# pool's own value wins, completely unaffected by this fix, even when
# analyze_ticker_lite() ALSO returns a (different) Yahoo fallback.
# ======================================================================
_ftse_pool_df = pd.DataFrame({"Ticker": ["BARC.L"], "Sector": ["Financials"]})


def _fake_analyze_ftse(ticker, attention_lite=True, discount_rate=None, log=print,
                        rate_limited_out=None, growth_summary_out=None,
                        oneoff_summary_out=None, shadow_out=None):
    row = _fake_analyze_with_fallback(ticker)
    row["Ticker"] = ticker
    row["_fallback_sector"] = "Some Other Yahoo Sector"  # deliberately different - must lose
    return row


with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_ftse_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze_ftse):
    ns.run_universe_scan("FTSE 100", log=lambda *a, **k: None, run_night="2026-10-09")

_ftse_saved = scan_store.load_scan("FTSE 100", allow_private=True)
_ftse_row = next(r for r in _ftse_saved["rows"] if r["Ticker"] == "BARC.L")
check('FTSE 100 row: Sector stays the POOL\'s own real value ("Financials") - the Yahoo '
      "fallback never overrides a universe that already has real sector data",
      _ftse_row.get("Sector") == "Financials")
check('FTSE 100 row: no "_fallback_sector" leak here either',
      "_fallback_sector" not in _ftse_row)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
