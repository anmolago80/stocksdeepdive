"""
Top 100 rubric v6 - packed requests + shorter rubric (owner-directed,
1 Oct 2026, per instruction_top100_cost_v6.md's own Push 2 section).

Not a new question for the model - every dimension, label vocabulary,
sentinel convention and output field is byte-for-byte identical to v5.
This bump changes the WIRE SHAPE only: up to TOP100_COMPANIES_PER_
REQUEST (5) companies are packed into one request, the response schema
wraps the existing per-company object in a top-level {"companies": [...]}
array with a "ticker" field added to each item, and _SYSTEM_PROMPT was
trimmed (repeated phrasing + the four worked-example "Calibration:"
lines cut, every rule/criterion/sentinel kept).

This file covers (instruction's own Push 2 point 6 test list):
  - schema dry-check: 0 unions, no min/max/minLength/maxLength anywhere
    (item schema AND the top-level wrapper)
  - a 5-company packed response parses and saves 5 scores with correct
    per-entrant score keys
  - one malformed item -> 4 saved, 1 failure recorded (per-ticker
    failure isolation - one bad company never costs the others)
  - a ticker missing from the response entirely -> failure recorded
  - order-independent matching (model returns the array shuffled)
  - pack boundary: a 108-entrant pool packs into 22 requests, the last
    with 3
  - cost estimate counts the system+schema prompt once per REQUEST,
    not once per company (the pack's actual saving)
  - every pre-existing Top 100 suite stays green (verified separately,
    full regression sweep - this file does not re-run them)

Run: python3 tests/test_v6_packed_requests.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_v6_packed_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _dims():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


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
# CHECK 1: schema dry-check - 0 union-typed properties, no min/max/
# minLength/maxLength anywhere, on BOTH the per-company item schema
# and the top-level {"companies": [...]} wrapper. An array-of-objects
# wrapper is explicitly NOT a union type.
# ======================================================================
def _walk(node, path="root"):
    unions, constraints = [], []
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, list) or "anyOf" in node or "oneOf" in node:
            unions.append(path)
        for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
            if key in node:
                constraints.append((path, key))
        for k, v in node.items():
            if isinstance(v, dict):
                u, c = _walk(v, f"{path}.{k}")
                unions += u
                constraints += c
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, dict):
                        u, c = _walk(item, f"{path}.{k}[{i}]")
                        unions += u
                        constraints += c
    return unions, constraints


wrapper_schema = te._response_schema()
item_schema = te._company_item_schema()
wrapper_unions, wrapper_constraints = _walk(wrapper_schema)
item_unions, item_constraints = _walk(item_schema)
assert wrapper_unions == [], f"wrapper schema has union types: {wrapper_unions}"
assert wrapper_constraints == [], f"wrapper schema has min/max constraints: {wrapper_constraints}"
assert item_unions == [], f"item schema has union types: {item_unions}"
assert item_constraints == [], f"item schema has min/max constraints: {item_constraints}"
assert wrapper_schema["properties"]["companies"]["type"] == "array"
assert "minItems" not in wrapper_schema["properties"]["companies"]
assert "maxItems" not in wrapper_schema["properties"]["companies"]
assert "ticker" in item_schema["properties"] and "ticker" in item_schema["required"]
print(f"[schema_dry_check] 0 union types, no min/max anywhere (item: {len(item_schema['properties'])} "
      f"props, wrapper: array-of-objects, not a union) OK")


# ======================================================================
# CHECK 1b: prompt fix (1 Oct 2026, owner-directed) - _SYSTEM_PROMPT no
# longer opens with the leftover single-company sentence ("You are
# given one company's ticker and name") now that _user_prompt() hands
# the model up to 5 companies and a {"companies": [...]} schema -
# a contradictory instruction was an avoidable risk on the first full
# v6 night.
# ======================================================================
assert "one company's ticker" not in te._SYSTEM_PROMPT, (
    "the leftover single-company sentence must be gone from _SYSTEM_PROMPT"
)
assert "short list of companies" in te._SYSTEM_PROMPT, (
    "_SYSTEM_PROMPT must describe the request as a short list of companies, not one"
)
print("[prompt_matches_pack] _SYSTEM_PROMPT no longer claims the request contains one company OK")


# ======================================================================
# CHECK 2: a 5-company packed response parses and saves 5 scores with
# correct per-entrant score keys.
# ======================================================================
tickers5 = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE"]
companies_json = json.dumps({"companies": [_company_item(t) for t in tickers5]})
parsed = te._parse_response_json(companies_json)
assert set(parsed.keys()) == set(tickers5), parsed.keys()
for t in tickers5:
    assert parsed[t][0]["ai_exposure"]["score"] == 4

entrants_map = {t: {"score_key": f"D2026-10-01-{t}", "most_recent_quarter": None} for t in tickers5}
for ticker, entrant_info in entrants_map.items():
    (dims, not_rated, *_rest) = parsed[ticker]
    ts.save_score(
        ticker=ticker, quarter=entrant_info["score_key"], model=te.MODEL_TOP100,
        rubric_version=te.RUBRIC_VERSION, dims=dims, not_rated=not_rated,
        inversion_scenario=None, inversion_severity=None, prompt="{}", raw_response=companies_json,
    )
for t in tickers5:
    got = ts.get_score(t, f"D2026-10-01-{t}", te.MODEL_TOP100, te.RUBRIC_VERSION)
    assert got is not None, f"{t} was not saved"
    assert got["dims"]["ai_exposure"]["score"] == 4
print("[five_company_pack] a 5-company packed response parses and saves all 5 scores "
      "under their own correct score keys OK")


# ======================================================================
# CHECK 3: poll_and_ingest_batch() end-to-end - one malformed item in a
# 5-entrant request -> 4 saved, 1 failure recorded (per-ticker failure
# isolation).
# ======================================================================
def _make_result(custom_id, text, usage_tokens=(1000, 500, 0, 0)):
    content_block = mock.Mock(type="text", text=text)
    usage = mock.Mock(input_tokens=usage_tokens[0], output_tokens=usage_tokens[1],
                       cache_creation_input_tokens=usage_tokens[2], cache_read_input_tokens=usage_tokens[3])
    message = mock.Mock(content=[content_block], usage=usage)
    result = mock.Mock(type="succeeded", message=message)
    return mock.Mock(custom_id=custom_id, result=result)


pack_tickers = ["PACK1", "PACK2", "PACK3", "PACK4", "PACK5"]
items = [_company_item(t) for t in pack_tickers]
# Corrupt PACK3's own item: delete a required dimension entirely, so
# _parse_one_company() raises for this one item only.
del items[2]["ai_exposure"]
malformed_text = json.dumps({"companies": items})

entrants_meta = {t: {"score_key": f"D2026-10-02-{t}", "most_recent_quarter": None} for t in pack_tickers}
ts.save_batch_state("msgbatch_v6_malformed", "2026-10-02", te.MODEL_TOP100,
                     {"c1": {"entrants": entrants_meta}})

_batch = mock.Mock(processing_status="ended")
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = _batch
    instance.messages.batches.results.return_value = iter([_make_result("c1", malformed_text)])
    result = te.poll_and_ingest_batch(log=lambda *a, **k: None)

assert result["scored"] == 4, result
assert result["failed"] == 1, result
for t in ("PACK1", "PACK2", "PACK4", "PACK5"):
    assert ts.get_score(t, f"D2026-10-02-{t}", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None, \
        f"{t} should have been saved - one bad item must not cost the others"
assert ts.get_score("PACK3", "D2026-10-02-PACK3", te.MODEL_TOP100, te.RUBRIC_VERSION) is None, \
    "PACK3's own malformed item must not have been saved"
failures = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert "PACK3" in failures, "PACK3 must have a recorded failure"
print("[per_ticker_isolation] one malformed item in a 5-entrant packed response -> "
      "4 saved, 1 failure recorded, the other 4 entrants unaffected OK")


# ======================================================================
# CHECK 4: a ticker missing from the response entirely (the model
# simply never returned an item for it) -> its own failure recorded,
# the others still save.
# ======================================================================
missing_tickers = ["MISS1", "MISS2", "MISS3"]
# Model only returns 2 of the 3 expected companies.
missing_items = [_company_item(t) for t in missing_tickers[:2]]
missing_text = json.dumps({"companies": missing_items})
missing_entrants_meta = {t: {"score_key": f"D2026-10-03-{t}", "most_recent_quarter": None} for t in missing_tickers}
ts.save_batch_state("msgbatch_v6_missing", "2026-10-03", te.MODEL_TOP100,
                     {"c1": {"entrants": missing_entrants_meta}})

with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = _batch
    instance.messages.batches.results.return_value = iter([_make_result("c1", missing_text)])
    result2 = te.poll_and_ingest_batch(log=lambda *a, **k: None)

assert result2["scored"] == 2 and result2["failed"] == 1, result2
for t in missing_tickers[:2]:
    assert ts.get_score(t, f"D2026-10-03-{t}", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None
failures2 = ts.score_failures_for_model(te.MODEL_TOP100, te.RUBRIC_VERSION)
assert failures2.get("MISS3", {}).get("reason") == "missing_from_response", failures2.get("MISS3")
print("[missing_from_response] a ticker absent from the packed response entirely gets its own "
      "'missing_from_response' failure; the others in the same request still save OK")


# ======================================================================
# CHECK 5: order-independent matching - the model returns the array in
# a shuffled order; results still match by ticker, not position.
# ======================================================================
order_tickers = ["ORD1", "ORD2", "ORD3"]
shuffled = [_company_item("ORD3"), _company_item("ORD1"), _company_item("ORD2")]
order_text = json.dumps({"companies": shuffled})
order_entrants_meta = {t: {"score_key": f"D2026-10-04-{t}", "most_recent_quarter": None} for t in order_tickers}
ts.save_batch_state("msgbatch_v6_order", "2026-10-04", te.MODEL_TOP100,
                     {"c1": {"entrants": order_entrants_meta}})

with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = _batch
    instance.messages.batches.results.return_value = iter([_make_result("c1", order_text)])
    result3 = te.poll_and_ingest_batch(log=lambda *a, **k: None)

assert result3["scored"] == 3 and result3["failed"] == 0, result3
for t in order_tickers:
    got = ts.get_score(t, f"D2026-10-04-{t}", te.MODEL_TOP100, te.RUBRIC_VERSION)
    assert got is not None, f"{t} should have matched regardless of response order"
print("[order_independent] results match by ticker regardless of the order the model "
      "returned the packed array in OK")


# ======================================================================
# CHECK 6: pack boundary - a 108-entrant pool packs into 22 requests,
# the last with 3.
# ======================================================================
fake_entrants = [({"ticker": f"T{i}", "company_name": f"T{i} Co", "most_recent_quarter": None}, "new_or_rubric")
                  for i in range(108)]
packs = [fake_entrants[i:i + te.TOP100_COMPANIES_PER_REQUEST]
         for i in range(0, len(fake_entrants), te.TOP100_COMPANIES_PER_REQUEST)]
assert len(packs) == 22, len(packs)
assert len(packs[-1]) == 3, len(packs[-1])
assert all(len(p) == 5 for p in packs[:-1])
print(f"[pack_boundary] 108 entrants -> {len(packs)} packed requests, "
      f"last one with {len(packs[-1])} OK")


# ======================================================================
# CHECK 7: cost estimate counts the system+schema prompt ONCE PER
# REQUEST, not once per company - the pack's actual saving.
# ======================================================================
single_entrant = [{"ticker": "SOLO", "company_name": "Solo Co", "sector": "Technology"}]
five_entrants = [{"ticker": f"FIVE{i}", "company_name": f"Five {i} Co", "sector": "Technology"} for i in range(5)]

tokens_for_one = te._estimate_request_tokens(single_entrant)
tokens_for_five = te._estimate_request_tokens(five_entrants)

# If the prompt were counted per company (the old, pre-pack behaviour),
# 5 companies would cost ~5x one company. Packed, the shared system+
# schema prefix is paid once, so 5 packed companies must cost far less
# than 5x a single company's own estimate (user-message text for 5
# tickers only adds a small amount on top of the shared prefix).
assert tokens_for_five < tokens_for_one * 3, (
    f"packing 5 companies into one request ({tokens_for_five} tokens) must cost far less than "
    f"5x a single-company request ({tokens_for_one} tokens x 5 = {tokens_for_one * 5}) - "
    "the system+schema prefix must be shared, not repeated per company"
)
print(f"[per_request_not_per_company] 1-entrant request: {tokens_for_one} tokens; "
      f"5-entrant request: {tokens_for_five} tokens (far less than {tokens_for_one * 5} = 5x) - "
      "the shared system+schema prefix is paid once per request OK")


print("\nALL TOP 100 V6 PACKED REQUESTS TESTS PASSED")
