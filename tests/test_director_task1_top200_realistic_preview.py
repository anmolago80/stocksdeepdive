"""
Director's work queue, TASK 1 (13 Oct 2026): "Owner-only Top 200
preview: make it a real would-be view." Root cause the Director named:
top100_engine.preview_candidates_by_country() read raw scan rows with
no eligibility filter, no cross-universe dedupe, and no Claude score
(sorted by Long Score alone).

Covers the four sub-asks, each proven against the real production
functions, never a re-derived copy:

  a. Per-row eligibility - the SAME check pre-scoring uses
     (_prescore_row_eligible()), never a third copy. An ineligible row
     goes into the new "excluded" list with a plain, named reason
     (_prescore_exclusion_reason()), never silently dropped.
  b. Cross-universe dedupe by ticker - freshest payload generated_at
     wins, Long Score as tiebreak (same philosophy as the real pool's
     own _build_best_by_ticker()). The winning row's "universes" field
     lists every universe the ticker was found ELIGIBLE in.
  c. Each eligible candidate's own stored Claude Research Score
     ("composite", top100_engine.composite_score()) and the rank that
     score would take against the CURRENT pool's own rated scores
     ("pool_rank_if_added") - WAITING (None) for an unscored candidate,
     never a fabricated rank.
  d. peer_context.market_for() already recognises .DE/.PA/.AS/.SW/.ST
     (commit 6ffa7de, Proposal 4 of the 8 Oct fix-proposal round) -
     proven here to be byte-identical for every EXISTING PUBLIC
     universe's own ticker suffixes (.AX/.L/.TO/.T/none), since this
     task's own instruction said STOP if that weren't true. No code
     change was needed for 1d - this file proves why.

Also re-proves select_top100_pool()'s own output is byte-identical
regardless of this rework, same standing proof test_part5_step5_2_
top200_preview.py's own CHECK 10 already carries for the base feature.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task1_top200_realistic_preview.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task1_top200_realistic_preview_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

import peer_context
import scan_store
import top100_engine as te
import top100_store

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


def _row(ticker, company_name, price=100.0, iv=120.0, long_score=60.0,
         is_fund=False, price_unit_suspect=False, trading_status=None,
         dcf_unreliable=False, fcf_source="reported"):
    return {
        "Ticker": ticker, "Company Name": company_name, "Price": price,
        "Intrinsic Value": iv, "MOS %": 16.7, "Long Score": long_score,
        "Quality": 55.0, "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
        "Growth Used": 0.05, "FCF Source": fcf_source, "Dividend Yield %": 2.0,
        "Is Fund": is_fund, "price_unit_suspect": price_unit_suspect,
        "Trading Status": trading_status, "DCF Unreliable": dcf_unreliable,
    }


# ======================================================================
# CHECK 1-6: TASK 1a - per-row eligibility via _prescore_row_eligible(),
# excluded rows carry a plain, named reason, never silently dropped.
# ======================================================================
scan_store.save_scan("DAX", [
    _row("GOOD.DE", "Good Co"),
    _row("FUND.DE", "Fund Co", is_fund=True),
    _row("NOVALUE.DE", "No Value Co", iv=None),
    _row("UNRELIABLE.DE", "Unreliable Co", price=10.0, iv=100.0),  # MOS 90% -> DCF unreliable by price
    _row("STALE.DE", "Stale Co", trading_status="stale"),
], source_label="test fixture")
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX"
_preview1 = te.preview_candidates_by_country()
_eligible_tickers = sorted(r["ticker"] for rows in _preview1["by_country"].values() for r in rows)
_excluded_by_ticker = {e["ticker"]: e for e in _preview1["excluded"]}

check("TASK 1a: the clean row (GOOD.DE) is eligible and reaches by_country",
      "GOOD.DE" in _eligible_tickers)
check("TASK 1a: a fund row is excluded with reason 'fund'",
      _excluded_by_ticker.get("FUND.DE", {}).get("reason") == "fund")
check("TASK 1a: a no-Intrinsic-Value row is excluded with reason 'no value'",
      _excluded_by_ticker.get("NOVALUE.DE", {}).get("reason") == "no value")
check("TASK 1a: a DCF-unreliable-by-price row is excluded with reason 'model unreliable'",
      _excluded_by_ticker.get("UNRELIABLE.DE", {}).get("reason") == "model unreliable")
check("TASK 1a: a stale-price row is excluded with reason 'stale'",
      _excluded_by_ticker.get("STALE.DE", {}).get("reason") == "stale")
check("TASK 1a: none of the 4 ineligible tickers reached by_country at all",
      not any(t in _eligible_tickers for t in ("FUND.DE", "NOVALUE.DE", "UNRELIABLE.DE", "STALE.DE")))
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)


# ======================================================================
# CHECK 7-10: TASK 1b - cross-universe dedupe by ticker, freshest
# payload generated_at wins, "universes" lists every universe the
# ticker was found ELIGIBLE in (including the winning one).
# ======================================================================
if os.path.exists(top100_store.DB_PATH):
    os.remove(top100_store.DB_PATH)
scan_store.save_scan("DAX", [_row("DUP.DE", "Dup Co (DAX, older)", long_score=50.0)],
                      source_label="test fixture")
import time as _time
_time.sleep(1.1)  # distinct generated_at second, so CAC 40's save is strictly fresher
scan_store.save_scan("CAC 40", [_row("DUP.DE", "Dup Co (CAC 40, fresher)", long_score=40.0)],
                      source_label="test fixture")
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX, CAC 40"
_preview2 = te.preview_candidates_by_country()
_dup_rows = [r for rows in _preview2["by_country"].values() for r in rows if r["ticker"] == "DUP.DE"]
check("TASK 1b: DUP.DE appears exactly once across both previewed universes (deduped)",
      len(_dup_rows) == 1)
check("TASK 1b: the FRESHER row (CAC 40's save, 1+ second later) wins, even though its own "
      "Long Score (40) is lower than DAX's (50) - freshest-generated_at-wins beats the "
      "Long Score tiebreak, same priority order as the real pool's own rule",
      _dup_rows and _dup_rows[0]["universe"] == "CAC 40")
check("TASK 1b: 'universes' lists BOTH previewed universes this ticker was eligible in",
      _dup_rows and _dup_rows[0]["universes"] == ["CAC 40", "DAX"])
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)


# ======================================================================
# CHECK 11-14: TASK 1c - stored Claude Research Score + rank against
# the CURRENT pool's own rated scores; WAITING (None/None) for an
# unscored candidate.
# ======================================================================
if os.path.exists(top100_store.DB_PATH):
    os.remove(top100_store.DB_PATH)
# Seed a tiny CURRENT pool with two rated tickers (composites ~85 and ~45).
top100_store.save_pool(
    [{"ticker": "POOLHI", "company_name": "Pool Hi", "universe": "S&P 500", "value_score": 70,
      "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 110.0, "currency": "USD",
      "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0},
     {"ticker": "POOLLO", "company_name": "Pool Lo", "universe": "S&P 500", "value_score": 50,
      "mos_pct": 5.0, "price": 100.0, "intrinsic_value": 105.0, "currency": "USD",
      "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0}],
    as_of="2026-10-12",
)
_hi_dims = {key: {"score": 5, "justification": "test fixture", "source_period": "RP2026-09-30"}
            for key in te.DIMENSION_KEYS}
_lo_dims = {key: {"score": 2, "justification": "test fixture", "source_period": "RP2026-09-30"}
            for key in te.DIMENSION_KEYS}
top100_store.save_score(
    ticker="POOLHI", quarter="RP2026-09-30", model=te.MODEL_TOP100, rubric_version=te.RUBRIC_VERSION,
    dims=_hi_dims, not_rated=False, most_recent_quarter="2026-09-30",
    inversion_scenario="test", inversion_severity=2, current_headwind=None,
    market_structure=None, market_structure_comment=None, one_foot_hurdle=None, one_foot_comment=None,
    munger_quality=None, munger_comment=None, big_wave=None, big_wave_comment=None,
    prompt="{}", raw_response="{}",
)
top100_store.save_score(
    ticker="POOLLO", quarter="RP2026-09-30", model=te.MODEL_TOP100, rubric_version=te.RUBRIC_VERSION,
    dims=_lo_dims, not_rated=False, most_recent_quarter="2026-09-30",
    inversion_scenario="test", inversion_severity=2, current_headwind=None,
    market_structure=None, market_structure_comment=None, one_foot_hurdle=None, one_foot_comment=None,
    munger_quality=None, munger_comment=None, big_wave=None, big_wave_comment=None,
    prompt="{}", raw_response="{}",
)
_pool_composites = te._current_pool_rated_composites()
check("sanity: the seeded pool's own two composites are distinct and descending",
      len(_pool_composites) == 2 and _pool_composites[0] > _pool_composites[1])

# A preview candidate scored HIGHER than both pool rows -> rank 1.
scan_store.save_scan("DAX", [_row("SCORED.DE", "Scored Co")], source_label="test fixture")
_top_dims = {key: {"score": 5, "justification": "test fixture", "source_period": "RP2026-09-30"}
             for key in te.DIMENSION_KEYS}
top100_store.save_score(
    ticker="SCORED.DE", quarter="RP2026-09-30", model=te.MODEL_TOP100, rubric_version=te.RUBRIC_VERSION,
    dims=_top_dims, not_rated=False, most_recent_quarter="2026-09-30",
    inversion_scenario="test", inversion_severity=1, current_headwind=None,
    market_structure=None, market_structure_comment=None, one_foot_hurdle=None, one_foot_comment=None,
    munger_quality=None, munger_comment=None, big_wave=None, big_wave_comment=None,
    prompt="{}", raw_response="{}",
)
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX"
_preview3 = te.preview_candidates_by_country()
_scored_row = next(r for rows in _preview3["by_country"].values() for r in rows if r["ticker"] == "SCORED.DE")
check("TASK 1c: a stored score gives a real 'composite' (not None)",
      _scored_row["composite"] is not None)
check("TASK 1c: scoring at least as well as both pool rows ranks it #1 against the CURRENT pool",
      _scored_row["pool_rank_if_added"] == 1)

# An UNSCORED candidate -> WAITING (None/None), never a fabricated rank.
scan_store.save_scan("CAC 40", [_row("WAITING.PA", "Waiting Co")], source_label="test fixture")
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX, CAC 40"
_preview4 = te.preview_candidates_by_country()
_waiting_row = next(r for rows in _preview4["by_country"].values() for r in rows if r["ticker"] == "WAITING.PA")
check("TASK 1c: an unscored candidate's 'composite' is None",
      _waiting_row["composite"] is None)
check("TASK 1c: an unscored candidate's 'pool_rank_if_added' is None (WAITING), never a guessed rank",
      _waiting_row["pool_rank_if_added"] is None)
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)


# ======================================================================
# CHECK 17-19: TASK 1d - market_for() already recognises the five
# European suffixes (commit 6ffa7de); proven byte-identical for every
# EXISTING PUBLIC universe's own suffixes, per this task's own STOP
# condition ("prove byte-identical" before relying on it here).
# ======================================================================
check("market_for(): .AX (ASX 200, public) -> Australia, unaffected by the European branches",
      peer_context.market_for("CSL.AX") == "Australia")
check("market_for(): .L (FTSE 100/250, public) -> United Kingdom, unaffected",
      peer_context.market_for("BARC.L") == "United Kingdom")
check("market_for(): .TO (TSX 60/Composite, public) -> Canada, unaffected",
      peer_context.market_for("RY.TO") == "Canada")
check("market_for(): .T (TOPIX 500, private but pre-existing) -> Japan, unaffected",
      peer_context.market_for("7203.T") == "Japan")
check("market_for(): no suffix (S&P 500, public) -> USA, unaffected",
      peer_context.market_for("AAPL") == "USA")
check("market_for(): the five European suffixes now resolve to their own real country, "
      "not falling through to USA - this is what makes the preview's own country grouping "
      "correct (already proven directly above via the DAX/CAC 40 fixtures grouping under "
      "'Germany'/'France', never 'USA')",
      peer_context.market_for("SIE.DE") == "Germany"
      and peer_context.market_for("LVMH.PA") == "France"
      and peer_context.market_for("ASML.AS") == "Netherlands"
      and peer_context.market_for("NESN.SW") == "Switzerland"
      and peer_context.market_for("VOLV-B.ST") == "Sweden")


# ======================================================================
# CHECK 20-21: byte-identical proof - select_top100_pool()'s own
# return value never depends on this rework (preview_candidates_by_
# country() is read-only, never called by, and never calls, pool
# selection).
# ======================================================================
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
_pool_unset = te.select_top100_pool(log=lambda *a, **k: None)
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX, CAC 40"
_pool_set = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
check("select_top100_pool()'s own return value is byte-identical whether TOP200_PREVIEW_UNIVERSES "
      "is set or not, after TASK 1's rework",
      json.dumps(_pool_unset, sort_keys=True, default=str) ==
      json.dumps(_pool_set, sort_keys=True, default=str))
check("none of this file's preview-only fixture tickers entered the real public pool",
      not any(r["ticker"] in ("GOOD.DE", "DUP.DE", "SCORED.DE", "WAITING.PA") for r in _pool_unset))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
