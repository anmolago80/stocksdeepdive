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
of the request's own entrants is recorded as "degenerate_response",
never "missing_from_response". A response carrying at least one real
judgement is untouched - today's per-item isolation still applies.

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
# recorded as degenerate_response, none as missing_from_response.
# ======================================================================
_logged = []
_text1 = json.dumps({"companies": [_degenerate_item("APAM")]})
_meta1 = {t: {"score_key": f"D2026-10-04-{t}", "most_recent_quarter": None}
          for t in ("APAM", "ASIC", "HIPO")}
_r1 = _poll_one("t100-0", _text1, "msgbatch_whole_degen_1", _meta1, log=_logged.append)
assert _r1["scored"] == 0 and _r1["failed"] == 3, _r1
_fail1 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
for t in ("APAM", "ASIC", "HIPO"):
    assert _fail1[t]["reason"] == "degenerate_response", (t, _fail1.get(t))
    assert ts.get_score(t, f"D2026-10-04-{t}", te.MODEL_TOP100, te.RUBRIC_VERSION) is None
assert any("whole response degenerate" in ln and "t100-0" in ln and "1 items for 3 entrants" in ln
           and "all 3 recorded as degenerate_response" in ln for ln in _logged), _logged
print("[live_evidence_shape] 3 entrants (APAM/ASIC/HIPO), 1 blank-ticket degenerate item -> "
      "ALL THREE recorded as degenerate_response (none missing_from_response), exact log line "
      "present OK")

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
    assert _fail2[t]["reason"] == "degenerate_response", (t, _fail2.get(t))
assert any("2 items for 4 entrants" in ln for ln in _logged2), _logged2
print("[fewer_items_uneven_scores] 2 non-identical-score, all-blank-field items for 4 entrants "
      "-> all 4 recorded as degenerate_response (condition (b), not just condition (a)) OK")

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
assert not any("whole response degenerate" in ln for ln in _logged3), _logged3
print("[real_item_among_blanks_unaffected] one real judgement present (2 entrants, 1 item) -> "
      "REAL1 saved, GONE1 still missing_from_response - per-item behaviour exactly as before "
      "this commit, no whole-response log line OK")

# ======================================================================
# CHECK 4: second CONSECUTIVE whole-response-degenerate attempt for the
# same entrants -> all accepted NOT RATED with degenerate_accepted=True
# (the existing two-strike path, now reached for every entrant of a
# whole-degenerate request, not just the one(s) an explicit ticker
# happened to land on).
# ======================================================================
_text4 = json.dumps({"companies": [_degenerate_item("APAM")]})
_r4 = _poll_one("t100-0b", _text4, "msgbatch_whole_degen_4", _meta1)
assert _r4["scored"] == 3 and _r4["failed"] == 0, _r4
for t in ("APAM", "ASIC", "HIPO"):
    _row = ts.get_score(t, f"D2026-10-04-{t}", te.MODEL_TOP100, te.RUBRIC_VERSION)
    assert _row is not None, t
    assert _row["not_rated"] is True, (t, _row)
    assert _row["degenerate_accepted"] is True, (t, _row)
_fail4 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
for t in ("APAM", "ASIC", "HIPO"):
    assert t not in _fail4, (t, "failure row must be cleared once accepted")
print("[second_whole_degenerate_accepted] second consecutive whole-response-degenerate attempt "
      "-> ALL 3 entrants accepted NOT RATED with degenerate_accepted=True, failure rows cleared "
      "OK")

# ======================================================================
# CHECK 5: RUBRIC_VERSION/prompt/schema/packing untouched by this
# commit (read-only confirmation, not a behaviour test).
# ======================================================================
assert te.RUBRIC_VERSION == "v6", te.RUBRIC_VERSION
print("[rubric_version_unchanged] RUBRIC_VERSION is still 'v6' OK")

print("\nALL TOP 200 COMMIT 1 (WHOLE-RESPONSE-DEGENERATE GUARD) FIXTURES PASSED")
