"""Audit fix A5 / Fable finding T2 (10 Oct 2026, instruction_combined_
10oct.md PART A): scheduler_engine._run_nightly() must read-modify-
write scheduler_state.json at the end of a scan, not save the
snapshot it took hours earlier at the very start.

Fable's scenario, reproduced here: _run_nightly() loads scheduler_
state.json ONCE into `_catchup_state` before its per-universe loop
begins (that loop can run for hours), mutates only `_catchup_state
["catchup_failures"]` in place as each universe's outcome comes in,
then (before this fix) called _save_state(_catchup_state) - writing
that ENTIRE stale snapshot back to disk. If some OTHER process (the
daily backup job, a manual rescan request from the admin panel, the
watchdog) wrote to scheduler_state.json at any point DURING the scan,
that write was simply gone the moment this function's own stale
snapshot overwrote the whole file at the end - even though this
function never touches those other fields itself.

Run: python3 tests/test_audit_a5_scheduler_state_rmw.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="audit_a5_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scheduler_engine as se
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
# Seed the state file with fields this function never touches, same
# shape a real daily-backup/rescan-request write would leave.
# ======================================================================
se._save_state({
    "catchup_failures": {"OLDUNIV": {"count": 1, "last_attempt": "2026-10-08T00:00:00+00:00"}},
    "last_backup_date": "2026-10-08",
})

cfg = {
    "universes": ["Dow Jones 30", "S&P 500"],
    "universe_cadence": {"Dow Jones 30": "daily", "S&P 500": "daily"},
}
fake_payload = {"rows": [{"Ticker": "AAA", "Long Score": 50}], "attention_lite": False}
_scan_call_count = {"n": 0}


def _run_universe_scan_with_concurrent_write(universe, log=print, run_night=None, cancel_event=None):
    # Simulates another process (the daily backup job, a manual
    # rescan request) writing to scheduler_state.json WHILE this
    # scan's own per-universe loop is still running - the real-world
    # race the old code lost.
    _scan_call_count["n"] += 1
    if _scan_call_count["n"] == 1:
        _concurrent = se._load_state()
        _concurrent["last_backup_date"] = "2026-10-09"
        _concurrent["rescan_request"] = {"universe": "ASX 200", "requested_at": "2026-10-09T03:00:00+00:00"}
        se._save_state(_concurrent)
    return fake_payload


with mock.patch.object(nightly_scan, "refresh_market_cap_ranking"), \
     mock.patch("scanner_engine.fetch_asx300"), \
     mock.patch("scanner_engine.fetch_allords"), \
     mock.patch("scanner_engine.fetch_asx_listed_companies"), \
     mock.patch("alert_engine.snapshot_previous_values", return_value={}), \
     mock.patch.object(nightly_scan, "run_universe_scan", side_effect=_run_universe_scan_with_concurrent_write), \
     mock.patch.object(nightly_scan, "run_imported_scan", return_value=None), \
     mock.patch("snapshot_store.build_snapshots_from_scan"), \
     mock.patch("alert_engine.check_universe_rows"), \
     mock.patch("insider_engine.refresh_universe"), \
     mock.patch.object(nightly_scan, "run_sector_topup"), \
     mock.patch.object(nightly_scan, "run_attention_topup"), \
     mock.patch.object(nightly_scan, "reprice_universe"), \
     mock.patch.object(se, "_build_derived_universes"), \
     mock.patch("alert_engine.run_extra_ticker_pass"), \
     mock.patch("alert_engine.send_batched_notifications"), \
     mock.patch("results_engine.check_results_day"):
    logs = []
    se._run_nightly(cfg, logs.append, run_night="2026-10-09")

_final_state = se._load_state()

check("the concurrent write's last_backup_date (2026-10-09) survived "
      "_run_nightly()'s own end-of-scan save",
      _final_state.get("last_backup_date") == "2026-10-09")
check("the concurrent write's rescan_request survived too",
      _final_state.get("rescan_request", {}).get("universe") == "ASX 200")
check("this run's OWN catchup_failures field is still present and correct - "
      "neither scanned universe (Dow Jones 30/S&P 500) failed, and the "
      "pre-existing OLDUNIV record (an unrelated universe this run never "
      "touched) is preserved untouched, not silently dropped either",
      _final_state.get("catchup_failures") == {
          "OLDUNIV": {"count": 1, "last_attempt": "2026-10-08T00:00:00+00:00"}})

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
