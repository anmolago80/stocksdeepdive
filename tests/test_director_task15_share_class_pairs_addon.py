"""
Director TASK 15 (18 Oct 2026): add 7 verified dual-class pairs to
SHARE_CLASS_PAIRS.

Verified in production logs: every nightly Top 200 selection logged
"[top100] same company name, NOT in SHARE_CLASS_PAIRS - review and add
by hand if genuine" for 7 US pairs. Owner has reviewed: all are genuine
dual share classes of one company with equal economic rights (voting
differs only) - UA/UAA, BF-A/BF-B, LBRDA/LBRDK, LLYVA/LLYVK, UHAL/
UHAL-B, RUSHA/RUSHB, BATRA/BATRK.

Added as 7 new frozensets to share_class_engine.SHARE_CLASS_PAIRS,
same style as the existing GOOG/GOOGL etc. pairs. No other code
change - top100_engine._dedupe_share_classes() and share_class_
engine.whole_company_shares() both already read this one list
dynamically (top100_engine.py does `from share_class_engine import
SHARE_CLASS_PAIRS`), so being ON the list is the entire mechanism:
  (a) _dedupe_share_classes() now collapses each pair to the higher-
      average-volume ticker in the Top 200 pool, same volume-first
      rule every other pair already uses.
  (b) whole_company_shares() now reaches tier 3 (the cached, targeted
      income-statement "Diluted Average Shares" fetch) for any of
      these 14 tickers when tiers 1-2 (a caller-supplied income
      statement, info["impliedSharesOutstanding"]) don't resolve it -
      same mechanism TASK 3's own VOW.DE/VOW3.DE addition already
      proved for a different pair.
  (c) _report_undocumented_same_name_duplicates() (the function that
      logs the nightly review line) runs AFTER _dedupe_share_classes()
      on the SAME best_by_ticker dict - once a pair is collapsed to
      one ticker there, the "more than one ticker under this name"
      condition that line checks for can never fire for it again.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access (confirmed directly: a real yf.Ticker(...).info call
fails with "CONNECT tunnel failed, response 403" here), same
disclosure as every other fixture-based test in this repo and as
TASK 3's own VOW.DE/VOW3.DE test file. The task's own report states
this plainly for item 3 (the 14-ticker share-count table) rather than
inventing live numbers.

Run: python3 tests/test_director_task15_share_class_pairs_addon.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task15_share_class_pairs_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import share_class_engine as sce
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


NEW_PAIRS = [
    ("UA", "UAA"),
    ("BF-A", "BF-B"),
    ("LBRDA", "LBRDK"),
    ("LLYVA", "LLYVK"),
    ("UHAL", "UHAL-B"),
    ("RUSHA", "RUSHB"),
    ("BATRA", "BATRK"),
]
PRE_EXISTING_PAIRS = [
    ("GOOG", "GOOGL"), ("FOX", "FOXA"), ("NWS", "NWSA"),
    ("BRK-A", "BRK-B"), ("HEI", "HEI-A"), ("VOW.DE", "VOW3.DE"),
]


# ======================================================================
# CHECK 1-7: all 7 new pairs are now in SHARE_CLASS_PAIRS.
# ======================================================================
for a, b in NEW_PAIRS:
    check(f"frozenset({{{a!r}, {b!r}}}) is in SHARE_CLASS_PAIRS",
          frozenset({a, b}) in sce.SHARE_CLASS_PAIRS)

# ======================================================================
# CHECK 8: the 6 pre-existing pairs are all still present, unchanged
# (purely additive).
# ======================================================================
check("all 6 pre-existing pairs (GOOG/GOOGL, FOX/FOXA, NWS/NWSA, "
      "BRK-A/BRK-B, HEI/HEI-A, VOW.DE/VOW3.DE) are still present",
      all(frozenset({a, b}) in sce.SHARE_CLASS_PAIRS for a, b in PRE_EXISTING_PAIRS))

check("SHARE_CLASS_PAIRS now has exactly 13 pairs (6 pre-existing + 7 new)",
      len(sce.SHARE_CLASS_PAIRS) == 13)


# ======================================================================
# CHECK 9-10: every one of the 14 new tickers is in _KNOWN_MULTI_CLASS_
# TICKERS - the set whole_company_shares() actually checks for tier 3
# eligibility.
# ======================================================================
_new_tickers = {t for pair in NEW_PAIRS for t in pair}
check("all 14 new tickers are in _KNOWN_MULTI_CLASS_TICKERS",
      _new_tickers <= sce._KNOWN_MULTI_CLASS_TICKERS)
check("the 6 pre-existing pairs' own tickers are still in "
      "_KNOWN_MULTI_CLASS_TICKERS too",
      {t for pair in PRE_EXISTING_PAIRS for t in pair} <= sce._KNOWN_MULTI_CLASS_TICKERS)


# ======================================================================
# CHECK 11-24 (one pair at a time): item 2a - top100_engine.
# _dedupe_share_classes() collapses each new pair to the higher-
# average-volume ticker, with also_trades_as naming the other -
# identical mechanism to every pre-existing pair, exercised directly
# for all 7.
# ======================================================================
for a, b in NEW_PAIRS:
    _best_by_ticker = {
        a: {"ticker": a, "avg_volume": 1_000_000, "value_score": 40.0},
        b: {"ticker": b, "avg_volume": 5_000_000, "value_score": 38.0},
    }
    te._dedupe_share_classes(_best_by_ticker, log=lambda *a2, **k: None)
    check(f"{a}/{b}: the higher-avg_volume ticker ({b}) wins, {a} is dropped",
          a not in _best_by_ticker and b in _best_by_ticker)
    check(f"{a}/{b}: the survivor ({b}) carries also_trades_as={a!r}",
          _best_by_ticker.get(b, {}).get("also_trades_as") == a)


# ======================================================================
# CHECK 25-38 (one ticker at a time): item 2b/3 - whole_company_
# shares() reaches tier 3 (the cached targeted filed-diluted-shares
# fetch) for each of the 14 new tickers when tiers 1-2 don't resolve
# (no income_df, no info["impliedSharesOutstanding"] at all) - and the
# accepted share count is the tier-3 FILED figure, not the single-
# class sharesOutstanding.
# ======================================================================
for a, b in NEW_PAIRS:
    for t in (a, b):
        _info = {"sharesOutstanding": 10_000_000}  # no impliedSharesOutstanding at all
        with mock.patch.object(sce, "_fetch_diluted_shares", return_value=25_000_000) as _m:
            shares, flagged, source, note = sce.whole_company_shares(_info, ticker=t)
        check(f"{t}: with tiers 1-2 unresolved, whole_company_shares() reaches "
              "tier 3 (calls the cached targeted fetch for THIS ticker)",
              _m.call_args == mock.call(t))
        check(f"{t}: tier 3's filed figure is accepted as the whole-company share count",
              flagged is True and source == "filed" and shares == 25_000_000 and note is None)


# ======================================================================
# CHECK 39-40: a ticker NOT on the list never reaches tier 3, even with
# the exact same shape of input - proves tier-3 eligibility is really
# gated on SHARE_CLASS_PAIRS membership, not just "any ratio-clearing
# candidate with a ticker given".
# ======================================================================
_info_unknown = {"sharesOutstanding": 10_000_000}
with mock.patch.object(sce, "_fetch_diluted_shares") as _m_unknown:
    shares_u, flagged_u, source_u, note_u = sce.whole_company_shares(
        _info_unknown, ticker="SOMERANDOMTICKER")
check("a ticker outside SHARE_CLASS_PAIRS never calls the tier-3 targeted fetch",
      _m_unknown.call_count == 0)
check("...and falls through to plain sharesOutstanding, unflagged",
      flagged_u is False and source_u is None and shares_u == 10_000_000)


# ======================================================================
# CHECK 41-44: regression - the pre-existing GOOG/GOOGL pair's own
# behaviour (tier 3 routing AND _dedupe_share_classes()) is completely
# unaffected by this purely-additive addition of 7 more pairs.
# ======================================================================
_info_goog = {"sharesOutstanding": 100_000}
with mock.patch.object(sce, "_fetch_diluted_shares", return_value=215_000) as _m_goog:
    shares_g, flagged_g, source_g, note_g = sce.whole_company_shares(_info_goog, ticker="GOOG")
check("regression: GOOG still reaches tier 3 and is accepted, unaffected by the new pairs",
      _m_goog.call_args == mock.call("GOOG")
      and flagged_g is True and source_g == "filed" and shares_g == 215_000)

_best_by_ticker_goog = {
    "GOOG": {"ticker": "GOOG", "avg_volume": 1_500_000, "value_score": 60.0},
    "GOOGL": {"ticker": "GOOGL", "avg_volume": 2_500_000, "value_score": 62.0},
}
te._dedupe_share_classes(_best_by_ticker_goog, log=lambda *a, **k: None)
check("regression: GOOG/GOOGL's own dedupe (GOOGL wins, higher avg_volume) is unaffected",
      "GOOG" not in _best_by_ticker_goog and "GOOGL" in _best_by_ticker_goog
      and _best_by_ticker_goog["GOOGL"].get("also_trades_as") == "GOOG")


# ======================================================================
# CHECK 45-51 (one pair at a time): item 2c/4 - the nightly "same
# company name, NOT in SHARE_CLASS_PAIRS" review line no longer fires
# for any of the 7 new pairs, run through the REAL pipeline order
# (_dedupe_share_classes() then _report_undocumented_same_name_
# duplicates(), same call-site order select_top100_pool() itself
# uses) - while an undocumented pair (same shape, NOT on the list)
# still fires, proving this isn't just "the function never fires at
# all any more".
# ======================================================================
for a, b in NEW_PAIRS:
    _best = {
        a: {"ticker": a, "company_name": f"{a} Shaped Co", "avg_volume": 1_000_000,
            "value_score": 40.0},
        b: {"ticker": b, "company_name": f"{a} Shaped Co", "avg_volume": 5_000_000,
            "value_score": 38.0},
    }
    te._dedupe_share_classes(_best, log=lambda *a2, **k: None)
    _review_logs = []
    te._report_undocumented_same_name_duplicates(_best, log=_review_logs.append)
    check(f"{a}/{b}: the nightly 'same company name, NOT in SHARE_CLASS_PAIRS' "
          "review line no longer fires - the pair was already collapsed to one "
          "ticker by _dedupe_share_classes() before this function ever ran",
          len(_review_logs) == 0)

_undocumented_best = {
    "UNDOCA": {"ticker": "UNDOCA", "company_name": "Undocumented Pair Co",
               "avg_volume": 1_000_000, "value_score": 40.0},
    "UNDOCB": {"ticker": "UNDOCB", "company_name": "Undocumented Pair Co",
               "avg_volume": 5_000_000, "value_score": 38.0},
}
te._dedupe_share_classes(_undocumented_best, log=lambda *a, **k: None)
_undoc_review_logs = []
te._report_undocumented_same_name_duplicates(_undocumented_best, log=_undoc_review_logs.append)
check("contrast: a genuinely undocumented same-name pair (not on SHARE_CLASS_PAIRS) "
      "still fires the review line - proving the 7 new pairs' silence above is "
      "because they're now documented, not because the function stopped working",
      len(_undoc_review_logs) == 1 and "NOT in SHARE_CLASS_PAIRS" in _undoc_review_logs[0])


# ======================================================================
# CHECK 52: top100_engine.py's own module-level SHARE_CLASS_PAIRS
# binding (imported from share_class_engine, never a second copy) is
# the SAME object identity's contents - the real list _dedupe_share_
# classes() reads already reflects this task's addition, not a stale
# import captured before the edit.
# ======================================================================
check("top100_engine.SHARE_CLASS_PAIRS (the binding _dedupe_share_classes() "
      "actually reads) contains all 7 new pairs too - a live import, never "
      "a second, independently-maintained copy",
      all(frozenset({a, b}) in te.SHARE_CLASS_PAIRS for a, b in NEW_PAIRS))


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
