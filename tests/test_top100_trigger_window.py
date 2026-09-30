"""
Owner-approved scheduler change (30 Sep 2026, from instruction_top100_
trigger_window.md): the Top 100 daily job must not skip a day when the
nightly scan overruns past 00:00 UTC.

Problem (scheduler_engine.py's old top100 trigger): fired on
`now.hour >= cfg["top100_hour"] and state.get("last_top100_date") !=
today` - the scheduler loop is single-threaded and blocks while the
nightly scan runs, so a scan that overruns past 00:00 UTC hands control
back with now.hour==0, the hour test fails, and Top 100 selection/
scoring for that scan silently doesn't run until 23:00 UTC the NEXT
day - a full day of stale Top 100 on the night it's needed most.

Fix: a scan-keyed window. _mark_top100_pending(scan_day, finished_at,
reason) is called by the due-scan/catch-up blocks in _loop() once the
nightly finishes (reason="completed") or hits its own hard timeout and
is abandoned (reason="timeout") - recording which run_night ("scan_day")
is now waiting for its own Top 100 pass. _top100_trigger_due(state, now,
top100_hour) reads that back: due when top100_pending_for is set (and
!= last_top100_date) AND now has reached scan_day's own top100_hour
gate in UTC - 23:00 the same day on a normal night, but on an overrun
night scan_day is still YESTERDAY (credited before midnight), so `now`
(already past midnight) is already past that gate and it fires on the
very first tick after the scan releases the lock.

Run: python3 tests/test_top100_trigger_window.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_trigger_window_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scheduler_engine as se

TOP100_HOUR = 23


def _reset_state():
    se._save_state({})


def _dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ======================================================================
# CHECK 1: normal night - scan ends 22:30 UTC (well before the 23:00
# gate) on the same calendar day it's credited to. Not due before the
# gate; due exactly at/after it.
# ======================================================================
_reset_state()
se._mark_top100_pending("2026-09-30", _dt(2026, 9, 30, 22, 30), "completed")
_state = se._load_state()
assert _state["top100_pending_for"] == "2026-09-30"
assert _state["top100_pending_finished_at"] == "22:30"
assert _state["top100_pending_reason"] == "completed"

assert se._top100_trigger_due(_state, _dt(2026, 9, 30, 22, 45), TOP100_HOUR) is False
assert se._top100_trigger_due(_state, _dt(2026, 9, 30, 23, 0), TOP100_HOUR) is True
print("[normal_night_gate] scan credited to today, finishes 22:30 -> not due at "
      "22:45, due at 23:00 (the plain hour gate, same as before this fix) OK")


# ======================================================================
# CHECK 2: overrun night - the nightly's own run_night is credited to
# YESTERDAY (captured before midnight, per nightly_scan.py's own
# run_night handling), but the job itself doesn't finish/get abandoned
# until 00:20 UTC the NEXT day. Must fire on the very first tick after
# that, same scan_day - never wait for the following 23:00.
# ======================================================================
_reset_state()
se._mark_top100_pending("2026-09-29", _dt(2026, 9, 30, 0, 20), "completed")
_state = se._load_state()
assert _state["top100_pending_for"] == "2026-09-29"

# The very next tick, moments after the scan releases the lock.
assert se._top100_trigger_due(_state, _dt(2026, 9, 30, 0, 21), TOP100_HOUR) is True
print("[overrun_night_fires_immediately] scan credited to 2026-09-29, actually "
      "finishes 00:20 UTC on 2026-09-30 -> due on the very next tick (00:21), not "
      "skipped until the following 23:00 - the exact bug this fix closes OK")


# ======================================================================
# CHECK 3: already-run guard - a second tick for the SAME scan_day,
# after the trigger already fired once (top100_pending_for cleared,
# last_top100_date set), must NOT fire again.
# ======================================================================
_reset_state()
se._mark_top100_pending("2026-09-30", _dt(2026, 9, 30, 22, 30), "completed")
_state = se._load_state()
assert se._top100_trigger_due(_state, _dt(2026, 9, 30, 23, 0), TOP100_HOUR) is True
# Simulate what the _loop() trigger block itself does once it fires.
_state["last_top100_date"] = _state["top100_pending_for"]
_state["top100_pending_for"] = None
_state["top100_pending_reason"] = None
se._save_state(_state)

_state2 = se._load_state()
assert se._top100_trigger_due(_state2, _dt(2026, 9, 30, 23, 30), TOP100_HOUR) is False
assert se._top100_trigger_due(_state2, _dt(2026, 10, 1, 5, 0), TOP100_HOUR) is False
print("[already_run_guard] after firing once for a scan_day, later ticks the same "
      "(or a later) day do NOT fire again - top100_pending_for was cleared OK")

# Even if a stale pending_for somehow reappeared for the ALREADY-credited
# scan_day, last_top100_date == pending_for alone still blocks it.
_state3 = dict(_state2)
_state3["top100_pending_for"] = "2026-09-30"
_state3["last_top100_date"] = "2026-09-30"
assert se._top100_trigger_due(_state3, _dt(2026, 9, 30, 23, 45), TOP100_HOUR) is False
print("[already_run_guard_last_top100_date] pending_for == last_top100_date alone "
      "blocks a re-fire, independent of whether pending_for was actually cleared OK")


# ======================================================================
# CHECK 4: a timed-out nightly still triggers Top 100 on whatever was
# saved - reason="timeout" is recorded and read back so the trigger
# block can log the "running after a timed-out nightly" line.
# ======================================================================
_reset_state()
se._mark_top100_pending("2026-09-30", _dt(2026, 9, 30, 21, 5), "timeout")
_state = se._load_state()
assert _state["top100_pending_reason"] == "timeout"
assert se._top100_trigger_due(_state, _dt(2026, 9, 30, 23, 0), TOP100_HOUR) is True
print("[timed_out_nightly_still_triggers] a nightly abandoned on its own hard "
      "timeout still marks its scan_day pending (reason='timeout') and the trigger "
      "fires normally at the hour gate OK")


# ======================================================================
# CHECK 5: a tick at 23:30 while the scan is STILL RUNNING (nothing has
# called _mark_top100_pending yet this scan_day - top100_pending_for is
# simply absent/None) must NOT fire, regardless of the wall-clock hour.
# The window must not depend on the loop happening to be blocked - this
# checks the pure function directly with no pending state at all.
# ======================================================================
_reset_state()
_state = se._load_state()
assert _state.get("top100_pending_for") is None
assert se._top100_trigger_due(_state, _dt(2026, 9, 30, 23, 30), TOP100_HOUR) is False
print("[no_fire_while_scan_still_running] with no scan_day marked pending yet "
      "(the scan hasn't finished), a 23:30 tick does not fire, independent of the "
      "scheduler loop's own single-threaded blocking behaviour OK")


# ======================================================================
# Supporting check: _loop() actually calls _mark_top100_pending() from
# both the due-scan and catch-up blocks - once on normal completion,
# once (with reason="timeout") in each block's own TimeoutError handler
# - and the top100 trigger block itself now calls _top100_trigger_due()
# instead of the old plain hour/date test.
# ======================================================================
import inspect
_loop_src = inspect.getsource(se._loop)
assert '_mark_top100_pending(today, datetime.now(timezone.utc), "completed")' in _loop_src
assert '_mark_top100_pending(today, datetime.now(timezone.utc), "timeout")' in _loop_src
assert '_mark_top100_pending(ref_night, datetime.now(timezone.utc), "completed")' in _loop_src
assert '_mark_top100_pending(ref_night, datetime.now(timezone.utc), "timeout")' in _loop_src
assert "_top100_trigger_due(state, now, cfg[\"top100_hour\"])" in _loop_src
print("[loop_wiring] _loop() calls _mark_top100_pending() from all 4 sites (due-scan "
      "completed/timeout, catch-up completed/timeout) and the top100 trigger block "
      "itself uses _top100_trigger_due(), not the old plain hour/date test OK")


# ======================================================================
# Grep-verify: no change to scheduler config this task explicitly says
# to leave alone.
# ======================================================================
_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scheduler_engine.py")).read()
import re
assert re.search(r'"top100_hour":\s*int\(os\.environ\.get\("TOP100_UTC_HOUR",\s*"23"\)\)', _src)
assert "_DEFAULT_NIGHTLY_UNIVERSES" in _src
assert "_parse_nightly_universes" in _src
assert "NIGHTLY_SCAN_UTC_HOUR" in _src
assert "CATCHUP_WINDOW_HOURS" in _src
print("[unrelated_config_untouched] TOP100_UTC_HOUR default (23), _DEFAULT_NIGHTLY_"
      "UNIVERSES, _parse_nightly_universes, NIGHTLY_SCAN_UTC_HOUR and CATCHUP_"
      "WINDOW_HOURS all still present/unchanged in scheduler_engine.py OK")

print("\nSWEEP_DONE")
