"""
Newcomer persistence filter (owner decision, 1 Oct 2026, per
instruction_top100_cost_v6.md's own Push 2 point 8).

A ticker entering the Top 100 pool for the first time (no score under
ANY rubric version, and not in the pool the previous night) is not
submitted for AI scoring until it has appeared in the pool on
NEWCOMER_PERSISTENCE_NIGHTS (3) CONSECUTIVE nightly selections. Applies
ONLY to never-scored newcomers - results-driven re-scores, the
RESCORE_MAX_AGE_DAYS safety net, rubric-version re-scores (including
this v6 bump's own full re-score) and the previous-rubric fallback are
all unaffected, since a ticker that already carries a score under any
rubric is exempt from the gate entirely (top100_store.
latest_score_previous_rubric() returning non-None).

This file covers (instruction's own Push 2 point 8 test list):
  - newcomer night 1 and night 2 -> deferred (not in _unscored_
    tickers()'s own result)
  - night 3 -> submitted (reason "new_or_rubric")
  - drop-out on night 2, reappearing later -> counter resets to 1
  - a SCORED ticker with a quarter rollover (results-driven re-score)
    is submitted on night 1 regardless of its own presence streak
  - seeding (seed_pool_presence_for_v6_once()) sets a ticker with
    prior-rubric history to 3, a true newcomer to 1
  - the nightly log line's new 3-bucket format and the admin-panel
    deferred count (_count_persistence_deferred())

Run: python3 tests/test_top100_persistence.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_persistence_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _pool_row(ticker, most_recent_quarter=None):
    return {"ticker": ticker, "company_name": f"{ticker} Co", "most_recent_quarter": most_recent_quarter,
            "sector": "Technology"}


# ======================================================================
# CHECK 1: a brand-new ticker (no pool_presence row, no score under any
# rubric) is deferred on night 1 and night 2, then submitted on night 3.
# ======================================================================
pool1 = [_pool_row("NEWCO")]

ts.update_pool_presence(["NEWCO"], "2026-10-01")
assert ts.pool_presence_map(["NEWCO"]) == {"NEWCO": 1}
unscored_n1 = te._unscored_tickers(pool1, te.MODEL_TOP100)
assert unscored_n1 == [], f"night 1 (consecutive=1): NEWCO must be deferred, got {unscored_n1}"
print("[night1_deferred] a brand-new ticker on night 1 (consecutive_nights=1) is deferred OK")

ts.update_pool_presence(["NEWCO"], "2026-10-02")
assert ts.pool_presence_map(["NEWCO"]) == {"NEWCO": 2}
unscored_n2 = te._unscored_tickers(pool1, te.MODEL_TOP100)
assert unscored_n2 == [], f"night 2 (consecutive=2): NEWCO must still be deferred, got {unscored_n2}"
print("[night2_deferred] the same ticker on night 2 (consecutive_nights=2) is still deferred OK")

ts.update_pool_presence(["NEWCO"], "2026-10-03")
assert ts.pool_presence_map(["NEWCO"]) == {"NEWCO": 3}
unscored_n3 = te._unscored_tickers(pool1, te.MODEL_TOP100)
unscored_n3_tickers = {row["ticker"]: reason for row, reason in unscored_n3}
assert unscored_n3_tickers.get("NEWCO") == "new_or_rubric", (
    f"night 3 (consecutive=3): NEWCO must now be submitted as 'new_or_rubric', got {unscored_n3}"
)
print("[night3_submitted] on night 3 (consecutive_nights=3) the ticker is finally "
      "submitted, reason='new_or_rubric' OK")


# ======================================================================
# CHECK 2: drop-out on night 2 (the ticker isn't in that night's pool
# at all), then reappears later -> the streak RESETS to 1, not 3.
# ======================================================================
ts.update_pool_presence(["DROPCO"], "2026-10-01")
assert ts.pool_presence_map(["DROPCO"]) == {"DROPCO": 1}
# Night 2: DROPCO is NOT in the pool (dropped out) - update_pool_
# presence() is simply never called with it for that night.
# Night 3: DROPCO reappears - the gap must reset the streak to 1, not
# continue at 2.
ts.update_pool_presence(["DROPCO"], "2026-10-03")
assert ts.pool_presence_map(["DROPCO"]) == {"DROPCO": 1}, (
    "a ticker that dropped out for a night and reappeared must reset to 1, not continue its old streak"
)
print("[dropout_resets] a ticker that drops out of the pool for a night and reappears "
      "later resets its consecutive-night streak to 1 OK")

# Confirm the reset ticker is deferred again (same as a fresh newcomer).
pool_dropco = [_pool_row("DROPCO")]
unscored_dropco = te._unscored_tickers(pool_dropco, te.MODEL_TOP100)
assert unscored_dropco == [], "a reset-to-1 ticker must be deferred again, exactly like a fresh newcomer"
print("[dropout_deferred_again] after the reset, the ticker is deferred again, same as night 1 OK")


# ======================================================================
# CHECK 3: a ticker that already carries a score under ANY rubric
# (results-driven re-score via a quarter rollover) is submitted on
# night 1 regardless of its own presence streak - the gate applies
# ONLY to never-scored newcomers.
# ======================================================================
ts.save_score(
    ticker="RESCORECO", quarter="RP2026Q2", model=te.MODEL_TOP100, rubric_version="v5",
    dims={k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS},
    not_rated=False, inversion_scenario=None, inversion_severity=None, prompt="{}", raw_response="{}",
)
# RESCORECO has never been seen by update_pool_presence() at all under
# the CURRENT rubric (consecutive_nights would read as 0/absent), yet
# it must still be submitted immediately - it carries a v5 score.
pool_rescoreco = [_pool_row("RESCORECO", most_recent_quarter="2026-09-30")]
unscored_rescore = te._unscored_tickers(pool_rescoreco, te.MODEL_TOP100)
unscored_rescore_tickers = {row["ticker"]: reason for row, reason in unscored_rescore}
assert unscored_rescore_tickers.get("RESCORECO") == "new_or_rubric", (
    f"a ticker with prior-rubric history must be submitted on night 1 regardless of its own "
    f"presence streak (it's a rubric-bump entrant, not a newcomer), got {unscored_rescore}"
)
print("[rubric_bump_exempt] a ticker that already carries a v5 score is submitted immediately "
      "under the v6 bump, regardless of its own pool_presence streak (not a true newcomer) OK")


# ======================================================================
# CHECK 4: seeding (seed_pool_presence_for_v6_once()) sets a ticker
# with prior-rubric history to 3, a true newcomer to 1.
# ======================================================================
ts.save_pool(
    [
        {"ticker": "SEEDOLD", "company_name": "Seed Old Co", "universe": "S&P 500",
         "value_score": 80.0, "mos_pct": 40.0, "price": 100.0, "intrinsic_value": 150.0,
         "currency": "USD", "pool_selection_rule": "freshest_v1"},
        {"ticker": "SEEDNEW", "company_name": "Seed New Co", "universe": "S&P 500",
         "value_score": 70.0, "mos_pct": 30.0, "price": 50.0, "intrinsic_value": 70.0,
         "currency": "USD", "pool_selection_rule": "freshest_v1"},
    ],
    as_of="2026-10-05",
)
ts.save_score(
    ticker="SEEDOLD", quarter="RP2026Q2", model=te.MODEL_TOP100, rubric_version="v5",
    dims={k: {"score": 3, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS},
    not_rated=False, inversion_scenario=None, inversion_severity=None, prompt="{}", raw_response="{}",
)
# SEEDNEW has no score under any rubric at all - a true newcomer.
te.seed_pool_presence_for_v6_once(log=lambda *a, **k: None)
seeded = ts.pool_presence_map(["SEEDOLD", "SEEDNEW"])
assert seeded["SEEDOLD"] == 3, f"a ticker with prior-rubric history must seed to 3, got {seeded['SEEDOLD']}"
assert seeded["SEEDNEW"] == 1, f"a true newcomer must seed to 1, got {seeded['SEEDNEW']}"
print(f"[seeding] seed_pool_presence_for_v6_once(): prior-rubric ticker seeded to 3, "
      f"true newcomer seeded to 1 OK")

# The marker file must prevent a second seeding run from touching
# anything (idempotent, one-off).
marker = te._pool_presence_v6_seed_marker_path()
assert os.path.exists(marker), "seeding must write its own marker file"
# seed_pool_presence_for_v6_once() seeds using the real "today" (not a
# test-chosen date), so advance SEEDNEW's streak from THAT same date.
_seed_today = datetime.now(timezone.utc).date()
_next_night = (_seed_today + timedelta(days=1)).isoformat()
ts.update_pool_presence(["SEEDNEW"], _next_night)  # advance SEEDNEW's real streak
te.seed_pool_presence_for_v6_once(log=lambda *a, **k: None)  # must be a no-op now
assert ts.pool_presence_map(["SEEDNEW"])["SEEDNEW"] == 2, (
    "a second seeding call after the marker exists must be a no-op - it must not reset "
    "a ticker's real, already-advancing streak back to its seed value"
)
print("[seeding_once_only] the marker-guarded seeding never re-runs once its own marker exists OK")


# ======================================================================
# CHECK 5: the nightly log line's new 3-bucket format (re-score N,
# newcomers N, deferred (persistence) N) and the admin-panel deferred
# count (_count_persistence_deferred()).
# ======================================================================
log_pool = [
    _pool_row("LOGRESCORE", most_recent_quarter="2026-09-30"),  # will be a re-score (new_results)
    _pool_row("LOGNEWCOMER"),  # true newcomer, consecutive_nights=3 -> submitted
    _pool_row("LOGDEFERRED"),  # true newcomer, consecutive_nights=1 -> deferred
]
ts.save_score(
    ticker="LOGRESCORE", quarter="RP2026-06-30", model=te.MODEL_TOP100, rubric_version=te.RUBRIC_VERSION,
    dims={k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS},
    not_rated=False, inversion_scenario=None, inversion_severity=None, prompt="{}", raw_response="{}",
    most_recent_quarter="2026-06-30",
)
ts.seed_pool_presence({"LOGNEWCOMER": 3}, "2026-10-06")
ts.seed_pool_presence({"LOGDEFERRED": 1}, "2026-10-06")

logs = []
import unittest.mock as mock
with mock.patch("top100_engine.top100_store.get_batch_state", return_value=None), \
     mock.patch("top100_engine.top100_store.get_daily_submission_state", return_value={"batches": 0, "entrants": 0}), \
     mock.patch("top100_engine.top100_store.record_daily_submission"), \
     mock.patch("top100_engine.top100_store.save_batch_state"), \
     mock.patch("anthropic.Anthropic") as MockClient, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw):
    instance = MockClient.return_value
    instance.messages.batches.create.return_value = mock.Mock(id="msgbatch_log_test")
    te.submit_nightly_batch(pool=log_pool, log=logs.append)

score_line = next((l for l in logs if "to score tonight" in l), None)
assert score_line is not None, logs
assert "re-score 1" in score_line, score_line
assert "newcomers 1" in score_line, score_line
assert "deferred (persistence) 1" in score_line, score_line
print(f"[log_line_format] nightly log line uses the new 3-bucket format: {score_line!r} OK")

deferred_count = te._count_persistence_deferred(log_pool, te.MODEL_TOP100)
assert deferred_count == 1, deferred_count
print(f"[admin_deferred_count] _count_persistence_deferred() (the admin panel's own read) "
      f"reports {deferred_count} OK")


print("\nALL TOP 100 NEWCOMER PERSISTENCE FILTER TESTS PASSED")
