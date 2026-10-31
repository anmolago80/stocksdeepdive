"""
TASK 25 (Director-directed, 31 Oct 2026) - run the nightly reprice pass
in a short-lived child process + catch-up cool-down fix.

EVIDENCE (Railway logs): MALLOC_ARENA_MAX=2 did NOT stop the reprice
ratchet - 29 Oct reprice fastapi rss 0.58 -> 0.76GB, 30 Oct 0.81 ->
0.96GB, service 1.57GB now (peak 2.08GB) with no restart since 29 Oct.
TASK 21b showed Python-object growth of only ~14MB across the pass, so
the retained memory is native and Python cleanup can't free it.
Containment that doesn't depend on the exact cause: do the work in a
child process; when it exits, the OS reclaims everything.

PART A covers:
  1-2. The reprice loop runs in ONE child process (subprocess.Popen of
       a standalone entry-point script, reprice_child_entry.py - see
       its own module docstring, and scheduler_engine._run_reprice_
       pass_in_child()'s, for why this is subprocess rather than
       multiprocessing's "spawn": spawn's bootstrap re-executes
       whatever script is the CALLING process's own "__main__", which
       is app.py - UNGUARDED, calls _start_background_scheduler() at
       its own top level - whenever the Streamlit subprocess's own
       scheduler thread is the one that reaches the reprice pass that
       tick; a plain subprocess has no such ambient "__main__" at
       all). The child writes to the exact same scan_store files
       reprice_universe() always has - CHECK 1 proves this end to end
       with a REAL spawned subprocess against a real fixture,
       comparing its saved rows to the SAME fixture run through the
       in-process path.
  3. Log lines (the child's own "[nightly_scan] reprice ..."/"[mem]
     rss=..." lines) reach the parent's log, same format, plus one
     parent "[mem]" line after the child exits - CHECK 1 also proves
     this (same real end-to-end run).
  4. Safety: can't-start / crashed / timed-out all fall back to running
     the loop in-process, each logged clearly - CHECK 2-4, via a
     controllable fake subprocess.Popen object (deterministic, so each
     failure mode is exercised exactly once rather than trying to
     provoke a real crash/timeout/start-failure out of a real OS
     process - CHECK 1 already proves the real, happy-path subprocess
     end to end).
  5. The child never starts the scheduler/Streamlit/any other thread -
     CHECK 5, by source inspection (stripped of its own docstring, so
     prose mentioning those names for documentation doesn't false-
     positive the check) of what reprice_child_entry.py/_reprice_
     universes_loop() import/call.
  6. Parent RSS before/after the child, reported at the end (also in
     this task's own chat report).

PART B covers:
  7. The catch-up cool-down fix (scheduler_engine._loop()): a due-scan
     that trips the rate-limit breaker mid-tick must stop a catch-up
     in the SAME tick - proven exactly like TASK 23's own test proved
     the BUG existed, now proving the FIX (that same test file's own
     CHECK 9 was updated in place to match - this is the standalone,
     self-contained version of that same proof, per this task's own
     "Test: a due-scan that trips the breaker in a tick must stop a
     catch-up in that same tick" requirement).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access; the real end-to-end child-process test (CHECK 1) relies
on yfinance failing fast and gracefully (reprice_universe()'s own
"kept stale" fallback), same as every other live-fetch-shaped test in
this repo's own sweep.

Run: python3 tests/test_director_task25_reprice_child_process.py
"""
import inspect
import os
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task25_reprice_child_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scan_store
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


def _without_docstring(src):
    """Strips the function's own leading triple-quoted docstring
    before a source-text check - a docstring explaining what the code
    DOESN'T do (e.g. "never calls start()") would otherwise false-
    positive a plain substring check against the function's own
    prose, not its actual code."""
    return re.sub(r'^\s*""".*?"""', '', src, count=1, flags=re.DOTALL)


# ======================================================================
# CHECK 1 (items 1-3, 6): real end-to-end - a genuine subprocess
# reprices a fixture universe, its own log lines (including the per-
# universe "[mem]" line) arrive at the parent's log via stdout, the
# saved rows match what the SAME fixture produces through the in-
# process path, and the parent's own RSS is logged before/after.
# ======================================================================
scan_store.save_scan("TASK25 CHILD FIXTURE", [
    {"Ticker": "AAA", "Type": "STOCK", "Company Name": "AAA Co", "Price": 10.0,
     "Intrinsic Value": 12.0, "MOS %": 16.7, "Long Score": 50.0, "Quality": 60.0,
     "Psychology": 5.0, "Discovery (lite)": 25.0, "Moat": 40.0,
     "Growth Used": 0.05, "FCF Source": "reported", "Dividend Yield %": 1.0},
    {"Ticker": "BBB", "Type": "STOCK", "Company Name": "BBB Co", "Price": 20.0,
     "Intrinsic Value": 24.0, "MOS %": 16.7, "Long Score": 55.0, "Quality": 65.0,
     "Psychology": 5.0, "Discovery (lite)": 28.0, "Moat": 45.0,
     "Growth Used": 0.04, "FCF Source": "reported", "Dividend Yield %": 1.5},
], source_label="test fixture")
scan_store.save_scan("TASK25 INPROCESS FIXTURE", [
    {"Ticker": "AAA", "Type": "STOCK", "Company Name": "AAA Co", "Price": 10.0,
     "Intrinsic Value": 12.0, "MOS %": 16.7, "Long Score": 50.0, "Quality": 60.0,
     "Psychology": 5.0, "Discovery (lite)": 25.0, "Moat": 40.0,
     "Growth Used": 0.05, "FCF Source": "reported", "Dividend Yield %": 1.0},
    {"Ticker": "BBB", "Type": "STOCK", "Company Name": "BBB Co", "Price": 20.0,
     "Intrinsic Value": 24.0, "MOS %": 16.7, "Long Score": 55.0, "Quality": 65.0,
     "Psychology": 5.0, "Discovery (lite)": 28.0, "Moat": 45.0,
     "Growth Used": 0.04, "FCF Source": "reported", "Dividend Yield %": 1.5},
], source_label="test fixture")

_parent_rss_before = se._rss_gb()
_child_logs = []
_child_ok = se._run_reprice_pass_in_child(["TASK25 CHILD FIXTURE"], "2026-10-31", _child_logs.append)
_parent_rss_after = se._rss_gb()

check("the child process ran to completion (clean exit, stdout EOF)", _child_ok is True)
check("the per-universe '[nightly_scan] reprice ...' line came through the child's stdout",
      any("reprice TASK25 CHILD FIXTURE" in l for l in _child_logs))
check("the per-universe '[mem] rss=...' line came through, same format as the "
      "in-process path has always produced",
      any(l.startswith("[mem] rss=") and "after reprice TASK25 CHILD FIXTURE" in l
          for l in _child_logs))

_in_process_logs = []
se._reprice_universes_loop(["TASK25 INPROCESS FIXTURE"], "2026-10-31", _in_process_logs.append)

_child_payload = scan_store.load_scan_raw("TASK25 CHILD FIXTURE", allow_private=True)
_inprocess_payload = scan_store.load_scan_raw("TASK25 INPROCESS FIXTURE", allow_private=True)


def _by_ticker(rows):
    # Nothing here is expected to differ between the two runs - with
    # outbound network blocked in this sandbox, every row is "kept
    # stale" (reprice_universe()'s own documented fallback) and the
    # payload's own row list carries no per-row timestamp (only the
    # payload-LEVEL "repriced_at" does, via scan_store.reprice_scan() -
    # deliberately not compared here, since it's wall-clock, not row
    # content).
    return sorted(rows, key=lambda r: r["Ticker"])


check("the child process's own saved rows are IDENTICAL to the same fixture run "
      "through the in-process path (_reprice_universes_loop - no change to what "
      "gets saved or how prices/scores are computed, regardless of which process "
      "ran the loop)",
      _child_payload is not None and _inprocess_payload is not None and
      _by_ticker(_child_payload["rows"]) == _by_ticker(_inprocess_payload["rows"]))

print(f"[task25_c1_reprice_rss] parent RSS before child: {_parent_rss_before!r} GB, "
      f"after: {_parent_rss_after!r} GB (local fixture run, 2-ticker universe - see "
      f"this task's own chat report for the full before/after numbers)")
print("[task25_c1_child_vs_inprocess] real subprocess: logs relayed, saved rows "
      "identical to the in-process path OK")

# ======================================================================
# CHECK 2-4 (item 4, safety/fallback): a controllable fake standing in
# for subprocess.Popen itself - these tests are about _run_reprice_
# pass_in_child()'s OWN orchestration logic (can't-start / crashed /
# timed-out -> log clearly, return False), not about what the real
# child computes (CHECK 1 already proved that end to end with a
# genuine subprocess) - a fake makes each failure mode deterministic
# and fast instead of trying to provoke a real crash/timeout/start-
# failure out of a real OS process.
# ======================================================================
class _FakeStdin:
    def write(self, s):
        pass

    def close(self):
        pass


class _FakePopenBase:
    def __init__(self, *a, **k):
        self.stdin = _FakeStdin()
        self.stdout = iter([])  # EOF immediately - no lines at all

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class _FakeStartFailPopen:
    def __init__(self, *a, **k):
        raise OSError("fake: could not fork/exec")


class _FakeCrashPopen(_FakePopenBase):
    def wait(self, timeout=None):
        return 1  # exited non-zero, stdout already at EOF (no sentinel-shaped content)


class _FakeHangPopen(_FakePopenBase):
    def __init__(self, *a, **k):
        self.stdin = _FakeStdin()
        self.stdout = iter(())  # still "EOF" eventually from the reader thread's
        # own for-loop, but we never let the orchestrator get that far - see
        # the patched _REPRICE_CHILD_TIMEOUT_SECONDS below, which makes the
        # very first deadline check fail before any stdout read matters.

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


_c2_logs = []
with mock.patch.object(se.subprocess, "Popen", side_effect=_FakeStartFailPopen):
    _c2_result = se._run_reprice_pass_in_child(["X"], "2026-10-31", _c2_logs.append)
check("child process CAN'T START -> returns False (falls back)", _c2_result is False)
check("the can't-start case is logged clearly",
      any("failed to start" in l for l in _c2_logs))
print("[task25_c2_cant_start_fallback] logged clearly, returns False OK")

_c3_logs = []
with mock.patch.object(se.subprocess, "Popen", new=_FakeCrashPopen):
    _c3_result = se._run_reprice_pass_in_child(["X"], "2026-10-31", _c3_logs.append)
check("child process CRASHES (stdout closes immediately, exit code non-zero) -> "
      "returns False (falls back)", _c3_result is False)
check("the crash case is logged clearly, naming the exit code",
      any("exited with code 1" in l for l in _c3_logs))
print("[task25_c3_crash_fallback] logged clearly, returns False OK")

_c4_logs = []
with mock.patch.object(se.subprocess, "Popen", new=_FakeHangPopen), \
     mock.patch.object(se, "_REPRICE_CHILD_TIMEOUT_SECONDS", -1):
    # A negative timeout guarantees the very first deadline check (time.time()
    # + (-1), compared against "now" a moment later) is already in the past,
    # so the orchestrator's own timeout branch fires deterministically -
    # never racing the (separate) reader thread's own near-instant EOF on
    # this fake's empty stdout iterator.
    _c4_result = se._run_reprice_pass_in_child(["X"], "2026-10-31", _c4_logs.append)
check("child process TIMES OUT (deadline already elapsed before stdout EOF is "
      "even reached) -> returns False (falls back)", _c4_result is False)
check("the timeout case is logged clearly, naming the timeout bound",
      any("exceeded" in l and "-1" in l for l in _c4_logs))
print("[task25_c4_timeout_fallback] logged clearly, returns False OK")

# The caller's own contract (_run_nightly()'s reprice-pass call site):
# every one of the three failure modes above must still result in the
# SAME loop running in-process - proven here directly against the real
# _reprice_universes_loop(), matching what the real call site does.
scan_store.save_scan("TASK25 FALLBACK FIXTURE", [
    {"Ticker": "CCC", "Type": "STOCK", "Company Name": "CCC Co", "Price": 5.0,
     "Intrinsic Value": 6.0, "MOS %": 16.7, "Long Score": 40.0, "Quality": 50.0,
     "Psychology": 5.0, "Discovery (lite)": 20.0, "Moat": 30.0,
     "Growth Used": 0.03, "FCF Source": "reported", "Dividend Yield %": 2.0},
], source_label="test fixture")
with mock.patch.object(se.subprocess, "Popen", side_effect=_FakeStartFailPopen):
    _fallback_ok = se._run_reprice_pass_in_child(["TASK25 FALLBACK FIXTURE"], "2026-10-31", lambda *a: None)
    if not _fallback_ok:
        se._reprice_universes_loop(["TASK25 FALLBACK FIXTURE"], "2026-10-31", lambda *a: None)
_fallback_payload = scan_store.load_scan_raw("TASK25 FALLBACK FIXTURE", allow_private=True)
check("a reprice is never skipped just because the child path failed - the "
      "fallback in-process run still produced a real repriced/kept-stale payload",
      _fallback_payload is not None and len(_fallback_payload["rows"]) == 1)
print("[task25_c4b_fallback_still_reprices] can't-start -> in-process fallback "
      "still completes the reprice OK")

# ======================================================================
# CHECK 5 (item 5): the child's own entry point never imports/calls
# anything that starts a second scheduler loop, the Streamlit
# subprocess, or any other background thread.
# ======================================================================
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "reprice_child_entry.py")) as _f:
    _entry_src_full = _f.read()
_entry_src_code_only = _without_docstring(_entry_src_full)
check("reprice_child_entry.py's own CODE (docstring excluded) never calls "
      "scheduler_engine.start()/start_with_retry(), and never imports app.py/server.py",
      "scheduler_engine.start(" not in _entry_src_code_only
      and "start_with_retry" not in _entry_src_code_only
      and "import app" not in _entry_src_code_only
      and "import server" not in _entry_src_code_only
      # it DOES, correctly, import scheduler_engine itself (for
      # _reprice_universes_loop) - confirm that didn't get stripped too
      and "import scheduler_engine" in _entry_src_code_only)

_loop_src = _without_docstring(inspect.getsource(se._reprice_universes_loop))
check("_reprice_universes_loop() (what the child actually runs) never starts a "
      "thread, subprocess, or scheduler of its own - only calls nightly_scan."
      "reprice_universe() plus the same mem-logging helpers the in-process path "
      "always called",
      "threading.Thread" not in _loop_src and "subprocess" not in _loop_src
      and ".start(" not in _loop_src)
print("[task25_c5_no_extra_background_work] child entry point starts nothing "
      "beyond the reprice loop itself OK")

# ======================================================================
# CHECK 6 (part B, item 7): the catch-up cool-down fix - a due-scan
# that trips the rate-limit breaker mid-tick must stop a catch-up in
# the SAME tick. Self-contained version of the same proof TASK 23's own
# test file now carries (its CHECK 9 was updated in place to match this
# fix) - see that file's own updated comment for the full root-cause
# writeup; this is the standalone proof for THIS task's own stated
# test requirement.
# ======================================================================
class _StopLoopAfterOneTick(BaseException):
    pass


_b_nightly_calls = []


def _b_fake_run_nightly(cfg_arg, lg, run_night=None, cancel_event=None):
    _b_nightly_calls.append({"run_night": run_night, "universes": list(cfg_arg.get("universes", []))})
    if run_night is None:
        # The due-scan call trips the breaker mid-run, recording a
        # fresh cool-down - exactly the real incident's own sequence.
        se._record_rate_limit_cooldown(lg)


_b_fake_cfg = {
    "enabled": True,
    "universes": ["ASX 200", "ASX 300"],
    "universe_cadence": {"ASX 200": "daily", "ASX 300": "daily"},
    "scan_hour": 0,
    "digest_weekday": 99, "digest_hour": 99, "watchdog_hour": 99,
    "earnings_refresh_weekday": 99, "earnings_refresh_hour": 99,
    "backup_hour": 99, "volume_check_hour": 99, "top100_hour": 99,
}


def _b_fake_acquire_lock(job_name, log):
    return job_name == "nightly"


_b_logs = []
import scan_store as _b_scan_store_mod

with mock.patch.object(se, "_cfg", return_value=_b_fake_cfg), \
     mock.patch.object(se, "_acquire_job_lock", side_effect=_b_fake_acquire_lock), \
     mock.patch.object(se, "_universes_needing_scan", return_value=["ASX 200"]), \
     mock.patch.object(se, "_catchup_reference_night", return_value="2026-10-31"), \
     mock.patch.object(se, "_universes_missing_today", return_value=["ASX 300"]), \
     mock.patch.object(_b_scan_store_mod, "load_scan", return_value=[{"Ticker": "FAKE"}]), \
     mock.patch.object(se, "_run_nightly", side_effect=_b_fake_run_nightly), \
     mock.patch.object(se.time, "sleep", side_effect=_StopLoopAfterOneTick):
    try:
        se._loop(_b_logs.append)
    except _StopLoopAfterOneTick:
        pass

check("FIX: only the due-scan's own _run_nightly() call happened in this tick - "
      "the catch-up block did NOT also call it despite the due-scan having just "
      "tripped the breaker moments earlier, same tick",
      len(_b_nightly_calls) == 1 and _b_nightly_calls[0]["run_night"] is None)
check("the catch-up block's own cool-down-skip line fired, reading the cool-down "
      "fresh right before its own decision instead of a stale tick-start snapshot",
      any("catch-up scan skipped - rate-limit cool-down" in l for l in _b_logs))
print("[task25_c6_partB_catchup_cooldown_fix] due-scan trips breaker mid-tick -> "
      "same-tick catch-up correctly stops OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
