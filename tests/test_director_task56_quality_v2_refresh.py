"""
TASK 56 (Director-directed, PRIORITY, 13 Nov 2026) - one-off Quality v2
refresh for all stored rows, so the go-live preview is clean. Covers:

  2. scan_store.apply_quality_v2_refresh() persistence (rows replaced,
     generated_at/other scan metadata carried over unchanged, display-
     only fields stripped, round-trip readable via load_scan_raw()).
  2. nightly_scan.run_quality_v2_refresh_pass(): reuses _attach_quality_
     v2() unchanged per row, counts updated/skipped correctly, the
     pop-then-restore pattern never leaves a row worse off than before
     a transient failure, chunked by universe, and the final watchlist
     diagnostic block reports a STALE universe's watchlist ticker
     correctly (the real bug this test file catches - see CHECK 4
     below: the first draft of this function read the post-refresh
     watchlist back through scan_store.find_ticker_row(), which goes
     through load_scan()'s own 72h staleness cutoff on `generated_at` -
     exactly the kind of long-stale universe this whole task exists to
     refresh - so a watchlist ticker sitting in a stale universe would
     have read back as "not found" immediately after this pass had just
     written a brand new Quality V2 value for it. Fixed by capturing
     each watchlist row during the main loop itself, which already
     reads via the freshness-independent load_scan_raw()).
  3. scheduler_engine._queue_quality_v2_refresh_request()/_quality_v2_
     refresh_request_status(): write/read the request, refuse a second
     request while one is already pending - same shape as the existing
     Rescan now pair (test_audit_c3_queued_rescan_abandoned_worker.py).
  2 (admin button). app.py's _is_owner_for_quality_v2_refresh()/
     _handle_quality_v2_refresh_click() fail-closed for a non-owner.
  3. QUALITY_V2_DIAGNOSTIC_WATCHLIST now includes "CTGO" per the
     Director's own refreshed 24-ticker watchlist for this task.
  4. CTGO root cause: quality_v2_engine.py's net_margin input had NO
     plausibility guard at all, unlike roe/roic (TASK 45, clamped/
     filtered to +-100%) and debt_to_equity (TASK 51). A CTGO-shaped
     fixture (tiny revenue, one fiscal year dominated by a large non-
     operating one-off gain the data provider's own "Total Unusual
     Items" row does not tag as unusual - plausible for a junior miner
     recognising a gain on sale of a mineral/royalty interest, which
     not every provider's taxonomy flags as "unusual") reproduces a
     wildly implausible net margin that - with as few as 1-2 usable
     fiscal years - passes straight through _median_of_available() with
     nothing to average it against, inflating the score to the 100
     ceiling even though today's live .info figures (what v1 reads)
     show a currently loss-making business. Fixed with the SAME "treat
     as unavailable, never a clamp" guard (_plausible_margin(),
     NET_MARGIN_PLAUSIBLE_MAX = 1.0, the same ceiling ROE/ROIC already
     use) TASK 45/51 already applied to the other two unguarded inputs.

Run: python3 tests/test_director_task56_quality_v2_refresh.py
"""
import datetime as _dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task56_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import nightly_scan
import quality_v2_engine as qv2
import scan_store
import scheduler_engine as se

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


# ======================================================================
# CHECK 1: QUALITY_V2_DIAGNOSTIC_WATCHLIST now includes CTGO.
# ======================================================================
check("QUALITY_V2_DIAGNOSTIC_WATCHLIST includes CTGO (TASK 56's own "
      "refreshed watchlist)",
      "CTGO" in nightly_scan.QUALITY_V2_DIAGNOSTIC_WATCHLIST)
check("QUALITY_V2_DIAGNOSTIC_WATCHLIST still has all 23 pre-existing tickers",
      {"AAL", "HCA", "MTCH", "CCI", "INSE", "KSPI", "MO", "HPQ", "FICO", "LII",
       "DPZ", "ORC", "NREF", "SOI.L", "CSL.AX", "OCL.AX", "NU", "ADBE", "COKE",
       "INTU", "CPRT", "WDAY", "ZM"} <= nightly_scan.QUALITY_V2_DIAGNOSTIC_WATCHLIST)
print("[task56_check1_watchlist] CTGO added, nothing else removed OK")


# ======================================================================
# CHECK 2: _plausible_margin() - the new guard itself, same shape as
# _plausible_roe().
# ======================================================================
check("a normal margin (12%) passes through unchanged", qv2._plausible_margin(0.12) == 0.12)
check("exactly at the 100% ceiling still passes (inclusive)", qv2._plausible_margin(1.0) == 1.0)
check("just over the 100% ceiling (105%) is None (implausible)",
      qv2._plausible_margin(1.05) is None)
check("a large negative margin (-150%) is also None - magnitude, not just high side",
      qv2._plausible_margin(-1.5) is None)
check("None in -> None out", qv2._plausible_margin(None) is None)
check("a non-numeric value never raises, returns None", qv2._plausible_margin("nope") is None)
print("[task56_check2_plausible_margin] _plausible_margin() excludes any margin whose "
      "magnitude exceeds 100%, in either direction, never raises on bad input OK")


# ======================================================================
# CHECK 3: CTGO-shaped fixture - the actual root-cause reproduction and
# fix, using the same synthetic-bundle monkeypatch harness
# test_director_task45_quality_v2_fixes.py already established.
# ======================================================================
_fy1 = _dt.date(2025, 12, 31)
_fy2 = _dt.date(2024, 12, 31)


def _run_v2_with_fixture(ticker, ni_by_fy, equity_by_fy, revenue_by_fy=None,
                          debt_by_fy=None, ocf_by_fy=None, capex_by_fy=None, info=None):
    revenue_by_fy = revenue_by_fy or {fy: 1000.0 for fy in ni_by_fy}
    debt_by_fy = debt_by_fy or {}
    ocf_by_fy = ocf_by_fy or {}
    capex_by_fy = capex_by_fy or {}
    bundle = {
        "income": "FAKE_INCOME", "balance": "FAKE_BALANCE", "cashflow": "FAKE_CASHFLOW",
        "info": info or {},
    }
    fy_dates = sorted(ni_by_fy.keys(), reverse=True)

    def _fake_normalized_income_for_fy(income, fy):
        return ni_by_fy.get(fy), False

    def _fake_series_by_date(df, field):
        if df == "FAKE_BALANCE" and field == "stockholders_equity":
            return equity_by_fy
        if df == "FAKE_BALANCE" and field == "total_debt":
            return debt_by_fy
        if df == "FAKE_INCOME" and field == "revenue":
            return revenue_by_fy
        if df == "FAKE_CASHFLOW" and field == "operating_cash_flow":
            return ocf_by_fy
        if df == "FAKE_CASHFLOW" and field == "capex":
            return capex_by_fy
        if df == "FAKE_CASHFLOW" and field == "free_cash_flow":
            return {}
        return {}

    with mock.patch.object(qv2.auto_compounder_engine, "normalized_income_for_fy",
                            side_effect=_fake_normalized_income_for_fy), \
         mock.patch.object(qv2.auto_compounder_engine, "series_by_date",
                            side_effect=_fake_series_by_date), \
         mock.patch.object(qv2, "_fy_dates", return_value=fy_dates):
        return qv2.get_quality_score_v2(ticker, bundle, info=info or {})


# CTGO-shaped: a single usable fiscal year, tiny revenue ($2M, a junior
# miner's royalty/option income) dominated by a $60M non-operating gain
# on sale of a mineral interest that the data provider's own normalized-
# income/unusual-items classification does NOT exclude (net income ==
# the raw figure including the gain) - net margin = 60M / 2M = 30.0
# (3000%). Equity/debt kept plain and positive, and the one-off gain
# year's own operating cash flow kept genuinely positive (the asset
# sale plausibly brought in real cash too) so neither roe/roic/debt_to_
# equity NOR the profitability gate's fcf leg can mask or fake the
# margin-specific result either way - isolating the net_margin bug.
_score_ctgo, _meta_ctgo = _run_v2_with_fixture(
    "CTGO",
    ni_by_fy={_fy1: 60.0},
    equity_by_fy={_fy1: 50.0},
    revenue_by_fy={_fy1: 2.0},
    debt_by_fy={_fy1: 5.0},
    ocf_by_fy={_fy1: 65.0},
    capex_by_fy={_fy1: -1.0},
    info={"returnOnEquity": -0.40, "profitMargins": -0.20, "netIncomeToCommon": -1.0,
          "freeCashflow": -0.5, "revenueGrowth": 0.0, "earningsGrowth": 0.0},
)
check("CTGO-shaped fixture: the implausible one-FY net margin (3000%) is excluded, "
      "net_margin falls back to today's live (loss-making) .info profitMargins instead "
      "of the statement-derived outlier",
      "net_margin" in _meta_ctgo["fallback_fields"] and _meta_ctgo["net_margin"] == -0.20)
check("CTGO-shaped fixture: with the guard in place, the score is NOT inflated to the "
      "100 ceiling by the unguarded margin term - it reflects the currently loss-making "
      ".info figures instead (capped by the profitability gate)",
      _score_ctgo <= qv2.PROFIT_GATE_CAP)

# Prove this is a genuine regression test, not a tautology: re-run the
# EXACT same fixture through the net_margin computation with the guard
# bypassed (patching _plausible_margin to a pass-through identity),
# confirming the unguarded path really did used to reach the 100
# ceiling - i.e. the guard is actually load-bearing for this fixture.
with mock.patch.object(qv2, "_plausible_margin", side_effect=lambda v: v):
    _score_ctgo_unguarded, _meta_ctgo_unguarded = _run_v2_with_fixture(
        "CTGO",
        ni_by_fy={_fy1: 60.0},
        equity_by_fy={_fy1: 50.0},
        revenue_by_fy={_fy1: 2.0},
        debt_by_fy={_fy1: 5.0},
        ocf_by_fy={_fy1: 65.0},
        capex_by_fy={_fy1: -1.0},
        info={"returnOnEquity": -0.40, "profitMargins": -0.20, "netIncomeToCommon": -1.0,
              "freeCashflow": -0.5, "revenueGrowth": 0.0, "earningsGrowth": 0.0},
    )
check("CTGO-shaped fixture, GUARD BYPASSED: confirms the bug was real - without "
      "_plausible_margin(), the same inputs hit the 100 ceiling, exactly reproducing "
      "the Director's reported v2=100 symptom",
      _score_ctgo_unguarded == 100)
print(f"[task56_check3_ctgo_root_cause] CTGO-shaped fixture: unguarded score "
      f"{_score_ctgo_unguarded}, guarded score {_score_ctgo} (v1-equivalent, loss-making "
      f"gate capped at {qv2.PROFIT_GATE_CAP}) - net_margin was the one input of the 7 "
      f"with no plausibility guard, now fixed OK")

# A genuinely plausible margin is still used normally - the fix never
# excludes a sane, real-world margin.
_score_good_margin, _meta_good_margin = _run_v2_with_fixture(
    "GOODCO",
    ni_by_fy={_fy1: 25.0, _fy2: 22.0},
    equity_by_fy={_fy1: 100.0, _fy2: 95.0},
    revenue_by_fy={_fy1: 100.0, _fy2: 95.0},
    info={"profitMargins": 0.24},
)
check("a normal, plausible net margin (~24%) is still computed from statements - "
      "never a fallback, never excluded",
      "net_margin" not in _meta_good_margin["fallback_fields"]
      and 0.20 < _meta_good_margin["net_margin"] < 0.28)
print("[task56_check3b_plausible_margin_unaffected] a genuinely plausible margin flows "
      "through completely unaffected by the new guard OK")


# ======================================================================
# CHECK 4: scan_store.apply_quality_v2_refresh() persistence.
# ======================================================================
scan_store.save_scan(
    "Task56 Test Universe",
    [{"Ticker": "AAA", "Quality V2": 10}, {"Ticker": "CTGO", "Quality V2": 100}],
    "test-source", attention_lite=False,
)
_before = scan_store.load_scan_raw("Task56 Test Universe")
_generated_at = _before["generated_at"]

_new_rows = [{"Ticker": "AAA", "Quality V2": 55}, {"Ticker": "CTGO", "Quality V2": 42}]
_result = scan_store.apply_quality_v2_refresh(
    "Task56 Test Universe", _new_rows, updated_count=2, skipped_count=0)
check("apply_quality_v2_refresh() returns the new payload with rows replaced",
      _result is not None and _result["rows"] == _new_rows)
check("apply_quality_v2_refresh() stamps a quality_v2_refreshed_at timestamp",
      "quality_v2_refreshed_at" in _result)
check("apply_quality_v2_refresh() records updated_count/skipped_count",
      _result["quality_v2_refresh_updated_count"] == 2
      and _result["quality_v2_refresh_skipped_count"] == 0)
check("apply_quality_v2_refresh() never touches generated_at (last FULL scan time)",
      _result["generated_at"] == _generated_at)

_after = scan_store.load_scan_raw("Task56 Test Universe")
check("the written rows round-trip back out via load_scan_raw()",
      _after["rows"] == _new_rows)
check("apply_quality_v2_refresh() strips display-only fields (none should be re-added)",
      "generated_at_label" not in _after)

check("apply_quality_v2_refresh() returns None (writes nothing) for a universe with no "
      "prior scan on disk",
      scan_store.apply_quality_v2_refresh("Nonexistent Universe Xyz", []) is None)
print("[task56_check4_scan_store_persistence] apply_quality_v2_refresh() replaces rows, "
      "stamps its own timestamp + counts, never touches generated_at, round-trips "
      "correctly, no-ops for an unknown universe OK")


# ======================================================================
# CHECK 5: nightly_scan.run_quality_v2_refresh_pass() - updated/skipped
# counting, pop-then-restore on failure, chunked-by-universe
# persistence, and the watchlist diagnostic block correctly covering a
# STALE universe (the real bug this test file exists to catch).
# ======================================================================
scan_store.save_scan(
    "Task56 Pass Universe",
    [
        {"Ticker": "GOODTICK", "Quality V2": 11, "Quality": 20},
        {"Ticker": "BADTICK", "Quality V2": 33, "Quality": 40},
        {"Ticker": "CTGO", "Quality V2": 100, "Quality": 30},
        {"Quality V2": 5},  # no Ticker field at all
    ],
    "test-source", attention_lite=False,
)

# Back-date generated_at well past load_scan()'s 72h staleness cutoff,
# proving the watchlist block below is NOT gated by it (the bug this
# pass's first draft had via scan_store.find_ticker_row()).
_stale_payload = scan_store.load_scan_raw("Task56 Pass Universe")
_stale_payload["generated_at"] = (
    _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=10)
).isoformat()
import json as _json
with open(scan_store._path("Task56 Pass Universe"), "w") as _f:
    _json.dump(_stale_payload, _f)
check("sanity: the universe this check uses really is past the 72h staleness cutoff "
      "(load_scan() itself refuses it)",
      scan_store.load_scan("Task56 Pass Universe") is None)


def _fake_attach_quality_v2(row, ticker, log=print):
    if ticker == "BADTICK":
        return  # simulate "no fundamentals bundle available" - leaves row untouched
    row["Quality V2"] = 77 if ticker != "CTGO" else 42
    row["Quality V2 Meta"] = {"roe": 0.1, "roic": 0.1, "net_margin": 0.1,
                               "revenue_growth": 0.0, "earnings_growth": 0.0,
                               "debt_to_equity": 0.0, "fallback_fields": []}


_logged = []
with mock.patch.object(nightly_scan, "_attach_quality_v2", side_effect=_fake_attach_quality_v2):
    _summary = nightly_scan.run_quality_v2_refresh_pass(log=_logged.append)

check("run_quality_v2_refresh_pass() counts GOODTICK and CTGO as updated, BADTICK and "
      "the no-Ticker row as skipped, within this one universe's totals",
      _summary["updated"] >= 2 and _summary["skipped"] >= 2)

_refreshed = scan_store.load_scan_raw("Task56 Pass Universe")
_by_ticker = {r.get("Ticker"): r for r in _refreshed["rows"] if r.get("Ticker")}
check("GOODTICK's Quality V2 was actually updated on disk", _by_ticker["GOODTICK"]["Quality V2"] == 77)
check("BADTICK's PRE-EXISTING Quality V2 (33) is restored, never wiped, on a skip",
      _by_ticker["BADTICK"]["Quality V2"] == 33)
check("CTGO's Quality V2 was updated on disk (100 -> 42)", _by_ticker["CTGO"]["Quality V2"] == 42)

_diag_lines = [ln for ln in _logged if ln.startswith("[quality_v2_diag] CTGO")]
check("the post-refresh [quality_v2_diag] block reports CTGO even though its universe's "
      "generated_at is 10 days stale - not gated by load_scan()'s 72h cutoff (the actual "
      "bug this test catches)",
      len(_diag_lines) == 1 and "not found" not in _diag_lines[0])
check("[quality_v2_refresh] SUMMARY line was logged with the right shape",
      any(ln.startswith("[quality_v2_refresh] SUMMARY:") for ln in _logged))
print("[task56_check5_refresh_pass] updated/skipped counted correctly, a skip never "
      "wipes the pre-existing value, and the watchlist diagnostic block correctly "
      "covers a 10-day-stale universe's CTGO row OK")


# ======================================================================
# CHECK 6: scheduler_engine queue/status pair - same shape as the
# existing Rescan now pair.
# ======================================================================
_clear_state = se._load_state()
_clear_state.pop("quality_v2_refresh_request", None)
_clear_state.pop("quality_v2_refresh_last_run", None)
se._save_state(_clear_state)

ok, reason = se._queue_quality_v2_refresh_request(requested_by="owner@x.com", log=lambda *a: None)
check("a quality v2 refresh request is queued successfully", ok is True and reason is None)
status = se._quality_v2_refresh_request_status()
check("the pending request is readable back with the right requested_by/status",
      status["pending"]["requested_by"] == "owner@x.com" and status["pending"]["status"] == "queued")

ok2, reason2 = se._queue_quality_v2_refresh_request(requested_by="owner@x.com", log=lambda *a: None)
check("a second request is refused while one is already pending",
      ok2 is False and reason2 and "already" in reason2)

_clear_state2 = se._load_state()
_clear_state2.pop("quality_v2_refresh_request", None)
se._save_state(_clear_state2)
print("[task56_check6_scheduler_queue] queue/status pair writes, reads, and refuses a "
      "second concurrent request OK")


# ======================================================================
# CHECK 7: app.py's owner gate fails closed for a non-owner / any
# exception - same pattern as _is_owner_for_rescan().
# ======================================================================
import app as _app


class _FakeAiGateDenies:
    @staticmethod
    def is_owner(email):
        return False


class _FakePaywallEngine:
    @staticmethod
    def current_user_email():
        return "not-the-owner@example.com"


with mock.patch.object(_app, "ai_gate", _FakeAiGateDenies), \
     mock.patch.object(_app, "paywall_engine", _FakePaywallEngine):
    check("_is_owner_for_quality_v2_refresh() is False for a non-owner",
          _app._is_owner_for_quality_v2_refresh() is False)

with mock.patch.object(_app, "ai_gate") as _mock_gate:
    _mock_gate.is_owner.side_effect = Exception("boom")
    check("_is_owner_for_quality_v2_refresh() fails CLOSED (False) on any exception, "
          "never the other way around",
          _app._is_owner_for_quality_v2_refresh() is False)
print("[task56_check7_app_owner_gate] the admin button's owner check fails closed for a "
      "non-owner and on any exception OK")


print(f"\n{passed} passed, {failed} failed")
if failed:
    sys.exit(1)
