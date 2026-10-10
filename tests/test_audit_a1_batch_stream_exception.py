"""Audit fix A1 / Fable finding T1 (High) (10 Oct 2026, instruction_
combined_10oct.md PART A): top100_engine.poll_and_ingest_batch() must
treat a results-STREAM exception (the iterator itself raising partway
through, e.g. a dropped connection) the same as a retrieve() failure -
log it and return None, never fall through to record_score_failure()/
clear_batch_state().

Fable's scenario, reproduced here: a batch is in flight, retrieve()
succeeds ("ended"), but iterating client.messages.batches.results()
raises after N results have already been yielded (and, for whichever
of those N already fully parsed, already saved via save_score() -
real work done before the interruption is not discarded). Before this
fix, the code logged "batch result retrieval failed partway through"
and then UNCONDITIONALLY fell through to:
  - record a top100_score_failures row for every ticker collected in
    failure_reasons before the raise (none in this exact fixture, but
    would be real tickers in a bigger batch with some errored results
    before the interruption), and
  - top100_store.clear_batch_state() - which discards the in-flight
    batch's custom_id_map entirely, so a re-poll has nothing left to
    retry for the tickers that never got reached.

Run: python3 tests/test_audit_a1_batch_stream_exception.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_audit_a1_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

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


def _make_succeeded_result(custom_id, ticker):
    item = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
    item.update({
        "ticker": ticker,
        "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "",
        "big_wave": "", "big_wave_comment": "",
    })
    text = json.dumps({"companies": [item]})
    content_block = mock.Mock(type="text", text=text)
    usage = mock.Mock(input_tokens=1000, output_tokens=500,
                       cache_creation_input_tokens=0, cache_read_input_tokens=0)
    message = mock.Mock(content=[content_block], usage=usage)
    result = mock.Mock(type="succeeded", message=message)
    return mock.Mock(custom_id=custom_id, result=result)


def _seed_batch_state(entries):
    custom_id_map = {cid: {"entrants": {t: {"score_key": "D2026-09-30", "most_recent_quarter": None}}}
                      for cid, t in entries.items()}
    ts.save_batch_state("msgbatch_a1_test", "2026-09-30", te.MODEL_TOP100, custom_id_map)


def _raising_results_iter(good_results):
    """Yields each of `good_results`, then raises - simulating a
    connection drop partway through the Batches API results stream."""
    def gen():
        for r in good_results:
            yield r
        raise ConnectionError("stream interrupted (simulated)")
    return gen()


# ======================================================================
# CHECK: an iterator that raises after N results.
# ======================================================================
_seed_batch_state({"c1": "AAAA", "c2": "BBBB", "c3": "CCCC"})
_batch = mock.Mock(processing_status="ended")
_good = [_make_succeeded_result("c1", "AAAA")]

_logs = []
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = _batch
    instance.messages.batches.results.return_value = _raising_results_iter(_good)
    _result = te.poll_and_ingest_batch(log=_logs.append)

check("poll_and_ingest_batch() returns None when the results stream raises",
      _result is None)
check("the batch row still exists - NOT cleared - so a re-poll can retry the rest",
      ts.get_batch_state() is not None)
check("no top100_score_failures row was recorded for any ticker in this batch",
      ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION) == {})
check("the already-fully-parsed ticker's real score is still saved (no discarded "
      "work for what completed before the interruption)",
      ts.get_score("AAAA", "D2026-09-30", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None)
check("the log records the stream failure",
      any("batch result retrieval failed partway through" in line for line in _logs))

# ======================================================================
# CHECK: a re-poll (results stream now completes normally) scores the
# rest, since the batch row was preserved.
# ======================================================================
_batch2 = mock.Mock(processing_status="ended")
_all_good = [
    _make_succeeded_result("c1", "AAAA"),
    _make_succeeded_result("c2", "BBBB"),
    _make_succeeded_result("c3", "CCCC"),
]
with mock.patch("anthropic.Anthropic") as MockClient2:
    instance2 = MockClient2.return_value
    instance2.messages.batches.retrieve.return_value = _batch2
    instance2.messages.batches.results.return_value = iter(_all_good)
    _result2 = te.poll_and_ingest_batch(log=lambda *a, **k: None)

check("the re-poll succeeds and scores all 3 tickers (1 re-saved, 2 new)",
      _result2 is not None and _result2["scored"] == 3 and _result2["failed"] == 0)
check("the batch row is now cleared, since this poll completed cleanly",
      ts.get_batch_state() is None)
for _t in ("AAAA", "BBBB", "CCCC"):
    check(f"{_t} has a real saved score after the re-poll",
          ts.get_score(_t, "D2026-09-30", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
