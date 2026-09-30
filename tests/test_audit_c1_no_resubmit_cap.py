"""
Audit fixes, Commit 1 (owner-directed, 30 Sep 2026, per instruction_
tonight_sequence.md step 2 - overriding instruction_audit_fixes_25-30_
sep.md's own "wait for all four freshness-fix commits" header, since the
owner wants the spend guard in place before tonight's manual rescans).

Problem (17ae413): scheduler_engine._run_top100_poll resubmitted
whenever poll_and_ingest_batch() returned non-None and nothing was
pending - a batch ending 0 scored / N failed still returns a dict, so
the same failed tickers got resubmitted every hourly poll, forever.
Failed tickers were never recorded anywhere, so _unscored_tickers()
kept re-selecting them. Nothing capped spend.

This file covers:
  - a 0-scored batch is not resubmitted (poll_and_ingest_batch()'s own
    "scored"/"failed" summary + its "NOT resubmitting" log line;
    scheduler_engine._run_top100_poll's own scored>0 gate)
  - a partial failure excludes only the failed tickers, for 24h
  - 3 strikes -> permanent skip for this rubric + the shelf caption
  - a cleared failure (later success) does not linger
  - the daily submission cap refuses a batch once reached, with the
    exact log line
  - refresh_all()'s force=True bypasses the batch-count cap but not
    the entrant cap
  - day rollover resets the cap cleanly

Run: python3 tests/test_audit_c1_no_resubmit_cap.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_audit_c1_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts
import top100_render as tr

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _iso(dt):
    return dt.isoformat()


def _dims():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


def _make_succeeded_result(custom_id, ticker):
    import json as _json
    data = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
    data.update({
        "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "",
        "big_wave": "", "big_wave_comment": "",
    })
    text = _json.dumps(data)
    content_block = mock.Mock(type="text", text=text)
    usage = mock.Mock(input_tokens=1000, output_tokens=500)
    message = mock.Mock(content=[content_block], usage=usage)
    result = mock.Mock(type="succeeded", message=message)
    return mock.Mock(custom_id=custom_id, result=result)


def _make_errored_result(custom_id):
    inner_err = mock.Mock(type="invalid_request_error", message="bad request")
    err_wrapper = mock.Mock(type="error", error=inner_err)
    err_wrapper.model_dump_json = mock.Mock(side_effect=Exception("no pydantic"))
    result = mock.Mock(type="errored", error=err_wrapper)
    return mock.Mock(custom_id=custom_id, result=result)


def _seed_batch_state(entries):
    """entries: {custom_id: ticker} - writes a batch-state row directly,
    same shape submit_nightly_batch() would leave behind."""
    custom_id_map = {cid: {"ticker": t, "score_key": "D2026-09-30", "most_recent_quarter": None}
                      for cid, t in entries.items()}
    ts.save_batch_state("msgbatch_test", "2026-09-30", te.MODEL_TOP100, custom_id_map)


# ======================================================================
# CHECK 1: a batch that ends 0 scored / N failed is NOT resubmitted -
# poll_and_ingest_batch() itself logs the "NOT resubmitting" line, and
# scheduler_engine._run_top100_poll's own scored>0 gate does not fire.
# ======================================================================
_seed_batch_state({"c1": "AAAA", "c2": "BBBB"})
_batch = mock.Mock(processing_status="ended")
_results = [_make_errored_result("c1"), _make_errored_result("c2")]

_logs = []
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = _batch
    instance.messages.batches.results.return_value = iter(_results)
    result1 = te.poll_and_ingest_batch(log=_logs.append)

assert result1["scored"] == 0 and result1["failed"] == 2, result1
assert ts.get_batch_state() is None, "batch state must be cleared after ingest"
assert any("NOT resubmitting" in line for line in _logs), _logs
assert any("0 scored, 2 failed" in line for line in _logs), _logs
print("[c1_0_scored_not_resubmitted] poll_and_ingest_batch(): 0 scored/2 failed logs "
      "'NOT resubmitting' and clears batch state OK")

failures = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert failures["AAAA"]["attempts"] == 1 and failures["BBBB"]["attempts"] == 1, failures
print("[c1_failures_recorded] both failed tickers now have a top100_score_failures "
      "row with attempts=1 OK")

with mock.patch("top100_engine.poll_and_ingest_batch", return_value={"scored": 0, "failed": 2}) as m_poll, \
     mock.patch("top100_engine.submit_nightly_batch") as m_submit, \
     mock.patch("top100_engine.top100_store.get_batch_state", return_value=None):
    import scheduler_engine
    scheduler_engine._run_top100_poll(log=lambda *a, **k: None)
    m_submit.assert_not_called()
print("[c1_scheduler_gate] scheduler_engine._run_top100_poll does NOT call "
      "submit_nightly_batch() when scored == 0 OK")

with mock.patch("top100_engine.poll_and_ingest_batch", return_value={"scored": 5, "failed": 0}), \
     mock.patch("top100_engine.submit_nightly_batch") as m_submit2, \
     mock.patch("top100_engine.top100_store.get_batch_state", return_value=None):
    scheduler_engine._run_top100_poll(log=lambda *a, **k: None)
    m_submit2.assert_called_once()
print("[c1_scheduler_gate_positive] scheduler_engine._run_top100_poll DOES call "
      "submit_nightly_batch() when scored > 0 and nothing pending OK")


# ======================================================================
# CHECK 2: partial failure - only the failed ticker is excluded, for
# TOP100_FAILURE_RETRY_HOURS; a successful ticker's failure clears.
# ======================================================================
_seed_batch_state({"c1": "CCCC", "c2": "DDDD"})
_batch2 = mock.Mock(processing_status="ended")
_results2 = [_make_succeeded_result("c1", "CCCC"), _make_errored_result("c2")]
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = _batch2
    instance.messages.batches.results.return_value = iter(_results2)
    result2 = te.poll_and_ingest_batch(log=lambda *a, **k: None)

assert result2["scored"] == 1 and result2["failed"] == 1, result2
failures2 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert "CCCC" not in failures2, "a succeeded ticker must not carry a failure row"
assert failures2["DDDD"]["attempts"] == 1, failures2
print("[c1_partial_failure] partial batch: succeeded ticker has no failure row, "
      "failed ticker has attempts=1 OK")

pool = [{"ticker": "CCCC", "company_name": "CCCC Co", "most_recent_quarter": None},
        {"ticker": "DDDD", "company_name": "DDDD Co", "most_recent_quarter": None},
        {"ticker": "EEEE", "company_name": "EEEE Co", "most_recent_quarter": None}]
unscored = te._unscored_tickers(pool, te.MODEL_TOP100)
unscored_tickers = {row["ticker"] for row, _ in unscored}
assert "DDDD" not in unscored_tickers, "DDDD just failed - must be excluded within the retry window"
assert "CCCC" not in unscored_tickers, "CCCC just got a real score above - it is not unscored"
assert "EEEE" in unscored_tickers, "EEEE has no score and no failure - must need scoring"
print("[c1_retry_window_excludes] _unscored_tickers(): a ticker with a failure younger "
      "than TOP100_FAILURE_RETRY_HOURS is excluded; others are unaffected OK")

# Age the DDDD failure past the retry window -> eligible again.
with ts._conn() as conn:
    old = (datetime.now(timezone.utc) - timedelta(hours=te.TOP100_FAILURE_RETRY_HOURS + 1)).isoformat()
    conn.execute(
        "UPDATE top100_score_failures SET failed_at = ? WHERE ticker = ? AND model = ? AND rubric_version = ?",
        (old, "DDDD", te.MODEL_TOP100, te.RUBRIC_VERSION),
    )
unscored_after = te._unscored_tickers(pool, te.MODEL_TOP100)
unscored_tickers_after = {row["ticker"] for row, _ in unscored_after}
assert "DDDD" in unscored_tickers_after, "DDDD's failure is now > 24h old - must be eligible again"
print("[c1_retry_window_expires] once TOP100_FAILURE_RETRY_HOURS has passed, the "
      "ticker becomes eligible for resubmission again OK")


# ======================================================================
# CHECK 3: 3 strikes -> permanent skip for this rubric, regardless of
# how recent the failure is; surfaced on the AWAITING shelf with the
# "scoring failed" caption instead of the generic one.
# ======================================================================
for _ in range(3):
    ts.record_score_failure("FFFF", te.MODEL_TOP100, te.RUBRIC_VERSION, "invalid_request_error")
strikes = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert strikes["FFFF"]["attempts"] == 3, strikes["FFFF"]

pool3 = [{"ticker": "FFFF", "company_name": "FFFF Co", "most_recent_quarter": None}]
unscored3 = te._unscored_tickers(pool3, te.MODEL_TOP100)
assert unscored3 == [], "a 3-strike ticker must never be re-selected for this rubric"
print("[c1_three_strikes_skip] _unscored_tickers(): a ticker with attempts >= "
      "TOP100_FAILURE_MAX_ATTEMPTS is permanently skipped for this rubric OK")

row_ffff = {"ticker": "FFFF", "score_row": None, "score_failure": strikes["FFFF"],
            "mos_pct": None, "value_score": 50.0, "generated_at": None, "stale_valuation": 0}
shelf_html_en = tr._shelf_row_html(row_ffff, "en")
assert "scoring failed" in shelf_html_en, shelf_html_en
assert "scored automatically" not in shelf_html_en
print("[c1_shelf_caption] a 3-strike ticker's shelf row shows the 'scoring failed' "
      "caption, not the generic AWAITING one (EN) OK")
shelf_html_es = tr._shelf_row_html(row_ffff, "es")
assert "la puntuación falló" in shelf_html_es, shelf_html_es
print("[c1_shelf_caption_es] ...and in Spanish OK")

# A ticker with 0/1/2 attempts still shows the generic AWAITING caption.
row_normal = {"ticker": "GGGG", "score_row": None, "score_failure": None,
              "mos_pct": None, "value_score": 50.0, "generated_at": None, "stale_valuation": 0}
normal_html = tr._shelf_row_html(row_normal, "en")
assert "scored automatically" in normal_html
assert "scoring failed" not in normal_html
print("[c1_shelf_caption_normal] a ticker with no failure (or <3 attempts) still "
      "shows the generic AWAITING caption OK")


# ======================================================================
# CHECK 4: hard daily cap - refused once TOP100_MAX_SUBMISSIONS_PER_
# UTC_DAY batches or TOP100_MAX_ENTRANTS_PER_UTC_DAY entrants are hit,
# with the exact log line.
# ======================================================================
_today = datetime.now(timezone.utc).date().isoformat()
ts.record_daily_submission(_today, 100)
ts.record_daily_submission(_today, 100)  # 2 batches, 200 entrants so far

fake_pool = [{"ticker": f"CAP{i}", "company_name": f"Cap {i} Co", "most_recent_quarter": None}
             for i in range(10)]
cap_logs = []
result_capped = te.submit_nightly_batch(pool=fake_pool, log=cap_logs.append)
assert result_capped is None, "submission must be refused once the batch-count cap is hit"
assert any("daily submission cap reached" in line for line in cap_logs), cap_logs
assert any("2/2" in line and "batches" in line for line in cap_logs), cap_logs
print("[c1_batch_count_cap] submit_nightly_batch(): refused once 2/2 batches are "
      "already used today, with the exact log line OK")


# ======================================================================
# CHECK 5: force=True (refresh_all()'s own path) bypasses the BATCH-
# COUNT cap but NOT the entrant cap.
# ======================================================================
with mock.patch("anthropic.Anthropic") as MockClient, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw):
    instance = MockClient.return_value
    fake_batch = mock.Mock(id="msgbatch_forced")
    instance.messages.batches.create.return_value = fake_batch
    forced_logs = []
    # Still at 2/2 batches, 200/240 entrants from Check 4 above.
    result_forced = te.submit_nightly_batch(pool=fake_pool[:10], log=forced_logs.append, force=True)
assert result_forced == "msgbatch_forced", (result_forced, forced_logs)
assert not any("daily submission cap reached" in line for line in forced_logs), forced_logs
print("[c1_force_bypasses_batch_cap] force=True submits successfully even though the "
      "batch-count cap (2/2) is already reached OK")

daily_after_force = ts.get_daily_submission_state(_today)
assert daily_after_force == {"batches": 3, "entrants": 210}, daily_after_force

# Audit fixes Commit 2 (30 Sep 2026, owner-directed): the forced submit
# above left a real in-flight batch (top100_batch_state) behind, which
# submit_nightly_batch()'s OWN "batch still in progress" guard (added in
# Commit 2, tested in test_audit_c2_backoff_breaker_batchguard.py) would
# now refuse before ever reaching the entrant-cap check this section
# means to isolate - so clear it here, exactly as poll_and_ingest_batch()
# would once that batch's results actually came back.
ts.clear_batch_state()

# Now push entrants past the 240 cap even under force=True.
big_pool = [{"ticker": f"BIG{i}", "company_name": f"Big {i} Co", "most_recent_quarter": None}
            for i in range(40)]
entrant_cap_logs = []
result_entrant_capped = te.submit_nightly_batch(pool=big_pool, log=entrant_cap_logs.append, force=True)
assert result_entrant_capped is None, "the entrant cap must refuse even a forced submission"
assert any("daily submission cap reached" in line for line in entrant_cap_logs), entrant_cap_logs
print("[c1_entrant_cap_blocks_force] force=True is refused once the entrant cap "
      "(240) would be exceeded, even though the batch-count cap doesn't apply to it OK")


# ======================================================================
# CHECK 6: day rollover resets the cap cleanly - a new UTC date has no
# prior submissions at all.
# ======================================================================
tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
fresh_state = ts.get_daily_submission_state(tomorrow)
assert fresh_state == {"batches": 0, "entrants": 0}, fresh_state
print("[c1_day_rollover] a UTC date with no prior submissions reads {batches:0, "
      "entrants:0} - the cap resets automatically OK")


print("\nALL AUDIT FIXES COMMIT 1 TESTS PASSED")
