"""
Top 200 Commit 1 (4 Oct 2026, Director-directed): a wholly blank packed
response fails every company in it the same way.

Live evidence (Andrew's Batch inspector screenshot, batch
msgbatch_01CZd7EPW99kfK1SNqaFUEfG, request t100-0): stop reason
end_turn, 1,561 output tokens, expected tickers ['APAM','ASIC','HIPO'],
tickers echoed []. Under the per-item logic alone, the one item
present (explicit ticker "APAM", sentinel-filled template) was
correctly flagged degenerate_response, but ASIC/HIPO (no item at all
for them) fell through to missing_from_response - two different
failure reasons for what is really one broken response.

Fix: top100_engine._whole_response_is_degenerate() + the check at the
top of _parse_response_json() - when the WHOLE packed response is
degenerate (every returned item is a sentinel template, OR there are
fewer items than entrants and every field present is blank), every one
of the request's own entrants is recorded under its own failure
reason, never "missing_from_response". A response carrying at least
one real judgement is untouched - today's per-item isolation still
applies.

SUPERSEDED IN PART by Commit A1 (5 Oct 2026, Director-directed, "a
blank request is not a strike against its companies"): the live
evidence showed about one request in three coming back wholly blank
regardless of which companies were inside it, so CL/WDAY/PIC.AX (good,
well-known companies) were being accepted as NOT RATED by chance after
two unlucky blank requests. The whole-response-degenerate case now
records its own "request_blank" reason (NOT "degenerate_response"),
which NEVER feeds the two-strike NOT-RATED acceptance path regardless
of how many consecutive attempts come back blank - see
TOP100_REQUEST_BLANK_MAX_RETRIES/_failure_exhausted() in top100_engine.py.
CHECK 1/2's log-line assertions and CHECK 4 below are rewritten for
this new behaviour; CHECK 3 (per-item isolation, untouched by either
commit) is unchanged. The genuine per-item degenerate guard (see
test_top100_degenerate_guard.py) is completely unaffected by this file.

No change to the prompt, schema, packing, weights, selection or
RUBRIC_VERSION (still "v6"). No retroactive rewrite of stored rows.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top100_whole_response_degenerate.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_whole_degen_test_")
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


def _degenerate_item(ticker="", score=0):
    """All ten dimensions share the SAME score, every justification and
    every free-text field blank - the exact t100-3/t100-17/t100-0
    shape. ticker="" by default (the live evidence's own blank-echo
    case)."""
    item = _company_item(ticker, score=score, justification="")
    if not ticker:
        item["ticker"] = ""
    return item


def _blank_but_uneven_item(ticker=""):
    """Every free-text field blank, but NOT all ten scores identical
    (0,1,2,...,9) - fails _is_degenerate_item()'s own stricter "all
    identical" test, but still qualifies under condition (b) (too few
    items, every field blank) when paired with a short item count."""
    item = {k: {"score": i % 4, "justification": "", "source_period": ""}
            for i, k in enumerate(te.DIMENSION_KEYS)}
    item.update({
        "ticker": ticker,
        "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "",
        "big_wave": "", "big_wave_comment": "",
    })
    return item


def _make_result(custom_id, text, usage_tokens=(1000, 500, 0, 0)):
    content_block = mock.Mock(type="text", text=text)
    usage = mock.Mock(input_tokens=usage_tokens[0], output_tokens=usage_tokens[1],
                       cache_creation_input_tokens=usage_tokens[2], cache_read_input_tokens=usage_tokens[3])
    message = mock.Mock(content=[content_block], usage=usage)
    result = mock.Mock(type="succeeded", message=message)
    return mock.Mock(custom_id=custom_id, result=result)


_batch_ended = mock.Mock(processing_status="ended")


def _poll_one(custom_id, text, batch_id, entrants_meta, log=None):
    ts.save_batch_state(batch_id, "2026-10-04", te.MODEL_TOP100, {custom_id: {"entrants": entrants_meta}})
    with mock.patch("anthropic.Anthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.batches.retrieve.return_value = _batch_ended
        instance.messages.batches.results.return_value = iter([_make_result(custom_id, text)])
        return te.poll_and_ingest_batch(log=log or (lambda *a, **k: None))


# ======================================================================
# CHECK 1 (the live evidence's own shape): 3 entrants, ONE blank-ticket
# degenerate-template item (fewer items than entrants) -> ALL THREE
# recorded as request_blank (NOT degenerate_response, NOT
# missing_from_response) - Commit A1's reclassification.
# ======================================================================
_logged = []
_text1 = json.dumps({"companies": [_degenerate_item("APAM")]})
_meta1 = {t: {"score_key": f"D2026-10-04-{t}", "most_recent_quarter": None}
          for t in ("APAM", "ASIC", "HIPO")}
_r1 = _poll_one("t100-0", _text1, "msgbatch_whole_degen_1", _meta1, log=_logged.append)
assert _r1["scored"] == 0 and _r1["failed"] == 3, _r1
_fail1 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
for t in ("APAM", "ASIC", "HIPO"):
    assert _fail1[t]["reason"] == "request_blank", (t, _fail1.get(t))
    assert ts.get_score(t, f"D2026-10-04-{t}", te.MODEL_TOP100, te.RUBRIC_VERSION) is None
assert any("request t100-0: blank" in ln and "1 items for 3 entrants" in ln
           and "3 recorded as request_blank, attempt 1/5" in ln for ln in _logged), _logged
print("[live_evidence_shape] 3 entrants (APAM/ASIC/HIPO), 1 blank-ticket degenerate item -> "
      "ALL THREE recorded as request_blank (none degenerate_response/missing_from_response), "
      "exact log line present OK")

# ======================================================================
# CHECK 2: fewer items than entrants, every field blank, but NOT an
# identical-score shape (condition (b), distinct from condition (a)
# above) -> same whole-request treatment.
# ======================================================================
_logged2 = []
_text2 = json.dumps({"companies": [_blank_but_uneven_item(), _blank_but_uneven_item()]})
_meta2 = {t: {"score_key": f"D2026-10-04-{t}", "most_recent_quarter": None}
          for t in ("UNV1", "UNV2", "UNV3", "UNV4")}
_r2 = _poll_one("t100-9", _text2, "msgbatch_whole_degen_2", _meta2, log=_logged2.append)
assert _r2["scored"] == 0 and _r2["failed"] == 4, _r2
_fail2 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
for t in ("UNV1", "UNV2", "UNV3", "UNV4"):
    assert _fail2[t]["reason"] == "request_blank", (t, _fail2.get(t))
assert any("2 items for 4 entrants" in ln and "request_blank" in ln for ln in _logged2), _logged2
print("[fewer_items_uneven_scores] 2 non-identical-score, all-blank-field items for 4 entrants "
      "-> all 4 recorded as request_blank (condition (b), not just condition (a)) OK")

# ======================================================================
# CHECK 3: one REAL item among blanks -> per-item behaviour UNCHANGED -
# the real judgement is saved, the genuinely-missing entrant is still
# "missing_from_response" (this response is not degenerate as a whole).
# ======================================================================
_logged3 = []
_text3 = json.dumps({"companies": [_company_item("REAL1")]})
_meta3 = {t: {"score_key": f"D2026-10-04-{t}", "most_recent_quarter": None}
          for t in ("REAL1", "GONE1")}
_r3 = _poll_one("t100-5", _text3, "msgbatch_whole_degen_3", _meta3, log=_logged3.append)
assert _r3["scored"] == 1 and _r3["failed"] == 1, _r3
assert ts.get_score("REAL1", "D2026-10-04-REAL1", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None
_fail3 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert _fail3["GONE1"]["reason"] == "missing_from_response", _fail3.get("GONE1")
assert not any("recorded as request_blank" in ln for ln in _logged3), _logged3
print("[real_item_among_blanks_unaffected] one real judgement present (2 entrants, 1 item) -> "
      "REAL1 saved, GONE1 still missing_from_response - per-item behaviour exactly as before "
      "this commit, no whole-response-blank log line OK")

# ======================================================================
# CHECK 4 (Commit A1, 5 Oct 2026, supersedes the old "second consecutive
# whole-degenerate -> NOT RATED" behaviour): a SECOND, and then up to a
# FIFTH, consecutive whole-blank attempt for the same entrants -> NEVER
# accepted as NOT RATED, attempts count climbs correctly, and after the
# fifth it stops being retried (permanently skipped, still unscored,
# never NOT RATED) - see TOP100_REQUEST_BLANK_MAX_RETRIES.
# ======================================================================
_text4 = json.dumps({"companies": [_degenerate_item("APAM")]})
for _attempt_no in range(2, 6):  # attempts 2, 3, 4, 5
    _logged4 = []
    _r4 = _poll_one(f"t100-0-try{_attempt_no}", _text4,
                     f"msgbatch_whole_degen_4_try{_attempt_no}", _meta1, log=_logged4.append)
    assert _r4["scored"] == 0 and _r4["failed"] == 3, (_attempt_no, _r4)
    for t in ("APAM", "ASIC", "HIPO"):
        assert ts.get_score(t, f"D2026-10-04-{t}", te.MODEL_TOP100, te.RUBRIC_VERSION) is None, (
            _attempt_no, t, "a blank request must NEVER produce a NOT RATED row")
    _fail4 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
    for t in ("APAM", "ASIC", "HIPO"):
        assert _fail4[t]["reason"] == "request_blank", (_attempt_no, t, _fail4.get(t))
        assert _fail4[t]["attempts"] == _attempt_no, (_attempt_no, t, _fail4[t])
    assert any(f"attempt {_attempt_no}/5" in ln for ln in _logged4), (_attempt_no, _logged4)
print("[never_not_rated_through_five_attempts] whole-blank requests for the SAME entrants, "
      "attempts 2 through 5 -> never NOT RATED at any point, attempts count climbs 2/5..5/5, "
      "exact log line present at each OK")

# A sixth attempt is where _failure_exhausted()/_unscored_tickers() stop
# RETRYING (this file tests the retry-count machinery directly, since
# _unscored_tickers() needs a real pool row, not just a failure record -
# the ingest-side "never NOT RATED" guarantee above already covers what
# poll_and_ingest_batch() itself can prove).
_exhausted_failure = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)["APAM"]
assert _exhausted_failure["attempts"] == 5, _exhausted_failure
assert te._failure_exhausted(_exhausted_failure) is True, (
    "a request_blank failure at 5 attempts must be exhausted (stop retrying), per "
    "TOP100_REQUEST_BLANK_MAX_RETRIES")
print("[exhausted_after_five] _failure_exhausted() reports True for a request_blank failure "
      "at exactly 5 attempts - _unscored_tickers() stops retrying it from here, still never "
      "NOT RATED OK")

# ======================================================================
# CHECK 5: RUBRIC_VERSION/prompt/schema/packing untouched by this
# commit (read-only confirmation, not a behaviour test).
# ======================================================================
assert te.RUBRIC_VERSION == "v6", te.RUBRIC_VERSION
print("[rubric_version_unchanged] RUBRIC_VERSION is still 'v6' OK")

print("\nALL TOP 200 COMMIT 1 (WHOLE-RESPONSE-DEGENERATE GUARD) FIXTURES PASSED")
