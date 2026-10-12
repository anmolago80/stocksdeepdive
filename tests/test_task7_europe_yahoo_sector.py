"""
Director's overnight queue, TASK 7 (12 Oct 2026): "Europe sector
labels" - use Yahoo's own sector field IN PLACE OF the Wikipedia-
scraped "sector"/"industry" column, for exactly five private
universes (DAX, CAC 40, AEX, SMI, OMX Stockholm 30) - scanner_engine.py's
own fetch_dax()/fetch_cac40()/fetch_aex()/fetch_smi()/fetch_omxs30()
all parse a real "sector"/"industry" Wikipedia column (unlike TOPIX
500/Nikkei 225, which have none at all - TASK 3's own, separate,
EMPTY-ONLY fallback already covers those two and is untouched here).

This is a DIFFERENT precedence rule from TASK 3's, not a reuse of it:
TASK 3 only reaches for Yahoo's sector when the pool's own column is
missing; TASK 7 asks for Yahoo's sector to be preferred for these five
universes EVEN WHEN the pool's own (Wikipedia) column has something -
the Wikipedia "sector"/"industry" text for these five is the thing
being replaced, not merely backstopped.

FIX, scoped purely by universe name (nightly_scan.run_universe_scan()'s
own per-ticker loop, right where TASK 3's fallback already lives):

    _EUROPE_YAHOO_SECTOR_UNIVERSES = {
        "DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30",
    }
    if universe in _EUROPE_YAHOO_SECTOR_UNIVERSES:
        row["Sector"] = _yahoo_sector_fallback or _sector_by_ticker.get(t)
    else:
        row["Sector"] = _sector_by_ticker.get(t) or _yahoo_sector_fallback

A hardcoded local set, not scan_store._DEFAULT_PRIVATE_UNIVERSES (which
also lists Nikkei 225/TOPIX 500 - would widen scope beyond the five
named here) and not read from the PRIVATE_UNIVERSES env var (would let
an unrelated production config change silently change which universes
get this precedence flip). For every universe NOT in the five-name set,
the line is byte-identical to the one TASK 3 already shipped - this is
an added branch, not a modification of the shared line other universes
go through.

Covers:
  - One of the five (DAX, as the representative case): pool/Wikipedia
    HAS a real sector, Yahoo's own sector is different - Yahoo wins
    (the new behaviour this task asks for).
  - One of the five: pool/Wikipedia has a real sector, Yahoo returns
    nothing for this ticker - the Wikipedia value is still used (never
    blanked to None just because Yahoo had nothing).
  - A non-scoped universe with a real pool sector (FTSE 100): completely
    unaffected - pool's own value still wins over a different Yahoo
    value, byte-identical to before this task, same proof shape as
    tests/test_task3_japan_sector_fallback.py's own CHECK 7-8.
  - A non-scoped universe with NO pool sector at all (TOPIX 500):
    completely unaffected - still falls back to Yahoo exactly as
    TASK 3 already shipped, byte-identical to before this task.
  - Each of the other four scoped universes (CAC 40, AEX, SMI, OMX
    Stockholm 30) independently exercises the same Yahoo-wins rule -
    not just DAX as a stand-in for all five.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_task7_europe_yahoo_sector.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task7_europe_yahoo_sector_test_")
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


def _fake_analyze(yahoo_sector):
    def _inner(ticker, attention_lite=True, discount_rate=None, log=print,
               rate_limited_out=None, growth_summary_out=None, oneoff_summary_out=None,
               shadow_out=None, info_failed_out=None, universe=None, no_yahoo_price_out=None):
        return {
            "Ticker": ticker, "Type": "COMPOUNDER", "Company Name": "Test Co",
            "Price": 100.0, "Quality": 70, "Quality Default": False,
            "Intrinsic Value": 120.0, "Intrinsic Default": False,
            "MOS %": 16.7, "Long Score": 65.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
            "Moat": 55.0, "Growth Used": 0.08, "FCF Source": "reported",
            "_fallback_sector": yahoo_sector,
        }
    return _inner


# ======================================================================
# CHECK 1-2: DAX (one of the five) - pool has a real Wikipedia sector,
# Yahoo has a DIFFERENT one. Yahoo wins (the new rule).
# ======================================================================
_dax_pool_df = pd.DataFrame({"Ticker": ["SAP.DE"], "Sector": ["Technology (Wikipedia/ICB label)"]})
with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_dax_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze("Technology (Yahoo)")):
    ns.run_universe_scan("DAX", log=lambda *a, **k: None, run_night="2026-10-12")
_dax_saved = scan_store.load_scan("DAX", allow_private=True)
_dax_row = next(r for r in _dax_saved["rows"] if r["Ticker"] == "SAP.DE")
check("TASK 7: DAX row's Sector is Yahoo's own value, not the Wikipedia/ICB label, "
      "even though the pool DID carry a real sector",
      _dax_row.get("Sector") == "Technology (Yahoo)")
check("TASK 7: the scratch key never reaches the saved scan_store row",
      "_fallback_sector" not in _dax_row)

# ======================================================================
# CHECK 3: DAX again - pool has a real sector, Yahoo has NOTHING for
# this ticker. The Wikipedia value survives (never blanked to None).
# ======================================================================
_dax_pool_df2 = pd.DataFrame({"Ticker": ["BMW.DE"], "Sector": ["Consumer Cyclical (Wikipedia)"]})
with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_dax_pool_df2, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze(None)):
    ns.run_universe_scan("DAX", log=lambda *a, **k: None, run_night="2026-10-12")
_dax_saved2 = scan_store.load_scan("DAX", allow_private=True)
_dax_row2 = next(r for r in _dax_saved2["rows"] if r["Ticker"] == "BMW.DE")
check("TASK 7: when Yahoo has no sector at all for this ticker, the Wikipedia/pool "
      "value is still used, not left None",
      _dax_row2.get("Sector") == "Consumer Cyclical (Wikipedia)")

# ======================================================================
# CHECK 4-5: a NON-scoped universe (FTSE 100) with a real pool sector -
# completely unaffected, pool's own value still wins over a different
# Yahoo value, byte-identical to before TASK 7.
# ======================================================================
_ftse_pool_df = pd.DataFrame({"Ticker": ["BARC.L"], "Sector": ["Financials"]})
with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_ftse_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze("Some Other Yahoo Sector")):
    ns.run_universe_scan("FTSE 100", log=lambda *a, **k: None, run_night="2026-10-12")
_ftse_saved = scan_store.load_scan("FTSE 100", allow_private=True)
_ftse_row = next(r for r in _ftse_saved["rows"] if r["Ticker"] == "BARC.L")
check("TASK 7: a non-scoped universe (FTSE 100) keeps the pool's own real sector - "
      "completely unaffected by this task's new precedence rule",
      _ftse_row.get("Sector") == "Financials")
check("TASK 7: no scratch-key leak for the unaffected universe either",
      "_fallback_sector" not in _ftse_row)

# ======================================================================
# CHECK 6: a NON-scoped universe with NO pool sector at all (TOPIX 500)
# - still falls back to Yahoo exactly as TASK 3 already shipped, byte-
# identical to before TASK 7.
# ======================================================================
_topix_pool_df = pd.DataFrame({"Ticker": ["7203.T"]})  # no "Sector" column
with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_topix_pool_df, "test source")), \
     mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze("Consumer Cyclical")):
    ns.run_universe_scan("TOPIX 500", log=lambda *a, **k: None, run_night="2026-10-12")
_topix_saved = scan_store.load_scan("TOPIX 500", allow_private=True)
_topix_row = next(r for r in _topix_saved["rows"] if r["Ticker"] == "7203.T")
check("TASK 7: TOPIX 500 (not one of the five) still uses TASK 3's own empty-only "
      "fallback, unaffected by TASK 7's new precedence rule",
      _topix_row.get("Sector") == "Consumer Cyclical")

# ======================================================================
# CHECK 7-10: the other four scoped universes (CAC 40, AEX, SMI, OMX
# Stockholm 30) each independently get the same Yahoo-wins rule - not
# just DAX as a stand-in for all five.
# ======================================================================
_cases = [
    ("CAC 40", "LVMH.PA", "Consumer Discretionary (Wikipedia)", "Luxury Goods (Yahoo)"),
    ("AEX", "ASML.AS", "Technology (Wikipedia)", "Semiconductors (Yahoo)"),
    ("SMI", "NESN.SW", "Consumer Staples (Wikipedia)", "Packaged Foods (Yahoo)"),
    ("OMX Stockholm 30", "VOLV-B.ST", "Industrials (Wikipedia)", "Trucks (Yahoo)"),
]
for universe, ticker, wiki_sector, yahoo_sector in _cases:
    _pool_df = pd.DataFrame({"Ticker": [ticker], "Sector": [wiki_sector]})
    with mock.patch.object(scanner_engine, "get_universe_pool", return_value=(_pool_df, "test source")), \
         mock.patch.object(ns, "analyze_ticker_lite", side_effect=_fake_analyze(yahoo_sector)):
        ns.run_universe_scan(universe, log=lambda *a, **k: None, run_night="2026-10-12")
    _saved = scan_store.load_scan(universe, allow_private=True)
    _row = next(r for r in _saved["rows"] if r["Ticker"] == ticker)
    check(f"TASK 7: {universe} row's Sector is Yahoo's own value ({yahoo_sector!r}), "
          f"not the Wikipedia label ({wiki_sector!r})",
          _row.get("Sector") == yahoo_sector)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
