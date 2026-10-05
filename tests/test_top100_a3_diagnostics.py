"""
Top 200 Commit A3 (5 Oct 2026, Director-directed, REVISED same day -
"diagnostics only: make the blank replies readable (the request itself
is NOT changed)").

This commit changes NOTHING about what is sent to the model (system
prompt, user prompt, response schema, model, max_tokens, companies per
request) or how a good reply is parsed/scored. It only adds:
  - one log line per request at ingest (top100_engine._log_request_
    diagnostic(), called from poll_and_ingest_batch() for EVERY
    result): custom id, entrant tickers in order, items returned,
    stop reason, output tokens, blank yes/no.
  - a "blank" field on inspect_batch_results()' own rows (reusing
    _whole_response_is_degenerate() read-only, never re-deciding
    anything) plus "input_tokens", and a pure-formatting CSV export
    (top100_engine.batch_inspector_csv()) for the Admin Batch
    inspector panel's download button.

This file covers:
  - inspect_batch_results(): "blank" True for a whole-blank packed
    request, False for a normal one and for an errored one; "input_
    tokens" carried through.
  - batch_inspector_csv(): correct header + rows, tickers joined with
    ";", blank rendered as yes/no.
  - poll_and_ingest_batch()'s new per-request diagnostic log line,
    for a succeeded-normal, succeeded-blank, and errored request alike.
  - a normal reply is parsed EXACTLY as before this commit (byte-
    identical score saved) - this commit touches no parsing logic.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top100_a3_diagnostics.py
"""
import csv
import io
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_a3_diag_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _real_item(ticker, score=4, justification="j"):
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


# ======================================================================
# CHECK 1: inspect_batch_results() - "blank" True for a whole-blank
# packed request (2 entrants, 1 blank item), False for a normal
# fully-matched one and for an errored one; "input_tokens" carried.
# ======================================================================
_raw_results = [
    {"custom_id": "t100-normal", "type": "succeeded", "stop_reason": "end_turn",
     "input_tokens": 9000, "output_tokens": 500,
     "text": json.dumps({"companies": [_real_item("AAPL"), _real_item("MSFT")]})},
    {"custom_id": "t100-blank", "type": "succeeded", "stop_reason": "end_turn",
     "input_tokens": 9100, "output_tokens": 1561,
     "text": json.dumps({"companies": [_blank_item()]})},
    {"custom_id": "t100-err", "type": "errored", "error_detail": "invalid_request_error: bad"},
]
_expected_map = {
    "t100-normal": ["AAPL", "MSFT"],
    "t100-blank": ["BLANK1", "BLANK2"],
    "t100-err": ["ERR1"],
}
_rows, _summary = te.inspect_batch_results(_raw_results, _expected_map)
_by_id = {r["custom_id"]: r for r in _rows}
assert _by_id["t100-normal"]["blank"] is False, _by_id["t100-normal"]
assert _by_id["t100-normal"]["input_tokens"] == 9000, _by_id["t100-normal"]
assert _by_id["t100-blank"]["blank"] is True, _by_id["t100-blank"]
assert _by_id["t100-blank"]["input_tokens"] == 9100, _by_id["t100-blank"]
assert _by_id["t100-err"]["blank"] is False, _by_id["t100-err"]
assert _by_id["t100-err"]["input_tokens"] is None, _by_id["t100-err"]
print("[inspect_batch_results_blank_field] blank=True only for the whole-blank packed "
      "request, False for normal and errored alike; input_tokens carried through for a "
      "succeeded result, None for errored OK")

# ======================================================================
# CHECK 2: batch_inspector_csv() - correct header, one row per request,
# tickers semicolon-joined, blank rendered as yes/no.
# ======================================================================
_csv_text = te.batch_inspector_csv(_rows)
_parsed_csv = list(csv.reader(io.StringIO(_csv_text)))
# COMMIT 4 of instruction_top200_unrated_and_blank_replies_combined.md
# (5 Oct 2026) adds two trailing columns, "schema_mode" and
# "packed_or_solo" - the header and the "blank" column's own index
# (no longer the last column) are updated accordingly; every other
# assertion in this file is unchanged.
assert _parsed_csv[0] == [
    "custom_id", "entrant_tickers", "entrant_count", "items_returned",
    "tickers_echoed", "stop_reason", "input_tokens", "output_tokens", "blank",
    "schema_mode", "packed_or_solo",
], _parsed_csv[0]
_BLANK_COL = _parsed_csv[0].index("blank")
_csv_by_id = {row[0]: row for row in _parsed_csv[1:]}
assert _csv_by_id["t100-normal"][1] == "AAPL;MSFT", _csv_by_id["t100-normal"]
assert _csv_by_id["t100-normal"][2] == "2", _csv_by_id["t100-normal"]
assert _csv_by_id["t100-normal"][_BLANK_COL] == "no", _csv_by_id["t100-normal"]
assert _csv_by_id["t100-blank"][1] == "BLANK1;BLANK2", _csv_by_id["t100-blank"]
assert _csv_by_id["t100-blank"][_BLANK_COL] == "yes", _csv_by_id["t100-blank"]
assert _csv_by_id["t100-err"][1] == "ERR1", _csv_by_id["t100-err"]
assert _csv_by_id["t100-err"][_BLANK_COL] == "no", _csv_by_id["t100-err"]
print("[batch_inspector_csv] header + rows correct, tickers semicolon-joined, blank "
      "rendered yes/no for all three request types OK")

# ======================================================================
# CHECK 3: poll_and_ingest_batch()'s new per-request diagnostic log
# line fires for a succeeded-normal, succeeded-blank, AND errored
# request alike - and a normal reply is parsed EXACTLY as before (the
# score is saved, byte-identical to what the pre-A3 engine would save).
# ======================================================================
def _make_text_result(custom_id, text, input_tokens=1000, output_tokens=500):
    content_block = mock.Mock(type="text", text=text)
    usage = mock.Mock(input_tokens=input_tokens, output_tokens=output_tokens,
                       cache_creation_input_tokens=0, cache_read_input_tokens=0)
    message = mock.Mock(content=[content_block], usage=usage, stop_reason="end_turn")
    result = mock.Mock(type="succeeded", message=message)
    return mock.Mock(custom_id=custom_id, result=result)


def _make_errored_result(custom_id):
    inner_err = mock.Mock(type="invalid_request_error", message="bad request")
    err_wrapper = mock.Mock(type="error", error=inner_err)
    err_wrapper.model_dump_json = mock.Mock(side_effect=Exception("no pydantic"))
    result = mock.Mock(type="errored", error=err_wrapper)
    return mock.Mock(custom_id=custom_id, result=result)


_diag_meta = {
    "t100-diag-normal": {"entrants": {"DIAGNORMAL1": {"score_key": "D2026-10-05-DIAGNORMAL1", "most_recent_quarter": None}}},
    "t100-diag-blank": {"entrants": {t: {"score_key": f"D2026-10-05-{t}", "most_recent_quarter": None}
                                      for t in ("DIAGBLANK1", "DIAGBLANK2")}},
    "t100-diag-err": {"entrants": {"DIAGERR1": {"score_key": "D2026-10-05-DIAGERR1", "most_recent_quarter": None}}},
}
ts.save_batch_state("msgbatch_a3_diag_test", "2026-10-05", te.MODEL_TOP100, _diag_meta)
_diag_results = [
    _make_text_result("t100-diag-normal", json.dumps({"companies": [_real_item("DIAGNORMAL1", score=5, justification="strong")]})),
    _make_text_result("t100-diag-blank", json.dumps({"companies": [_blank_item()]}), output_tokens=1561),
    _make_errored_result("t100-diag-err"),
]
_diag_log = []
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = mock.Mock(processing_status="ended")
    instance.messages.batches.results.return_value = iter(_diag_results)
    _diag_result = te.poll_and_ingest_batch(log=_diag_log.append)

assert any(
    "diag request t100-diag-normal:" in ln and "['DIAGNORMAL1']" in ln and "items=1" in ln
    and "stop_reason=end_turn" in ln and "output_tokens=500" in ln and "blank=no" in ln
    for ln in _diag_log
), _diag_log
assert any(
    "diag request t100-diag-blank:" in ln and "items=1" in ln
    and "output_tokens=1561" in ln and "blank=yes" in ln
    for ln in _diag_log
), _diag_log
assert any(
    "diag request t100-diag-err:" in ln and "items=0" in ln
    and "stop_reason=None" in ln and "output_tokens=None" in ln and "blank=no" in ln
    for ln in _diag_log
), _diag_log
print("[diagnostic_log_line_all_outcomes] the new per-request diagnostic line fires for a "
      "succeeded-normal, succeeded-blank, and errored request alike, with the correct "
      "fields in each OK")

_saved_row = ts.get_score("DIAGNORMAL1", "D2026-10-05-DIAGNORMAL1", te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _saved_row is not None, "a normal reply must still be saved exactly as before this commit"
assert _saved_row["dims"]["ai_exposure"]["score"] == 5, _saved_row
assert _saved_row["dims"]["ai_exposure"]["justification"] == "strong", _saved_row
assert _saved_row["not_rated"] is False, _saved_row
print("[normal_reply_parsed_unchanged] a normal succeeded reply is parsed and saved "
      "exactly as before this commit (byte-identical score/justification) OK")

print("\nALL TOP 200 COMMIT A3 (DIAGNOSTICS ONLY) FIXTURES PASSED")
