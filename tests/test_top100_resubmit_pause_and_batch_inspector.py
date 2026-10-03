"""
Commit 1 (owner-directed, 3 Oct 2026, resubmission-loop investigation):
the 2 Oct 23:00 UTC run packed 215 companies into 43 requests and came
back 146 scored / 69 "missing from packed batch response" - 146 ~ 29*5
suggests whole REQUESTS came back empty, not 69 individual per-ticker
omissions. Two independent pieces, both covered here:

  1. The resubmission PAUSE (top100_engine.is_resubmit_paused()/set_
     resubmit_paused(), wired into submit_nightly_batch()) - holds back
     only an entrant whose sole reason to be submitted is a prior
     failure now eligible to retry; newcomers/results-driven re-scores
     are unaffected.
  2. The Batch inspector's pure parsing (top100_engine.
     inspect_batch_results()) - full match, partial match, empty
     array, errored result, and a ticker echoed under a different
     string than expected ("RG1" vs "RG1.AX").

Run: python3 tests/test_top100_resubmit_pause_and_batch_inspector.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_pause_inspector_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _company_item(ticker, score=4):
    item = {k: {"score": score, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
    item.update({
        "ticker": ticker,
        "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "",
        "big_wave": "", "big_wave_comment": "",
    })
    return item


# ======================================================================
# CHECK 1-5: inspect_batch_results() pure parsing on fixtures - full
# match, partial match (one of two expected missing), empty companies[]
# array, errored result, and a request where ONE item echoes its
# ticker under a stem-matching DIFFERENT string than expected ("RG1"
# for the expected "RG1.AX" - now resolved by _parse_response_json()'s
# own _normalize_echoed_ticker(), 3 Oct 2026, owner-directed) while a
# SECOND expected ticker in the same request is still genuinely
# missing - this must still land in partially_matched (one real gap),
# while tickers_echoed shows the NORMALISED string ("RG1.AX"), proving
# the stem resolution actually ran rather than just happening to count
# right.
# ======================================================================
full_text = json.dumps({"companies": [_company_item("AAPL"), _company_item("MSFT")]})
partial_text = json.dumps({"companies": [_company_item("AAPL")]})
empty_text = json.dumps({"companies": []})
mismatch_text = json.dumps({"companies": [_company_item("RG1")]})

results = [
    {"custom_id": "t100-0", "type": "succeeded", "stop_reason": "end_turn",
     "output_tokens": 500, "text": full_text},
    {"custom_id": "t100-1", "type": "succeeded", "stop_reason": "end_turn",
     "output_tokens": 300, "text": partial_text},
    {"custom_id": "t100-2", "type": "succeeded", "stop_reason": "end_turn",
     "output_tokens": 10, "text": empty_text},
    {"custom_id": "t100-3", "type": "errored", "error_detail": "overloaded_error: ..."},
    {"custom_id": "t100-4", "type": "succeeded", "stop_reason": "end_turn",
     "output_tokens": 200, "text": mismatch_text},
]
expected_map = {
    "t100-0": ["AAPL", "MSFT"],
    "t100-1": ["AAPL", "MSFT"],
    "t100-2": ["ADBE"],
    "t100-3": ["PAYX"],
    "t100-4": ["RG1.AX", "OTHER.AX"],
}
rows, summary = te.inspect_batch_results(results, expected_map)
assert summary == {"total": 5, "fully_matched": 1, "partially_matched": 2, "empty": 1, "errored": 1}, summary
_by_id = {r["custom_id"]: r for r in rows}
assert _by_id["t100-0"]["excerpt"] is None, "a fully-matched request shows no excerpt"
assert _by_id["t100-1"]["excerpt"] is not None
assert _by_id["t100-2"]["excerpt"] is not None
assert _by_id["t100-3"]["excerpt"] == "overloaded_error: ..."
# "RG1" resolved to "RG1.AX" (the ONLY expected ticker whose stem
# matches) - tickers_echoed shows the normalised string, not the raw
# one, and the request is still partially_matched because OTHER.AX
# never showed up at all.
assert _by_id["t100-4"]["tickers_echoed"] == ["RG1.AX"], _by_id["t100-4"]
assert _by_id["t100-4"]["expected_tickers"] == ["RG1.AX", "OTHER.AX"], _by_id["t100-4"]
assert _by_id["t100-4"]["match_basis"] == "ticker string match", _by_id["t100-4"]
assert _by_id["t100-2"]["match_basis"] == "ticker string match", _by_id["t100-2"]
print("[inspector_fixtures] full/partial/empty/errored classify correctly, and a stem-"
      f"normalised ticker echo ('RG1' -> 'RG1.AX') still correctly flags the request "
      f"partially_matched when a SECOND expected ticker is genuinely missing - summary "
      f"{summary} OK")

# No expected_map at all (a batch ingested before the custom_id_map_json
# column existed) - never crashes, buckets purely by item presence, and
# every succeeded row is labelled "count match only" rather than looking
# like a verified ticker-string match.
rows_noexp, summary_noexp = te.inspect_batch_results(results, expected_map=None)
assert summary_noexp["errored"] == 1 and summary_noexp["empty"] == 1
assert summary_noexp["fully_matched"] == 3, summary_noexp  # t100-0/1/4 all have >=1 item, no expected to compare against
_by_id_noexp = {r["custom_id"]: r for r in rows_noexp}
assert _by_id_noexp["t100-0"]["match_basis"] == "count match only - expected map unavailable"
assert _by_id_noexp["t100-4"]["tickers_echoed"] == ["RG1"], _by_id_noexp["t100-4"]  # no map -> no normalisation
print(f"[inspector_no_expected_map] missing expected_map never crashes, buckets by presence only, "
      f"and is labelled 'count match only' rather than a verified ticker-string match - "
      f"summary {summary_noexp} OK")


# ======================================================================
# CHECK 6: expected_tickers_for_batch()/record_ingest_cost() round trip
# - the custom_id_map passed to record_ingest_cost() survives the store
# and comes back out keyed the way the inspector needs.
# ======================================================================
_custom_id_map = {
    "t100-0": {"entrants": {"AAPL": {"score_key": "k1"}, "MSFT": {"score_key": "k2"}}},
    "t100-1": {"entrants": {"ADBE": {"score_key": "k3"}}},
}
ts.record_ingest_cost(
    batch_id="msgbatch_roundtrip_test", scored=2, failed=1,
    input_tokens=100, cache_creation_tokens=0, cache_read_tokens=0, output_tokens=50,
    cost_usd=0.01, custom_id_map=_custom_id_map,
)
_expected = ts.expected_tickers_for_batch("msgbatch_roundtrip_test")
assert _expected == {"t100-0": ["AAPL", "MSFT"], "t100-1": ["ADBE"]}, _expected
assert ts.most_recent_ingested_batch_id() == "msgbatch_roundtrip_test"
# A batch with no custom_id_map (pre-dates the column, or never passed) -
# expected_tickers_for_batch() must return None, not {} or a crash, so
# the inspector can tell "not available" from "zero requests".
ts.record_ingest_cost(
    batch_id="msgbatch_no_map_test", scored=1, failed=0,
    input_tokens=10, cache_creation_tokens=0, cache_read_tokens=0, output_tokens=5,
    cost_usd=0.001,
)
assert ts.expected_tickers_for_batch("msgbatch_no_map_test") is None
print("[ingest_log_roundtrip] custom_id_map survives record_ingest_cost()/expected_tickers_for_batch(); "
      "a batch with no map returns None, not a crash OK")


# ======================================================================
# CHECK 7: resubmission pause - default OFF, toggles cleanly, and
# the marker file actually lives under RAILWAY_VOLUME_MOUNT_PATH.
# ======================================================================
assert te.is_resubmit_paused() is False
te.set_resubmit_paused(True, log=lambda *a, **k: None)
assert te.is_resubmit_paused() is True
assert os.path.exists(os.path.join(TESTVOL, ".top100_resubmit_paused"))
te.set_resubmit_paused(False, log=lambda *a, **k: None)
assert te.is_resubmit_paused() is False
print("[pause_marker_toggle] default OFF, ON writes the marker file, OFF removes it OK")


# ======================================================================
# CHECK 8: submit_nightly_batch() - paused holds back ONLY the entrant
# whose sole reason is a prior failure now eligible to retry
# ("new_or_rubric" + a failure row on file); a genuine newcomer with NO
# failure row, and a results-driven "new_results"/"age" entrant (even
# one that happens to ALSO carry a stale failure row), are both
# submitted unchanged.
# ======================================================================
POOL = [
    {"ticker": "RETRY1", "company_name": "Retry One", "sector": "Industrials"},
    {"ticker": "NEWCO1", "company_name": "New Co", "sector": "Industrials"},
    {"ticker": "RESCORE1", "company_name": "Rescore One", "sector": "Industrials"},
]
ts.record_score_failure("RETRY1", te.MODEL_TOP100, te.RUBRIC_VERSION, "missing_from_response")

FAKE_ENTRANTS = [
    (POOL[0], "new_or_rubric"),   # RETRY1 - has a failure row -> held when paused
    (POOL[1], "new_or_rubric"),   # NEWCO1 - no failure row -> never held
    (POOL[2], "new_results"),     # RESCORE1 - real existing score drives this, never held
]


def _fake_batch_create(requests):
    return mock.Mock(id="msgbatch_pause_test")


te.set_resubmit_paused(True, log=lambda *a, **k: None)
with mock.patch.object(te, "_unscored_tickers", return_value=list(FAKE_ENTRANTS)), \
     mock.patch("anthropic.Anthropic") as MockClient:
    MockClient.return_value.messages.batches.create.side_effect = _fake_batch_create
    te.submit_nightly_batch(pool=POOL, log=lambda *a, **k: None)
    # Inspect what was actually submitted via the saved batch state's
    # own custom_id_map instead of reaching into the mock's call args -
    # exercises the real persisted shape, same as every other test here.
    state = ts.get_batch_state()
    submitted = set()
    for entry in state["custom_id_map"].values():
        submitted |= set(entry["entrants"].keys())
assert submitted == {"NEWCO1", "RESCORE1"}, submitted
print(f"[pause_holds_only_failure_retries] paused ON -> submitted {sorted(submitted)} "
      "(RETRY1 held, NEWCO1/RESCORE1 unaffected) OK")

ts.clear_batch_state()
te.set_resubmit_paused(False, log=lambda *a, **k: None)
with mock.patch.object(te, "_unscored_tickers", return_value=list(FAKE_ENTRANTS)), \
     mock.patch("anthropic.Anthropic") as MockClient:
    MockClient.return_value.messages.batches.create.side_effect = _fake_batch_create
    te.submit_nightly_batch(pool=POOL, log=lambda *a, **k: None)
    state2 = ts.get_batch_state()
    submitted2 = set()
    for entry in state2["custom_id_map"].values():
        submitted2 |= set(entry["entrants"].keys())
assert submitted2 == {"RETRY1", "NEWCO1", "RESCORE1"}, submitted2
print(f"[pause_off_submits_everyone] paused OFF -> submitted {sorted(submitted2)} (all 3) OK")

# ======================================================================
# CHECK 9: the one-off "set pause ON for this deploy" boot hook -
# fires exactly once (writes its own marker), and a second call is a
# no-op even if the owner has since turned the pause back off.
# ======================================================================
te.set_resubmit_paused(False, log=lambda *a, **k: None)
assert te.is_resubmit_paused() is False
te.set_resubmit_pause_on_this_deploy_once(log=lambda *a, **k: None)
assert te.is_resubmit_paused() is True, "first call must turn the pause ON"
te.set_resubmit_paused(False, log=lambda *a, **k: None)  # owner turns it back off
te.set_resubmit_pause_on_this_deploy_once(log=lambda *a, **k: None)
assert te.is_resubmit_paused() is False, "second call (already marker-guarded) must NOT re-enable it"
print("[boot_hook_fires_once] set_resubmit_pause_on_this_deploy_once() enables the pause exactly "
      "once per deploy, never re-enabling it after the owner turns it back off OK")


print("TOP100_RESUBMIT_PAUSE_AND_BATCH_INSPECTOR_SWEEP_DONE")
