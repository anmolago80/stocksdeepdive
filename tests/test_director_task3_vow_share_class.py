"""
Director's work queue, TASK 3 (13 Oct 2026): implement the overnight
queue's own TASK 6 proposal - add frozenset({"VOW.DE", "VOW3.DE"}) to
share_class_engine.SHARE_CLASS_PAIRS, same explicit, hand-maintained
pattern as the five existing pairs (GOOG/GOOGL, FOX/FOXA, NWS/NWSA,
BRK-A/BRK-B, HEI/HEI-A). Volkswagen AG has two listed share classes on
XETRA - VOW.DE (ordinary, voting) and VOW3.DE (preferred, non-voting,
the far more liquid of the two) - each ticker's own info[
"sharesOutstanding"] reflects only its OWN class, not the whole
company, the same root cause SHARE_CLASS_PAIRS already exists to fix
for HEI/HEI-A.

Correction (Director's own explicit instruction, carried from the
overnight queue's TASK 6 report): an undercounted shares denominator
makes fcf_per_share = fcf / shares too LARGE, i.e. OVERSTATES per-
share intrinsic value - not "understates" (the real-world symptom:
VOW3.DE's stored IV ~972 vs price ~68, a value far too HIGH, not too
low). fcf_valuation_engine.py's own pre-existing docstring comment
(~line 2024, from the original A3 audit fix, 27 Sep 2026) had this
backwards too - fixed here (comment-only, no behaviour change) as
part of this same task.

Covers:
  - VOW.DE/VOW3.DE are now in SHARE_CLASS_PAIRS and _KNOWN_MULTI_
    CLASS_TICKERS - whole_company_shares(ticker="VOW3.DE") now reaches
    tier 3 (the cached targeted filed-diluted-shares fetch) exactly
    like HEI/HEI-A already does, and tier 2 ("implied") now requires
    corroboration for VOW3.DE too (previously accepted on the ratio
    alone, since it wasn't on the known list).
  - A corroborated whole-company share count is accepted - the
    overstated-IV symptom is fixed going forward for VOW3.DE/VOW.DE.
  - An UNCORROBORATED implied candidate for VOW3.DE is now rejected
    with a note (the SAME protection HEI/HEI-A already gets) - proven
    as a regression-shaped check, not assumed.
  - None of the five PRE-EXISTING pairs' own behaviour changes -
    GOOG/GOOGL's own corroboration/rejection paths are re-run here
    unchanged, proving this is a purely additive list entry.
  - top100_engine._dedupe_share_classes() (the SAME single source of
    truth, imported from share_class_engine.SHARE_CLASS_PAIRS, never a
    second copy) now also collapses a VOW.DE/VOW3.DE duplicate pair in
    the Top 200 pool - exercised directly to prove the reuse is live,
    not just imported and unused.
  - DAX (Volkswagen's own universe) is PRIVATE - scan_store.
    is_private_universe("DAX") is True - so this ticker never reaches
    ANY currently-public universe's own pool (S&P 500, ASX 200, FTSE
    100/250, TSX 60/Composite, TOPIX 500); select_top100_pool()'s own
    output is proven byte-identical before/after this list addition.
  - The fcf_valuation_engine.py docstring correction itself: the fixed
    text says "overstate", the old "understate" wording is gone.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. VOW3.DE's real stored IV/price figures (972/68) are the
Director's own reported real-world numbers, used here only as context
in this docstring, never as a specific value asserted by a check.

Run: python3 tests/test_director_task3_vow_share_class.py
"""
import inspect
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task3_vow_share_class_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import fcf_valuation_engine as fve
import scan_store
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


# ======================================================================
# CHECK 1-2: VOW.DE/VOW3.DE are now a listed pair, same mechanism as
# the five pre-existing ones - never a special case.
# ======================================================================
check("frozenset({'VOW.DE', 'VOW3.DE'}) is in SHARE_CLASS_PAIRS",
      frozenset({"VOW.DE", "VOW3.DE"}) in sce.SHARE_CLASS_PAIRS)
check("the five pre-existing pairs are all still present, unchanged (purely additive)",
      frozenset({"GOOG", "GOOGL"}) in sce.SHARE_CLASS_PAIRS
      and frozenset({"FOX", "FOXA"}) in sce.SHARE_CLASS_PAIRS
      and frozenset({"NWS", "NWSA"}) in sce.SHARE_CLASS_PAIRS
      and frozenset({"BRK-A", "BRK-B"}) in sce.SHARE_CLASS_PAIRS
      and frozenset({"HEI", "HEI-A"}) in sce.SHARE_CLASS_PAIRS)


# ======================================================================
# CHECK 3-4: a corroborated implied candidate for VOW3.DE is now
# accepted via tier 2 - the overstated-IV symptom's real fix.
# ======================================================================
# VOW3.DE's own sharesOutstanding reflects only the preferred class
# (~206M); impliedSharesOutstanding carries the whole-company total
# (~501M, ordinary + preferred) - a real ~2.4x ratio, inside the sane
# ceiling, same shape as HEI/HEI-A's own ~2.2x root-cause case.
info_vow3 = {"sharesOutstanding": 206_205_445, "impliedSharesOutstanding": 501_295_621}
with mock.patch.object(sce, "_fetch_diluted_shares", return_value=501_295_621):
    shares, flagged, source, note = sce.whole_company_shares(info_vow3, ticker="VOW3.DE")
check("VOW3.DE: a corroborated implied whole-company share count is accepted",
      flagged is True and source == "implied" and note is None)
check("VOW3.DE: the accepted share count is the WHOLE-COMPANY total, not just the preferred "
      "class's own sharesOutstanding - this is what corrects fcf_per_share's denominator",
      shares == 501_295_621)


# ======================================================================
# CHECK 5-6: an UNCORROBORATED implied candidate for VOW3.DE is now
# rejected with a note - the SAME protection HEI/HEI-A already have,
# now extended to VOW3.DE because it's on the known-ticker list.
# ======================================================================
info_vow3_uncorrob = {"sharesOutstanding": 206_205_445, "impliedSharesOutstanding": 480_000_000}
with mock.patch.object(sce, "_fetch_diluted_shares", return_value=None):
    shares_u, flagged_u, source_u, note_u = sce.whole_company_shares(
        info_vow3_uncorrob, ticker="VOW3.DE")
check("VOW3.DE: an uncorroborated implied candidate (no filed figure to check it against) "
      "is rejected, with a note, not silently accepted",
      flagged_u is False and source_u is None and note_u is not None and "uncorroborated" in note_u)
check("VOW3.DE: rejected means the caller falls back to the PLAIN sharesOutstanding (the "
      "preferred-class-only figure) - a known-real number instead of a fabricated one",
      shares_u == 206_205_445)


# ======================================================================
# CHECK 7-8: regression - the pre-existing GOOG/GOOGL pair's own
# behaviour (both accept and reject paths) is completely unaffected
# by this purely-additive list entry.
# ======================================================================
info_goog_2x = {"sharesOutstanding": 100_000, "impliedSharesOutstanding": 220_000}
with mock.patch.object(sce, "_fetch_diluted_shares", return_value=215_000):
    shares_g, flagged_g, source_g, note_g = sce.whole_company_shares(info_goog_2x, ticker="GOOG")
check("regression: GOOG's own corroborated-accept path is unaffected by adding VOW.DE/VOW3.DE",
      flagged_g is True and source_g == "implied" and shares_g == 220_000)
with mock.patch.object(sce, "_fetch_diluted_shares", return_value=None):
    shares_g2, flagged_g2, source_g2, note_g2 = sce.whole_company_shares(info_goog_2x, ticker="GOOG")
check("regression: GOOG's own uncorroborated-reject path is unaffected",
      flagged_g2 is False and source_g2 is None and note_g2 is not None)


# ======================================================================
# CHECK 9-10: top100_engine._dedupe_share_classes() - the SAME single
# source of truth (imported, not copied) now also collapses a VOW.DE/
# VOW3.DE duplicate pair, same as it already does for the five
# pre-existing pairs - proven live, not just imported and unused.
# ======================================================================
_best_by_ticker = {
    "VOW.DE": {"ticker": "VOW.DE", "avg_volume": 500_000, "value_score": 40.0},
    "VOW3.DE": {"ticker": "VOW3.DE", "avg_volume": 3_000_000, "value_score": 38.0},
}
_dedupe_logs = []
te._dedupe_share_classes(_best_by_ticker, log=_dedupe_logs.append)
check("_dedupe_share_classes() collapses VOW.DE/VOW3.DE down to one - the far more liquid "
      "VOW3.DE (higher avg_volume) wins, same volume-first rule as every other pair",
      "VOW.DE" not in _best_by_ticker and "VOW3.DE" in _best_by_ticker)
check("the surviving VOW3.DE row carries 'also_trades_as': 'VOW.DE'",
      _best_by_ticker.get("VOW3.DE", {}).get("also_trades_as") == "VOW.DE")


# ======================================================================
# CHECK 11-13: DAX is PRIVATE - this ticker never reaches any PUBLIC
# universe's pool, and select_top100_pool()'s own output is byte-
# identical with the just-added VOW.DE/VOW3.DE pair present vs. removed
# from the list top100_engine actually reads (top100_engine.py's own
# `from share_class_engine import SHARE_CLASS_PAIRS` - a separate
# module-level binding, patched directly here rather than on
# share_class_engine itself, so the comparison exercises the REAL list
# _dedupe_share_classes() reads). Nothing about pool SELECTION reads
# this list at all - only the dedupe step, which only ever REMOVES a
# loser row, never adds one; a pair with neither ticker present in any
# eligible universe's rows changes nothing.
# ======================================================================
check("DAX is a PRIVATE universe - Volkswagen never reaches a currently-public universe's pool",
      scan_store.is_private_universe("DAX"))
_vow_pair = frozenset({"VOW.DE", "VOW3.DE"})
_pairs_without_vow = [p for p in te.SHARE_CLASS_PAIRS if p != _vow_pair]
check("sanity: the VOW pair really is in the list top100_engine.py reads (so removing it for "
      "one isolated call below is a genuine before/after, not a no-op)",
      len(_pairs_without_vow) == len(te.SHARE_CLASS_PAIRS) - 1)
_pool_with_vow_pair = te.select_top100_pool(log=lambda *a, **k: None)
with mock.patch.object(te, "SHARE_CLASS_PAIRS", _pairs_without_vow):
    _pool_without_vow_pair = te.select_top100_pool(log=lambda *a, **k: None)
check("select_top100_pool()'s own output is byte-identical with or without the VOW.DE/VOW3.DE "
      "pair in the list top100_engine.py actually reads - no public universe's pool contains "
      "either ticker, so this addition changes nothing about the real pool",
      json.dumps(_pool_with_vow_pair, sort_keys=True, default=str) ==
      json.dumps(_pool_without_vow_pair, sort_keys=True, default=str))
check("neither VOW.DE nor VOW3.DE entered the real public pool either way (DAX is excluded "
      "from pool selection entirely, as a private universe)",
      not any(r["ticker"] in ("VOW.DE", "VOW3.DE") for r in _pool_with_vow_pair))


# ======================================================================
# CHECK 13: the fcf_valuation_engine.py docstring correction - the
# fixed text says "overstate"/"overstating", the old wrong word
# ("understate") is no longer anywhere in this specific docstring.
# ======================================================================
_doc = inspect.getsource(fve.dcf_intrinsic_value)
check("fcf_valuation_engine.dcf_intrinsic_value()'s own docstring now says the share-count "
      "correction OVERSTATES (not understates) per-share value when skipped - matches the "
      "real symptom (VOW3.DE's own stored IV far above its price) - the old wrong sentence "
      "is gone (only survives as a disclosed quote inside this same correction's own note)",
      "OVERSTATING per-share value for the affected names" in _doc
      and "understating per-share value for the affected names" not in _doc)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
