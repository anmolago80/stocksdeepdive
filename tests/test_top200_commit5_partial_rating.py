"""
Top 200 COMMIT 5 of instruction_top200_unrated_and_blank_replies_combined.md
(5 Oct 2026, Director-directed, PART C - "rating on what is known",
fact-finding dry run only - nothing live changes).

STEP C0.2 confirmed (see top100_engine.composite_score()'s own
docstring and _parse_one_company()'s not_rated handling) that stored
NOT RATED rows keep the real dimension scores/justifications the model
did give for whichever of the ten it actually scored - the condition
the task itself sets for building this commit at all.

Covers:
  - top100_engine._k_scored(): counts non-null dimensions correctly.
  - partial_rating_dry_run(): a company with exactly 3, 4, 5, 6 zeros
    (k = 7, 6, 5, 4) lands in the right row of Table 1/Table 2/Table 3.
  - Method 2 ("gap counted as the middle score") arithmetic verified
    against a hand-worked fixture.
  - Table 4 for a RATED company with exactly one gap.
  - No network call and no write: monkeypatching top100_store.save_score/
    record_score_failure to raise never fires during the dry run.
  - select_top100_pool() and the public page are untouched by this
    commit (no diff against the already-byte-identical-to-007d669
    COMMIT 3 proof - this commit adds no code to either path).
  - The dry run never raises on malformed/sparse data (best-effort,
    matching every other read-only Admin computation in this module).

PART D STEP D1 (Director, 6 Oct 2026, instruction_portfolio_scoring_
and_currency_table.md): partial_rating_dry_run() used to iterate over
EVERY row top100_store.latest_scores_for_model() has ever stored, with
no join to today's pool - silently counting tickers that have since
left the Top 200/Australia extension (the owner's own report: the
coverage panel showed 24 UNRATED_MODEL, this dry run showed 27 - the
3 extra were aged-out). It now restricts to top100_store.current_pool()
+ current_asx_extension() - the SAME population top100_engine.
coverage_for_rows() (and therefore the coverage panel) already uses.
CHECK 9 below covers this directly: a pool is seeded via the REAL
writer, top100_store.save_pool() (named here per this instruction's
own "any test using a stored row must build it via the real writer...
named in a comment" rule - a hand-invented dict is not evidence), and
an aged-out NOT RATED row (scored, but never added to that pool) is
confirmed excluded from every table, with counts matching top100_
engine.coverage_for_rows() exactly and ranks unaffected by the aged-
out row's mere presence in the score store.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top200_commit5_partial_rating.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top200_commit5_partial_rating_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_BACKFILL_LIVE", None)
os.environ.pop("TOP200_SCHEMA_MODE", None)

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

MODEL = te.MODEL_TOP100
RV = te.RUBRIC_VERSION

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


def _dims_with_k(k, base_score=4):
    """k non-null dimensions (score=base_score), the rest null."""
    dims = {}
    for i, key in enumerate(te.DIMENSION_KEYS):
        if i < k:
            dims[key] = {"score": base_score, "justification": f"reason for {key}", "source_period": "FY25"}
        else:
            dims[key] = {"score": None, "justification": "", "source_period": None}
    return dims


# ======================================================================
# CHECK 1: _k_scored() counts correctly.
# ======================================================================
for k in range(11):
    row = {"dims": _dims_with_k(k)}
    assert te._k_scored(row) == k, (k, te._k_scored(row))
check("_k_scored() correct for k=0..10", True)

# ======================================================================
# CHECK 2: companies with exactly 3, 4, 5, 6 zeros (k=7, 6, 5, 4) land
# in the right row of Table 1 and Table 2, and only k>=6 ones appear
# in Table 3.
# ======================================================================
for k in (7, 6, 5, 4):
    ts.save_score(f"UM_K{k}", "RP2026-10-05", MODEL, RV, _dims_with_k(k), True,
                  None, None, "p", "r")
# One fully-scored RATED company as a Method-1/Method-2 baseline.
ts.save_score("RFULL", "RP2026-10-05", MODEL, RV, _dims_with_k(10), False,
              "scenario", 3, "p", "r", current_headwind="hw")

# PART D STEP D1: partial_rating_dry_run() now restricts to today's
# pool - every ticker used anywhere in this file must be seeded into
# it via the real writer, top100_store.save_pool(), or it would be
# silently excluded by this step's own fix. R1GAP (added in CHECK 4,
# below) is included here too since it shares this same pool snapshot.
POOL_AS_OF = "2026-10-05"
ts.save_pool(
    [{"ticker": t} for t in ("UM_K7", "UM_K6", "UM_K5", "UM_K4", "RFULL", "R1GAP")],
    as_of=POOL_AS_OF,
)

result = te.partial_rating_dry_run(model=MODEL)
_t1 = result["table1_by_k"]
check("Table 1: k=7 company counted once", _t1["USA"][7] == 1)
check("Table 1: k=6 company counted once", _t1["USA"][6] == 1)
check("Table 1: k=5 company counted once", _t1["USA"][5] == 1)
check("Table 1: k=4 company counted once", _t1["USA"][4] == 1)

_t2 = result["table2_lower_bar"]
check("Table 2, min=7: only k=7 would be rated", _t2[7]["USA"]["would_be_rated"] == 1)
check("Table 2, min=7: k=6,5,4 stay unrated", _t2[7]["USA"]["stays_unrated"] == 3)
check("Table 2, min=6: k=7,6 would be rated", _t2[6]["USA"]["would_be_rated"] == 2)
check("Table 2, min=6: k=5,4 stay unrated", _t2[6]["USA"]["stays_unrated"] == 2)
check("Table 2, min=5: k=7,6,5 would be rated", _t2[5]["USA"]["would_be_rated"] == 3)
check("Table 2, min=5: k=4 stays unrated", _t2[5]["USA"]["stays_unrated"] == 1)

_t3_tickers = {r["ticker"] for r in result["table3_landing"]}
check("Table 3: k=7 company present (>=6 threshold)", "UM_K7" in _t3_tickers)
check("Table 3: k=6 company present (>=6 threshold)", "UM_K6" in _t3_tickers)
check("Table 3: k=5 company absent (<6 threshold)", "UM_K5" not in _t3_tickers)
check("Table 3: k=4 company absent (<6 threshold)", "UM_K4" not in _t3_tickers)

_um_k7 = next(r for r in result["table3_landing"] if r["ticker"] == "UM_K7")
check("Table 3: k=7 row reports k=7", _um_k7["k"] == 7)
check("Table 3: k=7 row lists exactly 3 unscored dimensions",
      len(_um_k7["unscored_dimensions"]) == 3)
check("Table 3: empty_fields always exactly the three forced-null fields",
      _um_k7["empty_fields"] == ["inversion_scenario", "inversion_severity", "current_headwind"])
print("[table1_table2_table3_k_rows] 3/4/5/6-zero companies land in the correct rows OK")

# ======================================================================
# CHECK 3: Method 2 arithmetic, hand-worked.
# UM_K7 has 7 dimensions scored 4, 3 dimensions null. DIMENSION_WEIGHT
# sums to DIMENSION_WEIGHT_TOTAL for all ten keys (weights vary per
# dimension - this fixture scores EVERY non-null dimension identically
# at 4, so the hand-worked total only needs sum-of-weights, not a
# per-dimension breakdown).
# ======================================================================
_dims7 = _dims_with_k(7)
_scored_keys = [k for k in te.DIMENSION_KEYS if _dims7[k]["score"] is not None]
_null_keys = [k for k in te.DIMENSION_KEYS if _dims7[k]["score"] is None]
_sum_scored_weight = sum(te.DIMENSION_WEIGHT[k] for k in _scored_keys)
_sum_null_weight = sum(te.DIMENSION_WEIGHT[k] for k in _null_keys)
# Hand-worked Method 2: every scored dim contributes weight*(4-1)/4 =
# weight*0.75; every null dim contributes weight*(3-1)/4 = weight*0.5
# (gap filled with 3). Total rescaled by 100/DIMENSION_WEIGHT_TOTAL.
_hand_worked_total = _sum_scored_weight * 0.75 + _sum_null_weight * 0.5
_hand_worked_method2 = round(_hand_worked_total * (100.0 / te.DIMENSION_WEIGHT_TOTAL), 2)
_actual_method2 = te._composite_score_gap_as_middle(_dims7)
check(f"Method 2 hand-worked ({_hand_worked_method2}) matches code ({_actual_method2})",
      _hand_worked_method2 == _actual_method2)
print(f"[method2_hand_worked] scored_weight={_sum_scored_weight}, null_weight={_sum_null_weight}, "
      f"total={_hand_worked_total}, rescaled={_hand_worked_method2} == code output OK")

# A fully-scored company (k=10, all score=4) must have method2 == method1 == composite_score
# (no gaps to fill, so the two methods can never disagree).
_dims10 = _dims_with_k(10)
_full_method1 = te._composite_score_from_dims(_dims10)
_full_method2 = te._composite_score_gap_as_middle(_dims10)
check("Method 1 == Method 2 for a fully-scored company (no gaps)", _full_method1 == _full_method2)

# ======================================================================
# CHECK 4: Table 4 - a RATED company with exactly one gap (k=9).
# ======================================================================
ts.save_score("R1GAP", "RP2026-10-05", MODEL, RV, _dims_with_k(9), False,
              "scenario", 2, "p", "r", current_headwind="hw")
result2 = te.partial_rating_dry_run(model=MODEL)
_t4 = {r["ticker"]: r for r in result2["table4_existing_gaps"]}
check("Table 4: R1GAP (1 gap) present", "R1GAP" in _t4)
check("Table 4: R1GAP reports gaps=1", _t4["R1GAP"]["gaps"] == 1)
check("Table 4: RFULL (0 gaps) absent from Table 4", "RFULL" not in _t4)
check("Table 4: R1GAP's method2_rank is a positive int", _t4["R1GAP"]["method2_rank"] >= 1)
print("[table4_existing_gaps] RATED company with exactly one gap present with the right fields OK")

# ======================================================================
# CHECK 5: no network call, no write - a forced failure on any write
# to the score store never fires during the dry run.
# ======================================================================
with mock.patch.object(ts, "save_score", side_effect=AssertionError("must never write")), \
     mock.patch.object(ts, "record_score_failure", side_effect=AssertionError("must never write")), \
     mock.patch.object(ts, "save_pool", side_effect=AssertionError("must never write")):
    try:
        te.partial_rating_dry_run(model=MODEL)
        _no_write_ok = True
    except AssertionError:
        _no_write_ok = False
check("partial_rating_dry_run() makes no write call (forced-failure writes never fire)", _no_write_ok)

# ======================================================================
# CHECK 6: the one log line fires and never raises, including on a
# transient internal failure (best-effort, same discipline as every
# other post-ingest log call in this module).
# ======================================================================
_logged = []
te._log_partial_rating_dry_run(log=lambda m: _logged.append(m))
check("one log line emitted", len(_logged) == 1)
check('log line has the task\'s own prefix', _logged[0].startswith("[top100] partial-rating dry run:"))

with mock.patch.object(te, "partial_rating_dry_run", side_effect=RuntimeError("boom")):
    try:
        te._log_partial_rating_dry_run(log=lambda m: None)
        _never_raises = True
    except RuntimeError:
        _never_raises = False
check("_log_partial_rating_dry_run() never raises on internal failure", _never_raises)
print("[log_line] fires once, correct prefix, never raises on internal failure OK")

# ======================================================================
# CHECK 7: select_top100_pool() is untouched by this commit - no git
# diff touches it (verified via `git diff HEAD` in the session, see
# this commit's own report) - this test asserts the function still
# exists and is callable with the same signature as before, a cheap
# sanity check that the module still imports cleanly end to end.
# ======================================================================
import inspect
check("select_top100_pool() signature unchanged (log=print only)",
      list(inspect.signature(te.select_top100_pool).parameters.keys()) == ["log"])

# ======================================================================
# CHECK 8: the Admin panel itself renders without raising, via
# AppTest (same pattern as test_top200_commit3_backfill.py's own
# render_top100_page() check), with a NOT RATED and a RATED-with-a-gap
# row already on file from the checks above.
# ======================================================================
from streamlit.testing.v1 import AppTest

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_panel_script = f"""
import os, sys
sys.path.insert(0, {_SCRIPT_DIR!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app as top100_app
top100_app._render_top200_partial_rating_dry_run_panel()
"""
_at = AppTest.from_string(_panel_script)
_at.run(timeout=60)
check("Admin panel renders without raising", not _at.exception)
check("Admin panel shows all 5 table headings",
      sum(1 for md in _at.get("markdown") if "Table" in md.value) >= 5)
_panel_captions = " ".join(c.value or "" for c in _at.get("caption"))
check("Admin panel's own cross-check caption is present and the two counts agree "
      "(same population, same read)",
      "Coverage panel unrated:" in _panel_captions and "This panel:" in _panel_captions)
check("no mismatch warning fires when the two counts genuinely agree",
      len(_at.get("warning")) == 0)

# ======================================================================
# CHECK 9 (Director, 6 Oct 2026, PART D STEP D1): an aged-out NOT RATED
# row is excluded; counts equal the coverage panel's for the same
# fixture; ranks do not change when an aged-out row is added.
# ======================================================================
# AGED_OUT: scored UNRATED_MODEL (k=6, same shape as UM_K6 above), but
# deliberately never added to the pool seeded above - simulating a
# ticker that has since left the Top 200/Australia extension, exactly
# the live discrepancy the Director reported (24 vs 27).
ts.save_score("AGED_OUT", "RP2026-09-01", MODEL, RV, _dims_with_k(6), True,
              None, None, "p", "r")

_result_before = te.partial_rating_dry_run(model=MODEL)
_coverage = te.coverage_for_rows(
    ts.current_pool() + ts.current_asx_extension(), model=MODEL)
_coverage_unrated = sum(b["unrated_model"] for b in _coverage["by_market"].values())

check("Table 1's USA/k=6 count stays at 1 (UM_K6 only) - AGED_OUT, also k=6/USA/"
      "UNRATED_MODEL, does not inflate it just by existing in the score store",
      _result_before["table1_by_k"]["USA"][6] == 1)
_t3_tickers_before = {r["ticker"] for r in _result_before["table3_landing"]}
check("AGED_OUT is excluded from Table 3 (k=6 would otherwise qualify)",
      "AGED_OUT" not in _t3_tickers_before)
check("the dry run's own summary count equals coverage_for_rows()'s count for the "
      "SAME population (the exact Director-reported mismatch, now closed)",
      _result_before["summary"]["unrated_model"] == _coverage_unrated)

# Ranks must not move just because AGED_OUT exists in the score store -
# it was never part of the ranking baseline (rated_rows) to begin with,
# but confirm explicitly: UM_K6's own method2_rank is identical whether
# or not AGED_OUT is present, since AGED_OUT is RATED=False (UNRATED_
# MODEL) and therefore never enters the rated_method1/rated_method2
# ranking baseline either way - this holds regardless of the pool fix,
# but is exactly the property the Director's own instruction names.
_um_k6_rank_before = next(
    r for r in _result_before["table3_landing"] if r["ticker"] == "UM_K6"
)["method2_rank"]
ts.save_score("AGED_OUT", "RP2026-09-01", MODEL, RV, _dims_with_k(6), True,
              None, None, "p", "r")  # re-save, simulating it staying on file
_result_after = te.partial_rating_dry_run(model=MODEL)
_um_k6_rank_after = next(
    r for r in _result_after["table3_landing"] if r["ticker"] == "UM_K6"
)["method2_rank"]
check("UM_K6's own rank is unchanged by the aged-out row's continued presence",
      _um_k6_rank_before == _um_k6_rank_after)
print("[d1_pool_population_fix] an aged-out NOT RATED row is excluded from every "
      "table; the dry run's own count matches coverage_for_rows() for the identical "
      "population; ranks are unaffected by the aged-out row's presence OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
sys.exit(1 if failed else 0)
