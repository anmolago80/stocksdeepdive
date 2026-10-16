"""
Director TASK 7 (16 Oct 2026, "VEIL.L fund flag - blocks UK go-live").

Verified: VEIL.L (Vietnam Enterprise Investments) is a closed-ended
fund; its Yahoo business summary says "closed ended equity mutual
fund". Its stored Sector is "General Financial" (from the Wikipedia
FTSE 250 sector column), so _is_sector_fund() misses it - and
"General Financial" can't be added to _FUND_SECTOR_NAMES because real
operating companies share that label.

1. nightly_scan._FUND_TICKER_OVERRIDE_IS_FUND = {"VEIL.L"}, mirroring
   the existing _FUND_SECTOR_OVERRIDE_NOT_A_FUND (EMG.L) - any ticker
   in it is Is Fund=True regardless of Sector, with the same "Fund
   Reason" treatment as a real sector-matched fund.
2. Review logging only, no auto-flagging: in non-US/non-AU scans, a
   row with Is Fund=False whose Yahoo business summary mentions one of
   the phrases in _FUND_SUMMARY_REVIEW_PHRASES gets one
   "[funds] review: ..." log line - Is Fund itself never changes.

Run: python3 tests/test_director_task7_veil_fund_flag.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import nightly_scan as ns
import scan_store
import scanner_engine

TESTVOL = tempfile.mkdtemp(prefix="task7_veil_fund_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

from unittest import mock

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
check("_FUND_TICKER_OVERRIDE_IS_FUND contains exactly VEIL.L",
      ns._FUND_TICKER_OVERRIDE_IS_FUND == frozenset({"VEIL.L"}))

# ======================================================================
# CHECK 2: EMG.L's existing not-a-fund override is unchanged by this
# task - still not caught, even with an exact fund-sector match.
# ======================================================================
check("EMG.L is still NOT caught even with an exact fund-sector match "
      "(this task doesn't touch the existing override)",
      not ns._is_sector_fund("EMG.L", "Investment Trusts"))
check("EMG.L is not in the NEW is-fund override set",
      "EMG.L" not in ns._FUND_TICKER_OVERRIDE_IS_FUND)

# ======================================================================
# CHECK 3: a "General Financial" row that is NOT VEIL.L is unaffected -
# the new override is ticker-specific, never sector-wide.
# ======================================================================
check('a different ticker with Sector "General Financial" is NOT '
      'caught by _is_sector_fund() (unaffected by this task)',
      not ns._is_sector_fund("AV.L", "General Financial"))


# ======================================================================
# CHECK 4-9: end to end through run_universe_scan() - VEIL.L (Sector
# "General Financial", the real mismatch), a different "General
# Financial" non-fund row (MAN2.L), and EMG.L (sector deliberately set
# to a real fund name, to prove ITS override still wins too) all in
# the same FTSE 250-shaped pool.
# ======================================================================
_ftse250_pool_df = pd.DataFrame({
    "Ticker": ["VEIL.L", "MAN2.L", "EMG.L"],
    "Sector": ["General Financial", "General Financial", "Investment Trusts"],
})

_SUMMARIES = {
    "VEIL.L": (
        "VEIL Investment Managers Limited is a closed ended equity "
        "mutual fund launched by Dragon Capital Group Limited."
    ),
    "MAN2.L": "Example Financial Services Group plc provides insurance and "
              "asset management services to retail and institutional clients.",
    "EMG.L": "Man Group plc is an active investment management business.",
}


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
        "_business_summary": _SUMMARIES.get(ticker),
    }


_logged = []
with mock.patch.object(scanner_engine, "get_universe_pool",
                        return_value=(_ftse250_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze):
    ns.run_universe_scan("FTSE 250", log=_logged.append, run_night="2026-10-16")

_saved = scan_store.load_scan("FTSE 250", allow_private=True)
check("run_universe_scan() saved a FTSE 250 payload", _saved is not None)
_rows_by_ticker = {r["Ticker"]: r for r in _saved["rows"]}

_veil = _rows_by_ticker["VEIL.L"]
check("VEIL.L is flagged Is Fund=True via the ticker override, despite "
      'its stored Sector ("General Financial") never matching '
      "_FUND_SECTOR_NAMES", _veil.get("Is Fund") is True)
check("VEIL.L has Intrinsic Value forced to None", _veil.get("Intrinsic Value") is None)
check("VEIL.L has MOS % forced to None", _veil.get("MOS %") is None)
check('VEIL.L has Valuation forced to "N/A"', _veil.get("Valuation") == "N/A")
check('VEIL.L carries the same "Fund Reason" text as a real sector-matched fund',
      _veil.get("Fund Reason") == "Fund — company valuation does not apply")
check("VEIL.L's Signal is downgraded from STRONG LONG to WATCHLIST, same "
      "as any other fund row", _veil.get("Signal") == "WATCHLIST")
check("VEIL.L still has its real Quality score untouched (valuation-only scope)",
      _veil.get("Quality") == 70)

_man2 = _rows_by_ticker["MAN2.L"]
check('MAN2.L (a DIFFERENT "General Financial" row, not VEIL.L) is '
      "Is Fund=False - the override is ticker-specific",
      _man2.get("Is Fund") is False)
check("MAN2.L keeps its real Intrinsic Value - never touched",
      _man2.get("Intrinsic Value") == 600.0)

_emg = _rows_by_ticker["EMG.L"]
check("EMG.L is still Is Fund=False even with its stored Sector set to "
      '"Investment Trusts" - the existing not-a-fund override still wins',
      _emg.get("Is Fund") is False)
check("EMG.L keeps its real Valuation - the existing override protects "
      "it end to end, unchanged by this task", _emg.get("Valuation") == "FAIR")

# ======================================================================
# CHECK 10-12: review logging - fires once for MAN2.L (summary mentions
# no fund phrase... wait, MAN2.L's own summary is plain; use a row
# whose summary mentions a phrase but isn't flagged) without ever
# changing Is Fund; never fires for a row with no summary at all.
# ======================================================================
_review_lines = [l for l in _logged if l.startswith("[funds] review:")]
check("exactly one review line fires (VEIL.L is excluded - it's already "
      "flagged via the override, not a candidate for review-only logging; "
      "MAN2.L/EMG.L's summaries don't mention a review phrase)",
      len(_review_lines) == 0)

# A row with Is Fund=False and a summary that DOES mention a review
# phrase - the log line fires, carrying the ticker/Sector/phrase, and
# Is Fund is untouched.
_pool_with_hint = pd.DataFrame({
    "Ticker": ["TRUSTCO.L"], "Sector": ["General Financial"],
})


def _fake_analyze_with_hint(ticker, **kwargs):
    return {
        "Ticker": ticker, "Type": "COMPOUNDER", "Company Name": f"{ticker} Co",
        "Price": 500.0, "Quality": 70, "Quality Default": False,
        "Intrinsic Value": 600.0, "Intrinsic Default": False,
        "MOS %": 16.7, "Valuation": "FAIR", "Signal": "STRONG LONG",
        "Long Score": 65.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
        "Moat": 55.0, "Growth Used": 0.08, "FCF Source": "reported",
        "_business_summary": (
            "Trust Co plc is a closed-ended investment trust focused on "
            "UK small-cap equities."
        ),
    }


_logged2 = []
with mock.patch.object(scanner_engine, "get_universe_pool",
                        return_value=(_pool_with_hint, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze_with_hint):
    ns.run_universe_scan("FTSE 250", log=_logged2.append, run_night="2026-10-16")

_review_lines2 = [l for l in _logged2 if l.startswith("[funds] review:")]
check("a non-fund row whose summary mentions a review phrase gets "
      "exactly one review log line",
      len(_review_lines2) == 1)
if _review_lines2:
    check("the review line names the ticker, the Sector, and the "
          "matched phrase, ending '- not flagged'",
          "TRUSTCO.L" in _review_lines2[0]
          and "General Financial" in _review_lines2[0]
          and "closed-ended" in _review_lines2[0]
          and _review_lines2[0].endswith("- not flagged"))

_trustco_saved = scan_store.load_scan("FTSE 250", allow_private=True)
_trustco_row = next(r for r in _trustco_saved["rows"] if r["Ticker"] == "TRUSTCO.L")
check("the review-logged row's Is Fund is still False - review logging "
      "never auto-flags", _trustco_row.get("Is Fund") is False)
check("the review-logged row keeps its real Intrinsic Value - untouched",
      _trustco_row.get("Intrinsic Value") == 600.0)
check("the scratch '_business_summary' key never reaches the saved row "
      "(popped before persisting, same as '_fallback_sector')",
      "_business_summary" not in _trustco_row)

# ======================================================================
# CHECK 13: no business summary at all -> no review line, no crash.
# ======================================================================
_pool_no_summary = pd.DataFrame({"Ticker": ["NOSUM.L"], "Sector": ["General Financial"]})


def _fake_analyze_no_summary(ticker, **kwargs):
    return {
        "Ticker": ticker, "Type": "COMPOUNDER", "Company Name": f"{ticker} Co",
        "Price": 500.0, "Quality": 70, "Quality Default": False,
        "Intrinsic Value": 600.0, "Intrinsic Default": False,
        "MOS %": 16.7, "Valuation": "FAIR", "Signal": "STRONG LONG",
        "Long Score": 65.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
        "Moat": 55.0, "Growth Used": 0.08, "FCF Source": "reported",
        # no "_business_summary" key at all - simulates a totally
        # failed .info fetch for this ticker this run.
    }


_logged3 = []
with mock.patch.object(scanner_engine, "get_universe_pool",
                        return_value=(_pool_no_summary, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze_no_summary):
    ns.run_universe_scan("FTSE 250", log=_logged3.append, run_night="2026-10-16")
check("no business summary at all -> no review line, and the scan "
      "doesn't crash",
      not any(l.startswith("[funds] review:") for l in _logged3))


# ======================================================================
# CHECK 14: US/AU universes - the ticker override is checked
# unconditionally (not gated by universe), matching the instruction's
# "regardless of Sector" (VEIL.L isn't a US/AU ticker in practice, but
# the override's own code path must not silently depend on universe).
# ======================================================================
_us_pool_df = pd.DataFrame({"Ticker": ["VEIL.L"], "Sector": ["General Financial"]})
with mock.patch.object(scanner_engine, "get_universe_pool",
                        return_value=(_us_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze):
    ns.run_universe_scan("S&P 500", log=lambda *a, **k: None, run_night="2026-10-16")
_sp500_saved = scan_store.load_scan("S&P 500", allow_private=True)
_sp500_row = next(r for r in _sp500_saved["rows"] if r["Ticker"] == "VEIL.L")
check("the ticker override fires even inside a nominally US universe "
      "(the override is unconditional, never universe-gated)",
      _sp500_row.get("Is Fund") is True)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
