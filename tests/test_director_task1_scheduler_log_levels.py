"""
Director's 2-task queue, TASK 1 (13 Oct 2026): "server.py passes
log.error into the scheduler tick, so every routine scheduler line
(e.g. '[scheduler] alive', '[top100] selected 200', '[nightly_scan]
... saved') is logged at error level and Railway shows it red. Route
routine lines to info and keep log.error only for real failures."

This generalises the overnight queue's own TASK 5 fix (which only
ever redirected ONE lock-skip line) to the WHOLE scheduler tick, via
_as_routine_logger() (scheduler_engine.py, replacing the narrower
_log_at_info()'s own internals - that function is kept, now a thin
wrapper over this one, for _run_queued_rescan_tick()'s own direct
callers). _loop() captures the ORIGINAL (possibly error-bound) `log`
as `_log_error`, then reassigns its own local `log = _as_routine_
logger(log)` - every routine line logged through the reassigned `log`
from that point on (including every line deep inside top100_engine.py/
nightly_scan.py, reached via _run_nightly()/_run_top100()/
_record_job()'s run_fn - NOT touched in those files at all) now
correctly logs at info whenever `log` was a bound Logger.error method,
with the message TEXT completely unchanged (this file proves that for
a representative sample, not just "no exception").

Real failures are explicitly, deliberately EXCLUDED from that
redirect, via a SEPARATE `_log_error` reference threaded to exactly
where a genuine, whole-job/whole-subsystem failure is logged (never a
per-step/per-ticker "failed, continuing" line, which is routine by this
codebase's own long-standing "log and continue" convention and is
correctly redirected like everything else):
  - _record_job()'s own hard-TIMED-OUT line (new `log_error` param,
    defaults to `log` for any caller that doesn't pass it).
  - _run_top100_job_if_due()'s own "Top 100 job failed" outer wrapper
    (new `log_error` param, forwarded into its own _record_job() call).
  - _run_queued_rescan_tick()'s own _record_job() call (new
    `log_error` param, forwarded through - this function has no
    failure line of its own besides that call).
  - _loop()'s own ~9 inline "subsystem failed" lines (portfolio
    watchdog, 5 quote recorders, db backup, hourly Top 100 batch poll)
    and its outer BaseException catch-all ("loop error (tick
    continues)") - call _log_error(...) directly, never the
    reassigned `log`.

app.py's own scheduler thread (log=print) is completely unaffected -
_as_routine_logger(print) returns print unchanged, same pass-through
_log_at_info() already proved for a non-Logger callable.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task1_scheduler_log_levels.py
"""
import inspect
import logging
import os
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="director_task1_log_levels_test_")
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


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _fresh_logger():
    logger = logging.getLogger(f"sdd.test_task1_{id(object())}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _CapturingHandler()
    logger.addHandler(handler)
    return logger, handler


# ======================================================================
# CHECK 1-3: _as_routine_logger() - the generalised redirect.
# ======================================================================
_logger1, _handler1 = _fresh_logger()
_routine = se._as_routine_logger(_logger1.error)
_routine("[scheduler] alive, next due: top100 poll due now")
check("_as_routine_logger(): a bound Logger.error method redirects to that SAME logger's .info()",
      len(_handler1.records) == 1 and _handler1.records[0].levelno == logging.INFO)
check("_as_routine_logger(): the message text is carried through byte-identical",
      _handler1.records[0].getMessage() == "[scheduler] alive, next due: top100 poll due now")

_plain_calls = []
_bound_append = _plain_calls.append
check("_as_routine_logger(): a plain callable (print-shaped, list.append-shaped) is returned "
      "UNCHANGED - app.py's own log=print path is untouched",
      se._as_routine_logger(_bound_append) is _bound_append)


# ======================================================================
# CHECK 4: _log_at_info() is now a thin wrapper over _as_routine_
# logger() - still correct for _run_queued_rescan_tick()'s own direct
# callers (its existing dedup line), not a second, divergent redirect.
# ======================================================================
_logger2, _handler2 = _fresh_logger()
se._log_at_info(_logger2.error, "[scheduler] queued rescan skipped - another process already holds the lock")
check("_log_at_info() still redirects to .info() exactly as the overnight queue's TASK 5 left it",
      len(_handler2.records) == 1 and _handler2.records[0].levelno == logging.INFO)


# ======================================================================
# CHECK 5-8: _record_job()'s new `log_error` param - the hard-TIMED-OUT
# line uses it, defaults to `log` when not given (existing callers/
# tests unaffected), and the real timeout-registration behaviour is
# completely unchanged (logging only).
# ======================================================================
import threading
_release_worker = threading.Event()


def _slow_job(log):
    log("started")
    _release_worker.wait(timeout=10)


with mock.patch.dict(se._JOB_HARD_TIMEOUT_SECONDS, {"nightly": 0.2}):
    _routine_logs = []
    _error_logs = []
    _cancel = threading.Event()
    raised = None
    try:
        se._record_job("nightly", _routine_logs.append, _slow_job,
                        cancel_event=_cancel, log_error=_error_logs.append)
    except TimeoutError as e:
        raised = e
check("_record_job(): a hard timeout still raises TimeoutError, unchanged",
      raised is not None)
check("_record_job(): the TIMED OUT line goes to `log_error`, NOT the routine `log`",
      any("TIMED OUT" in m for m in _error_logs) and not any("TIMED OUT" in m for m in _routine_logs))
check("_record_job(): the routine `log` still received the job's own ordinary line ('started')",
      any("started" in m for m in _routine_logs))
_release_worker.set()
import time as _time
_deadline = _time.time() + 5
while se._abandoned_worker_status("nightly")[0] is not None and _time.time() < _deadline:
    _time.sleep(0.05)

# Omitting log_error entirely still works (defaults to `log`) - every
# existing caller/test of _record_job() that doesn't pass it is
# unaffected.
with mock.patch.dict(se._JOB_HARD_TIMEOUT_SECONDS, {"nightly": 0.2}):
    _release_worker2 = threading.Event()

    def _slow_job2(log):
        _release_worker2.wait(timeout=10)

    _default_logs = []
    _cancel2 = threading.Event()
    raised2 = None
    try:
        se._record_job("nightly", _default_logs.append, _slow_job2, cancel_event=_cancel2)
    except TimeoutError:
        raised2 = True
check("_record_job(): omitting log_error entirely still logs TIMED OUT (falls back to `log`) - "
      "every existing caller/test is unaffected",
      raised2 and any("TIMED OUT" in m for m in _default_logs))
_release_worker2.set()
_deadline2 = _time.time() + 5
while se._abandoned_worker_status("nightly")[0] is not None and _time.time() < _deadline2:
    _time.sleep(0.05)


# ======================================================================
# CHECK 9-10: _run_top100_job_if_due()'s new `log_error` param - its
# own "Top 100 job failed" line uses it; routine lines (the "due for"/
# "starting" lines) still go to the plain `log`.
# ======================================================================
_cfg = {"top100_hour": 0}
_now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
_state = {"top100_pending_for": "2026-10-13", "top100_pending_finished_at": "03:00",
          "top100_attempts": {}}
with mock.patch.object(se, "_acquire_job_lock", return_value=True), \
     mock.patch.object(se, "_release_job_lock"), \
     mock.patch.object(se, "_save_state"), \
     mock.patch.object(se, "_load_state", return_value=_state), \
     mock.patch.object(se, "_run_top100", side_effect=RuntimeError("boom")):
    _routine_logs2 = []
    _error_logs2 = []
    se._run_top100_job_if_due(_state, _now, _cfg, _routine_logs2.append, log_error=_error_logs2.append)
check("_run_top100_job_if_due(): a crashed job logs 'Top 100 job failed' via log_error, not log",
      any("Top 100 job failed" in m for m in _error_logs2)
      and not any("Top 100 job failed" in m for m in _routine_logs2))
check("_run_top100_job_if_due(): its own routine progress lines ('due for'/'starting') still "
      "go to the plain `log`",
      any("due for" in m for m in _routine_logs2) and any("starting Top 100" in m for m in _routine_logs2))


# ======================================================================
# CHECK 11: _run_queued_rescan_tick()'s new `log_error` param forwards
# into its own _record_job() call - a hard timeout there also stays
# on log_error, not the routine log.
# ======================================================================
se._queue_rescan_request(["ASX 200"], requested_by="owner@x.com", log=lambda *a: None)
with mock.patch.dict(se._JOB_HARD_TIMEOUT_SECONDS, {"nightly": 0.2}), \
     mock.patch.object(se, "_run_nightly", side_effect=lambda cfg, lg, **kw: _release_worker3.wait(timeout=10)):
    _release_worker3 = threading.Event()
    _routine_logs3 = []
    _error_logs3 = []
    raised3 = None
    try:
        se._run_queued_rescan_tick(
            {"universes": [], "universe_cadence": {}}, _routine_logs3.append,
            rl_cooldown_active=False, rl_cooldown_until=None,
            abandoned_status=None, abandoned_info=None, log_error=_error_logs3.append,
        )
    except TimeoutError:
        raised3 = True
check("_run_queued_rescan_tick(): a hard timeout inside its own _record_job() call logs "
      "TIMED OUT via log_error, not the routine log",
      raised3 and any("TIMED OUT" in m for m in _error_logs3)
      and not any("TIMED OUT" in m for m in _routine_logs3))
_release_worker3.set()
_deadline3 = _time.time() + 5
while se._abandoned_worker_status("nightly")[0] is not None and _time.time() < _deadline3:
    _time.sleep(0.05)
_clear_state = se._load_state()
_clear_state.pop("rescan_request", None)
se._save_state(_clear_state)


# ======================================================================
# CHECK 12-16: _loop()'s own wiring - source-text proof (same
# established convention as tests/test_top100_trigger_window.py's own
# inspect.getsource() checks), since _loop() itself is an unbounded
# `while True:` daemon loop that can't be run to completion in a test.
# ======================================================================
_loop_src = inspect.getsource(se._loop)
check("_loop(): captures the original (possibly error-bound) `log` as `_log_error` before "
      "reassigning its own local `log` to the routine-safe version",
      "_log_error = log" in _loop_src and "log = _as_routine_logger(log)" in _loop_src)
check("_loop(): the reassignment happens BEFORE the first routine line is logged",
      _loop_src.index("log = _as_routine_logger(log)") <
      _loop_src.index('log(f"[scheduler] loop started'))

# Every inline genuine-failure line now uses _log_error(, not log(.
for _fragment in (
    'log_error(f"[scheduler] loop error (tick continues)',
    'log_error(f"[scheduler] portfolio watchdog failed',
    'log_error(f"[scheduler] ASX quote recorder failed',
    'log_error(f"[scheduler] US quote recorder failed',
    'log_error(f"[scheduler] UK quote recorder failed',
    'log_error(f"[scheduler] TSX quote recorder failed',
    'log_error(f"[scheduler] JP quote recorder failed',
    'log_error(f"[scheduler] db backup failed',
    'log_error(f"[scheduler] hourly Top 100 batch poll failed',
):
    check(f"_loop(): genuine-failure line now uses log_error - {_fragment!r} present verbatim "
          "(message TEXT unchanged, only the callable changed)",
          _fragment in _loop_src)

# Every _record_job()/_run_top100_job_if_due()/_run_queued_rescan_tick()
# call site inside _loop() now forwards log_error=_log_error.
check("_loop(): _run_top100_job_if_due() is called with log_error=_log_error",
      "_run_top100_job_if_due(state, now, cfg, log, log_error=_log_error)" in _loop_src)
check("_loop(): _run_queued_rescan_tick() is called with log_error=_log_error",
      re.search(r"_run_queued_rescan_tick\(\s*cfg, log,.*?log_error=_log_error", _loop_src, re.DOTALL))

# Every ACTUAL _record_job() call site inline in _loop() (excluding the
# 2 plain-English mentions of "_record_job()" inside comments/docstrings
# elsewhere in this function) now passes log_error=_log_error - checked
# individually since two of these calls (nightly due-scan, catch-up)
# span several lines with a nested _run_nightly(...) call of their own,
# which a single non-greedy regex can't safely skip over.
for _fragment in (
    # nightly due-scan and catch-up: multi-line calls, matched by their
    # own distinctive cancel_event kwarg immediately preceding log_error.
    'cancel_event=_due_cancel_event,\n                                    log_error=_log_error,',
    'cancel_event=_catchup_cancel_event,\n                                        log_error=_log_error,',
    '_record_job("watchdog", log, _run_watchdog, log_error=_log_error)',
    '_record_job("quote_recorder_asx", log, _run_quote_recorder_asx,\n                                            log_error=_log_error)',
    '_record_job("quote_recorder_us", log, _run_quote_recorder_us,\n                                            log_error=_log_error)',
    '_record_job("quote_recorder_uk", log, _run_quote_recorder_uk,\n                                            log_error=_log_error)',
    '_record_job("quote_recorder_tsx", log, _run_quote_recorder_tsx,\n                                            log_error=_log_error)',
    '_record_job("quote_recorder_jp", log, _run_quote_recorder_jp,\n                                            log_error=_log_error)',
    '_record_job("backup", log, _run_backup, log_error=_log_error)',
    '_record_job("volume_check", log, _run_volume_check, log_error=_log_error)',
    'lock_name="top100",\n                                        log_error=_log_error)',
    '_record_job("earnings_refresh", log, _run_earnings_refresh,\n                                        log_error=_log_error)',
):
    check(f"_loop(): _record_job() call site carries log_error=_log_error - fragment present: "
          f"{_fragment.splitlines()[0]!r}...",
          _fragment in _loop_src)
check("_loop(): both weekly-digest _record_job() calls (forced + scheduled) carry log_error=_log_error",
      _loop_src.count('_record_job("digest", log, _run_digest, log_error=_log_error)') == 2)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
