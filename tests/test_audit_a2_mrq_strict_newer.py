"""Audit fix A2 / Fable finding T3 (10 Oct 2026, instruction_combined_
10oct.md PART A): _unscored_tickers()'s "new_results" trigger and
_failure_reentry_reason()'s own mirror of it must fire only when the
pool row's most_recent_quarter is STRICTLY NEWER than what's already
on record (row_mrq > score_mrq / failure_mrq), never merely DIFFERENT
(row_mrq != score_mrq). most_recent_quarter is an ISO date string
(YYYY-MM-DD, see nightly_scan.py's own
datetime.fromtimestamp(...).date().isoformat()), so lexicographic
string comparison is a correct date comparison.

Fable's scenario, reproduced here: a ticker was already scored
against most_recent_quarter "2026-06-30". If the live pool data's own
most_recent_quarter now reads "2026-03-31" - OLDER than what's
already scored, e.g. a transient data-source regression/stale read,
never a real new reporting period - the old `!=` check wrongly
treated that as "new_results" and triggered an unnecessary, incorrect
rescore. "2026-09-30" (genuinely newer) must still trigger it.

Run: python3 tests/test_audit_a2_mrq_strict_newer.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_audit_a2_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

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


def _pool_row(ticker, most_recent_quarter):
    return {"ticker": ticker, "company_name": f"{ticker} Co", "most_recent_quarter": most_recent_quarter}


def _dims():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


def _save_score(ticker, most_recent_quarter):
    ts.save_score(
        ticker=ticker, quarter="D2026-06-30", model=te.MODEL_TOP100,
        rubric_version=te.RUBRIC_VERSION, dims=_dims(), not_rated=False,
        most_recent_quarter=most_recent_quarter,
        inversion_scenario="", inversion_severity=0, current_headwind="",
        market_structure="", market_structure_comment="",
        one_foot_hurdle="", one_foot_comment="",
        munger_quality="", munger_comment="", big_wave="", big_wave_comment="",
        matched_by=None, prompt="{}", raw_response="{}", schema_mode="legacy",
    )


# ======================================================================
# CHECK A: the SCORE-ROW path (_unscored_tickers()'s main loop).
# Each ticker is seeded past NEWCOMER_PERSISTENCE_NIGHTS so persistence
# gating never interferes with these assertions.
# ======================================================================
ts.seed_pool_presence({"OLDMRQ": 3, "NEWMRQ": 3}, "2026-09-29")

_save_score("OLDMRQ", most_recent_quarter="2026-06-30")
_pool_older = [_pool_row("OLDMRQ", most_recent_quarter="2026-03-31")]
_reasons_older = {row["ticker"]: reason for row, reason in te._unscored_tickers(_pool_older, te.MODEL_TOP100)}
check("score 2026-06-30, pool row 2026-03-31 (OLDER) -> no re-score at all "
      "(not merely 'not new_results' - the row is absent from the unscored list)",
      "OLDMRQ" not in _reasons_older)

_save_score("NEWMRQ", most_recent_quarter="2026-06-30")
_pool_newer = [_pool_row("NEWMRQ", most_recent_quarter="2026-09-30")]
_reasons_newer = {row["ticker"]: reason for row, reason in te._unscored_tickers(_pool_newer, te.MODEL_TOP100)}
check("score 2026-06-30, pool row 2026-09-30 (NEWER) -> re-scored as 'new_results'",
      _reasons_newer.get("NEWMRQ") == "new_results")

# Equal values must never trigger either way (sanity check, both old
# and new logic agree here).
ts.seed_pool_presence({"SAMEMRQ": 3}, "2026-09-29")
_save_score("SAMEMRQ", most_recent_quarter="2026-06-30")
_pool_same = [_pool_row("SAMEMRQ", most_recent_quarter="2026-06-30")]
_reasons_same = {row["ticker"]: reason for row, reason in te._unscored_tickers(_pool_same, te.MODEL_TOP100)}
check("score 2026-06-30, pool row 2026-06-30 (SAME) -> no re-score",
      "SAMEMRQ" not in _reasons_same)

# ======================================================================
# CHECK B: the FAILURE-REENTRY path (_failure_reentry_reason(), via
# _unscored_tickers() for an EXHAUSTED (3-strike) failure).
# ======================================================================
for _ in range(te.TOP100_FAILURE_MAX_ATTEMPTS):
    ts.record_score_failure("OLDMRQFAIL", te.MODEL_TOP100, te.RUBRIC_VERSION, "weird_error",
                             most_recent_quarter="2026-06-30")
ts.seed_pool_presence({"OLDMRQFAIL": 3}, "2026-09-29")
_pool_fail_older = [_pool_row("OLDMRQFAIL", most_recent_quarter="2026-03-31")]
_reasons_fail_older = {row["ticker"]: reason for row, reason in te._unscored_tickers(_pool_fail_older, te.MODEL_TOP100)}
check("exhausted failure's own most_recent_quarter 2026-06-30, pool row 2026-03-31 "
      "(OLDER) -> stays permanently skipped (no re-entry)",
      "OLDMRQFAIL" not in _reasons_fail_older)

for _ in range(te.TOP100_FAILURE_MAX_ATTEMPTS):
    ts.record_score_failure("NEWMRQFAIL", te.MODEL_TOP100, te.RUBRIC_VERSION, "weird_error",
                             most_recent_quarter="2026-06-30")
ts.seed_pool_presence({"NEWMRQFAIL": 3}, "2026-09-29")
_pool_fail_newer = [_pool_row("NEWMRQFAIL", most_recent_quarter="2026-09-30")]
_reasons_fail_newer = {row["ticker"]: reason for row, reason in te._unscored_tickers(_pool_fail_newer, te.MODEL_TOP100)}
check("exhausted failure's own most_recent_quarter 2026-06-30, pool row 2026-09-30 "
      "(NEWER) -> re-enters as 'new_results'",
      _reasons_fail_newer.get("NEWMRQFAIL") == "new_results")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
