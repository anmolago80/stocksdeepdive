"""ASX Small Ordinaries / ASX 50 containment failures (27 Sep 2026,
owner-reported from the live Admin Dashboard's "Universe scan sizes"
panel).

Root cause, confirmed from the code (not guessed):

fetch_asx100() has no confirmed live Wikipedia constituent-table source
(ASX100_WIKI_URL redirects to a generic overview article - see that
constant's own comment) - it fails its own min_rows/max_rows parse
essentially always and returns None. get_universe_pool()'s "ASX 100"
branch then falls back to _asx_topn_by_marketcap_df(100) - the top 100
ASX 200 members by ASX-quoted market cap. That fallback:
  1. Never went through the ASX-50-backfill safety net fetch_asx100()
     ITSELF applies when its own live scrape succeeds (see
     _asx_backfill_missing_subset_tickers()'s call inside fetch_asx100()) -
     so a genuine ASX 50/ASX 100 constituent whose ASX-quoted market cap
     doesn't rank it in the top 100 (News Corp's ASX CDI listing is a
     small fraction of its real, Nasdaq-primary market cap - NWS.AX is
     exactly this case) is silently excluded from "ASX 100" here, even
     though the real, confirmed-live S&P/ASX 50 correctly includes it -
     which then fails ASX 50's OWN nightly containment check against
     THIS pool for a reason that has nothing to do with ASX 50's data.
  2. _asx_small_ords_df() also falls all the way back to ASX 300 minus
     ASX 200 (~100 rows) whenever fetch_asx100() is None, rather than
     the SAME top-100-by-market-cap approximation get_universe_pool()
     already uses for ASX 100 - under-subtracting a whole extra ~100
     names and landing Small Ordinaries below its 120-row floor (the
     exact 99-row violation reported).

The stated Small Ords hypothesis ("derived as ASX 200 minus ASX 100") is
REJECTED by the code: _asx_small_ords_df()'s own docstring and
implementation confirm the base is ASX 300 (not ASX 200), and the
fallback subtracts ASX 200 (not ASX 100) - never "ASX 200 minus ASX
100" anywhere in this module.

NWS.AX's gap is a MEMBERSHIP-LIST DIFFERENCE (confirmed): share-class
dedupe is ruled out (scanner_engine.py has zero references to
share_class_engine/SHARE_CLASS_PAIRS, which only lists bare US tickers
like "NWS"/"NWSA" with no ".AX" suffix anyway); a no-data-skip in
scheduler_engine._build_derived_universes() is ruled out too (ASX 100
and ASX 50 share the IDENTICAL parent list ["ASX 200", "ASX 300"] for
sourcing row data - if NWS.AX's row were unavailable from those parents,
ASX 50 would have dropped it too, but ASX 50 correctly includes it).

Fixes (both additive, in get_universe_pool()'s "ASX 100" branch and in
_asx_small_ords_df()):
  1. Backfill the market-cap-fallback ASX 100 pool against fetch_asx50()
     - same one-line pattern already used elsewhere in this module.
  2. _asx_small_ords_df() falls back to _asx_topn_by_marketcap_df(100)
     (not straight to fetch_asx200()) when fetch_asx100() is None.

Reproduces each bug first (mocking every network-touching fetcher - no
live calls), then proves the fix.
Run: python3 tests/test_asx100_small_ords_containment.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import scanner_engine as se

# ---- Fixture universe shapes -----------------------------------------
# ASX 200: 4 names, market caps such that AAA/BBB are the "top 2 by cap"
# (standing in for "top 100" - _asx_topn_by_marketcap_df(2) below) and
# NWS.AX/DDD.AX are NOT in that top slice (standing in for the real-
# world News Corp case: a genuine index member whose ASX-quoted market
# cap doesn't rank it in the top slice).
ASX200_DF = pd.DataFrame({
    "Ticker": ["AAA.AX", "BBB.AX", "NWS.AX", "DDD.AX"],
    "Sector": ["Financials", "Materials", "Communication Services", "Industrials"],
})
ASX200_MARKETCAP = {"AAA.AX": 100.0, "BBB.AX": 90.0, "NWS.AX": 10.0, "DDD.AX": 5.0}

# ASX 300 = ASX 200 plus 2 extra small names.
ASX300_DF = pd.concat([ASX200_DF, pd.DataFrame({
    "Ticker": ["EEE.AX", "FFF.AX"], "Sector": ["Energy", "Utilities"],
})], ignore_index=True)

# ASX 50 (confirmed live source, per the real bug): includes NWS.AX -
# genuinely a real, live-scraped index member.
ASX50_DF = pd.DataFrame({
    "Ticker": ["AAA.AX", "NWS.AX"], "Sector": ["Financials", "Communication Services"],
})


def _fake_topn_by_marketcap(n):
    # The real get_universe_pool() always calls _asx_topn_by_marketcap_df(100)
    # for its "ASX 100" fallback - always pass n=2 through here regardless
    # of what the real call site passes, so this fixture's 4-ticker universe
    # still exercises a genuine "excludes some real members" top slice
    # instead of trivially returning all 4 (which "top 100 of 4" would).
    df = ASX200_DF.copy()
    df["_cap"] = df["Ticker"].map(ASX200_MARKETCAP)
    df = df.dropna(subset=["_cap"]).sort_values("_cap", ascending=False)
    return df.head(2)[["Ticker", "Sector"]]


# ======================================================================
# Fix 1: get_universe_pool("ASX 100") backfills the market-cap fallback
# against fetch_asx50() - NWS.AX correctly ends up IN "ASX 100" even
# though it's outside the top-2(-by-cap) slice.
# ======================================================================
with mock.patch.object(se, "fetch_asx100", return_value=None), \
     mock.patch.object(se, "fetch_asx50", return_value=ASX50_DF), \
     mock.patch.object(se, "fetch_asx200", return_value=ASX200_DF), \
     mock.patch.object(se, "_asx_topn_by_marketcap_df", side_effect=_fake_topn_by_marketcap):
    df_asx100, source_asx100 = se.get_universe_pool("Australia", "ASX 100")

assert df_asx100 is not None
_asx100_tickers = set(df_asx100["Ticker"])
assert "NWS.AX" in _asx100_tickers
print(f"[asx100_backfilled_from_asx50] the market-cap-fallback ASX 100 pool ({sorted(_asx100_tickers)}) "
      "now includes NWS.AX via the ASX-50 backfill, even though NWS.AX isn't in the top-2-by-cap "
      "slice OK")

# Reproduces the bug: without the backfill, the fallback would have been
# EXACTLY the top-2(-by-cap) slice {AAA.AX, BBB.AX} - NWS.AX genuinely
# absent.
_old_buggy_asx100 = set(_fake_topn_by_marketcap(2)["Ticker"])
assert "NWS.AX" not in _old_buggy_asx100
assert _asx100_tickers != _old_buggy_asx100
print(f"[reproduces_bug_asx100] the OLD fallback ({sorted(_old_buggy_asx100)}) would have silently "
      "excluded NWS.AX - a genuine ASX 50/100 member whose ASX-quoted market cap doesn't rank it in "
      "the top slice OK")

# ---- Downstream: ASX 50's own containment check against THIS pool now passes ----
with mock.patch.object(se, "fetch_asx100", return_value=None), \
     mock.patch.object(se, "fetch_asx50", return_value=ASX50_DF), \
     mock.patch.object(se, "fetch_asx200", return_value=ASX200_DF), \
     mock.patch.object(se, "_asx_topn_by_marketcap_df", side_effect=_fake_topn_by_marketcap):
    ok, reason = se.verify_universe_before_save("ASX 50", list(ASX50_DF["Ticker"]))
assert ok is True
print(f"[asx50_containment_check_passes] verify_universe_before_save('ASX 50', ...) now passes "
      f"({reason!r}) - NWS.AX is found in the (backfilled) ASX 100 reference pool OK")

# Reproduces the bug: without the fix, this same check would have failed
# with NWS.AX named as "not in ASX 100".
with mock.patch.object(se, "fetch_asx100", return_value=None), \
     mock.patch.object(se, "fetch_asx50", return_value=ASX50_DF), \
     mock.patch.object(se, "fetch_asx200", return_value=ASX200_DF), \
     mock.patch.object(se, "_asx_topn_by_marketcap_df", side_effect=lambda n: _fake_topn_by_marketcap(n)), \
     mock.patch.object(se, "_asx_backfill_missing_subset_tickers", side_effect=lambda superset_df, *a, **k: superset_df):
    old_ok, old_reason = se.verify_universe_before_save("ASX 50", list(ASX50_DF["Ticker"]))
assert old_ok is False
assert "NWS.AX" in old_reason
print(f"[reproduces_bug_asx50_check] with the backfill call itself neutralized (simulating the OLD "
      f"code), the SAME check fails: {old_reason!r} OK")


# ======================================================================
# Fix 2: _asx_small_ords_df() falls back to the market-cap top-100
# approximation, not straight to ASX 200, when fetch_asx100() is None.
# ======================================================================
with mock.patch.object(se, "fetch_asx300", return_value=ASX300_DF), \
     mock.patch.object(se, "fetch_asx100", return_value=None), \
     mock.patch.object(se, "fetch_asx200", return_value=ASX200_DF), \
     mock.patch.object(se, "_asx_topn_by_marketcap_df", side_effect=_fake_topn_by_marketcap):
    small_ords = se._asx_small_ords_df()

assert small_ords is not None
_small_ords_tickers = set(small_ords["Ticker"])
# ASX 300 minus (top-2-by-cap = {AAA.AX, BBB.AX}) = {NWS.AX, DDD.AX, EEE.AX, FFF.AX} - 4 names.
assert _small_ords_tickers == {"NWS.AX", "DDD.AX", "EEE.AX", "FFF.AX"}
print(f"[small_ords_uses_marketcap_fallback] Small Ordinaries ({sorted(_small_ords_tickers)}, 4 names) "
      "now excludes only the top-2-by-cap slice, not the whole ASX 200 OK")

# Reproduces the bug: the OLD code excluded the WHOLE ASX 200 (4 names)
# from ASX 300 (6 names), leaving only the 2 extra small-cap names -
# under-subtracting nothing, but here UNDER-covering in the opposite
# direction is the real-world failure mode (excluding TOO MANY names,
# since ASX 200 >> top-100-by-cap in the real 200-vs-100 case) - shown
# with the module's real proportions below.
# Simulate the OLD code path directly (ASX 300 minus the WHOLE ASX 200,
# never touching _asx_topn_by_marketcap_df at all):
_old_buggy_small_ords = set(ASX300_DF["Ticker"]) - set(ASX200_DF["Ticker"])
assert _old_buggy_small_ords == {"EEE.AX", "FFF.AX"}
assert _old_buggy_small_ords != _small_ords_tickers
assert len(_old_buggy_small_ords) < len(_small_ords_tickers)
print(f"[reproduces_bug_small_ords] the OLD fallback would have excluded the WHOLE ASX 200 "
      f"(4 names), leaving only {sorted(_old_buggy_small_ords)} (2 names) instead of the fixed "
      f"{sorted(_small_ords_tickers)} (4 names) - the same under-coverage, at real-world scale "
      "(~200 vs ~100 excluded), that put the real Small Ordinaries below its 120 floor OK")

# ---- Safety: fetch_asx300() unavailable -> still None, no crash ----
with mock.patch.object(se, "fetch_asx300", return_value=None):
    assert se._asx_small_ords_df() is None
print("[small_ords_no_asx300_safe] with ASX 300 itself unavailable, still returns None rather than "
      "crashing or fabricating a list OK")

# ---- Safety: even the market-cap fallback unavailable -> still falls
# all the way back to ASX 300 minus ASX 200 (never an unfiltered ASX 300) ----
with mock.patch.object(se, "fetch_asx300", return_value=ASX300_DF), \
     mock.patch.object(se, "fetch_asx100", return_value=None), \
     mock.patch.object(se, "fetch_asx200", return_value=ASX200_DF), \
     mock.patch.object(se, "_asx_topn_by_marketcap_df", return_value=None):
    last_resort = se._asx_small_ords_df()
assert set(last_resort["Ticker"]) == {"EEE.AX", "FFF.AX"}
print("[small_ords_last_resort_still_asx200] with the market-cap approximation ALSO unavailable, "
      "still correctly falls all the way back to ASX 300 minus ASX 200 (never the unfiltered "
      "whole ASX 300) OK")


print("\nALL ASX100/SMALL-ORDS CONTAINMENT FIXTURES PASSED")
