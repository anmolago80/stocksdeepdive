"""TASK 2 of instruction_scanner_chips_trusts_japan_sectors.md (9 Oct
2026, Director-directed) - build go from the Director's review, with
the real 80-ticker catch list supplied directly (FTSE 100 x4 + FTSE
250 x76, from the Director's own stored readiness CSVs - no CSV file
was ever actually present in this sandbox's uploads, confirmed by a
thorough search before this build; the Director pasted the list
instead).

Rule: a non-US/non-AU company whose stored Sector (from the
constituent pool) case-insensitively EXACT-matches one of five fund-
structure sector names gets no company valuation - same reasoning
moat_engine._is_fund() already applies to the Moat Score for ETF/
mutual-fund quoteTypes, applied here via a second, independent signal
(stored Sector text) because a UK investment trust's own yfinance
quoteType is frequently just "EQUITY".

EMG.L (Man Group) is an explicit, commented override - it is NOT a
fund (operating asset manager; its own stored Sector label is simply
wrong) - never caught regardless of what its stored Sector says.

Run: python3 tests/test_task2_uk_fund_sector_detection.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import nightly_scan as ns
import scan_store
import scanner_engine
import market_readiness_engine
import tempfile

TESTVOL = tempfile.mkdtemp(prefix="task2_uk_fund_test_")
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
# The Director's own real 80-ticker list - FTSE 100 (4) + FTSE 250 (76).
# ======================================================================
FTSE100_FUNDS = ["SMT.L", "ALW.L", "PCT.L", "FCIT.L"]
FTSE250_FUNDS = [
    "RTW.L", "HFEL.L", "CORD.L", "MNTN.L", "BNKR.L", "FGEN.L", "WWH.L", "HVPE.L", "OCI.L",
    "FCSS.L", "ICGT.L", "BRSC.L", "HRI.L", "HSL.L", "EDIN.L", "FEML.L", "TFIF.L", "BRWM.L",
    "HANA.L", "BPCR.L", "UEM.L", "RCP.L", "TRIG.L", "HICL.L", "RICA.L", "BHMG.L", "BRGE.L",
    "FEV.L", "AIE.L", "PIN.L", "CTY.L", "PINT.L", "IAD.L", "SDP.L", "JFJ.L", "SSIT.L",
    "EWI.L", "TRY.L", "ATT.L", "PEY.L", "USA.L", "HGT.L", "ESCT.L", "FGT.L", "JEGI.L",
    "JEDT.L", "PCGH.L", "MRC.L", "MUT.L", "LWDB.L", "JCH.L", "MYI.L", "MRCH.L", "NAIT.L",
    "AGT.L", "ATR.L", "TMPL.L", "CLDN.L", "FSV.L", "GSCT.L", "NAS.L", "BGFD.L", "CGT.L",
    "JGGI.L", "JMGI.L", "PHI.L", "PNL.L", "SAIN.L", "BUT.L", "JEMI.L", "ASL.L", "TEM.L",
    "JAM.L", "AAS.L", "MNKS.L", "NBPE.L",
]
ALL_80_FUNDS = FTSE100_FUNDS + FTSE250_FUNDS
check("the Director's own list is exactly 80 tickers", len(ALL_80_FUNDS) == 80)
check("no duplicates in the 80-ticker list", len(set(ALL_80_FUNDS)) == 80)

REITS_NOT_FUNDS = ["LAND.L", "SGRO.L", "LMP.L", "BBOX.L"]

_FUND_SECTOR_CYCLE = [
    "Investment Trusts", "Collective Investments", "Hedge Funds",
    "Equity Investment Instruments", "Equity Investments",
]

# ======================================================================
# CHECK 1: every one of the 80 real tickers is caught - cycling through
# all 5 recognised sector-name variants so each is proven to fire, not
# just the first one.
# ======================================================================
_all_80_caught = True
for _i, _tkr in enumerate(ALL_80_FUNDS):
    _sector = _FUND_SECTOR_CYCLE[_i % len(_FUND_SECTOR_CYCLE)]
    if not ns._is_sector_fund(_tkr, _sector):
        _all_80_caught = False
        print(f"    MISSED: {_tkr} with sector {_sector!r}")
check("all 80 of the Director's own real tickers are caught by the rule "
      "(cycling through all 5 sector-name variants)", _all_80_caught)

# Case-insensitivity, explicitly, for each of the 5 variants.
check('case-insensitive: "INVESTMENT TRUSTS" (upper) matches',
      ns._is_sector_fund("SMT.L", "INVESTMENT TRUSTS"))
check('case-insensitive: "collective investments" (lower) matches',
      ns._is_sector_fund("HFEL.L", "collective investments"))
check('case-insensitive: "Hedge Funds" (title) matches',
      ns._is_sector_fund("ICGT.L", "Hedge Funds"))
check('case-insensitive: "equity INVESTMENT instruments" (mixed) matches',
      ns._is_sector_fund("PIN.L", "equity INVESTMENT instruments"))
check('the 5th variant, "Equity Investments" (distinct from "Equity Investment '
      'Instruments"), matches on its own',
      ns._is_sector_fund("CTY.L", "Equity Investments"))

# ======================================================================
# CHECK 2: EMG.L override - never a fund, regardless of sector text.
# ======================================================================
check("EMG.L is NOT caught even with an exact fund-sector match",
      not ns._is_sector_fund("EMG.L", "Investment Trusts"))
check("EMG.L is NOT caught with a different fund-sector variant either",
      not ns._is_sector_fund("EMG.L", "Equity Investments"))
check("EMG.L is NOT a fund when its sector is something else entirely",
      not ns._is_sector_fund("EMG.L", "Financial Services"))

# ======================================================================
# CHECK 3: REITs are never caught - a REIT's own stored Sector text is
# a different ICB sub-sector, not in the 5-name set, despite both
# containing the word "Trust"/"Investment".
# ======================================================================
for _reit in REITS_NOT_FUNDS:
    check(f"{_reit} (REIT) is NOT caught with its real REIT sector text",
          not ns._is_sector_fund(_reit, "Real Estate Investment Trusts"))

# ======================================================================
# CHECK 4: a sector merely CONTAINING one of the 5 names as a substring
# must NOT match - exact match only, never substring.
# ======================================================================
check("a sector that merely contains 'equity investments' as a substring, "
      "but isn't an exact match, is NOT caught",
      not ns._is_sector_fund("XXX.L", "UK Equity Investments Fund Managers Group"))
check("None sector is never caught", not ns._is_sector_fund("XXX.L", None))
check("empty-string sector is never caught", not ns._is_sector_fund("XXX.L", ""))

# ======================================================================
# CHECK 5: end to end through run_universe_scan() - a FTSE 250-shaped
# pool with one real fund ticker, one real REIT ticker, and EMG.L
# (sector deliberately set to a fund name to prove the override wins
# even when the stored data itself says "fund").
# ======================================================================
_ftse250_pool_df = pd.DataFrame({
    "Ticker": ["SMT.L", "LAND.L", "EMG.L"],
    "Sector": ["Investment Trusts", "Real Estate Investment Trusts", "Investment Trusts"],
})


def _fake_analyze_uk(ticker, attention_lite=True, discount_rate=None, log=print,
                      rate_limited_out=None, growth_summary_out=None,
                      oneoff_summary_out=None, shadow_out=None, info_failed_out=None):
    return {
        "Ticker": ticker, "Type": "COMPOUNDER", "Company Name": f"{ticker} Co",
        "Price": 500.0, "Quality": 70, "Quality Default": False,
        "Intrinsic Value": 600.0, "Intrinsic Default": False,
        "MOS %": 16.7, "Valuation": "FAIR", "Signal": "STRONG LONG",
        "Long Score": 65.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
        "Moat": 55.0, "Growth Used": 0.08, "FCF Source": "reported",
    }


with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_ftse250_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze_uk):
    ns.run_universe_scan("FTSE 250", log=lambda *a, **k: None, run_night="2026-10-10")

_ftse250_saved = scan_store.load_scan("FTSE 250", allow_private=True)
check("run_universe_scan() saved a FTSE 250 payload", _ftse250_saved is not None)
_rows_by_ticker = {r["Ticker"]: r for r in _ftse250_saved["rows"]}

_smt = _rows_by_ticker["SMT.L"]
check("SMT.L (real fund) is marked Is Fund=True", _smt.get("Is Fund") is True)
check("SMT.L has Intrinsic Value forced to None", _smt.get("Intrinsic Value") is None)
check("SMT.L has MOS % forced to None", _smt.get("MOS %") is None)
check('SMT.L has Valuation forced to "N/A"', _smt.get("Valuation") == "N/A")
check('SMT.L carries the plain-English reason "Fund — company valuation does not apply"',
      _smt.get("Fund Reason") == "Fund — company valuation does not apply")
check("SMT.L's Signal is downgraded from STRONG LONG to WATCHLIST (consistent with every "
      "other N/A row)", _smt.get("Signal") == "WATCHLIST")
check("SMT.L still has its real Quality score untouched (valuation-only scope)",
      _smt.get("Quality") == 70)

_land = _rows_by_ticker["LAND.L"]
check("LAND.L (REIT) is marked Is Fund=False", _land.get("Is Fund") is False)
check("LAND.L keeps its real Intrinsic Value - never touched", _land.get("Intrinsic Value") == 600.0)
check("LAND.L keeps its real MOS % - never touched", _land.get("MOS %") == 16.7)
check('LAND.L keeps its real Valuation ("FAIR") - never touched', _land.get("Valuation") == "FAIR")
check("LAND.L has no Fund Reason", "Fund Reason" not in _land)

_emg = _rows_by_ticker["EMG.L"]
check("EMG.L is marked Is Fund=False even though its OWN stored Sector says "
      '"Investment Trusts" - the override wins', _emg.get("Is Fund") is False)
check("EMG.L keeps its real Intrinsic Value - the override protects it end to end",
      _emg.get("Intrinsic Value") == 600.0)
check("EMG.L keeps its real Valuation - the override protects it end to end",
      _emg.get("Valuation") == "FAIR")

# ======================================================================
# CHECK 6: non-US/non-AU-only - the SAME fund-sector text on an ASX 200
# or S&P 500 pool must never trigger the rule.
# ======================================================================
_asx_pool_df = pd.DataFrame({"Ticker": ["FAKE.AX"], "Sector": ["Investment Trusts"]})
with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_asx_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze_uk):
    ns.run_universe_scan("ASX 200", log=lambda *a, **k: None, run_night="2026-10-10")
_asx_saved = scan_store.load_scan("ASX 200", allow_private=True)
_asx_row = next(r for r in _asx_saved["rows"] if r["Ticker"] == "FAKE.AX")
check("ASX 200 (AU universe): the rule never fires even with an exact fund-sector match",
      _asx_row.get("Is Fund") is False)
check("ASX 200: Intrinsic Value is untouched", _asx_row.get("Intrinsic Value") == 600.0)

_sp500_pool_df = pd.DataFrame({"Ticker": ["FAKE"], "Sector": ["Investment Trusts"]})
with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_sp500_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze_uk):
    ns.run_universe_scan("S&P 500", log=lambda *a, **k: None, run_night="2026-10-10")
_sp500_saved = scan_store.load_scan("S&P 500", allow_private=True)
_sp500_row = next(r for r in _sp500_saved["rows"] if r["Ticker"] == "FAKE")
check("S&P 500 (US universe): the rule never fires even with an exact fund-sector match",
      _sp500_row.get("Is Fund") is False)

# ======================================================================
# CHECK 7: excluded from the Top 200 dry run (market_readiness_engine.
# top200_dry_run) - candidate_count and no_stored_score_count both
# drop the fund row.
# ======================================================================
_dry_run_rows = [
    {"Ticker": "SMT.L", "Is Fund": True, "Long Score": None},   # fund, no score - must be excluded entirely
    {"Ticker": "LAND.L", "Is Fund": False, "Long Score": 70.0},  # real candidate, has a score
    {"Ticker": "ABC.L", "Is Fund": False, "Long Score": None},   # real candidate, no score yet
]
_dry_run_result = market_readiness_engine.top200_dry_run(_dry_run_rows)
check("top200_dry_run(): candidate_count excludes the fund row (2, not 3)",
      _dry_run_result["candidate_count"] == 2)
check("top200_dry_run(): no_stored_score_count excludes the fund row too (1, not 2)",
      _dry_run_result["no_stored_score_count"] == 1)

# ======================================================================
# CHECK 8: excluded from the Scanner's UNDERVALUED count / median MOS /
# Value Map - these already filter on Valuation=="UNDERVALUED"/numeric
# MOS %, which SMT.L's own forced None/"N/A" automatically fails. Proven
# directly against the real saved FTSE 250 rows from CHECK 5 above.
# ======================================================================
_undervalued_n = sum(1 for r in _ftse250_saved["rows"] if r.get("Valuation") == "UNDERVALUED")
check("the fund row (SMT.L, Valuation=N/A) is never counted as UNDERVALUED",
      not any(r["Ticker"] == "SMT.L" and r.get("Valuation") == "UNDERVALUED" for r in _ftse250_saved["rows"]))
_mos_vals_for_median = [r["MOS %"] for r in _ftse250_saved["rows"] if isinstance(r.get("MOS %"), (int, float))]
check("the fund row's MOS % (None) is excluded from the median-MOS input list",
      len(_mos_vals_for_median) == 2)  # LAND.L and EMG.L only, SMT.L's None is filtered out
_value_map_valid = [
    r for r in _ftse250_saved["rows"]
    if isinstance(r.get("MOS %"), (int, float)) and isinstance(r.get("Quality"), (int, float))
]
check("the fund row is excluded from the Value Map's own valid-points filter",
      not any(r["Ticker"] == "SMT.L" for r in _value_map_valid))
check("the REIT and the override-protected EMG.L both remain in the Value Map",
      {"LAND.L", "EMG.L"} <= {r["Ticker"] for r in _value_map_valid})

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
