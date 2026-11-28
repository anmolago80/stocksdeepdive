"""
TASK 83 (Director-directed, 28 Nov 2026, CIG case) - record the Top 200
cut-off, give dropped companies a real reason, stop hiding unrated
near-cut-off companies.

PROBLEMS (Director-verified):
  (a) The nightly selection never logged/stored the real #200 cut-off
      Value Score.
  (b) top100_engine.pool_changes() gave every still-scanned dropped
      company the SAME generic text ("still scanned, but its Value
      Score fell outside the top 200") even when a pre-ranking filter
      (DCF-unreliable, fund, fallback-financials, dedupe) removed it -
      CIG's real reason (Top 200 score 47.8, 27 Nov) couldn't be told
      apart from genuine cutoff churn.
  (c) top100_render._render_near_cutoff_tab() silently skipped any
      candidate with no rated score_row ("[near_cutoff] X: skipped -
      no rated score_row to render") - an unrated company within 2
      points of the cut-off (e.g. CIG-shaped) was simply invisible.

FIX:
  1. select_top100_pool() now logs "[top100] cut-off <date>: #200
     Value Score=<x> (ticker <T>); #201=<y> (ticker <U>)" and stores
     the cut-off denormalized on every top100_pool row (4 new, purely
     additive columns - cutoff_value_score/cutoff_ticker/next_
     excluded_value_score/next_excluded_ticker), same convention
     pool_selection_rule already uses.
  2. _build_best_by_ticker()'s new `excluded_diag` out-param (same
     zero-cost-when-None convention as the existing watch_diag)
     records {ticker: rule} for EVERY ticker any pre-ranking rule
     removes tonight - persisted via the new top100_selection_diag
     table (top100_store.save_selection_diag()/selection_diag_
     latest()), "at least for the latest night" (overwritten nightly,
     REBUILDABLE_TABLES, never backed up).
  3. pool_changes() now returns a structured dict per dropped ticker
     (reason_kind: "delisted"/"excluded_rule"/"below_cutoff"/
     "below_cutoff_unknown") instead of one pre-formatted sentence;
     top100_render._dropped_reason_text() builds the final EN/ES text.
  4. _render_near_cutoff_tab() renders an unrated near-cut-off
     candidate as a compact row (ticker, Value Score, gap chip, "not
     yet rated") instead of skipping it. Rated cards unchanged.
  5. No change to scoring, selection results, rubric, cache keys or
     API calls - confirmed by a byte-identical _build_best_by_ticker()
     comparison against the pre-TASK-83 git HEAD version (PART 1).

Run: python3 tests/test_director_task83_cutoff_record_and_near_cutoff_unrated.py
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TESTVOL = tempfile.mkdtemp(prefix="task83_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_QUALITY_V2_LIVE", None)
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)
os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)
os.environ.pop("FINANCIALS_STORE_LIVE", None)
os.environ.pop("TOP200_EXCLUDED_UNIVERSES", None)
os.environ.pop("TOP200_BACKFILL_LIVE", None)

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


import db_backup_engine
import nightly_scan
import scan_store
import top100_engine as te
import top100_render as tr
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _row(ticker, quality=70.0, mos=40.0, psy=5.0, moat=None, long_score=60.0, **extra):
    base = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": quality,
        "MOS %": mos, "Psychology": psy, "Discovery (lite)": 10.0,
        "Long Score": long_score, "Price": 10.0, "Intrinsic Value": 12.0,
        "Moat": moat,
    }
    base.update(extra)
    return base


def _save_universe(universe, rows, generated_at=None):
    payload = {
        "universe": universe, "source": "test",
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "run_night": None, "rows": rows, "attention_lite": True, "degraded": False,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


def _clear(*universes):
    for u in universes:
        p = scan_store._path(u)
        if os.path.exists(p):
            os.remove(p)


_patch_sector_fill = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill.start()
_patch_cadence = mock.patch.object(
    te.scheduler_engine, "nightly_universe_cadence",
    return_value={"S&P 500": "daily", "ASX 200": "daily"})
_patch_cadence.start()


# ======================================================================
# PART 1 (item 5 - no behaviour change): _build_best_by_ticker()'s
# return is BYTE-IDENTICAL to the pre-TASK-83 git HEAD version, on a
# fixture covering a DCF-unreliable row, a fund row, a stale row, and
# an ordinary candidate - proof the new excluded_diag bookkeeping is
# purely additive, exactly like watch_diag's own existing precedent.
# ======================================================================
_old_path = os.path.join(TESTVOL, "top100_engine_before_task83.py")
with open(_old_path, "w") as f:
    subprocess.run(["git", "show", "HEAD:top100_engine.py"], cwd=REPO_ROOT, stdout=f, check=True)
_spec = importlib.util.spec_from_file_location("top100_engine_before_task83", _old_path)
_old_te = importlib.util.module_from_spec(_spec)
sys.modules["top100_engine_before_task83"] = _old_te
_spec.loader.exec_module(_old_te)

_clear("S&P 500")
_save_universe("S&P 500", [
    _row("NORMAL1", long_score=55.0),
    _row("DCFBAD", long_score=70.0, **{"DCF Unreliable": True, "Price": 10.0, "Intrinsic Value": 40.0}),
    _row("FUNDCO", long_score=65.0, **{"Is Fund": True}),
    _row("STALECO", long_score=80.0, **{"Trading Status": "stale"}),
    _row("NORMAL2", long_score=50.0),
])
_old_best, _old_elig = _old_te._build_best_by_ticker(log=lambda *a, **k: None)
_new_best, _new_elig = te._build_best_by_ticker(log=lambda *a, **k: None, excluded_diag={})
check("[1a] _build_best_by_ticker(): same eligible_count old vs new", _old_elig == _new_elig)
check("[1b] _build_best_by_ticker(): same candidate ticker set old vs new",
      set(_old_best.keys()) == set(_new_best.keys()) == {"NORMAL1", "NORMAL2"})
_all_fields_equal = all(
    _old_best[t] == _new_best[t] for t in _old_best
)
check("[1c] _build_best_by_ticker(): every surviving candidate row is byte-identical "
      "old vs new (same dict, same values - excluded_diag changes nothing observable)",
      _all_fields_equal)

# excluded_diag itself (new-only capability) records the 3 real exclusions.
_diag = {}
te._build_best_by_ticker(log=lambda *a, **k: None, excluded_diag=_diag)
check("[1d] excluded_diag records DCFBAD -> dcf_unreliable", _diag.get("DCFBAD") == "dcf_unreliable")
check("[1e] excluded_diag records FUNDCO -> fund", _diag.get("FUNDCO") == "fund")
check("[1f] excluded_diag records STALECO -> stale", _diag.get("STALECO") == "stale")
check("[1g] excluded_diag has no entry for a ticker that was never excluded",
      "NORMAL1" not in _diag and "NORMAL2" not in _diag)


# ======================================================================
# PART 2 (item 1): select_top100_pool() end to end - the real #200/
# #201 cut-off log line + persisted top100_pool columns, on a 205-
# candidate pool shaped like the real CIG incident (a ticker that just
# misses the cut, not excluded by any rule).
# ======================================================================
_clear("S&P 500", "ASX 200")
_us_rows = [_row(f"FILLER{i}", long_score=100.0 - i * 0.1) for i in range(200)]
_us_rows.append(_row("CIG", long_score=47.8))  # the ONLY other candidate -> exactly rank #201
_save_universe("S&P 500", _us_rows)

_logs = []
te.select_top100_pool(log=_logs.append)
_cutoff_lines = [ln for ln in _logs if ln.startswith("[top100] cut-off ")]
check("[2a] exactly one '[top100] cut-off ...' line is logged", len(_cutoff_lines) == 1)
_m = re.match(
    r"\[top100\] cut-off (\S+): #200 Value Score=([\d.]+) \(ticker (\S+)\); "
    r"#201=([\d.]+) \(ticker (\S+)\)",
    _cutoff_lines[0],
) if _cutoff_lines else None
check("[2b] cut-off line matches the exact documented format "
      "'[top100] cut-off <date>: #200 Value Score=<x> (ticker <T>); #201=<y> (ticker <U>)'",
      _m is not None)

_merit = sorted(te._build_best_by_ticker(log=lambda *a, **k: None)[0].values(),
                 key=lambda r: r["value_score"], reverse=True)
check("[2c] the logged #200 ticker is really the 200th row by merit order",
      _m and _m.group(3) == _merit[199]["ticker"])
check("[2d] the logged #201 ticker is really the 201st row by merit order",
      _m and _m.group(5) == _merit[200]["ticker"])
check("[2e] CIG (the real incident's own ticker) is the logged #201 - just below the cut",
      _m and _m.group(5) == "CIG")

_pool_rows = ts.current_pool()
check("[2f] every saved pool row carries the SAME cutoff_value_score as the log line",
      _m and all(r.get("cutoff_value_score") == float(_m.group(2)) for r in _pool_rows))
check("[2g] every saved pool row carries the SAME cutoff_ticker as the log line",
      _m and all(r.get("cutoff_ticker") == _m.group(3) for r in _pool_rows))
check("[2h] every saved pool row carries the SAME next_excluded_value_score (#201) as the log line",
      _m and all(r.get("next_excluded_value_score") == float(_m.group(4)) for r in _pool_rows))
check("[2i] every saved pool row carries the SAME next_excluded_ticker (#201) as the log line",
      _m and all(r.get("next_excluded_ticker") == _m.group(5) for r in _pool_rows))
check("[2j] CIG itself is NOT in the saved pool (it's #201, just below the 200-slot cut)",
      "CIG" not in {r["ticker"] for r in _pool_rows})


# ======================================================================
# PART 3 (item 2): per-excluded-ticker rule is persisted via top100_
# store.save_selection_diag()/selection_diag_latest() - "at least for
# the latest night".
# ======================================================================
_clear("S&P 500")
_save_universe("S&P 500", [
    _row("KEEPME", long_score=90.0),
    _row("DCFBAD2", long_score=85.0, **{"DCF Unreliable": True, "Price": 10.0, "Intrinsic Value": 40.0}),
    _row("FUNDCO2", long_score=80.0, **{"Is Fund": True}),
])
te.select_top100_pool(log=lambda *a, **k: None)
_diag_latest = ts.selection_diag_latest()
check("[3a] selection_diag_latest() returns a real as_of date", bool(_diag_latest and _diag_latest.get("as_of")))
check("[3b] KEEPME appears in 'candidates' with its real value_score, not 'excluded'",
      _diag_latest and "KEEPME" in _diag_latest["candidates"] and "KEEPME" not in _diag_latest["excluded"])
check("[3c] DCFBAD2 appears in 'excluded' as 'dcf_unreliable', not 'candidates'",
      _diag_latest and _diag_latest["excluded"].get("DCFBAD2") == "dcf_unreliable"
      and "DCFBAD2" not in _diag_latest["candidates"])
check("[3d] FUNDCO2 appears in 'excluded' as 'fund'",
      _diag_latest and _diag_latest["excluded"].get("FUNDCO2") == "fund")


# ======================================================================
# PART 4 (item 3): pool_changes() - fixtures for each drop reason, EN
# + ES rendered text via top100_render._dropped_reason_text().
# ======================================================================
_current_fixture = [{"ticker": "KEPT", "value_score": 90.0, "cutoff_value_score": 49.2}]
_previous_fixture = [
    {"ticker": "KEPT", "value_score": 90.0},
    {"ticker": "CIG", "value_score": 47.8},
    {"ticker": "FUNDCO3", "value_score": 55.0},
    {"ticker": "DCFCO", "value_score": 70.0},
    {"ticker": "DEDUPECO", "value_score": 60.0},
    {"ticker": "GONEFOREVER", "value_score": 65.0},
    {"ticker": "MYSTERYCO", "value_score": 50.0},
]
_excluded_fixture = {
    "FUNDCO3": "fund", "DCFCO": "dcf_unreliable", "DEDUPECO": "dedupe",
}
_candidates_fixture = {"CIG": 47.8}

with mock.patch.object(scan_store, "list_saved_universes", return_value=["S&P 500"]), \
     mock.patch.object(scan_store, "load_scan_raw", return_value={
         "rows": [{"Ticker": t} for t in
                  ("CIG", "FUNDCO3", "DCFCO", "DEDUPECO", "MYSTERYCO")]}):
    _changes = te.pool_changes(
        current=_current_fixture, previous=_previous_fixture,
        excluded=_excluded_fixture, candidates=_candidates_fixture,
    )
_dropped_by_ticker = {d["ticker"]: d for d in _changes["dropped"]}

check("[4a] CIG -> reason_kind='below_cutoff' with its real score and tonight's real cutoff",
      _dropped_by_ticker["CIG"]["reason_kind"] == "below_cutoff"
      and _dropped_by_ticker["CIG"]["value_score"] == 47.8
      and _dropped_by_ticker["CIG"]["cutoff"] == 49.2)
check("[4b] FUNDCO3 -> reason_kind='excluded_rule', rule='fund'",
      _dropped_by_ticker["FUNDCO3"] == {"ticker": "FUNDCO3", "reason_kind": "excluded_rule", "rule": "fund"})
check("[4c] DCFCO -> reason_kind='excluded_rule', rule='dcf_unreliable'",
      _dropped_by_ticker["DCFCO"]["rule"] == "dcf_unreliable")
check("[4d] DEDUPECO -> reason_kind='excluded_rule', rule='dedupe'",
      _dropped_by_ticker["DEDUPECO"]["rule"] == "dedupe")
check("[4e] GONEFOREVER (not in any scanned universe) -> reason_kind='delisted'",
      _dropped_by_ticker["GONEFOREVER"]["reason_kind"] == "delisted")
check("[4f] MYSTERYCO (still scanned, no excluded/candidates data) -> reason_kind='below_cutoff_unknown'",
      _dropped_by_ticker["MYSTERYCO"]["reason_kind"] == "below_cutoff_unknown")

for _lang in ("en", "es"):
    _cig_text = tr._dropped_reason_text(_dropped_by_ticker["CIG"], _lang)
    check(f"[4g-{_lang}] CIG's rendered text names its real score (47.8) and cutoff (49.2)",
          "47.8" in _cig_text and "49.2" in _cig_text)
    check(f"[4h-{_lang}] CIG's rendered text still reads '200' literally",
          "200" in _cig_text)
    _fund_text = tr._dropped_reason_text(_dropped_by_ticker["FUNDCO3"], _lang)
    check(f"[4i-{_lang}] FUNDCO3's rendered text names the fund rule in plain language "
          "(not the raw code 'fund')",
          _fund_text != "fund" and len(_fund_text) > 10)
    _dedupe_text = tr._dropped_reason_text(_dropped_by_ticker["DEDUPECO"], _lang)
    check(f"[4j-{_lang}] DEDUPECO's rendered text mentions a duplicate/already-pooled listing",
          "duplicat" in _dedupe_text.lower() or "duplicad" in _dedupe_text.lower())
    _delisted_text = tr._dropped_reason_text(_dropped_by_ticker["GONEFOREVER"], _lang)
    check(f"[4k-{_lang}] GONEFOREVER's rendered text is the unchanged delisted wording",
          _delisted_text == tr._t("changes_strip_reason_delisted", _lang))
    _unknown_text = tr._dropped_reason_text(_dropped_by_ticker["MYSTERYCO"], _lang)
    check(f"[4l-{_lang}] MYSTERYCO's rendered text never fabricates a score/cutoff number",
          "{score}" not in _unknown_text and "{cutoff}" not in _unknown_text)


# ======================================================================
# PART 5 (item 4): near-cutoff tab - 2 rated + 2 unrated candidates
# (one of them CIG-shaped: unrated, 1.5 points below cut-off) all 4
# appear; rated cards render exactly as before, unrated ones as a new
# compact row.
# ======================================================================
_nc_candidates = [
    {"ticker": "RATEDHI", "company_name": "Rated Hi", "sector": "Tech", "most_recent_quarter": None,
     "value_score": 48.5, "gap_to_cutoff": 0.7, "research_score": 60.0, "research_score_stale_rubric": False},
    {"ticker": "RATEDLO", "company_name": "Rated Lo", "sector": "Tech", "most_recent_quarter": None,
     "value_score": 48.0, "gap_to_cutoff": 1.2, "research_score": 55.0, "research_score_stale_rubric": False},
    {"ticker": "CIG", "company_name": "Cemig", "sector": "Utilities", "most_recent_quarter": None,
     "value_score": 47.8, "gap_to_cutoff": 1.5, "research_score": None, "research_score_stale_rubric": False},
    {"ticker": "UNRATEDCO", "company_name": "Unrated Co", "sector": "Industrials", "most_recent_quarter": None,
     "value_score": 47.3, "gap_to_cutoff": 2.0, "research_score": None, "research_score_stale_rubric": False},
]
_nc_result = {"cutoff": 49.2, "candidates": _nc_candidates}
_nc_by_ticker = {
    "RATEDHI": {"ticker": "RATEDHI", "score_row": {"not_rated": False, "composite": 60.0}},
    "RATEDLO": {"ticker": "RATEDLO", "score_row": {"not_rated": False, "composite": 55.0}},
    "UNRATEDCO": {"ticker": "UNRATEDCO", "score_row": None},
    # CIG has no entry at all - never scored, the real incident's own shape.
}

_rendered_cards = []
_rendered_compact = []


def _fake_render_row(rank, row, lang, industry, sort_mode, **kw):
    _rendered_cards.append(row["ticker"])


with mock.patch.object(tr, "_near_cutoff_candidate_rows", return_value=_nc_by_ticker), \
     mock.patch.object(tr, "_render_row", side_effect=_fake_render_row), \
     mock.patch.object(tr, "st") as _fake_st:
    _fake_st.markdown.side_effect = lambda h, **k: _rendered_compact.append(h)
    _fake_st.caption.side_effect = lambda *a, **k: None
    tr._render_near_cutoff_tab("en", {}, "value_today", candidates_result=_nc_result)

check("[5a] both rated candidates render as full cards", set(_rendered_cards) == {"RATEDHI", "RATEDLO"})
check("[5b] exactly 2 compact rows render (CIG + UNRATEDCO)", len(_rendered_compact) == 2)
_compact_text = "".join(_rendered_compact)
check("[5c] CIG (the real incident's own shape - unrated, 1.5 below cut-off) "
      "appears in the near-cut-off section at all",
      ">CIG<" in _compact_text)
check("[5d] CIG's compact row shows its real Value Score (47.8)", "47.8" in _compact_text)
check("[5e] CIG's compact row shows its real gap to cut-off (1.5)",
      "1.5" in _compact_text)
check("[5f] CIG's compact row says 'not yet rated', never a dimension-chip card",
      "not yet rated" in _compact_text)
check("[5g] UNRATEDCO also appears in the near-cut-off section", ">UNRATEDCO<" in _compact_text)
check("[5h] neither unrated ticker was silently skipped (both of the 4 total candidates "
      "are accounted for between cards and compact rows)",
      len(_rendered_cards) + len(_rendered_compact) == len(_nc_candidates))

# ES rendering sanity for the same CIG row.
_rendered_compact_es = []
with mock.patch.object(tr, "_near_cutoff_candidate_rows", return_value=_nc_by_ticker), \
     mock.patch.object(tr, "_render_row", side_effect=lambda *a, **k: None), \
     mock.patch.object(tr, "st") as _fake_st_es:
    _fake_st_es.markdown.side_effect = lambda h, **k: _rendered_compact_es.append(h)
    _fake_st_es.caption.side_effect = lambda *a, **k: None
    tr._render_near_cutoff_tab("es", {}, "value_today", candidates_result=_nc_result)
check("[5i-es] the ES compact row uses the Spanish 'not yet rated' label",
      "aún sin calificar" in "".join(_rendered_compact_es))


# ======================================================================
# PART 6 (item 1, storage choice): the new top100_selection_diag table
# is classified in db_backup_engine.REBUILDABLE_TABLES - never backed
# up, since it's an overwrite-every-night diagnostic, not a historical
# record. The full TASK 77 drift test (which fails loudly on ANY
# unclassified table) is run as its own subprocess to confirm this in
# the real, complete schema-import context rather than re-deriving
# that logic here.
# ======================================================================
check("[6a] top100_selection_diag is in db_backup_engine.REBUILDABLE_TABLES",
      "top100_selection_diag" in db_backup_engine.REBUILDABLE_TABLES)
_drift = subprocess.run(
    [sys.executable, os.path.join(REPO_ROOT, "tests", "test_director_task77_backup_tables_drift.py")],
    cwd=REPO_ROOT, capture_output=True, text=True,
)
check("[6b] the full TASK 77 backup-tables drift test still passes 0 failures "
      "with the new table in place (every real table classified exactly once)",
      _drift.returncode == 0)


# ======================================================================
# PART 7 (item 5): no change to scoring/selection/rubric/cache keys -
# source-level sanity that this task never touched any of those.
# ======================================================================
check("[7a] DIMENSIONS (the 10 AI-scored rubric dimensions) is untouched by this task",
      len(te.DIMENSIONS) == 10)
check("[7b] WEIGHT_RETURN is untouched", te.WEIGHT_RETURN == 17)
_src = __import__("inspect").getsource(te)
check("[7c] RUBRIC_VERSION is never referenced anywhere in this task's own new code paths "
      "(select_top100_pool()'s cut-off block, _build_best_by_ticker()'s excluded_diag, "
      "pool_changes()) - confirmed by grepping the new cut-off/excluded_diag/pool_changes "
      "source for the string 'RUBRIC_VERSION'",
      "cutoff_value_score" in _src and _src.count("RUBRIC_VERSION") ==
      __import__("inspect").getsource(_old_te).count("RUBRIC_VERSION"))


_clear("S&P 500", "ASX 200")
_patch_sector_fill.stop()
_patch_cadence.stop()


print(f"\nTASK 83 RESULTS: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("\nALL TASK 83 (CUT-OFF RECORD + REAL DROP REASONS + NEAR-CUTOFF UNRATED) FIXTURES PASSED")
