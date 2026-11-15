"""
TASK 65 (Director-directed, 15 Nov 2026) - log the Top 200 decision for
watched tickers. LOGGING ONLY - no selection/scoring change.

WHY: NU (owner holding) dropped out of the Top 200 on 14 Nov 23:00 UTC.
Its live Deep Dive "Scanner & Top 200 score" is 45.4, above the cut-off
(45.9) minus the 2.0 sticky buffer (43.9), so it looks like it should
have been kept - but nothing in the logs showed the score/row actually
used for NU, so the cause couldn't be confirmed.

Covers:
  - _naive_cutoff_for(): the exact naive-cutoff formula, always
    available regardless of TOP200_STICKY_BUFFER_LIVE.
  - _watch_quality_used(): the v2/v1 swap+fallback label.
  - _build_best_by_ticker()'s new watch_tickers/watch_diag out-param -
    "seen"/exclude_reason bookkeeping for stale/DCF-unreliable/fund/
    fallback-financials/dedupe, and that every EXISTING caller (no
    watch_tickers/watch_diag) is byte-identical.
  - _log_top100_watch()/_log_top100_watch_row()/_log_top100_watch_
    excluded() - the exact line format, and every result type
    (selected / kept by buffer / dropped / excluded:<reason> / not
    scanned).
  - select_top100_pool() end to end: the real NU-shaped case, and that
    the real pool/extension CONTENT is byte-identical whether or not
    the watch log fires (logging only, never influences selection).

Run: python3 tests/test_director_task65_top100_watch_log.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task65_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_QUALITY_V2_LIVE", None)
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)
os.environ.pop("TOP200_STICKY_BUFFER_LIVE", None)
os.environ.pop("FINANCIALS_STORE_LIVE", None)

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


import nightly_scan
import ranking_engine
import scan_store
import top100_engine as te
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
# PART A: _naive_cutoff_for() - the exact naive-cutoff formula.
# ======================================================================
_merit = [{"value_score": 90.0 - i} for i in range(10)]
check("[A1] naive cutoff is the n-th row's own score",
      te._naive_cutoff_for(_merit, 5) == _merit[4]["value_score"])
check("[A2] n larger than the list -> cutoff is the LAST available row's score",
      te._naive_cutoff_for(_merit, 50) == _merit[-1]["value_score"])
check("[A3] empty list -> None", te._naive_cutoff_for([], 5) is None)


# ======================================================================
# PART B: _watch_quality_used() - v2/v1 swap+fallback label.
# ======================================================================
_row_both = {"quality": 77.0, "quality_v2": 50.0}
_row_v1_only = {"quality": 77.0, "quality_v2": None}

os.environ.pop("TOP200_QUALITY_V2_LIVE", None)
check("[B1] gate off -> always v1, regardless of a present quality_v2",
      te._watch_quality_used(_row_both) == (77.0, "v1"))

os.environ["TOP200_QUALITY_V2_LIVE"] = "1"
check("[B2] gate on, quality_v2 present -> v2 value, labelled v2",
      te._watch_quality_used(_row_both) == (50.0, "v2"))
check("[B3] gate on, quality_v2 missing -> falls back to v1, labelled v1",
      te._watch_quality_used(_row_v1_only) == (77.0, "v1"))
os.environ.pop("TOP200_QUALITY_V2_LIVE", None)


# ======================================================================
# PART C: _build_best_by_ticker()'s watch_tickers/watch_diag bookkeeping.
# ======================================================================

# CHECK C1: a watched ticker that makes it through cleanly - "seen",
# no exclude_reason, and it IS in the real best_by_ticker.
_clear("S&P 500")
_save_universe("S&P 500", [_row("NU", quality=62.0, mos=49.0, psy=13.0, moat=55.0)])
_diag = {}
_best, _ = te._build_best_by_ticker(log=lambda *a, **k: None,
                                     watch_tickers={"NU"}, watch_diag=_diag)
check("[C1a] NU made it through cleanly -> in best_by_ticker",
      "NU" in _best)
check("[C1b] watch_diag marks it seen, no exclude_reason",
      _diag.get("NU") == {"seen": True, "exclude_reason": None})

# CHECK C2: stale -> excluded:stale.
_clear("S&P 500")
_save_universe("S&P 500", [_row("NU", **{"Trading Status": "stale"})])
_diag = {}
_best, _ = te._build_best_by_ticker(log=lambda *a, **k: None,
                                     watch_tickers={"NU"}, watch_diag=_diag)
check("[C2a] a stale-flagged row never reaches best_by_ticker",
      "NU" not in _best)
check("[C2b] watch_diag records 'stale'", _diag.get("NU", {}).get("exclude_reason") == "stale")

# CHECK C3: DCF-unreliable -> excluded:DCF-unreliable. Needs the
# CURRENT ratio (today's own Intrinsic Value/Price) to actually be
# unreliable, not just the stale stored flag - TASK 41 item 2 makes
# the current ratio the one true signal whenever it's computable (see
# that function's own comment).
_clear("S&P 500")
_save_universe("S&P 500", [_row("NU", **{"DCF Unreliable": True,
                                          "Intrinsic Value": 40.0, "Price": 10.0})])
_diag = {}
_best, _ = te._build_best_by_ticker(log=lambda *a, **k: None,
                                     watch_tickers={"NU"}, watch_diag=_diag)
check("[C3a] a DCF-unreliable row never reaches best_by_ticker (exception gate off)",
      "NU" not in _best)
check("[C3b] watch_diag records 'DCF-unreliable'",
      _diag.get("NU", {}).get("exclude_reason") == "DCF-unreliable")

# CHECK C4: fund -> excluded:fund.
_clear("S&P 500")
_save_universe("S&P 500", [_row("NU", **{"Is Fund": True})])
_diag = {}
_best, _ = te._build_best_by_ticker(log=lambda *a, **k: None,
                                     watch_tickers={"NU"}, watch_diag=_diag)
check("[C4a] a fund row never reaches best_by_ticker", "NU" not in _best)
check("[C4b] watch_diag records 'fund'", _diag.get("NU", {}).get("exclude_reason") == "fund")

# CHECK C5: fallback-financials -> excluded:fallback-financials (needs
# FINANCIALS_STORE_LIVE=1).
_clear("S&P 500")
_save_universe("S&P 500", [_row("NU", **{"FCF Source": "info", "Is Financials Mode": True})])
_diag = {}
os.environ["FINANCIALS_STORE_LIVE"] = "1"
_best, _ = te._build_best_by_ticker(log=lambda *a, **k: None,
                                     watch_tickers={"NU"}, watch_diag=_diag)
os.environ.pop("FINANCIALS_STORE_LIVE", None)
check("[C5a] a fallback-financials row never reaches best_by_ticker", "NU" not in _best)
check("[C5b] watch_diag records 'fallback-financials'",
      _diag.get("NU", {}).get("exclude_reason") == "fallback-financials")

# CHECK C6: not scanned at all - no row for this ticker in any eligible
# universe.
_clear("S&P 500")
_save_universe("S&P 500", [_row("OTHER")])
_diag = {}
_best, _ = te._build_best_by_ticker(log=lambda *a, **k: None,
                                     watch_tickers={"NU"}, watch_diag=_diag)
check("[C6a] NU is absent from best_by_ticker", "NU" not in _best)
check("[C6b] watch_diag never even marks it 'seen'", "NU" not in _diag)

# CHECK C7: dedupe - a watched ticker loses its own share-class pair.
_clear("S&P 500")
_save_universe("S&P 500", [
    _row("NU", long_score=50.0, **{"Average Volume": 100}),
    _row("NUFAKE2", long_score=55.0, **{"Average Volume": 500}),
])
_diag = {}
with mock.patch.object(te, "SHARE_CLASS_PAIRS", (("NU", "NUFAKE2"),)):
    _best, _ = te._build_best_by_ticker(log=lambda *a, **k: None,
                                         watch_tickers={"NU"}, watch_diag=_diag)
check("[C7a] NU lost the share-class dedupe to its higher-volume pair partner",
      "NU" not in _best and "NUFAKE2" in _best)
check("[C7b] watch_diag records 'dedupe'", _diag.get("NU", {}).get("exclude_reason") == "dedupe")

# CHECK C8: every EXISTING caller shape (watch_tickers=None, watch_diag=
# None, the default) is completely unaffected - no exception, same
# best_by_ticker contents as always.
_clear("S&P 500")
_save_universe("S&P 500", [_row("NU", quality=62.0, mos=49.0, psy=13.0)])
_best_default, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
check("[C8] default call (no watch params) still returns NU normally, "
      "no exception", "NU" in _best_default)

_clear("S&P 500")


# ======================================================================
# PART D: _log_top100_watch_row()/_log_top100_watch_excluded()/
# _log_top100_watch() - exact line format, every result type.
# ======================================================================
_logs = []


def _cap(msg):
    _logs.append(msg)


_sample_row = {
    "value_score": 45.69, "quality": 62.0, "quality_v2": None,
    "mos_pct": 49.0, "psychology": 13.0, "moat": 55.0,
    "generated_at": "2026-11-14T23:00:00+00:00", "universe": "S&P 500",
    "price": 10.5,
}

# CHECK D1: format - incumbent, selected (score >= naive_cutoff).
_logs.clear()
te._log_top100_watch_row(_cap, "NU", _sample_row, {"NU"}, 44.0, "selected")
check("[D1] line format matches the Director's own exact template",
      _logs[0] == "[top100_watch] NU score=45.69 (quality_used=62.0 v1, mos=49.0, "
                  "psy=13.0, moat=55.0) row_date=2026-11-14T23:00:00+00:00 "
                  "row_universe=S&P 500 price_used=10.5 incumbent=yes "
                  "naive_cutoff=44.0 buffer_floor=42.0 result=selected")

# CHECK D2: non-incumbent, dropped.
_logs.clear()
te._log_top100_watch_row(_cap, "NU", _sample_row, set(), 50.0, "dropped")
check("[D2] incumbent=no, result=dropped, naive_cutoff/buffer_floor still "
      "reported even though this ticker didn't make it",
      "incumbent=no" in _logs[0] and "naive_cutoff=50.0" in _logs[0]
      and "buffer_floor=48.0" in _logs[0] and "result=dropped" in _logs[0])

# CHECK D3: kept by buffer (score below naive_cutoff).
_logs.clear()
_below_cutoff_row = dict(_sample_row, value_score=43.9)
te._log_top100_watch_row(_cap, "NU", _below_cutoff_row, {"NU"}, 45.9, "kept by buffer")
check("[D3] 'kept by buffer' result renders exactly as given",
      "result=kept by buffer" in _logs[0])

# CHECK D4: naive_cutoff=None (no sticky-buffer-relevant comparison at
# all, e.g. fewer candidates than the pool target) -> buffer_floor is
# also None, never a crash on None - STICKY_BUFFER_POINTS.
_logs.clear()
te._log_top100_watch_row(_cap, "NU", _sample_row, {"NU"}, None, "selected")
check("[D4] naive_cutoff=None -> buffer_floor=None too, no exception",
      "naive_cutoff=None" in _logs[0] and "buffer_floor=None" in _logs[0])

# CHECK D5: _log_top100_watch_excluded() - "not scanned".
_logs.clear()
te._log_top100_watch_excluded(_cap, "NU", set(), {})
check("[D5] not-scanned line", _logs[0] == "[top100_watch] NU incumbent=no result=not scanned")

# CHECK D6: _log_top100_watch_excluded() - excluded:<reason>, incumbent.
_logs.clear()
te._log_top100_watch_excluded(_cap, "NU", {"NU"}, {"NU": {"seen": True, "exclude_reason": "fund"}})
check("[D6] excluded:fund line, incumbent=yes",
      _logs[0] == "[top100_watch] NU incumbent=yes result=excluded:fund")

# CHECK D7: seen but somehow no reason recorded (defensive) ->
# excluded:unknown, never a crash/blank reason.
_logs.clear()
te._log_top100_watch_excluded(_cap, "NU", set(), {"NU": {"seen": True, "exclude_reason": None}})
check("[D7] seen with no recorded reason -> excluded:unknown (defensive, never blank)",
      _logs[0] == "[top100_watch] NU incumbent=no result=excluded:unknown")

# CHECK D8: _log_top100_watch() end-to-end dispatch - one ticker in the
# main pool (selected), one dropped from main (not .AX, no extension
# context), one excluded, one not scanned, one .AX kept by the ASX
# extension's own buffer.
_watch_set = {"MAIN_SEL", "MAIN_DROP", "MAIN_EXCL", "MAIN_UNSEEN", "AU_BUFFER.AX"}
_best_by_ticker_d8 = {
    "MAIN_SEL": dict(_sample_row, value_score=80.0),
    "MAIN_DROP": dict(_sample_row, value_score=10.0),
    "AU_BUFFER.AX": dict(_sample_row, value_score=20.0, universe="ASX 200"),
}
_diag_d8 = {"MAIN_EXCL": {"seen": True, "exclude_reason": "fund"}}
_logs.clear()
te._log_top100_watch(
    _cap, _watch_set, _diag_d8, _best_by_ticker_d8,
    pool_tickers={"MAIN_SEL"},
    main_naive_cutoff=50.0,
    extension_tickers={"AU_BUFFER.AX"},
    au_naive_cutoff=30.0,
    incumbent_tickers={"AU_BUFFER.AX"},
)
_by_ticker_d8 = {ln.split(" ", 2)[1]: ln for ln in _logs}
check("[D8a] MAIN_SEL (in pool, score >= main cutoff) -> selected",
      "result=selected" in _by_ticker_d8["MAIN_SEL"])
check("[D8b] MAIN_DROP (candidate, not in pool, not .AX) -> dropped against the "
      "main cutoff", "result=dropped" in _by_ticker_d8["MAIN_DROP"]
      and "naive_cutoff=50.0" in _by_ticker_d8["MAIN_DROP"])
check("[D8c] MAIN_EXCL (never a candidate, recorded reason 'fund') -> excluded:fund",
      _by_ticker_d8["MAIN_EXCL"] == "[top100_watch] MAIN_EXCL incumbent=no result=excluded:fund")
check("[D8d] MAIN_UNSEEN (never a candidate, never even seen) -> not scanned",
      _by_ticker_d8["MAIN_UNSEEN"] == "[top100_watch] MAIN_UNSEEN incumbent=no result=not scanned")
check("[D8e] AU_BUFFER.AX (.AX, not in main pool, in the AU extension, score "
      "BELOW the AU cutoff) -> kept by buffer, against the AU cutoff "
      "(never the main one)",
      "result=kept by buffer" in _by_ticker_d8["AU_BUFFER.AX"]
      and "naive_cutoff=30.0" in _by_ticker_d8["AU_BUFFER.AX"]
      and "incumbent=yes" in _by_ticker_d8["AU_BUFFER.AX"])


# ======================================================================
# PART E: select_top100_pool() end to end - the real NU-shaped case.
# ======================================================================
_clear("S&P 500", "ASX 200")
_us_rows = [_row(f"FILLER{i}", quality=70.0, mos=40.0, psy=5.0, long_score=50.0 + i * 0.1)
            for i in range(250)]
_us_rows.append(_row("NU", quality=62.0, mos=49.0, psy=13.0, moat=55.0, long_score=55.0))
_save_universe("S&P 500", _us_rows)

_logs.clear()
te.select_top100_pool(log=_cap)
_nu_lines = [ln for ln in _logs if ln.startswith("[top100_watch] NU ")]
check("[E1] select_top100_pool() logs exactly one [top100_watch] line for NU",
      len(_nu_lines) == 1)
check("[E2] NU's real recomputed value_score, Quality (v1, gate off), MOS, "
      "Psychology and Moat all appear verbatim in the line",
      "score=" in _nu_lines[0] and "quality_used=62.0 v1" in _nu_lines[0]
      and "mos=49.0" in _nu_lines[0] and "psy=13.0" in _nu_lines[0]
      and "moat=55.0" in _nu_lines[0])
check("[E3] the line names the real stored row's own universe (S&P 500)",
      "row_universe=S&P 500" in _nu_lines[0])
check("[E4] a real naive_cutoff and buffer_floor (naive_cutoff - 2.0) are "
      "both present and numeric, never None, for a 251-candidate pool",
      "naive_cutoff=None" not in _nu_lines[0] and "buffer_floor=None" not in _nu_lines[0])

# CHECK E5 (NO BEHAVIOUR CHANGE): the real saved pool is IDENTICAL
# whether or not the watch log fires - this task is logging only.
_pool_with_watch = {r["ticker"] for r in ts.current_pool()}
_clear("S&P 500")
_save_universe("S&P 500", _us_rows)
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
with mock.patch.object(nightly_scan, "QUALITY_V2_DIAGNOSTIC_WATCHLIST", frozenset()):
    te.select_top100_pool(log=lambda *a, **k: None)
_pool_without_watch = {r["ticker"] for r in ts.current_pool()}
check("[E5] the real saved pool's ticker set is byte-identical whether the "
      "watchlist is empty or populated - purely additive logging, zero "
      "effect on selection", _pool_with_watch == _pool_without_watch)

_clear("S&P 500", "ASX 200")
_patch_sector_fill.stop()
_patch_cadence.stop()


print(f"\nTASK 65 RESULTS: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("\nALL TASK 65 (TOP 200 WATCH LOG) FIXTURES PASSED")
