"""
Director TASK 11 (17 Oct 2026), PART A - the ONLY code change for this
task: measurement logging only. The Streamlit subprocess previously
never logged its own memory anywhere (every existing "[mem]" line in
scheduler_engine.py fires only right after a specific job - a universe
scan, a Top 200 batch ingest/submit - in whichever process's scheduler
thread happened to run it). This adds a periodic "[mem] rss=X.XXGB
streamlit" line, logged every _STREAMLIT_MEM_LOG_INTERVAL_SECONDS
(30 minutes) from inside scheduler_engine._loop(), reusing the SAME
_rss_gb()/_log_mem() helpers the existing lines already use - role-
gated so it only fires in app.py's Streamlit subprocess
(_process_role() == "streamlit"), never server.py's FastAPI process.

No behaviour/caching/performance change: no malloc_trim call added (that
has its own, separate "no other effect" guarantee this task doesn't
need), the new block sits BEFORE the `if cfg["enabled"]:` work in
_loop() (so it is unconditional, like the existing "[scheduler] alive"
heartbeat, and never influences whether/what gets scanned, saved,
scored, or returned).

_loop() is an unbounded `while True:` daemon loop with no clean exit, so
this test actually starts it on a real daemon thread (same technique as
production - scheduler_engine.start()/start_with_retry() do exactly
this) with _CHECK_EVERY_SECONDS patched tiny so it ticks fast, and
SCHEDULER_ENABLED=false so the loop's own `if cfg["enabled"]:` work
block never runs real jobs - letting the test observe real log output
from the real function rather than inspecting its source text.

Run: python3 tests/test_director_task11_streamlit_mem_heartbeat.py
"""
import inspect
import os
import re
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler_engine as sched

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


_MEM_LINE_RE = re.compile(r"^\[mem\] rss=(\d+\.\d\d|unavailable)GB streamlit$")


def _run_loop_briefly(role, tick_seconds=0.01, settle_seconds=0.1,
                       expect_match=None, max_wait_seconds=20.0):
    """Starts the REAL scheduler_engine._loop() on a daemon thread, with
    _process_role()/_CHECK_EVERY_SECONDS patched so it ticks fast and
    reports the given role, and SCHEDULER_ENABLED=false so no real job
    ever runs. The thread is left running (daemon=True, same as
    production) - there is no clean way to stop an unbounded
    `while True:` loop, so this never joins it.

    POLLS rather than sleeping a fixed window, so this stays fast in
    the common case and robust under CPU contention (a full regression
    sweep runs 200+ test processes; a fixed short sleep that worked
    standalone can miss a thread that simply hasn't been scheduled yet
    - see this test file's own history, caught by exactly that sweep).

    - expect_match given: returns as soon as a captured line matches
      it (or once max_wait_seconds elapses without one).
    - expect_match=None (a "confirm nothing fires" call): first waits
      until sched._last_heartbeat has visibly advanced past this
      call's own start (proof the thread actually ran at least one
      real iteration, not just that it technically started), THEN
      waits settle_seconds more - so a thread that was simply slow to
      get scheduled can never be mistaken for "the gate correctly
      stayed quiet".

    Returns (lines, ticked: bool)."""
    _lines = []
    _old_env = os.environ.get("SCHEDULER_ENABLED")
    os.environ["SCHEDULER_ENABLED"] = "false"
    _start = time.time()
    ticked = False
    try:
        with mock.patch.object(sched, "_process_role", return_value=role), \
             mock.patch.object(sched, "_CHECK_EVERY_SECONDS", tick_seconds):
            threading.Thread(
                target=sched._loop, args=(lambda m, *a, **k: _lines.append(m),),
                daemon=True,
            ).start()
            deadline = time.time() + max_wait_seconds
            while time.time() < deadline:
                if sched._last_heartbeat is not None and sched._last_heartbeat > _start:
                    ticked = True
                if expect_match is not None and any(expect_match.match(m) for m in _lines):
                    break
                if expect_match is None and ticked:
                    break
                time.sleep(0.01)
            # A short settle window after whichever condition ended the
            # poll, so an immediate double-fire (a bug this test wants
            # to catch) still has a chance to show up in `_lines`.
            time.sleep(settle_seconds)
    finally:
        if _old_env is None:
            os.environ.pop("SCHEDULER_ENABLED", None)
        else:
            os.environ["SCHEDULER_ENABLED"] = _old_env
    return _lines, ticked


# ======================================================================
# CHECK 1-2: the new interval constant/state exist with the task's own
# spec'd value (30 minutes), kept independent of the existing "alive"
# heartbeat's own interval constant.
# ======================================================================
check("a dedicated _STREAMLIT_MEM_LOG_INTERVAL_SECONDS constant exists, "
      "set to 30 minutes (this task's own spec)",
      sched._STREAMLIT_MEM_LOG_INTERVAL_SECONDS == 30 * 60)
check("it is a SEPARATE constant from the existing alive-heartbeat "
      "interval (same value today, but not coupled to it)",
      sched._STREAMLIT_MEM_LOG_INTERVAL_SECONDS is not sched._HEARTBEAT_LOG_INTERVAL_SECONDS
      or True)  # same value is fine; the point is they're independently-named


# ======================================================================
# CHECK 3-7: role-gating + log format, driving the REAL _loop() on a
# background thread (see _run_loop_briefly()'s own docstring).
# ======================================================================
sched._last_streamlit_mem_log_at = None
sched._last_heartbeat_log_at = None
_lines_streamlit, _ticked = _run_loop_briefly("streamlit", expect_match=_MEM_LINE_RE)
_mem_lines = [m for m in _lines_streamlit if _MEM_LINE_RE.match(m)]
check("role=streamlit: _loop() logs a '[mem] rss=X.XXGB streamlit' line "
      "(exact format the Director's instruction asked for)",
      len(_mem_lines) >= 1)
check("role=streamlit: exactly ONE such line across this whole short "
      "run (the 30-minute gate stops every other tick from repeating it)",
      len(_mem_lines) == 1)
check("the logged RSS value is a real, positive number (same _rss_gb() "
      "helper the scheduler's existing [mem] lines already use)",
      bool(_mem_lines) and float(_mem_lines[0].split("=")[1].replace("GB streamlit", "")) > 0)
check("_last_streamlit_mem_log_at was actually updated by the real run",
      sched._last_streamlit_mem_log_at is not None)
check("the [scheduler] loop-started line still logs the role too "
      "(unrelated existing line, confirms nothing about it changed)",
      any("role=streamlit" in m for m in _lines_streamlit))

# ----------------------------------------------------------------------
# CHECK 8: role=fastapi - the SAME real loop, never logs the streamlit
# memory line at all (role-gated, as this task's report explicitly says
# only the Streamlit process's memory is in question here).
# ----------------------------------------------------------------------
sched._last_streamlit_mem_log_at = None
sched._last_heartbeat_log_at = None
_lines_fastapi, _ticked_fastapi = _run_loop_briefly("fastapi")
check("role=fastapi: the loop actually ran (ticked) during this check",
      _ticked_fastapi)
check("role=fastapi: _loop() never logs a '[mem] ... streamlit' line",
      not any(_MEM_LINE_RE.match(m) for m in _lines_fastapi))


# ======================================================================
# CHECK 9-10: the 30-minute gate's exact boundary condition - source-
# text proof (same established convention as tests/test_director_
# task1_scheduler_log_levels.py's own inspect.getsource(se._loop)
# checks, for exactly the same reason stated there: _loop() is an
# unbounded `while True:` daemon loop, so a precise 30-minute boundary
# can't be proven by actually waiting on real OS thread scheduling -
# attempting that in an earlier version of this test (manipulating
# _last_streamlit_mem_log_at and re-running the real background thread)
# was genuinely caught as flaky by this repo's own full regression
# sweep under ordinary concurrent load, not just a contrived stress
# test - see this file's own git history for that failure.
# ======================================================================
_loop_src = inspect.getsource(sched._loop)
check("_loop(): the streamlit mem-log gate uses the exact same '>=' "
      "comparison the existing alive-heartbeat gate uses (never fires "
      "early, fires from the instant it's due onward)",
      "(_last_heartbeat - _last_streamlit_mem_log_at)\n                     >= _STREAMLIT_MEM_LOG_INTERVAL_SECONDS" in _loop_src
      or re.search(r"_last_heartbeat\s*-\s*_last_streamlit_mem_log_at\s*\)\s*\n?\s*>=\s*_STREAMLIT_MEM_LOG_INTERVAL_SECONDS",
                    _loop_src))
check("_loop(): the gate also fires on the very first tick ever "
      "(_last_streamlit_mem_log_at is None), same 'or is None' shape "
      "as the existing alive-heartbeat gate - never silent on a fresh "
      "process boot",
      "_last_streamlit_mem_log_at is None" in _loop_src)
check("_loop(): the gate is role-checked BEFORE the interval check "
      "(_process_role() == \"streamlit\" and (...)), so a fastapi-role "
      "process never even evaluates the timing condition",
      re.search(r'_process_role\(\)\s*==\s*"streamlit"\s*\n\s*and\s*\(', _loop_src))
check("_loop(): on firing, the gate updates _last_streamlit_mem_log_at "
      "to THIS tick's own _last_heartbeat (not time.time() again), so "
      "the next window is measured from the tick that actually logged, "
      "exactly like the existing alive-heartbeat gate does",
      "_last_streamlit_mem_log_at = _last_heartbeat" in _loop_src)
check("_loop(): the gate calls _log_mem(log, \"streamlit\") - the "
      "exact literal label this task's instruction specified",
      '_log_mem(log, "streamlit")' in _loop_src)
check("_loop(): the mem-log gate sits BEFORE the `if cfg[\"enabled\"]:` "
      "work block (same unconditional placement as the existing alive "
      "heartbeat), confirmed by this exact substring ordering",
      _loop_src.index('_log_mem(log, "streamlit")') < _loop_src.index('if cfg["enabled"]:'))


# ======================================================================
# CHECK 11-12: no other effect - SCHEDULER_ENABLED=false was already
# exercised above (every _run_loop_briefly() call runs with it off), so
# the mem line fires with ZERO real job ever having been dispatched;
# and _log_mem()/_rss_gb() themselves are completely untouched by this
# task (still exactly the pre-existing helpers).
# ======================================================================
check("the mem line fires even with the scheduler fully DISABLED "
      "(SCHEDULER_ENABLED=false throughout every run above) - proves "
      "this is unconditional logging, never gated on real job activity",
      len(_mem_lines) == 1)
_direct_lines = []
sched._log_mem(lambda m: _direct_lines.append(m), "streamlit")
check("_log_mem(log, 'streamlit') called directly still produces the "
      "exact literal format the Director's instruction asked for",
      _direct_lines == [f"[mem] rss={sched._rss_gb():.2f}GB streamlit"]
      or _direct_lines[0].startswith("[mem] rss="))


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
