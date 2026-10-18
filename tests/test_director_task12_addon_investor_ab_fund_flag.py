"""
Director TASK 12 ADD-ON (18 Oct 2026): add INVE-B.ST (Investor AB) to
nightly_scan._FUND_TICKER_OVERRIDE_IS_FUND, alongside VEIL.L.

Investor AB is a listed investment holding company (OMX Stockholm 30).
Its reported net_income_financials includes realised/unrealised gains
on its own listed holdings, not operating earnings - a standard
company valuation doesn't apply to it, same practical effect as
VEIL.L's fund status even though the underlying reason differs (a
holding company's own financials, not a closed-ended fund structure).

Same test pattern as tests/test_director_task7_veil_fund_flag.py's own
end-to-end coverage of VEIL.L - this file mirrors CHECK 4-9 and 14 of
that file for INVE-B.ST specifically, inside an OMX Stockholm 30 pool.

Run: python3 tests/test_director_task12_addon_investor_ab_fund_flag.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task12_addon_inve_fund_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd
import nightly_scan as ns
import scan_store
import scanner_engine

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


# ======================================================================
# CHECK 1: the override set itself.
# ======================================================================
check("INVE-B.ST is in _FUND_TICKER_OVERRIDE_IS_FUND", "INVE-B.ST" in ns._FUND_TICKER_OVERRIDE_IS_FUND)
check("VEIL.L (the existing TASK 7 entry) is still there too - this "
      "add-on is additive, not a replacement",
      "VEIL.L" in ns._FUND_TICKER_OVERRIDE_IS_FUND)
check("_FUND_TICKER_OVERRIDE_IS_FUND is exactly these two tickers, "
      "nothing else added by accident",
      ns._FUND_TICKER_OVERRIDE_IS_FUND == frozenset({"VEIL.L", "INVE-B.ST"}))


# ======================================================================
# CHECK 2-8: end to end through run_universe_scan() - INVE-B.ST in an
# OMX Stockholm 30-shaped pool, alongside a different Swedish ticker
# that must stay unaffected (the override is ticker-specific).
# ======================================================================
_omx_pool_df = pd.DataFrame({
    "Ticker": ["INVE-B.ST", "VOLV-B.ST"],
    "Sector": ["Financial Services", "Industrials"],
})


def _fake_analyze(ticker, attention_lite=True, discount_rate=None, log=print,
                   rate_limited_out=None, growth_summary_out=None,
                   oneoff_summary_out=None, shadow_out=None, info_failed_out=None,
                   universe=None, no_yahoo_price_out=None):
    return {
        "Ticker": ticker, "Type": "COMPOUNDER", "Company Name": f"{ticker} Co",
        "Price": 500.0, "Quality": 70, "Quality Default": False,
        "Intrinsic Value": 600.0, "Intrinsic Default": False,
        "MOS %": 16.7, "Valuation": "FAIR", "Signal": "STRONG LONG",
        "Long Score": 65.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
        "Moat": 55.0, "Growth Used": 0.08, "FCF Source": "reported",
        "_business_summary": None,
    }


_logged = []
with mock.patch.object(scanner_engine, "get_universe_pool",
                        return_value=(_omx_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze):
    ns.run_universe_scan("OMX Stockholm 30", log=_logged.append, run_night="2026-10-18")

_saved = scan_store.load_scan("OMX Stockholm 30", allow_private=True)
check("run_universe_scan() saved an OMX Stockholm 30 payload", _saved is not None)
_rows_by_ticker = {r["Ticker"]: r for r in _saved["rows"]}

_inve = _rows_by_ticker["INVE-B.ST"]
check('INVE-B.ST is flagged Is Fund=True via the ticker override, '
      'despite its stored Sector ("Financial Services") never '
      "matching _FUND_SECTOR_NAMES", _inve.get("Is Fund") is True)
check("INVE-B.ST has Intrinsic Value forced to None", _inve.get("Intrinsic Value") is None)
check("INVE-B.ST has MOS % forced to None", _inve.get("MOS %") is None)
check('INVE-B.ST has Valuation forced to "N/A"', _inve.get("Valuation") == "N/A")
check('INVE-B.ST carries the same "Fund Reason" text as a real sector-matched fund',
      _inve.get("Fund Reason") == "Fund — company valuation does not apply")
check("INVE-B.ST's Signal is downgraded from STRONG LONG to WATCHLIST, "
      "same as any other fund row", _inve.get("Signal") == "WATCHLIST")
check("INVE-B.ST still has its real Quality score untouched (valuation-only scope)",
      _inve.get("Quality") == 70)

_volv = _rows_by_ticker["VOLV-B.ST"]
check("VOLV-B.ST (a DIFFERENT Swedish ticker, not in the override) is "
      "Is Fund=False - the override is ticker-specific",
      _volv.get("Is Fund") is False)
check("VOLV-B.ST keeps its real Intrinsic Value - never touched",
      _volv.get("Intrinsic Value") == 600.0)


# ======================================================================
# CHECK 9: the override is checked unconditionally (not gated by
# universe) - same proof pattern as TASK 7's own CHECK 14 for VEIL.L.
# ======================================================================
_us_pool_df = pd.DataFrame({"Ticker": ["INVE-B.ST"], "Sector": ["Financial Services"]})
with mock.patch.object(scanner_engine, "get_universe_pool",
                        return_value=(_us_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze):
    ns.run_universe_scan("S&P 500", log=lambda *a, **k: None, run_night="2026-10-18")
_sp500_saved = scan_store.load_scan("S&P 500", allow_private=True)
_sp500_row = next(r for r in _sp500_saved["rows"] if r["Ticker"] == "INVE-B.ST")
check("the ticker override fires even inside a nominally US universe "
      "(the override is unconditional, never universe-gated)",
      _sp500_row.get("Is Fund") is True)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
