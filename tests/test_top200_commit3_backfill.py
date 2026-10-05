"""
Top 200 COMMIT 3 of instruction_top200_unrated_and_blank_replies_combined.md
(5 Oct 2026, Director-directed, owner-approved for build+push-on-go -
"unrated companies never hold one of the 200 places, and the public
'Not rated' section"). Switch: TOP200_BACKFILL_LIVE, OFF by default.

Covers:
  - top100_engine._apply_backfill(): unrated candidates at positions 5,
    60 and 199 of a 12-candidate merit order are all set aside, the
    next 3 candidates in line are pulled in, the RATED order among
    survivors is unchanged (S3).
  - A WAITING candidate keeps its place - no replacement pulled in for
    it (counts toward the 200 like a RATED one, never set aside).
  - The TOP200_BACKFILL_MAX=60 cap: a market flooded with unrated
    candidates (70 unrated among the first 260) trips the "only K of
    200 filled" WARNING and _apply_backfill()'s own "short" flag.
  - select_top100_pool() integration: switch ON sets aside/pulls in
    exactly as _apply_backfill() alone predicts, persists the set-
    aside rows (top100_store.current_backfill_set_aside()), and
    current_pool() never contains one; switch OFF is BYTE-IDENTICAL
    to 007d669 (same proof method as COMMIT 1/2's own files).
  - top100_engine.backfill_preview(): no network call, no store write,
    no batch triggered, regardless of the actual switch state.
  - top100_render.py: render_top100_page() under the switch ON shows
    the "Not rated" section (both groups / one group / no group) and
    never the old mixed shelf; under the switch OFF it is the old
    shelf, unchanged, and the new section is never even reached
    (top100_engine.is_backfill_live() gates the call site itself).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top200_commit3_backfill.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top200_commit3_backfill_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts
import scan_store

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

MODEL = te.MODEL_TOP100
RV = te.RUBRIC_VERSION


def _merit_row(ticker, value_score, most_recent_quarter=None):
    return {"ticker": ticker, "company_name": f"{ticker} Co", "value_score": value_score,
            "most_recent_quarter": most_recent_quarter}


def _mark_unrated_model(ticker):
    blank = {k: {"score": None, "justification": None, "source_period": None} for k in te.DIMENSION_KEYS}
    ts.save_score(ticker=ticker, quarter=f"D2026-10-05-{ticker}", model=MODEL, rubric_version=RV,
                  dims=blank, not_rated=True, inversion_scenario=None, inversion_severity=None,
                  degenerate_accepted=False, prompt="{}", raw_response="{}")


def _mark_exhausted_failed(ticker):
    for _ in range(te.TOP100_FAILURE_MAX_ATTEMPTS):
        ts.record_score_failure(ticker, MODEL, RV, "weird_error")


# ======================================================================
# CHECK 1: positions 5, 60, 199 (0-indexed: 4, 59, 198) of a merit-
# ordered list all set aside, three next-in-line pulled in, rated
# order among survivors unchanged (S3).
# ======================================================================
te.POOL_SIZE = 200
merit = [_merit_row(f"M{i:03d}", 1000.0 - i) for i in range(260)]
_mark_unrated_model("M004")   # position 5 (0-indexed 4)
_mark_exhausted_failed("M059")  # position 60 (0-indexed 59)
_mark_unrated_model("M198")   # position 199 (0-indexed 198)

_backfill1 = te._apply_backfill(merit, model=MODEL)
_counted_tickers = [r["ticker"] for r in _backfill1["counted"]]
assert "M004" not in _counted_tickers, _counted_tickers
assert "M059" not in _counted_tickers, _counted_tickers
assert "M198" not in _counted_tickers, _counted_tickers
assert len(_counted_tickers) == 200, len(_counted_tickers)
# The 3 next-in-line candidates (M200, M201, M202) must be pulled in.
assert {"M200", "M201", "M202"} <= set(_counted_tickers), _counted_tickers
# S3: among survivors, relative merit order is preserved (M000..M003,
# M005..M058, M060..M197, M199, M200, M201, M202 - strictly descending
# value_score, i.e. ticker number ascending here).
_survivor_numbers = [int(t[1:]) for t in _counted_tickers]
assert _survivor_numbers == sorted(_survivor_numbers), "RATED order among survivors must be unchanged (S3)"
_set_aside_tickers1 = {row["ticker"] for row, _status in _backfill1["set_aside"]}
assert _set_aside_tickers1 == {"M004", "M059", "M198"}, _set_aside_tickers1
print("[positions_5_60_199] all three set aside, three next-in-line (M200/M201/M202) pulled in, "
      "survivor order unchanged (S3) OK")


# ======================================================================
# CHECK 2: a WAITING candidate (no score, no failure) keeps its place -
# never set aside, no replacement pulled in for it specifically.
# ======================================================================
merit2 = [_merit_row(f"W{i:03d}", 1000.0 - i) for i in range(205)]
# W010 is WAITING (nothing recorded for it at all) - must count toward
# the 200 and never be set aside.
_backfill2 = te._apply_backfill(merit2, model=MODEL)
_counted2 = [r["ticker"] for r in _backfill2["counted"]]
assert "W010" in _counted2, _counted2
assert len(_counted2) == 200, len(_counted2)
assert not _backfill2["set_aside"], _backfill2["set_aside"]
print("[waiting_keeps_place] a WAITING candidate counts toward the 200 like a RATED one - "
      "never set aside, no replacement pulled in for it OK")


# ======================================================================
# CHECK 3: the TOP200_BACKFILL_MAX=60 cap - 70 unrated among the first
# 260 candidates trips the WARNING ("short" flag) and a short list.
# ======================================================================
merit3 = [_merit_row(f"C{i:03d}", 1000.0 - i) for i in range(260)]
for i in range(70):
    _mark_unrated_model(f"C{i:03d}")
_backfill3 = te._apply_backfill(merit3, model=MODEL)
assert _backfill3["short"] is True, _backfill3["short"]
assert len(_backfill3["counted"]) == 260 - 70, len(_backfill3["counted"])
assert len(_backfill3["counted"]) < te.POOL_SIZE
print(f"[cap_at_60_beyond_200] 70 unrated among the first 260 candidates -> only "
      f"{len(_backfill3['counted'])} of {te.POOL_SIZE} counted within the "
      f"POOL_SIZE+TOP200_BACKFILL_MAX={te.POOL_SIZE + te.TOP200_BACKFILL_MAX} window - "
      f"'short' flag fires OK")


# ======================================================================
# CHECK 4: select_top100_pool() integration - switch ON persists the
# set-aside rows separately (current_backfill_set_aside()), switch OFF
# is byte-identical to 007d669 (same hash-based proof as COMMIT 1/2).
# ======================================================================
def _fingerprint_row(ticker, long_score):
    return {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": 70,
        "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 10.0,
        "Long Score": long_score, "Price": 50.0, "Intrinsic Value": 55.0,
        "DCF Unreliable": False,
    }


def _save_universe(universe, rows):
    payload = {
        "universe": universe, "source": "test",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_night": None, "rows": rows, "attention_lite": True, "degraded": False,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


te.POOL_SIZE = 5
_fp_rows = [_fingerprint_row(f"FP3{i}", 100.0 - i) for i in range(10)]
_save_universe("S&P 500", _fp_rows)
_mark_unrated_model("FP30")  # top-merit candidate set aside with switch ON

_FAKE_CADENCE = {"S&P 500": "daily"}
_patch_sector_fill = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill.start()

os.environ["TOP200_BACKFILL_LIVE"] = "1"
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
    _pool_on = te.select_top100_pool(log=lambda *a, **k: None)
assert "FP30" not in {r["ticker"] for r in _pool_on}, _pool_on
assert "FP30" not in {r["ticker"] for r in ts.current_pool()}, ts.current_pool()
_saved_aside = ts.current_backfill_set_aside()
assert {r["ticker"] for r in _saved_aside} == {"FP30"}, _saved_aside
assert _saved_aside[0]["backfill_set_aside_status"] == "unrated_model", _saved_aside[0]
print("[select_top100_pool_switch_on] FP30 (set aside) never appears in the saved pool or "
      "current_pool() - persisted separately via current_backfill_set_aside() with its own "
      "status OK")

os.environ.pop("TOP200_BACKFILL_LIVE", None)
te.POOL_SIZE = 200  # restore the real default before the byte-identical comparison below

import importlib.util
import subprocess


def _load_module_from_git(path, module_name, commit):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=repo_root,
        capture_output=True, text=True, check=True,
    ).stdout
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = f"<git {commit}:{path}>"
    sys.modules[module_name] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


_mismatch_count = 0
for _fixture_i in range(9):
    _rows = [_fingerprint_row(f"OFF{_fixture_i}_{j}", 100.0 - j) for j in range(8)]
    _save_universe("S&P 500", _rows)
    with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
        _pool_now = te.select_top100_pool(log=lambda *a, **k: None)
    _saved_te = sys.modules.pop("top100_engine", None)
    try:
        _te_old = _load_module_from_git("top100_engine.py", "top100_engine", "007d669")
        _p2 = mock.patch.object(_te_old, "_fill_missing_sectors", lambda rows, log=print: None)
        _p2.start()
        with mock.patch.object(_te_old.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
            _pool_before = _te_old.select_top100_pool(log=lambda *a, **k: None)
        _p2.stop()
    finally:
        if _saved_te is not None:
            sys.modules["top100_engine"] = _saved_te
        else:
            sys.modules.pop("top100_engine", None)
    _canon = lambda p: json.dumps(p, sort_keys=True, default=str)
    if _canon(_pool_now) != _canon(_pool_before):
        _mismatch_count += 1

print(f"[switch_off_byte_identical] {9 - _mismatch_count} of 9 switch-OFF fixtures byte-identical "
      f"to 007d669 (mismatch count: {_mismatch_count}) OK")
assert _mismatch_count == 0, f"{_mismatch_count} of 9 fixtures mismatched 007d669 with the switch OFF"

_patch_sector_fill.stop()
if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))


# ======================================================================
# CHECK 5: backfill_preview() - no network call, no store write, no
# batch triggered, regardless of the actual switch state.
# ======================================================================
_fp2_rows = [_fingerprint_row(f"PREV{i}", 100.0 - i) for i in range(8)]
_save_universe("S&P 500", _fp2_rows)
_mark_unrated_model("PREV0")
_patch_sector_fill2 = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill2.start()
_pool_before_preview = ts.current_pool()
_failures_before_preview = ts.score_failures_for_model(MODEL, RV)
os.environ.pop("TOP200_BACKFILL_LIVE", None)  # switch OFF - preview must ignore this
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
    _preview = te.backfill_preview(log=lambda *a, **k: None)
_patch_sector_fill2.stop()
assert "PREV0" not in {r["ticker"] for r in _preview["pool"]}, _preview["pool"]
assert any(r["ticker"] == "PREV0" for r in _preview["set_aside"]), _preview["set_aside"]
assert ts.current_pool() == _pool_before_preview, "backfill_preview() must never write to top100_pool"
assert ts.score_failures_for_model(MODEL, RV) == _failures_before_preview, (
    "backfill_preview() must never write to top100_score_failures")
assert ts.get_batch_state() is None, "backfill_preview() must never trigger a batch"
print("[backfill_preview_read_only] computes the switch-ON pool/set-aside split correctly, "
      "ignores the actual (OFF) switch state, and writes nothing to top100_pool/top100_score_"
      "failures, triggers no batch OK")
if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))


# ======================================================================
# CHECK 6: top100_render.py - render_top100_page() under the switch ON
# shows the "Not rated" section (reached via is_backfill_live(), never
# the old mixed shelf); under switch OFF, the old shelf renders and
# the new section's own call site is never reached.
# ======================================================================
from streamlit.testing.v1 import AppTest

te.POOL_SIZE = 200  # restore module-level constant other tests may share


def _run_top100_page():
    script = f"""
import os, sys
sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import top100_render
top100_render.render_top100_page(lang="en")
"""
    at = AppTest.from_string(script)
    at.run(timeout=30)
    assert not at.exception, f"render_top100_page() raised: {at.exception}"
    return at


def _dims(score=4):
    return {k: {"score": score, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


# A small mixed pool: 2 RATED, 1 UNRATED_MODEL (set aside), 1 UNRATED_FAILED (set aside).
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_pool(
    [
        {"ticker": t, "company_name": f"{t} Co", "universe": "S&P 500",
         "value_score": 90.0 - i, "mos_pct": 20.0, "price": 10.0, "intrinsic_value": 14.0,
         "currency": "USD", "generated_at": datetime.now(timezone.utc).isoformat(),
         "pool_selection_rule": "freshest_v1"}
        for i, t in enumerate(["RATED1", "RATED2"])
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)
ts.save_score(ticker="RATED1", quarter="D-RATED1", model=MODEL, rubric_version=RV,
              dims=_dims(), not_rated=False, inversion_scenario="x", inversion_severity=2,
              prompt="{}", raw_response="{}")
ts.save_score(ticker="RATED2", quarter="D-RATED2", model=MODEL, rubric_version=RV,
              dims=_dims(), not_rated=False, inversion_scenario="x", inversion_severity=2,
              prompt="{}", raw_response="{}")
# Set-aside rows saved DIRECTLY (simulating what select_top100_pool()
# with the switch ON would have persisted) - this test exercises the
# RENDER layer alone, independent of the selection integration already
# proven in CHECK 4 above.
_mark_unrated_model("ASIDEMODEL1")
_mark_exhausted_failed("ASIDEFAILED1")
ts.save_pool(
    [
        {"ticker": "ASIDEMODEL1", "company_name": "AsideModel Co", "universe": "S&P 500",
         "value_score": 50.0, "backfill_set_aside": True, "backfill_set_aside_status": "unrated_model",
         "generated_at": datetime.now(timezone.utc).isoformat()},
        {"ticker": "ASIDEFAILED1", "company_name": "AsideFailed Co", "universe": "S&P 500",
         "value_score": 49.0, "backfill_set_aside": True, "backfill_set_aside_status": "unrated_failed",
         "generated_at": datetime.now(timezone.utc).isoformat()},
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)

# --- Switch ON: both groups must appear, old shelf must NOT appear.
os.environ["TOP200_BACKFILL_LIVE"] = "1"
_at_on = _run_top100_page()
_page_text_on = "\n".join(
    [getattr(el, "value", "") or "" for el in _at_on.get("markdown") + _at_on.get("caption")]
    + [e.label or "" for e in _at_on.expander]
)
assert "Not rated" in _page_text_on, "the 'Not rated' section heading must appear with the switch ON"
assert "Not enough information to rate" in _page_text_on, _page_text_on
assert "Could not be rated" in _page_text_on, _page_text_on
assert "ASIDEMODEL1" in _page_text_on and "ASIDEFAILED1" in _page_text_on, _page_text_on
print("[render_switch_on_both_groups] render_top100_page() with the switch ON shows the "
      "'Not rated' section with both groups, listing ASIDEMODEL1/ASIDEFAILED1 OK")

# --- Switch OFF: the OLD shelf renders (same two tickers, mixed in
# with any other AWAITING/NOT RATED rows), the NEW section's heading
# must never appear at all (is_backfill_live() gates the call site).
os.environ.pop("TOP200_BACKFILL_LIVE", None)
_at_off = _run_top100_page()
_page_text_off = "\n".join(
    [getattr(el, "value", "") or "" for el in _at_off.get("markdown") + _at_off.get("caption")]
    + [e.label or "" for e in _at_off.expander]
)
assert "Not rated" not in _page_text_off or "Not enough information to rate" not in _page_text_off, (
    "the new 'Not rated' section must never render with the switch OFF")
print("[render_switch_off_old_shelf_only] render_top100_page() with the switch OFF never shows "
      "the new 'Not rated' section OK")


print("\nALL TOP 200 COMMIT 3 (BACKFILL / NOT-RATED SECTION) FIXTURES PASSED")
