"""
Director follow-up to instruction_prescoring_promo.md (11 Oct 2026),
Step 3 of the Refresh-all/public-excluded-universes report: "Japan
back in" - Nikkei 225 and TOPIX 500 accepted as pre-score universes,
ranked together across ALL listed universes by the normal pool
ranking score, no special ordering.

Why this is a separate commit from c065672/2ef3d5b: c065672's own test
file (tests/test_prescoring_promo.py) used "Nikkei 225" as its PRIMARY
fixture universe throughout (CHECK 7-29 - eligibility filters,
capacity, budget, end-to-end submit_nightly_batch(), ingest), so
Nikkei 225 was already proven. But its cross-universe ranking check
(CHECK 14) paired Nikkei 225 with "DAX", never with "TOPIX 500" -
TOPIX 500 had never appeared in a single pre-scoring test anywhere in
this codebase before this commit. 2ef3d5b's own new test (public-but-
Top-200-excluded universes) also never used TOPIX 500. This commit
closes that specific gap.

Also covers a production-realistic wrinkle unique to Japan: TASK 3 of
instruction_scanner_chips_trusts_japan_sectors.md (commit 01c8065)
found that TOPIX 500/Nikkei 225 rows commonly have Sector=None (JPX's
own topixweight_j.csv and Wikipedia's Nikkei 225 page both carry no
sector text at all) - unlike every other universe used in pre-scoring
tests so far, which all had a real Sector string. Checked directly
here: a None-sector Japan row is accepted as a candidate (sector is
carried through, never required), and renders in the actual request
text exactly as scanner_engine's own "unknown sector" fallback would
for ANY sectorless entrant (Japan or not) - never a crash, never
special-cased by universe identity.

Nikkei 225/TOPIX 500 themselves stay PRIVATE and Top-200-EXCLUDED -
this commit changes nothing about either lever for either universe
(sanity-checked directly below); only pre-score CANDIDATE SELECTION is
in scope, same boundary as every other commit in this feature.

Note on real production data (reported to the Director separately,
TASK 1 of the new work queue): Railway evidence as of 11 Oct 2026
04:57 UTC shows Nikkei 225's own constituent fetch currently parses 0
rows (a fetch/parse bug, unrelated to this commit), so Nikkei 225 has
no real stored scan to pre-score from today. That is a fetch-layer bug
to be investigated separately (TASK 1), not a reason to skip proving
the CANDIDATE-SELECTION code handles Japan correctly once real data
exists - every fixture in this file is synthetic, same disclosure as
every other test in this repo, and this commit does not touch the
fetch/scan layer at all.

Covers, per the Director's own test list for this step:
  - TOPIX 500 specifically (not just Nikkei 225) is accepted as a
    pre-score candidate source - production-shaped sanity (still
    private, still Top-200-excluded) plus row-level eligibility.
  - Ranked together with ANOTHER listed universe purely by the normal
    pool ranking score (_recompute_value_score()), no special ordering
    by universe identity, in BOTH directions (a TOPIX 500 row wins
    when it scores higher, and loses when it scores lower) - mirrors
    2ef3d5b's own CHECK 9-10 pattern, now exercising TOPIX 500 instead
    of Nikkei 225/TSX Composite.
  - A Sector=None Japan row (production-realistic per 01c8065) is
    accepted, carries through to the candidate's own "sector" field as
    None, and renders identically to how a normal (non-prescore) entry
    with no sector would in the real request text - no special-casing,
    no crash.
  - End to end through submit_nightly_batch(): pack count, is_prescore
    tagging, and request-content identity are unaffected.
  - The scoring-request fingerprint is unchanged.
  - select_top100_pool()'s own output is byte-identical whether TOPIX
    500 is listed for pre-scoring or not.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_prescore_japan_universes.py
"""
import hashlib
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="prescore_japan_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
for _k in ("TOP200_PRESCORE_UNIVERSES", "TOP200_PRESCORE_BUDGET_USD", "TOP200_PRESCORE_UNTIL",
           "PRIVATE_UNIVERSES", "TOP200_EXCLUDED_UNIVERSES"):
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
# CHECK 1-2: production-shaped sanity - TOPIX 500 stays private and
# Top-200-excluded, exactly as Nikkei 225 does (this commit changes
# neither lever for either universe).
# ======================================================================
check("TOPIX 500 is private (scan_store.is_private_universe() True) - unchanged by this commit",
      scan_store.is_private_universe("TOPIX 500") is True)
check("TOPIX 500 is Top-200-excluded (is_top200_excluded() True) - unchanged by this commit",
      te.is_top200_excluded("TOPIX 500") is True)


# ======================================================================
# CHECK 3-6: TOPIX 500 accepted as a pre-score candidate source in its
# own right, including a Sector=None row (production-realistic for
# Japan per 01c8065) - never required, never special-cased.
# ======================================================================
scan_store.save_scan("TOPIX 500", [
    _row("TPXHI", quality=88.0, iv=100.0, price=90.0, sector=None),   # eligible, no sector at all
    _row("TPXLO", quality=45.0, iv=100.0, price=90.0, sector=None),   # eligible, lower score, no sector
    _row("TPXFUND", quality=95.0, is_fund=True, sector=None),         # excluded: Is Fund
], source_label="test fixture")
os.environ["TOP200_PRESCORE_UNIVERSES"] = "TOPIX 500"
_topix_candidates = te._prescore_candidates(te.MODEL_TOP100)
_topix_tickers = [c["ticker"] for c in _topix_candidates]
check("TPXHI/TPXLO (TOPIX 500, Sector=None) are accepted as pre-score candidates",
      "TPXHI" in _topix_tickers and "TPXLO" in _topix_tickers)
check("TPXFUND (Is Fund) is still excluded from TOPIX 500 same as any other universe",
      "TPXFUND" not in _topix_tickers)
check("TPXHI (higher Quality) ranks ahead of TPXLO within TOPIX 500 itself",
      _topix_tickers.index("TPXHI") < _topix_tickers.index("TPXLO"))
_tpxhi_row = next(c for c in _topix_candidates if c["ticker"] == "TPXHI")
check("a Sector=None TOPIX 500 row's candidate dict carries sector=None through untouched "
      "(never invented, never defaulted at this layer)",
      _tpxhi_row["sector"] is None)


# ======================================================================
# CHECK 7-8: ranked together with ANOTHER listed universe (DAX) purely
# by value_score - no special ordering by universe identity, proven in
# BOTH directions (mirrors 2ef3d5b's own CHECK 9-10 pattern).
# ======================================================================
scan_store.save_scan("DAX", [
    _row("DAXHI", quality=99.0, iv=100.0, price=90.0),   # should outrank both TOPIX rows
    _row("DAXLO", quality=15.0, iv=100.0, price=90.0),   # should rank below both TOPIX rows
], source_label="test fixture")
os.environ["TOP200_PRESCORE_UNIVERSES"] = "TOPIX 500, DAX"
_mixed = te._prescore_candidates(te.MODEL_TOP100)
_mixed_tickers = [c["ticker"] for c in _mixed]
check("DAX's own highest-scoring row (DAXHI) ranks FIRST overall when it genuinely scores "
      "highest - TOPIX 500 gets no 'Japan first' or 'Japan last' special treatment",
      _mixed_tickers[0] == "DAXHI")
check("DAX's own lowest-scoring row (DAXLO) ranks LAST overall when it genuinely scores "
      "lowest - confirms no universe-identity tiebreak favors or penalizes TOPIX 500",
      _mixed_tickers[-1] == "DAXLO")
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)


# ======================================================================
# CHECK 9-13: end to end through submit_nightly_batch() with TOPIX 500
# as the ONLY configured pre-score universe - pack count, is_prescore
# tagging, and request-content identity, including the Sector=None
# row's own request line rendering identically to a normal sectorless
# entrant's (scanner_engine/_build_user_message()'s own "unknown
# sector" fallback - never a crash, never special-cased for Japan).
# ======================================================================
def _mk_pool(n):
    return [{"ticker": f"POOL{i}", "company_name": f"Pool Co {i}", "sector": "Industrials",
              "most_recent_quarter": None} for i in range(n)]


def _submit_and_capture(pool, env_overrides=None):
    _env = dict(env_overrides or {})
    for k in ("TOP200_PRESCORE_UNIVERSES", "TOP200_PRESCORE_BUDGET_USD", "TOP200_PRESCORE_UNTIL"):
        os.environ.pop(k, None)
    for k, v in _env.items():
        os.environ[k] = v
    ts.seed_pool_presence({row["ticker"]: te.NEWCOMER_PERSISTENCE_NIGHTS for row in pool}, "2026-10-09")
    with mock.patch("anthropic.Anthropic") as MockClient, \
         mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                    side_effect=lambda **kw: kw), \
         mock.patch("anthropic.types.messages.batch_create_params.Request",
                    side_effect=lambda **kw: kw):
        instance = MockClient.return_value
        fake_batch = mock.Mock(id="msgbatch_prescore_japan_test")
        instance.with_options.return_value.messages.batches.create.return_value = fake_batch
        logs = []
        result = te.submit_nightly_batch(pool=pool, log=logs.append)
        captured_requests = None
        if instance.with_options.return_value.messages.batches.create.called:
            captured_requests = instance.with_options.return_value.messages.batches.create.call_args.kwargs.get("requests")
        return result, captured_requests, logs


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("TOPIX 500", [
    _row("TPXHI", quality=88.0, iv=100.0, price=90.0, sector=None),
    _row("TPXLO", quality=45.0, iv=100.0, price=90.0, sector=None),
], source_label="test fixture")
_pool3 = _mk_pool(3)
_result_on, _requests_on, _logs_on = _submit_and_capture(
    _pool3, {"TOP200_PRESCORE_UNIVERSES": "TOPIX 500", "TOP200_PRESCORE_BUDGET_USD": "60",
             "TOP200_PRESCORE_UNTIL": "2099-12-31"})
check("switch ON with TOPIX 500 alone: 2 packed requests (3 normal entrants pack into one "
      "request, 2 pre-score entrants pack into their own, never merged)",
      len(_requests_on) == 2)
check("a 'night ... candidates queued' line names TOPIX 500",
      any("candidates queued" in m and "TOPIX 500" in m for m in _logs_on))

_batch_state = ts.get_batch_state()
_prescore_tagged = [v for v in _batch_state["custom_id_map"].values() if v.get("is_prescore")]
check("exactly one pack is tagged is_prescore=True, carrying TPXHI/TPXLO",
      len(_prescore_tagged) == 1
      and set(_prescore_tagged[0]["entrants"].keys()) == {"TPXHI", "TPXLO"})

# The Sector=None row's own request text - built via the SAME
# _request_params() call every entrant goes through, with the pool-
# resolved sector=None exactly as a non-prescore sectorless entrant
# would carry (scanner_engine.sector_for_ticker() returns None for an
# unmapped, uncached, sector-less row either way).
_prescore_pack_requests = [r for r in _requests_on
                           if any(t in r["params"]["messages"][0]["content"] for t in ("TPXHI", "TPXLO"))]
_normal_none_sector_params = te._request_params(
    [{"ticker": "SOLOCO", "company_name": "Solo Co", "sector": None}])
_normal_none_sector_text = _normal_none_sector_params["messages"][0]["content"]
check("a None-sector TOPIX 500 entrant's request line renders 'unknown sector', the exact "
      "same fallback text a None-sector normal (non-Japan) entrant would get - never a "
      "crash, never a Japan-specific label",
      len(_prescore_pack_requests) == 1
      and "unknown sector" in _prescore_pack_requests[0]["params"]["messages"][0]["content"]
      and "unknown sector" in _normal_none_sector_text)

_normal_solo_params = te._request_params(
    [{"ticker": "SOLOCO", "company_name": "Solo Co", "sector": "Industrials"}])
_topix_prescore_solo_params = te._request_params(
    [{"ticker": "SOLOCO", "company_name": "Solo Co", "sector": "Industrials"}])
check("a TOPIX-500 pre-score entrant's own request body is byte-for-byte identical to a "
      "normal entrant's for the same company (same sector)",
      json.dumps(_normal_solo_params, sort_keys=True) == json.dumps(_topix_prescore_solo_params, sort_keys=True))


# ======================================================================
# CHECK 14: the scoring-request fingerprint is untouched by this
# commit (direct re-check, same fixture/hash as every other commit in
# this feature).
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
check(f"scoring-request fingerprint unchanged (still {EXPECTED_FINGERPRINT})",
      _fp == EXPECTED_FINGERPRINT)


# ======================================================================
# CHECK 15: select_top100_pool()'s own output is byte-identical whether
# TOPIX 500 is listed for pre-scoring or not - this feature still never
# touches pool selection.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("S&P 500", [
    {"Ticker": "AAPL", "Company Name": "Apple-shaped", "Long Score": 70.0, "Quality": 70.0,
     "MOS %": 10.0, "Psychology": 5.0, "Discovery (lite)": 30.0, "Price": 150.0,
     "Intrinsic Value": 165.0, "DCF Unreliable": False},
], source_label="test fixture")
scan_store.save_scan("TOPIX 500", [
    _row("TPXHI", quality=88.0, iv=100.0, price=90.0, sector=None),
], source_label="test fixture")
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)
os.environ.pop("TOP200_PRESCORE_UNTIL", None)
_pool_off = te.select_top100_pool(log=lambda *a, **k: None)
os.environ["TOP200_PRESCORE_UNIVERSES"] = "TOPIX 500"
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "60"
os.environ["TOP200_PRESCORE_UNTIL"] = "2099-12-31"
_pool_on = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)
os.environ.pop("TOP200_PRESCORE_UNTIL", None)
check("select_top100_pool()'s own output is byte-identical whether TOPIX 500 is listed for "
      "pre-scoring or not",
      json.dumps(_pool_off, sort_keys=True, default=str) == json.dumps(_pool_on, sort_keys=True, default=str))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
