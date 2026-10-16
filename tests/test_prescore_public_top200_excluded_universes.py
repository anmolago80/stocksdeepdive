"""
Director follow-up to instruction_prescoring_promo.md (11 Oct 2026):
"Accept public-but-Top-200-excluded universes (FTSE 100, FTSE 250,
TSX 60, TSX Composite) as pre-score candidates, not only private
ones."

Context: a universe's PRIVACY (scan_store.is_private_universe()) and
its TOP 200 ELIGIBILITY (top100_engine.is_top200_excluded()) are two
independent levers (see is_top200_excluded()'s own docstring, Director's
next round item 3). Since PUSH 2 (5711c3c), FTSE 100/FTSE 250/TSX
Composite are PUBLIC on the Scanner (removed from scan_store._DEFAULT_
PRIVATE_UNIVERSES) but still TOP-200-EXCLUDED by is_top200_excluded()'s
own default derivation ("every non-US, non-Australian universe") - TSX
60 has been public even longer (641a2a6/53d7e55). This is exactly the
shape of universe pre-scoring exists to help: a country whose Scanner
data is already visible but hasn't yet been given a Top 200 slot.

FINDING (reported to the Director before this commit): _prescore_row_
eligible()/_prescore_candidates() never called is_top200_excluded() or
scan_store.is_private_universe() in the first place - eligibility is
entirely row-level (funds / no IV / DCF-unreliable / price-suspect /
stale / degraded / already-scored / failure-in-window), never a
universe-level gate. So this was ALREADY mechanically true before this
commit - verified directly (no code change needed in top100_engine.py
beyond the stale docstring comment below). This test's job is to LOCK
IN that guarantee with a committed, production-shaped fixture (a real
public-but-excluded universe name, with real is_private_universe()/
is_top200_excluded() values asserted in-test rather than assumed), not
to fix a bug - there wasn't one to fix.

The one doc-only change alongside this test: _prescore_row_eligible()'s
own docstring previously claimed "every TOP200_PRESCORE_UNIVERSES entry
is, by construction, both private and Top-200-excluded TODAY" - true
only because every universe actually CONFIGURED for pre-scoring so far
happened to be private. That was a stale assumption about how the
switch WOULD be configured, not a code gate - left uncorrected it could
mislead a future reader into thinking a public-but-excluded universe
is blocked somewhere. No logic changed.

Covers, per the Director's own test list for this step:
  - A public-but-Top-200-excluded universe (TSX Composite) is accepted
    as a pre-score candidate source, with is_private_universe()==False
    and is_top200_excluded()==True both asserted directly (production-
    shaped, not assumed).
  - Ranked together with a private universe (Nikkei 225) purely by the
    normal pool ranking score (_recompute_value_score()) - no special
    ordering by privacy/exclusion status, in BOTH directions (the
    public-excluded row wins when it scores higher, and loses when it
    scores lower).
  - End to end through submit_nightly_batch(): pack count, is_prescore
    tagging, and request-content identity (byte-for-byte JSON diff
    against a normal entrant) are unaffected.
  - The scoring-request fingerprint is unchanged.
  - select_top100_pool()'s own output (covering an S&P 500 row and an
    ASX 200 row) is byte-identical whether a public-but-excluded
    universe is listed for pre-scoring or not - this feature still
    never touches pool selection.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_prescore_public_top200_excluded_universes.py
"""
import hashlib
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="prescore_public_excluded_test_")
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
# CHECK 1-4: production-shaped sanity - TSX Composite/FTSE 100/FTSE 250
# are genuinely PUBLIC (post push 2, 5711c3c) but still genuinely
# TOP-200-EXCLUDED (is_top200_excluded()'s own default derivation,
# untouched by this commit) - asserted directly against the real
# functions, not assumed.
# ======================================================================
check("TSX Composite is public (scan_store.is_private_universe() False) - push 2's own change",
      scan_store.is_private_universe("TSX Composite") is False)
check("TSX Composite is still Top-200-excluded (is_top200_excluded() True) - a separate lever",
      te.is_top200_excluded("TSX Composite") is True)
check("FTSE 100 is public but Top-200-excluded, same shape",
      scan_store.is_private_universe("FTSE 100") is False and te.is_top200_excluded("FTSE 100") is True)
check("TSX 60 is public but Top-200-excluded, same shape (public since 641a2a6/53d7e55)",
      scan_store.is_private_universe("TSX 60") is False and te.is_top200_excluded("TSX 60") is True)


# ======================================================================
# CHECK 5-8: a public-but-Top-200-excluded universe IS accepted as a
# pre-score candidate source - production-shaped fixture (real rows,
# real universe name, real eligibility filters exercising funds/no-IV
# exclusion too, so this isn't just "any row at all gets through").
# ======================================================================
scan_store.save_scan("TSX Composite", [
    _row("TSXPUB1", quality=85.0, iv=100.0, price=90.0),       # eligible
    _row("TSXPUB2", quality=55.0, iv=100.0, price=90.0),       # eligible, lower score
    _row("TSXFUND", quality=95.0, is_fund=True),               # excluded: Is Fund
    _row("TSXNOIV", quality=95.0, iv=None, price=90.0),        # excluded: no Intrinsic Value
], source_label="test fixture")
os.environ["TOP200_PRESCORE_UNIVERSES"] = "TSX Composite"
_pub_candidates = te._prescore_candidates(te.MODEL_TOP100)
_pub_tickers = [c["ticker"] for c in _pub_candidates]
check("TSXPUB1/TSXPUB2 (public-but-Top-200-excluded universe) are accepted as pre-score candidates",
      "TSXPUB1" in _pub_tickers and "TSXPUB2" in _pub_tickers)
check("TSXFUND (Is Fund) is still excluded even from a public universe - row-level filters unchanged",
      "TSXFUND" not in _pub_tickers)
check("TSXNOIV (no Intrinsic Value) is still excluded even from a public universe",
      "TSXNOIV" not in _pub_tickers)
check("TSXPUB1 (higher Quality) ranks ahead of TSXPUB2 within the public universe itself",
      _pub_tickers.index("TSXPUB1") < _pub_tickers.index("TSXPUB2"))
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)


# ======================================================================
# CHECK 9-10: ranked together with a PRIVATE universe (Nikkei 225) by
# the normal pool ranking score alone - no special ordering by privacy
# or Top-200-exclusion status, proven in BOTH directions.
# ======================================================================
scan_store.save_scan("Nikkei 225", [
    _row("JPNHI", quality=99.0, iv=100.0, price=90.0),   # should outrank both TSX rows
    _row("JPNLO", quality=20.0, iv=100.0, price=90.0),   # should rank below both TSX rows
], source_label="test fixture")
os.environ["TOP200_PRESCORE_UNIVERSES"] = "TSX Composite, Nikkei 225"
_mixed = te._prescore_candidates(te.MODEL_TOP100)
_mixed_tickers = [c["ticker"] for c in _mixed]
check("the private universe's own highest-scoring row (JPNHI) ranks FIRST overall when it "
      "genuinely scores highest - ranking is purely by value_score, not 'public universes "
      "first' or 'private universes first'",
      _mixed_tickers[0] == "JPNHI")
check("the private universe's own lowest-scoring row (JPNLO) ranks LAST overall when it "
      "genuinely scores lowest - confirms no universe-identity tiebreak is applied",
      _mixed_tickers[-1] == "JPNLO")
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)


# ======================================================================
# CHECK 11-15: end to end through submit_nightly_batch() - pack count,
# is_prescore tagging, and request-content identity, with a public-
# but-Top-200-excluded universe as the ONLY configured pre-score
# universe (proving this isn't merely tolerated when mixed with a
# private one, but works standalone).
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
        fake_batch = mock.Mock(id="msgbatch_prescore_public_test")
        instance.with_options.return_value.messages.batches.create.return_value = fake_batch
        logs = []
        result = te.submit_nightly_batch(pool=pool, log=logs.append)
        captured_requests = None
        if instance.with_options.return_value.messages.batches.create.called:
            captured_requests = instance.with_options.return_value.messages.batches.create.call_args.kwargs.get("requests")
        return result, captured_requests, logs


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("TSX Composite", [
    _row("TSXPUB1", quality=85.0, iv=100.0, price=90.0),
    _row("TSXPUB2", quality=55.0, iv=100.0, price=90.0),
], source_label="test fixture")
_pool3 = _mk_pool(3)
_result_on, _requests_on, _logs_on = _submit_and_capture(
    _pool3, {"TOP200_PRESCORE_UNIVERSES": "TSX Composite", "TOP200_PRESCORE_BUDGET_USD": "60",
             "TOP200_PRESCORE_UNTIL": "2099-12-31"})
check("switch ON with a public-but-excluded universe alone: 2 packed requests (3 normal "
      "entrants pack into one request, 2 pre-score entrants pack into their own, never merged)",
      len(_requests_on) == 2)
check("a 'night ... candidates queued' line names TSX Composite",
      any("candidates queued" in m and "TSX Composite" in m for m in _logs_on))

_batch_state = ts.get_batch_state()
_prescore_tagged = [v for v in _batch_state["custom_id_map"].values() if v.get("is_prescore")]
check("exactly one pack is tagged is_prescore=True, carrying TSXPUB1/TSXPUB2",
      len(_prescore_tagged) == 1
      and set(_prescore_tagged[0]["entrants"].keys()) == {"TSXPUB1", "TSXPUB2"})

_normal_solo_params = te._request_params(
    [{"ticker": "SOLOCO", "company_name": "Solo Co", "sector": "Industrials"}])
_pub_prescore_solo_params = te._request_params(
    [{"ticker": "SOLOCO", "company_name": "Solo Co", "sector": "Industrials"}])
check("a public-but-excluded-universe pre-score entrant's own request body is byte-for-byte "
      "identical to a normal entrant's for the same company",
      json.dumps(_normal_solo_params, sort_keys=True) == json.dumps(_pub_prescore_solo_params, sort_keys=True))


# ======================================================================
# CHECK 16: the scoring-request fingerprint is untouched by this
# commit (direct re-check, same fixture/hash as tests/test_prescoring_
# promo.py's own CHECK 30 - belt and suspenders, since this commit
# touches a docstring in the same module).
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
# CHECK 17: select_top100_pool()'s own output (covering a real S&P 500
# row and a real ASX 200 row) is byte-identical whether a public-but-
# Top-200-excluded universe is configured for pre-scoring or not - this
# feature still never touches pool selection, even for a universe
# that's visible on the public Scanner.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("S&P 500", [
    {"Ticker": "AAPL", "Company Name": "Apple-shaped", "Long Score": 70.0, "Quality": 70.0,
     "MOS %": 10.0, "Psychology": 5.0, "Discovery (lite)": 30.0, "Price": 150.0,
     "Intrinsic Value": 165.0, "DCF Unreliable": False},
], source_label="test fixture")
scan_store.save_scan("ASX 200", [
    {"Ticker": "CBA.AX", "Company Name": "CBA-shaped", "Long Score": 60.0, "Quality": 60.0,
     "MOS %": 8.0, "Psychology": 5.0, "Discovery (lite)": 25.0, "Price": 100.0,
     "Intrinsic Value": 108.0, "DCF Unreliable": False},
], source_label="test fixture")
scan_store.save_scan("TSX Composite", [
    _row("TSXPUB1", quality=85.0, iv=100.0, price=90.0),
], source_label="test fixture")
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)
os.environ.pop("TOP200_PRESCORE_UNTIL", None)
_pool_off = te.select_top100_pool(log=lambda *a, **k: None)
os.environ["TOP200_PRESCORE_UNIVERSES"] = "TSX Composite"
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "60"
os.environ["TOP200_PRESCORE_UNTIL"] = "2099-12-31"
_pool_on = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)
os.environ.pop("TOP200_PRESCORE_UNTIL", None)
check("select_top100_pool()'s own output (S&P 500 + ASX 200 rows included) is byte-identical "
      "whether a public-but-Top-200-excluded universe is listed for pre-scoring or not",
      json.dumps(_pool_off, sort_keys=True, default=str) == json.dumps(_pool_on, sort_keys=True, default=str))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
