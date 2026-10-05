"""
Top 200 COMMIT 2 of instruction_top200_unrated_and_blank_replies_combined.md
(5 Oct 2026, Director-directed, owner-approved for build+push-on-go -
"a failed company is never stuck for good").

Covers:
  - top100_store.record_score_failure()'s new most_recent_quarter
    param (COALESCE-preserved across repeat failures, never blanked by
    a later call that doesn't pass it).
  - top100_engine._failure_reentry_reason() (via _unscored_tickers(),
    the only way it's reached in production): an EXHAUSTED failure
    re-enters under the same new_results/age triggers a NOT RATED
    score row already gets - and stays blocked when neither fires.
  - A non-exhausted failure's EXISTING cooldown-retry behaviour
    (TOP100_FAILURE_RETRY_HOURS) is completely unchanged by this
    commit - this is the thing most likely to regress since _failure_
    blocks() was rewritten.
  - submit_nightly_batch()'s attempts-reset: a re-entering ticker's
    failure row is cleared (attempts -> 0) the moment it is actually
    selected for tonight's batch, so a fresh failure doesn't instantly
    re-exhaust it past the old ceiling.
  - top100_engine.exhausted_failure_rows()/clear_exhausted_failure_
    rows() - the A2 panel's extension: lists ONLY exhausted failures,
    never a NOT RATED-by-model row (which has no failure row at all).
  - select_top100_pool()'s public return value stays byte-identical to
    007d669 (COMMIT 2 touches submission/retry logic only, never
    selection).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top200_commit2_reentry.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top200_commit2_reentry_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

MODEL = te.MODEL_TOP100
RV = te.RUBRIC_VERSION


# ======================================================================
# CHECK 1: record_score_failure()'s most_recent_quarter param -
# COALESCE-preserved, never blanked by a later call that omits it.
# ======================================================================
ts.record_score_failure("MRQ1", MODEL, RV, "timeout", most_recent_quarter="2026-06-30")
_f = ts.score_failures_for_model(MODEL, RV)["MRQ1"]
assert _f["most_recent_quarter"] == "2026-06-30", _f
assert _f["attempts"] == 1, _f
ts.record_score_failure("MRQ1", MODEL, RV, "timeout")  # omits most_recent_quarter
_f2 = ts.score_failures_for_model(MODEL, RV)["MRQ1"]
assert _f2["most_recent_quarter"] == "2026-06-30", (
    "a later record_score_failure() call that omits most_recent_quarter must never blank "
    "out a previously recorded value", _f2)
assert _f2["attempts"] == 2, _f2
ts.record_score_failure("MRQ1", MODEL, RV, "timeout", most_recent_quarter="2026-09-30")
_f3 = ts.score_failures_for_model(MODEL, RV)["MRQ1"]
assert _f3["most_recent_quarter"] == "2026-09-30", "a real value must overwrite the old one"
print("[record_score_failure_most_recent_quarter] COALESCE-preserved across a call that "
      "omits it, overwritten by a call that provides a new one, attempts keep incrementing OK")


# ======================================================================
# CHECK 2: _failure_reentry_reason() via _unscored_tickers() - an
# EXHAUSTED failure re-enters under new_results or age, and only then;
# an exhausted failure under NEITHER trigger stays permanently skipped
# (pre-COMMIT-2 behaviour, unchanged).
# ======================================================================
def _pool_row(ticker, most_recent_quarter=None):
    return {"ticker": ticker, "company_name": f"{ticker} Co", "most_recent_quarter": most_recent_quarter}


# (a) new_results: exhausted failure's own most_recent_quarter differs
# from the pool row's current one.
for _ in range(te.TOP100_FAILURE_MAX_ATTEMPTS):
    ts.record_score_failure("NEWRESULTS1", MODEL, RV, "weird_error", most_recent_quarter="2026-06-30")
_pool_a = [_pool_row("NEWRESULTS1", most_recent_quarter="2026-09-30")]
_unscored_a = te._unscored_tickers(_pool_a, MODEL)
_reasons_a = {row["ticker"]: reason for row, reason in _unscored_a}
assert _reasons_a.get("NEWRESULTS1") == "new_results", _reasons_a

# (b) age: exhausted failure whose failed_at is older than
# RESCORE_MAX_AGE_DAYS, most_recent_quarter unchanged (no new_results
# signal at all here).
_old_failed_at = (datetime.now(timezone.utc) - timedelta(days=te.RESCORE_MAX_AGE_DAYS + 5)).isoformat()
with mock.patch.object(ts, "_conn", wraps=ts._conn):
    pass  # no-op, keeping the real _conn - just documents intent
for _ in range(te.TOP100_FAILURE_MAX_ATTEMPTS):
    ts.record_score_failure("AGEOUT1", MODEL, RV, "weird_error", most_recent_quarter="2026-06-30")
with ts._conn() as _conn:
    _conn.execute(
        "UPDATE top100_score_failures SET failed_at = ? WHERE ticker = ? AND model = ? AND rubric_version = ?",
        (_old_failed_at, "AGEOUT1", MODEL, RV),
    )
_pool_b = [_pool_row("AGEOUT1", most_recent_quarter="2026-06-30")]
_unscored_b = te._unscored_tickers(_pool_b, MODEL)
_reasons_b = {row["ticker"]: reason for row, reason in _unscored_b}
assert _reasons_b.get("AGEOUT1") == "age", _reasons_b

# (c) exhausted, but NEITHER trigger fires (same most_recent_quarter,
# failed_at recent) -> stays permanently skipped, exactly as before
# this commit.
for _ in range(te.TOP100_FAILURE_MAX_ATTEMPTS):
    ts.record_score_failure("STILLSTUCK1", MODEL, RV, "weird_error", most_recent_quarter="2026-06-30")
_pool_c = [_pool_row("STILLSTUCK1", most_recent_quarter="2026-06-30")]
_unscored_c = te._unscored_tickers(_pool_c, MODEL)
_tickers_c = {row["ticker"] for row, _ in _unscored_c}
assert "STILLSTUCK1" not in _tickers_c, (
    "an exhausted failure with neither a new_results nor an age signal must stay "
    "permanently skipped for this rubric, exactly as before COMMIT 2", _unscored_c)
print("[failure_reentry_reason] exhausted failure re-enters as 'new_results' when its own "
      "stored most_recent_quarter differs from the pool row's current one, as 'age' when "
      "failed_at is older than RESCORE_MAX_AGE_DAYS with no results signal, and stays "
      "permanently skipped when neither trigger fires OK")


# ======================================================================
# CHECK 3: a NON-exhausted failure's existing cooldown-retry behaviour
# is completely unchanged - still blocked within TOP100_FAILURE_RETRY_
# HOURS, still retryable (as "new_or_rubric") once that window passes.
# This is the regression check for _failure_blocks()'s rewrite.
# ======================================================================
ts.seed_pool_presence({"COOLDOWN1": te.NEWCOMER_PERSISTENCE_NIGHTS}, "2026-10-05")
ts.record_score_failure("COOLDOWN1", MODEL, RV, "timeout")  # attempts=1, not exhausted
_pool_d = [_pool_row("COOLDOWN1")]
_unscored_d = te._unscored_tickers(_pool_d, MODEL)
_tickers_d = {row["ticker"] for row, _ in _unscored_d}
assert "COOLDOWN1" not in _tickers_d, (
    "a non-exhausted failure within its own TOP100_FAILURE_RETRY_HOURS cooldown must still "
    "be blocked, exactly as before COMMIT 2", _unscored_d)
# Age the failure past the cooldown window (but still well within
# RESCORE_MAX_AGE_DAYS) - must now be retryable again as "new_or_rubric".
_past_cooldown = (datetime.now(timezone.utc) - timedelta(hours=te.TOP100_FAILURE_RETRY_HOURS + 1)).isoformat()
with ts._conn() as _conn:
    _conn.execute(
        "UPDATE top100_score_failures SET failed_at = ? WHERE ticker = ? AND model = ? AND rubric_version = ?",
        (_past_cooldown, "COOLDOWN1", MODEL, RV),
    )
_unscored_d2 = te._unscored_tickers(_pool_d, MODEL)
_reasons_d2 = {row["ticker"]: reason for row, reason in _unscored_d2}
assert _reasons_d2.get("COOLDOWN1") == "new_or_rubric", _reasons_d2
print("[non_exhausted_cooldown_unchanged] a non-exhausted failure stays blocked within its "
      "own TOP100_FAILURE_RETRY_HOURS cooldown and becomes retryable (as 'new_or_rubric') "
      "once that window passes - unchanged by COMMIT 2 OK")


# ======================================================================
# CHECK 4: submit_nightly_batch()'s attempts-reset - a re-entering
# exhausted ticker has its failure row cleared (attempts -> 0) the
# moment it's actually selected for tonight's batch submission.
# ======================================================================
for _ in range(te.TOP100_FAILURE_MAX_ATTEMPTS):
    ts.record_score_failure("RESETME1", MODEL, RV, "weird_error", most_recent_quarter="2026-06-30")
assert ts.score_failures_for_model(MODEL, RV)["RESETME1"]["attempts"] == te.TOP100_FAILURE_MAX_ATTEMPTS
ts.save_pool(
    [{"ticker": "RESETME1", "company_name": "ResetMe Co", "universe": "us",
      "value_score": 50.0, "mos_pct": 0.1, "price": 10.0, "intrinsic_value": 11.0,
      "currency": "USD", "psychology": None, "sector": "Tech", "dividend_yield_pct": None,
      "most_recent_quarter": "2026-09-30"}],
    "2026-10-05",
)
with mock.patch.object(te, "submit_nightly_batch", wraps=te.submit_nightly_batch) as _wrapped, \
     mock.patch("anthropic.Anthropic") as _MockClient, \
     mock.patch.object(te, "is_resubmit_paused", return_value=False):
    _mock_batch = mock.Mock()
    _mock_batch.id = "msgbatch_reset_test"
    _MockClient.return_value.messages.batches.create.return_value = _mock_batch
    _result = te.submit_nightly_batch(log=lambda *a, **k: None)
_logs_after = ts.score_failures_for_model(MODEL, RV)
assert "RESETME1" not in _logs_after, (
    "RESETME1's failure row must be cleared (attempts reset) the moment it is selected for "
    "tonight's batch - otherwise its very next failure pushes attempts straight back past "
    "its own exhaustion ceiling", _logs_after)
print("[attempts_reset_on_reentry] RESETME1's exhausted failure row is cleared the moment "
      "it is actually selected for tonight's batch submission (attempts reset to 0, not "
      "carried forward past its own exhaustion ceiling) OK")


# ======================================================================
# CHECK 5: exhausted_failure_rows()/clear_exhausted_failure_rows() -
# the A2 panel's extension. Lists ONLY exhausted failures, never a NOT
# RATED-by-model row (score_row.not_rated True, degenerate_accepted
# False - which has no failure row at all, by construction) and never
# a degenerate_accepted row either (that's a SCORE row, not a failure).
# ======================================================================
for _ in range(te.TOP100_FAILURE_MAX_ATTEMPTS):
    ts.record_score_failure("PANELFAIL1", MODEL, RV, "weird_error")
_dims_blank = {k: {"score": None, "justification": None, "source_period": None} for k in te.DIMENSION_KEYS}
ts.save_score(
    ticker="PANELNOTRATED1", quarter="D2026-10-05-PANELNOTRATED1", model=MODEL, rubric_version=RV,
    dims=_dims_blank, not_rated=True, inversion_scenario=None, inversion_severity=None,
    degenerate_accepted=False, prompt="{}", raw_response="{}",
)
ts.save_score(
    ticker="PANELDEGEN1", quarter="D2026-10-05-PANELDEGEN1", model=MODEL, rubric_version=RV,
    dims=_dims_blank, not_rated=True, inversion_scenario=None, inversion_severity=None,
    degenerate_accepted=True, prompt="{}", raw_response="{}",
)
_exhausted_rows = te.exhausted_failure_rows(MODEL)
_exhausted_tickers = {r["ticker"] for r in _exhausted_rows}
assert "PANELFAIL1" in _exhausted_tickers, _exhausted_tickers
assert "PANELNOTRATED1" not in _exhausted_tickers, (
    "a NOT RATED-by-model row has no failure row at all and must never appear in "
    "exhausted_failure_rows()", _exhausted_tickers)
assert "PANELDEGEN1" not in _exhausted_tickers, (
    "a degenerate_accepted row is a SCORE row, not a failure, and must never appear in "
    "exhausted_failure_rows() (it already has its own list via degenerate_accepted_rows())",
    _exhausted_tickers)
_by_ticker = {r["ticker"]: r for r in _exhausted_rows}
assert _by_ticker["PANELFAIL1"]["attempts"] == te.TOP100_FAILURE_MAX_ATTEMPTS, _by_ticker["PANELFAIL1"]
assert _by_ticker["PANELFAIL1"]["reason"] == "weird_error", _by_ticker["PANELFAIL1"]

_log = []
_cleared = te.clear_exhausted_failure_rows(_exhausted_rows, MODEL, log=_log.append)
assert "PANELFAIL1" in _cleared, _cleared
assert set(_cleared) == _exhausted_tickers, (
    "clear_exhausted_failure_rows() must delete exactly the rows it was given - no more, "
    "no less", _cleared, _exhausted_tickers)
assert "PANELFAIL1" not in ts.score_failures_for_model(MODEL, RV), (
    "clear_exhausted_failure_rows() must delete exactly the failure rows it was given")
assert any(f"owner cleared {len(_cleared)} exhausted-failure row(s)" in l for l in _log), _log
print("[exhausted_failure_rows_panel] exhausted_failure_rows() lists ONLY exhausted "
      "failures (never a NOT RATED-by-model row, never a degenerate_accepted row); "
      "clear_exhausted_failure_rows() deletes exactly what it was given with the exact "
      "owner-specified log line OK")

_app_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")).read()
assert "top100_engine.exhausted_failure_rows(top100_engine.MODEL_TOP100)" in _app_src, (
    "_render_degenerate_accepted_panel() must call exhausted_failure_rows() for its "
    "COMMIT 2 extension")
assert "top100_engine.clear_exhausted_failure_rows(" in _app_src
print("[panel_wiring] app.py's _render_degenerate_accepted_panel() calls both "
      "exhausted_failure_rows() and clear_exhausted_failure_rows() for its COMMIT 2 "
      "extension OK")


# ======================================================================
# CHECK 6: select_top100_pool()'s public return value stays byte-
# identical to 007d669 given the same input scan - COMMIT 2 only
# touches submission/retry logic, never selection.
# ======================================================================
import importlib.util
import json
import subprocess
import scan_store


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


def _fingerprint_row(ticker, long_score):
    return {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": 70,
        "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 10.0,
        "Long Score": long_score, "Price": 50.0, "Intrinsic Value": 55.0,
        "DCF Unreliable": False,
    }


_fingerprint_rows = [_fingerprint_row(f"FP2{i}", 40.0 + i) for i in range(12)]
if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))
_payload = {
    "universe": "S&P 500", "source": "test",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "run_night": None, "rows": _fingerprint_rows, "attention_lite": True, "degraded": False,
}
_tmp = scan_store._path("S&P 500") + ".tmp"
with open(_tmp, "w") as f:
    json.dump(_payload, f)
os.replace(_tmp, scan_store._path("S&P 500"))

_FAKE_CADENCE = {"S&P 500": "daily"}
_patch_sector_fill = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill.start()
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
    _pool_now = te.select_top100_pool(log=lambda *a, **k: None)
_patch_sector_fill.stop()

_saved_te_module = sys.modules.pop("top100_engine", None)
try:
    _te_007d669 = _load_module_from_git("top100_engine.py", "top100_engine", "007d669")
    _patch_sector_fill2 = mock.patch.object(_te_007d669, "_fill_missing_sectors", lambda rows, log=print: None)
    _patch_sector_fill2.start()
    with mock.patch.object(_te_007d669.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
        _pool_before = _te_007d669.select_top100_pool(log=lambda *a, **k: None)
    _patch_sector_fill2.stop()
finally:
    if _saved_te_module is not None:
        sys.modules["top100_engine"] = _saved_te_module
    else:
        sys.modules.pop("top100_engine", None)

_canon = lambda pool: json.dumps(pool, sort_keys=True, default=str)
assert _canon(_pool_now) == _canon(_pool_before), (
    "select_top100_pool()'s return value must stay byte-identical to 007d669 after "
    "COMMIT 2 - it only ever touches submission/retry logic, never selection")
print(f"[select_top100_pool_byte_identical_to_007d669] {len(_pool_now)}-row pool, identical "
      f"sorted-JSON serialization against the 007d669 baseline OK")

if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))


print("\nALL TOP 200 COMMIT 2 (FAILED-COMPANY RE-ENTRY) FIXTURES PASSED")
