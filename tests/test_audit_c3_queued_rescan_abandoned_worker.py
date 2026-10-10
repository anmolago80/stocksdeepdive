"""
Audit fixes, Commit 3 (owner-directed, 30 Sep 2026, per instruction_
tonight_sequence_v2.md step 2). Covers:

  - _queue_rescan_request()/_rescan_request_status(): write/read the
    request, refuse an empty universe list, refuse a second request
    while one is already pending.
  - _run_queued_rescan_tick(): a queued request runs through
    _record_job() (heartbeat/lock), transitions queued -> running ->
    cleared + rescan_last_run; skipped while rate-limit cooled down,
    while another process holds the lock, or while an abandoned worker
    is still alive.
  - _record_job()'s own abandoned-worker registration on a real hard
    timeout: the still-alive worker thread is tracked so a second scan
    cannot start while it's alive, and the tracked cancel_event gets
    nudged.
  - _abandoned_worker_status(): alive / expired (past ABANDONED_WORKER_
    MAX_HOURS) / cleared once the thread actually exits.
  - nightly_scan.run_universe_scan()'s cooperative cancel_event: the
    per-ticker loop stops within one ticker of the flag being set,
    checkpoints first, and returns None.

Run: python3 tests/test_audit_c3_queued_rescan_abandoned_worker.py
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="audit_c3_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scheduler_engine as se
import nightly_scan
import scan_checkpoint_store


# ======================================================================
# CHECK 1: _queue_rescan_request() / _rescan_request_status()
# ======================================================================
ok, reason = se._queue_rescan_request([], requested_by="owner@x.com", log=lambda *a: None)
assert ok is False and "Select at least one" in reason, (ok, reason)
print("[c3_queue_empty_refused] an empty universe list is refused OK")

ok, reason = se._queue_rescan_request(
    ["Dow Jones 30", "S&P 500"], requested_by="owner@x.com", log=lambda *a: None)
assert ok is True and reason is None, (ok, reason)
status = se._rescan_request_status()
assert status["pending"]["universes"] == ["Dow Jones 30", "S&P 500"]
assert status["pending"]["status"] == "queued"
assert status["pending"]["requested_by"] == "owner@x.com"
assert status["last_run"] is None
print("[c3_queue_written] a queued request is written and readable via "
      "_rescan_request_status() OK")

ok2, reason2 = se._queue_rescan_request(["ASX 200"], requested_by="owner@x.com", log=lambda *a: None)
assert ok2 is False and "already" in reason2, (ok2, reason2)
print("[c3_queue_refuses_second] a second request is refused while one is "
      "already pending OK")

# Clear it for the next checks.
_clear_state = se._load_state()
_clear_state.pop("rescan_request", None)
se._save_state(_clear_state)


# ======================================================================
# CHECK 2: _run_queued_rescan_tick() - happy path runs through
# _record_job(), transitions queued -> running -> cleared + last_run.
# ======================================================================
se._queue_rescan_request(["Dow Jones 30"], requested_by="owner@x.com", log=lambda *a: None)

_ran_with = {}


def _fake_run_nightly(cfg, log, run_night=None, cancel_event=None):
    _ran_with["universes"] = cfg["universes"]
    _ran_with["cancel_event"] = cancel_event
    log("ok")


with mock.patch.object(se, "_run_nightly", side_effect=_fake_run_nightly):
    logs = []
    se._run_queued_rescan_tick(
        {"universes": [], "universe_cadence": {}}, logs.append,
        rl_cooldown_active=False, rl_cooldown_until=None,
        abandoned_status=None, abandoned_info=None,
    )

assert _ran_with["universes"] == ["Dow Jones 30"], _ran_with
assert isinstance(_ran_with["cancel_event"], threading.Event)
assert any("starting queued rescan" in l for l in logs), logs
status = se._rescan_request_status()
assert status["pending"] is None, status
assert status["last_run"]["universes"] == ["Dow Jones 30"]
assert "finished_at" in status["last_run"]
print("[c3_tick_happy_path] a queued request runs through _record_job()/"
      "_run_nightly() and is cleared into rescan_last_run OK")


# ======================================================================
# CHECK 3: _run_queued_rescan_tick() skips - rate-limit cooldown,
# another process holding the lock, and an abandoned worker alive.
# ======================================================================
se._queue_rescan_request(["S&P 500"], requested_by="owner@x.com", log=lambda *a: None)

with mock.patch.object(se, "_run_nightly") as m_run:
    logs3 = []
    se._run_queued_rescan_tick(
        {"universes": [], "universe_cadence": {}}, logs3.append,
        rl_cooldown_active=True, rl_cooldown_until="2026-09-30T09:00:00+00:00",
        abandoned_status=None, abandoned_info=None,
    )
assert not m_run.called
assert any("rate-limit cool-down" in l for l in logs3), logs3
# still queued - the tick above must not have consumed it
assert se._rescan_request_status()["pending"]["universes"] == ["S&P 500"]
print("[c3_tick_skips_rl_cooldown] a queued rescan is skipped (and stays "
      "queued) during a rate-limit cool-down OK")

with mock.patch.object(se, "_acquire_job_lock", return_value=False), \
     mock.patch.object(se, "_run_nightly") as m_run2:
    logs4 = []
    se._run_queued_rescan_tick(
        {"universes": [], "universe_cadence": {}}, logs4.append,
        rl_cooldown_active=False, rl_cooldown_until=None,
        abandoned_status=None, abandoned_info=None,
    )
assert not m_run2.called
assert any("another process already holds the lock" in l for l in logs4), logs4
print("[c3_tick_skips_lock_contention] a queued rescan is skipped while "
      "another process holds the lock OK")

with mock.patch.object(se, "_run_nightly") as m_run3:
    logs5 = []
    se._run_queued_rescan_tick(
        {"universes": [], "universe_cadence": {}}, logs5.append,
        rl_cooldown_active=False, rl_cooldown_until=None,
        abandoned_status="alive", abandoned_info={"pid": 123, "started": "2026-09-30T08:00:00+00:00"},
    )
assert not m_run3.called
assert any("abandoned scan worker still running" in l and "pid 123" in l for l in logs5), logs5
assert se._rescan_request_status()["pending"]["universes"] == ["S&P 500"], \
    "a second scan must not start (or consume the queued request) while the abandoned worker is alive"
print("[c3_tick_skips_abandoned_alive] a queued rescan (and the request "
      "itself) does not start while an abandoned worker is still running - "
      "a second scan cannot start while it's alive OK")

_clear_state2 = se._load_state()
_clear_state2.pop("rescan_request", None)
se._save_state(_clear_state2)


# ======================================================================
# CHECK 4: _record_job()'s own abandoned-worker registration on a real
# hard timeout - the worker thread is genuinely still alive afterwards,
# tracked, and its cancel_event nudged.
# ======================================================================
_release_worker = threading.Event()


def _slow_job(log):
    log("started")
    _release_worker.wait(timeout=10)
    log("finished")


with mock.patch.dict(se._JOB_HARD_TIMEOUT_SECONDS, {"nightly": 0.2}):
    _cancel = threading.Event()
    job_logs = []
    raised = None
    try:
        se._record_job("nightly", job_logs.append, _slow_job, cancel_event=_cancel)
    except TimeoutError as e:
        raised = e
assert raised is not None, "a slow job past its hard timeout must raise TimeoutError"
assert any("TIMED OUT" in l for l in job_logs), job_logs

status, info = se._abandoned_worker_status("nightly")
assert status == "alive", status
assert info["thread"].is_alive()
assert _cancel.is_set(), "the abandoned worker's cancel_event must be nudged by the status check"
print("[c3_record_job_abandons_on_timeout] a hard-timed-out job registers "
      "its still-alive worker thread as abandoned, with its cancel_event "
      "nudged OK")

# Let the worker actually finish, then confirm the record self-clears.
_release_worker.set()
_deadline = time.time() + 5
while se._abandoned_worker_status("nightly")[0] is not None and time.time() < _deadline:
    time.sleep(0.05)
status2, info2 = se._abandoned_worker_status("nightly")
assert status2 is None and info2 is None, (status2, info2)
print("[c3_abandoned_worker_clears_on_exit] once the worker thread actually "
      "exits, _abandoned_worker_status() clears the record on its own OK")


# ======================================================================
# CHECK 5: _abandoned_worker_status() "expired" bucket - past
# ABANDONED_WORKER_MAX_HOURS, a caller may proceed anyway.
# ======================================================================
_never_ending = threading.Event()


def _target():
    _never_ending.wait(timeout=30)


t = threading.Thread(target=_target, daemon=True)
t.start()
se._mark_worker_abandoned("nightly", t, None)
se._abandoned_workers["nightly"]["abandoned_at"] = (
    time.time() - (se.ABANDONED_WORKER_MAX_HOURS * 3600 + 60)
)
status3, info3 = se._abandoned_worker_status("nightly")
assert status3 == "expired", status3
_never_ending.set()
t.join(timeout=5)
se._abandoned_workers.pop("nightly", None)
print("[c3_abandoned_worker_expires] a worker abandoned past "
      f"{se.ABANDONED_WORKER_MAX_HOURS}h reads as 'expired', letting a "
      "caller proceed anyway OK")


# ======================================================================
# CHECK 6: nightly_scan.run_universe_scan()'s cooperative cancel_event -
# stops within one ticker, checkpoints first, returns None.
# ======================================================================
import pandas as pd

_tickers = [f"T{i}.AX" for i in range(10)]
_fake_pool_df = pd.DataFrame({"Ticker": _tickers})
_cancel_after_first = threading.Event()
_calls = []


def _analyze_stub(ticker, attention_lite=True, discount_rate=None, log=print,
                   rate_limited_out=None, growth_summary_out=None, oneoff_summary_out=None,
                   shadow_out=None, info_failed_out=None):
    _calls.append(ticker)
    if len(_calls) == 1:
        _cancel_after_first.set()
    return {"Ticker": ticker, "Price": 10.0, "Long Score": 50}


with mock.patch("scanner_engine.get_universe_pool", return_value=(_fake_pool_df, "test source")), \
     mock.patch.object(nightly_scan, "_yf_call_with_retry", return_value=None), \
     mock.patch.object(nightly_scan, "analyze_ticker_lite", side_effect=_analyze_stub), \
     mock.patch.object(nightly_scan, "_attach_moat"), \
     mock.patch.object(nightly_scan, "_attach_dividend_payout"), \
     mock.patch.object(scan_checkpoint_store, "load", return_value=None), \
     mock.patch.object(scan_checkpoint_store, "save") as m_checkpoint_save:
    cancel_logs = []
    result = nightly_scan.run_universe_scan(
        "Dow Jones 30", log=cancel_logs.append, run_night="2026-09-30",
        cancel_event=_cancel_after_first)

assert result is None, "a cancelled scan must not save to scan_store"
assert len(_calls) == 1, f"expected exactly 1 ticker attempted before cancel, got {_calls}"
assert m_checkpoint_save.called, "a cancelled scan must checkpoint before returning"
assert any("cancelled after 1/10 tickers" in l for l in cancel_logs), cancel_logs
print("[c3_cancel_event_stops_loop] run_universe_scan()'s cancel_event "
      "stops the per-ticker loop within one ticker, checkpoints, and "
      "returns None OK")

print("SWEEP_DONE")
