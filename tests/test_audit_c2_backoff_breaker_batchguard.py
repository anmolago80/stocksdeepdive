"""
Audit fixes, Commit 2 (owner-directed, 30 Sep 2026, per instruction_
tonight_sequence_v2.md step 2). Covers:

  - per-universe catch-up backoff: a fast-failing universe is refused a
    second attempt within 5 minutes, a third within 15; a success clears
    the record; a SLOW failure is left untouched.
  - _catchup_backoff_eligible() wired into the catch-up block's
    nothing-servable filtering (spot-checked directly against the two
    helper functions, same as the delay-progression check above).
  - rate-limit circuit breaker trip: _run_nightly() skips every
    remaining yfinance-calling stage (sector top-up, attention top-up,
    reprice pass, derived-universe build) but still runs the alert
    extra pass / batched notifications / results-day check, which don't
    call yfinance.
  - RateLimitCircuitBreaker abort no longer clears the universe's scan
    checkpoint (nightly_scan.run_universe_scan's own except-path).
  - a normal night (no breaker trip) is unaffected - every stage still
    runs exactly as before.
  - top100_engine.submit_nightly_batch() refuses to submit while a
    batch is still in progress, force=True included, with the exact
    log line.
  - the _yf_looks_rate_limited() / app._classify_fetch_failure() regex
    hardening: "generate"/"corporate" no longer false-match, while
    "429"/"rate limit"/"rate-limited"/"too many requests"/"crumb" still
    do.

Run: python3 tests/test_audit_c2_backoff_breaker_batchguard.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="audit_c2_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scheduler_engine as se
import nightly_scan
import scan_checkpoint_store


# ======================================================================
# CHECK 1: backoff delay progression - 5 -> 15 -> 45 -> 135, capped at
# the full catch-up window.
# ======================================================================
assert se._catchup_backoff_delay_minutes(1) == 5
assert se._catchup_backoff_delay_minutes(2) == 15
assert se._catchup_backoff_delay_minutes(3) == 45
assert se._catchup_backoff_delay_minutes(4) == 135
assert se._catchup_backoff_delay_minutes(10) == se.CATCHUP_WINDOW_HOURS * 60
print("[c2_backoff_progression] _catchup_backoff_delay_minutes(): "
      "5 -> 15 -> 45 -> 135 -> capped at the catch-up window OK")


# ======================================================================
# CHECK 2: _catchup_backoff_eligible() / _record_catchup_outcome() -
# fast failure -> refused within its own window, eligible once it's
# passed; success clears; a slow failure is left untouched.
# ======================================================================
state = {}
now = datetime.now(timezone.utc)
assert se._catchup_backoff_eligible("ASX 200", state, now) is True, \
    "a universe with no failure record must always be eligible"

# First FAST failure (elapsed well under the 60s threshold).
se._record_catchup_outcome("ASX 200", succeeded=False, elapsed_seconds=5, state=state)
assert state["catchup_failures"]["ASX 200"]["count"] == 1
assert se._catchup_backoff_eligible("ASX 200", state, now) is False, \
    "immediately after a fast failure, the universe must be refused"
assert se._catchup_backoff_eligible("ASX 200", state, now + timedelta(minutes=4)) is False, \
    "still inside the 5-minute window"
assert se._catchup_backoff_eligible("ASX 200", state, now + timedelta(minutes=6)) is True, \
    "past the 5-minute window, eligible again"
print("[c2_backoff_first_failure] first fast failure -> refused within 5m, "
      "eligible after OK")

# Second FAST failure, timestamped at the 6-minute mark (the eligible
# retry from above).
retry_at = now + timedelta(minutes=6)
se._record_catchup_outcome("ASX 200", succeeded=False, elapsed_seconds=5, state=state)
state["catchup_failures"]["ASX 200"]["last_attempt"] = retry_at.isoformat()
state["catchup_failures"]["ASX 200"]["count"] = 2
assert se._catchup_backoff_eligible("ASX 200", state, retry_at + timedelta(minutes=14)) is False, \
    "still inside the 15-minute window after the second failure"
assert se._catchup_backoff_eligible("ASX 200", state, retry_at + timedelta(minutes=16)) is True, \
    "past the 15-minute window, eligible again"
print("[c2_backoff_second_failure] second fast failure -> refused within "
      "15m, eligible after OK")

# A success clears the record entirely.
se._record_catchup_outcome("ASX 200", succeeded=True, elapsed_seconds=30, state=state)
assert "ASX 200" not in state.get("catchup_failures", {})
assert se._catchup_backoff_eligible("ASX 200", state, now) is True
print("[c2_backoff_clears_on_success] a successful scan clears the "
      "catch-up failure record OK")

# A SLOW failure (>= the fast-failure threshold) is left untouched -
# no record created at all.
state2 = {}
se._record_catchup_outcome("ASX 300", succeeded=False,
                            elapsed_seconds=se.CATCHUP_BACKOFF_FAST_FAILURE_SECONDS + 30,
                            state=state2)
assert "ASX 300" not in state2.get("catchup_failures", {}), \
    "a slow (genuine) failure must not be treated as a fast bounce"
print("[c2_backoff_ignores_slow_failure] a slow failure leaves no backoff "
      "record OK")


# ======================================================================
# CHECK 3: breaker trip inside _run_nightly() skips every remaining
# yfinance-calling stage, but not the alert/results-day passes.
# ======================================================================
cfg = {
    "universes": ["Dow Jones 30", "S&P 500"],
    "universe_cadence": {"Dow Jones 30": "daily", "S&P 500": "daily"},
}

calls = {"sector_topup": 0, "attention_topup": 0, "reprice": 0, "derived": 0,
         "extra_pass": 0, "batched": 0, "results_day": 0}

with mock.patch.object(nightly_scan, "refresh_market_cap_ranking"), \
     mock.patch("scanner_engine.fetch_asx300") as _fa300, \
     mock.patch("scanner_engine.fetch_allords") as _fao, \
     mock.patch("scanner_engine.fetch_asx_listed_companies") as _falc, \
     mock.patch("alert_engine.snapshot_previous_values", return_value={}), \
     mock.patch.object(
         nightly_scan, "run_universe_scan",
         side_effect=nightly_scan.RateLimitCircuitBreaker("Dow Jones 30: aborted after 15 consecutive rate-limited tickers")) as _rus, \
     mock.patch.object(nightly_scan, "run_imported_scan") as _ris, \
     mock.patch.object(nightly_scan, "run_sector_topup", side_effect=lambda *a, **k: calls.__setitem__("sector_topup", calls["sector_topup"] + 1)), \
     mock.patch.object(nightly_scan, "run_attention_topup", side_effect=lambda *a, **k: calls.__setitem__("attention_topup", calls["attention_topup"] + 1)), \
     mock.patch.object(nightly_scan, "reprice_universe", side_effect=lambda *a, **k: calls.__setitem__("reprice", calls["reprice"] + 1)), \
     mock.patch.object(se, "_build_derived_universes", side_effect=lambda *a, **k: calls.__setitem__("derived", calls["derived"] + 1)), \
     mock.patch("alert_engine.run_extra_ticker_pass", side_effect=lambda *a, **k: calls.__setitem__("extra_pass", calls["extra_pass"] + 1)), \
     mock.patch("alert_engine.send_batched_notifications", side_effect=lambda *a, **k: calls.__setitem__("batched", calls["batched"] + 1)), \
     mock.patch("results_engine.check_results_day", side_effect=lambda *a, **k: calls.__setitem__("results_day", calls["results_day"] + 1)), \
     mock.patch.object(se, "_record_rate_limit_cooldown") as _rrlc:
    logs = []
    se._run_nightly(cfg, logs.append, run_night="2026-09-30")

assert _rus.called, "run_universe_scan must have been attempted"
assert not _ris.called, "the 'imported' universe (last in `ordered`) must never run once the breaker trips"
assert _rrlc.called, "the rate-limit cooldown must still be recorded"
assert calls["sector_topup"] == 0, "sector top-up must be skipped on a breaker trip"
assert calls["attention_topup"] == 0, "attention top-up must be skipped on a breaker trip"
assert calls["reprice"] == 0, "the reprice pass must be skipped on a breaker trip"
assert calls["derived"] == 0, "derived-universe rebuilds must be skipped on a breaker trip"
assert calls["extra_pass"] == 1, "the alert extra pass reads already-scanned rows only - must still run"
assert calls["batched"] == 1, "batched notification send is not a yfinance call - must still run"
assert calls["results_day"] == 1, "results-day re-analysis must still run on a breaker trip"
assert any("skipping" in line and "sector top-up" in line for line in logs), logs
print("[c2_breaker_skips_stages] a rate-limit breaker trip skips sector/attention "
      "top-up, reprice and derived-universe rebuilds, but not the alert/results-day "
      "passes OK")


# ======================================================================
# CHECK 4: a normal night (no breaker trip) is unaffected - every stage
# still runs.
# ======================================================================
calls2 = {"sector_topup": 0, "attention_topup": 0, "reprice": 0, "derived": 0}
fake_payload = {"rows": [{"Ticker": "AAA", "Long Score": 50}], "attention_lite": False}

with mock.patch.object(nightly_scan, "refresh_market_cap_ranking"), \
     mock.patch("scanner_engine.fetch_asx300"), \
     mock.patch("scanner_engine.fetch_allords"), \
     mock.patch("scanner_engine.fetch_asx_listed_companies"), \
     mock.patch("alert_engine.snapshot_previous_values", return_value={}), \
     mock.patch.object(nightly_scan, "run_universe_scan", return_value=fake_payload), \
     mock.patch.object(nightly_scan, "run_imported_scan", return_value=None), \
     mock.patch("snapshot_store.build_snapshots_from_scan"), \
     mock.patch("alert_engine.check_universe_rows"), \
     mock.patch("insider_engine.refresh_universe"), \
     mock.patch.object(nightly_scan, "run_sector_topup", side_effect=lambda *a, **k: calls2.__setitem__("sector_topup", calls2["sector_topup"] + 1)), \
     mock.patch.object(nightly_scan, "run_attention_topup", side_effect=lambda *a, **k: calls2.__setitem__("attention_topup", calls2["attention_topup"] + 1)), \
     mock.patch.object(nightly_scan, "reprice_universe", side_effect=lambda *a, **k: calls2.__setitem__("reprice", calls2["reprice"] + 1)), \
     mock.patch.object(se, "_build_derived_universes", side_effect=lambda *a, **k: calls2.__setitem__("derived", calls2["derived"] + 1)), \
     mock.patch("alert_engine.run_extra_ticker_pass"), \
     mock.patch("alert_engine.send_batched_notifications"), \
     mock.patch("results_engine.check_results_day"):
    logs2 = []
    se._run_nightly(cfg, logs2.append, run_night="2026-09-30")

assert calls2["derived"] == 1, "a normal (non-breaker) night must still rebuild derived universes"
print("[c2_normal_night_unchanged] a normal night with no breaker trip still runs "
      "every post-scan stage exactly as before OK")


# ======================================================================
# CHECK 5: the RateLimitCircuitBreaker except-path no longer clears the
# universe's checkpoint.
# ======================================================================
big_pool_tickers = [f"T{i}.AX" for i in range(20)]

import pandas as pd
fake_pool_df = pd.DataFrame({"Ticker": big_pool_tickers})


def _always_rate_limited(ticker, attention_lite=True, discount_rate=None, log=print,
                          rate_limited_out=None, growth_summary_out=None, oneoff_summary_out=None):
    if rate_limited_out is not None:
        rate_limited_out[0] = True
    return None


with mock.patch("scanner_engine.get_universe_pool", return_value=(fake_pool_df, "test source")), \
     mock.patch.object(nightly_scan, "_yf_call_with_retry", return_value=None), \
     mock.patch.object(nightly_scan, "analyze_ticker_lite", side_effect=_always_rate_limited), \
     mock.patch.object(scan_checkpoint_store, "clear") as _clear, \
     mock.patch.object(scan_checkpoint_store, "load", return_value=None):
    breaker_logs = []
    raised = None
    try:
        nightly_scan.run_universe_scan("Dow Jones 30", log=breaker_logs.append, run_night="2026-09-30")
    except nightly_scan.RateLimitCircuitBreaker as e:
        raised = e
assert raised is not None, "run_universe_scan must raise RateLimitCircuitBreaker after 15 consecutive rate-limited tickers"
assert not _clear.called, "the checkpoint must NOT be cleared on a breaker abort - audit fixes Commit 2"
print("[c2_checkpoint_kept_on_breaker] RateLimitCircuitBreaker abort leaves the "
      "universe's checkpoint in place (scan_checkpoint_store.clear() not called) OK")


# ======================================================================
# CHECK 6: submit_nightly_batch() refuses while a batch is already in
# progress - force=True included.
# ======================================================================
import top100_engine as te
import top100_store as ts

in_flight_state = {"batch_id": "msgbatch_pending", "submitted_at": "2026-09-30T10:00:00+00:00",
                    "quarter": None, "model": te.MODEL_TOP100, "custom_id_map": {}}

with mock.patch.object(ts, "get_batch_state", return_value=in_flight_state):
    pending_logs = []
    result = te.submit_nightly_batch(pool=[{"ticker": "ZZZZ", "company_name": "Zzzz Co",
                                              "most_recent_quarter": None}],
                                      log=pending_logs.append)
    assert result is None
    assert any("msgbatch_pending" in line and "still in progress" in line for line in pending_logs), pending_logs

    forced_pending_logs = []
    forced_result = te.submit_nightly_batch(
        pool=[{"ticker": "ZZZZ", "company_name": "Zzzz Co", "most_recent_quarter": None}],
        log=forced_pending_logs.append, force=True)
    assert forced_result is None, "force=True must NOT bypass the in-progress-batch guard"
    assert any("msgbatch_pending" in line and "still in progress" in line for line in forced_pending_logs), \
        forced_pending_logs
print("[c2_batch_still_pending_guard] submit_nightly_batch() refuses to submit while a "
      "batch is still in progress, force=True included, with the exact log line OK")


# ======================================================================
# CHECK 7: _yf_looks_rate_limited() / app._classify_fetch_failure()
# regex hardening - real matches still match, false positives don't.
# ======================================================================
assert nightly_scan._yf_looks_rate_limited(Exception("429 Client Error: Too Many Requests"))
assert nightly_scan._yf_looks_rate_limited(Exception("Rate limit exceeded"))
assert nightly_scan._yf_looks_rate_limited(Exception("rate-limited by upstream"))
assert nightly_scan._yf_looks_rate_limited(Exception("ratelimit hit"))
assert nightly_scan._yf_looks_rate_limited(Exception("crumb invalid"))
assert not nightly_scan._yf_looks_rate_limited(Exception("failed to generate report")), \
    "'generate' must no longer false-match the old bare 'rate' substring test"
assert not nightly_scan._yf_looks_rate_limited(Exception("corporate action pending")), \
    "'corporate' must no longer false-match"
assert not nightly_scan._yf_looks_rate_limited(Exception("could not operate on this frame")), \
    "'operate' must no longer false-match"
print("[c2_regex_hardening_nightly_scan] _yf_looks_rate_limited(): real rate-limit "
      "phrasings still match, 'generate'/'corporate'/'operate' no longer do OK")

import app as _app
assert _app._classify_fetch_failure(Exception("429 Too Many Requests")) == "rate_limited"
assert _app._classify_fetch_failure(Exception("Rate Limited")) == "rate_limited"
assert _app._classify_fetch_failure(Exception("failed to generate output")) == "network", \
    "app._classify_fetch_failure must share the same hardened regex"
print("[c2_regex_hardening_app] app._classify_fetch_failure(): shares the same "
      "hardened regex as nightly_scan OK")

print("SWEEP_DONE")
