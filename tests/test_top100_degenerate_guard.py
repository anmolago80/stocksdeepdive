"""
Degenerate-response guard + stored-score viewer (owner-directed, 3 Oct
2026, replacing the blocked "1b" raw-batch-lookup task) - live evidence
from the Batch inspector on msgbatch_01Jbz1uEmi1zesnrEbVHtchE: t100-3
returned 5 items with every score 0 and every text "" (no tickers);
t100-17 the same shape with every score 1. These are sentinel-filled
TEMPLATES, not real judgements - CL, WDAY, DUOL and RRL.AX were saved
as NOT RATED from this same pattern, with tickers present.

Fix lives in top100_engine.py:
  - _is_degenerate_item()/_parse_response_json(degenerate_out=...): an
    item with all ten dimension scores identical AND every free-text
    field empty is never saved as a score; it's recorded as a
    "degenerate_response" failure and retried, UNLESS the stored
    failure reason coming into this run was ALREADY "degenerate_
    response" (the second consecutive degenerate attempt), in which
    case it's accepted as NOT RATED with degenerate_accepted=True.
  - run_degenerate_sweep_once(): one-off, marker-guarded boot sweep
    that clears any already-stored v6 row matching the same pattern.
  - top100_store.py: matched_by column (threaded through _parse_
    response_json's matched_by_out) + degenerate_accepted column +
    latest_score_for_ticker() for the admin Stored score viewer panel.

This file covers (owner's own task item 4):
  - all-zero/all-empty item -> degenerate failure, not saved
  - all-one/all-empty item -> same
  - >=3 zeros with one justified non-zero dimension -> saved as
    ordinary NOT RATED, never flagged degenerate
  - a second CONSECUTIVE degenerate attempt -> accepted as NOT RATED
    with degenerate_accepted=True
  - run_degenerate_sweep_once() clears only matching rows and is
    idempotent (a second call, after the marker exists, is a no-op)

Run: python3 tests/test_top100_degenerate_guard.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_degenerate_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _company_item(ticker, score=4, justification="j"):
    item = {k: {"score": score, "justification": justification, "source_period": "FY25"}
            for k in te.DIMENSION_KEYS}
    item.update({
        "ticker": ticker,
        "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "",
        "big_wave": "", "big_wave_comment": "",
    })
    return item


def _degenerate_item(ticker, score=0):
    """All ten dimensions share the SAME score, every justification and
    every free-text field blank - the exact t100-3/t100-17 shape."""
    return _company_item(ticker, score=score, justification="")


def _make_result(custom_id, text, usage_tokens=(1000, 500, 0, 0)):
    content_block = mock.Mock(type="text", text=text)
    usage = mock.Mock(input_tokens=usage_tokens[0], output_tokens=usage_tokens[1],
                       cache_creation_input_tokens=usage_tokens[2], cache_read_input_tokens=usage_tokens[3])
    message = mock.Mock(content=[content_block], usage=usage)
    result = mock.Mock(type="succeeded", message=message)
    return mock.Mock(custom_id=custom_id, result=result)


_batch_ended = mock.Mock(processing_status="ended")


def _poll_one(custom_id, text, batch_id, entrants_meta):
    ts.save_batch_state(batch_id, "2026-10-03", te.MODEL_TOP100, {custom_id: {"entrants": entrants_meta}})
    with mock.patch("anthropic.Anthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.batches.retrieve.return_value = _batch_ended
        instance.messages.batches.results.return_value = iter([_make_result(custom_id, text)])
        return te.poll_and_ingest_batch(log=lambda *a, **k: None)


# ======================================================================
# CHECK 1: all-zero/all-empty item -> degenerate failure, not saved,
# and never confused with a genuine NOT RATED decline.
# ======================================================================
_zero_text = json.dumps({"companies": [_degenerate_item("ZERO1", score=0)]})
_zero_meta = {"ZERO1": {"score_key": "D2026-10-03-ZERO1", "most_recent_quarter": None}}
_r1 = _poll_one("c-zero", _zero_text, "msgbatch_degen_zero", _zero_meta)
assert _r1["scored"] == 0 and _r1["failed"] == 1, _r1
assert ts.get_score("ZERO1", "D2026-10-03-ZERO1", te.MODEL_TOP100, te.RUBRIC_VERSION) is None, (
    "a degenerate all-zero item must never be saved as a score")
_fail1 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _fail1["ZERO1"]["reason"] == "degenerate_response", _fail1["ZERO1"]
print("[all_zero_degenerate] all-zero/all-empty item -> recorded as a 'degenerate_response' "
      "failure, not saved as a score OK")

# ======================================================================
# CHECK 2: all-one/all-empty item -> same treatment (any identical-
# score sentinel template is caught, not just the all-zero shape).
# ======================================================================
_one_text = json.dumps({"companies": [_degenerate_item("ONE1", score=1)]})
_one_meta = {"ONE1": {"score_key": "D2026-10-03-ONE1", "most_recent_quarter": None}}
_r2 = _poll_one("c-one", _one_text, "msgbatch_degen_one", _one_meta)
assert _r2["scored"] == 0 and _r2["failed"] == 1, _r2
assert ts.get_score("ONE1", "D2026-10-03-ONE1", te.MODEL_TOP100, te.RUBRIC_VERSION) is None
_fail2 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _fail2["ONE1"]["reason"] == "degenerate_response", _fail2["ONE1"]
print("[all_one_degenerate] all-one/all-empty item -> same 'degenerate_response' failure, "
      "not saved OK")

# ======================================================================
# CHECK 3: >=3 zeros with one justified non-zero dimension -> saved as
# an ORDINARY NOT RATED score, never flagged degenerate - the guard
# must never swallow a legitimate decline.
# ======================================================================
_legit = _company_item("LEGIT1", score=0, justification="")
_legit["ai_exposure"] = {"score": 3, "justification": "genuine partial signal", "source_period": "FY25"}
_legit_text = json.dumps({"companies": [_legit]})
_legit_meta = {"LEGIT1": {"score_key": "D2026-10-03-LEGIT1", "most_recent_quarter": None}}
_r3 = _poll_one("c-legit", _legit_text, "msgbatch_degen_legit", _legit_meta)
assert _r3["scored"] == 1 and _r3["failed"] == 0, _r3
_legit_row = ts.get_score("LEGIT1", "D2026-10-03-LEGIT1", te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _legit_row is not None, "a legitimate NOT RATED score must still be saved"
assert _legit_row["not_rated"] is True, _legit_row
assert _legit_row["degenerate_accepted"] is False, _legit_row
print("[legit_not_rated_unaffected] >=3 zeros with one justified non-zero dimension -> saved "
      "as an ordinary NOT RATED score, never flagged degenerate OK")

# ======================================================================
# CHECK 4: a SECOND CONSECUTIVE degenerate attempt -> accepted as NOT
# RATED with degenerate_accepted=True - the stored failure reason
# coming into this run must already be "degenerate_response" (not
# merely "failed twice for any reason").
# ======================================================================
_second_text = json.dumps({"companies": [_degenerate_item("ZERO1", score=0)]})
_r4 = _poll_one("c-zero2", _second_text, "msgbatch_degen_zero2", _zero_meta)
assert _r4["scored"] == 1 and _r4["failed"] == 0, _r4
_zero1_row = ts.get_score("ZERO1", "D2026-10-03-ZERO1", te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _zero1_row is not None, "the second consecutive degenerate attempt must be accepted"
assert _zero1_row["not_rated"] is True, _zero1_row
assert _zero1_row["degenerate_accepted"] is True, _zero1_row
_fail_after = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert "ZERO1" not in _fail_after, "the failure row must be cleared once accepted"
print("[second_degenerate_accepted] a second CONSECUTIVE degenerate attempt -> accepted as "
      "NOT RATED with degenerate_accepted=True, failure row cleared OK")

# A fresh, FIRST-time degenerate ticker must NOT be accepted just
# because some OTHER ticker already has a degenerate failure on file -
# the "consecutive" check is per-ticker.
_fresh_text = json.dumps({"companies": [_degenerate_item("FRESH1", score=2)]})
_fresh_meta = {"FRESH1": {"score_key": "D2026-10-03-FRESH1", "most_recent_quarter": None}}
_r5 = _poll_one("c-fresh", _fresh_text, "msgbatch_degen_fresh", _fresh_meta)
assert _r5["scored"] == 0 and _r5["failed"] == 1, _r5
assert ts.get_score("FRESH1", "D2026-10-03-FRESH1", te.MODEL_TOP100, te.RUBRIC_VERSION) is None
print("[first_degenerate_not_accepted] a ticker's FIRST degenerate attempt is retried, not "
      "accepted, regardless of other tickers' failure history OK")

# ======================================================================
# CHECK 5: run_degenerate_sweep_once() clears only matching rows and is
# idempotent.
# ======================================================================
# A clean, legitimately-scored row that must survive the sweep untouched.
ts.save_score(
    ticker="CLEAN1", quarter="D2026-10-03-CLEAN1", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION,
    dims={k: {"score": 4, "justification": "solid", "source_period": "FY25"} for k in te.DIMENSION_KEYS},
    not_rated=False, inversion_scenario="some scenario", inversion_severity=2,
    prompt="{}", raw_response="{}",
)
# A degenerate row saved directly (bypassing the ingest guard), as if
# it had slipped through before this fix shipped - exactly what the
# sweep exists to retroactively clean up.
ts.save_score(
    ticker="SLIPPED1", quarter="D2026-10-03-SLIPPED1", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION,
    dims={k: {"score": 0, "justification": "", "source_period": None} for k in te.DIMENSION_KEYS},
    not_rated=True, inversion_scenario=None, inversion_severity=None,
    prompt="{}", raw_response="{}",
)
assert os.path.exists(te._degenerate_sweep_marker_path()) is False
_sweep_log = []
te.run_degenerate_sweep_once(model=te.MODEL_TOP100, log=_sweep_log.append)
assert ts.get_score("SLIPPED1", "D2026-10-03-SLIPPED1", te.MODEL_TOP100, te.RUBRIC_VERSION) is None, (
    "the sweep must delete a degenerate-shaped stored row")
assert ts.get_score("CLEAN1", "D2026-10-03-CLEAN1", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None, (
    "the sweep must never touch a legitimately-scored row")
_sweep_failures = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _sweep_failures.get("SLIPPED1", {}).get("reason") == "degenerate_response", _sweep_failures.get("SLIPPED1")
assert any("cleared" in ln and "SLIPPED1" in ln for ln in _sweep_log), _sweep_log
print("[sweep_clears_matching_only] run_degenerate_sweep_once() deletes a degenerate-shaped "
      "stored row and records its failure, while leaving a legitimate score row untouched OK")

# Idempotent: a second call, with the marker now in place, is a no-op
# (no further log line, nothing re-cleared, even if a new degenerate
# row were present - the marker alone short-circuits it).
ts.save_score(
    ticker="SLIPPED2", quarter="D2026-10-03-SLIPPED2", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION,
    dims={k: {"score": 1, "justification": "", "source_period": None} for k in te.DIMENSION_KEYS},
    not_rated=True, inversion_scenario=None, inversion_severity=None,
    prompt="{}", raw_response="{}",
)
_sweep_log_2 = []
te.run_degenerate_sweep_once(model=te.MODEL_TOP100, log=_sweep_log_2.append)
assert _sweep_log_2 == [], "a second call after the marker exists must log nothing and do nothing"
assert ts.get_score("SLIPPED2", "D2026-10-03-SLIPPED2", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None, (
    "the marker-guarded sweep must not re-run and must leave a post-marker row untouched")
print("[sweep_idempotent] a second run_degenerate_sweep_once() call, after the marker exists, "
      "is a complete no-op OK")

# ======================================================================
# CHECK 6: matched_by is recorded on the saved score row - "ticker" for
# an explicit echo, "position" for the blank-ticker positional
# fallback - and latest_score_for_ticker() surfaces it for the Stored
# score viewer admin panel.
# ======================================================================
_mb_tickers = ["MB1", "MB2"]
_mb_items = [_company_item("MB1"), _company_item("")]  # MB2 echoes blank -> positional
_mb_text = json.dumps({"companies": _mb_items})
_mb_meta = {t: {"score_key": f"D2026-10-03-{t}", "most_recent_quarter": None} for t in _mb_tickers}
_r6 = _poll_one("c-mb", _mb_text, "msgbatch_matched_by", _mb_meta)
assert _r6["scored"] == 2, _r6
_mb1_row = ts.latest_score_for_ticker("MB1", te.MODEL_TOP100)
_mb2_row = ts.latest_score_for_ticker("MB2", te.MODEL_TOP100)
assert _mb1_row["matched_by"] == "ticker", _mb1_row
assert _mb2_row["matched_by"] == "position", _mb2_row
print("[matched_by_recorded] matched_by is stored as 'ticker' for an explicit echo and "
      "'position' for the blank-ticker positional fallback, read back by latest_score_for_"
      "ticker() OK")

print("\nALL TOP 100 DEGENERATE-RESPONSE GUARD FIXTURES PASSED")
