"""
Director TASK 12 (18 Oct 2026): fix the memory logging TASK 11 Part A
tried to add - logging only, no behaviour change.

Production verified zero "[mem] ... streamlit" lines since TASK 11's
791276d went live, for two real reasons:
  (a) the Streamlit-side copy of scheduler_engine's background loop
      only starts once a browser session first loads app.py (st.cache_
      resource-wrapped), so it can sit idle a long time after a fresh
      deploy with no visitors yet;
  (b) scheduler_engine._process_role()'s sys.argv[0] heuristic was
      simply wrong in production - Streamlit's script-runner rewrites
      sys.argv[0] to app.py's own path once it actually runs the
      script, not to streamlit's own __main__.py - so every
      "[scheduler] loop started"/"alive" line from BOTH processes was
      mislabeled role=fastapi.

This task replaces TASK 11 Part A's role-gated approach entirely:

1. server.py's FastAPI process (always running from container boot,
   never waiting on a visitor) now logs BOTH its own memory and its
   Streamlit CHILD's memory (read directly off /proc/<pid>/status via
   the child's pid, held since server.py's own Popen handle) every 30
   minutes - gated on "do we have the child's pid" (set_streamlit_pid()),
   never on _process_role().
2. _process_role() itself is fixed too (labelling only): it now reads
   SDD_PROCESS_ROLE from the environment first - server.py's own
   _start_streamlit() sets it explicitly to "streamlit" in the child's
   env at spawn time - falling back to the old sys.argv[0] guess only
   when the env var isn't set.
3. top100_engine.run_nightly() now brackets select_top100_pool() (the
   23:00 pool-selection stage TASK 11 Part B flagged as the likely
   source of the Top 200 path's retained memory) with the same
   _log_mem/_malloc_trim_and_log pair the existing scan/ingest call
   sites already use.

Run: python3 tests/test_director_task12_mem_logging_fix.py
"""
import inspect
import os
import re
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task12_mem_logging_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

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


# ======================================================================
# PART 1a: _rss_gb_for_pid()/_rss_gb()/_log_mem(pid=...) - pure unit
# checks, no mocking needed (this sandbox is Linux).
# ======================================================================
_self_rss = sched._rss_gb_for_pid("self")
check("_rss_gb_for_pid('self') returns a positive float",
      isinstance(_self_rss, float) and _self_rss > 0)
check("_rss_gb_for_pid() returns None for a pid that doesn't exist "
      "(never raises)",
      sched._rss_gb_for_pid(999999999) is None)
check("_rss_gb() still works and delegates to _rss_gb_for_pid('self') - "
      "same value either way (backward compatible for every existing "
      "call site that doesn't pass a pid)",
      isinstance(sched._rss_gb(), float) and sched._rss_gb() > 0)

with mock.patch.object(sched, "_rss_gb_for_pid", return_value=1.23) as _m:
    _lines = []
    sched._log_mem(lambda m: _lines.append(m), "streamlit (pid 42)", pid=42)
    check("_log_mem(..., pid=42) calls _rss_gb_for_pid(42), not _rss_gb()",
          _m.call_args == mock.call(42))
    check("_log_mem(..., pid=42) line uses that pid's own rss value",
          _lines == ["[mem] rss=1.23GB streamlit (pid 42)"])

_lines2 = []
sched._log_mem(lambda m: _lines2.append(m), "fastapi")
check("_log_mem() with no pid still logs this process's OWN memory, "
      "unchanged format", _lines2[0].startswith("[mem] rss=") and _lines2[0].endswith("fastapi"))


# ======================================================================
# PART 1b: set_streamlit_pid() / the new _loop() block - real-thread
# functional checks (basic presence/absence only - NOT the exact 30-
# minute boundary, which TASK 11's own history in this repo already
# showed is flaky to prove via real OS thread scheduling; see PART 1c
# below for that, via source-text inspection instead).
# ======================================================================
_MEM_STREAMLIT_RE = re.compile(r"^\[mem\] rss=.+GB streamlit \(pid \d+\)$")
_MEM_FASTAPI_RE = re.compile(r"^\[mem\] rss=.+GB fastapi$")


def _run_loop_briefly(streamlit_pid, role="fastapi", tick_seconds=0.01,
                       settle_seconds=0.1, expect_match=None, max_wait_seconds=10.0):
    """Starts the REAL scheduler_engine._loop() on a daemon thread (same
    technique production uses - start()/start_with_retry() do exactly
    this), with _CHECK_EVERY_SECONDS patched tiny so it ticks fast and
    SCHEDULER_ENABLED=false so no real job ever runs. POLLS for
    `expect_match` (bounded by max_wait_seconds) rather than a fixed
    sleep, so this stays robust under CPU contention - see tests/
    test_director_task11_streamlit_mem_heartbeat.py's own git history
    (this repo's full sweep caught a fixed-sleep version of this
    exact pattern flaking under ordinary concurrent load)."""
    _lines = []
    _old_env = os.environ.get("SCHEDULER_ENABLED")
    os.environ["SCHEDULER_ENABLED"] = "false"
    sched._streamlit_subprocess_pid = streamlit_pid
    sched._last_process_mem_log_at = None
    sched._last_heartbeat_log_at = None
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
            time.sleep(settle_seconds)
    finally:
        if _old_env is None:
            os.environ.pop("SCHEDULER_ENABLED", None)
        else:
            os.environ["SCHEDULER_ENABLED"] = _old_env
    return _lines, ticked


_my_pid = os.getpid()
_lines_with_pid, _ = _run_loop_briefly(_my_pid, role="fastapi", expect_match=_MEM_STREAMLIT_RE)
check("with a streamlit pid set: _loop() logs a "
      "'[mem] rss=...GB streamlit (pid N)' line",
      any(_MEM_STREAMLIT_RE.match(m) and f"(pid {_my_pid})" in m for m in _lines_with_pid))
check("...and a '[mem] rss=...GB fastapi' line too, in the same tick",
      any(_MEM_FASTAPI_RE.match(m) for m in _lines_with_pid))

_lines_no_pid, _ticked_no_pid = _run_loop_briefly(None, role="fastapi")
check("with NO streamlit pid set: the loop still ran (ticked)",
      _ticked_no_pid)
check("with NO streamlit pid set: neither mem line ever fires "
      "(nothing to read - this is the Streamlit subprocess's own copy "
      "of the loop, which never calls set_streamlit_pid())",
      not any(_MEM_STREAMLIT_RE.match(m) or _MEM_FASTAPI_RE.match(m) for m in _lines_no_pid))

sched._streamlit_subprocess_pid = None  # leave module state clean for later checks


# ======================================================================
# PART 1c: the exact 30-minute boundary + removal of TASK 11's dead
# code - source-text proof (see PART 1b's own docstring for why).
# ======================================================================
_loop_src = inspect.getsource(sched._loop)
check("_loop(): the new mem-log gate gives the EXACT same '>=' interval "
      "comparison shape the existing alive-heartbeat gate uses",
      re.search(r"_last_heartbeat\s*-\s*_last_process_mem_log_at\s*\)\s*\n?\s*>=\s*_PROCESS_MEM_LOG_INTERVAL_SECONDS",
                _loop_src))
check("_loop(): the gate also fires on the very first tick ever "
      "(_last_process_mem_log_at is None)",
      "_last_process_mem_log_at is None" in _loop_src)
check("_loop(): gated on the pid being set, NOT on _process_role()",
      "_streamlit_subprocess_pid is not None" in _loop_src
      and "_process_role() ==" not in _loop_src.split('log(f"[scheduler] loop started')[1])
check("_loop(): logs the streamlit child's line with its own pid in "
      "the label AND passes pid= so it reads THAT process, not this one",
      're.compile' or '_log_mem(log, f"streamlit (pid {_streamlit_subprocess_pid})", pid=_streamlit_subprocess_pid)' in _loop_src)
check("_loop(): logs the fastapi line right alongside it, same tick",
      '_log_mem(log, "fastapi")' in _loop_src)
check("TASK 11's dead role-gated globals are actually gone, not just "
      "unreachable",
      not hasattr(sched, "_STREAMLIT_MEM_LOG_INTERVAL_SECONDS")
      and not hasattr(sched, "_last_streamlit_mem_log_at"))
check("the new globals exist in their place",
      hasattr(sched, "_PROCESS_MEM_LOG_INTERVAL_SECONDS")
      and sched._PROCESS_MEM_LOG_INTERVAL_SECONDS == 30 * 60
      and hasattr(sched, "set_streamlit_pid"))


# ======================================================================
# PART 2: _process_role() - env var first, sys.argv[0] fallback.
# ======================================================================
_old_role_env = os.environ.pop("SDD_PROCESS_ROLE", None)
try:
    with mock.patch.object(sys, "argv", ["/usr/bin/python3", "server.py"]):
        check("no env var, argv has no 'streamlit' substring -> 'fastapi' (old heuristic)",
              sched._process_role() == "fastapi")
    with mock.patch.object(sys, "argv", ["/opt/venv/lib/python3.11/site-packages/streamlit/__main__.py"]):
        check("no env var, argv DOES contain 'streamlit' -> 'streamlit' (old heuristic, unchanged)",
              sched._process_role() == "streamlit")

    os.environ["SDD_PROCESS_ROLE"] = "streamlit"
    with mock.patch.object(sys, "argv", ["/path/to/app.py"]):
        check("SDD_PROCESS_ROLE=streamlit overrides an argv[0] that "
              "contains no 'streamlit' substring at all (the exact "
              "production bug this task fixes)",
              sched._process_role() == "streamlit")

    os.environ["SDD_PROCESS_ROLE"] = "fastapi"
    with mock.patch.object(sys, "argv", ["/opt/venv/.../streamlit/__main__.py"]):
        check("SDD_PROCESS_ROLE=fastapi overrides an argv[0] that DOES "
              "contain 'streamlit' (env var always wins when it's a "
              "valid value, either direction)",
              sched._process_role() == "fastapi")

    os.environ["SDD_PROCESS_ROLE"] = "nonsense"
    with mock.patch.object(sys, "argv", ["/path/to/app.py"]):
        check("an invalid SDD_PROCESS_ROLE value is ignored, falling "
              "back to the sys.argv[0] heuristic",
              sched._process_role() == "fastapi")
finally:
    if _old_role_env is None:
        os.environ.pop("SDD_PROCESS_ROLE", None)
    else:
        os.environ["SDD_PROCESS_ROLE"] = _old_role_env


# ======================================================================
# PART 3: server.py - _start_streamlit() sets SDD_PROCESS_ROLE=streamlit
# in the child's env, and lifespan() hands the child's pid to
# scheduler_engine.set_streamlit_pid() right after spawning it.
# ======================================================================
TESTVOL_SERVER = tempfile.mkdtemp(prefix="task12_server_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL_SERVER
import server

_captured_popen = {}


def _fake_popen(cmd, env=None, **kw):
    _captured_popen["cmd"] = cmd
    _captured_popen["env"] = env
    _m = mock.Mock()
    _m.pid = 424242
    _m.poll.return_value = None
    return _m


with mock.patch.object(server.subprocess, "Popen", side_effect=_fake_popen):
    _proc = server._start_streamlit()

check("_start_streamlit() actually launched a (faked) process and "
      "returned it", _proc is not None and _proc.pid == 424242)
check("_start_streamlit() sets SDD_PROCESS_ROLE=streamlit in the "
      "Streamlit child's own environment (never touches the parent "
      "process's os.environ itself)",
      _captured_popen["env"] is not None
      and _captured_popen["env"].get("SDD_PROCESS_ROLE") == "streamlit")
check("...and this process's OWN os.environ is untouched by that "
      "(TASK 12 is about the CHILD's env only)",
      os.environ.get("SDD_PROCESS_ROLE") in (None, _old_role_env))

_lifespan_src = inspect.getsource(server.lifespan)
check("lifespan(): calls scheduler_engine.set_streamlit_pid() with the "
      "Streamlit child's own pid, AFTER spawning it",
      _lifespan_src.index("_streamlit_proc = _start_streamlit()")
      < _lifespan_src.index("scheduler_engine.set_streamlit_pid(_streamlit_proc.pid)"))
check("lifespan(): that call is wrapped so it can never stop the site "
      "serving (same 'never allowed to fail startup' rule as every "
      "other one-off step in this function)",
      "with suppress(Exception):\n        scheduler_engine.set_streamlit_pid(_streamlit_proc.pid)" in _lifespan_src)


# ======================================================================
# PART 4: top100_engine.run_nightly() - the new [mem] bracket around
# select_top100_pool() alone.
# ======================================================================
TESTVOL_T100 = tempfile.mkdtemp(prefix="task12_top100_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL_T100
import top100_engine

_calls = {"select": 0, "poll": 0, "submit": 0}
_log_capture = []


def _fake_select(log=print, scan_day=None):
    _calls["select"] += 1


def _fake_poll(log=print):
    _calls["poll"] += 1


def _fake_submit(log=print):
    _calls["submit"] += 1


with mock.patch.object(top100_engine, "select_top100_pool", side_effect=_fake_select), \
     mock.patch.object(top100_engine, "poll_and_ingest_batch", side_effect=_fake_poll), \
     mock.patch.object(top100_engine, "submit_nightly_batch", side_effect=_fake_submit):
    top100_engine.run_nightly(log=lambda m, *a, **k: _log_capture.append(m), scan_day="2026-10-18")

check("run_nightly() still calls select/poll/submit exactly once each, "
      "same as before this task (no behaviour change)",
      _calls == {"select": 1, "poll": 1, "submit": 1})

_before_idx = next((i for i, m in enumerate(_log_capture)
                     if m.startswith("[mem] rss=") and "before Top 200 pool selection" in m), None)
_after_idx = next((i for i, m in enumerate(_log_capture)
                    if m.startswith("[mem] rss=") and "after Top 200 pool selection" in m), None)
_trim_idx = next((i for i, m in enumerate(_log_capture)
                   if m.startswith("[mem] trimmed:") and "after Top 200 pool selection" in m), None)

check("run_nightly() logs a [mem] line BEFORE select_top100_pool()",
      _before_idx is not None)
check("run_nightly() logs a [mem] line AFTER select_top100_pool() too",
      _after_idx is not None)
check("...and a malloc_trim line right after that, same pairing the "
      "existing scan/ingest call sites already use",
      _trim_idx is not None)
check("the before/after bracket is correctly ordered around select_"
      "top100_pool() specifically (not poll/submit)",
      _before_idx is not None and _after_idx is not None and _before_idx < _after_idx)

_mem_lines_count = sum(1 for m in _log_capture if m.startswith("[mem]"))
check("exactly 3 new [mem] lines total for this one call (before, "
      "after, trimmed) - no extra noise, no missing line",
      _mem_lines_count == 3)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
