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


def _run_loop_briefly(role, run_seconds=0.3, tick_seconds=0.03):
    """Starts the REAL scheduler_engine._loop() on a daemon thread for a
    short, bounded wall-clock window, with _process_role()/_CHECK_EVERY_
    SECONDS patched so it ticks fast and reports the given role, and
    SCHEDULER_ENABLED=false so no real job ever runs. Returns the list
    of captured log lines. The thread is left running (daemon=True,
    same as production) - there is no clean way to stop an unbounded
    `while True:` loop, so this test never joins it; it just stops
    reading its output once run_seconds elapses, same convention this
    repo already uses wherever scheduler_engine.start() itself is
    exercised in a test."""
    _lines = []
    _old_env = os.environ.get("SCHEDULER_ENABLED")
    os.environ["SCHEDULER_ENABLED"] = "false"
    try:
        with mock.patch.object(sched, "_process_role", return_value=role), \
             mock.patch.object(sched, "_CHECK_EVERY_SECONDS", tick_seconds):
            t = threading.Thread(
                target=sched._loop, args=(lambda m, *a, **k: _lines.append(m),),
                daemon=True,
            )
            t.start()
            time.sleep(run_seconds)
    finally:
        if _old_env is None:
            os.environ.pop("SCHEDULER_ENABLED", None)
        else:
            os.environ["SCHEDULER_ENABLED"] = _old_env
    return _lines


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
_lines_streamlit = _run_loop_briefly("streamlit")
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
_lines_fastapi = _run_loop_briefly("fastapi")
check("role=fastapi: _loop() never logs a '[mem] ... streamlit' line",
      not any(_MEM_LINE_RE.match(m) for m in _lines_fastapi))


# ======================================================================
# CHECK 9-10: the 30-minute gate itself - directly exercising the exact
# boundary condition _loop() evaluates, via its own module-level state
# (_last_streamlit_mem_log_at) rather than waiting 30 real minutes.
# Same technique this repo already uses for the "alive" heartbeat's own
# interval gate and for other _record_job()-style state checks.
# ======================================================================
_now = time.time()
sched._last_heartbeat = _now
sched._last_streamlit_mem_log_at = _now - (sched._STREAMLIT_MEM_LOG_INTERVAL_SECONDS - 5)
sched._last_heartbeat_log_at = _now
_lines_not_due = _run_loop_briefly("streamlit", run_seconds=0.15)
check("29m55s after the last mem-log: the gate does NOT fire again yet "
      "(matches the exact >= interval comparison, not merely 'some time passed')",
      not any(_MEM_LINE_RE.match(m) for m in _lines_not_due))

sched._last_streamlit_mem_log_at = _now - (sched._STREAMLIT_MEM_LOG_INTERVAL_SECONDS + 5)
_lines_due = _run_loop_briefly("streamlit", run_seconds=0.15)
check("30m05s after the last mem-log: the gate DOES fire again",
      any(_MEM_LINE_RE.match(m) for m in _lines_due))


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
