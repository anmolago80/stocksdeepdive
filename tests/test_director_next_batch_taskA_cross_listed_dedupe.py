"""
Director: next batch, TASK A (14 Oct 2026, "Cross-listed companies -
correcting your TASK 4 finding"). Evidence from production logs (the
2026-10-14 23:00 UTC selection): "[top100] same company name, NOT in
SHARE_CLASS_PAIRS" fired for real cross-market pairs that both compete
for the pool - e.g. QSR/QSR.TO, GFL/GFL.TO, CS.TO/CSC.AX, NWS.AX/NWSA.

Fix: top100_engine._dedupe_cross_listings() - the SAME normalized-
company-name matcher _report_undocumented_same_name_duplicates()
already uses (never a second matcher), restricted to tickers whose
peer_context.market_for() differs (a same-country same-name duplicate
stays exactly as today - reported, never auto-merged). Runs where
_dedupe_share_classes() runs (same call site, right after it, before
_report_undocumented_same_name_duplicates()), on best_by_ticker,
mutated in place, before scoring.

Winner: higher traded value in a common currency (avg_volume x price,
converted to USD via the EXISTING fcf_valuation_engine.fx_rate() -
never a second conversion function); if EITHER side lacks the data to
compute that, falls back to the higher recomputed Value Score for the
WHOLE comparison. One log line per merge naming both values.
CROSS_LISTED_EXCLUDE (empty today) is a set of normalized company
names a human has reviewed and found to be a false match (two
unrelated companies sharing an identical name string) - never
auto-populated.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access (fcf_valuation_engine.fx_rate() is mocked throughout),
same disclosure as every other fixture-based test in this repo. The
REAL winner for each of the Director's own named production pairs
(QSR/QSR.TO, GFL/GFL.TO, etc.) depends on live avg_volume/price data
this sandbox cannot fetch - see this task's own report for that
disclosure; this file proves the MECHANISM, not those specific real
tickers' real numbers.

Run: python3 tests/test_director_next_batch_taskA_cross_listed_dedupe.py
"""
import hashlib
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import peer_context
import top100_engine as te

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
# CHECK 1-3: a cross-country same-name pair - higher traded value
# (avg_volume x price, converted to USD) wins. Synthetic US/Canada pair
# in the shape of the Director's own QSR/QSR.TO example.
# ======================================================================
_best_by_ticker = {
    "USCO": {"ticker": "USCO", "company_name": "US Co Inc", "avg_volume": 1_000_000,
             "price": 50.0, "value_score": 60.0},
    "USCO.TO": {"ticker": "USCO.TO", "company_name": "US Co Inc", "avg_volume": 2_000_000,
                "price": 10.0, "value_score": 65.0},
}
# USCO traded value: 1,000,000 x 50.0 x 1.0 (USD->USD) = $50,000,000
# USCO.TO traded value: 2,000,000 x 10.0 x 0.75 (CAD->USD) = $15,000,000
# USCO should win despite USCO.TO's higher Value Score, since traded
# value (not Value Score) is the FIRST tiebreak when both sides have
# the data.
_logs = []
with mock.patch("fcf_valuation_engine.fx_rate", return_value=(0.75, "live")):
    te._dedupe_cross_listings(_best_by_ticker, log=_logs.append)
check("USCO (higher traded value, $50M vs $15M) wins over USCO.TO despite USCO.TO's "
      "higher Value Score - traded value is the first tiebreak",
      "USCO" in _best_by_ticker and "USCO.TO" not in _best_by_ticker)
check("the surviving USCO row carries 'also_trades_as': 'USCO.TO'",
      _best_by_ticker.get("USCO", {}).get("also_trades_as") == "USCO.TO")
check("one log line names both traded values",
      any("USCO" in l and "USCO.TO" in l and "$50,000,000" in l and "$15,000,000" in l
          for l in _logs))


# ======================================================================
# CHECK 4-5: Canada/Australia pair (CS.TO/CSC.AX shape) - same
# mechanism, proving it's not US-specific.
# ======================================================================
_best_by_ticker2 = {
    "CACO.TO": {"ticker": "CACO.TO", "company_name": "CA Co Ltd", "avg_volume": 500_000,
                "price": 20.0, "value_score": 55.0},
    "CACO.AX": {"ticker": "CACO.AX", "company_name": "CA Co Ltd", "avg_volume": 10_000_000,
                "price": 1.0, "value_score": 50.0},
}
# CACO.TO: 500,000 x 20.0 x 0.75 (CAD->USD) = $7,500,000
# CACO.AX: 10,000,000 x 1.0 x 0.65 (AUD->USD) = $6,500,000
with mock.patch("fcf_valuation_engine.fx_rate", return_value=(0.75, "live")) as _m:
    def _fx_side_effect(from_ccy, to_ccy, log=print):
        return (0.75, "live") if from_ccy == "CAD" else (0.65, "live")
    _m.side_effect = _fx_side_effect
    te._dedupe_cross_listings(_best_by_ticker2, log=lambda *a, **k: None)
check("CACO.TO (higher traded value, $7.5M vs $6.5M) wins over CACO.AX",
      "CACO.TO" in _best_by_ticker2 and "CACO.AX" not in _best_by_ticker2)
check("peer_context.market_for() confirms this really was a cross-country pair "
      "(Canada vs Australia), not a same-country one",
      peer_context.market_for("CACO.TO") == "Canada" and peer_context.market_for("CACO.AX") == "Australia")


# ======================================================================
# CHECK 6-7: either side lacking traded-value data (missing avg_volume)
# falls back to the higher Value Score for the WHOLE comparison.
# ======================================================================
_best_by_ticker3 = {
    "NODATA": {"ticker": "NODATA", "company_name": "No Data Co", "avg_volume": None,
               "price": 50.0, "value_score": 40.0},
    "NODATA.TO": {"ticker": "NODATA.TO", "company_name": "No Data Co", "avg_volume": 1_000_000,
                  "price": 10.0, "value_score": 70.0},
}
_logs3 = []
with mock.patch("fcf_valuation_engine.fx_rate", return_value=(0.75, "live")):
    te._dedupe_cross_listings(_best_by_ticker3, log=_logs3.append)
check("with avg_volume missing on one side, NODATA.TO (higher Value Score, 70 vs 40) wins - "
      "the fallback compares Value Score for BOTH sides, never a half-traded-value mix",
      "NODATA.TO" in _best_by_ticker3 and "NODATA" not in _best_by_ticker3)
check("the fallback log line names Value Score, not a traded value",
      any("Value Score" in l for l in _logs3))


# ======================================================================
# CHECK 8: a same-country same-name duplicate is left COMPLETELY alone
# by this function - same-country pairs stay as today (reported by
# _report_undocumented_same_name_duplicates(), never auto-merged).
# ======================================================================
_best_by_ticker4 = {
    "SAMECO1": {"ticker": "SAMECO1", "company_name": "Same Country Co", "avg_volume": 1_000_000,
                "price": 50.0, "value_score": 40.0},
    "SAMECO2": {"ticker": "SAMECO2", "company_name": "Same Country Co", "avg_volume": 2_000_000,
                "price": 10.0, "value_score": 70.0},
}
te._dedupe_cross_listings(_best_by_ticker4, log=lambda *a, **k: None)
check("a same-country (both USA) same-name duplicate is untouched - both tickers still present",
      "SAMECO1" in _best_by_ticker4 and "SAMECO2" in _best_by_ticker4)


# ======================================================================
# CHECK 9: CROSS_LISTED_EXCLUDE - a normalized name on this list is
# never cross-listing-deduped, even across two countries.
# ======================================================================
_best_by_ticker5 = {
    "FALSEMATCH.TO": {"ticker": "FALSEMATCH.TO", "company_name": "Generic Holdings",
                       "avg_volume": 1_000_000, "price": 50.0, "value_score": 40.0},
    "FALSEMATCH.AX": {"ticker": "FALSEMATCH.AX", "company_name": "Generic Holdings",
                       "avg_volume": 2_000_000, "price": 10.0, "value_score": 70.0},
}
with mock.patch.object(te, "CROSS_LISTED_EXCLUDE", frozenset({"generic holdings"})):
    te._dedupe_cross_listings(_best_by_ticker5, log=lambda *a, **k: None)
check("a name on CROSS_LISTED_EXCLUDE is never touched, even though it spans two countries",
      "FALSEMATCH.TO" in _best_by_ticker5 and "FALSEMATCH.AX" in _best_by_ticker5)
check("CROSS_LISTED_EXCLUDE is empty by default - no known false match yet",
      te.CROSS_LISTED_EXCLUDE == frozenset())


# ======================================================================
# CHECK 10: a ticker that already won a SHARE_CLASS_PAIRS dedupe (its
# own "also_trades_as" already set) keeps that value - this function
# never overwrites an existing relationship.
# ======================================================================
_best_by_ticker6 = {
    "ALREADY": {"ticker": "ALREADY", "company_name": "Already Co", "avg_volume": 1_000_000,
                "price": 50.0, "value_score": 90.0, "also_trades_as": "ALREADY-B"},
    "ALREADY.TO": {"ticker": "ALREADY.TO", "company_name": "Already Co", "avg_volume": 500_000,
                   "price": 10.0, "value_score": 40.0},
}
with mock.patch("fcf_valuation_engine.fx_rate", return_value=(0.75, "live")):
    te._dedupe_cross_listings(_best_by_ticker6, log=lambda *a, **k: None)
check("ALREADY wins (higher traded value) and keeps its PRE-EXISTING 'also_trades_as' "
      "('ALREADY-B', from an earlier SHARE_CLASS_PAIRS dedupe) rather than it being overwritten",
      _best_by_ticker6.get("ALREADY", {}).get("also_trades_as") == "ALREADY-B")


# ======================================================================
# CHECK 11: called with no cross-country collisions at all (the common
# case, every existing test's own fixture shape) - a complete no-op,
# byte-identical to before this function existed.
# ======================================================================
_best_by_ticker7 = {
    "ADP": {"ticker": "ADP", "company_name": "ADP-shaped", "avg_volume": 1_000_000,
            "price": 50.0, "value_score": 60.0},
    "CSL.AX": {"ticker": "CSL.AX", "company_name": "CSL-shaped", "avg_volume": 500_000,
               "price": 200.0, "value_score": 70.0},
}
_before = dict(_best_by_ticker7)
te._dedupe_cross_listings(_best_by_ticker7, log=lambda *a, **k: None)
check("with no cross-country same-name collision present, _dedupe_cross_listings() is a "
      "complete no-op - byte-identical to before this function existed",
      _best_by_ticker7 == _before)


# ======================================================================
# CHECK 12: Director PART 2 review - a REAL 3-way group from production
# (NWS.AX / NWSA / NWSLV.AX, all company_name="news corporation", 2
# Australia + 1 USA). _dedupe_cross_listings() groups by name first
# (by_name.setdefault(name, []).append(ticker)), so a group of any size
# falls out of the SAME code path as a pair - "winner, *losers =
# ranked" already generalizes to N-1 losers for any N. Confirms: EXACTLY
# ONE ticker survives, the OTHER TWO are both dropped, two log lines
# (one per loser), and the survivor's also_trades_as now lists BOTH
# dropped tickers (comma-joined) - a real gap this review caught: before
# this fix it silently kept only losers[0], dropping the third ticker's
# identity from the display badge while it was still correctly excluded
# from scoring either way.
# ======================================================================
_best_by_ticker8 = {
    "NWS.AX": {"ticker": "NWS.AX", "company_name": "News Corporation",
               "avg_volume": 500_000, "price": 30.0, "value_score": 50.0},
    "NWSA": {"ticker": "NWSA", "company_name": "News Corporation",
             "avg_volume": 3_000_000, "price": 28.0, "value_score": 55.0},
    "NWSLV.AX": {"ticker": "NWSLV.AX", "company_name": "News Corporation",
                 "avg_volume": 100_000, "price": 30.0, "value_score": 45.0},
}
_logs8 = []
with mock.patch("fcf_valuation_engine.fx_rate", return_value=(0.66, "live")):
    te._dedupe_cross_listings(_best_by_ticker8, log=_logs8.append)
check("3-way News Corp group: exactly ONE ticker survives (NWSA, "
      "highest traded value - its USD-converted 3,000,000 x 28.0 beats "
      "both AUD-converted AU listings)",
      list(_best_by_ticker8.keys()) == ["NWSA"])
check("3-way News Corp group: BOTH other tickers (NWS.AX, NWSLV.AX) "
      "are dropped, not just one",
      "NWS.AX" not in _best_by_ticker8 and "NWSLV.AX" not in _best_by_ticker8)
check("3-way News Corp group: one log line per dropped ticker (two "
      "total), not one line for the whole group",
      len(_logs8) == 2
      and any("NWS.AX" in l for l in _logs8) and any("NWSLV.AX" in l for l in _logs8))
check("3-way News Corp group: the survivor's also_trades_as now names "
      "BOTH dropped tickers, not just the first one",
      set(_best_by_ticker8["NWSA"]["also_trades_as"].split(", ")) == {"NWS.AX", "NWSLV.AX"})


# ======================================================================
# CHECK 13: the scoring-request fingerprint is unchanged - this task
# never touches _request_params()/the batch-submission entrant path.
# Same fixture/fingerprint tests/test_nextround_item3_top200_exclusion.py
# already carries.
# ======================================================================
FIXTURE_ENTRANTS = [
    {"ticker": "ADP", "company_name": "ADP-shaped", "sector": "Industrials"},
    {"ticker": "CSL.AX", "company_name": "CSL-shaped", "sector": "Health Care"},
    {"ticker": "AZN_FIXTURE.L", "company_name": "AZN-shaped", "sector": "Health Care"},
    {"ticker": "RY_FIXTURE.TO", "company_name": "RY-shaped", "sector": "Financials"},
    {"ticker": "7203_FIXTURE.T", "company_name": "Toyota-shaped", "sector": "Consumer Discretionary"},
]
EXPECTED_FINGERPRINT = "f00c69f5392af5e2ca435fb4c8a314393c14b4c0dc498cc4293a59d1276e5196"
_params = te._request_params(FIXTURE_ENTRANTS)
_fp = hashlib.sha256(json.dumps(_params, sort_keys=True).encode()).hexdigest()
check("scoring-request fingerprint unchanged by TASK A "
      f"(_request_params() sha256 still {EXPECTED_FINGERPRINT})",
      _fp == EXPECTED_FINGERPRINT)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
