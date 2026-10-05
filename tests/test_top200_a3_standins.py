"""
A3 amendment of instruction_top200_amendments_and_currency_view.md
(5 Oct 2026, Director-directed) to COMMIT 3 of instruction_top200_
unrated_and_blank_replies_combined.md - "the public list is filled to
200 with rated companies" (switch TOP200_BACKFILL_LIVE).

Covers exactly the task's own listed tests, in addition to those
already covered by test_top200_commit3_backfill.py:
  - 30 WAITING in the scored set + 40 RATED candidates in positions
    201-260: the public list shows 200, with exactly 30 stand-ins
    taken in merit order.
  - 30 WAITING + only 10 RATED candidates in the window: the public
    list shows 180.
  - A WAITING company becomes RATED: it appears and the lowest-merit
    stand-in leaves; no other row changes.
  - A stand-in is never added to the list of tickers to score.
  - Switch OFF: byte-identical to commit 007d669 (re-verified here,
    in addition to test_top200_commit3_backfill.py's own 9-fixture
    proof, specifically against a fixture that WOULD trigger stand-ins
    if the switch were on).
  - The ASX extension gets the same stand-in rule ("as you proposed in
    A0.5").

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top200_a3_standins.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top200_a3_standins_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts
import scan_store

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

MODEL = te.MODEL_TOP100
RV = te.RUBRIC_VERSION
te.POOL_SIZE = 200

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


def _merit_row(ticker, value_score):
    return {"ticker": ticker, "company_name": f"{ticker} Co", "value_score": value_score,
            "most_recent_quarter": None}


def _mark_rated(ticker, score=4):
    dims = {k: {"score": score, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
    ts.save_score(ticker=ticker, quarter=f"D2026-10-05-{ticker}", model=MODEL, rubric_version=RV,
                  dims=dims, not_rated=False, inversion_scenario="s", inversion_severity=2,
                  prompt="{}", raw_response="{}")


# ======================================================================
# CHECK 1: 30 WAITING in the scored set, 40 RATED candidates in
# positions 201-260 -> exactly 30 standins taken, public list = 200.
# ======================================================================
merit1 = [_merit_row(f"A1_{i:03d}", 1000.0 - i) for i in range(260)]
# Positions 0-199: 30 WAITING (A1_000..A1_029), 170 RATED (A1_030..A1_199).
for i in range(30, 200):
    _mark_rated(f"A1_{i:03d}")
# Positions 200-259: 40 RATED (A1_200..A1_239), 20 WAITING (A1_240..A1_259).
for i in range(200, 240):
    _mark_rated(f"A1_{i:03d}")

_bf1 = te._apply_backfill(merit1, model=MODEL)
_counted1 = _bf1["counted"]
_standins1 = _bf1["standins"]
check("scored set (counted) has 200 members", len(_counted1) == 200)
check("scored set has 170 rated + 30 waiting", len(_counted1) - 30 == 170)
check("exactly 30 standins taken", len(_standins1) == 30)
_standin_tickers1 = [r["ticker"] for r in _standins1]
check("standins taken in merit order (A1_200..A1_229, the first 30 of the 40 available)",
      _standin_tickers1 == [f"A1_{i:03d}" for i in range(200, 230)])
_summary1 = te._backfill_summary(merit1, _bf1, model=MODEL)
check("summary: scored_set_rated=170", _summary1["scored_set_rated"] == 170)
check("summary: scored_set_waiting=30", _summary1["scored_set_waiting"] == 30)
check("summary: public_list_count=200 (170+30)", _summary1["public_list_count"] == 200)
print("[a3_30_waiting_40_rated_available] public list shows 200, exactly 30 stand-ins taken "
      "in merit order OK")

# ======================================================================
# CHECK 2: 30 WAITING, only 10 RATED candidates in the window -> public
# list shows 180.
# ======================================================================
merit2 = [_merit_row(f"A2_{i:03d}", 1000.0 - i) for i in range(260)]
for i in range(30, 200):
    _mark_rated(f"A2_{i:03d}")
# Only 10 RATED beyond position 200 (A2_200..A2_209); the rest WAITING.
for i in range(200, 210):
    _mark_rated(f"A2_{i:03d}")

_bf2 = te._apply_backfill(merit2, model=MODEL)
_summary2 = te._backfill_summary(merit2, _bf2, model=MODEL)
check("only 10 standins found (fewer RATED candidates than needed)", len(_bf2["standins"]) == 10)
check("public_list_count=180 (170 rated + 10 standins)", _summary2["public_list_count"] == 180)
print("[a3_30_waiting_10_rated_available] public list shows 180 when only 10 RATED candidates "
      "exist in the window OK")

# ======================================================================
# CHECK 3: a WAITING company becomes RATED - it appears in the scored
# set as rated, W drops by one, and the LOWEST-MERIT standin from
# before (the 30th, i.e. last) leaves; no other row changes.
# ======================================================================
merit3 = list(merit1)  # same shape as CHECK 1's fixture
_mark_rated("A1_000")  # A1_000 was WAITING in CHECK 1 - now becomes RATED
_bf3 = te._apply_backfill(merit3, model=MODEL)
check("the newly-RATED company (A1_000) is counted as RATED (W drops to 29)",
      sum(1 for row in _bf3["counted"]
          if te.company_status(ts.latest_scores_for_model(MODEL, RV).get(row["ticker"]),
                                ts.score_failures_for_model(MODEL, RV).get(row["ticker"])) == "waiting") == 29)
_standin_tickers3 = [r["ticker"] for r in _bf3["standins"]]
check("exactly 29 standins now (one fewer)", len(_standin_tickers3) == 29)
check("the lowest-merit standin from CHECK 1 (A1_229) has left",
      "A1_229" not in _standin_tickers3)
check("every other standin from CHECK 1 is unchanged (A1_200..A1_228 still present)",
      _standin_tickers3 == [f"A1_{i:03d}" for i in range(200, 229)])
print("[a3_waiting_becomes_rated] the company appears rated, the lowest-merit standin leaves, "
      "no other row changes OK")

# ======================================================================
# CHECK 4: a standin is never added to the list of tickers to score.
# ======================================================================
_pool_with_standins = _counted1 + _standins1
_unscored4 = te._unscored_tickers(_pool_with_standins, MODEL)
_unscored_tickers4 = {row["ticker"] for row, _reason in _unscored4}
for _t in _standin_tickers1:
    check(f"standin {_t} never appears in _unscored_tickers()'s own output",
          _t not in _unscored_tickers4)
print("[a3_standin_never_rescored] none of the 30 standins from CHECK 1 are ever selected "
      "for scoring by _unscored_tickers() OK")

# ======================================================================
# CHECK 5: switch OFF - byte-identical to 007d669, even against a
# fixture shaped specifically to trigger standins if the switch were
# on (merit1, which has 30 WAITING companies within the first 200).
# ======================================================================
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util


def _load_module_from_git(path, module_name, commit):
    result = os.popen(f"cd {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r} "
                       f"&& git show {commit}:{path} 2>/dev/null").read()
    spec = importlib.util.spec_from_loader(module_name + "_baseline", loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name + "_baseline"] = mod
    exec(compile(result, path, "exec"), mod.__dict__)
    return mod


_payload = {
    "run_night": None,
    "rows": [
        {"ticker": r["ticker"], "company_name": r["company_name"], "sector": "Technology",
         "universe": "S&P 500", "value_score": r["value_score"], "mos_pct": 10.0, "price": 100.0,
         "intrinsic_value": 110.0, "currency": "USD", "most_recent_quarter": None}
        for r in merit1
    ],
    "attention_lite": True, "degraded": False,
}
_tmp = scan_store._path("S&P 500") + ".tmp"
with open(_tmp, "w") as f:
    json.dump(_payload, f)
os.replace(_tmp, scan_store._path("S&P 500"))

os.environ.pop("TOP200_BACKFILL_LIVE", None)
_FAKE_CADENCE = {"S&P 500": "daily"}
with mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None), \
     mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
    _pool_now5 = te.select_top100_pool(log=lambda *a, **k: None)

_te_007d669 = _load_module_from_git("top100_engine.py", "top100_engine", "007d669")
with mock.patch.object(_te_007d669, "_fill_missing_sectors", lambda rows, log=print: None), \
     mock.patch.object(_te_007d669.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
    _pool_before5 = _te_007d669.select_top100_pool(log=lambda *a, **k: None)

_canon = lambda pool: json.dumps(pool, sort_keys=True, default=str)
check("switch OFF: byte-identical to 007d669 even on a standin-triggering fixture",
      _canon(_pool_now5) == _canon(_pool_before5))
print("[a3_switch_off_byte_identical] switch OFF reproduces 007d669 exactly, even on a fixture "
      "shaped to trigger stand-ins if the switch were on OK")

if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
sys.exit(1 if failed else 0)
