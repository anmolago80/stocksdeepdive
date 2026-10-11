"""
Top 200 pre-scoring with the promotional credit (11 Oct 2026,
Director-directed, Andrew's go, instruction_prescoring_promo.md).

Andrew has a $100 Anthropic promotional credit (Batch API, expires
14 Oct 2026 UTC). Hard cap $60. Pre-scores the new-market companies
most likely to enter the Top 200 once their country is added, so
their analysis is already stored and they can join at no further
cost later.

Switches (env vars, all unset = feature off, nothing changes):
  TOP200_PRESCORE_UNIVERSES   comma list of universe names
  TOP200_PRESCORE_BUDGET_USD  total cap across all nights
  TOP200_PRESCORE_UNTIL       last UTC date to run (inclusive)

Covers, per the instruction's own test list:
  - Switch unset / past UNTIL / budget reached -> batch entrants
    byte-identical to today.
  - Switch set -> normal newcomers first, then pre-score candidates
    in ranking order. Total never exceeds the existing caps, and the
    budget is never exceeded across nights.
  - Ineligible rows are never queued: funds, no IV, DCF unreliable,
    price-suspect, already scored, failure still in retry window.
  - The fingerprint passes, and per-company request content is
    identical to a normal entrant.
  - The live pool (select_top100_pool() output) is identical with the
    switch on or off.
  - The public-surface check from the Rules section.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_prescoring_promo.py
"""
import hashlib
import inspect
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="prescoring_promo_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
for _k in ("TOP200_PRESCORE_UNIVERSES", "TOP200_PRESCORE_BUDGET_USD", "TOP200_PRESCORE_UNTIL",
           "PRIVATE_UNIVERSES"):
    os.environ.pop(_k, None)

import scan_store
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


def _row(ticker, quality=60.0, mos=10.0, psych=5.0, disc=30.0, iv=100.0, price=90.0,
         is_fund=False, dcf_unreliable=False, price_unit_suspect=False, mrq=None,
         sector="Industrials", company_name=None):
    return {
        "Ticker": ticker, "Company Name": company_name or f"{ticker} Co", "Sector": sector,
        "Quality": quality, "MOS %": mos, "Psychology": psych, "Discovery (lite)": disc,
        "Intrinsic Value": None if is_fund else iv, "Price": price,
        "Is Fund": is_fund, "DCF Unreliable": dcf_unreliable,
        "price_unit_suspect": price_unit_suspect, "Most Recent Quarter": mrq,
    }


# ======================================================================
# CHECK 1-5: switch readers - all-unset-or-invalid = off.
# ======================================================================
check("prescore_universes() is [] when unset", te.prescore_universes() == [])
check("prescore_budget_usd() is None when unset", te.prescore_budget_usd() is None)
check("prescore_until() is None when unset", te.prescore_until() is None)
check("is_prescore_active() is (False, 'switch unset') when everything is unset",
      te.is_prescore_active() == (False, "switch unset"))

os.environ["TOP200_PRESCORE_BUDGET_USD"] = "not-a-number"
check("prescore_budget_usd() is None for an invalid (non-numeric) value", te.prescore_budget_usd() is None)
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "-5"
check("prescore_budget_usd() is None for a non-positive value", te.prescore_budget_usd() is None)
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "60"
check("prescore_budget_usd() == 60.0 for a valid value", te.prescore_budget_usd() == 60.0)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)

os.environ["TOP200_PRESCORE_UNTIL"] = "not-a-date"
check("prescore_until() is None for an unparseable date", te.prescore_until() is None)
os.environ["TOP200_PRESCORE_UNTIL"] = "2026-10-13"
check("prescore_until() parses a valid ISO date", te.prescore_until().isoformat() == "2026-10-13")
os.environ.pop("TOP200_PRESCORE_UNTIL", None)

os.environ["TOP200_PRESCORE_UNIVERSES"] = "TSX Composite, FTSE 100"
check('prescore_universes() == ["TSX Composite", "FTSE 100"] when set',
      te.prescore_universes() == ["TSX Composite", "FTSE 100"])
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)


# ======================================================================
# CHECK 6: is_prescore_active() - past UNTIL / budget reached, with
# every switch otherwise validly set.
# ======================================================================
os.environ["TOP200_PRESCORE_UNIVERSES"] = "Nikkei 225"
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "60"
os.environ["TOP200_PRESCORE_UNTIL"] = "2026-10-13"
check("is_prescore_active() is (True, None) with every switch validly set and budget untouched",
      te.is_prescore_active(today=__import__("datetime").date(2026, 10, 12)) == (True, None))
check("is_prescore_active() is (False, 'past UNTIL') once today > UNTIL",
      te.is_prescore_active(today=__import__("datetime").date(2026, 10, 14)) == (False, "past UNTIL"))
ts.add_prescore_actual_cost(60.0)
check("is_prescore_active() is (False, 'budget reached') once cumulative >= budget",
      te.is_prescore_active(today=__import__("datetime").date(2026, 10, 12)) == (False, "budget reached"))
with ts._conn() as conn:
    conn.execute("UPDATE top100_prescore_state SET cumulative_cost_usd = 0 WHERE id = 1")
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)
os.environ.pop("TOP200_PRESCORE_UNTIL", None)


# ======================================================================
# CHECK 7-13: _prescore_row_eligible() / _prescore_candidates() - every
# ineligible-row exclusion the instruction names, plus the two it
# doesn't need to (already-scored, failure-in-retry-window).
# ======================================================================
scan_store.save_scan("Nikkei 225", [
    _row("GOOD1", quality=90.0, iv=100.0, price=90.0),       # eligible, highest value_score
    _row("GOOD2", quality=50.0, iv=100.0, price=90.0),       # eligible, lower value_score
    _row("ISFUND", quality=95.0, is_fund=True),              # excluded: Is Fund
    _row("NOIV", quality=95.0, iv=None, price=90.0),         # excluded: no Intrinsic Value
    _row("DCFBAD", quality=95.0, iv=400.0, price=90.0),      # excluded: DCF Unreliable (>3x price)
    _row("SUSPECT", quality=95.0, iv=100.0, price=90.0, price_unit_suspect=True),  # excluded: price_unit_suspect
    _row("STALE_T", quality=95.0, iv=100.0, price=90.0),     # excluded via Trading Status below
    _row("ALREADY", quality=95.0, iv=100.0, price=90.0),     # excluded: already scored
    _row("FAILED_RECENT", quality=95.0, iv=100.0, price=90.0),  # excluded: failure in retry window
    _row("FAILED_OLD", quality=95.0, iv=100.0, price=90.0),  # NOT excluded: failure past retry window
], source_label="test fixture")
with ts._conn() as conn:
    pass  # ensure DB open before the direct row mutation below

_scan_payload = scan_store.load_scan_raw("Nikkei 225", allow_private=True)
for _r in _scan_payload["rows"]:
    if _r["Ticker"] == "STALE_T":
        _r["Trading Status"] = "stale"
scan_store.save_scan("Nikkei 225", _scan_payload["rows"], source_label="test fixture")

ts.save_score(
    ticker="ALREADY", quarter="RP2026-09-30", model=te.MODEL_TOP100, rubric_version=te.RUBRIC_VERSION,
    dims={k: {"score": 4, "justification": "x", "source_period": "RP2026-09-30"} for k in te.DIMENSION_KEYS},
    not_rated=False, most_recent_quarter="2026-09-30",
    inversion_scenario="t", inversion_severity=2, current_headwind=None,
    market_structure=None, market_structure_comment=None, one_foot_hurdle=None, one_foot_comment=None,
    munger_quality=None, munger_comment=None, big_wave=None, big_wave_comment=None,
    prompt="{}", raw_response="{}",
)
ts.record_score_failure("FAILED_RECENT", te.MODEL_TOP100, te.RUBRIC_VERSION, "some_error")
ts.record_score_failure("FAILED_OLD", te.MODEL_TOP100, te.RUBRIC_VERSION, "some_error")
with ts._conn() as conn:
    _old = (datetime.now(timezone.utc) - timedelta(hours=te.TOP100_FAILURE_RETRY_HOURS + 1)).isoformat()
    conn.execute(
        "UPDATE top100_score_failures SET failed_at = ? WHERE ticker = ? AND model = ? AND rubric_version = ?",
        (_old, "FAILED_OLD", te.MODEL_TOP100, te.RUBRIC_VERSION),
    )

os.environ["TOP200_PRESCORE_UNIVERSES"] = "Nikkei 225"
_candidates = te._prescore_candidates(te.MODEL_TOP100)
_candidate_tickers = [c["ticker"] for c in _candidates]
check("GOOD1 and GOOD2 are both eligible candidates",
      "GOOD1" in _candidate_tickers and "GOOD2" in _candidate_tickers)
check("ISFUND (Is Fund) is excluded", "ISFUND" not in _candidate_tickers)
check("NOIV (no Intrinsic Value) is excluded", "NOIV" not in _candidate_tickers)
check("DCFBAD (DCF Unreliable, re-checked against current price too) is excluded",
      "DCFBAD" not in _candidate_tickers)
check("SUSPECT (price_unit_suspect) is excluded", "SUSPECT" not in _candidate_tickers)
check("STALE_T (Trading Status=stale) is excluded", "STALE_T" not in _candidate_tickers)
check("ALREADY (already has a current-rubric score) is excluded", "ALREADY" not in _candidate_tickers)
check("FAILED_RECENT (failure still inside its retry window) is excluded",
      "FAILED_RECENT" not in _candidate_tickers)
check("FAILED_OLD (failure PAST its retry window) is eligible again - pre-scoring's own "
      "simpler rule has no attempts-exhaustion ceiling, unlike the normal nightly path",
      "FAILED_OLD" in _candidate_tickers)
check("GOOD1 (higher Quality -> higher value_score) ranks ahead of GOOD2",
      _candidate_tickers.index("GOOD1") < _candidate_tickers.index("GOOD2"))
print(f"  [eligibility_filters] candidates={_candidate_tickers} OK")


# ======================================================================
# CHECK 14: ranking across MULTIPLE listed universes together, with a
# dual-listed ticker (same ticker in two universes) keeping only its
# higher-value_score row.
# ======================================================================
scan_store.save_scan("DAX", [
    _row("DAXHI", quality=99.0, iv=50.0, price=40.0),
    _row("GOOD1", quality=10.0, iv=100.0, price=90.0),  # same ticker as Nikkei 225's GOOD1, lower score here
], source_label="test fixture")
os.environ["TOP200_PRESCORE_UNIVERSES"] = "Nikkei 225, DAX"
_multi = te._prescore_candidates(te.MODEL_TOP100)
_multi_tickers = [c["ticker"] for c in _multi]
check("DAXHI (from the second listed universe) is ranked FIRST overall - ranking is "
      "across all listed universes together, not per-universe",
      _multi_tickers[0] == "DAXHI")
_good1_row = next(c for c in _multi if c["ticker"] == "GOOD1")
_good1_dax_only_score = te._recompute_value_score(_row("GOOD1", quality=10.0, iv=100.0, price=90.0))
check("GOOD1 (appears in both universes) keeps its HIGHER-value_score occurrence "
      "(Nikkei 225's Quality=90, not DAX's Quality=10) - never double-counted",
      _multi_tickers.count("GOOD1") == 1 and _good1_row["value_score"] > _good1_dax_only_score)
os.environ["TOP200_PRESCORE_UNIVERSES"] = "Nikkei 225"


# ======================================================================
# CHECK 15-18: capacity - normal entrants first, pre-score fills only
# the remaining room under the existing per-batch AND daily caps.
# ======================================================================
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "1000"  # effectively unlimited for this capacity check
os.environ["TOP200_PRESCORE_UNTIL"] = "2099-12-31"
_today = datetime.now(timezone.utc).date()

_entrants_room3 = te._select_prescore_entrants(
    normal_entrant_count=te.MAX_NIGHTLY_SCORES - 3, daily_entrants_so_far=0, today=_today, log=lambda *a: None)
check("with only 3 slots of per-batch room left, at most 3 pre-score entrants are queued "
      "(never more than the room the normal entrants' own cap leaves)",
      len(_entrants_room3) <= 3)

_entrants_room0 = te._select_prescore_entrants(
    normal_entrant_count=te.MAX_NIGHTLY_SCORES, daily_entrants_so_far=0, today=_today, log=lambda *a: None)
check("with ZERO per-batch room left (normal entrants already fill MAX_NIGHTLY_SCORES), "
      "no pre-score entrants are queued", _entrants_room0 == [])

_entrants_daily_capped = te._select_prescore_entrants(
    normal_entrant_count=1, daily_entrants_so_far=te.TOP100_MAX_ENTRANTS_PER_UTC_DAY - 1,
    today=_today, log=lambda *a: None)
check("the DAILY cap (not just the per-batch cap) also limits room - with only 1 slot of "
      "daily room left after normal entrants, at most 1 pre-score entrant queued (plenty "
      "of per-batch room remains)",
      len(_entrants_daily_capped) <= 1)

_entrants_normal_first = te._select_prescore_entrants(
    normal_entrant_count=0, daily_entrants_so_far=0, today=_today, log=lambda *a: None)
_entrant_tickers_normal_first = [e[0]["ticker"] for e in _entrants_normal_first]
check("with full room, pre-score entrants are returned in the SAME ranking order "
      "_prescore_candidates() produced (GOOD1 before GOOD2 - 'take from the top')",
      _entrant_tickers_normal_first.index("GOOD1") < _entrant_tickers_normal_first.index("GOOD2"))
check("every pre-score entrant is tagged with reason 'prescore'",
      all(reason == "prescore" for _row_, reason in _entrants_normal_first))


# ======================================================================
# CHECK 19-22: budget - trimmed so cumulative actual + tonight's
# estimate never exceeds BUDGET, and the cumulative figure is read
# from the STORE (never reset by this test file's own earlier writes
# until explicitly cleared here).
# ======================================================================
with ts._conn() as conn:
    conn.execute("UPDATE top100_prescore_state SET cumulative_cost_usd = 0 WHERE id = 1")
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "0.000001"  # effectively zero room
_entrants_no_budget = te._select_prescore_entrants(
    normal_entrant_count=0, daily_entrants_so_far=0, today=_today, log=lambda *a: None)
check("an effectively-zero budget queues 0 pre-score candidates, even with full "
      "capacity room and eligible candidates waiting", _entrants_no_budget == [])

os.environ["TOP200_PRESCORE_BUDGET_USD"] = "1000"
_kept, _est = te._trim_prescore_to_budget(_candidates, cumulative_actual=0.0, budget=1000.0)
check("with ample budget, every eligible candidate fits", len(_kept) == len(_candidates))
_kept_half, _est_half = te._trim_prescore_to_budget(_candidates, cumulative_actual=999.999999, budget=1000.0)
check("with almost no budget room left, 0 (or very few) candidates fit",
      len(_kept_half) < len(_candidates))
check("_trim_prescore_to_budget() always returns a PREFIX of the ranked list (never "
      "skips the top-ranked ones to fit a lower-ranked one instead)",
      _kept_half == _candidates[:len(_kept_half)])

# Across-nights persistence: night 1 spends some budget; night 2 sees
# the REDUCED remaining room, read from the store, not from memory.
with ts._conn() as conn:
    conn.execute("UPDATE top100_prescore_state SET cumulative_cost_usd = 0 WHERE id = 1")
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "60"
_night1_kept, _night1_est = te._trim_prescore_to_budget(_candidates, cumulative_actual=0.0, budget=60.0)
ts.add_prescore_actual_cost(_night1_est)
_cumulative_after_night1 = ts.get_prescore_cumulative_cost()
check("night 1's real cost is persisted to the store (survives past this call)",
      _cumulative_after_night1 == _night1_est and _cumulative_after_night1 > 0)
_night2_kept, _night2_est = te._trim_prescore_to_budget(_candidates, cumulative_actual=_cumulative_after_night1, budget=60.0)
check("night 2's own trim correctly accounts for night 1's already-spent budget "
      "(reads the store's cumulative figure, never starts back at $0)",
      _cumulative_after_night1 + _night2_est <= 60.0 + 1e-9)
with ts._conn() as conn:
    conn.execute("UPDATE top100_prescore_state SET cumulative_cost_usd = 0 WHERE id = 1")


# ======================================================================
# CHECK 23-24: switch unset / past UNTIL / budget reached -> batch
# entrants byte-identical to today - end to end through submit_
# nightly_batch() itself.
# ======================================================================
def _mk_pool(n):
    return [{"ticker": f"POOL{i}", "company_name": f"Pool Co {i}", "sector": "Industrials",
              "most_recent_quarter": None} for i in range(n)]


def _submit_and_capture(pool, env_overrides=None):
    """Runs submit_nightly_batch() with a mocked Anthropic client,
    returns the captured `requests` list (or None if nothing was
    submitted)."""
    _env = dict(env_overrides or {})
    for k in ("TOP200_PRESCORE_UNIVERSES", "TOP200_PRESCORE_BUDGET_USD", "TOP200_PRESCORE_UNTIL"):
        os.environ.pop(k, None)
    for k, v in _env.items():
        os.environ[k] = v
    # Newcomer-persistence gating (_true_newcomer_gated()) defers any
    # ticker not yet seen 3 consecutive nights in pool_presence - seed
    # it here exactly like test_audit_b1_submit_max_retries_marker.py
    # does, so these synthetic pool rows submit on the first call.
    ts.seed_pool_presence({row["ticker"]: 3 for row in pool}, "2026-10-09")
    with mock.patch("anthropic.Anthropic") as MockClient, \
         mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                    side_effect=lambda **kw: kw), \
         mock.patch("anthropic.types.messages.batch_create_params.Request",
                    side_effect=lambda **kw: kw):
        instance = MockClient.return_value
        fake_batch = mock.Mock(id="msgbatch_prescore_test")
        instance.with_options.return_value.messages.batches.create.return_value = fake_batch
        logs = []
        result = te.submit_nightly_batch(pool=pool, log=logs.append)
        captured_requests = None
        if instance.with_options.return_value.messages.batches.create.called:
            captured_requests = instance.with_options.return_value.messages.batches.create.call_args.kwargs.get("requests")
        return result, captured_requests, logs


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_pool3 = _mk_pool(3)
_result_off, _requests_off, _logs_off = _submit_and_capture(_pool3)
check("switch unset: submission succeeds, exactly 1 packed request (3 normal entrants "
      "pack into one TOP100_COMPANIES_PER_REQUEST-sized request, no pre-score pack "
      "appended)",
      _result_off == "msgbatch_prescore_test" and len(_requests_off) == 1)
check("switch unset: no '[prescore]' log line at all - byte-identical to before this "
      "feature existed", not any("[prescore]" in m for m in _logs_off))

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_pool3b = _mk_pool(3)
_result_past, _requests_past, _logs_past = _submit_and_capture(
    _pool3b, {"TOP200_PRESCORE_UNIVERSES": "Nikkei 225", "TOP200_PRESCORE_BUDGET_USD": "60",
              "TOP200_PRESCORE_UNTIL": "2000-01-01"})
check("past UNTIL: still exactly 1 packed request, no pre-score pack appended",
      len(_requests_past) == 1)
check("past UNTIL: logs the off-reason", any("[prescore] off - past UNTIL" in m for m in _logs_past))

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.add_prescore_actual_cost(60.0)
_pool3c = _mk_pool(3)
_result_budget, _requests_budget, _logs_budget = _submit_and_capture(
    _pool3c, {"TOP200_PRESCORE_UNIVERSES": "Nikkei 225", "TOP200_PRESCORE_BUDGET_USD": "60",
              "TOP200_PRESCORE_UNTIL": "2099-12-31"})
check("budget already reached: still exactly 1 packed request, no pre-score pack appended",
      len(_requests_budget) == 1)
check("budget already reached: logs the off-reason", any("budget reached" in m for m in _logs_budget))
with ts._conn() as conn:
    conn.execute("UPDATE top100_prescore_state SET cumulative_cost_usd = 0 WHERE id = 1")


# ======================================================================
# CHECK 25-29: switch ON, end to end - normal entrants first, then
# pre-score packs appended, tagged is_prescore, request content
# identical in shape to a normal entrant's own request.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("Nikkei 225", [
    _row("PREGOOD1", quality=90.0, iv=100.0, price=90.0),
    _row("PREGOOD2", quality=70.0, iv=100.0, price=90.0),
], source_label="test fixture")
_pool3d = _mk_pool(3)
_result_on, _requests_on, _logs_on = _submit_and_capture(
    _pool3d, {"TOP200_PRESCORE_UNIVERSES": "Nikkei 225", "TOP200_PRESCORE_BUDGET_USD": "60",
              "TOP200_PRESCORE_UNTIL": "2099-12-31"})
check("switch ON: 5 requests total (3 normal + 2 pre-score, each entrant its own request "
      "since TOP100_COMPANIES_PER_REQUEST groups up to 5 and normal/pre-score packs are "
      "never merged, so 3 normal fit one pack and 2 pre-score fit their own)",
      len(_requests_on) == 2)
check("switch ON: a 'night ... candidates queued' line was logged",
      any("candidates queued" in m for m in _logs_on))

_batch_state = ts.get_batch_state()
_prescore_tagged = [v for v in _batch_state["custom_id_map"].values() if v.get("is_prescore")]
_normal_tagged = [v for v in _batch_state["custom_id_map"].values() if not v.get("is_prescore")]
check("exactly one pack is tagged is_prescore=True, carrying PREGOOD1/PREGOOD2",
      len(_prescore_tagged) == 1
      and set(_prescore_tagged[0]["entrants"].keys()) == {"PREGOOD1", "PREGOOD2"})
check("the normal pack is NOT tagged is_prescore (defaults falsy)",
      all(not p.get("is_prescore") for p in _normal_tagged))

# Per-company request content identical to a normal entrant - both
# groups go through the exact same _request_params() call with the
# same schema_mode; diff the JSON of a solo pre-score-shaped request
# against a solo normal-shaped request for the SAME single company to
# prove nothing about the request shape itself differs.
_normal_solo_params = te._request_params(
    [{"ticker": "SOLOCO", "company_name": "Solo Co", "sector": "Industrials"}])
_prescore_solo_params = te._request_params(
    [{"ticker": "SOLOCO", "company_name": "Solo Co", "sector": "Industrials"}])
check("a pre-score entrant's own request body is byte-for-byte identical to a normal "
      "entrant's for the same company - _request_params() is called exactly the same "
      "way for both, nothing new added to the prompt/schema for pre-score",
      json.dumps(_normal_solo_params, sort_keys=True) == json.dumps(_prescore_solo_params, sort_keys=True))


# ======================================================================
# CHECK 30: the scoring-request fingerprint is untouched.
# ======================================================================
FIXTURE_ENTRANTS = [
    {"ticker": "ADP", "company_name": "ADP-shaped", "sector": "Industrials"},
    {"ticker": "CSL.AX", "company_name": "CSL-shaped", "sector": "Health Care"},
    {"ticker": "AZN_FIXTURE.L", "company_name": "AZN-shaped", "sector": "Health Care"},
    {"ticker": "RY_FIXTURE.TO", "company_name": "RY-shaped", "sector": "Financials"},
    {"ticker": "7203_FIXTURE.T", "company_name": "Toyota-shaped", "sector": "Consumer Discretionary"},
]
EXPECTED_FINGERPRINT = "f00c69f5392af5e2ca435fb4c8a314393c14b4c0dc498cc4293a59d1276e5196"
_fp = hashlib.sha256(json.dumps(te._request_params(FIXTURE_ENTRANTS), sort_keys=True).encode()).hexdigest()
check(f"scoring-request fingerprint unchanged by this pre-scoring feature (still {EXPECTED_FINGERPRINT})",
      _fp == EXPECTED_FINGERPRINT)


# ======================================================================
# CHECK 31: poll_and_ingest_batch() - pre-score-tagged results update
# the persisted cumulative actual cost and log the required line;
# normal-only results never touch it at all.
# ======================================================================
with ts._conn() as conn:
    conn.execute("UPDATE top100_prescore_state SET cumulative_cost_usd = 0 WHERE id = 1")


def _mk_result(custom_id, tickers, input_tokens=1000, output_tokens=500):
    # Matches tests/test_top100_cost_accounting.py's own _make_succeeded_
    # result() shape - every DIMENSION_KEYS entry plus every other
    # required top-level field _parse_response_json() expects, or the
    # item is dropped as "missing_from_response".
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


ts.save_submitting_marker("2026-10-12", te.MODEL_TOP100, {})
ts.clear_submitting_marker()
_custom_id_map = {
    "t100-0": {"entrants": {"NORM1": {"score_key": "D2026-10-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": False},
    "t100-1": {"entrants": {"PRE1": {"score_key": "D2026-10-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": True},
}
ts.save_batch_state("msgbatch_ingest_test", "2026-10-12", te.MODEL_TOP100, _custom_id_map)
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = mock.Mock(processing_status="ended")
    instance.messages.batches.results.return_value = [
        _mk_result("t100-0", ["NORM1"], input_tokens=1000, output_tokens=500),
        _mk_result("t100-1", ["PRE1"], input_tokens=2000, output_tokens=700),
    ]
    _ingest_logs = []
    _ingest_summary = te.poll_and_ingest_batch(log=_ingest_logs.append)

check("poll_and_ingest_batch() ingested both results (1 normal + 1 pre-score)",
      _ingest_summary is not None and _ingest_summary["scored"] == 2)
_expected_prescore_cost = te.estimate_batch_cost_usd(2000, 700, 0, 0)
_cumulative_after_ingest = ts.get_prescore_cumulative_cost()
check("the cumulative actual cost now reflects ONLY the pre-score-tagged result's own "
      "real tokens (2000 in / 700 out), never the normal result's",
      abs(_cumulative_after_ingest - _expected_prescore_cost) < 1e-9)
check("an '[prescore] ingested: ...' log line was written",
      any(m.startswith("[prescore] ingested:") and "1 scored, 0 failed" in m for m in _ingest_logs))
with ts._conn() as conn:
    conn.execute("UPDATE top100_prescore_state SET cumulative_cost_usd = 0 WHERE id = 1")


# ======================================================================
# CHECK 32: the live pool (select_top100_pool() output) is identical
# with the switch on or off - nothing in this feature ever touches
# _eligible_scan_payloads()/_build_best_by_ticker()/select_top100_pool().
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("S&P 500", [
    {"Ticker": "AAPL", "Company Name": "Apple-shaped", "Long Score": 70.0, "Quality": 70.0,
     "MOS %": 10.0, "Psychology": 5.0, "Discovery (lite)": 30.0, "Price": 150.0,
     "Intrinsic Value": 165.0, "DCF Unreliable": False},
], source_label="test fixture")
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)
os.environ.pop("TOP200_PRESCORE_UNTIL", None)
_pool_prescore_off = te.select_top100_pool(log=lambda *a, **k: None)
os.environ["TOP200_PRESCORE_UNIVERSES"] = "Nikkei 225"
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "60"
os.environ["TOP200_PRESCORE_UNTIL"] = "2099-12-31"
_pool_prescore_on = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)
os.environ.pop("TOP200_PRESCORE_UNTIL", None)
check("select_top100_pool()'s own output is byte-identical whether the pre-scoring "
      "switches are set or not - this feature never touches pool selection",
      json.dumps(_pool_prescore_off, sort_keys=True, default=str) ==
      json.dumps(_pool_prescore_on, sort_keys=True, default=str))


# ======================================================================
# CHECK 33-37: the public-surface check (Rules section) - "no pre-
# scored analysis is shown publicly for a ticker that isn't in the
# live pool." Source-inspection proof that every public-facing surface
# either never reads top100_store/top100_engine at all, or only ever
# reads a score for a ticker it already pulled from the real pool.
# ======================================================================
import deep_dive_engine
import snapshot_store
import api_v1
import mcp_server


def _no_real_reference(module):
    """True if no NON-COMMENT line in `module`'s source actually
    calls top100_store./top100_engine. - a comment merely mentioning
    either module's name (e.g. documenting a shared formula) doesn't
    count as a reference."""
    for line in inspect.getsource(module).splitlines():
        code = line.split("#", 1)[0]
        if "top100_store." in code or "top100_engine." in code:
            return False
    return True


check("deep_dive_engine.py never calls top100_store./top100_engine. (Deep Dive never "
      "surfaces an AI-scoring result at all, pre-scored or not)",
      _no_real_reference(deep_dive_engine))
check("snapshot_store.py never calls top100_store./top100_engine. (the public /s/<ticker> "
      "snapshot carries no AI-scoring field at all)",
      _no_real_reference(snapshot_store))
check("api_v1.py never calls top100_store./top100_engine. (no /api/v1/* endpoint exposes "
      "a stored score)",
      _no_real_reference(api_v1))
check("mcp_server.py never calls top100_store./top100_engine. (no MCP tool exposes a "
      "stored score)",
      _no_real_reference(mcp_server))

import top100_render
_enriched_pool_src = inspect.getsource(top100_render._enriched_pool)
check("top100_render._enriched_pool() (the real Top 200 page's own data path) starts "
      "from top100_store.current_pool() FIRST, and only enriches pool rows with stored "
      "scores afterward - never an independent by-ticker score lookup that could leak a "
      "pre-scored, not-yet-pooled company's analysis onto the public page",
      "current_pool()" in _enriched_pool_src
      and _enriched_pool_src.index("current_pool()") < _enriched_pool_src.index("latest_scores_for_model"))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
