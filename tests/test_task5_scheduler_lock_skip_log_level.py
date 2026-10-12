"""
Director's overnight queue, TASK 5 (12 Oct 2026): "[scheduler] queued
rescan skipped - another process already holds the lock" was logged at
ERROR severity every minute the lock stayed contended, even though it
is routine, expected behaviour, not a failure.

Root cause: server.py:282 calls scheduler_engine.start_with_retry(log=
log.error), where `log` is logging.getLogger("sdd.server") - a plain
callable parameter threaded through every tick of the scheduler loop,
not just start()'s own failure path. Every message _run_queued_rescan_
tick() logs through that same `log` therefore inherits ERROR severity
in server.py's process, including this routine, expected skip. app.py's
own scheduler loop passes log=print instead, so the same line reads as
plain stdout there.

Fix (logging only - no change to locking behaviour): a new helper,
_log_at_info(log, message), introspects the passed-in callable. If it
is a bound method of a real logging.Logger (getattr(log, "__self__",
...) exposes that Logger), it calls that SAME logger's own .info()
instead - so the message is correctly demoted to INFO without
introducing a second, possibly disconnected logger. Any other callable
(print, a list's .append, a test's lambda) falls through to log(message)
completely unchanged. _run_queued_rescan_tick() also now logs this one
line only ONCE per continuous "losing the lock" stretch via a module-
level flag (_queued_rescan_lock_skip_logged), reset the moment the lock
IS acquired or there is no pending request at all - so a fresh
contention episode logs again, but a long one doesn't spam every tick.

Covers:
  - _log_at_info() redirects a bound Logger.error-shaped callable to
    that same logger's own .info(), confirmed via logger.level/handler
    capture, not just "didn't crash".
  - _log_at_info() leaves a plain callable (print-shaped, or a list's
    .append) completely unchanged - same message, called the same way.
  - _run_queued_rescan_tick() logs the lock-contention line on the
    FIRST tick the lock is held, but not on a SECOND consecutive tick
    with the lock still held (dedup within one episode).
  - Once the lock becomes free (a tick where _acquire_job_lock succeeds,
    modelled here as a cleared pending request - the simplest way to
    exercise the reset branch without a real _run_nightly happy path)
    and the dedup flag is cleared, a FRESH episode of contention logs
    again - the dedup resets per episode, it does not suppress forever.
  - No change to locking behaviour itself: _acquire_job_lock/_release_
    job_lock are never touched by this fix, and the skip decision
    (m_run.called is False) is identical before and after.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_task5_scheduler_lock_skip_log_level.py
"""
import logging
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task5_lock_skip_log_level_test_")
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
# CHECK 1-2: _log_at_info() redirects a bound Logger.error-shaped
# callable to that SAME logger's .info(), and leaves any other
# callable completely unchanged.
# ======================================================================
class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


_logger = logging.getLogger("sdd.test_task5_lock_skip")
_logger.setLevel(logging.INFO)
_handler = _CapturingHandler()
_logger.addHandler(_handler)
_logger.propagate = False

se._log_at_info(_logger.error, "[scheduler] queued rescan skipped - another process already holds the lock")

check("a bound Logger.error-shaped callable is redirected to that same logger's own .info()",
      len(_handler.records) == 1 and _handler.records[0].levelno == logging.INFO)
check("the redirected record carries the exact same message text, unchanged",
      _handler.records and _handler.records[0].getMessage() ==
      "[scheduler] queued rescan skipped - another process already holds the lock")
check("the redirected record is on the SAME logger (sdd.test_task5_lock_skip), not a new/disconnected one",
      _handler.records and _handler.records[0].name == "sdd.test_task5_lock_skip")

_plain_calls = []
se._log_at_info(_plain_calls.append, "plain message")
check("a plain callable (e.g. a list's .append) is left completely unchanged - called with the "
      "same message, no severity concept involved",
      _plain_calls == ["plain message"])

_print_calls = []


def _fake_print(msg):
    _print_calls.append(msg)


se._log_at_info(_fake_print, "another plain message")
check("a print-shaped plain function is also left completely unchanged",
      _print_calls == ["another plain message"])

_logger.removeHandler(_handler)


# ======================================================================
# CHECK 3-5: _run_queued_rescan_tick() logs the lock-contention line
# once per held-lock episode, not every tick, and a fresh episode
# (after the flag resets) logs again.
# ======================================================================
se._queue_rescan_request(["ASX 200"], requested_by="owner@x.com", log=lambda *a: None)

with mock.patch.object(se, "_acquire_job_lock", return_value=False), \
     mock.patch.object(se, "_run_nightly") as m_run_a:
    logs_tick1 = []
    se._run_queued_rescan_tick(
        {"universes": [], "universe_cadence": {}}, logs_tick1.append,
        rl_cooldown_active=False, rl_cooldown_until=None,
        abandoned_status=None, abandoned_info=None,
    )
check("TASK 5: first tick with the lock held logs the lock-contention line",
      not m_run_a.called and
      any("another process already holds the lock" in l for l in logs_tick1))

with mock.patch.object(se, "_acquire_job_lock", return_value=False), \
     mock.patch.object(se, "_run_nightly") as m_run_b:
    logs_tick2 = []
    se._run_queued_rescan_tick(
        {"universes": [], "universe_cadence": {}}, logs_tick2.append,
        rl_cooldown_active=False, rl_cooldown_until=None,
        abandoned_status=None, abandoned_info=None,
    )
check("TASK 5 fix: a SECOND consecutive tick with the lock still held does NOT repeat the "
      "lock-contention line - the dedup flag suppresses it within the same episode",
      not m_run_b.called and
      not any("another process already holds the lock" in l for l in logs_tick2))
check("the skip decision itself is unchanged by the fix - no job ran on either tick, "
      "exactly as before TASK 5 (logging only, no locking-behaviour change)",
      not m_run_a.called and not m_run_b.called)

# Clear the pending request (the simplest, already-covered path for
# "no pending rescan request at all") - this is one of the two places
# _run_queued_rescan_tick() resets the dedup flag, the other being a
# successful lock acquisition exercised in test_audit_c3's own happy path.
_clear_state = se._load_state()
_clear_state.pop("rescan_request", None)
se._save_state(_clear_state)

with mock.patch.object(se, "_run_nightly") as m_run_c:
    logs_tick3 = []
    se._run_queued_rescan_tick(
        {"universes": [], "universe_cadence": {}}, logs_tick3.append,
        rl_cooldown_active=False, rl_cooldown_until=None,
        abandoned_status=None, abandoned_info=None,
    )
check("a tick with no pending rescan request at all resets the dedup flag and runs/logs nothing",
      not m_run_c.called and logs_tick3 == [])

se._queue_rescan_request(["ASX 200"], requested_by="owner@x.com", log=lambda *a: None)
with mock.patch.object(se, "_acquire_job_lock", return_value=False), \
     mock.patch.object(se, "_run_nightly") as m_run_d:
    logs_tick4 = []
    se._run_queued_rescan_tick(
        {"universes": [], "universe_cadence": {}}, logs_tick4.append,
        rl_cooldown_active=False, rl_cooldown_until=None,
        abandoned_status=None, abandoned_info=None,
    )
check("TASK 5 fix: a FRESH contention episode (after the flag was reset) logs the "
      "lock-contention line again - the dedup resets per episode, it does not "
      "suppress the line forever",
      not m_run_d.called and
      any("another process already holds the lock" in l for l in logs_tick4))

_clear_state2 = se._load_state()
_clear_state2.pop("rescan_request", None)
se._save_state(_clear_state2)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
