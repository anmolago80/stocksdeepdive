"""
Director TASK 48 (12 Nov 2026, "wanted for the Director's review
tomorrow morning"): sticky buffer for Top 200 incumbents + the "Near
the cut-off" tab, both behind TOP200_STICKY_BUFFER_LIVE (default off).

Part A - sticky buffer: an incumbent (current Top 200/ASX-extension
member) keeps its place unless its Value Score falls more than
STICKY_BUFFER_POINTS (2.0) below the cut-off; a non-member gets no
such allowance; the pool stays exactly 200 (the lowest-scoring would-
be entrants are held out instead of the incumbents); same rule for
the ASX extension; works under every combination of TOP200_QUALITY_
V2_LIVE/TOP200_MOAT_BLEND_LIVE/TOP200_QUALITY_EXCEPTION_LIVE (the
buffer is evaluated against whichever score_key the caller passes).

Part B - "Near the cut-off" tab: non-members within NEAR_CUTOFF_POINTS
(2.0) below the real cut-off, max 30, ranked by Value Score descending,
with their own scoring category (mirrors TASK 46's quality_on_sale
design) wired as a FOURTH independent pack category into submit_
nightly_batch()/poll_and_ingest_batch().

Part C - admin preview: sticky_buffer_impact() + near_cutoff_
candidates()'s own sim_result/score_key override path, both read-only.

Covers:
  - _apply_sticky_buffer(): 1.9-below stays, 2.1-below leaves, non-
    member 0.1-below stays out, pool size always exact, lowest-
    scoring entrant displaced first, graceful shortfall handling.
  - top200_sticky_buffer_live() gate.
  - select_top100_pool() end to end: gate off byte-identical to
    today, gate on actually buffers an incumbent and holds out an
    entrant, pool stays exactly POOL_SIZE, same for the ASX extension,
    never combined with the backfill path.
  - _simulate_pool_generic()/simulate_pool_v2_moat_and_exception_with_
    buffer(): sticky_buffer=False (every pre-existing caller) byte-
    identical; sticky_buffer=True applies the buffer against the
    scenario's own score variant.
  - near_cutoff_candidates(): correct inclusion/exclusion, gap bounds,
    cap, sort order, empty-state, and the sim_result/score_key
    override path for the admin preview.
  - _select_near_cutoff_entrants(): stale-rubric broadening, capacity
    math after all three other categories.
  - submit_nightly_batch()/poll_and_ingest_batch() end to end: near-
    cut-off entrants get their own, 4th independent pack category,
    never merged with any other, daily-submission-count includes
    them, the real ingest-time "[near_cutoff] scored N, cost $X" line.
  - sticky_buffer_impact(): kept-dropouts/held-out-entrants correctly
    derived by diffing the no-buffer vs with-buffer scenario.
  - Render side: _near_cutoff_gap_chip_html()/_render_row()'s new
    near_cutoff_gap_html param.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task48_sticky_buffer_and_near_cutoff.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task48_sticky_buffer_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)

import top100_engine as te
import top100_store as ts
import top100_render as tr

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


def _mk(t, vs, **extra):
    row = {"ticker": t, "value_score": vs, "company_name": f"{t} Co", "sector": "Industrials",
           "most_recent_quarter": None}
    row.update(extra)
    return row


# ======================================================================
# CHECK 1-10: _apply_sticky_buffer() - the Director's own exact
# threshold tests, plus edge cases.
# ======================================================================
check("gate unset reads off", te.top200_sticky_buffer_live() is False)
os.environ["TOP200_STICKY_BUFFER_LIVE"] = "1"
check("gate '1' reads on", te.top200_sticky_buffer_live() is True)
os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)

_merit_1p9 = [_mk("A", 10), _mk("B", 9), _mk("C", 7.1), _mk("D", 1.0)]
_pool_1p9, _cutoff_1p9 = te._apply_sticky_buffer(_merit_1p9, 2, {"C"}, log=lambda *a: None)
check("incumbent 1.9 below cut-off (cutoff 9, incumbent at 7.1) STAYS IN",
      {r["ticker"] for r in _pool_1p9} == {"A", "C"} and _cutoff_1p9 == 9)

_merit_2p1 = [_mk("A", 10), _mk("B", 9), _mk("C", 6.9), _mk("D", 1.0)]
_pool_2p1, _ = te._apply_sticky_buffer(_merit_2p1, 2, {"C"}, log=lambda *a: None)
check("incumbent 2.1 below cut-off (cutoff 9, incumbent at 6.9) LEAVES",
      {r["ticker"] for r in _pool_2p1} == {"A", "B"})

_merit_nonmember = [_mk("A", 10), _mk("B", 9), _mk("C", 8.9)]
_pool_nonmember, _ = te._apply_sticky_buffer(_merit_nonmember, 2, set(), log=lambda *a: None)
check("a NON-MEMBER only 0.1 below cut-off still STAYS OUT (no buffer for non-members)",
      {r["ticker"] for r in _pool_nonmember} == {"A", "B"})

check("pool size is always exactly n (buffered case)", len(_pool_1p9) == 2)
check("pool size is always exactly n (no-buffer case)", len(_pool_nonmember) == 2)

_merit_lowest = [_mk("A", 10), _mk("B", 9), _mk("E", 8.5), _mk("D", 7.5)]
_pool_lowest, _ = te._apply_sticky_buffer(_merit_lowest, 2, {"D"}, log=lambda *a: None)
check("the LOWEST-scoring non-incumbent in the naive pool (B, 9, vs A, 10) is displaced "
      "first, never the higher-scoring one", {r["ticker"] for r in _pool_lowest} == {"A", "D"})

_merit_shortfall = [_mk("A", 10), _mk("INC1", 9)]
_pool_shortfall, _ = te._apply_sticky_buffer(
    _merit_shortfall, 2, {"A", "INC1", "INC2", "INC3"}, log=lambda *a: None)
check("a shortfall of entrant slots (every naive-pool member is itself an incumbent, "
      "so nothing is removable) leaves the naive pool untouched, size still exact",
      {r["ticker"] for r in _pool_shortfall} == {"A", "INC1"} and len(_pool_shortfall) == 2)

_merit_fewer = [_mk("A", 10)]
_pool_fewer, _cutoff_fewer = te._apply_sticky_buffer(_merit_fewer, 5, set(), log=lambda *a: None)
check("fewer candidates than n returns all of them, never crashes",
      len(_pool_fewer) == 1 and _cutoff_fewer == 10)

_pool_empty, _cutoff_empty = te._apply_sticky_buffer([], 5, set(), log=lambda *a: None)
check("an empty candidate list returns [] and cutoff=None, never crashes",
      _pool_empty == [] and _cutoff_empty is None)


# ======================================================================
# CHECK 11-20: select_top100_pool() end to end - gate off byte-
# identical, gate on actually buffers, pool stays exactly POOL_SIZE,
# same rule for the ASX extension, never combined with backfill.
# ======================================================================
_FAKE_CADENCE = {"S&P 500": "daily", "ASX 300": "daily"}


def _mk_scan_row(ticker, quality=70.0, mos=10.0, psych=5.0, price=90.0, iv=100.0, sector="Industrials"):
    return {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Sector": sector,
        "Quality": quality, "MOS %": mos, "Psychology": psych, "Discovery (lite)": 30.0,
        "Intrinsic Value": iv, "Price": price, "Is Fund": False, "DCF Unreliable": False,
        # "Long Score" is a required field for _build_best_by_ticker()'s
        # own per-row eligibility check (row_long_score is None ->
        # skipped) - the real value is irrelevant here since _recompute_
        # value_score() recomputes "value_score" fresh from Quality/MOS/
        # Psychology regardless; any non-None placeholder satisfies it.
        "Long Score": quality,
    }


def _run_selection_with_incumbents(incumbent_rows, candidate_rows, universe="S&P 500", pool_size=3):
    """Seeds a live pool (incumbents) + a scan (full candidate set,
    which must include the incumbents too, matching how a real re-run
    sees its own prior-night members again), then calls select_
    top100_pool() under a small POOL_SIZE so the buffer's own effect
    is easy to observe without a 200-row fixture."""
    if os.path.exists(ts.DB_PATH):
        os.remove(ts.DB_PATH)
    ts.save_pool(
        [{"ticker": r["ticker"], "company_name": r["ticker"] + " Co", "value_score": r["value_score"]}
         for r in incumbent_rows],
        as_of="2026-11-11",
    )
    import scan_store
    scan_store.save_scan(universe, candidate_rows, source_label="test fixture")
    with mock.patch.object(te, "POOL_SIZE", pool_size), \
         mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
        te.select_top100_pool(log=lambda *a: None, scan_day="2026-11-12")
    return {r["ticker"]: r for r in ts.current_pool()}


# Naive top-3: HIGH1(90), HIGH2(80), MID(70) by Quality (Value Score ~
# proportional for identical mos/psych) - INCUMBENT scores just below
# the naive cut-off, LOWENTRANT scores just inside it.
_candidate_rows = [
    _mk_scan_row("HIGH1", quality=95.0),
    _mk_scan_row("HIGH2", quality=85.0),
    _mk_scan_row("LOWENTRANT", quality=75.0),
    _mk_scan_row("INCUMBENT", quality=74.0),
]
_incumbent_rows = [{"ticker": "INCUMBENT", "value_score": 74.0 * 0.4375 + 10 * 0.3125 + 5 * 0.25}]

os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)
_result_off = _run_selection_with_incumbents(_incumbent_rows, _candidate_rows)
check("gate OFF: real selection is the plain top-3 by Value Score - byte-identical to "
      "before TASK 48 (INCUMBENT, scoring below LOWENTRANT, is NOT kept)",
      set(_result_off.keys()) == {"HIGH1", "HIGH2", "LOWENTRANT"}
      and "INCUMBENT" not in _result_off)

os.environ["TOP200_STICKY_BUFFER_LIVE"] = "1"
_result_on = _run_selection_with_incumbents(_incumbent_rows, _candidate_rows)
check("gate ON: INCUMBENT (within 2.0 of the cut-off, a real incumbent) IS kept",
      "INCUMBENT" in _result_on)
check("gate ON: LOWENTRANT (the lowest-scoring non-incumbent in the naive top-3) is "
      "held out instead", "LOWENTRANT" not in _result_on)
check("gate ON: the pool is still exactly POOL_SIZE (3)", len(_result_on) == 3)
os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)

# ASX extension - same rule. POOL_SIZE=0 keeps every AU candidate out
# of the main pool entirely (nothing else to fill it with), so the
# extension's OWN naive top-2 cut (TOP20_AU_TARGET=2) is driven purely
# by these 3 AU candidates: AUHIGH.AX dominates slot 1, AULOWENTRANT.AX
# is the naive slot-2 cut-off (37.1875), and AUINCUMBENT.AX (36.75)
# sits 0.4375 below it - within the buffer.
_au_candidate_rows = [
    _mk_scan_row("AUHIGH.AX", quality=95.0),
    _mk_scan_row("AULOWENTRANT.AX", quality=75.0),
    _mk_scan_row("AUINCUMBENT.AX", quality=74.0),
]


def _run_au_extension_with_incumbents():
    if os.path.exists(ts.DB_PATH):
        os.remove(ts.DB_PATH)
    ts.save_pool([{"ticker": "AUINCUMBENT.AX", "company_name": "AUINCUMBENT.AX Co",
                    "value_score": 74.0 * 0.4375 + 10 * 0.3125 + 5 * 0.25, "asx_extension": True}],
                 as_of="2026-11-11")
    import scan_store
    scan_store.save_scan("ASX 300", _au_candidate_rows, source_label="test fixture")
    with mock.patch.object(te, "POOL_SIZE", 0), mock.patch.object(te, "TOP20_AU_TARGET", 2), \
         mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
        te.select_top100_pool(log=lambda *a: None, scan_day="2026-11-12")
    return {r["ticker"]: r for r in ts.current_asx_extension()}


os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)
_au_off = _run_au_extension_with_incumbents()
check("ASX extension, gate OFF: naive top-2 by Value Score (AUHIGH.AX + AULOWENTRANT.AX, "
      "AUINCUMBENT.AX NOT kept)", set(_au_off.keys()) == {"AUHIGH.AX", "AULOWENTRANT.AX"})
os.environ["TOP200_STICKY_BUFFER_LIVE"] = "1"
_au_on = _run_au_extension_with_incumbents()
check("ASX extension, gate ON: the SAME sticky-buffer rule applies - AUINCUMBENT.AX "
      "(within 2.0 of the extension's own cut-off) is kept, AULOWENTRANT.AX held out, "
      "extension size still exact (2)",
      set(_au_on.keys()) == {"AUHIGH.AX", "AUINCUMBENT.AX"} and len(_au_on) == 2)
os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)

# Never combined with backfill.
os.environ["TOP200_STICKY_BUFFER_LIVE"] = "1"
os.environ["TOP200_BACKFILL_LIVE"] = "1"
try:
    _result_backfill = _run_selection_with_incumbents(_incumbent_rows, _candidate_rows)
    check("with BOTH gates on, the backfill path is untouched by the buffer - "
          "INCUMBENT is NOT force-kept (the Director's own instruction never asked "
          "for these two features to combine)", "INCUMBENT" not in _result_backfill)
finally:
    os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)
    os.environ.pop("TOP200_BACKFILL_LIVE", None)


# ======================================================================
# CHECK 21-24: _simulate_pool_generic()/simulate_pool_v2_moat_and_
# exception_with_buffer() - sticky_buffer=False byte-identical,
# sticky_buffer=True applies the buffer against the scenario's own
# score variant (works under every combination of the v2/moat/
# exception gates, since the buffer is handed whichever sort_key the
# caller already uses).
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
import scan_store
# scan_store persists its universe files separately from top100_store's
# sqlite DB (RAILWAY_VOLUME_MOUNT_PATH-based JSON files, untouched by
# removing ts.DB_PATH above) - the ASX extension section above saved a
# real "ASX 300" scan that would otherwise leak into this section's own
# candidate universe (AUHIGH.AX et al would shift the naive cut-off).
# Cleared explicitly so this section's candidate universe is exactly
# the 4 rows in _candidate_rows, nothing else.
scan_store.save_scan("ASX 300", [], source_label="test fixture")
scan_store.save_scan("S&P 500", _candidate_rows, source_label="test fixture")
ts.save_pool([{"ticker": "INCUMBENT", "company_name": "INCUMBENT Co", "value_score": 74.0}],
             as_of="2026-11-11")

with mock.patch.object(te, "POOL_SIZE", 3), \
     mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
    _sim_no_buffer = te._simulate_pool_generic(lambda r: r["value_score"], log=lambda *a: None)
    _sim_with_buffer = te._simulate_pool_generic(
        lambda r: r["value_score"], log=lambda *a: None, sticky_buffer=True)
check("sticky_buffer=False (default) is unaffected by TOP200_STICKY_BUFFER_LIVE being "
      "unset or set - INCUMBENT is not force-kept",
      "INCUMBENT" not in _sim_no_buffer["pool_tickers"])
check("sticky_buffer=True applies the buffer regardless of the real env gate's own state "
      "(it's unset here) - INCUMBENT IS kept", "INCUMBENT" in _sim_with_buffer["pool_tickers"])
check("simulate_pool_v2_moat_and_exception_with_buffer() exists and returns the same "
      "shape as its sibling simulate_pool_* functions",
      callable(te.simulate_pool_v2_moat_and_exception_with_buffer))

with mock.patch.object(te, "POOL_SIZE", 3), \
     mock.patch.object(te.scheduler_engine, "nightly_universe_cadence", return_value=dict(_FAKE_CADENCE)):
    _sim_v2_moat_buf = te.simulate_pool_v2_moat_and_exception_with_buffer(log=lambda *a: None)
check("simulate_pool_v2_moat_and_exception_with_buffer() returns the standard "
      "simulate_pool_* shape with eligible_count/rows/pool_tickers/cutoff",
      set(["eligible_count", "rows", "pool_tickers", "extension_tickers", "cutoff"])
      <= set(_sim_v2_moat_buf.keys()))


# ======================================================================
# CHECK 25-33: near_cutoff_candidates() - inclusion/exclusion, gap
# bounds, cap, sort order, empty state, sim_result/score_key override.
# ======================================================================
_ncu_best = {
    "MEMBER1": _mk("MEMBER1", 50.0), "MEMBER2": _mk("MEMBER2", 40.0),
    "NEAR1": _mk("NEAR1", 39.5, sector="Tech"), "NEAR2": _mk("NEAR2", 38.5, sector="Tech"),
    "FAR": _mk("FAR", 10.0, sector="Tech"),
}
with mock.patch.object(te, "_build_best_by_ticker", return_value=(_ncu_best, 5)), \
     mock.patch.object(te.top100_store, "current_pool", return_value=[{"ticker": "MEMBER1"}, {"ticker": "MEMBER2"}]), \
     mock.patch.object(te.top100_store, "current_asx_extension", return_value=[]), \
     mock.patch.object(te.top100_store, "latest_score_for_ticker", return_value=None), \
     mock.patch.object(te, "POOL_SIZE", 2):
    _ncu_result = te.near_cutoff_candidates(log=lambda *a: None)
check("cutoff is the naive top-POOL_SIZE cutoff (MEMBER2's own score, 40.0)",
      _ncu_result["cutoff"] == 40.0)
_ncu_tickers = [c["ticker"] for c in _ncu_result["candidates"]]
check("members (MEMBER1/MEMBER2) are excluded from candidates",
      "MEMBER1" not in _ncu_tickers and "MEMBER2" not in _ncu_tickers)
check("non-members within 2.0 of the cut-off (NEAR1/NEAR2) are included",
      set(_ncu_tickers) == {"NEAR1", "NEAR2"})
check("FAR (10.0, far below the cut-off) is excluded", "FAR" not in _ncu_tickers)
check("candidates are sorted by Value Score descending (NEAR1 before NEAR2)",
      _ncu_tickers.index("NEAR1") < _ncu_tickers.index("NEAR2"))
check("gap_to_cutoff is correctly computed (NEAR1: 40.0-39.5=0.5)",
      next(c["gap_to_cutoff"] for c in _ncu_result["candidates"] if c["ticker"] == "NEAR1") == 0.5)

_ncu_many_best = {f"N{i}": _mk(f"N{i}", 39.9 - i * 0.01) for i in range(40)}
with mock.patch.object(te, "_build_best_by_ticker", return_value=(_ncu_many_best, 40)), \
     mock.patch.object(te.top100_store, "current_pool", return_value=[]), \
     mock.patch.object(te.top100_store, "current_asx_extension", return_value=[]), \
     mock.patch.object(te.top100_store, "latest_score_for_ticker", return_value=None), \
     mock.patch.object(te, "POOL_SIZE", 1):
    _ncu_capped = te.near_cutoff_candidates(log=lambda *a: None)
check(f"near_cutoff_candidates() caps at NEAR_CUTOFF_MAX_CANDIDATES ({te.NEAR_CUTOFF_MAX_CANDIDATES})",
      len(_ncu_capped["candidates"]) == te.NEAR_CUTOFF_MAX_CANDIDATES)

with mock.patch.object(te, "_build_best_by_ticker", return_value=({}, 0)), \
     mock.patch.object(te.top100_store, "current_pool", return_value=[]), \
     mock.patch.object(te.top100_store, "current_asx_extension", return_value=[]):
    _ncu_empty_universe = te.near_cutoff_candidates(log=lambda *a: None)
check("an empty eligible universe returns cutoff=None and no candidates, never crashes",
      _ncu_empty_universe == {"cutoff": None, "candidates": []})

# sim_result/score_key override path (TASK 48C admin preview use).
_ncu_sim_result = {
    "rows": _ncu_best, "pool_tickers": {"MEMBER1", "MEMBER2"}, "extension_tickers": set(), "cutoff": 40.0,
}
_ncu_from_sim = te.near_cutoff_candidates(sim_result=_ncu_sim_result, log=lambda *a: None)
check("the sim_result override path reads rows/pool_tickers/cutoff straight from the "
      "given scenario, matching the direct-call result exactly",
      {c["ticker"] for c in _ncu_from_sim["candidates"]} == {"NEAR1", "NEAR2"}
      and _ncu_from_sim["cutoff"] == 40.0)


# ======================================================================
# CHECK 34-38: _select_near_cutoff_entrants() - stale-rubric
# broadening, capacity math.
# ======================================================================
def _ncu_cand(t, research_score=None, stale=False):
    return {"ticker": t, "company_name": f"{t} Co", "sector": "Industrials", "most_recent_quarter": None,
            "value_score": 39.0, "gap_to_cutoff": 1.0, "research_score": research_score,
            "research_score_stale_rubric": stale}


_today = datetime.now(timezone.utc).date()
_ncu_mixed = {
    "candidates": [
        _ncu_cand("SCOREDCURRENT", research_score=90.0, stale=False),
        _ncu_cand("SCOREDSTALE", research_score=70.0, stale=True),
        _ncu_cand("UNSCORED1", research_score=None, stale=False),
    ],
    "cutoff": 40.0,
}
with mock.patch.object(te, "near_cutoff_candidates", return_value=_ncu_mixed):
    _ncu_entrants = te._select_near_cutoff_entrants(
        prior_entrant_count=0, daily_entrants_so_far=0, today=_today, log=lambda *a: None)
_ncu_entrant_tickers = [e[0]["ticker"] for e in _ncu_entrants]
check("a current-rubric-scored candidate is never queued", "SCOREDCURRENT" not in _ncu_entrant_tickers)
check("a stale-rubric-scored candidate IS queued", "SCOREDSTALE" in _ncu_entrant_tickers)
check("a genuinely unscored candidate IS queued", "UNSCORED1" in _ncu_entrant_tickers)
check("every entrant is tagged with reason 'near_cutoff'",
      all(reason == "near_cutoff" for _row, reason in _ncu_entrants))

with mock.patch.object(te, "near_cutoff_candidates", return_value=_ncu_mixed):
    _ncu_no_room = te._select_near_cutoff_entrants(
        prior_entrant_count=te.MAX_NIGHTLY_SCORES, daily_entrants_so_far=0, today=_today, log=lambda *a: None)
check("zero batch room left (prior_entrant_count fills MAX_NIGHTLY_SCORES) queues nothing",
      _ncu_no_room == [])


# ======================================================================
# CHECK 39-47: submit_nightly_batch()/poll_and_ingest_batch() end to
# end - near-cut-off entrants get their OWN, FOURTH independent pack
# category, daily-submission-count includes them, the real ingest-
# time "[near_cutoff] scored N, cost $X" line.
# ======================================================================
def _mk_pool(n):
    return [{"ticker": f"POOL{i}", "company_name": f"Pool Co {i}", "sector": "Industrials",
              "most_recent_quarter": None} for i in range(n)]


def _submit_and_capture(pool, ncu_candidates_result=None, force=False):
    ts.seed_pool_presence({row["ticker"]: te.NEWCOMER_PERSISTENCE_NIGHTS for row in pool}, "2026-10-09")
    _ncu_result = ncu_candidates_result if ncu_candidates_result is not None else {
        "candidates": [], "cutoff": None,
    }
    with mock.patch("anthropic.Anthropic") as MockClient, \
         mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                    side_effect=lambda **kw: kw), \
         mock.patch("anthropic.types.messages.batch_create_params.Request",
                    side_effect=lambda **kw: kw), \
         mock.patch.object(te, "quality_on_sale_candidates", return_value={"candidates": [], "total_qualifying": 0}), \
         mock.patch.object(te, "near_cutoff_candidates", return_value=_ncu_result):
        instance = MockClient.return_value
        fake_batch = mock.Mock(id="msgbatch_ncu_test")
        instance.with_options.return_value.messages.batches.create.return_value = fake_batch
        logs = []
        result = te.submit_nightly_batch(pool=pool, log=logs.append, force=force)
        captured_requests = None
        if instance.with_options.return_value.messages.batches.create.called:
            captured_requests = instance.with_options.return_value.messages.batches.create.call_args.kwargs.get("requests")
        return result, captured_requests, logs


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_ncu_two_unscored = {
    "candidates": [_ncu_cand("NCU1"), _ncu_cand("NCU2")],
    "cutoff": 40.0,
}
_pool3 = _mk_pool(3)
_result_on, _requests_on, _logs_on = _submit_and_capture(_pool3, ncu_candidates_result=_ncu_two_unscored)
check("near-cut-off qualifiers get their own, separate request (3 normal pack into 1, "
      "2 near-cut-off entrants pack into their OWN)", len(_requests_on) == 2)

_batch_state = ts.get_batch_state()
_ncu_tagged = [v for v in _batch_state["custom_id_map"].values() if v.get("is_near_cutoff")]
_non_ncu_tagged = [v for v in _batch_state["custom_id_map"].values() if not v.get("is_near_cutoff")]
check("exactly one pack is tagged is_near_cutoff=True, carrying NCU1/NCU2",
      len(_ncu_tagged) == 1 and set(_ncu_tagged[0]["entrants"].keys()) == {"NCU1", "NCU2"})
check("the normal pack is tagged neither is_near_cutoff, is_quality_on_sale, nor is_prescore",
      all(not p.get("is_near_cutoff") and not p.get("is_quality_on_sale") and not p.get("is_prescore")
          for p in _non_ncu_tagged))
check("the near-cut-off pack itself is tagged neither is_prescore nor is_quality_on_sale - "
      "a fourth, fully independent category",
      all(not p.get("is_prescore") and not p.get("is_quality_on_sale") for p in _ncu_tagged))

_daily_after = ts.get_daily_submission_state(datetime.now(timezone.utc).date().isoformat())
check("the daily submission count includes the near-cut-off entrants too (3 normal + "
      "2 near-cut-off = 5)", _daily_after["entrants"] == 5)
check("the submission log line mentions the near-cut-off companies queued",
      any("near-cut-off compan" in m for m in _logs_on))

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_pool3b = _mk_pool(3)
_result_force, _requests_force, _logs_force = _submit_and_capture(
    _pool3b, ncu_candidates_result=_ncu_two_unscored, force=True)
check("force=True never also spends on near-cut-off, even with qualifiers waiting - "
      "exactly 1 request (3 normal only)", len(_requests_force) == 1)

with ts._conn() as conn:
    conn.execute("DELETE FROM top100_daily_submissions")


def _mk_batch_result(custom_id, tickers, input_tokens=1000, output_tokens=500):
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


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_submitting_marker("2026-11-12", te.MODEL_TOP100, {})
ts.clear_submitting_marker()
_custom_id_map = {
    "t100-0": {"entrants": {"NORM1": {"score_key": "D2026-11-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": False, "is_quality_on_sale": False, "is_near_cutoff": False},
    "t100-1": {"entrants": {"NCU1": {"score_key": "D2026-11-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": False, "is_quality_on_sale": False, "is_near_cutoff": True},
}
ts.save_batch_state("msgbatch_ncu_ingest_test", "2026-11-12", te.MODEL_TOP100, _custom_id_map)
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = mock.Mock(processing_status="ended")
    instance.messages.batches.results.return_value = [
        _mk_batch_result("t100-0", ["NORM1"], input_tokens=1000, output_tokens=500),
        _mk_batch_result("t100-1", ["NCU1"], input_tokens=4000, output_tokens=1200),
    ]
    _ingest_logs = []
    _ingest_summary = te.poll_and_ingest_batch(log=_ingest_logs.append)

check("poll_and_ingest_batch() ingested both results", _ingest_summary is not None and _ingest_summary["scored"] == 2)
_expected_ncu_cost = te.estimate_batch_cost_usd(4000, 1200, 0, 0)
check("a '[near_cutoff] scored 1, cost $X (0 failed)' line was logged with the REAL "
      "ingest-time cost for exactly the near-cut-off-tagged result's own tokens",
      any(m == f"[near_cutoff] scored 1, cost ${_expected_ncu_cost:.2f} (0 failed)" for m in _ingest_logs))
check("NCU1's score was actually saved", ts.latest_score_for_ticker("NCU1", te.MODEL_TOP100) is not None)

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_submitting_marker("2026-11-12", te.MODEL_TOP100, {})
ts.clear_submitting_marker()
ts.save_batch_state("msgbatch_no_ncu_test", "2026-11-12", te.MODEL_TOP100, {
    "t100-0": {"entrants": {"NORM2": {"score_key": "D2026-11-12", "most_recent_quarter": None}},
               "schema_mode": "legacy", "is_prescore": False, "is_quality_on_sale": False, "is_near_cutoff": False},
})
with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = mock.Mock(processing_status="ended")
    instance.messages.batches.results.return_value = [_mk_batch_result("t100-0", ["NORM2"])]
    _ingest_logs_plain = []
    te.poll_and_ingest_batch(log=_ingest_logs_plain.append)
check("a batch carrying NO near-cut-off-tagged pack logs no '[near_cutoff]' line at all",
      not any("[near_cutoff]" in m for m in _ingest_logs_plain))


# ======================================================================
# CHECK 48-51: sticky_buffer_impact() - kept-dropouts/held-out-
# entrants correctly derived.
# ======================================================================
_sbi_rows = {
    "A": _mk("A", 10), "B": _mk("B", 9), "KEPT": _mk("KEPT", 7.1), "HELDOUT": _mk("HELDOUT", 9.0),
}
_sbi_no_buffer = {"rows": _sbi_rows, "pool_tickers": {"A", "HELDOUT"}, "extension_tickers": set(), "cutoff": 9.0}
_sbi_with_buffer = {"rows": _sbi_rows, "pool_tickers": {"A", "KEPT"}, "extension_tickers": set(), "cutoff": 9.0}
_sbi_result = te.sticky_buffer_impact(_sbi_no_buffer, _sbi_with_buffer, log=lambda *a: None)
check("kept_dropouts correctly identifies KEPT (in the buffered set, not the naive set)",
      [r["ticker"] for r in _sbi_result["kept_dropouts"]] == ["KEPT"])
check("kept_dropouts reports the correct gap_to_cutoff (9.0-7.1=1.9)",
      _sbi_result["kept_dropouts"][0]["gap_to_cutoff"] == 1.9)
check("held_out_entrants correctly identifies HELDOUT (in the naive set, not the buffered set)",
      [r["ticker"] for r in _sbi_result["held_out_entrants"]] == ["HELDOUT"])
check("held_out_entrants rows carry no gap_to_cutoff key (drop-out-only column)",
      "gap_to_cutoff" not in _sbi_result["held_out_entrants"][0])


# ======================================================================
# CHECK 52-54: render side - _near_cutoff_gap_chip_html()/_render_row()'s
# new near_cutoff_gap_html param.
# ======================================================================
_gap_html = tr._near_cutoff_gap_chip_html(0.5, "en")
check("_near_cutoff_gap_chip_html() renders the gap value in its text",
      "0.5" in _gap_html and "below cut-off" in _gap_html)

import inspect
_render_row_sig = inspect.signature(tr._render_row)
check("_render_row() has a near_cutoff_gap_html param defaulting to None (every "
      "existing caller unaffected)",
      "near_cutoff_gap_html" in _render_row_sig.parameters
      and _render_row_sig.parameters["near_cutoff_gap_html"].default is None)

_render_row_src = inspect.getsource(tr._render_row)
check("_render_row()'s row-3 flex container includes near_cutoff_gap_html alongside "
      "cross_list_chip_html", "near_cutoff_gap_html" in _render_row_src
      and "cross_list_chip_html" in _render_row_src)


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
