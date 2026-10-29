"""
TASK 23 (Director-directed, 29 Oct 2026) - scan lock stolen mid-run ->
two concurrent nightly scans -> Yahoo 429 abort.

EVIDENCE (Railway logs, 28 Oct): 21:01:23 fastapi starts nightly "ASX
200: scanning 200 tickers". 21:04:57 the Streamlit process logs
"[scheduler] reclaimed stale scan lock for 'nightly' (legacy lock
format, no heartbeat/holder fields)" and starts a 2nd ASX 200 scan at
21:05:13. Both hit Yahoo -> 429 -> "ASX 200: ABORTING after 105/200",
breaker skipped the reprice pass, ASX 200 not saved, no retry after
the 45-min cool-down.

ROOT CAUSE (verified in scheduler_engine.py): _refresh_job_lock_
heartbeat() rewrote the live lock with open(path, "w") on every log
line - truncate-then-write, not atomic. A reader's _acquire_job_lock()
racing that exact window got an empty/partial file, _read_lock_
payload() returned None, which was treated as "legacy format" and
reclaimed (os.remove()'d) immediately - stealing a lock its holder was
actively, legitimately refreshing.

Covers:
  1-2. Every lock write is now atomic (_atomic_write_json: temp file +
       os.replace(), same guarantee _save_state() already uses) - both
       in _refresh_job_lock_heartbeat() AND in _acquire_job_lock()'s
       own payload write after the O_CREAT|O_EXCL create. A concurrent
       reader can only ever observe the lock's PREVIOUS complete
       payload or its NEW complete payload, never a truncated one.
  3.   _acquire_job_lock() no longer reclaims an unreadable/None
       payload immediately - it falls back to the lock FILE's own
       mtime, reclaiming only once that's older than _JOB_LOCK_
       HEARTBEAT_STALE_SECONDS (the same bound every other reclaim
       path here already uses), returning False (held) otherwise.
       (a) a reader racing 2000 heartbeat refreshes never sees an
           empty/partial file.
       (b) an unreadable lock with a FRESH mtime is NOT reclaimed.
       (c) an unreadable lock with an OLD mtime IS reclaimed (a
           genuinely abandoned/legacy lock stays recoverable).
       (d) the unchanged, valid-payload reclaim paths (fresh
           heartbeat blocks; stale heartbeat reclaims) still work
           exactly as before - this fix only touches the None-payload
           branch.
  4.   INVESTIGATE ONLY, test-first, per the task's own instruction:
       at 21:09:46 fastapi started "catch-up scan ... [attempt 3/3]"
       ~1 min after it had itself recorded a rate-limit cool-down
       until 21:53:48. A test reproducing that exact single-process
       sequence - the due-scan block itself tripping the breaker mid-
       run - PROVES the real mechanism is simpler than a cross-process
       scheduler_state.json lost-update: _loop() computes
       `_rl_cooldown_active` ONCE per tick, before the due-scan block
       runs, and reuses that SAME stale value to gate the catch-up
       block further down in the SAME tick - even though the due-
       scan's own _run_nightly() call (which can run for minutes) may
       itself have just recorded a brand-new cool-down via
       _record_rate_limit_cooldown(). See CHECK 9's own comment below
       for the full writeup and why no fix is applied here, per the
       task's explicit "report findings; do not change it unless
       proven [to be the two-process state-loss hypothesis]" framing.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task23_scan_lock_atomic_and_cooldown.py
"""
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task23_scan_lock_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scheduler_engine as se

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
# CHECK 1 (item 3a): a reader racing 2000 heartbeat refreshes in a tight
# loop never sees an empty/partial lock file - proves _atomic_write_json
# (temp file + os.replace()) closes the exact race the real incident's
# root cause describes.
# ======================================================================
_race_job = "task23_race"
_race_path = se._lock_path(_race_job)
_got_lock = se._acquire_job_lock(_race_job, lambda m: None)
check("setup: real lock acquired for the race test", _got_lock)

_stop = threading.Event()
_bad_reads = []
_read_count = [0]


def _reader_loop():
    while not _stop.is_set():
        payload = se._read_lock_payload(_race_path)
        _read_count[0] += 1
        if payload is None:
            _bad_reads.append(time.time())


def _writer_loop():
    for _ in range(2000):
        se._refresh_job_lock_heartbeat(_race_job)


_t_reader = threading.Thread(target=_reader_loop)
_t_writer = threading.Thread(target=_writer_loop)
_t_reader.start()
_t_writer.start()
_t_writer.join()
_stop.set()
_t_reader.join()

check(f"no reader ever observed an empty/partial lock file while 2000 heartbeat "
      f"refreshes raced it ({_read_count[0]} reads, {len(_bad_reads)} bad)",
      _bad_reads == [])
se._release_job_lock(_race_job)
print(f"[task23_c1_heartbeat_race] {_read_count[0]} concurrent reads, 0 saw a "
      f"truncated/partial file OK")

# ======================================================================
# CHECK 2 (item 3b): an unreadable (non-JSON) lock file with a FRESH
# mtime is NOT reclaimed - _acquire_job_lock returns False (held), and
# the file's own garbage content is left completely untouched.
# ======================================================================
_fresh_job = "task23_fresh_garbage"
_fresh_path = se._lock_path(_fresh_job)
with open(_fresh_path, "w") as f:
    f.write("pid=12345 started=2026-01-01")  # old plain-text format, mtime = now

_fresh_logs = []
_fresh_result = se._acquire_job_lock(_fresh_job, _fresh_logs.append)
check("FIX: an unreadable lock with a FRESH mtime is NOT reclaimed - "
      "_acquire_job_lock returns False (treated as held)",
      _fresh_result is False)
check("the garbage file's content is left completely untouched - nothing was "
      "removed or overwritten",
      open(_fresh_path).read() == "pid=12345 started=2026-01-01")
check("no reclaim was logged for the fresh-mtime case",
      _fresh_logs == [])
print("[task23_c2_fresh_unreadable_not_reclaimed] fresh-mtime unreadable lock "
      "held, not stolen OK")

# ======================================================================
# CHECK 3 (item 3c): the SAME unreadable lock, but with an OLD mtime
# (older than _JOB_LOCK_HEARTBEAT_STALE_SECONDS), IS reclaimed - a
# genuinely abandoned/legacy-format lock stays recoverable, exactly as
# intended before this fix, just gated by file age now instead of
# firing unconditionally.
# ======================================================================
_old_job = "task23_old_garbage"
_old_path = se._lock_path(_old_job)
with open(_old_path, "w") as f:
    f.write("pid=99999 started=2020-01-01")
_old_mtime = time.time() - (se._JOB_LOCK_HEARTBEAT_STALE_SECONDS + 60)
os.utime(_old_path, (_old_mtime, _old_mtime))

_old_logs = []
_old_result = se._acquire_job_lock(_old_job, _old_logs.append)
check("FIX: the SAME unreadable lock, with an mtime older than "
      "_JOB_LOCK_HEARTBEAT_STALE_SECONDS, IS reclaimed - "
      "_acquire_job_lock returns True",
      _old_result is True)
check("a reclaim was logged, same text as the real incident's own log line",
      any("legacy lock format, no heartbeat/holder fields" in m for m in _old_logs))
_old_payload = se._read_lock_payload(_old_path)
check("the lock file now holds a real, valid, freshly-written payload for THIS "
      "process (boot_id matches)",
      _old_payload is not None and _old_payload.get("boot_id") == se._BOOT_ID)
se._release_job_lock(_old_job)
print("[task23_c3_old_unreadable_reclaimed] stale-mtime unreadable (legacy) "
      "lock still reclaimable OK")

# ======================================================================
# CHECK 4-5 (item 3d): the unchanged, VALID-payload reclaim paths still
# work exactly as before this fix - a fresh heartbeat (same or
# different boot_id) blocks acquisition; a stale heartbeat reclaims.
# This fix only touches the None-payload branch above.
# ======================================================================
_valid_job = "task23_valid_fresh"
assert se._acquire_job_lock(_valid_job, lambda m: None)
_valid_logs = []
check("a VALID payload with a fresh heartbeat (this process's own, just "
      "acquired) blocks a second acquisition attempt",
      se._acquire_job_lock(_valid_job, _valid_logs.append) is False)
se._release_job_lock(_valid_job)

_stale_job = "task23_valid_stale"
_stale_path = se._lock_path(_stale_job)
import json as _json
_stale_payload = {
    "pid": 424242, "boot_id": "some-other-boot-id",
    "started": "2020-01-01T00:00:00+00:00",
    "heartbeat": time.time() - (se._JOB_LOCK_HEARTBEAT_STALE_SECONDS + 60),
}
with open(_stale_path, "w") as f:
    f.write(_json.dumps(_stale_payload))
_stale_logs = []
check("a VALID payload with a heartbeat older than "
      "_JOB_LOCK_HEARTBEAT_STALE_SECONDS is still reclaimed, unchanged behaviour",
      se._acquire_job_lock(_stale_job, _stale_logs.append) is True)
check("the stale-heartbeat reclaim is logged with the holder's boot_id, same "
      "text as before this fix",
      any("reclaimed stale scan lock held by some-other-boot-id" in m for m in _stale_logs))
se._release_job_lock(_stale_job)
print("[task23_c4c5_valid_payload_paths_unchanged] fresh-heartbeat blocks, "
      "stale-heartbeat reclaims - unchanged by this fix OK")

# ======================================================================
# CHECK 6-8: _atomic_write_json itself - temp file cleaned up, final
# file content is exactly the payload given, and no stray .tmp.* files
# are left behind in the lock directory after normal use.
# ======================================================================
_atomic_target = os.path.join(TESTVOL, "task23_atomic_direct.json")
se._atomic_write_json(_atomic_target, {"hello": "world"})
check("_atomic_write_json writes exactly the given payload",
      _json.loads(open(_atomic_target).read()) == {"hello": "world"})
se._atomic_write_json(_atomic_target, {"hello": "world2"})
check("a second call atomically REPLACES the content (os.replace, not append)",
      _json.loads(open(_atomic_target).read()) == {"hello": "world2"})
_leftover_tmp = [f for f in os.listdir(TESTVOL) if ".tmp." in f]
check("no stray .tmp.* files are left behind in the lock directory after "
      "normal use (os.replace() always consumes the temp file)",
      _leftover_tmp == [])
print("[task23_c6c7c8_atomic_write_json_direct] writes/replaces cleanly, no "
      "leftover temp files OK")

# ======================================================================
# CHECK 9 (item 4 - INVESTIGATE ONLY, per the task's own instruction:
# "Report findings; do not change it unless proven"): reproduces the
# EXACT single-process sequence the Railway evidence describes -
# fastapi's own due-scan block tripping the rate-limit circuit breaker
# mid-run, then its own catch-up block (same tick, ~1 min later once
# the due-scan call returns) starting attempt 3/3 anyway.
#
# _loop() computes `_rl_cooldown_active`/`_rl_cooldown_until` exactly
# ONCE per tick (from state loaded BEFORE the due-scan block runs), and
# reuses that same snapshot to gate BOTH the due-scan check and the
# catch-up check further down in the SAME tick. In production, the
# due-scan's own _run_nightly() call runs synchronously inside that
# tick and can take minutes; if its per-universe loop trips nightly_
# scan.py's RateLimitCircuitBreaker partway through, it calls
# _record_rate_limit_cooldown() (scheduler_state.json now correctly
# carries a fresh cool-down) - but `_rl_cooldown_active` in THIS tick's
# local variable was already computed as False before the due-scan
# ran, and nothing re-derives it before the catch-up block's own gate
# check reads it. So catch-up proceeds, immediately re-hits the same
# rate limit.
#
# This is a DIFFERENT, simpler mechanism than the task's own named
# hypothesis ("the cool-down state can be overwritten/lost between the
# two processes [via] scheduler_state.json read-modify-write") - it
# needs only ONE process, and the cool-down is never lost from disk at
# all (confirmed below: it's correctly persisted and still active
# immediately after this tick). The task's named cross-process lost-
# update hypothesis is NOT what this test proves - what it proves is a
# same-tick stale LOCAL VARIABLE, a different bug with the same
# symptom. Per the task's explicit "do not change it unless proven"
# framing (and "INVESTIGATE ONLY"), no fix is applied for this item in
# this task - reported as a finding for the Director's own follow-up
# decision, same as TASK 22b's own "additional finding, reported, not
# auto-fixed beyond what was explicitly asked" precedent.
# ======================================================================
import json as _json2
from datetime import datetime as _dt2, timezone as _tz2


class _StopLoopAfterOneTick(BaseException):
    pass


_nightly_calls = []


def _fake_run_nightly(cfg_arg, lg, run_night=None, cancel_event=None):
    _nightly_calls.append({
        "run_night": run_night,
        "universes": list(cfg_arg.get("universes", [])),
    })
    if run_night is None:
        # The due-scan call (no run_night passed) - simulate nightly_
        # scan.py's own in-run circuit breaker tripping mid-scan and
        # recording a fresh cool-down, exactly as the real incident's
        # own fastapi process did around 21:08 UTC, moments before its
        # own catch-up block started attempt 3/3 at 21:09:46.
        se._record_rate_limit_cooldown(lg)


_fake_cfg = {
    "enabled": True,
    "universes": ["ASX 200", "ASX 300"],
    "universe_cadence": {"ASX 200": "daily", "ASX 300": "daily"},
    "scan_hour": 0,       # always "due" - now.hour >= 0 always true
    "digest_weekday": 99,  # never matches now.weekday() (0-6)
    "digest_hour": 99,
    "watchdog_hour": 99,
    "earnings_refresh_weekday": 99,
    "earnings_refresh_hour": 99,
    "backup_hour": 99,
    "volume_check_hour": 99,
    "top100_hour": 99,
}


def _fake_acquire_lock(job_name, log):
    # Only the "nightly" lock is real for this test - every other job
    # (top100, digest, watchdog, backup, volume_check, quote
    # recorders) is left untouched/skipped so the tick stays focused on
    # exactly the due-scan/catch-up sequence under investigation.
    return job_name == "nightly"


_cooldown_test_logs = []
import scan_store as _scan_store_mod_for_task23

with mock.patch.object(se, "_cfg", return_value=_fake_cfg), \
     mock.patch.object(se, "_acquire_job_lock", side_effect=_fake_acquire_lock), \
     mock.patch.object(se, "_universes_needing_scan", return_value=["ASX 200"]), \
     mock.patch.object(se, "_catchup_reference_night", return_value="2026-10-28"), \
     mock.patch.object(se, "_universes_missing_today", return_value=["ASX 300"]), \
     mock.patch.object(_scan_store_mod_for_task23, "load_scan", return_value=[{"Ticker": "FAKE"}]), \
     mock.patch.object(se, "_run_nightly", side_effect=_fake_run_nightly), \
     mock.patch.object(se.time, "sleep", side_effect=_StopLoopAfterOneTick):
    try:
        se._loop(_cooldown_test_logs.append)
    except _StopLoopAfterOneTick:
        pass

check("due-scan ran first, with no run_night (the regular due-scan call)",
      len(_nightly_calls) >= 1 and _nightly_calls[0]["run_night"] is None
      and _nightly_calls[0]["universes"] == ["ASX 200"])

_cooldown_now_active, _cooldown_now_until = se._rate_limit_cooldown_status(
    se._load_state(), _dt2.now(_tz2.utc))
check("the cool-down WAS correctly persisted to scheduler_state.json by the "
      "due-scan's own breaker trip, and is still active immediately after - "
      "ruling out a lost/overwritten write as what let the catch-up through",
      _cooldown_now_active is True)

check("FINDING: despite that freshly-active cool-down, the catch-up block ran "
      "anyway IN THE SAME TICK - a second _run_nightly() call with a real "
      "run_night, proving _rl_cooldown_active's one-per-tick snapshot (taken "
      "BEFORE the due-scan ran) is what let it through, not a lost state write",
      len(_nightly_calls) == 2 and _nightly_calls[1]["run_night"] == "2026-10-28"
      and _nightly_calls[1]["universes"] == ["ASX 300"])

check("the catch-up block's own cool-down-skip log line never fired - it was "
      "gated on the SAME stale _rl_cooldown_active value computed at tick-start",
      not any("catch-up scan skipped - rate-limit cool-down" in m for m in _cooldown_test_logs))
check("the catch-up scan's own start line DID log (attempt 2/3 - sharing the "
      "due-scan's own scan_attempts budget, exactly as designed)",
      any("catch-up scan for 2026-10-28" in m for m in _cooldown_test_logs))

print("[task23_c9_item4_cooldown_staleness_finding] REPRODUCED: single-process, "
      "same-tick stale `_rl_cooldown_active` snapshot - not a cross-process "
      "scheduler_state.json lost update (cool-down was correctly persisted and "
      "remained active throughout). No fix applied for this item in this task, "
      "per its own 'INVESTIGATE ONLY ... do not change it unless proven "
      "[the named cross-process hypothesis]' instruction - reported as a "
      "finding for the Director's own follow-up decision OK")

print()
print(f"PASS so far: {passed}, FAIL so far: {failed}")
sys.exit(1 if failed else 0)
