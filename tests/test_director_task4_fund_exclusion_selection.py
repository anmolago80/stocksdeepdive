"""
Director TASK 4 (16 Oct 2026, "keep funds/investment trusts out of the
Top 200 - blocks UK go-live").

Root cause (verified in code, not guessed): nightly_scan.py already
sets row["Is Fund"] for a non-US/non-AU row whose Sector exactly
matches _FUND_SECTOR_NAMES (case-insensitively), and top100_engine.py's
pre-score eligibility (_prescore_row_eligible(), ~line 5141 - also used
by preview_candidates_by_country(), so the preview ALREADY honoured
this flag before this task) already excludes a flagged row - but the
real Top 200 SELECTION loop (_build_best_by_ticker(), ~line 674,
alongside its _is_flagged_stale()/DCF-unreliable per-row skips) never
checked it at all, so a fund row with a surviving Long Score could
still win a real pool slot. Fixed: the selection loop now skips any
row where row.get("Is Fund") is true, counted and logged exactly like
the existing DCF-unreliable exclusion ("[top100] selection: excluded N
fund row(s): ...").

This file covers the task's own test list:
  - a fund row never enters the real pool (the NEW fix) or the preview
    (the PRE-EXISTING _prescore_row_eligible() check - confirmed here
    too, since "the preview matches" is part of the task).
  - a non-fund row is completely unaffected by either check.
  - EMG.L's not-a-fund override still holds (Is Fund=False reaches the
    selection loop unexcluded, exactly as nightly_scan.py's own
    _FUND_SECTOR_OVERRIDE_NOT_A_FUND already guarantees upstream).

Run: python3 tests/test_director_task4_fund_exclusion_selection.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task4_fund_exclusion_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scan_store
import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

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


_now_dt = datetime.now(timezone.utc)
_FAKE_CADENCE = {"S&P 500": "daily"}


def _row(ticker, long_score, is_fund=False, mos=20.0):
    return {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": 70,
        "MOS %": (None if is_fund else mos), "Psychology": 5.0,
        "Discovery (lite)": 10.0, "Long Score": long_score,
        "Price": 50.0, "Intrinsic Value": (None if is_fund else 55.0),
        "Is Fund": is_fund,
    }


def _save_universe(universe, rows):
    payload = {
        "universe": universe, "source": "test", "generated_at": _now_dt.isoformat(),
        "run_night": None, "rows": rows, "attention_lite": True, "degraded": False,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


# ======================================================================
# CHECK 1-3: the real selection loop (select_top100_pool() via
# _build_best_by_ticker()) - a fund row never enters the pool; a
# non-fund row is completely unaffected; EMG.L's override (Is Fund=
# False, as nightly_scan.py's own override already guarantees) still
# reaches the pool like any ordinary row.
# ======================================================================
_rows = [
    _row("GOODCO", long_score=80.0, is_fund=False, mos=40.0),
    _row("FUNDCO", long_score=95.0, is_fund=True),  # highest score - would win a slot if not excluded
    _row("EMG.L", long_score=70.0, is_fund=False, mos=30.0),  # override already applied upstream
]
_save_universe("S&P 500", _rows)

_logs = []
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool = te.select_top100_pool(log=_logs.append)

pool_tickers = {r["ticker"] for r in pool}
check("the fund row (FUNDCO, highest Long Score) never enters the real "
      "pool despite having the top score",
      "FUNDCO" not in pool_tickers)
check("the ordinary non-fund row (GOODCO) is in the pool, completely "
      "unaffected by the fund check",
      "GOODCO" in pool_tickers)
check("EMG.L (Is Fund=False via nightly_scan.py's own override) reaches "
      "the pool like any ordinary row - the override is respected, not "
      "re-derived or second-guessed by the selection loop",
      "EMG.L" in pool_tickers)
check("exactly 2 tickers in the pool (FUNDCO excluded, both others kept)",
      len(pool_tickers) == 2)

_fund_log = next((l for l in _logs if "fund row(s)" in l), None)
check("a log line reports the fund exclusion, same convention as the "
      "existing DCF-unreliable exclusion line",
      _fund_log is not None and "excluded 1 fund row(s): FUNDCO" in _fund_log)
_no_dcf_log = not any("DCF-unreliable" in l for l in _logs)
check("no DCF-unreliable exclusion line fires for this fixture (proves "
      "FUNDCO was excluded BY THE FUND CHECK, not accidentally caught "
      "by a different, pre-existing exclusion)",
      _no_dcf_log)

if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))


# ======================================================================
# CHECK 4-6: preview_candidates_by_country() - confirms the PRE-
# EXISTING _prescore_row_eligible() check (already live before this
# task) excludes a fund row from "by_country" with reason "fund", a
# non-fund row is unaffected, and EMG.L's override holds here too -
# "the preview matches" the selection loop's own behaviour.
# ======================================================================
_save_universe("FTSE 100", _rows)
with mock.patch.dict(os.environ, {"TOP200_PREVIEW_UNIVERSES": "FTSE 100"}):
    preview = te.preview_candidates_by_country()

_preview_tickers = {
    r["ticker"] for rows in preview["by_country"].values() for r in rows
}
_excluded_fund = [e for e in preview["excluded"] if e["ticker"] == "FUNDCO"]
check("the fund row never appears in 'by_country' (the preview's own "
      "'would enter' view)",
      "FUNDCO" not in _preview_tickers)
check("the fund row appears in 'excluded' with reason 'fund'",
      len(_excluded_fund) == 1 and _excluded_fund[0]["reason"] == "fund")
check("the non-fund row (GOODCO) IS in 'by_country' - unaffected",
      "GOODCO" in _preview_tickers)
check("EMG.L (Is Fund=False) is also in 'by_country' - the override "
      "holds in the preview too",
      "EMG.L" in _preview_tickers)

if os.path.exists(scan_store._path("FTSE 100")):
    os.remove(scan_store._path("FTSE 100"))


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
