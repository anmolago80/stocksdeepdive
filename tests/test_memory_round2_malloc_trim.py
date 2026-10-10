"""Memory addendum, round 2 (9 Oct 2026, Director-directed, build go),
item (a): glibc malloc_trim(0) via ctypes after each nightly universe
scan and after each Top 200 batch submit/ingest - asks the C allocator
to return freed-but-retained heap arenas to the OS (see MEMORY_COST_
ADDENDUM_REPORT.md's own cause #3: CPython/glibc rarely does this on
its own after a scan/batch's burst of allocations, which run in-
process).

Linux-only, best-effort, must never raise, logs "[mem] trimmed: rss
before -> after <label>". Must NOT run inside a per-ticker loop - only
once per universe/batch, same call sites as the round-1 [mem] rss=
lines (scheduler_engine._log_mem).

Run: python3 tests/test_memory_round2_malloc_trim.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler_engine as sched
import nightly_scan

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


# ---------------------------------------------------------------------
# _malloc_trim_and_log(): pure unit checks.
# ---------------------------------------------------------------------
_lines = []
with mock.patch.object(sched.sys, "platform", "linux"):
    sched._malloc_trim_and_log(lambda m: _lines.append(m), "after scan TEST")
check("on Linux, _malloc_trim_and_log() writes exactly one line (ctypes/malloc_trim "
      "genuinely available in this sandbox)", len(_lines) == 1)
check("the line starts with '[mem] trimmed: rss'", _lines[0].startswith("[mem] trimmed: rss") if _lines else False)
check("the line carries the label verbatim", "after scan TEST" in _lines[0] if _lines else False)

_lines_non_linux = []
with mock.patch.object(sched.sys, "platform", "darwin"):
    sched._malloc_trim_and_log(lambda m: _lines_non_linux.append(m), "after scan X")
check("on a non-Linux platform, _malloc_trim_and_log() writes NO line at all (never fakes a trim)",
      _lines_non_linux == [])

# A broken/missing libc must never raise and must never crash the caller.
_lines_broken = []
with mock.patch.object(sched.sys, "platform", "linux"), \
     mock.patch("ctypes.CDLL", side_effect=OSError("no such library")):
    try:
        sched._malloc_trim_and_log(lambda m: _lines_broken.append(m), "after scan Y")
        _raised = False
    except Exception:
        _raised = True
check("a broken/missing libc never raises out of _malloc_trim_and_log()", _raised is False)
check("...and produces no log line in that case (nothing to report)", _lines_broken == [])

# A malloc_trim call that itself raises (e.g. attribute missing) must
# also never escape.
_lines_attr_missing = []


class _FakeLibNoTrim:
    pass


with mock.patch.object(sched.sys, "platform", "linux"), \
     mock.patch("ctypes.CDLL", return_value=_FakeLibNoTrim()):
    try:
        sched._malloc_trim_and_log(lambda m: _lines_attr_missing.append(m), "after scan Z")
        _raised2 = False
    except Exception:
        _raised2 = True
check("a libc object with no malloc_trim attribute never raises either", _raised2 is False)

# ---------------------------------------------------------------------
# Call-site wiring: once per universe in _run_nightly() (success AND
# failure paths), never inside the per-ticker loop itself.
# ---------------------------------------------------------------------
_fake_payload = {"rows": [{"Ticker": "TEST.AX", "Sector": "Industrials"}], "attention_lite": False}
_trim_calls = []

with mock.patch.object(nightly_scan, "run_universe_scan", return_value=_fake_payload), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None), \
     mock.patch.object(sched, "_malloc_trim_and_log",
                        side_effect=lambda log, label: _trim_calls.append(label)):
    import alert_engine
    import snapshot_store
    import insider_engine
    with mock.patch.object(alert_engine, "snapshot_previous_values", return_value={}), \
         mock.patch.object(alert_engine, "check_universe_rows", return_value=None), \
         mock.patch.object(snapshot_store, "build_snapshots_from_scan", return_value=None), \
         mock.patch.object(insider_engine, "refresh_universe", return_value=None):
        sched._run_nightly({"universes": ["ASX 200"]}, log=lambda *a, **k: None, run_night="2026-10-10")

check("_run_nightly() on a successful scan calls _malloc_trim_and_log() exactly once "
      "(once per universe, not once per ticker)", _trim_calls == ["after scan ASX 200"])

_trim_calls_fail = []
with mock.patch.object(nightly_scan, "run_universe_scan", side_effect=RuntimeError("boom")), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None), \
     mock.patch.object(sched, "_malloc_trim_and_log",
                        side_effect=lambda log, label: _trim_calls_fail.append(label)):
    sched._run_nightly({"universes": ["ASX 200"]}, log=lambda *a, **k: None, run_night="2026-10-10")
check("_run_nightly() on a FAILED scan also calls _malloc_trim_and_log() exactly once",
      _trim_calls_fail == ["after scan ASX 200 (failed)"])

# ---------------------------------------------------------------------
# Call-site wiring: after Top 200 batch ingest, and after submit.
# ---------------------------------------------------------------------
import top100_engine

_trim_calls_top100 = []
with mock.patch.object(top100_engine, "poll_and_ingest_batch",
                        return_value={"scored": 0, "failed": 3}), \
     mock.patch.object(top100_engine.top100_store, "get_batch_state", return_value={"batch_id": "b1"}), \
     mock.patch.object(sched, "_malloc_trim_and_log",
                        side_effect=lambda log, label: _trim_calls_top100.append(label)):
    sched._run_top100_poll(log=lambda *a, **k: None)
check("_run_top100_poll() calls _malloc_trim_and_log() once after an actual ingest",
      _trim_calls_top100 == ["after Top 200 batch ingest"])

_trim_calls_submit = []
with mock.patch.object(top100_engine, "poll_and_ingest_batch", return_value={"scored": 5, "failed": 0}), \
     mock.patch.object(top100_engine.top100_store, "get_batch_state", return_value=None), \
     mock.patch.object(top100_engine, "submit_nightly_batch", return_value=None), \
     mock.patch.object(sched, "_malloc_trim_and_log",
                        side_effect=lambda log, label: _trim_calls_submit.append(label)):
    sched._run_top100_poll(log=lambda *a, **k: None)
check("_run_top100_poll() calls _malloc_trim_and_log() for BOTH ingest and submit when a "
      "new batch is actually submitted",
      _trim_calls_submit == ["after Top 200 batch ingest", "after Top 200 batch submit"])

_trim_calls_noop = []
with mock.patch.object(top100_engine, "poll_and_ingest_batch", return_value=None), \
     mock.patch.object(sched, "_malloc_trim_and_log",
                        side_effect=lambda log, label: _trim_calls_noop.append(label)):
    sched._run_top100_poll(log=lambda *a, **k: None)
check("_run_top100_poll() calls _malloc_trim_and_log() NOT AT ALL on an empty no-op poll",
      _trim_calls_noop == [])

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
