"""
Top 200 Commit A1 (5 Oct 2026, Director-directed, owner-approved 5 Oct -
"a blank request is not a strike against its companies").

Live evidence (instruction_top200_blank_replies_and_financials_gap.md):
about one packed request in three comes back wholly blank, whichever
companies are inside it, and the SAME companies score normally on
another night - a blank request says nothing about the companies
inside it. Before this commit, a whole-blank request fed the SAME
"degenerate_response" failure reason as a genuine per-item degenerate,
so two unlucky blank requests could get a perfectly good, well-known
company (CL, WDAY, PIC.AX on 5 Oct) accepted as NOT RATED by chance.

This file covers what test_top100_whole_response_degenerate.py does
NOT (see that file for the "never NOT RATED through five attempts" and
"one real item among blanks is unaffected" checks):
  - solo packing: an entrant whose CURRENT stored failure reason is
    "request_blank" is packed ALONE on its next submission, never
    grouped with any other entrant.
  - the 4 Oct strike conversion (run_request_blank_strike_conversion_
    once()): every stored "degenerate_response" row is converted to
    "request_blank", idempotent, never touches any other reason.
  - top100_store.convert_score_failure_reason(): the plain-UPDATE
    migration helper itself (attempts/failed_at untouched).
  - the per-batch aggregate log line
    "[top100] batch <id>: R requests, B blank (ids ...), S scored, F failed".
  - _failure_exhausted(): reason-aware retry limit (3 for an ordinary
    failure, 5 for request_blank).
  - the solo-request edge case: a SOLO (one-entrant) retry of a ticker
    already mid a request_blank chain that comes back blank AGAIN must
    keep accumulating under "request_blank", never fall back to the
    two-strike degenerate_response/NOT-RATED path just because it
    happens to be a one-entrant request (distinguishing this from
    test_top100_degenerate_guard.py's own ZERO1/ONE1 - a brand-new,
    never-retried solo ticker's degenerate response, which correctly
    stays on the pre-existing per-item degenerate_response path).

No change to the prompt, schema, packing of NON-retried entrants,
weights, selection or RUBRIC_VERSION (still "v6"). No already-scored
company is touched by any check here.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top100_request_blank.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_request_blank_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


# ======================================================================
# CHECK 1: top100_store.convert_score_failure_reason() - plain UPDATE,
# attempts/failed_at untouched, returns the converted ticker list, only
# touches rows matching the FROM reason under this exact (model,
# rubric_version).
# ======================================================================
ts.record_score_failure("CONV1", te.MODEL_TOP100, te.RUBRIC_VERSION, "degenerate_response")
ts.record_score_failure("CONV1", te.MODEL_TOP100, te.RUBRIC_VERSION, "degenerate_response")  # attempts -> 2
ts.record_score_failure("CONV2", te.MODEL_TOP100, te.RUBRIC_VERSION, "degenerate_response")
ts.record_score_failure("UNRELATED1", te.MODEL_TOP100, te.RUBRIC_VERSION, "missing_from_response")
_before = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
_before_attempts = _before["CONV1"]["attempts"]
_before_failed_at = _before["CONV1"]["failed_at"]
assert _before_attempts == 2, _before["CONV1"]

_converted = ts.convert_score_failure_reason(
    te.MODEL_TOP100, te.RUBRIC_VERSION, "degenerate_response", "request_blank")
assert sorted(_converted) == ["CONV1", "CONV2"], _converted
_after = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _after["CONV1"]["reason"] == "request_blank", _after["CONV1"]
assert _after["CONV2"]["reason"] == "request_blank", _after["CONV2"]
assert _after["CONV1"]["attempts"] == _before_attempts, (_after["CONV1"], _before_attempts)
assert _after["CONV1"]["failed_at"] == _before_failed_at, (_after["CONV1"], _before_failed_at)
assert _after["UNRELATED1"]["reason"] == "missing_from_response", _after["UNRELATED1"]
print("[convert_score_failure_reason] plain UPDATE converts only matching rows, leaves "
      "attempts/failed_at untouched, returns the converted ticker list, never touches an "
      "unrelated reason OK")

# A second call with nothing left to convert returns an empty list.
_converted_again = ts.convert_score_failure_reason(
    te.MODEL_TOP100, te.RUBRIC_VERSION, "degenerate_response", "request_blank")
assert _converted_again == [], _converted_again
print("[convert_score_failure_reason_empty] a second call with no matching rows left "
      "returns an empty list OK")


# ======================================================================
# CHECK 2: run_request_blank_strike_conversion_once() - the boot-time,
# marker-guarded sweep. Converts every stored degenerate_response row
# (the blanket, safety-direction-only migration - the stored schema has
# no per-request metadata to identify "the 23 from 4 Oct" surgically),
# is idempotent, never touches an unrelated reason.
# ======================================================================
ts.record_score_failure("OCT4A", te.MODEL_TOP100, te.RUBRIC_VERSION, "degenerate_response")
ts.record_score_failure("OCT4B", te.MODEL_TOP100, te.RUBRIC_VERSION, "degenerate_response")
ts.record_score_failure("STILL_MISSING", te.MODEL_TOP100, te.RUBRIC_VERSION, "missing_from_response")
assert os.path.exists(te._request_blank_strike_conversion_marker_path()) is False

_sweep_log = []
te.run_request_blank_strike_conversion_once(model=te.MODEL_TOP100, log=_sweep_log.append)
_swept = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _swept["OCT4A"]["reason"] == "request_blank", _swept["OCT4A"]
assert _swept["OCT4B"]["reason"] == "request_blank", _swept["OCT4B"]
assert _swept["STILL_MISSING"]["reason"] == "missing_from_response", _swept["STILL_MISSING"]
assert any("converted" in ln and "OCT4A" in ln and "OCT4B" in ln for ln in _sweep_log), _sweep_log
print("[strike_conversion_sweep] run_request_blank_strike_conversion_once(): every stored "
      "degenerate_response row is converted to request_blank, an unrelated reason is "
      "untouched, exact log line present OK")

# Idempotent: a second call (marker now in place) is a complete no-op,
# even if a NEW degenerate_response row appears afterward.
ts.record_score_failure("OCT4C", te.MODEL_TOP100, te.RUBRIC_VERSION, "degenerate_response")
_sweep_log_2 = []
te.run_request_blank_strike_conversion_once(model=te.MODEL_TOP100, log=_sweep_log_2.append)
assert _sweep_log_2 == [], "a second call after the marker exists must log nothing and do nothing"
_after_second = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _after_second["OCT4C"]["reason"] == "degenerate_response", (
    "the marker-guarded sweep must not re-run and must leave a post-marker row untouched")
print("[strike_conversion_idempotent] a second run_request_blank_strike_conversion_once() "
      "call, after the marker exists, is a complete no-op OK")


# ======================================================================
# CHECK 3: _failure_exhausted() - reason-aware retry limit.
# ======================================================================
assert te._failure_exhausted(None) is False
assert te._failure_exhausted({"reason": "missing_from_response", "attempts": 2}) is False
assert te._failure_exhausted({"reason": "missing_from_response", "attempts": 3}) is True
assert te._failure_exhausted({"reason": "request_blank", "attempts": 3}) is False, (
    "a request_blank failure gets 5 attempts, not 3")
assert te._failure_exhausted({"reason": "request_blank", "attempts": 4}) is False
assert te._failure_exhausted({"reason": "request_blank", "attempts": 5}) is True
print("[failure_exhausted_reason_aware] _failure_exhausted(): 3 attempts for an ordinary "
      "reason, 5 for request_blank OK")


# ======================================================================
# CHECK 4: solo packing - an entrant whose CURRENT stored failure
# reason is "request_blank" is packed ALONE on its next submission;
# every other entrant is still packed up to TOP100_COMPANIES_PER_
# REQUEST together, exactly as before this commit.
# ======================================================================
ts.record_score_failure("RETRY1", te.MODEL_TOP100, te.RUBRIC_VERSION, "request_blank")
# Age the failure past TOP100_FAILURE_RETRY_HOURS so it's eligible for
# retry again (same pattern test_audit_c1_no_resubmit_cap.py's own
# "retry window expires" check uses) - otherwise _unscored_tickers()
# would correctly hold it back for the 24h wait, same as any other
# failure, and it would never reach packing at all.
from datetime import datetime, timedelta, timezone as _tz
with ts._conn() as _conn:
    _old = (datetime.now(_tz.utc) - timedelta(hours=te.TOP100_FAILURE_RETRY_HOURS + 1)).isoformat()
    _conn.execute(
        "UPDATE top100_score_failures SET failed_at = ? WHERE ticker = ? AND model = ? AND rubric_version = ?",
        (_old, "RETRY1", te.MODEL_TOP100, te.RUBRIC_VERSION),
    )
pool = [{"ticker": "RETRY1", "company_name": "Retry One Co", "most_recent_quarter": None}]
for i in range(4):
    pool.append({"ticker": f"FRESH{i}", "company_name": f"Fresh {i} Co", "most_recent_quarter": None})
ts.seed_pool_presence({row["ticker"]: 3 for row in pool}, "2026-10-04")

_captured_requests = []


def _fake_request(**kw):
    _captured_requests.append(kw)
    return kw


with mock.patch("anthropic.Anthropic") as MockClient, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=_fake_request):
    instance = MockClient.return_value
    fake_batch = mock.Mock(id="msgbatch_solo_test")
    instance.with_options.return_value.messages.batches.create.return_value = fake_batch
    result_id = te.submit_nightly_batch(pool=pool, log=lambda *a, **k: None)

assert result_id == "msgbatch_solo_test", result_id
_state = ts.get_batch_state()
assert _state is not None
_entrants_by_custom_id = {cid: set(v["entrants"].keys()) for cid, v in _state["custom_id_map"].items()}
_retry1_custom_id = next(cid for cid, tickers in _entrants_by_custom_id.items() if "RETRY1" in tickers)
assert _entrants_by_custom_id[_retry1_custom_id] == {"RETRY1"}, (
    "RETRY1 (current failure reason request_blank) must be packed ALONE", _entrants_by_custom_id)
_other_packs = [tickers for cid, tickers in _entrants_by_custom_id.items() if cid != _retry1_custom_id]
assert len(_other_packs) == 1 and _other_packs[0] == {"FRESH0", "FRESH1", "FRESH2", "FRESH3"}, (
    "the other 4 fresh entrants must still be packed together", _other_packs)
print("[solo_packing_on_retry] an entrant with a stored request_blank failure is packed "
      "ALONE on retry; the other 4 fresh entrants are still packed together OK")
ts.clear_batch_state()



# ======================================================================
# CHECK 5: the per-batch aggregate log line - "[top100] batch <id>: R
# requests, B blank (ids ...), S scored, F failed" - counting every
# request the batch carried (R), only the wholly-blank ones (B, by
# custom_id), and the already-existing scored/failed entrant totals.
# ======================================================================
def _dims():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


def _real_item(ticker):
    item = _dims()
    item.update({
        "ticker": ticker,
        "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "",
        "big_wave": "", "big_wave_comment": "",
    })
    return item


def _blank_item():
    item = {k: {"score": 0, "justification": "", "source_period": None} for k in te.DIMENSION_KEYS}
    item.update({
        "ticker": "", "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "",
        "big_wave": "", "big_wave_comment": "",
    })
    return item


def _make_text_result(custom_id, text):
    content_block = mock.Mock(type="text", text=text)
    usage = mock.Mock(input_tokens=1000, output_tokens=500,
                       cache_creation_input_tokens=0, cache_read_input_tokens=0)
    message = mock.Mock(content=[content_block], usage=usage)
    result = mock.Mock(type="succeeded", message=message)
    return mock.Mock(custom_id=custom_id, result=result)


def _make_errored_result(custom_id):
    inner_err = mock.Mock(type="invalid_request_error", message="bad request")
    err_wrapper = mock.Mock(type="error", error=inner_err)
    err_wrapper.model_dump_json = mock.Mock(side_effect=Exception("no pydantic"))
    result = mock.Mock(type="errored", error=err_wrapper)
    return mock.Mock(custom_id=custom_id, result=result)


_agg_meta = {
    "t100-agg-0": {"entrants": {"AGGSCORED": {"score_key": "D2026-10-05-AGGSCORED", "most_recent_quarter": None}}},
    "t100-agg-1": {"entrants": {t: {"score_key": f"D2026-10-05-{t}", "most_recent_quarter": None}
                                 for t in ("AGGBLANK1", "AGGBLANK2")}},
    "t100-agg-2": {"entrants": {"AGGERRORED": {"score_key": "D2026-10-05-AGGERRORED", "most_recent_quarter": None}}},
}
ts.save_batch_state("msgbatch_agg_test", "2026-10-05", te.MODEL_TOP100, _agg_meta)
_agg_results = [
    _make_text_result("t100-agg-0", json.dumps({"companies": [_real_item("AGGSCORED")]})),
    _make_text_result("t100-agg-1", json.dumps({"companies": [_blank_item()]})),
    _make_errored_result("t100-agg-2"),
]
_agg_batch = mock.Mock(processing_status="ended")
_agg_log = []
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = _agg_batch
    instance.messages.batches.results.return_value = iter(_agg_results)
    _agg_result = te.poll_and_ingest_batch(log=_agg_log.append)

assert _agg_result["scored"] == 1 and _agg_result["failed"] == 3, _agg_result
_agg_line = next((ln for ln in _agg_log if ln.startswith("[top100] batch msgbatch_agg_test: ")
                   and "requests" in ln), None)
assert _agg_line is not None, _agg_log
assert "3 requests" in _agg_line, _agg_line
assert "1 blank (ids t100-agg-1)" in _agg_line, _agg_line
assert "1 scored" in _agg_line, _agg_line
assert "3 failed" in _agg_line, _agg_line
print("[batch_aggregate_log_line] '[top100] batch <id>: R requests, B blank (ids ...), "
      "S scored, F failed' - 3 requests, 1 blank (named by its own custom_id), 1 scored, "
      "3 failed OK")
ts.clear_batch_state()



# ======================================================================
# CHECK 6: a SOLO retry of a ticker already mid a request_blank chain
# that comes back blank again stays "request_blank" (never reverts to
# the two-strike degenerate_response/NOT-RATED path just because the
# request happens to carry only one entrant).
# ======================================================================
ts.record_score_failure("SOLORETRY1", te.MODEL_TOP100, te.RUBRIC_VERSION, "request_blank")
_solo_meta = {"SOLORETRY1": {"score_key": "D2026-10-05-SOLORETRY1", "most_recent_quarter": None}}
_solo_text = json.dumps({"companies": [_blank_item()]})
_solo_log = []
ts.save_batch_state("msgbatch_solo_retry_blank", "2026-10-05", te.MODEL_TOP100,
                     {"t100-solo-retry": {"entrants": _solo_meta}})
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = mock.Mock(processing_status="ended")
    instance.messages.batches.results.return_value = iter(
        [_make_text_result("t100-solo-retry", _solo_text)])
    _r_solo = te.poll_and_ingest_batch(log=_solo_log.append)
assert _r_solo["scored"] == 0 and _r_solo["failed"] == 1, _r_solo
assert ts.get_score("SOLORETRY1", "D2026-10-05-SOLORETRY1", te.MODEL_TOP100, te.RUBRIC_VERSION) is None, (
    "a solo retry that comes back blank again must NEVER be saved as NOT RATED")
_solo_fail = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _solo_fail["SOLORETRY1"]["reason"] == "request_blank", _solo_fail["SOLORETRY1"]
assert _solo_fail["SOLORETRY1"]["attempts"] == 2, _solo_fail["SOLORETRY1"]
assert any("request_blank, attempt 2/5" in ln for ln in _solo_log), _solo_log
print("[solo_retry_stays_request_blank] a SOLO retry of a ticker already mid a "
      "request_blank chain that comes back blank again stays 'request_blank' (attempt "
      "2/5), never NOT RATED, never reverts to degenerate_response just because it's a "
      "one-entrant request OK")


print("\nALL TOP 200 COMMIT A1 (REQUEST_BLANK) FIXTURES PASSED")
