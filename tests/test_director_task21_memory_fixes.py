"""
TASK 21 (Director-directed, 25 Oct 2026) - stop the nightly memory
staying high. Ranked by saving, per the Director's own 7-day Railway
[mem] evidence (fresh restart streamlit 0.14GB + fastapi 0.25GB;
nightly fastapi climbs 0.31GB -> 1.19GB across the post-scan stages,
held until the 23:00 selection trim -> 0.65-0.70GB, held ALL DAY until
the next deploy; streamlit grows 0.5-0.66GB and never shrinks; 70
@st.cache_data(ttl=86400) functions app-wide had no max_entries).

Covers the task's own 5 items + its own test list:
  1. POST-SCAN STAGES: _log_mem/_malloc_trim_and_log after EACH of
     sector top-up, each attention top-up universe, the alert extra
     pass, send_batched_notifications, and each reprice-pass universe
     (the stage root-caused as the largest single jump - see
     nightly_scan.reprice_universe()'s own TASK 21 comment). reprice_
     universe() itself frees each chunk's downloaded history
     explicitly (del + gc.collect()) before the next chunk.
  2. DAILY RETENTION: every uncapped @st.cache_data in scanner_engine.py
     (and every other module in the repo) now has max_entries, sized to
     its own key shape; nightly-only caches (constituent fetchers,
     attention-top-up signal fetchers) are cleared + trimmed at the end
     of the nightly job, the 23:00 Top 200 selection job, and the
     ~00:00 batch-ingest poll.
  3. STREAMLIT GROWTH: app.py's uncapped per-ticker/per-portfolio
     caches now have max_entries (_stress_history_bundle: 50, per the
     Director's own instruction); a background thread in the Streamlit
     process itself trims via malloc_trim when no session has been
     active for 10+ minutes, checked on the same 30-minute cadence the
     FastAPI side already uses.
  5. One "[mem] daily peak fastapi=X streamlit=Y" summary line per UTC
     day, at 00:05.
  (item 4, the deploy-peak investigation, is reporting-only - no code
  to test.)

ALL FIXTURES ARE SYNTHETIC/MOCKED - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task21_memory_fixes.py
"""
import ast
import glob
import os
import sys
import time
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler_engine as sched
import nightly_scan
import scanner_engine

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


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ======================================================================
# CHECK 1 (item 2): every @st.cache_data decorator in every .py file in
# the repo has max_entries - an AST-based scan (catches every call
# shape, including multi-arg decorators the earlier source-text-regex
# based test_memory_round2_cache_max_entries.py's own check also
# covers in a simpler form).
# ======================================================================
_missing = []
for _path in glob.glob(os.path.join(REPO_ROOT, "*.py")):
    with open(_path, encoding="utf-8") as _f:
        _src = _f.read()
    _tree = ast.parse(_src, filename=_path)
    for _node in ast.walk(_tree):
        if not isinstance(_node, ast.Call):
            continue
        _func = _node.func
        _is_cache_data = (
            isinstance(_func, ast.Attribute) and _func.attr == "cache_data"
            and isinstance(_func.value, ast.Name) and _func.value.id == "st"
        )
        if _is_cache_data and not any(kw.arg == "max_entries" for kw in _node.keywords):
            _missing.append((os.path.basename(_path), _node.lineno))
check("every @st.cache_data(...) call in every top-level .py file has max_entries "
      "(AST scan - catches every decorator call shape, not just a single-line regex)",
      _missing == [])
print(f"[task21_c1_ast_cache_scan] {len(_missing)} missing (expect 0) OK")

# ======================================================================
# CHECK 2 (item 2): scanner_engine.py's zero-arg constituent fetchers
# are capped at 4 (a cosmetic constant - zero args means exactly one
# possible cache key regardless of the number chosen); the one
# genuinely-keyed function (compute_sector_heat, keyed by its own
# ticker_sector_pairs argument) is capped at 50, a real per-universe
# bound.
# ======================================================================
import inspect

_src_scanner = inspect.getsource(scanner_engine)
_cache_decorator_re_src = _src_scanner
import re as _re
_fetch_names = ("fetch_sp500", "fetch_asx200", "fetch_nikkei225", "fetch_omxs30", "fetch_dax")
for _name in _fetch_names:
    _m = _re.search(r"@st\.cache_data\(([^)]*)\)\s*\ndef " + _name + r"\(", _src_scanner)
    check(f"scanner_engine.{_name}() (zero-arg) is capped at max_entries=4",
          _m is not None and "max_entries=4" in _m.group(1))
_m2 = _re.search(r"@st\.cache_data\(([^)]*)\)\s*\ndef compute_sector_heat\(", _src_scanner)
check("scanner_engine.compute_sector_heat() (keyed by its own argument) is capped at "
      "max_entries=50 - a real per-universe bound, not a cosmetic constant",
      _m2 is not None and "max_entries=50" in _m2.group(1))
print("[task21_c2_scanner_engine_caps] zero-arg fetchers=4, compute_sector_heat=50 OK")

# ======================================================================
# CHECK 3 (item 3): app.py's _stress_history_bundle carries EXACTLY
# max_entries=50, per the Director's own explicit instruction.
# ======================================================================
with open(os.path.join(REPO_ROOT, "app.py"), encoding="utf-8") as _f:
    _app_src = _f.read()
_m3 = _re.search(r"@st\.cache_data\(([^)]*)\)\s*\ndef _stress_history_bundle\(", _app_src)
check("app.py's _stress_history_bundle() carries max_entries=50 exactly "
      "(the Director's own explicit instruction)",
      _m3 is not None and "max_entries=50" in _m3.group(1))
print("[task21_c3_stress_history_bundle_cap] max_entries=50 confirmed OK")

# ======================================================================
# CHECK 4 (item 1): reprice_universe() frees each chunk's downloaded
# history explicitly (del + gc.collect()) before the next chunk/
# universe - and still produces the exact same repriced rows as
# before (a pure performance/memory fix, no behaviour change).
# ======================================================================
import pandas as pd
import scan_store

_RICKER_ROWS = [{"Ticker": f"T{i}.AX", "Long Score": 50} for i in range(5)]
_EXISTING_SCAN = {"rows": list(_RICKER_ROWS), "attention_lite": True}

_gc_calls = {"n": 0}


def _fake_download_chunk(tickers):
    return {t: pd.DataFrame({"Close": [1.0, 2.0]}) for t in tickers}


with mock.patch.object(scan_store, "load_scan_raw", return_value=_EXISTING_SCAN), \
     mock.patch.object(nightly_scan, "_reprice_download_chunk", side_effect=_fake_download_chunk), \
     mock.patch.object(nightly_scan, "_reprice_row", side_effect=lambda row, hist, lite: dict(row, repriced=True)), \
     mock.patch.object(scan_store, "reprice_scan", side_effect=lambda universe, rows, **k: {"rows": rows}), \
     mock.patch.object(nightly_scan, "admin_metrics_store") as _mock_admin_metrics, \
     mock.patch("gc.collect", side_effect=lambda: _gc_calls.__setitem__("n", _gc_calls["n"] + 1)):
    _result = nightly_scan.reprice_universe("TEST UNIVERSE", log=lambda *a, **k: None, run_night="2026-10-25")

check("reprice_universe() calls gc.collect() at least once (one chunk here, since 5 "
      "tickers < REPRICE_CHUNK_SIZE)", _gc_calls["n"] >= 1)
check("reprice_universe() still produces the correct repriced rows (del/gc.collect() "
      "is purely a memory fix, no behaviour change)",
      _result is not None and len(_result["rows"]) == 5
      and all(r.get("repriced") for r in _result["rows"]))
print(f"[task21_c4_reprice_frees_chunk_history] gc.collect() called {_gc_calls['n']} time(s), "
      f"rows still correct OK")

# ======================================================================
# CHECK 5 (item 1): the reprice-pass loop inside _run_nightly() logs/
# trims PER UNIVERSE, not once for the whole pass - exercised with TWO
# universes left to reprice (the single-universe tests in tests/
# test_mem_cost_addendum_rss_log_lines.py and tests/test_memory_round2_
# malloc_trim.py never have anything in `to_reprice`, so this is the
# dedicated per-universe-reprice-loop proof).
# ======================================================================
_fake_payload = {"rows": [{"Ticker": "TEST.AX", "Sector": "Industrials"}], "attention_lite": False}
_mem_lines_reprice = []
_trim_lines_reprice = []

with mock.patch.object(nightly_scan, "run_universe_scan", return_value=_fake_payload), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(nightly_scan, "reprice_universe", return_value={"rows": []}), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None), \
     mock.patch.object(sched, "_log_mem", side_effect=lambda log, label, pid=None: _mem_lines_reprice.append(label)), \
     mock.patch.object(sched, "_malloc_trim_and_log", side_effect=lambda log, label: _trim_lines_reprice.append(label)):
    import alert_engine
    import snapshot_store
    import insider_engine
    with mock.patch.object(alert_engine, "snapshot_previous_values", return_value={}), \
         mock.patch.object(alert_engine, "check_universe_rows", return_value=None), \
         mock.patch.object(alert_engine, "run_extra_ticker_pass", return_value=None), \
         mock.patch.object(alert_engine, "send_batched_notifications", return_value=None), \
         mock.patch.object(snapshot_store, "build_snapshots_from_scan", return_value=None), \
         mock.patch.object(insider_engine, "refresh_universe", return_value=None):
        sched._run_nightly(
            {"universes": ["ASX 200"], "universe_cadence": {"ASX 200": {}, "S&P 500": {}, "FTSE 100": {}}},
            log=lambda *a, **k: None, run_night="2026-10-25",
        )

check("reprice pass: 'after reprice <universe>' appears for EACH of the two "
      "universes not scanned tonight (S&P 500, FTSE 100), not once for the whole pass",
      "after reprice S&P 500" in _mem_lines_reprice and "after reprice FTSE 100" in _mem_lines_reprice)
check("reprice pass: _malloc_trim_and_log() ALSO fires per universe, same labels",
      "after reprice S&P 500" in _trim_lines_reprice and "after reprice FTSE 100" in _trim_lines_reprice)
print(f"[task21_c5_reprice_pass_per_universe] mem labels={_mem_lines_reprice} OK")

# ======================================================================
# CHECK 6 (item 2): _clear_nightly_only_caches() clears every named
# scanner_engine/trends_engine/news_engine/social_engine function, and
# a missing/renamed function on either end degrades gracefully (never
# raises).
# ======================================================================
_cleared = []


class _FakeCachedFn:
    def __init__(self, name):
        self._name = name

    def clear(self):
        _cleared.append(self._name)


class _FakeModule:
    def __init__(self, names):
        for n in names:
            setattr(self, n, _FakeCachedFn(n))


_fake_scanner = _FakeModule([
    "fetch_sp500", "fetch_sp600", "fetch_nasdaq100", "fetch_nasdaq100_invesco",
    "fetch_russell2000", "fetch_asx200", "fetch_asx_listed_companies", "fetch_asx300",
    "fetch_dow30", "fetch_dividend_aristocrats", "fetch_sp400", "fetch_russell1000",
    "fetch_russell1000_wikipedia", "fetch_allords", "fetch_asx20", "fetch_asx50",
    "fetch_asx100", "fetch_ftse100", "fetch_ftse250", "fetch_tsx60", "fetch_tsxcomposite",
    "fetch_ftse100_ishares", "fetch_ftse250_ishares", "fetch_tsx60_ishares",
    "fetch_tsxcomposite_ishares", "fetch_nikkei225", "fetch_topix500", "fetch_dax",
    "fetch_cac40", "fetch_aex", "fetch_smi", "fetch_omxs30", "compute_sector_heat",
])
_fake_trends = _FakeModule(["get_google_trend_score", "get_news_trend_score"])
_fake_news = _FakeModule(["get_news_score", "get_yahoo_news_score"])
_fake_social = _FakeModule(["get_social_score"])

_fake_modules = {
    "scanner_engine": _fake_scanner, "trends_engine": _fake_trends,
    "news_engine": _fake_news, "social_engine": _fake_social,
}
with mock.patch("importlib.import_module", side_effect=lambda name: _fake_modules[name]):
    sched._clear_nightly_only_caches(lambda *a, **k: None)
check("_clear_nightly_only_caches() clears every scanner_engine constituent fetcher "
      "(32 of them) plus compute_sector_heat (33), plus the 5 attention-top-up signal "
      "fetchers (trends_engine x2, news_engine x2, social_engine x1) = 38 total",
      len(_cleared) == 38 and "fetch_sp500" in _cleared and "compute_sector_heat" in _cleared)
check("_clear_nightly_only_caches() also clears the attention-top-up signal fetchers "
      "(trends_engine x2, news_engine x2, social_engine x1)",
      all(n in _cleared for n in (
          "get_google_trend_score", "get_news_trend_score", "get_news_score",
          "get_yahoo_news_score", "get_social_score",
      )))

_err_lines = []


class _BrokenModule:
    pass


with mock.patch("importlib.import_module", side_effect=lambda name: _BrokenModule()):
    sched._clear_nightly_only_caches(lambda m, *a, **k: _err_lines.append(m))
check("_clear_nightly_only_caches() never raises when a named function doesn't exist "
      "on the (real) module - degrades to one log line per miss",
      len(_err_lines) == len(sum((n for _, n in sched._NIGHTLY_ONLY_CACHE_OWNERS), ())))
print(f"[task21_c6_clear_nightly_only_caches] {len(_cleared)} caches cleared, "
      f"missing-function case logs {len(_err_lines)} line(s) without raising OK")

# ======================================================================
# CHECK 7 (item 2): the end-of-nightly cache clear ALSO fires at the
# end of _run_top100() (23:00 selection) and inside _run_top100_poll()'s
# ingest path (~00:00), not just at the end of _run_nightly().
# ======================================================================
_clear_calls = {"n": 0}
with mock.patch.object(sched, "_clear_nightly_only_caches",
                        side_effect=lambda log: _clear_calls.__setitem__("n", _clear_calls["n"] + 1)):
    import top100_engine
    with mock.patch.object(top100_engine, "run_nightly", return_value=None):
        sched._run_top100(log=lambda *a, **k: None, scan_day="2026-10-25")
    check("_run_top100() (the 23:00 selection job) calls _clear_nightly_only_caches() once",
          _clear_calls["n"] == 1)

    _clear_calls["n"] = 0
    with mock.patch.object(top100_engine, "poll_and_ingest_batch", return_value={"scored": 0, "failed": 1}), \
         mock.patch.object(top100_engine.top100_store, "get_batch_state", return_value={"batch_id": "b1"}):
        sched._run_top100_poll(log=lambda *a, **k: None)
    check("_run_top100_poll()'s ingest path (~00:00) calls _clear_nightly_only_caches() once",
          _clear_calls["n"] == 1)

    _clear_calls["n"] = 0
    with mock.patch.object(top100_engine, "poll_and_ingest_batch", return_value=None):
        sched._run_top100_poll(log=lambda *a, **k: None)
    check("_run_top100_poll() does NOT call _clear_nightly_only_caches() on an empty/no-op poll",
          _clear_calls["n"] == 0)
print("[task21_c7_clear_at_23_00_and_00_00] end-of-nightly clear also fires at the "
      "23:00 selection job and the ~00:00 ingest path OK")

# ======================================================================
# CHECK 8 (item 3): note_streamlit_activity()/the idle-trim tick -
# trims only when idle >= 10 minutes, leaves an active session alone,
# and the background thread starts exactly once per process.
# ======================================================================
sched._streamlit_idle_trim_thread_started = False
with mock.patch.object(sched.threading, "Thread") as _mock_thread:
    sched.note_streamlit_activity()
    sched.note_streamlit_activity()  # a second rerun - must NOT start a second thread
    check("note_streamlit_activity() starts the idle-trim thread exactly once across "
          "two calls (guarded by _streamlit_idle_trim_thread_started)",
          _mock_thread.call_count == 1)
check("note_streamlit_activity() updates _streamlit_last_activity_at to a recent timestamp",
      sched._streamlit_last_activity_at is not None
      and abs(time.time() - sched._streamlit_last_activity_at) < 5)

sched._streamlit_last_activity_at = time.time() - (sched._STREAMLIT_IDLE_TRIM_AFTER_SECONDS - 60)
check("_streamlit_idle_trim_tick(): an active-enough session (idle just under the "
      "threshold) is left alone - no trim attempted",
      sched._streamlit_idle_trim_tick() is None)

sched._streamlit_last_activity_at = time.time() - (sched._STREAMLIT_IDLE_TRIM_AFTER_SECONDS + 60)
_idle_result = sched._streamlit_idle_trim_tick()
check("_streamlit_idle_trim_tick(): idle for 10+ minutes -> trims (returns the idle "
      "duration rather than None)", _idle_result is not None and _idle_result >= sched._STREAMLIT_IDLE_TRIM_AFTER_SECONDS)

sched._streamlit_last_activity_at = None
check("_streamlit_idle_trim_tick(): no activity ever recorded yet -> never trims "
      "(a freshly-booted process with no page view yet)",
      sched._streamlit_idle_trim_tick() is None)
print("[task21_c8_streamlit_idle_trim] active session left alone, idle 10+ min trims, "
      "thread starts exactly once per process OK")

# ======================================================================
# CHECK 9 (item 5): _log_mem() feeds the daily-peak trackers (self and
# by-pid), taking the max across repeated samples; _maybe_log_daily_
# peak_summary() fires once per UTC day at 00:05+, carries both
# numbers, and resets for the next day.
# ======================================================================
sched._daily_peak_self_gb = None
sched._daily_peak_child_gb = {}
# _log_mem(log, label, pid=None) reads _rss_gb() for the pid=None/self
# case and _rss_gb_for_pid(pid) only when a real pid is given - both
# need mocking here, independently.
with mock.patch.object(sched, "_rss_gb", side_effect=[0.5, 0.2]):
    sched._log_mem(lambda *a, **k: None, "sample 1")
    sched._log_mem(lambda *a, **k: None, "sample 2 (self, lower, must not reduce peak)")
check("_log_mem() tracks THIS process's own peak across repeated self-samples "
      "(0.5GB recorded, a later lower 0.2GB sample doesn't reduce it)",
      sched._daily_peak_self_gb == 0.5)

with mock.patch.object(sched, "_rss_gb_for_pid", return_value=0.3):
    sched._log_mem(lambda *a, **k: None, "child sample", pid=777)
    sched._log_mem(lambda *a, **k: None, "child sample 2", pid=777)
check("_log_mem() tracks a CHILD pid's own peak separately from self",
      sched._daily_peak_child_gb.get(777) == 0.3)

sched.set_streamlit_pid(777)
_lines9 = []
sched._maybe_log_daily_peak_summary(_lines9.append, now=datetime(2026, 10, 26, 0, 7, tzinfo=timezone.utc))
check("_maybe_log_daily_peak_summary() logs exactly one line, carrying both numbers",
      len(_lines9) == 1 and "fastapi=0.50GB" in _lines9[0] and "streamlit=0.30GB" in _lines9[0])
check("...and both peak trackers are reset for the next day",
      sched._daily_peak_self_gb is None and sched._daily_peak_child_gb == {})

_lines9b = []
sched._maybe_log_daily_peak_summary(_lines9b.append, now=datetime(2026, 10, 26, 0, 12, tzinfo=timezone.utc))
check("_maybe_log_daily_peak_summary() does NOT log a second time on the same UTC day",
      len(_lines9b) == 0)
print(f"[task21_c9_daily_peak_summary] {_lines9[0]!r} OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
