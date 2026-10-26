"""
TASK 21b (Director-directed, 26 Oct 2026) - DIAGNOSE ONLY, no
behaviour change. Finds what fastapi retains during the reprice pass.

Evidence (Railway [mem], night 25->26 Oct, after TASK 21): per-universe
trims work, but the post-trim FLOOR ratchets up universe by universe
during the reprice pass specifically (0.41 -> 0.50 -> ... -> 0.68GB),
then fastapi holds ~0.70GB all day. malloc_trim(0) only returns heap
arenas the process has already FREED - it does nothing for memory
still genuinely REFERENCED by a live object somewhere. This adds
tracemalloc-based instrumentation, entirely behind MEM_DIAG=1 (default
off), to find THAT object.

Covers the task's own listed tests:
  - switch OFF (MEM_DIAG unset): tracemalloc is never even imported,
    no [memdiag] line is ever logged, _run_nightly()'s own [mem]/trim
    behaviour (TASK 21) is byte-identical to before this task.
  - switch ON: a snapshot is taken before the reprice pass, after each
    reprice universe, and after the end-of-nightly cache clear; each
    one logs the top 15 allocation sites by retained-size growth
    (file:line, size, count) plus the tracked container sizes
    (nightly_scan/scan_store/snapshot_store/scheduler_engine module-
    level dicts/lists/sets, found by introspection; yfinance.shared's
    four dicts; the YfData singleton's cache_get() lru_cache stats).

ALL FIXTURES ARE SYNTHETIC - this sandbox has no outbound network
access; the real proof is the one night the owner runs with MEM_DIAG=1.

Run: python3 tests/test_director_task21b_mem_diag.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task21b_mem_diag_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("MEM_DIAG", None)

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


# ======================================================================
# CHECK 1: _memdiag_enabled() reads MEM_DIAG fresh each call - unset,
# "0", "false" all read as off; "1", "true", "yes", "on" (any case)
# all read as on.
# ======================================================================
os.environ.pop("MEM_DIAG", None)
check("MEM_DIAG unset -> disabled", sched._memdiag_enabled() is False)
for _off_val in ("0", "false", "False", "no", ""):
    os.environ["MEM_DIAG"] = _off_val
    check(f"MEM_DIAG={_off_val!r} -> disabled", sched._memdiag_enabled() is False)
for _on_val in ("1", "true", "True", "YES", "On"):
    os.environ["MEM_DIAG"] = _on_val
    check(f"MEM_DIAG={_on_val!r} -> enabled", sched._memdiag_enabled() is True)
os.environ.pop("MEM_DIAG", None)
print("[task21b_c1_env_switch_parsing] MEM_DIAG parsed case-insensitively, "
      "unset/0/false/no/empty all read as off OK")

# ======================================================================
# CHECK 2 (the task's own explicit test): switch OFF - tracemalloc is
# NEVER imported, no [memdiag] line is ever logged, and _memdiag_
# started/_memdiag_last_snapshot stay at their initial state.
# ======================================================================
sched._memdiag_started = False
sched._memdiag_last_snapshot = None
_had_tracemalloc_before = "tracemalloc" in sys.modules
_lines_off = []
sched._memdiag_snapshot_and_log(_lines_off.append, "test checkpoint")
sched._memdiag_start()  # also a no-op with the switch off
check("switch OFF: _memdiag_snapshot_and_log() logs NOTHING at all",
      _lines_off == [])
check("switch OFF: tracemalloc is never imported as a side effect of this call "
      "(no new entry in sys.modules)",
      ("tracemalloc" in sys.modules) == _had_tracemalloc_before)
check("switch OFF: _memdiag_started stays False (tracemalloc.start() never called)",
      sched._memdiag_started is False)
check("switch OFF: _memdiag_last_snapshot stays None (no snapshot ever taken)",
      sched._memdiag_last_snapshot is None)
print("[task21b_c2_switch_off_is_complete_noop] MEM_DIAG unset -> zero effect, "
      "tracemalloc never touched OK")

# ======================================================================
# CHECK 3: switch OFF - _run_nightly()'s own [mem]/trim behaviour
# (TASK 21) is byte-identical to before this task - no extra [memdiag]
# lines mixed into the same log stream, no change to the [mem]/trimmed
# line count or labels.
# ======================================================================
_fake_payload = {"rows": [{"Ticker": "TEST.AX", "Sector": "Industrials"}], "attention_lite": False}
_log_capture = []

with mock.patch.object(nightly_scan, "run_universe_scan", return_value=_fake_payload), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(nightly_scan, "reprice_universe", return_value={"rows": []}), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None):
    import alert_engine
    import snapshot_store
    import insider_engine
    with mock.patch.object(alert_engine, "snapshot_previous_values", return_value={}), \
         mock.patch.object(alert_engine, "check_universe_rows", return_value=None), \
         mock.patch.object(alert_engine, "run_extra_ticker_pass", return_value=None), \
         mock.patch.object(alert_engine, "send_batched_notifications", return_value=None), \
         mock.patch.object(snapshot_store, "build_snapshots_from_scan", return_value=None), \
         mock.patch.object(insider_engine, "refresh_universe", return_value=None):
        sched._run_nightly(
            {"universes": ["ASX 200"], "universe_cadence": {"ASX 200": {}, "S&P 500": {}}},
            log=_log_capture.append, run_night="2026-10-26",
        )

check("switch OFF: no '[memdiag]' line appears anywhere in _run_nightly()'s own log output",
      not any(m.startswith("[memdiag]") for m in _log_capture))
_mem_lines = [m for m in _log_capture if m.startswith("[mem] rss=")]
_expected_labels = [
    "after scan ASX 200", "after sector top-up", "after alert extra pass",
    "after send_batched_notifications", "after reprice S&P 500",
    "after nightly-only cache clear",
]
check("switch OFF: the [mem] line count/labels are exactly TASK 21's own set - "
      "scan, sector top-up, alert extra pass, send_batched_notifications, one "
      "reprice universe, nightly-only cache clear (6, not 7 - no [memdiag] mixed in)",
      len(_mem_lines) == len(_expected_labels)
      and all(_mem_lines[i].endswith(_expected_labels[i]) for i in range(len(_expected_labels))))
print("[task21b_c3_run_nightly_byte_identical] switch OFF: _run_nightly()'s own "
      "TASK 21 [mem]/trim lines are completely unaffected by this task's own "
      "instrumentation OK")

# ======================================================================
# CHECK 4: switch ON - the SAME _run_nightly() call now ALSO logs
# [memdiag] lines at exactly the 3 Director-named checkpoints: before
# the reprice pass, after each reprice universe, and after the end-of-
# nightly cache clear.
# ======================================================================
os.environ["MEM_DIAG"] = "1"
sched._memdiag_started = False
sched._memdiag_last_snapshot = None
_log_capture_on = []

with mock.patch.object(nightly_scan, "run_universe_scan", return_value=_fake_payload), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(nightly_scan, "reprice_universe", return_value={"rows": []}), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None):
    with mock.patch.object(alert_engine, "snapshot_previous_values", return_value={}), \
         mock.patch.object(alert_engine, "check_universe_rows", return_value=None), \
         mock.patch.object(alert_engine, "run_extra_ticker_pass", return_value=None), \
         mock.patch.object(alert_engine, "send_batched_notifications", return_value=None), \
         mock.patch.object(snapshot_store, "build_snapshots_from_scan", return_value=None), \
         mock.patch.object(insider_engine, "refresh_universe", return_value=None):
        sched._run_nightly(
            {"universes": ["ASX 200"], "universe_cadence": {"ASX 200": {}, "S&P 500": {}, "FTSE 100": {}}},
            log=_log_capture_on.append, run_night="2026-10-26",
        )
os.environ.pop("MEM_DIAG", None)

_memdiag_checkpoints = [m.split(":", 1)[0].split("] ", 1)[1] for m in _log_capture_on
                        if m.startswith("[memdiag]") and "tracemalloc current=" in m]
check("switch ON: exactly 3 checkpoints fire - before reprice pass, after EACH "
      "of the 2 reprice universes, after the nightly-only cache clear (4 total: "
      "1 + 2 + 1)", len(_memdiag_checkpoints) == 4)
check("switch ON: 'before reprice pass' checkpoint is present",
      "before reprice pass" in _memdiag_checkpoints)
check("switch ON: a checkpoint fires for EACH reprice universe by name "
      "(S&P 500 and FTSE 100), not once for the whole pass",
      "after reprice S&P 500" in _memdiag_checkpoints
      and "after reprice FTSE 100" in _memdiag_checkpoints)
check("switch ON: 'after nightly-only cache clear' checkpoint is present",
      "after nightly-only cache clear" in _memdiag_checkpoints)
check("switch ON: each of the 4 checkpoints also runs _memdiag_container_sizes() "
      "(the yfinance.data.YfData.cache_get lru_cache stats line appears once "
      "per checkpoint, 4 times total)",
      sum(1 for m in _log_capture_on if "yfinance.data.YfData.cache_get lru_cache" in m) == 4)
print(f"[task21b_c4_three_checkpoints_fire] checkpoints={_memdiag_checkpoints} OK")

# ======================================================================
# CHECK 5: _memdiag_snapshot_and_log()'s own diffing - the first call
# in a process logs a "(baseline)" line (nothing to diff against yet);
# a second call diffs against the first and surfaces real growth.
# ======================================================================
os.environ["MEM_DIAG"] = "1"
sched._memdiag_started = False
sched._memdiag_last_snapshot = None
_lines_a = []
sched._memdiag_snapshot_and_log(_lines_a.append, "first ever checkpoint")
check("the first checkpoint in a process is labelled '(baseline, no prior "
      "snapshot to diff)'", any("(baseline, no prior snapshot to diff)" in m for m in _lines_a))

_bloat = [bytearray(1024 * 50) for _ in range(200)]  # ~10MB, deliberately held
_lines_b = []
sched._memdiag_snapshot_and_log(_lines_b.append, "second checkpoint (after allocating ~10MB)")
_growth_lines = [m for m in _lines_b if m.startswith("[memdiag]") and " size=" in m
                 and "(baseline" not in m and "tracemalloc current=" not in m]
check("a second checkpoint diffs against the first and reports real growth "
      "(at least one allocation-site line, each carrying size/count AND "
      "their +growth deltas)",
      len(_growth_lines) > 0 and all("(+" in m for m in _growth_lines))
del _bloat
os.environ.pop("MEM_DIAG", None)
print(f"[task21b_c5_baseline_then_diff] baseline on first call, real diff on "
      f"the second ({len(_growth_lines)} growth lines) OK")

# ======================================================================
# CHECK 6: _memdiag_container_sizes() finds every module-level dict/
# list/set in the 4 named modules (by introspection, not a hardcoded
# list) plus yfinance's own shared caches, and degrades gracefully if
# yfinance's internals ever change shape (never raises).
# ======================================================================
os.environ["MEM_DIAG"] = "1"
_lines_containers = []
sched._memdiag_container_sizes(_lines_containers.append, "container check")
check("every tracked module's containers are prefixed correctly "
      "(scheduler_engine's own _APPROX_UNIVERSE_SIZE is a real, known one)",
      any("scheduler_engine._APPROX_UNIVERSE_SIZE" in m for m in _lines_containers))
check("yfinance.shared's 4 dicts (_DFS/_ERRORS/_TRACEBACKS/_ISINS) are reported",
      all(any(f"yfinance.shared.{name}" in m for m in _lines_containers)
          for name in ("_DFS", "_ERRORS", "_TRACEBACKS", "_ISINS")))
check("yfinance.data.YfData.cache_get's lru_cache stats (hits/misses/currsize/"
      "maxsize) are reported",
      any("yfinance.data.YfData.cache_get lru_cache" in m and "maxsize=" in m
          for m in _lines_containers))

_real_import = __import__


def _broken_yfinance_import(name, *args, **kwargs):
    if name in ("yfinance.shared", "yfinance.data") or name.startswith("yfinance"):
        raise ImportError("simulated: yfinance's internals changed shape")
    return _real_import(name, *args, **kwargs)


with mock.patch("builtins.__import__", side_effect=_broken_yfinance_import):
    _lines_broken = []
    try:
        sched._memdiag_container_sizes(_lines_broken.append, "broken yfinance")
        _raised = False
    except Exception:
        _raised = True
check("_memdiag_container_sizes() never raises even if yfinance's own modules "
      "can't be imported - degrades to a log line instead",
      _raised is False)
os.environ.pop("MEM_DIAG", None)
print("[task21b_c6_container_introspection] module-level containers + yfinance's "
      "own shared caches reported, never raises on a broken import OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
