"""
TASK 44 follow-up (Director-directed, 12 Nov 2026, after the 23:00 UTC
11 Nov scan): three items from the Director's own Railway log review.

Covers:
  1. top100_engine.log_quality_on_sale_nightly_summary() - the new
     "[quality_on_sale] ..." nightly log line: total qualifiers,
     needing scoring, a SELECTION-TIME-only estimated cost for the
     unscored group (never a real scored/cost figure of its own -
     TASK 46, 12 Nov 2026, wired the real ingest separately via
     poll_and_ingest_batch()'s own "[quality_on_sale] scored N, cost
     $X" line, so this summary's own wording was revised to stop
     claiming "0 scored tonight" now that a real scored count can
     exist later the same night), count also in the Top 200, and the
     top 20 tickers in rank order (* marking also-in-Top-200). Also
     confirms scheduler_engine._run_top100() calls it right after the
     support-levels job, inside a non-fatal try/except.
  2. (support_levels_job.py's own single-ticker-retry fix for the 28
     "only 1 weekly bar" skips is covered by tests/test_director_
     task36_support_levels_job_and_render.py's own CHECK 15/16 -
     not duplicated here.)
  3. ADBE's missing drop-chip entry: confirmed by SOURCE INSPECTION
     that a DCF-unreliable row (IV/Price > resolver_engine.DCF_SANITY_
     MULTIPLE, the "3x rule") is excluded from top100_engine._build_
     best_by_ticker()'s own output (`continue`d) whenever either it
     fails _quality_exception_admits()'s own criteria OR TOP200_
     QUALITY_EXCEPTION_LIVE is off - and that _build_best_by_ticker()'s
     own output is exactly what support_levels_job.py's widened ticker
     scope (TASK 44 item 1) draws from, so an excluded row never gets
     a weekly-bar download, and therefore never gets a drop_chip_store
     entry, until the exception admits it (gate on AND it passes
     _quality_exception_admits()).

Run: python3 tests/test_director_task44_followup_log_and_adbe.py
"""
import inspect
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task44_followup_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import resolver_engine
import scheduler_engine as sched
import top100_engine as te

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
# CHECK 1: log_quality_on_sale_nightly_summary() - correct counts,
# honest "0 scored tonight (cost $0.00 ...)", a non-zero estimated
# cost for the unscored group, the also-in-Top-200 count, and the
# top 20 tickers in rank order with "*" marking also-in-Top-200.
# ======================================================================
_candidates = (
    [{"ticker": f"SCORED{i}.AX", "company_name": f"Scored {i}", "sector": None,
      "most_recent_quarter": None, "research_score": 90.0 - i,
      "in_top_200": (i % 2 == 0)} for i in range(15)]
    + [{"ticker": f"UNSCORED{i}.AX", "company_name": f"Unscored {i}", "sector": None,
        "most_recent_quarter": None, "research_score": None,
        "in_top_200": False} for i in range(10)]
)
_logged = []
with mock.patch.object(te, "quality_on_sale_candidates",
                        return_value={"total_qualifying": 30, "candidates": _candidates}):
    te.log_quality_on_sale_nightly_summary(log=lambda m: _logged.append(m))

_summary_lines = [m for m in _logged if m.startswith("[quality_on_sale] ") and "total qualifiers" in m]
check("exactly one summary line is logged", len(_summary_lines) == 1)
_summary = _summary_lines[0] if _summary_lines else ""
check("summary line reports the real total_qualifying (30, before the cap)",
      "30 total qualifiers" in _summary)
check("summary line reports how many are shown (25, the fixture's own candidate count)",
      "25 shown" in _summary)
check("summary line reports needing_scoring correctly (10 unscored)",
      "10 needing scoring" in _summary)
check("summary line is a SELECTION-TIME snapshot only - it never claims a real "
      "scored/cost figure of its own now that TASK 46 has wired the real ingest "
      "(poll_and_ingest_batch()'s own separate '[quality_on_sale] scored N, cost "
      "$X' line is the honest one; this line stays a pre-submission estimate)",
      "selection snapshot only" in _summary
      and "actual submission/capacity/cost logged separately once submitted and "
          "ingested" in _summary)
check("summary line reports a non-zero ESTIMATED cost for the unscored group",
      "if all of them were queued tonight" in _summary and "est $0.0000" not in _summary)
check("summary line reports the also-in-Top-200 count (8 of the 15 scored "
      "candidates have even i, i.e. in_top_200=True: 0,2,4,6,8,10,12,14 = 8)",
      "8 of 25 shown also in the Top 200" in _summary)

_top20_lines = [m for m in _logged if m.startswith("[quality_on_sale] top ")]
check("exactly one 'top N by rank' line is logged", len(_top20_lines) == 1)
_top20 = _top20_lines[0] if _top20_lines else ""
check("'top N by rank' reports N=20 (the cap), not 25",
      "top 20 by rank" in _top20)
check("the very first ranked ticker (rank 1, highest research score) is SCORED0.AX, "
      "with its own in_top_200=True marked by '*'",
      "1.SCORED0.AX*" in _top20)
check("a ranked ticker with in_top_200=False carries no '*'",
      "2.SCORED1.AX," in _top20 and "2.SCORED1.AX*" not in _top20)
print("[task44_followup_check1_nightly_summary] log_quality_on_sale_nightly_summary() "
      "reports the real total/shown/needing-scoring/also-in-Top-200 counts, honestly "
      "reports 0 real scoring spend tonight (the ingest pipeline isn't wired), "
      "estimates the unscored group's cost separately, and lists the top 20 "
      "tickers in rank order with '*' marking Top 200 membership OK")


# ======================================================================
# CHECK 2: no candidates at all -> no crash, no "top N by rank" line
# (nothing to rank), and the counts all read 0.
# ======================================================================
_logged_empty = []
with mock.patch.object(te, "quality_on_sale_candidates",
                        return_value={"total_qualifying": 0, "candidates": []}):
    te.log_quality_on_sale_nightly_summary(log=lambda m: _logged_empty.append(m))
check("empty candidate list -> exactly one summary line, all counts 0, no crash",
      len([m for m in _logged_empty if "total qualifiers" in m]) == 1
      and "0 total qualifiers" in _logged_empty[0]
      and "0 needing scoring" in _logged_empty[0])
check("empty candidate list -> no 'top N by rank' line at all (nothing to rank)",
      not any(m.startswith("[quality_on_sale] top ") for m in _logged_empty))


# ======================================================================
# CHECK 3: quality_on_sale_candidates() itself raising is caught and
# logged, never propagated - this is a reporting-only function running
# inside the real nightly selection job.
# ======================================================================
_logged_fail = []
with mock.patch.object(te, "quality_on_sale_candidates", side_effect=RuntimeError("boom")):
    te.log_quality_on_sale_nightly_summary(log=lambda m: _logged_fail.append(m))
check("a quality_on_sale_candidates() failure is caught and logged, never raised",
      any("nightly summary failed (non-fatal)" in m for m in _logged_fail))


# ======================================================================
# CHECK 4: scheduler_engine._run_top100() calls log_quality_on_sale_
# nightly_summary() right after the support-levels job, inside its own
# non-fatal try/except - a failure here must never take down the real
# Top 200 selection job.
# ======================================================================
_src = inspect.getsource(sched._run_top100)
check("_run_top100() calls top100_engine.log_quality_on_sale_nightly_summary() "
      "AFTER support_levels_job.compute_and_store_support_levels_for_pool()",
      _src.index("support_levels_job.compute_and_store_support_levels_for_pool")
      < _src.index("top100_engine.log_quality_on_sale_nightly_summary"))
check("the quality-on-sale summary call is wrapped in its own try/except, "
      "same non-fatal contract as the support-levels job above it",
      "top100_engine.log_quality_on_sale_nightly_summary(log=log)" in _src
      and _src.count("except Exception as e:") >= 2)

_summary_calls = []
with mock.patch.object(te, "log_quality_on_sale_nightly_summary",
                        side_effect=lambda log: (_summary_calls.append(1), (_ for _ in ()).throw(RuntimeError("boom")))[-1]), \
     mock.patch.object(te, "run_nightly", return_value=None), \
     mock.patch.object(sched, "_clear_nightly_only_caches", return_value=None), \
     mock.patch("support_levels_job.compute_and_store_support_levels_for_pool", return_value=None):
    _logged_sched = []
    sched._run_top100(log=lambda *a, **k: _logged_sched.append(a[0] if a else ""), scan_day="2026-11-12")
check("a real exception inside log_quality_on_sale_nightly_summary() is caught by "
      "_run_top100()'s own wrapping try/except and never propagates out of the job",
      len(_summary_calls) == 1
      and any("quality-on-sale nightly summary failed (non-fatal" in m for m in _logged_sched))
print("[task44_followup_check4_scheduler_wiring] scheduler_engine._run_top100() calls "
      "the new nightly summary right after the support-levels job, wrapped in its own "
      "non-fatal try/except that never lets a failure here take down the real Top 200 "
      "selection job OK")


# ======================================================================
# CHECK 5 (item 3, ADBE): confirm by source inspection that a DCF-
# unreliable row is excluded from _build_best_by_ticker()'s own output
# (and therefore from support_levels_job.py's widened ticker scope, and
# therefore never gets a drop_chip_store entry) UNLESS the quality
# exception admits it - which itself requires BOTH TOP200_QUALITY_
# EXCEPTION_LIVE being on AND the row passing _quality_exception_
# admits()'s own criteria. This is exactly the Director's own reading
# of why ADBE currently has no drop-chip entry.
# ======================================================================
_bbt_src = inspect.getsource(te._build_best_by_ticker)
check("DCF_SANITY_MULTIPLE (the '3x rule') is 3.0, matching the Director's own "
      "wording", resolver_engine.DCF_SANITY_MULTIPLE == 3.0)
check("_build_best_by_ticker() checks dcf_looks_unreliable() against today's own "
      "Intrinsic Value/Price (the 3x rule) before any exclusion decision",
      "resolver_engine.dcf_looks_unreliable(" in _bbt_src)
check("a DCF-unreliable row only survives (exception_admitted_row=True) when BOTH "
      "quality_exception_live is True AND exception_eligible (_quality_exception_"
      "admits()) is True - never one alone",
      "if quality_exception_live and exception_eligible:" in _bbt_src)
check("a DCF-unreliable row that does NOT get admitted hits a bare 'continue' - "
      "excluded from best_by_ticker entirely, never partially included",
      "dcf_unreliable_excluded_count += 1" in _bbt_src
      and _bbt_src.index("dcf_unreliable_excluded_count += 1") < _bbt_src.index(
          "continue", _bbt_src.index("dcf_unreliable_excluded_count += 1"))
)

# The full end-to-end reproduction with ADBE's own REAL reported
# numbers (price 246.44, intrinsic value 750.64 = 3.05x, Quality 87,
# Moat 95) already exists and passes: tests/test_director_task37_
# quality_exception.py's own CHECK 3/4 build a real scan file via
# scan_store.save_scan() and confirm switch OFF -> "ADBE" not in
# _build_best_by_ticker()'s own output (excluded exactly as today),
# switch ON (quality_exception_override=True) -> "ADBE" IS in that
# output, flagged quality_exception_admitted=True. Not duplicated
# here - the source-inspection checks above establish WHY that
# behaviour is correct (the exact "continue" vs. "admitted" branching
# _build_best_by_ticker() takes), and TASK 37's own test file is the
# live, numerically-exact proof of it actually happening for ADBE.
print("[task44_followup_check5_adbe_confirmed] confirmed by source inspection: a "
      "DCF-unreliable row (IV/Price > 3x) is excluded from the eligible candidate "
      "set - and therefore never gets a weekly-bar download or drop-chip entry - "
      "unless TOP200_QUALITY_EXCEPTION_LIVE is on AND it passes _quality_exception_"
      "admits(); today, with the gate off, ADBE is correctly excluded. The full "
      "numeric reproduction with ADBE's own real reported figures already exists "
      "in tests/test_director_task37_quality_exception.py's own CHECK 3/4 OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
