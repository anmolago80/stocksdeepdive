"""
Director TASK 46 + addendum (12 Nov 2026): wire Quality on sale scoring
into the real nightly Batch API ingest, as its OWN category - never the
is_prescore bucket, never the prescore $-budget tracker (Director's
explicit choice).

  1. Score only qualifiers with NO stored research score under the
     CURRENT rubric (unscored, or scored only under a retired rubric -
     the addendum's CPRT case) - Top 200/extension members and
     anything already current-rubric-scored reuse their existing
     score, never re-scored for this tab.
  2. Max 100 new research scores per night, within the existing daily
     cap.
  3. Log cost on its own line: "[quality_on_sale] scored N, cost $X".
  4. Behind TOP200_QUALITY_ON_SALE_LIVE (unaffected by this file - the
     scoring/ingest pipeline itself carries no live-gate check; the
     gate only controls whether the TAB is shown publicly).

Covers:
  - quality_on_sale_candidates()'s own "research_score_stale_rubric"
    field (unit, via top100_store.latest_score_for_ticker()).
  - _select_quality_on_sale_entrants()'s new prior_entrant_count
    signature, broadened "needing scoring" (unscored OR stale-rubric),
    capacity math (batch_room/daily_room/max_new_scores).
  - submit_nightly_batch() end to end: quality-on-sale packs are
    built, tagged is_quality_on_sale in custom_id_map, never merged
    into normal/prescore packs, skipped entirely on force=True, and
    correctly claim capacity AFTER both normal and prescore entrants
    (prior_entrant_count = len(entrants) + len(prescore_entrants)) -
    including the daily-submission-count fix (quality_on_sale_entrants
    must count toward record_daily_submission(), same as prescore).
  - poll_and_ingest_batch() end to end: a quality-on-sale-tagged
    result updates qos_saved/qos_failed/tokens separately from both
    the overall and prescore counters, and the final "[quality_on_sale]
    scored N, cost $X" line fires only when non-zero, with the real
    (ingest-time) cost.
  - log_quality_on_sale_nightly_summary()'s "needing_scoring" count
    now includes stale-rubric candidates too.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task46_quality_on_sale_scoring.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task46_quality_on_sale_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
for _k in ("TOP200_PRESCORE_UNIVERSES", "TOP200_PRESCORE_BUDGET_USD", "TOP200_PRESCORE_UNTIL",
           "TOP200_QUALITY_ON_SALE_LIVE", "PRIVATE_UNIVERSES"):
    os.environ.pop(_k, None)

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


def _candidate(ticker, research_score=None, stale=False, drop_pct=-50.0, in_top_200=False):
    return {
        "ticker": ticker, "company_name": f"{ticker} Co", "sector": "Industrials",
        "most_recent_quarter": None, "quality_used": 80.0, "quality_used_source": "v2",
        "drop_pct": drop_pct, "peak_date": None, "peak_price": 10.0, "current_price": 5.0,
        "quality_exception_admitted": False, "in_top_200": in_top_200,
        "research_score": research_score, "research_score_stale_rubric": stale,
        "moat": None,
    }


# ======================================================================
# CHECK 1-4: quality_on_sale_candidates()'s own research_score_stale_
# rubric field - unit, via a mocked latest_score_for_ticker().
# ======================================================================
_fake_best = {
    "STALETICKER": {"ticker": "STALETICKER", "company_name": "Stale Co", "sector": "Industrials",
                     "quality": 70.0, "quality_v2": 80.0, "quality_exception_admitted": False, "moat": None},
    "FRESHTICKER": {"ticker": "FRESHTICKER", "company_name": "Fresh Co", "sector": "Industrials",
                     "quality": 70.0, "quality_v2": 80.0, "quality_exception_admitted": False, "moat": None},
    "NEVERTICKER": {"ticker": "NEVERTICKER", "company_name": "Never Co", "sector": "Industrials",
                     "quality": 70.0, "quality_v2": 80.0, "quality_exception_admitted": False, "moat": None},
}
_fake_drops = {
    t: {"drop_pct": -50.0, "peak_date": None, "peak_price": 10.0, "current_price": 5.0}
    for t in _fake_best
}
_fake_score_rows = {
    "STALETICKER": {"rubric_version": "v1", "dims": {k: {"score": 4} for k in te.DIMENSION_KEYS},
                     "not_rated": False},
    "FRESHTICKER": {"rubric_version": te.RUBRIC_VERSION, "dims": {k: {"score": 4} for k in te.DIMENSION_KEYS},
                     "not_rated": False},
}


def _fake_latest_score(ticker, model):
    return _fake_score_rows.get(ticker)


with mock.patch.object(te, "_build_best_by_ticker", return_value=(_fake_best, 3)), \
     mock.patch.object(te.top100_store, "current_pool", return_value=[]), \
     mock.patch.object(te.top100_store, "latest_score_for_ticker", side_effect=_fake_latest_score), \
     mock.patch.object(te.drop_chip_store, "get", side_effect=lambda t: _fake_drops.get(t)):
    _result = te.quality_on_sale_candidates()

_by_ticker = {c["ticker"]: c for c in _result["candidates"]}
check("a candidate scored under an OLDER rubric_version is flagged "
      "research_score_stale_rubric=True, with a real (non-None) research_score",
      _by_ticker["STALETICKER"]["research_score_stale_rubric"] is True
      and _by_ticker["STALETICKER"]["research_score"] is not None)
check("a candidate scored under the CURRENT rubric_version is flagged "
      "research_score_stale_rubric=False", _by_ticker["FRESHTICKER"]["research_score_stale_rubric"] is False)
check("a candidate with NO stored score at all has research_score=None and "
      "research_score_stale_rubric=False (never true for a ticker with no score row)",
      _by_ticker["NEVERTICKER"]["research_score"] is None
      and _by_ticker["NEVERTICKER"]["research_score_stale_rubric"] is False)
check("total_qualifying matches all 3 fixture tickers", _result["total_qualifying"] == 3)


# ======================================================================
# CHECK 5-11: _select_quality_on_sale_entrants() - broadened "needing
# scoring" (unscored OR stale-rubric), capacity math, logging.
# ======================================================================
_today = datetime.now(timezone.utc).date()

_mixed_candidates = {
    "candidates": [
        _candidate("SCOREDCURRENT", research_score=90.0, stale=False),
        _candidate("SCOREDSTALE", research_score=70.0, stale=True),
        _candidate("UNSCORED1", research_score=None, stale=False),
        _candidate("UNSCORED2", research_score=None, stale=False),
    ],
    "total_qualifying": 4,
}

with mock.patch.object(te, "quality_on_sale_candidates", return_value=_mixed_candidates):
    _entrants_full_room = te._select_quality_on_sale_entrants(
        prior_entrant_count=0, daily_entrants_so_far=0, today=_today, log=lambda *a: None)

_entrant_tickers = [e[0]["ticker"] for e in _entrants_full_room]
check("a current-rubric-scored candidate (SCOREDCURRENT) is NEVER queued for re-scoring",
      "SCOREDCURRENT" not in _entrant_tickers)
check("a stale-rubric-scored candidate (SCOREDSTALE) IS queued for re-scoring "
      "(the TASK 46 addendum's CPRT case)", "SCOREDSTALE" in _entrant_tickers)
check("a genuinely unscored candidate IS queued", "UNSCORED1" in _entrant_tickers and "UNSCORED2" in _entrant_tickers)
check("every returned entrant is tagged with reason 'quality_on_sale'",
      all(reason == "quality_on_sale" for _row_, reason in _entrants_full_room))
check("entrant row shape matches _select_prescore_entrants()'s own convention "
      "(ticker/company_name/sector/most_recent_quarter, nothing extra from the "
      "candidate dict leaking through)",
      set(_entrants_full_room[0][0].keys()) == {"ticker", "company_name", "sector", "most_recent_quarter"})

with mock.patch.object(te, "quality_on_sale_candidates", return_value=_mixed_candidates):
    _entrants_zero_batch_room = te._select_quality_on_sale_entrants(
        prior_entrant_count=te.MAX_NIGHTLY_SCORES, daily_entrants_so_far=0, today=_today, log=lambda *a: None)
check("zero batch room left (prior_entrant_count already fills MAX_NIGHTLY_SCORES) "
      "queues nothing", _entrants_zero_batch_room == [])

with mock.patch.object(te, "quality_on_sale_candidates", return_value=_mixed_candidates):
    _entrants_daily_capped = te._select_quality_on_sale_entrants(
        prior_entrant_count=0, daily_entrants_so_far=te.TOP100_MAX_ENTRANTS_PER_UTC_DAY - 1,
        today=_today, log=lambda *a: None)
check("the DAILY cap (not just the per-batch cap) also limits room - with only 1 "
      "slot of daily room left, at most 1 entrant is queued even though 3 need scoring",
      len(_entrants_daily_capped) <= 1)

with mock.patch.object(te, "quality_on_sale_candidates", return_value=_mixed_candidates):
    _entrants_max_new = te._select_quality_on_sale_entrants(
        prior_entrant_count=0, daily_entrants_so_far=0, today=_today, log=lambda *a: None, max_new_scores=1)
check("max_new_scores (the 100/night cap) is respected even with full batch/daily room",
      len(_entrants_max_new) == 1)

_all_scored = {
    "candidates": [_candidate("ALLSCORED", research_score=90.0, stale=False)],
    "total_qualifying": 1,
}
with mock.patch.object(te, "quality_on_sale_candidates", return_value=_all_scored):
    _logs_none_needed = []
    _entrants_none_needed = te._select_quality_on_sale_entrants(
        prior_entrant_count=0, daily_entrants_so_far=0, today=_today, log=_logs_none_needed.append)
check("every qualifier already current-rubric-scored queues 0 and logs that explicitly",
      _entrants_none_needed == [] and any("already scored under the current rubric" in m for m in _logs_none_needed))


# ======================================================================
# CHECK 12: log_quality_on_sale_nightly_summary()'s "needing scoring"
# count now includes stale-rubric candidates, not just unscored ones.
# ======================================================================
with mock.patch.object(te, "quality_on_sale_candidates", return_value=_mixed_candidates):
    _summary_logs = []
    te.log_quality_on_sale_nightly_summary(log=_summary_logs.append)
_summary_line = next(m for m in _summary_logs if m.startswith("[quality_on_sale] 4 total"))
check("the nightly summary counts 3 needing scoring (2 unscored + 1 stale-rubric), "
      "not just the 2 genuinely-unscored ones", "3 needing scoring" in _summary_line)


# ======================================================================
# CHECK 13-20: submit_nightly_batch() end to end - quality-on-sale
# entrants get their OWN packs, tagged is_quality_on_sale, claim
# capacity AFTER normal + prescore, never appear on force=True, and
# the daily-submission-count fix (quality_on_sale_entrants counted
# too).
# ======================================================================
def _mk_pool(n):
    return [{"ticker": f"POOL{i}", "company_name": f"Pool Co {i}", "sector": "Industrials",
              "most_recent_quarter": None} for i in range(n)]


def _submit_and_capture(pool, qos_candidates_result=None, force=False):
    """Mirrors tests/test_prescoring_promo.py's own _submit_and_capture()
    pattern - mocked Anthropic client, captures the submitted requests,
    plus mocks quality_on_sale_candidates() (defaulting to "nothing
    qualifies") so the quality-on-sale selection doesn't depend on a
    real scan/drop_chip_store fixture."""
    ts.seed_pool_presence({row["ticker"]: te.NEWCOMER_PERSISTENCE_NIGHTS for row in pool}, "2026-10-09")
    _qos_result = qos_candidates_result if qos_candidates_result is not None else {
        "candidates": [], "total_qualifying": 0,
    }
    with mock.patch("anthropic.Anthropic") as MockClient, \
         mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                    side_effect=lambda **kw: kw), \
         mock.patch("anthropic.types.messages.batch_create_params.Request",
                    side_effect=lambda **kw: kw), \
         mock.patch.object(te, "quality_on_sale_candidates", return_value=_qos_result):
        instance = MockClient.return_value
        fake_batch = mock.Mock(id="msgbatch_qos_test")
        instance.with_options.return_value.messages.batches.create.return_value = fake_batch
        logs = []
        result = te.submit_nightly_batch(pool=pool, log=logs.append, force=force)
        captured_requests = None
        if instance.with_options.return_value.messages.batches.create.called:
            captured_requests = instance.with_options.return_value.messages.batches.create.call_args.kwargs.get("requests")
        return result, captured_requests, logs


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_qos_two_unscored = {
    "candidates": [
        _candidate("QOS1", research_score=None, stale=False),
        _candidate("QOS2", research_score=None, stale=False),
    ],
    "total_qualifying": 2,
}
_pool3 = _mk_pool(3)
_result_on, _requests_on, _logs_on = _submit_and_capture(_pool3, qos_candidates_result=_qos_two_unscored)
check("switch has qualifiers: 2 requests total (3 normal pack into 1 request since "
      "TOP100_COMPANIES_PER_REQUEST=5, quality-on-sale's 2 entrants pack into their OWN, "
      "separate request)", len(_requests_on) == 2)

_batch_state = ts.get_batch_state()
_qos_tagged = [v for v in _batch_state["custom_id_map"].values() if v.get("is_quality_on_sale")]
_non_qos_tagged = [v for v in _batch_state["custom_id_map"].values() if not v.get("is_quality_on_sale")]
check("exactly one pack is tagged is_quality_on_sale=True, carrying QOS1/QOS2",
      len(_qos_tagged) == 1 and set(_qos_tagged[0]["entrants"].keys()) == {"QOS1", "QOS2"})
check("the normal pack is NOT tagged is_quality_on_sale (defaults falsy) and also "
      "NOT tagged is_prescore (this feature never touches the prescore bucket)",
      all(not p.get("is_quality_on_sale") and not p.get("is_prescore") for p in _non_qos_tagged))
check("the quality-on-sale pack itself is NOT tagged is_prescore - a third, fully "
      "independent category, never commingled with the prescore bucket",
      all(not p.get("is_prescore") for p in _qos_tagged))

_daily_after = ts.get_daily_submission_state(datetime.now(timezone.utc).date().isoformat())
check("the daily submission count includes the quality-on-sale entrants too "
      "(3 normal + 2 quality-on-sale = 5) - the TASK 46 fix for the bug where "
      "record_daily_submission() omitted quality_on_sale_entrants entirely",
      _daily_after["entrants"] == 5)
check("the submission log line mentions the quality-on-sale companies queued",
      any("quality-on-sale compan" in m for m in _logs_on))

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_pool3b = _mk_pool(3)
_result_force, _requests_force, _logs_force = _submit_and_capture(
    _pool3b, qos_candidates_result=_qos_two_unscored, force=True)
check("force=True (an explicit owner-initiated refresh) NEVER also spends on "
      "quality-on-sale, even with qualifiers waiting - exactly 1 request (3 normal "
      "only, no quality-on-sale pack appended)", len(_requests_force) == 1)
_batch_state_force = ts.get_batch_state()
check("no pack is tagged is_quality_on_sale on a force=True submission",
      not any(v.get("is_quality_on_sale") for v in _batch_state_force["custom_id_map"].values()))

# Capacity: quality-on-sale claims room AFTER normal + prescore - here
# normal entrants alone (via a tiny pool) already fill MAX_NIGHTLY_SCORES,
# so quality-on-sale (and prescore) get zero batch room.
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_big_pool = _mk_pool(te.MAX_NIGHTLY_SCORES)
_result_noroom, _requests_noroom, _logs_noroom = _submit_and_capture(
    _big_pool, qos_candidates_result=_qos_two_unscored)
_batch_state_noroom = ts.get_batch_state()
check("with normal entrants alone already filling MAX_NIGHTLY_SCORES, zero batch room "
      "is left for quality-on-sale - no pack tagged is_quality_on_sale",
      not any(v.get("is_quality_on_sale") for v in _batch_state_noroom["custom_id_map"].values()))

with ts._conn() as conn:
    conn.execute("DELETE FROM top100_daily_submissions")


# ======================================================================
# CHECK 21-24: poll_and_ingest_batch() end to end - a quality-on-sale-
# tagged result updates qos_saved/qos_failed/tokens separately from
# both the overall and prescore counters, and the final log line fires
# only when non-zero, with the real ingest-time cost.
# ======================================================================
def _mk_result(custom_id, tickers, input_tokens=1000, output_tokens=500):
    companies = []
    for t in tickers:
        item = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
        item.update({
            "ticker": t,
            "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
            "market_structure": "", "market_structure_comment": "",
            "one_foot_hurdle": "", "one_foot_comment": "",
            "munger_quality": "", "munger_comment": "",
            "big_wave": "", "big_wave_comment": "",
        })
        companies.append(item)
    msg = mock.Mock()
    msg.usage = mock.Mock(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_creation_input_tokens=0, cache_read_input_tokens=0)
    msg.content = [mock.Mock(type="text", text=json.dumps({"companies": companies}))]
    msg.stop_reason = "end_turn"
    result = mock.Mock()
    result.custom_id = custom_id
    result.result = mock.Mock(type="succeeded", message=msg)
    return result


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_submitting_marker("2026-11-12", te.MODEL_TOP100, {})
ts.clear_submitting_marker()
_custom_id_map = {
    "t100-0": {"entrants": {"NORM1": {"score_key": "D2026-11-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": False, "is_quality_on_sale": False},
    "t100-1": {"entrants": {"PRE1": {"score_key": "D2026-11-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": True, "is_quality_on_sale": False},
    "t100-2": {"entrants": {"QOS1": {"score_key": "D2026-11-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": False, "is_quality_on_sale": True},
}
ts.save_batch_state("msgbatch_qos_ingest_test", "2026-11-12", te.MODEL_TOP100, _custom_id_map)
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = mock.Mock(processing_status="ended")
    instance.messages.batches.results.return_value = [
        _mk_result("t100-0", ["NORM1"], input_tokens=1000, output_tokens=500),
        _mk_result("t100-1", ["PRE1"], input_tokens=2000, output_tokens=700),
        _mk_result("t100-2", ["QOS1"], input_tokens=3000, output_tokens=900),
    ]
    _ingest_logs = []
    _ingest_summary = te.poll_and_ingest_batch(log=_ingest_logs.append)

check("poll_and_ingest_batch() ingested all 3 results (1 normal + 1 prescore + "
      "1 quality-on-sale)", _ingest_summary is not None and _ingest_summary["scored"] == 3)
_expected_qos_cost = te.estimate_batch_cost_usd(3000, 900, 0, 0)
check("a '[quality_on_sale] scored 1, cost $X (0 failed)' line was logged with the "
      "REAL ingest-time cost for exactly the quality-on-sale-tagged result's own "
      "tokens (3000 in / 900 out) - never the normal or prescore result's",
      any(m == f"[quality_on_sale] scored 1, cost ${_expected_qos_cost:.2f} (0 failed)"
          for m in _ingest_logs))
check("the prescore ingest line still fires too, unaffected by quality-on-sale's "
      "own parallel accounting", any(m.startswith("[prescore] ingested:") and "1 scored, 0 failed" in m
                                      for m in _ingest_logs))
check("QOS1's score was actually saved to the store (the quality-on-sale tag doesn't "
      "stop the normal save_score() path)", ts.latest_score_for_ticker("QOS1", te.MODEL_TOP100) is not None)

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_submitting_marker("2026-11-12", te.MODEL_TOP100, {})
ts.clear_submitting_marker()
_custom_id_map_no_qos = {
    "t100-0": {"entrants": {"NORM2": {"score_key": "D2026-11-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": False, "is_quality_on_sale": False},
}
ts.save_batch_state("msgbatch_no_qos_test", "2026-11-12", te.MODEL_TOP100, _custom_id_map_no_qos)
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = mock.Mock(processing_status="ended")
    instance.messages.batches.results.return_value = [_mk_result("t100-0", ["NORM2"])]
    _ingest_logs_plain = []
    te.poll_and_ingest_batch(log=_ingest_logs_plain.append)
check("a batch carrying NO quality-on-sale-tagged pack logs no '[quality_on_sale]' "
      "line at all - byte-identical to before this feature existed",
      not any("[quality_on_sale]" in m for m in _ingest_logs_plain))


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
