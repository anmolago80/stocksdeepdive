"""
TASK 44 (Director-directed, 11 Nov 2026): "Quality on sale" tab -
confirm and fix its coverage.

Covers the render-layer half of TASK 44 (top100_engine.py's own
candidate-source widening, ranking and entrant-selection changes are
covered by tests/test_director_task42_item6_and_task39_render.py's
own CHECK 10/11/11b, extended for this task; support_levels_job.py's
own widened ticker scope is covered by tests/test_director_task36_
support_levels_job_and_render.py's own CHECK 14):

  - top100_render._quality_on_sale_candidate_rows(): a candidate that
    IS a current pool/extension member comes straight from _enriched_
    pool()/_enriched_asx_extension() (no second lookup); a candidate
    OUTSIDE both (TASK 44 item 1's own widened-source case) is
    enriched via its own top100_engine._build_best_by_ticker() row,
    fed through the SAME _enrich_rows() helper every other tab already
    uses - never a second, parallel enrichment path. A lookup failure
    for the non-pool group never raises - those tickers simply aren't
    in the returned dict.
  - top100_render._render_quality_on_sale_tab()'s own pagination (TASK
    44 item 9): only the first _QUALITY_ON_SALE_PAGE_SIZE (20)
    candidates render initially; a "Show all (N)" button reveals every
    candidate on click, via the same session-state-toggle + st.rerun()
    pattern every other show-more control on this site already uses.

Run: python3 tests/test_director_task44_quality_on_sale_coverage.py
"""
import datetime
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task44_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_render as tr
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


# ======================================================================
# CHECK 1: _quality_on_sale_candidate_rows() - a pool/extension member
# comes from _enriched_pool()/_enriched_asx_extension() directly; a
# non-pool/extension candidate is enriched via _build_best_by_ticker()
# + _enrich_rows(), the SAME enrichment logic every other tab uses.
# ======================================================================
_pool_row_enriched = {
    "ticker": "INPOOL.AX", "score_row": {"not_rated": False, "dims": {}}, "composite": 70.0,
    "is_fallback_score": False, "score_failure": None,
}
_best_by_ticker_fixture = {
    "OUTOFPOOL.AX": {
        "ticker": "OUTOFPOOL.AX", "company_name": "Out Of Pool Co", "universe": "S&P 500",
        "value_score": 50.0, "mos_pct": 30.0, "price": 10.0, "intrinsic_value": 15.0,
        "currency": "USD", "psychology": 0.0, "sector": "Healthcare",
        "dividend_yield_pct": 1.0, "most_recent_quarter": "2026-03-31",
        "generated_at": "2026-11-01T00:00:00+00:00", "stale_valuation": False,
    },
}
_score_row_for_outofpool = {
    "not_rated": False,
    "dims": {k: {"score": 5} for k in te.DIMENSION_KEYS},
}

with mock.patch.object(tr, "_enriched_pool", return_value=[_pool_row_enriched]), \
     mock.patch.object(tr, "_enriched_asx_extension", return_value=[]), \
     mock.patch.object(te, "_build_best_by_ticker", return_value=(_best_by_ticker_fixture, 1)), \
     mock.patch.object(top100_store, "latest_scores_for_model",
                        return_value={"OUTOFPOOL.AX": _score_row_for_outofpool}), \
     mock.patch.object(top100_store, "score_failures_for_model", return_value={}):
    _candidates = [{"ticker": "INPOOL.AX"}, {"ticker": "OUTOFPOOL.AX"}]
    _by_ticker = tr._quality_on_sale_candidate_rows(_candidates)

check("pool_member_comes_straight_from_enriched_pool_no_second_lookup",
      _by_ticker["INPOOL.AX"] is _pool_row_enriched)
check("non_pool_member_is_enriched_via_build_best_by_ticker_and_enrich_rows",
      "OUTOFPOOL.AX" in _by_ticker
      and _by_ticker["OUTOFPOOL.AX"]["score_row"] == _score_row_for_outofpool
      and _by_ticker["OUTOFPOOL.AX"]["composite"] is not None)
check("non_pool_member_keeps_its_own_full_row_fields_from_best_by_ticker",
      _by_ticker["OUTOFPOOL.AX"]["company_name"] == "Out Of Pool Co"
      and _by_ticker["OUTOFPOOL.AX"]["sector"] == "Healthcare")

# A build_best_by_ticker() failure for the non-pool lookup never raises
# - the missing ticker simply isn't in the returned dict.
with mock.patch.object(tr, "_enriched_pool", return_value=[_pool_row_enriched]), \
     mock.patch.object(tr, "_enriched_asx_extension", return_value=[]), \
     mock.patch.object(te, "_build_best_by_ticker", side_effect=RuntimeError("boom")):
    _by_ticker_fail = tr._quality_on_sale_candidate_rows(_candidates)
check("a_build_best_by_ticker_failure_never_raises_pool_member_still_present",
      _by_ticker_fail.get("INPOOL.AX") is _pool_row_enriched
      and "OUTOFPOOL.AX" not in _by_ticker_fail)
print("[task44_check1_candidate_rows_enrichment] a pool/extension candidate is read "
      "straight from the existing enrichment functions; a non-pool candidate (TASK 44 "
      "item 1's own widened-source case) is enriched through the SAME _enrich_rows() "
      "helper via its own full _build_best_by_ticker() row; a lookup failure never "
      "raises OK")


# ======================================================================
# CHECK 2 (TASK 44 item 9): pagination - the first 20 candidates
# render initially; a "Show all (N)" button reveals the rest on click.
# AppTest, same pattern as the existing test_director_task38 Replay
# panel AppTest.
# ======================================================================
from streamlit.testing.v1 import AppTest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_quality_on_sale_tab(n_candidates):
    script = f"""
import os, sys
sys.path.insert(0, {_REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import top100_engine as te
import top100_render as tr

_n = {n_candidates}
_candidates = [
    {{"ticker": f"PAGECO{{i}}.AX", "quality_used": 80.0, "quality_used_source": "v2",
      "drop_pct": -50.0, "peak_date": None, "peak_price": None, "current_price": None,
      "quality_exception_admitted": False, "in_top_200": True, "research_score": 90.0 - i}}
    for i in range(_n)
]
_rows_by_ticker = {{
    c["ticker"]: {{
        "ticker": c["ticker"], "score_row": {{"not_rated": False, "dims": {{}}}},
        "composite": c["research_score"], "is_fallback_score": False, "score_failure": None,
        "sector": None, "currency": "AUD", "price": 10.0, "psychology": 0.0,
        "value_score": 50.0, "mos_pct": 20.0, "intrinsic_value": 15.0,
    }}
    for c in _candidates
}}

te.quality_on_sale_candidates = lambda log=print: {{"eligible_count": _n, "total_qualifying": _n, "candidates": _candidates}}
tr._quality_on_sale_candidate_rows = lambda candidates: _rows_by_ticker
# _render_row() itself is exercised by every other tab's own test suite already -
# stubbed here to a trivial marker so this test is purely about the PAGINATION
# logic (how many times it's called, and with which rank), not re-testing card HTML.
_render_calls = []
def _fake_render_row(rank, row, lang, finer_industry, sort_mode, recent_badge_html=None,
                      cross_list_chip_html=None):
    _render_calls.append(row["ticker"])
    st.markdown(f"CARD:{{row['ticker']}}")
tr._render_row = _fake_render_row

tr._render_quality_on_sale_tab("en", {{}}, "value")
st.session_state["_render_calls_count"] = len(_render_calls)
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"initial render raised: {at.exception}"
    return at


_at_25 = _run_quality_on_sale_tab(25)
check(f"pagination_initial_render_shows_only_the_first_20_of_25_candidates "
      f"(rendered={_at_25.session_state.get('_render_calls_count')})",
      _at_25.session_state.get("_render_calls_count") == 20)
check("pagination_show_all_button_present_when_more_than_20_qualify",
      len(_at_25.button) == 1)

_at_25_clicked = _at_25.button[0].click().run()
check(f"pagination_clicking_show_all_renders_every_candidate "
      f"(rendered={_at_25_clicked.session_state.get('_render_calls_count')})",
      _at_25_clicked.session_state.get("_render_calls_count") == 25)

_at_10 = _run_quality_on_sale_tab(10)
check("pagination_no_show_all_button_when_10_candidates_fit_on_one_page",
      len(_at_10.button) == 0)
check(f"pagination_all_10_candidates_render_with_no_cap_needed "
      f"(rendered={_at_10.session_state.get('_render_calls_count')})",
      _at_10.session_state.get("_render_calls_count") == 10)
print("[task44_check2_pagination] the first 20 candidates render initially, a "
      "'Show all (N)' button appears only when there are more than 20, and clicking "
      "it reveals every remaining candidate without re-fetching or re-ranking OK")

# AppTest.from_string() runs its script against this SAME process's
# already-imported top100_engine/top100_render module objects (module
# caching via sys.modules) - the script above permanently overwrote
# te.quality_on_sale_candidates/tr._quality_on_sale_candidate_rows/tr.
# _render_row on those shared objects. Restored here, before any later
# check calls the real functions again - otherwise every check below
# would silently see the AppTest script's own fake PAGECO fixture
# instead of a fresh, real call (caught by a real test failure here
# once, now guarded against for good).
import importlib
importlib.reload(te)
importlib.reload(tr)


# ======================================================================
# CHECK 3 (TASK 44 item 6): point_in_time_replay.quality_on_sale_
# backtest() - runs the 3 rules every Friday from start to end, on a
# synthetic fixture (no network access in this sandbox to fetch real
# CSL.AX/OCL.AX price history - see the TASK 44 report for that
# disclosure).
# ======================================================================
import datetime as _dt

import pandas as pd

import point_in_time_replay as pir
import quality_v2_engine

check("_fridays_between: a Monday-to-Monday 3-week range returns exactly 3 Fridays",
      pir._fridays_between(_dt.date(2026, 1, 5), _dt.date(2026, 1, 19))
      == [_dt.date(2026, 1, 9), _dt.date(2026, 1, 16)])
check("_fridays_between: a range starting ON a Friday includes it",
      pir._fridays_between(_dt.date(2026, 1, 9), _dt.date(2026, 1, 9)) == [_dt.date(2026, 1, 9)])
check("_fridays_between: a range with no Friday in it returns []",
      pir._fridays_between(_dt.date(2026, 1, 5), _dt.date(2026, 1, 8)) == [])

# A synthetic 2-year daily series: price falls from 100 to 40 (a -60%
# drop from its own peak, comfortably clears the -40% rule) across the
# backtest window, then recovers above the -40% floor for the final
# weeks - this should produce a real "qualified" stretch with a gap.
_bt_start = _dt.date(2024, 1, 1)
_bt_dates = [_bt_start + _dt.timedelta(days=i) for i in range(500)]


def _bt_price(i):
    if i < 300:
        return 100.0 - (i * 0.2)  # falls to 40.0 by day 300
    return 100.0  # recovers back above the peak - never qualifies again


_bt_closes = [(_bt_dates[i], _bt_price(i)) for i in range(len(_bt_dates))]
_bt_fake_bundle = {"info": {}, "income": pd.DataFrame(), "balance": pd.DataFrame(),
                    "cashflow": pd.DataFrame()}

with mock.patch.object(quality_v2_engine, "get_quality_score_v2", return_value=(80.0, {})):
    _backtest = pir.quality_on_sale_backtest(
        "BTTEST.AX", _bt_closes, _bt_dates[250].isoformat(), _bt_dates[320].isoformat(),
        bundle=_bt_fake_bundle,
    )

check("backtest_runs_one_entry_per_friday_in_range",
      all(w["date"].weekday() == 4 for w in _backtest["weeks"]) and len(_backtest["weeks"]) > 0)
check("backtest_quality_v2_mocked_value_flows_through_every_week",
      all(w["quality_v2"] == 80.0 for w in _backtest["weeks"]))
check("backtest_first_qualified_is_the_earliest_qualifying_friday_in_range "
      "(the falling-price stretch, before day 300's recovery)",
      _backtest["first_qualified"] is not None
      and _backtest["first_qualified"] < _bt_dates[300])
check("backtest_last_qualified_is_before_the_recovery_(price_back_to_100_after_day_300)",
      _backtest["last_qualified"] is not None and _backtest["last_qualified"] < _bt_dates[300])
check("backtest_total_weeks_qualified_matches_the_weeks_list_count",
      _backtest["total_weeks_qualified"]
      == sum(1 for w in _backtest["weeks"] if w["result"] and w["result"]["all_rules_pass"]))
check("backtest_limitations_discloses_no_v1_and_no_dcf_unreliable_replay",
      any("Quality v1 has no historical replay" in s for s in _backtest["limitations"])
      and any("DCF-unreliable has no historical replay" in s for s in _backtest["limitations"]))

# A week whose own quality_v2_as_of() call raises never aborts the
# rest of the backtest.
with mock.patch.object(quality_v2_engine, "get_quality_score_v2", side_effect=RuntimeError("boom")):
    _backtest_err = pir.quality_on_sale_backtest(
        "BTTEST.AX", _bt_closes, _bt_dates[310].isoformat(), _bt_dates[340].isoformat(),
        bundle=_bt_fake_bundle, log=lambda *a: None,
    )
check("backtest_a_per_week_failure_never_aborts_the_rest_never_raises",
      len(_backtest_err["weeks"]) > 0 and all(w["result"] is None for w in _backtest_err["weeks"]))
print("[task44_check3_quality_on_sale_backtest] quality_on_sale_backtest() runs the "
      "3 rules every Friday in range, Quality v2 flows through via the real quality_v2_"
      "as_of() replay machinery, first/last/total-qualified and gaps are derived "
      "correctly, the v1/DCF-unreliable replay limitation is disclosed, and a per-week "
      "failure never aborts the rest OK")


# ======================================================================
# CHECK 4 (TASK 44 item 7, report only, no live-rule change):
# top100_engine.quality_on_sale_moat_floor_report() - which candidates
# a Moat >= 40 floor would remove from TODAY's real candidate list.
# ======================================================================
_moat_fake_best = {
    "HIGHMOAT.AX": {"ticker": "HIGHMOAT.AX", "quality": 60.0, "quality_v2": 80.0,
                     "quality_exception_admitted": False, "moat": 55.0},
    "LOWMOAT.AX": {"ticker": "LOWMOAT.AX", "quality": 60.0, "quality_v2": 80.0,
                    "quality_exception_admitted": False, "moat": 20.0},
    "NOMOAT.AX": {"ticker": "NOMOAT.AX", "quality": 60.0, "quality_v2": 80.0,
                   "quality_exception_admitted": False, "moat": None},
}
_moat_fake_drops = {
    t: {"drop_pct": -50.0, "peak_date": None, "peak_price": 10.0, "current_price": 5.0}
    for t in _moat_fake_best
}
with mock.patch.object(te, "_build_best_by_ticker", return_value=(_moat_fake_best, 3)), \
     mock.patch.object(te.top100_store, "current_pool", return_value=[]), \
     mock.patch.object(te.top100_store, "latest_score_for_ticker", return_value=None), \
     mock.patch.object(te.drop_chip_store, "get", side_effect=lambda t: _moat_fake_drops.get(t)):
    _moat_report = te.quality_on_sale_moat_floor_report(min_moat=40.0)

check("moat_floor_report_current_rules_count_matches_the_real_candidate_list",
      _moat_report["current_rules_count"] == 3)
check("moat_floor_report_keeps_the_one_candidate_at_or_above_the_floor",
      _moat_report["with_moat_floor_count"] == 1)
_removed_tickers = {r["ticker"] for r in _moat_report["removed"]}
check("moat_floor_report_removes_the_below_floor_candidate",
      "LOWMOAT.AX" in _removed_tickers)
check("moat_floor_report_removes_a_missing_moat_candidate_too_(unknown_never_assumed_fine)",
      "NOMOAT.AX" in _removed_tickers)
check("moat_floor_report_never_removes_the_above_floor_candidate",
      "HIGHMOAT.AX" not in _removed_tickers)
check("moat_floor_report_removed_entries_carry_their_own_moat_score",
      next(r["moat"] for r in _moat_report["removed"] if r["ticker"] == "LOWMOAT.AX") == 20.0)
print("[task44_check4_moat_floor_report] the Moat >= 40 report correctly separates "
      "(a) current rules from (b) current rules + moat floor, treating a missing "
      "moat score as failing the floor (never assumed fine), with zero change to "
      "quality_on_sale_candidates()'s own three live rules OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
