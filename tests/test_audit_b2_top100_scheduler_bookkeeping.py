"""Audit fix B2 / Fable findings T5+T6+T10+T11 (10 Oct 2026,
instruction_combined_10oct.md PART B): scheduler bookkeeping for the
Top 100 nightly job.

T5 - scheduler_engine._loop()'s own Top 100 trigger block persisted
last_top100_date (and cleared top100_pending_for) BEFORE _run_top100()
actually ran. A crashed or hard-timed-out job was marked "done" for
that scan night and never retried - exactly the failure mode the
backup/volume_check jobs already learned from (see those jobs' own
comments). Extracted into _run_top100_job_if_due() (no behaviour
change from the extraction itself) so this is directly testable.

T6 - the new attempts cap (top100_attempts), and the scan_day now
threaded into _run_top100()->top100_engine.run_nightly()->
select_top100_pool(), are both keyed by the SCAN NIGHT this job is
for (state["top100_pending_for"]) - never by a fresh wall-clock read
- so a job attempted or still running across a midnight boundary
still counts against, and credits pool_presence for, the night it's
actually working for (mirrors nightly_scan.run_universe_scan()'s own
already-fixed "Commit H" run_night parameter).

T10/T11 - TOP100_FAILURE_RETRY_HOURS is now 20 (was 24).

Run: python3 tests/test_audit_b2_top100_scheduler_bookkeeping.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="audit_b2_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scheduler_engine as se
import top100_engine as te
import top100_store as ts

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


def _dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


CFG = {"top100_hour": 23}


# ======================================================================
# CHECK T11: the constant itself.
# ======================================================================
check("TOP100_FAILURE_RETRY_HOURS is now 20 (was 24)", te.TOP100_FAILURE_RETRY_HOURS == 20)


# ======================================================================
# CHECK T5a: a FAILING Top 100 job does NOT mark last_top100_date/
# clear top100_pending_for - it stays retryable.
# ======================================================================
se._save_state({})
se._mark_top100_pending("2026-10-09", _dt(2026, 10, 9, 22, 30), "completed")
_now1 = _dt(2026, 10, 9, 23, 0)

_logs1 = []
with mock.patch.object(se, "_run_top100", side_effect=RuntimeError("boom")):
    se._run_top100_job_if_due(se._load_state(), _now1, CFG, _logs1.append)

_state_after_fail = se._load_state()
check("a failing job does NOT persist last_top100_date",
      _state_after_fail.get("last_top100_date") != "2026-10-09")
check("a failing job does NOT clear top100_pending_for - stays retryable",
      _state_after_fail.get("top100_pending_for") == "2026-10-09")
check("the attempt was still counted (1/cap) even though it failed",
      _state_after_fail.get("top100_attempts", {}).get("2026-10-09") == 1)
check("the job lock was released despite the failure (no stuck lock)",
      se._acquire_job_lock("top100", lambda *a: None) is True)
se._release_job_lock("top100")


# ======================================================================
# CHECK T5b: the SAME scan night retries on the next tick (still due),
# and a SUCCESSFUL run this time DOES persist last_top100_date and
# clears top100_pending_for.
# ======================================================================
_logs2 = []
with mock.patch.object(se, "_run_top100") as _m_run:
    se._run_top100_job_if_due(se._load_state(), _now1, CFG, _logs2.append)

_state_after_success = se._load_state()
check("a successful run DOES persist last_top100_date",
      _state_after_success.get("last_top100_date") == "2026-10-09")
check("a successful run clears top100_pending_for",
      _state_after_success.get("top100_pending_for") is None)
check("the attempt counter now reads 2/cap for that scan night",
      _state_after_success.get("top100_attempts", {}).get("2026-10-09") == 2)
check("_run_top100 was called with scan_day='2026-10-09' (T6 threading)",
      _m_run.call_args.kwargs.get("scan_day") == "2026-10-09")


# ======================================================================
# CHECK T5c: the attempts cap - once exhausted, the job is skipped
# entirely (never attempted again that scan night).
# ======================================================================
se._save_state({})
se._mark_top100_pending("2026-10-08", _dt(2026, 10, 8, 22, 0), "completed")
_now3 = _dt(2026, 10, 8, 23, 0)
_run_count = {"n": 0}


def _always_fails(lg, scan_day=None):
    _run_count["n"] += 1
    raise RuntimeError("persistently broken")


with mock.patch.object(se, "_run_top100", side_effect=_always_fails):
    for _ in range(se._DAILY_JOB_RETRY_CAP + 2):
        se._run_top100_job_if_due(se._load_state(), _now3, CFG, lambda *a: None)

check(f"a persistently-failing job is attempted exactly "
      f"{se._DAILY_JOB_RETRY_CAP} times (the cap), never more",
      _run_count["n"] == se._DAILY_JOB_RETRY_CAP)
check("top100_pending_for survives exhausting the cap (still 'waiting', "
      "just not retried again today)",
      se._load_state().get("top100_pending_for") == "2026-10-08")


# ======================================================================
# CHECK T6: select_top100_pool()'s own scan_day parameter is used as
# `as_of` for save_pool/update_pool_presence/seed_pool_presence
# INSTEAD OF a fresh wall-clock read - proven by a fixed, obviously-
# not-"now" scan_day showing up as the as_of value top100_store
# actually receives.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

_save_pool_calls = []
with mock.patch.object(ts, "save_pool", side_effect=lambda rows, as_of: _save_pool_calls.append(as_of)), \
     mock.patch.object(te, "_fill_missing_sectors"), \
     mock.patch.object(ts, "update_pool_presence") as _m_update_presence:
    te.select_top100_pool(log=lambda *a: None, scan_day="2019-01-01")

check("select_top100_pool(scan_day='2019-01-01') saves the pool under "
      "that exact scan_day, never a fresh wall-clock date",
      _save_pool_calls and _save_pool_calls[0] == "2019-01-01")
check("update_pool_presence() is credited to the same fixed scan_day",
      _m_update_presence.call_args is not None
      and _m_update_presence.call_args[0][1] == "2019-01-01")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
