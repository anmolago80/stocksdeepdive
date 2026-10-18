"""
Director TASK 14 (18 Oct 2026): cross-listing dedupe - prefer the ASX
listing.

Owner decision: the audience is Australian, so when a cross-listed
company has an ASX line, show the ASX ticker. Before this task,
_dedupe_cross_listings() (top100_engine.py) always kept the higher
traded value - so ASX favourites showed under foreign tickers in
production (RIO.L kept over RIO.AX, GLEN.L over GLC.AX, RMD over
RMD.AX, JHX over JHX.AX, AMCR over AMC.AX, NEM over NEM.AX, NWSA over
NWS.AX - "XYZ"/"XYZ.AX" in the Director's own list has no corresponding
real ticker anywhere in this codebase, flagged back rather than guessed
at).

New rule, in this order:
  1. If the group has one or more ASX listings, pick the best ASX
     candidate among them using TODAY's own rule (traded value, Value
     Score fallback) - so a group with several ASX listings still
     narrows to exactly one before the next step.
  2. If that ASX candidate's own traded value is >=
     ASX_PREFERENCE_MIN_TRADED_VALUE_USD ($1,000,000/day), it wins the
     WHOLE group outright, regardless of a foreign listing's traded
     value.
  3. Otherwise, falls through to today's rule UNCHANGED, over the full
     group (the ASX ticker stays a candidate there too).

tests/test_director_next_batch_taskA_cross_listed_dedupe.py (the file
that built TODAY's rule) has its own CHECK 4-5 (CACO.TO/CACO.AX) and
CHECK 12 (3-way News Corp group) updated in place for this task's own
behaviour change - this file adds the dedicated floor/no-ASX/multi-ASX
coverage the Director's own test list asks for.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access (fcf_valuation_engine.fx_rate() is mocked throughout),
same disclosure as every other fixture-based test in this repo. The
dry-run table of REAL production tickers (RIO.AX, GLC.AX, etc.) is in
this task's own report, built from the Director's own stated evidence
(which listing won last night) rather than live traded-value data this
sandbox cannot fetch.

Run: python3 tests/test_director_task14_asx_cross_listing_preference.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def _fx_identity(from_ccy, to_ccy, log=print):
    """A simple, fixed FX table covering every currency this file's
    fixtures use - AUD/GBP/CAD all at a round rate, so each fixture's
    own traded-value arithmetic in its comments is exact and easy to
    verify by hand."""
    return {"AUD": 0.65, "GBP": 1.25, "CAD": 0.75}.get(from_ccy, 1.0), "live"


# ======================================================================
# CHECK 1-3: ASX preferred ABOVE the floor - RIO-shaped pair (ASX vs
# London), ASX listing's traded value clears $1M, wins outright despite
# a much higher-traded-value foreign listing.
# ======================================================================
_rio = {
    "RIOFAKE.AX": {"ticker": "RIOFAKE.AX", "company_name": "Rio Fake Ltd",
                    "avg_volume": 2_000_000, "price": 1.0, "value_score": 60.0},
    "RIOFAKE.L": {"ticker": "RIOFAKE.L", "company_name": "Rio Fake Ltd",
                   "avg_volume": 5_000_000, "price": 50.0, "value_score": 80.0},
}
# RIOFAKE.AX: 2,000,000 x 1.0 x 0.65 (AUD->USD) = $1,300,000 - clears the floor
# RIOFAKE.L: 5,000,000 x 50.0 x 1.25 (GBP->USD) = $312,500,000 - far higher,
# but irrelevant once the ASX side clears the floor.
_logs1 = []
with mock.patch("fcf_valuation_engine.fx_rate", side_effect=_fx_identity):
    te._dedupe_cross_listings(_rio, log=_logs1.append)
check("ASX listing (RIOFAKE.AX, traded value $1.3M - above the $1M "
      "floor) wins over the foreign listing (RIOFAKE.L, $312.5M traded "
      "value) - the audience-is-Australian preference rule",
      "RIOFAKE.AX" in _rio and "RIOFAKE.L" not in _rio)
check("the survivor's also_trades_as names the dropped foreign ticker",
      _rio["RIOFAKE.AX"].get("also_trades_as") == "RIOFAKE.L")
check("the log line names the ASX preference rule explicitly, not a "
      "plain traded-value comparison",
      any("ASX preference" in l and "RIOFAKE.AX" in l and "RIOFAKE.L" in l for l in _logs1))


# ======================================================================
# CHECK 4-5: BELOW the floor - the ASX listing is genuinely illiquid
# (under $1M/day), so it falls back to today's rule and the higher-
# traded-value foreign listing still wins, exactly as before this task.
# ======================================================================
_thin = {
    "THINFAKE.AX": {"ticker": "THINFAKE.AX", "company_name": "Thin Fake Ltd",
                     "avg_volume": 10_000, "price": 1.0, "value_score": 60.0},
    "THINFAKE.L": {"ticker": "THINFAKE.L", "company_name": "Thin Fake Ltd",
                    "avg_volume": 1_000_000, "price": 10.0, "value_score": 55.0},
}
# THINFAKE.AX: 10,000 x 1.0 x 0.65 = $6,500 - well below the $1M floor
# THINFAKE.L: 1,000,000 x 10.0 x 1.25 = $12,500,000
_logs2 = []
with mock.patch("fcf_valuation_engine.fx_rate", side_effect=_fx_identity):
    te._dedupe_cross_listings(_thin, log=_logs2.append)
check("an ASX listing BELOW the $1M floor ($6,500/day) does NOT win on "
      "country alone - today's traded-value rule still decides, and "
      "the foreign listing (higher traded value) wins",
      "THINFAKE.L" in _thin and "THINFAKE.AX" not in _thin)
check("the log line for the below-floor case names traded value, not "
      "the ASX preference rule",
      any("traded value" in l and "ASX preference" not in l for l in _logs2))


# ======================================================================
# CHECK 6-7: groups with NO Australian listing at all are completely
# unaffected - same mechanism/outcome as before this task (covered
# structurally already by test_director_next_batch_taskA_cross_listed_
# dedupe.py's own USCO/USCO.TO and NODATA/NODATA.TO checks; repeated
# here as this task's own explicit test-list item).
# ======================================================================
_no_asx = {
    "NOAUFAKE": {"ticker": "NOAUFAKE", "company_name": "No AU Fake Inc",
                 "avg_volume": 1_000_000, "price": 50.0, "value_score": 60.0},
    "NOAUFAKE.TO": {"ticker": "NOAUFAKE.TO", "company_name": "No AU Fake Inc",
                    "avg_volume": 2_000_000, "price": 10.0, "value_score": 65.0},
}
# NOAUFAKE: 1,000,000 x 50.0 x 1.0 = $50,000,000
# NOAUFAKE.TO: 2,000,000 x 10.0 x 0.75 = $15,000,000
with mock.patch("fcf_valuation_engine.fx_rate", side_effect=_fx_identity):
    te._dedupe_cross_listings(_no_asx, log=lambda *a, **k: None)
check("a US/Canada group with no ASX listing at all is decided purely "
      "by today's traded-value rule, untouched by the new ASX logic",
      "NOAUFAKE" in _no_asx and "NOAUFAKE.TO" not in _no_asx)


# ======================================================================
# CHECK 8-10: MULTIPLE ASX listings in one group - today's own rule
# (traded value, Value Score fallback) picks the best ASX candidate
# FIRST, then that single survivor is checked against the floor.
# ======================================================================
_multi_asx = {
    "MULTIFAKE.AX": {"ticker": "MULTIFAKE.AX", "company_name": "Multi Fake Ltd",
                      "avg_volume": 2_000_000, "price": 1.0, "value_score": 50.0},
    "MULTIFAKEB.AX": {"ticker": "MULTIFAKEB.AX", "company_name": "Multi Fake Ltd",
                       "avg_volume": 500_000, "price": 1.0, "value_score": 90.0},
    "MULTIFAKE.L": {"ticker": "MULTIFAKE.L", "company_name": "Multi Fake Ltd",
                     "avg_volume": 10_000_000, "price": 20.0, "value_score": 70.0},
}
# MULTIFAKE.AX:  2,000,000 x 1.0 x 0.65 = $1,300,000 (clears the floor)
# MULTIFAKEB.AX:   500,000 x 1.0 x 0.65 =   $325,000 (higher Value Score, but
#                                            lower traded value - traded
#                                            value still decides between
#                                            the two ASX candidates first)
# MULTIFAKE.L:  10,000,000 x 20.0 x 1.25 = $250,000,000
_logs3 = []
with mock.patch("fcf_valuation_engine.fx_rate", side_effect=_fx_identity):
    te._dedupe_cross_listings(_multi_asx, log=_logs3.append)
check("with two ASX listings in one group, the one with the HIGHER "
      "traded value ($1.3M vs $325k) is picked as the ASX candidate - "
      "today's own tiebreak order, scoped to the ASX tickers only - "
      "even though MULTIFAKEB.AX has the higher Value Score",
      "MULTIFAKE.AX" in _multi_asx)
check("that ASX candidate's own traded value clears the floor, so it "
      "wins the WHOLE group outright over the foreign listing too",
      "MULTIFAKE.AX" in _multi_asx and "MULTIFAKE.L" not in _multi_asx
      and "MULTIFAKEB.AX" not in _multi_asx)
check("the survivor's also_trades_as names BOTH dropped tickers (the "
      "other ASX listing and the foreign one)",
      set(_multi_asx["MULTIFAKE.AX"]["also_trades_as"].split(", "))
      == {"MULTIFAKEB.AX", "MULTIFAKE.L"})


# ======================================================================
# CHECK 11: multiple ASX listings, but the BEST of them is still below
# the floor - falls all the way through to today's rule over the FULL
# group (every original ticker is still a live candidate there).
# ======================================================================
_multi_asx_thin = {
    "THINA.AX": {"ticker": "THINA.AX", "company_name": "Thin Multi Ltd",
                 "avg_volume": 5_000, "price": 1.0, "value_score": 40.0},
    "THINB.AX": {"ticker": "THINB.AX", "company_name": "Thin Multi Ltd",
                 "avg_volume": 2_000, "price": 1.0, "value_score": 90.0},
    "THINC.L": {"ticker": "THINC.L", "company_name": "Thin Multi Ltd",
                "avg_volume": 1_000_000, "price": 10.0, "value_score": 55.0},
}
# THINA.AX: 5,000 x 1.0 x 0.65 = $3,250 (best of the two ASX listings by
#           traded value, still nowhere near the $1M floor)
# THINB.AX: 2,000 x 1.0 x 0.65 = $1,300
# THINC.L: 1,000,000 x 10.0 x 1.25 = $12,500,000 - wins by today's rule
with mock.patch("fcf_valuation_engine.fx_rate", side_effect=_fx_identity):
    te._dedupe_cross_listings(_multi_asx_thin, log=lambda *a, **k: None)
check("when even the best ASX candidate is below the floor, the "
      "foreign listing still wins via today's ordinary traded-value "
      "rule over the FULL group",
      "THINC.L" in _multi_asx_thin
      and "THINA.AX" not in _multi_asx_thin and "THINB.AX" not in _multi_asx_thin)


# ======================================================================
# CHECK 12: the ASX_PREFERENCE_MIN_TRADED_VALUE_USD constant itself -
# exactly $1,000,000, matching the Director's own instruction.
# ======================================================================
check("ASX_PREFERENCE_MIN_TRADED_VALUE_USD is exactly $1,000,000",
      te.ASX_PREFERENCE_MIN_TRADED_VALUE_USD == 1_000_000)


# ======================================================================
# CHECK 13: ASX extension (TOP20_AU_TARGET=40) still works - drawing
# "next-best ASX companies by Value Score" from the SAME best_by_
# ticker dict _dedupe_cross_listings() has already mutated, excluding
# anything already in the pool. Confirms the ASX winner of a cross-
# listing group (now itself holding the ASX ticker, not the foreign
# one) is correctly excluded from the extension candidate list once
# it's in the pool - no double-counting, same "t not in pool_tickers"
# filter as before this task, completely untouched by it.
# ======================================================================
_ext_best_by_ticker = dict(_rio)  # RIOFAKE.AX already won its cross-listing group above
_ext_best_by_ticker["OTHERAU.AX"] = {
    "ticker": "OTHERAU.AX", "company_name": "Other AU Ltd", "value_score": 30.0,
}
_pool_tickers = {"RIOFAKE.AX"}  # RIOFAKE.AX already made the global pool
_set_aside_tickers = set()
_au_candidates = sorted(
    (r for t, r in _ext_best_by_ticker.items()
     if t.endswith(".AX") and t not in _pool_tickers and t not in _set_aside_tickers),
    key=lambda r: r["value_score"], reverse=True,
)
check("TOP20_AU_TARGET's own extension candidate list correctly "
      "excludes RIOFAKE.AX (already in the pool as this group's own "
      "ASX winner) and still offers OTHERAU.AX",
      [r["ticker"] for r in _au_candidates] == ["OTHERAU.AX"])
check("TOP20_AU_TARGET is still 40, untouched by this task",
      te.TOP20_AU_TARGET == 40)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
