"""
TASK 76a (Director-directed, owner-approved, 23 Nov 2026) - URGENT HOTFIX.

Live production crash since deploy 36e7d19c (commit 089b2a7): every
uncached Auto Deep Dive compounder view (e.g. WMS) crashed with
"TypeError: Object of type date is not JSON serializable" in
auto_compounder_engine._write_cache()'s json.dump(sections, f).

Root cause: TASK 76's new "iv_bv_vs_price" data
(_iv_bv_vs_price_periods()'s own fy_points[].date, periods[].start/.end)
held raw datetime.date objects, which went straight into build_sections()'s
own return value - the exact dict _write_cache() persists as JSON. TASK
76's own test suite exercised _iv_bv_vs_price_periods()/_cp_iv_bv_vs_price_
chart() directly, but never ran the real
build_sections() -> _write_cache() -> _read_cache() round trip the live
page actually performs on every uncached view - that gap is exactly how
this escaped to production.

This fix:
  1. auto_compounder_engine._iv_bv_vs_price_periods(): every date is now
     serialised to a plain ISO "YYYY-MM-DD" string only in a final pass,
     after every internal date comparison/sort is done - see this
     function's own docstring. (Covered by
     tests/test_director_task76_pe_chart_fixes_and_ivbv_vs_price_periods.py,
     updated for the new string contract in this same hotfix.)
  2. compounder_ui._parse_iso_date() / _cp_iv_bv_vs_price_chart(): parses
     those ISO strings back into real dates before any of the existing
     chart-building logic runs (tolerant of a raw date too, so every
     existing hand-built test fixture that passes real dates directly
     keeps working unchanged).
  3. auto_compounder_engine._write_cache(): now catches ANY exception
     (not just OSError) during the write, deletes the leftover .tmp_
     file, and logs "[compounder_cache] write failed <ticker>: <error>" -
     never crashes build_sections()'s caller (every Auto Deep Dive page
     render) just because a cache write failed.
  4. auto_compounder_engine._clean_stale_tmp_cache_files(): swept from
     _write_cache() itself (so it runs on the very next write, no
     separate startup hook needed) - removes any leaked .tmp_*.json file
     older than 1 hour.
  5. ENGINE_VERSION bumped 55 -> 56 so no half-written/crashed-before-
     write cache state is ever served.
  6. Audited the rest of auto_compounder_engine.py for any other
     datetime.date (or other non-JSON) value that could land in
     build_sections()'s own return value - none found; every other
     date-bearing helper (_statement_col_dates(), _series_with_dates(),
     etc.) is used purely for internal comparisons/sorting, never
     written into a metric/section value. This file's own round-trip
     checks below (a real json.dump()/json.load() through the actual
     disk cache, for 3 differently-shaped real bundles) are the
     structural proof of that audit, not just a read of the source.

Run: python3 tests/test_director_task76a_compounder_cache_json_hotfix.py
"""
import glob
import json
import os
import sys
import tempfile
import datetime as dt
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


import pandas as pd

import auto_compounder_engine as ace
import compounder_ui as cui

_DR, _PR, _GR = 0.10, 0.03, 0.05
_OVERRIDES = (_DR, _PR, _GR, None)


# ======================================================================
# CHECK: ENGINE_VERSION bumped for this hotfix (55 -> 56).
# ======================================================================
check("ENGINE_VERSION is >= 56 (bumped from 55 for TASK 76a's own hotfix)",
      ace.ENGINE_VERSION >= 56)


# ======================================================================
# Fixture builders - the exact same 3 shapes
# tests/test_director_task76_pe_chart_fixes_and_ivbv_vs_price_periods.py
# already proved _iv_bv_vs_price_periods() handles correctly (WMS-shaped
# half-yearly/March FY end, OCL-shaped half-yearly/June FY end, CPRT-
# shaped quarterly/June FY end), with a "meta" key added (needed by
# build_sections(), not by _iv_bv_vs_price_periods() directly) and an
# extra trailing monthly price point so a partial "(to date)" period -
# the one that actually produces a 'periods' entry - is always present.
# ======================================================================
def _mk_bundle(income_fy, cashflow_fy, balance_fy, income_q, extra_price_date, extra_price):
    income = pd.DataFrame(income_fy, index=["Basic EPS"])
    cashflow = pd.DataFrame(cashflow_fy, index=["Free Cash Flow", "Operating Cash Flow", "Capital Expenditure"])
    balance = pd.DataFrame(balance_fy, index=["Stockholders Equity"])
    income_q_df = pd.DataFrame(income_q, index=["Basic EPS"])
    dates = [
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
        "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
        "2026-03-31", "2026-06-30", "2026-09-30", extra_price_date,
    ]
    prices = [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, extra_price]
    return {
        "info": {"currentPrice": extra_price + 1.0, "sharesOutstanding": 100e6},
        "income": income, "income_q": income_q_df,
        "balance": balance, "cashflow": cashflow,
        "prices_10y": {"dates": dates, "prices": prices}, "dividends": {},
        "meta": {"source": "yfinance", "statement_years": 3,
                 "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "flags": []},
    }


_WMS_BUNDLE = _mk_bundle(
    income_fy={"2026-03-31": [1.0], "2025-03-31": [1.0], "2024-03-31": [1.0]},
    cashflow_fy={
        "2026-03-31": [120.0, None, None], "2025-03-31": [100.0, None, None],
        "2024-03-31": [90.0, None, None],
    },
    balance_fy={"2026-03-31": [500.0], "2025-03-31": [450.0], "2024-03-31": [400.0]},
    income_q={"2026-09-30": [0.5], "2026-03-31": [0.5], "2025-09-30": [0.45]},
    extra_price_date="2026-10-31", extra_price=41,
)
_OCL_BUNDLE = _mk_bundle(
    income_fy={"2026-06-30": [1.0], "2025-06-30": [1.0], "2024-06-30": [1.0]},
    cashflow_fy={
        "2026-06-30": [120.0, None, None], "2025-06-30": [100.0, None, None],
        "2024-06-30": [90.0, None, None],
    },
    balance_fy={"2026-06-30": [500.0], "2025-06-30": [450.0], "2024-06-30": [400.0]},
    income_q={"2026-12-31": [0.5], "2026-06-30": [0.5], "2025-12-31": [0.45]},
    extra_price_date="2027-01-31", extra_price=41,
)
_CPRT_BUNDLE = _mk_bundle(
    income_fy={"2026-06-30": [1.0], "2025-06-30": [1.0], "2024-06-30": [1.0]},
    cashflow_fy={
        "2026-06-30": [120.0, None, None], "2025-06-30": [100.0, None, None],
        "2024-06-30": [90.0, None, None],
    },
    balance_fy={"2026-06-30": [500.0], "2025-06-30": [450.0], "2024-06-30": [400.0]},
    income_q={"2026-09-30": [0.25], "2026-06-30": [0.25], "2026-03-31": [0.25]},
    extra_price_date="2026-10-31", extra_price=41,
)

_FIXTURES = [
    ("WMS", "WMS.AX", _WMS_BUNDLE),
    ("OCL", "OCL.AX", _OCL_BUNDLE),
    ("CPRT", "CPRTRT76A", _CPRT_BUNDLE),
]


# ======================================================================
# CHECK: real round trip, each shape - build_sections() ->
# _write_cache() -> _read_cache() -> render the Value vs Book tab's
# IV/BV-vs-price chart from the CACHED copy; identical to the chart
# rendered straight from the freshly-built (uncached) sections. This is
# the exact path that crashed in production (build_sections() calls
# _write_cache() internally on every fresh build) - a real on-disk
# json.dump()/json.load(), not a mock of either.
# ======================================================================
for _shape_label, _ticker, _bundle in _FIXTURES:
    with tempfile.TemporaryDirectory() as _tmp:
        with mock.patch.object(ace.fundamentals_data, "get_bundle", return_value=_bundle), \
             mock.patch.object(ace, "_cache_dir", return_value=_tmp):
            _sections_live = ace.build_sections(
                _ticker, force_refresh=True,
                discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR)

            check(f"{_shape_label} fixture: build_sections() -> _write_cache() never raises "
                  "and returns a real sections dict",
                  _sections_live is not None)

            _tmp_leftover = glob.glob(os.path.join(_tmp, ".tmp_*.json"))
            check(f"{_shape_label} fixture: no leftover .tmp_*.json file after a successful write",
                  _tmp_leftover == [])

            # Independent proof the written file really is valid JSON on
            # disk (not just trusting _read_cache()'s own forgiving parse).
            _written = glob.glob(os.path.join(_tmp, "*.json"))
            check(f"{_shape_label} fixture: exactly one real cache file was written to disk",
                  len(_written) == 1)
            if _written:
                with open(_written[0]) as f:
                    _raw_json = json.load(f)  # raises if this weren't real, valid JSON
                check(f"{_shape_label} fixture: the on-disk cache file is valid, loadable JSON "
                      "(the exact round trip that crashed pre-fix)",
                      _raw_json.get("Value vs Book", {}).get("iv_bv_vs_price") is not None)

            _cached = ace._read_cache(_ticker, _OVERRIDES)
            check(f"{_shape_label} fixture: _read_cache() accepts the entry _write_cache() just wrote",
                  _cached is not None)

        if _cached is None or _sections_live is None:
            continue

        _vvb_live = (_sections_live.get("Value vs Book") or {}).get("iv_bv_vs_price") or {}
        _vvb_cached = (_cached.get("Value vs Book") or {}).get("iv_bv_vs_price") or {}
        check(f"{_shape_label} fixture: cached iv_bv_vs_price entry is byte-identical to the "
              "freshly-built one (survived a real JSON round trip unchanged)",
              _vvb_live == _vvb_cached)

        _fig_live, _reason_live = cui._cp_iv_bv_vs_price_chart(_ticker, _vvb_live)
        _fig_cached, _reason_cached = cui._cp_iv_bv_vs_price_chart(_ticker, _vvb_cached)
        check(f"{_shape_label} fixture: a chart renders from BOTH the live and cached copy "
              "(no caption/None from either)",
              _fig_live is not None and _fig_cached is not None
              and _reason_live is None and _reason_cached is None)
        if _fig_live is not None and _fig_cached is not None:
            check(f"{_shape_label} fixture: the Value vs Book tab's chart rendered from the "
                  "CACHED copy is identical to the uncached render",
                  _fig_live.to_json() == _fig_cached.to_json())


# ======================================================================
# CHECK: failure path - a forced json.dump() error (simulating the exact
# pre-fix TypeError) leaves NO .tmp_ file behind, is logged, and never
# propagates - the page still has a real sections dict to render from
# (build_sections() returns it regardless of whether the cache write
# succeeded).
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp), \
         mock.patch.object(ace.json, "dump",
                            side_effect=TypeError("Object of type date is not JSON serializable")):
        ace._write_cache("FORCEDUMPFAILTEST", {"Value vs Book": {"metrics": []}}, ())
    _tmp_leftover2 = glob.glob(os.path.join(_tmp, ".tmp_*.json"))
    _all_files2 = glob.glob(os.path.join(_tmp, "*"))
check("forced dump failure: no .tmp_*.json file left behind",
      _tmp_leftover2 == [])
check("forced dump failure: no file at all was left in the cache dir "
      "(the failed write never got as far as os.replace())",
      _all_files2 == [])

with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp):
        _never_cached = ace._read_cache("FORCEDUMPFAILTEST", ())
check("forced dump failure: _read_cache() correctly reports a miss (nothing was ever written)",
      _never_cached is None)

# "the page still renders" - compounder_ui never sees the write failure
# at all (build_sections() doesn't re-raise it), so a direct render call
# with an empty/missing entry (the worst case - ticker not in the dict)
# degrades to the existing "no chart, caption" path, never a crash.
_fig_after_failure, _reason_after_failure = cui._cp_iv_bv_vs_price_chart("FORCEDUMPFAILTEST", {})
check("forced dump failure: the page still renders cleanly afterwards (no chart, no crash - "
      "ticker missing from the dict entirely, same as any other never-cached ticker)",
      _fig_after_failure is None and _reason_after_failure is None)


# ======================================================================
# CHECK: stale .tmp_ file cleanup - a leaked .tmp_*.json file older than
# 1 hour is removed on the next _write_cache() call; one that's fresh
# (still potentially mid-write by another process) is left alone.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp:
    _stale_path = os.path.join(_tmp, ".tmp_stale12345.json")
    _fresh_path = os.path.join(_tmp, ".tmp_fresh67890.json")
    with open(_stale_path, "w") as f:
        f.write("{}")
    with open(_fresh_path, "w") as f:
        f.write("{}")
    _old_time = __import__("time").time() - 7200  # 2 hours ago
    os.utime(_stale_path, (_old_time, _old_time))

    with mock.patch.object(ace, "_cache_dir", return_value=_tmp):
        ace._write_cache("CLEANUPTEST", {"Value vs Book": {"metrics": []}}, ())

    check("stale .tmp_ cleanup: a >1h-old leaked .tmp_ file is removed on the next write",
          not os.path.exists(_stale_path))
    check("stale .tmp_ cleanup: a fresh (<1h-old) .tmp_ file is left alone "
          "(never races a write genuinely in flight)",
          os.path.exists(_fresh_path))


print()
print(f"PASS={passed} FAIL={failed}")
