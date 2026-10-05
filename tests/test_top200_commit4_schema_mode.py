"""
Top 200 COMMIT 4 of instruction_top200_unrated_and_blank_replies_combined.md
(5 Oct 2026, Director-directed, owner-approved for build+push-on-go -
"ticker first in the response schema"). Switch: TOP200_SCHEMA_MODE,
unset by default = "legacy".

Covers:
  - top200_schema_mode(): unset/"" -> "legacy"; "ticker_first" ->
    "ticker_first"; any other value -> "legacy", logged as a WARNING
    exactly once for the life of the process (not re-logged on a
    second call with the same bad value).
  - Request fingerprint: with the switch unset, _request_params() for
    the task's own 5 fixture entrants (ADP/CSL.AX/AZN_FIXTURE.L/
    RY_FIXTURE.TO/7203_FIXTURE.T) must sha256(sorted-JSON) to
    f00c69f5392af5e2ca435fb4c8a314393c14b4c0dc498cc4293a59d1276e5196 -
    the exact value recorded in the governing instruction document and
    confirmed on 5 Oct 2026, proving this commit changed nothing about
    the live request when the switch is off.
  - schema_mode="ticker_first": a unified diff of the two complete
    requests for the same 5 fixture entrants shows ONLY "ticker"'s own
    position in the per-company properties object moving - nothing
    else (not model, max_tokens, system, messages, or the `required`
    list's own order).
  - Parser order-independence: the same company item, serialized with
    ticker first vs. last among its own JSON properties, parses to the
    identical result - proving a schema_mode flip can never change
    what gets stored for an otherwise-identical reply.
  - top100_store: save_score()/record_score_failure() record
    schema_mode on a new row; an existing (pre-commit) row with no
    schema_mode column value reads back as None, and schema_mode_
    for_batch()/score_failures_for_model() both read that as "legacy".
  - top100_engine.inspect_batch_results()/batch_inspector_csv(): a
    batch with both a packed (multi-entrant) and solo (single-entrant)
    request, one under each schema_mode, produces the two new columns
    correctly; blank vs non-blank classification is untouched.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top200_commit4_schema_mode.py
"""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top200_commit4_schema_mode_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_SCHEMA_MODE", None)

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

MODEL = te.MODEL_TOP100
RV = te.RUBRIC_VERSION

FIXTURE_ENTRANTS = [
    {"ticker": "ADP", "company_name": "ADP-shaped", "sector": "Industrials"},
    {"ticker": "CSL.AX", "company_name": "CSL-shaped", "sector": "Health Care"},
    {"ticker": "AZN_FIXTURE.L", "company_name": "AZN-shaped", "sector": "Health Care"},
    {"ticker": "RY_FIXTURE.TO", "company_name": "RY-shaped", "sector": "Financials"},
    {"ticker": "7203_FIXTURE.T", "company_name": "Toyota-shaped", "sector": "Consumer Discretionary"},
]
EXPECTED_FINGERPRINT = "f00c69f5392af5e2ca435fb4c8a314393c14b4c0dc498cc4293a59d1276e5196"

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


# --- top200_schema_mode() switch reader ------------------------------
print("top200_schema_mode() switch reader")
os.environ.pop("TOP200_SCHEMA_MODE", None)
check("unset -> legacy", te.top200_schema_mode(log=lambda *a: None) == "legacy")
os.environ["TOP200_SCHEMA_MODE"] = ""
check('empty string -> legacy', te.top200_schema_mode(log=lambda *a: None) == "legacy")
os.environ["TOP200_SCHEMA_MODE"] = "ticker_first"
check("ticker_first -> ticker_first", te.top200_schema_mode(log=lambda *a: None) == "ticker_first")
os.environ["TOP200_SCHEMA_MODE"] = "bogus_value_xyz"
_logged = []
check("unrecognized -> legacy", te.top200_schema_mode(log=lambda m: _logged.append(m)) == "legacy")
check("unrecognized logged once (first call)", len(_logged) == 1)
check("unrecognized -> legacy (2nd call)", te.top200_schema_mode(log=lambda m: _logged.append(m)) == "legacy")
check("unrecognized NOT re-logged (2nd call)", len(_logged) == 1)
os.environ.pop("TOP200_SCHEMA_MODE", None)

# --- Request fingerprint, switch unset -------------------------------
print("Request fingerprint (switch unset)")
params_legacy_default = te._request_params(FIXTURE_ENTRANTS)
fp = hashlib.sha256(json.dumps(params_legacy_default, sort_keys=True).encode()).hexdigest()
check(f"fingerprint == {EXPECTED_FINGERPRINT}", fp == EXPECTED_FINGERPRINT)

params_legacy_explicit = te._request_params(FIXTURE_ENTRANTS, schema_mode="legacy")
check("explicit schema_mode='legacy' byte-identical to default",
      json.dumps(params_legacy_default, sort_keys=True) == json.dumps(params_legacy_explicit, sort_keys=True))

# --- ticker_first: unified diff touches only ticker's position -------
print("schema_mode='ticker_first' unified diff")
params_tf = te._request_params(FIXTURE_ENTRANTS, schema_mode="ticker_first")

legacy_props = list(params_legacy_default["output_config"]["format"]["schema"]
                     ["properties"]["companies"]["items"]["properties"].keys())
tf_props = list(params_tf["output_config"]["format"]["schema"]
                 ["properties"]["companies"]["items"]["properties"].keys())
check("ticker_first: ticker is first", tf_props[0] == "ticker")
check("legacy: ticker is NOT first", legacy_props[0] != "ticker")
check("ticker_first: dropping ticker reproduces legacy's own order",
      [k for k in tf_props if k != "ticker"] == [k for k in legacy_props if k != "ticker"])

legacy_req = params_legacy_default["output_config"]["format"]["schema"]["properties"]["companies"]["items"]["required"]
tf_req = params_tf["output_config"]["format"]["schema"]["properties"]["companies"]["items"]["required"]
check("required list order unchanged by schema_mode", legacy_req == tf_req)

for key in ("model", "max_tokens", "system", "messages"):
    check(f"'{key}' unchanged by schema_mode", params_legacy_default[key] == params_tf[key])

import difflib
legacy_text = json.dumps(params_legacy_default, indent=2).splitlines()
tf_text = json.dumps(params_tf, indent=2).splitlines()
diff_lines = [l for l in difflib.unified_diff(legacy_text, tf_text, lineterm="") if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
diff_text = "\n".join(diff_lines)
check('diff mentions "ticker"', '"ticker"' in diff_text)
# Every other property name (the ten dimension keys + the eleven other
# per-item fields) must be ABSENT from the diff - only the "ticker"
# property's own block should have moved.
_other_property_names = [k for k in legacy_props if k != "ticker"]
check("diff names no other per-item property (only ticker moved)",
      not any(f'"{name}"' in diff_text for name in _other_property_names))

# --- Parser order-independence ---------------------------------------
print("Parser order-independence (schema_mode never changes parsed result)")
_dims_item = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
_common_fields = {
    "inversion_scenario": "scenario", "inversion_severity": 2, "current_headwind": "hw",
    "market_structure": "oligopoly", "market_structure_comment": "mc",
    "one_foot_hurdle": "yes", "one_foot_comment": "oc",
    "munger_quality": "yes", "munger_comment": "mqc",
    "big_wave": "tailwind", "big_wave_comment": "bwc",
}
item_legacy_order = dict(_dims_item)
item_legacy_order["ticker"] = "ADP"
item_legacy_order.update(_common_fields)

item_ticker_first_order = {"ticker": "ADP"}
item_ticker_first_order.update(_dims_item)
item_ticker_first_order.update(_common_fields)

result_legacy = te._parse_response_json(
    json.dumps({"companies": [item_legacy_order]}), expected_tickers=["ADP"])
result_tf = te._parse_response_json(
    json.dumps({"companies": [item_ticker_first_order]}), expected_tickers=["ADP"])
check("parsed result identical regardless of wire property order", result_legacy == result_tf)

# --- top100_store: schema_mode persistence ---------------------------
print("top100_store: schema_mode persistence")
dims = {k: {"score": 3, "justification": "", "source_period": ""} for k in te.DIMENSION_KEYS}
ts.save_score("C4AAA", "RP2026-10-05", MODEL, RV, dims, False, None, None, "p", "r",
              schema_mode="ticker_first")
ts.save_score("C4BBB", "RP2026-10-05", MODEL, RV, dims, False, None, None, "p", "r")  # no schema_mode -> legacy row
latest = ts.latest_scores_for_model(MODEL, RV)
check("new row records schema_mode", latest["C4AAA"]["schema_mode"] == "ticker_first")
check("row saved without schema_mode reads back as None (pre-commit convention)",
      latest["C4BBB"]["schema_mode"] is None)

ts.record_score_failure("C4FAIL", MODEL, RV, "request_blank", schema_mode="ticker_first")
failures = ts.score_failures_for_model(MODEL, RV)
check("failure row records schema_mode", failures["C4FAIL"]["schema_mode"] == "ticker_first")
ts.record_score_failure("C4FAIL", MODEL, RV, "request_blank")  # omitted on retry -> must NOT blank it
failures2 = ts.score_failures_for_model(MODEL, RV)
check("omitted schema_mode on retry does not blank the stored value (COALESCE-preserve)",
      failures2["C4FAIL"]["schema_mode"] == "ticker_first")

ts.record_ingest_cost(
    "c4batch1", scored=2, failed=0, input_tokens=100, cache_creation_tokens=0,
    cache_read_tokens=0, output_tokens=200, cost_usd=0.01,
    custom_id_map={
        "p0": {"entrants": {"C4AAA": {}, "C4BBB": {}}, "schema_mode": "ticker_first"},
        "p1": {"entrants": {"C4CCC": {}}},  # no schema_mode key -> legacy, solo (1 entrant)
    },
)
schema_mode_map = ts.schema_mode_for_batch("c4batch1")
check("schema_mode_for_batch: packed pack reads its stored mode", schema_mode_map["p0"] == "ticker_first")
check("schema_mode_for_batch: pack with no stored mode reads legacy", schema_mode_map["p1"] == "legacy")
check("schema_mode_for_batch: missing batch id -> None", ts.schema_mode_for_batch("no_such_batch") is None)

# --- Admin breakdown ---------------------------------------------------
print("top100_engine.schema_mode_breakdown()")
breakdown = te.schema_mode_breakdown(model=MODEL)
check("breakdown has ticker_first group", "ticker_first" in breakdown)
check("breakdown has legacy group (includes None-schema_mode row)",
      breakdown.get("legacy", {}).get("count", 0) >= 1)
check("breakdown ticker_first count == 1", breakdown["ticker_first"]["count"] == 1)
check("breakdown mean_composite is a number for a rated group",
      isinstance(breakdown["ticker_first"]["mean_composite"], float))

# --- Batch inspector: schema_mode + packed_or_solo columns -----------
print("inspect_batch_results()/batch_inspector_csv(): new columns")
raw_results = [
    {"custom_id": "p0", "type": "succeeded", "stop_reason": "end_turn",
     "input_tokens": 500, "output_tokens": 800,
     "text": json.dumps({"companies": [
         {**item_ticker_first_order, "ticker": "C4AAA"},
         {**item_ticker_first_order, "ticker": "C4BBB"},
     ]})},
    {"custom_id": "p1", "type": "succeeded", "stop_reason": "end_turn",
     "input_tokens": 100, "output_tokens": 200,
     "text": json.dumps({"companies": [{**item_legacy_order, "ticker": "C4CCC"}]})},
]
expected_map = {"p0": ["C4AAA", "C4BBB"], "p1": ["C4CCC"]}
schema_mode_map2 = {"p0": "ticker_first", "p1": "legacy"}
rows, summary = te.inspect_batch_results(raw_results, expected_map, schema_mode_map2)
by_id = {r["custom_id"]: r for r in rows}
check("p0 (packed, 2 entrants) classified packed_or_solo='packed'", by_id["p0"]["packed_or_solo"] == "packed")
check("p1 (solo, 1 entrant) classified packed_or_solo='solo'", by_id["p1"]["packed_or_solo"] == "solo")
check("p0 schema_mode carried through", by_id["p0"]["schema_mode"] == "ticker_first")
check("p1 schema_mode carried through", by_id["p1"]["schema_mode"] == "legacy")
check("p0 fully matched (both tickers echoed)", summary["fully_matched"] >= 1)
check("blank classification untouched (neither request is blank)",
      by_id["p0"]["blank"] is False and by_id["p1"]["blank"] is False)

csv_text = te.batch_inspector_csv(rows)
csv_header = csv_text.splitlines()[0]
check('CSV header includes "schema_mode"', "schema_mode" in csv_header)
check('CSV header includes "packed_or_solo"', "packed_or_solo" in csv_header)
check('CSV body carries "ticker_first" and "packed"', "ticker_first" in csv_text and "packed" in csv_text)
check('CSV body carries "legacy" and "solo"', "legacy" in csv_text and "solo" in csv_text)

# --- inspect_batch_results with NO schema_mode_map (batch predates commit) ---
print("inspect_batch_results(): schema_mode_map=None (pre-commit batch)")
rows_no_map, _ = te.inspect_batch_results(raw_results, expected_map, schema_mode_map=None)
check("schema_mode is None when schema_mode_map itself is None (not available for batch)",
      all(r["schema_mode"] is None for r in rows_no_map))

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
sys.exit(1 if failed else 0)
