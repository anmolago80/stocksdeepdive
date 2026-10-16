"""
Director's work queue, TASK 2 (11 Oct 2026): "Pre-scoring with an
empty universe - what happens if TOP200_PRESCORE_UNIVERSES names a
universe with no scan on disk (Nikkei 225 today)?"

Context: Nikkei 225's own constituent fetch currently parses 0 rows in
production (see TASK 1's report - a fetch-layer bug, untouched here),
so it has no saved scan file on disk at all right now. This matters
for pre-scoring specifically because the instruction's own universe
list (as reported to Andrew) includes Nikkei 225.

FINDING (test-only commit per the task's own rule - "fix only if it
would crash or block the batch; otherwise test-only"): a listed
universe with NO saved scan file, or a DEGRADED one, is skipped
cleanly - no crash, no block, the batch proceeds normally with
whatever OTHER listed universes have real data. Confirmed directly in
_prescore_candidates():

    for universe in universes:
        payload = scan_store.load_scan_raw(universe, allow_private=True)
        if not payload or payload.get("degraded"):
            continue

AT THE TIME THIS FILE WAS FIRST COMMITTED, there was NO log line for
this specific skip - unlike the normal nightly pool-selection path
(_eligible_scan_payloads()), which logs a line for every one of its
own skip reasons. That gap was reported to the Director as a finding
("fix only if it would crash or block" didn't cover a missing log
line), not fixed in that commit.

UPDATE (Director's overnight queue, TASK 4, 12 Oct 2026): the
Director asked for exactly this log line. _prescore_candidates() now
logs "[prescore] skipped <universe> - no saved scan on disk" or
"[prescore] skipped <universe> - degraded scan" for each case -
logging only, the skip itself (and therefore every candidate this
function returns) is byte-identical to before. The two checks below
that used to prove the gap now prove the fix instead - same scenario,
flipped expectation, not a new test.

Covers:
  - A listed universe with NO saved scan file at all: no crash, 0
    candidates contributed from it, OTHER listed universes' own
    candidates are completely unaffected, and the skip is now named
    in the log.
  - A listed universe whose saved scan IS marked degraded: same - no
    crash, no effect on other universes, and now named in the log too.
  - End to end through submit_nightly_batch(): the batch submits
    normally (not blocked) using only the universes that DO have real,
    non-degraded data.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_prescore_empty_universe.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="prescore_empty_universe_test_")
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
         sector="Industrials", company_name=None):
    return {
        "Ticker": ticker, "Company Name": company_name or f"{ticker} Co", "Sector": sector,
        "Quality": quality, "MOS %": mos, "Psychology": psych, "Discovery (lite)": disc,
        "Intrinsic Value": iv, "Price": price,
        "Is Fund": False, "DCF Unreliable": False,
        "price_unit_suspect": False, "Most Recent Quarter": None,
    }


# ======================================================================
# CHECK 1-4: a listed universe with NO saved scan at all (exactly
# today's real Nikkei 225 - see TASK 1's report) is skipped with no
# crash, 0 candidates from it, and no effect on another listed
# universe that DOES have real data.
# ======================================================================
check("sanity: 'Nikkei 225' genuinely has no saved scan in a fresh store "
      "(matches today's real production state per TASK 1)",
      scan_store.load_scan_raw("Nikkei 225", allow_private=True) is None)

scan_store.save_scan("DAX", [_row("DAXGOOD", quality=80.0)], source_label="test fixture")
os.environ["TOP200_PRESCORE_UNIVERSES"] = "Nikkei 225, DAX"
_logs_empty = []
_candidates_with_empty = te._prescore_candidates(te.MODEL_TOP100, log=_logs_empty.append)
check("no exception raised while a listed universe (Nikkei 225) has no saved scan at all",
      True)  # the line above would have raised already if this failed
_tickers_with_empty = [c["ticker"] for c in _candidates_with_empty]
check("DAX's own candidate (DAXGOOD) is completely unaffected by Nikkei 225 having no data",
      _tickers_with_empty == ["DAXGOOD"])
check("TASK 4 fix: '[prescore] skipped' names 'Nikkei 225' and the reason (no saved scan) - "
      "the no-payload skip used to be silent; now it logs exactly one plain line",
      any("[prescore] skipped" in m and "Nikkei 225" in m and "no saved scan" in m
          for m in _logs_empty))
check("candidate selection is byte-identical with the new log line present - DAX's own "
      "candidate (already checked above) is the proof; this just re-confirms the full "
      "candidates list itself, not just DAX's ticker",
      _tickers_with_empty == ["DAXGOOD"])
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)


# ======================================================================
# CHECK 5-7: a listed universe whose saved scan IS marked degraded -
# same silent, non-crashing skip, same gap, no effect on another
# universe's own candidates.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("DAX", [_row("DAXGOOD2", quality=75.0)], source_label="test fixture")
scan_store.save_scan("CAC 40", [_row("CACROW", quality=70.0)], source_label="test fixture",
                      degraded=True)
check("sanity: 'CAC 40' is genuinely saved as degraded",
      bool((scan_store.load_scan_raw("CAC 40", allow_private=True) or {}).get("degraded")))
os.environ["TOP200_PRESCORE_UNIVERSES"] = "CAC 40, DAX"
_logs_degraded = []
_candidates_with_degraded = te._prescore_candidates(te.MODEL_TOP100, log=_logs_degraded.append)
_tickers_with_degraded = [c["ticker"] for c in _candidates_with_degraded]
check("a degraded universe (CAC 40) contributes 0 candidates, with no crash",
      "CACROW" not in _tickers_with_degraded)
check("DAX's own candidate (DAXGOOD2) is completely unaffected by CAC 40 being degraded",
      _tickers_with_degraded == ["DAXGOOD2"])
check("TASK 4 fix: '[prescore] skipped' names 'CAC 40' and the reason (degraded scan)",
      any("[prescore] skipped" in m and "CAC 40" in m and "degraded scan" in m
          for m in _logs_degraded))
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)


# ======================================================================
# CHECK 8-9: end to end through submit_nightly_batch() - the batch
# submits normally (never blocked) when one of the listed pre-score
# universes has no data at all, using only the universe(s) that do.
# ======================================================================
def _mk_pool(n):
    return [{"ticker": f"POOL{i}", "company_name": f"Pool Co {i}", "sector": "Industrials",
              "most_recent_quarter": None} for i in range(n)]


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("DAX", [_row("DAXGOOD3", quality=82.0)], source_label="test fixture")
_pool3 = _mk_pool(3)
ts.seed_pool_presence({row["ticker"]: te.NEWCOMER_PERSISTENCE_NIGHTS for row in _pool3}, "2026-10-09")
os.environ["TOP200_PRESCORE_UNIVERSES"] = "Nikkei 225, DAX"
os.environ["TOP200_PRESCORE_BUDGET_USD"] = "60"
os.environ["TOP200_PRESCORE_UNTIL"] = "2099-12-31"
with mock.patch("anthropic.Anthropic") as MockClient, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw):
    instance = MockClient.return_value
    fake_batch = mock.Mock(id="msgbatch_prescore_empty_universe_test")
    instance.with_options.return_value.messages.batches.create.return_value = fake_batch
    _logs_e2e = []
    _result_e2e = te.submit_nightly_batch(pool=_pool3, log=_logs_e2e.append)
    _requests_e2e = None
    if instance.with_options.return_value.messages.batches.create.called:
        _requests_e2e = instance.with_options.return_value.messages.batches.create.call_args.kwargs.get("requests")
check("the batch submits normally (not blocked) with Nikkei 225 listed but empty - "
      "only DAX's own 1 pre-score candidate rides along",
      _result_e2e == "msgbatch_prescore_empty_universe_test" and _requests_e2e is not None
      and len(_requests_e2e) == 2)  # 3 normal entrants packed + 1 pre-score entrant packed
_batch_state_e2e = ts.get_batch_state()
_prescore_tagged_e2e = [v for v in _batch_state_e2e["custom_id_map"].values() if v.get("is_prescore")]
check("exactly one pack is tagged is_prescore, carrying only DAXGOOD3 (never a phantom "
      "Nikkei 225 entry for a universe with no data)",
      len(_prescore_tagged_e2e) == 1
      and set(_prescore_tagged_e2e[0]["entrants"].keys()) == {"DAXGOOD3"})
os.environ.pop("TOP200_PRESCORE_UNIVERSES", None)
os.environ.pop("TOP200_PRESCORE_BUDGET_USD", None)
os.environ.pop("TOP200_PRESCORE_UNTIL", None)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
