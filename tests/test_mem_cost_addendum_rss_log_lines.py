"""The optional small commit allowed by instruction_addendum_memory_cost.md
(9 Oct 2026, Director-directed, diagnosis-only addendum): a `[mem]`
log line written after each nightly scan (per universe), after a
failed scan attempt, and after a Top 200 batch poll/ingest actually
did something - so live memory can be read straight off Railway logs
without any new metrics system. Must have NO OTHER EFFECT: no change
to what gets scanned, saved, scored, or returned.

scheduler_engine._rss_gb() reads /proc/self/status's VmRSS line
directly (Linux-only, which is what Railway containers run) -
best-effort, returns None rather than raising on any platform/parse
issue. scheduler_engine._log_mem(log, label) wraps it into the
"[mem] rss=X.XXGB <label>" line (or "[mem] rss=unavailable <label>"
if RSS couldn't be read), so a log search for "[mem]" always finds
one line per call site.

Call sites (scheduler_engine.py): inside _run_nightly()'s per-universe
loop, right after each universe's scan (success or failure) - and
inside _run_top100_poll(), right after poll_and_ingest_batch() returns
a non-None result (an actual batch event, not every empty hourly poll).

Run: python3 tests/test_mem_cost_addendum_rss_log_lines.py
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
# _rss_gb() / _log_mem(): pure unit checks, no mocking needed.
# ---------------------------------------------------------------------
_rss = sched._rss_gb()
check("_rss_gb() returns a positive float on this (Linux) sandbox", isinstance(_rss, float) and _rss > 0)

_lines = []
sched._log_mem(lambda msg: _lines.append(msg), "after scan TEST UNIVERSE")
check("_log_mem() writes exactly one line", len(_lines) == 1)
check("_log_mem() line starts with '[mem] rss='", _lines[0].startswith("[mem] rss="))
check("_log_mem() line carries the label verbatim", "after scan TEST UNIVERSE" in _lines[0])

with mock.patch.object(sched, "_rss_gb", return_value=None):
    _lines2 = []
    sched._log_mem(lambda msg: _lines2.append(msg), "after scan X")
    check("_log_mem() degrades to 'rss=unavailable' when RSS can't be read, never raises",
          _lines2 == ["[mem] rss=unavailable after scan X"])

# ---------------------------------------------------------------------
# _run_nightly(): one [mem] line per universe, success and failure,
# and the existing side effects (snapshot/alert/insider) are completely
# unchanged by this addition - same mocking shape as tests/test_stage1b_
# private_universes.py's own T8 check.
# ---------------------------------------------------------------------
_fake_payload = {"rows": [{"Ticker": "TEST.AX", "Sector": "Industrials"}], "attention_lite": False}
_calls = {"snapshot": 0, "alert": 0, "insider": 0}
_log_capture = []


def _capture_log(msg, *a, **k):
    _log_capture.append(msg)


with mock.patch.object(nightly_scan, "run_universe_scan", return_value=_fake_payload), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None):
    import alert_engine
    import snapshot_store
    import insider_engine
    with mock.patch.object(alert_engine, "snapshot_previous_values", return_value={}), \
         mock.patch.object(alert_engine, "check_universe_rows",
                            side_effect=lambda *a, **k: _calls.__setitem__("alert", _calls["alert"] + 1)), \
         mock.patch.object(snapshot_store, "build_snapshots_from_scan",
                            side_effect=lambda *a, **k: _calls.__setitem__("snapshot", _calls["snapshot"] + 1)), \
         mock.patch.object(insider_engine, "refresh_universe",
                            side_effect=lambda *a, **k: _calls.__setitem__("insider", _calls["insider"] + 1)):
        sched._run_nightly({"universes": ["ASX 200"]}, log=_capture_log, run_night="2026-10-09")

_mem_lines = [m for m in _log_capture if m.startswith("[mem] rss=")]
check("_run_nightly() on a successful scan writes exactly one [mem] line", len(_mem_lines) == 1)
check("that [mem] line names the universe it ran for", "ASX 200" in _mem_lines[0] if _mem_lines else False)
check("_run_nightly()'s existing side effects are untouched by the [mem] addition "
      "(snapshot=1, alert=1, insider=1, same as before this commit)",
      _calls == {"snapshot": 1, "alert": 1, "insider": 1})

# Failure path: run_universe_scan raises -> still exactly one [mem] line,
# labelled "(failed)".
_log_capture2 = []
with mock.patch.object(nightly_scan, "run_universe_scan", side_effect=RuntimeError("boom")), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None):
    sched._run_nightly({"universes": ["ASX 200"]}, log=lambda m, *a, **k: _log_capture2.append(m),
                        run_night="2026-10-09")

_mem_lines2 = [m for m in _log_capture2 if m.startswith("[mem] rss=")]
check("_run_nightly() on a FAILED scan still writes exactly one [mem] line", len(_mem_lines2) == 1)
check("the failed-scan [mem] line says '(failed)'", "(failed)" in _mem_lines2[0] if _mem_lines2 else False)

# ---------------------------------------------------------------------
# _run_top100_poll(): a [mem] line only when poll_and_ingest_batch()
# actually returned something (not on an empty/no-op hourly poll).
# ---------------------------------------------------------------------
import top100_engine

_log_capture3 = []
with mock.patch.object(top100_engine, "poll_and_ingest_batch", return_value=None):
    sched._run_top100_poll(log=lambda m, *a, **k: _log_capture3.append(m))
check("_run_top100_poll() writes NO [mem] line when poll_and_ingest_batch() returns None (no-op poll)",
      not any(m.startswith("[mem]") for m in _log_capture3))

_log_capture4 = []
with mock.patch.object(top100_engine, "poll_and_ingest_batch", return_value={"scored": 0, "failed": 3}), \
     mock.patch.object(top100_engine.top100_store, "get_batch_state", return_value={"batch_id": "b1"}):
    sched._run_top100_poll(log=lambda m, *a, **k: _log_capture4.append(m))
_mem_lines4 = [m for m in _log_capture4 if m.startswith("[mem] rss=")]
check("_run_top100_poll() writes exactly one [mem] line when a batch was actually polled/ingested",
      len(_mem_lines4) == 1)
check("that [mem] line says 'after Top 200 batch ingest'",
      "after Top 200 batch ingest" in _mem_lines4[0] if _mem_lines4 else False)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
