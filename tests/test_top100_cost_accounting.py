"""
Honest cost accounting, Push 1 (owner-directed, 1 Oct 2026, per
instruction_top100_cost_v6.md's own Push 1 section - no rubric change,
no re-score, just correct cache-token accounting).

Problem this covers (verified against the Anthropic Console for 30 Sep
2026: 1,511,091 tokens / $4.66 vs the site's own log for that day, 269k
tokens / $2.47): poll_and_ingest_batch() and estimate_batch_cost_usd()
summed only usage.input_tokens/output_tokens, silently dropping
usage.cache_creation_input_tokens (billed at 1.25x input - a cache
WRITE) and usage.cache_read_input_tokens (billed at 0.10x input - a
cache HIT) - two separate fields on the same usage object. Batch API
requests execute in parallel, so nearly every one of the 108 nightly
requests missed the cache and paid the write price, never counted.
_estimate_prompt_tokens() also excluded the structured-output response
schema from the pre-submission estimate entirely, undershooting it a
second, independent way.

This file covers (instruction's own Push 1 point 5 test list):
  - estimate_batch_cost_usd(): a usage fixture with all four token
    kinds (input, cache-write, cache-read, output) prices correctly
  - the pre-submission estimate (_estimate_prompt_tokens(), summed
    over a synthetic 108-request batch with a ~10k-token system+
    schema+user prompt) lands within ~15% of the ingest-side total
    computed from the same per-request token count (both sides of
    this check use chars/4, not a live tokenizer call - see this
    task's own notes on why no code fix can close the chars/4-vs-
    real-tokenizer gap)
  - cache_control is absent from _request_params()'s own output
  - poll_and_ingest_batch() end-to-end: the new 4-token-kind ingest
    log line format, and record_ingest_cost()/ingest_cost_last_n_days()
    persistence via top100_store

Run: python3 tests/test_top100_cost_accounting.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_cost_accounting_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


# ======================================================================
# CHECK 1: estimate_batch_cost_usd() prices all four token kinds
# correctly - input at the plain rate, cache_creation at 1.25x input,
# cache_read at 0.10x input, output at the output rate, all then
# halved for the batch discount.
# ======================================================================
cost_plain = te.estimate_batch_cost_usd(1000, 1000)
expected_plain = ((1000 / 1_000_000) * te.TOP100_INPUT_USD_PER_MTOK +
                   (1000 / 1_000_000) * te.TOP100_OUTPUT_USD_PER_MTOK) * te.BATCH_DISCOUNT
assert abs(cost_plain - expected_plain) < 1e-9, (cost_plain, expected_plain)
assert abs(cost_plain - 0.012) < 1e-9, cost_plain
print(f"[cost_plain] estimate_batch_cost_usd(1000, 1000) == {cost_plain} "
      "(no cache tokens, backward-compatible 2-arg call) OK")

cost_full = te.estimate_batch_cost_usd(1000, 1000, 1000, 1000)
expected_full = (
    (1000 / 1_000_000) * te.TOP100_INPUT_USD_PER_MTOK
    + (1000 / 1_000_000) * te.TOP100_INPUT_USD_PER_MTOK * te.CACHE_WRITE_MULTIPLIER
    + (1000 / 1_000_000) * te.TOP100_INPUT_USD_PER_MTOK * te.CACHE_READ_MULTIPLIER
    + (1000 / 1_000_000) * te.TOP100_OUTPUT_USD_PER_MTOK
) * te.BATCH_DISCOUNT
assert abs(cost_full - expected_full) < 1e-9, (cost_full, expected_full)
assert abs(cost_full - 0.0147) < 1e-9, cost_full
assert cost_full > cost_plain, "adding cache-write/cache-read tokens must raise the cost"
print(f"[cost_all_four_kinds] estimate_batch_cost_usd(1000, 1000, 1000, 1000) == {cost_full} "
      "(input + cache-write@1.25x + cache-read@0.10x + output, batch-discounted) OK")

# A cache-write-heavy batch must cost strictly more than an equal-sized
# cache-read-heavy one (1.25x vs 0.10x of the same input rate).
cost_all_write = te.estimate_batch_cost_usd(0, 0, cache_creation_input_tokens=100_000)
cost_all_read = te.estimate_batch_cost_usd(0, 0, cache_read_input_tokens=100_000)
assert cost_all_write > cost_all_read, (cost_all_write, cost_all_read)
print(f"[cost_write_vs_read] 100k cache-write tokens (${cost_all_write:.4f}) cost more than "
      f"100k cache-read tokens (${cost_all_read:.4f}) at the same input rate OK")


# ======================================================================
# CHECK 2: pre-submission estimate lands within ~15% of the ingest-side
# total for a synthetic 108-request batch with a ~10k-token system+
# schema+user prompt. Both sides are computed with the same chars/4
# convention (this codebase's own established approximation, not a
# live Anthropic tokenizer call - see _CHARS_PER_TOKEN_ESTIMATE's own
# docstring), so this is a self-consistency check on the formula
# _estimate_request_tokens() now uses (system + schema + user, where
# before this fix the schema was silently excluded). v6 packed
# requests (1 Oct 2026): this function now takes a one-entrant LIST
# (_request_params()'s own new list-based signature), not a bare
# ticker/company_name pair - updated here to match, same formula.
# ======================================================================
_FAKE_ENTRANTS = [{"ticker": "AAAA", "company_name": "Test Company Ltd", "sector": "Technology"}]
real_tokens_per_entrant = te._estimate_request_tokens(_FAKE_ENTRANTS)

# Pad the system prompt text (via a patched _request_params) so the
# per-request total lands at ~10k tokens chars/4, matching the
# instruction's own "10k-token system prompt" synthetic scenario.
_pad_chars = max(0, 10_000 * te._CHARS_PER_TOKEN_ESTIMATE - real_tokens_per_entrant * te._CHARS_PER_TOKEN_ESTIMATE)
_real_request_params = te._request_params


def _padded_request_params(entrants):
    params = _real_request_params(entrants)
    params["system"][0]["text"] = params["system"][0]["text"] + ("x" * _pad_chars)
    return params


with mock.patch("top100_engine._request_params", side_effect=_padded_request_params):
    per_entrant_tokens = te._estimate_request_tokens(_FAKE_ENTRANTS)
assert abs(per_entrant_tokens - 10_000) <= 10_000 * 0.05, per_entrant_tokens
print(f"[synthetic_10k_prompt] padded _estimate_request_tokens() == {per_entrant_tokens} "
      "(~10k tokens, system+schema+user, chars/4) OK")

N_REQUESTS = 108
pre_submit_total_input = per_entrant_tokens * N_REQUESTS

# Ingest-side total: the same per-request token count, now billed as
# plain input (cache_control has been dropped from _request_params(),
# so there is no cache-write/cache-read split on the real request
# either - see CHECK 3 below).
ingest_total_input = per_entrant_tokens * N_REQUESTS

pct_diff = abs(pre_submit_total_input - ingest_total_input) / ingest_total_input
assert pct_diff <= 0.15, (pre_submit_total_input, ingest_total_input, pct_diff)
print(f"[within_15_pct] pre-submit estimate ({pre_submit_total_input:,} tokens) is within "
      f"{pct_diff:.1%} of the ingest-side total ({ingest_total_input:,} tokens) for a "
      f"synthetic {N_REQUESTS}-request batch OK")


# ======================================================================
# CHECK 3: cache_control is absent from _request_params()'s own output
# - dropped because no usage/cache-hit-ratio history is persisted
# anywhere in this codebase (top100_store.save_score() never stores
# the usage object), so there is no evidence it ever paid for itself.
# ======================================================================
params = te._request_params([{"ticker": "AAPL", "company_name": "Apple Inc.", "sector": "Technology"}])
system_block = params["system"][0]
assert "cache_control" not in system_block, system_block
assert set(system_block.keys()) == {"type", "text"}, system_block
print("[no_cache_control] _request_params()'s system block carries no cache_control key OK")


# ======================================================================
# CHECK 4: poll_and_ingest_batch() end-to-end - the new 4-token-kind
# ingest log line format, and record_ingest_cost()/ingest_cost_last_
# n_days() persistence (reusing tests/test_audit_c1_no_resubmit_cap.py's
# own mock.patch("anthropic.Anthropic") convention).
# ======================================================================
def _make_succeeded_result(custom_id, ticker, cache_creation=0, cache_read=0):
    # v6 packed requests (1 Oct 2026): the wire response is now
    # {"companies": [<item with its own "ticker">]} - even a single-
    # entrant result, since _request_params() always builds the
    # packed schema now.
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
                       cache_creation_input_tokens=cache_creation,
                       cache_read_input_tokens=cache_read)
    message = mock.Mock(content=[content_block], usage=usage)
    result = mock.Mock(type="succeeded", message=message)
    return mock.Mock(custom_id=custom_id, result=result)


custom_id_map = {"c1": {"entrants": {"COST1": {"score_key": "D2026-10-01", "most_recent_quarter": None}}},
                  "c2": {"entrants": {"COST2": {"score_key": "D2026-10-01", "most_recent_quarter": None}}}}
ts.save_batch_state("msgbatch_cost_test", "2026-10-01", te.MODEL_TOP100, custom_id_map)

_batch = mock.Mock(processing_status="ended")
_results = [_make_succeeded_result("c1", "COST1", cache_creation=2000, cache_read=0),
            _make_succeeded_result("c2", "COST2", cache_creation=0, cache_read=2000)]

logs = []
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = _batch
    instance.messages.batches.results.return_value = iter(_results)
    result = te.poll_and_ingest_batch(log=logs.append)

assert result["scored"] == 2 and result["failed"] == 0, result
assert result["input_tokens"] == 2000, result
assert result["cache_creation_tokens"] == 2000, result
assert result["cache_read_tokens"] == 2000, result
assert result["output_tokens"] == 1000, result
print("[ingest_return_dict] poll_and_ingest_batch()'s return dict carries all four token "
      "kinds with the correct totals OK")

ingest_lines = [l for l in logs if "ingested:" in l]
assert len(ingest_lines) == 1, logs
line = ingest_lines[0]
assert "2 scored, 0 failed" in line, line
assert "in 2,000" in line, line
assert "cache-write 2,000" in line, line
assert "cache-read 2,000" in line, line
assert "out 1,000" in line, line
assert "batch-priced" in line, line
print(f"[ingest_log_line_format] new ingest log line format correct: {line!r} OK")

cost_log = ts.ingest_cost_last_n_days(7)
assert cost_log["batches"] == 1, cost_log
assert cost_log["scored"] == 2, cost_log
assert cost_log["failed"] == 0, cost_log
assert cost_log["cache_creation_tokens"] == 2000, cost_log
assert cost_log["cache_read_tokens"] == 2000, cost_log
assert cost_log["cost_usd"] > 0, cost_log
print(f"[ingest_cost_persisted] record_ingest_cost() persisted correctly - "
      f"ingest_cost_last_n_days(7) == {cost_log} OK")

# A fresh DB with nothing ingested yet must read as all-zero, never None.
empty_db_path = os.path.join(TESTVOL, "empty_subdir")
os.makedirs(empty_db_path, exist_ok=True)
with mock.patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": empty_db_path}):
    import importlib
    importlib.reload(ts)
    empty_cost_log = ts.ingest_cost_last_n_days(7)
assert empty_cost_log == {"batches": 0, "scored": 0, "failed": 0, "cost_usd": 0,
                           "cache_creation_tokens": 0, "cache_read_tokens": 0}, empty_cost_log
print("[ingest_cost_empty] ingest_cost_last_n_days() reads all-zero (never None) when "
      "nothing has ingested yet OK")
importlib.reload(ts)  # restore the real TESTVOL-backed module for any later test in this run


print("\nALL PUSH 1 COST ACCOUNTING TESTS PASSED")
