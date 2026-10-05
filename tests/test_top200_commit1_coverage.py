"""
Top 200 COMMIT 1 of instruction_top200_unrated_and_blank_replies_combined.md
(5 Oct 2026, Director-directed, owner-approved for build+push-on-go -
"automatic per-market coverage count").

Covers the engine-side logic behind the new Admin "Top 200 coverage"
panel (app.py's _render_top200_coverage_panel() is pure Streamlit glue
with no logic of its own beyond what these functions already return -
same precedent as test_top100_degenerate_accepted_panel.py):

  - top100_engine.company_status(): RATED / UNRATED_MODEL /
    UNRATED_FAILED / WAITING, including the reason-aware request_blank
    exhaustion threshold (5 attempts, not 3) via _failure_exhausted().
  - top100_engine.coverage_for_rows() / _log_coverage(): the per-market
    breakdown and the exact "[top100] coverage ..." log line for a
    mixed selection, plus the WARNING line firing at 11% unrated share
    but not at exactly 10% (the task's own ">=10 resolved and >10%
    unrated" rule).
  - top100_engine.unrated_failed_detail_for_rows(): the "large company -
    check" flag against a LARGE_COMPANY_UNIVERSES-listed saved scan.
  - top100_engine.waiting_detail_for_rows(): reason classification
    ("retrying" vs "deferred (newcomer)" vs "awaiting first score").
  - top100_engine.zeros_breakdown_for_rows(): UNRATED_MODEL-by-dimension
    counts and RATED-with-1/2-zeros counts.
  - select_top100_pool()'s public return value stays BYTE-IDENTICAL to
    007d669 (the commit immediately before this one) given the exact
    same input scan - the new coverage/zeros logging this commit adds
    is purely additive (log lines only), never changes the selected
    pool itself.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top200_commit1_coverage.py
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top200_commit1_coverage_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts
import scan_store

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _dims(score=4, justification="j"):
    return {k: {"score": score, "justification": justification, "source_period": "FY25"}
            for k in te.DIMENSION_KEYS}


def _blank_dims():
    return {k: {"score": None, "justification": None, "source_period": None} for k in te.DIMENSION_KEYS}


# ======================================================================
# CHECK 1: company_status() unit - all four statuses, including the
# request_blank reason-aware exhaustion threshold (5, not 3).
# ======================================================================
# RATED: a real score row, not_rated False.
_rated_row = {"not_rated": False, "degenerate_accepted": False, "dims": _dims()}
assert te.company_status(_rated_row, None) == "rated"

# UNRATED_MODEL: not_rated True, degenerate_accepted False (the model's
# own judgement, not a failure).
_unrated_model_row = {"not_rated": True, "degenerate_accepted": False, "dims": _blank_dims()}
assert te.company_status(_unrated_model_row, None) == "unrated_model"

# UNRATED_FAILED via degenerate_accepted=True (the A1/3 Oct sweep path).
_degenerate_row = {"not_rated": True, "degenerate_accepted": True, "dims": _blank_dims()}
assert te.company_status(_degenerate_row, None) == "unrated_failed"

# No score row at all + no failure on record at all -> WAITING.
assert te.company_status(None, None) == "waiting"

# No score row + a request_blank failure at attempt 2 (below its own
# 5-attempt ceiling) -> still WAITING (not exhausted).
_rb_attempt2 = {"reason": "request_blank", "attempts": 2}
assert te.company_status(None, _rb_attempt2) == "waiting"

# No score row + a request_blank failure at attempt 5 (AT its own
# 5-attempt ceiling) -> UNRATED_FAILED.
_rb_attempt5 = {"reason": "request_blank", "attempts": 5}
assert te.company_status(None, _rb_attempt5) == "unrated_failed"

# Sanity: a non-request_blank failure uses the ordinary 3-attempt
# ceiling, not the request_blank 5-attempt one.
_other_attempt3 = {"reason": "weird_error", "attempts": 3}
assert te.company_status(None, _other_attempt3) == "unrated_failed"
_other_attempt2 = {"reason": "weird_error", "attempts": 2}
assert te.company_status(None, _other_attempt2) == "waiting"

print("[company_status_unit] RATED/UNRATED_MODEL/UNRATED_FAILED(degenerate_accepted)/WAITING "
      "all correct; request_blank WAITING at attempt 2, UNRATED_FAILED at attempt 5 (its own "
      "5-attempt ceiling, not the ordinary 3) OK")


# ======================================================================
# CHECK 2: coverage_for_rows() / _log_coverage() - mixed selection
# across two markets, exact log line, and the WARNING threshold firing
# at 11% unrated share but NOT at exactly 10%.
# ======================================================================
def _save_score_row(ticker, not_rated=False, degenerate_accepted=False, score=4):
    ts.save_score(
        ticker=ticker, quarter=f"D2026-10-05-{ticker}", model=te.MODEL_TOP100,
        rubric_version=te.RUBRIC_VERSION,
        dims=(_blank_dims() if not_rated else _dims(score=score)),
        not_rated=not_rated, inversion_scenario=None, inversion_severity=None,
        degenerate_accepted=degenerate_accepted, prompt="{}", raw_response="{}",
    )


# Market USA: 100 resolved tickers, exactly 10 unrated_model -> 10.0%
# unrated share -> must NOT warn (rule is ">10%", not ">=10%").
_usa_rows = []
for i in range(90):
    t = f"USR{i}"
    _save_score_row(t, not_rated=False)
    _usa_rows.append({"ticker": t})
for i in range(10):
    t = f"USU{i}"
    _save_score_row(t, not_rated=True, degenerate_accepted=False)
    _usa_rows.append({"ticker": t})

# Market Australia (.AX): 100 resolved tickers, 11 unrated (1 model + 10
# failed) -> 11.0% unrated share -> MUST warn.
_au_rows = []
for i in range(89):
    t = f"AUR{i}.AX"
    _save_score_row(t, not_rated=False)
    _au_rows.append({"ticker": t})
_save_score_row("AUU0.AX", not_rated=True, degenerate_accepted=False)
_au_rows.append({"ticker": "AUU0.AX"})
for i in range(10):
    t = f"AUF{i}.AX"
    _save_score_row(t, not_rated=True, degenerate_accepted=True)
    _au_rows.append({"ticker": t})

# A couple of genuinely WAITING tickers (no score, no failure) in a
# third, small market (never reaches the >=10-resolved warning floor).
_waiting_rows = [{"ticker": "RYWAIT.TO"}, {"ticker": "RYWAIT2.TO"}]

_all_rows = _usa_rows + _au_rows + _waiting_rows
_log = []
_coverage = te._log_coverage(_all_rows, log=_log.append)
assert _coverage is not None, _log

_by_market = _coverage["by_market"]
assert _by_market["USA"] == {"rated": 90, "unrated_model": 10, "unrated_failed": 0, "waiting": 0}, _by_market["USA"]
assert _by_market["Australia"] == {"rated": 89, "unrated_model": 1, "unrated_failed": 10, "waiting": 0}, _by_market["Australia"]
assert _by_market["Canada"] == {"rated": 0, "unrated_model": 0, "unrated_failed": 0, "waiting": 2}, _by_market["Canada"]

_coverage_line = next(l for l in _log if l.startswith("[top100] coverage "))
assert "selected 202" in _coverage_line, _coverage_line
assert "rated 179" in _coverage_line, _coverage_line
assert "unrated_model 11" in _coverage_line, _coverage_line
assert "unrated_failed 10" in _coverage_line, _coverage_line
assert "waiting 2" in _coverage_line, _coverage_line
assert "Australia 89/1/10/0" in _coverage_line, _coverage_line
assert "USA 90/10/0/0" in _coverage_line, _coverage_line
print(f"[coverage_log_line] exact mixed-selection coverage line '{_coverage_line.strip()}' OK")

_warning_lines = [l for l in _log if "coverage WARNING" in l]
assert len(_warning_lines) == 1, _warning_lines
assert "Australia" in _warning_lines[0] and "11.0%" in _warning_lines[0], _warning_lines[0]
assert not any("USA" in l for l in _warning_lines), (
    "USA sits at exactly 10.0% unrated share - the rule is '>10%', not '>=10%', so it must "
    "NOT warn", _warning_lines)
print(f"[warning_threshold_11_not_10] Australia (11.0% unrated, 100 resolved) warns, USA "
      f"(exactly 10.0% unrated, 100 resolved) does not - exact line "
      f"'{_warning_lines[0].strip()}' OK")


# ======================================================================
# CHECK 3: unrated_failed_detail_for_rows() - the "large company -
# check" flag, keyed off a saved S&P 500 scan (one of LARGE_COMPANY_
# UNIVERSES) carrying the failed ticker.
# ======================================================================
def _save_universe(universe, tickers):
    payload = {
        "universe": universe, "source": "test",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_night": None,
        "rows": [{"Ticker": t} for t in tickers],
        "attention_lite": True, "degraded": False,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


_save_universe("S&P 500", ["AUF0.AX", "SOMEOTHERBIGCO"])

_failed_detail = te.unrated_failed_detail_for_rows(_au_rows + _usa_rows, model=te.MODEL_TOP100)
_by_ticker = {d["ticker"]: d for d in _failed_detail}
assert set(_by_ticker.keys()) == {f"AUF{i}.AX" for i in range(10)}, _by_ticker.keys()
assert _by_ticker["AUF0.AX"]["large_company"] is True, _by_ticker["AUF0.AX"]
for i in range(1, 10):
    assert _by_ticker[f"AUF{i}.AX"]["large_company"] is False, _by_ticker[f"AUF{i}.AX"]
print("[large_company_flag] AUF0.AX (member of the saved S&P 500 universe) flagged "
      "large_company=True; AUF1..9.AX (same unrated_failed status, not in any "
      "LARGE_COMPANY_UNIVERSES scan) flagged False OK")

if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))


# ======================================================================
# CHECK 4: waiting_detail_for_rows() - "retrying" (non-exhausted
# failure on record) vs "deferred (newcomer)" (_true_newcomer_gated())
# vs "awaiting first score" (neither).
# ======================================================================
ts.record_score_failure("RETRYME", te.MODEL_TOP100, te.RUBRIC_VERSION, "timeout")
_waiting_fixture_rows = [{"ticker": "RETRYME"}, {"ticker": "BRANDNEW"}]
# BRANDNEW has never been seen by pool_presence at all -> 0 consecutive
# nights -> _true_newcomer_gated() is True (fully gated) unless it
# already carries a score under a previous rubric version (it does
# not, in this fixture).
_waiting_detail = te.waiting_detail_for_rows(_waiting_fixture_rows, model=te.MODEL_TOP100)
_by_ticker = {d["ticker"]: d for d in _waiting_detail}
assert _by_ticker["RETRYME"]["reason"] == "retrying", _by_ticker["RETRYME"]
assert _by_ticker["RETRYME"]["attempts"] == 1, _by_ticker["RETRYME"]
assert _by_ticker["RETRYME"]["next_attempt_at"] is not None, _by_ticker["RETRYME"]
assert _by_ticker["BRANDNEW"]["reason"] == "deferred (newcomer)", _by_ticker["BRANDNEW"]
print("[waiting_detail_reasons] RETRYME (non-exhausted failure on record) -> 'retrying' with "
      "attempts=1 and a real next_attempt_at; BRANDNEW (never scored, never seen by pool "
      "presence) -> 'deferred (newcomer)' OK")

# A ticker that's held its pool slot for NEWCOMER_PERSISTENCE_NIGHTS
# nights already -> no longer gated -> "awaiting first score".
with mock.patch.object(ts, "pool_presence_map", return_value={"PERSISTED": te.NEWCOMER_PERSISTENCE_NIGHTS}):
    _waiting_detail2 = te.waiting_detail_for_rows([{"ticker": "PERSISTED"}], model=te.MODEL_TOP100)
assert _waiting_detail2[0]["reason"] == "awaiting first score", _waiting_detail2
print("[waiting_detail_awaiting_first_score] a ticker that has already held its pool slot for "
      "NEWCOMER_PERSISTENCE_NIGHTS nights (no longer newcomer-gated), no failure on record -> "
      "'awaiting first score' OK")


# ======================================================================
# CHECK 5: zeros_breakdown_for_rows() - UNRATED_MODEL-by-dimension
# counts and RATED-with-1/2-zeros counts, per market.
# ======================================================================
_zeros_dims_1missing = _dims(score=4)
_zeros_dims_1missing["ai_exposure"] = {"score": None, "justification": None, "source_period": None}
_save_score_row_full = lambda ticker, dims, not_rated=False, degenerate_accepted=False: ts.save_score(
    ticker=ticker, quarter=f"D2026-10-05-{ticker}", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION, dims=dims, not_rated=not_rated,
    inversion_scenario=None, inversion_severity=None,
    degenerate_accepted=degenerate_accepted, prompt="{}", raw_response="{}",
)
_save_score_row_full("ONEZERO", _zeros_dims_1missing)

_zeros_dims_2missing = _dims(score=4)
_zeros_dims_2missing["ai_exposure"] = {"score": None, "justification": None, "source_period": None}
_zeros_dims_2missing["management"] = {"score": None, "justification": None, "source_period": None}
_save_score_row_full("TWOZERO", _zeros_dims_2missing)

_unrated_model_dims = _dims(score=4)
_unrated_model_dims["pricing_power"] = {"score": None, "justification": None, "source_period": None}
_save_score_row_full("UMODEL1", _unrated_model_dims, not_rated=True, degenerate_accepted=False)

_zeros_rows = [{"ticker": "ONEZERO"}, {"ticker": "TWOZERO"}, {"ticker": "UMODEL1"}]
_zeros = te.zeros_breakdown_for_rows(_zeros_rows, model=te.MODEL_TOP100)
_usa_bucket = _zeros["by_market"]["USA"]
assert _usa_bucket["rated_with_1_zero"] == 1, _usa_bucket
assert _usa_bucket["rated_with_2_zeros"] == 1, _usa_bucket
assert _usa_bucket["unrated_model_by_dimension"]["pricing_power"] == 1, _usa_bucket
assert all(v == (1 if k == "pricing_power" else 0) for k, v in _usa_bucket["unrated_model_by_dimension"].items()), _usa_bucket
print("[zeros_breakdown] ONEZERO (1 zero dim, rated) -> rated_with_1_zero=1; TWOZERO (2 zero "
      "dims, rated) -> rated_with_2_zeros=1; UMODEL1 (unrated_model, exactly one null "
      "dimension - pricing_power) -> only pricing_power counted in "
      "unrated_model_by_dimension OK")


# ======================================================================
# CHECK 6: select_top100_pool()'s public return value is BYTE-IDENTICAL
# to 007d669 (the commit immediately before this one) given the same
# input scan - the new coverage/zeros logging is purely additive.
# ======================================================================
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


_fingerprint_rows = [_fingerprint_row(f"FP{i}", 40.0 + i) for i in range(12)]
if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))
_save_universe("S&P 500", [])  # overwritten by the real payload below
_payload = {
    "universe": "S&P 500", "source": "test",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "run_night": None, "rows": _fingerprint_rows, "attention_lite": True, "degraded": False,
}
_tmp = scan_store._path("S&P 500") + ".tmp"
with open(_tmp, "w") as f:
    json.dump(_payload, f)
os.replace(_tmp, scan_store._path("S&P 500"))

_FAKE_CADENCE2 = {"S&P 500": "daily"}
_patch_sector_fill2 = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill2.start()
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE2)):
    _pool_now = te.select_top100_pool(log=lambda *a, **k: None)
_patch_sector_fill2.stop()

_saved_te_module = sys.modules.pop("top100_engine", None)
try:
    _te_007d669 = _load_module_from_git("top100_engine.py", "top100_engine", "007d669")
    _patch_sector_fill3 = mock.patch.object(_te_007d669, "_fill_missing_sectors", lambda rows, log=print: None)
    _patch_sector_fill3.start()
    with mock.patch.object(_te_007d669.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE2)):
        _pool_before = _te_007d669.select_top100_pool(log=lambda *a, **k: None)
    _patch_sector_fill3.stop()
finally:
    if _saved_te_module is not None:
        sys.modules["top100_engine"] = _saved_te_module
    else:
        sys.modules.pop("top100_engine", None)

_canon = lambda pool: json.dumps(pool, sort_keys=True, default=str)
assert _canon(_pool_now) == _canon(_pool_before), (
    "select_top100_pool()'s return value must be byte-identical to 007d669 given the same "
    "input scan - COMMIT 1's coverage/zeros logging must be purely additive")
print(f"[select_top100_pool_byte_identical_to_007d669] {len(_pool_now)}-row pool, identical "
      f"sorted-JSON serialization against the 007d669 baseline OK")

if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))


# ======================================================================
# CHECK 7: unreachable for non-owner - grep-verify app.py's own page_
# admin_dashboard() gates the panel, same precedent as the A2 panel
# test file.
# ======================================================================
_app_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")).read()
_page_start = _app_src.index("def page_admin_dashboard():")
_page_body = _app_src[_page_start:]
assert "ai_gate.is_owner(paywall_engine.current_user_email())" in _page_body
_owner_check_pos = _page_body.index("ai_gate.is_owner(paywall_engine.current_user_email())")
assert "    _render_top200_coverage_panel()" in _page_body, (
    "_render_top200_coverage_panel() must be CALLED from inside page_admin_dashboard()")
_panel_call_pos = _page_body.index("    _render_top200_coverage_panel()")
assert _panel_call_pos > _owner_check_pos, (
    "_render_top200_coverage_panel() must be called AFTER the owner gate")
print("[unreachable_for_non_owner] page_admin_dashboard() checks ai_gate.is_owner() before "
      "_render_top200_coverage_panel() is ever called - unreachable for a non-owner OK")


print("\nALL TOP 200 COMMIT 1 (COVERAGE/ZEROS) FIXTURES PASSED")
