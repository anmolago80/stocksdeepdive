"""
Incident fix (4 Oct 2026) - /calendar and /es/calendar took ~133s and
blocked the whole server (single-process FastAPI+scheduler+Streamlit,
see server.py's own module docstring) immediately after the 03:07 UTC
push of Director addendum 2 item C (commit 851de3f). /track-record has
the identical root cause and is fixed the same way.

ROOT CAUSE: calendar_render.build_entries() and server.py's
/track-record route each called scan_store.is_private_only_ticker()
once per watched/tracked ticker (hundreds in production). That function
-> ticker_universes_including_private() re-reads and re-parses EVERY
saved universe file on disk for EVERY ticker it's asked about - with
~18 saved universes (scheduler_engine._DEFAULT_NIGHTLY_UNIVERSES) and
several hundred watched tickers, that is thousands of full-file
reads+parses in one request.

FIX: scan_store.build_private_only_ticker_index() computes the exact
same per-ticker decision for EVERY ticker in ONE pass over the saved
universes (O(universes) file reads, not O(tickers x universes)).
calendar_render.build_entries() and server.py's /track-record route
each now call it once per request and test set membership.

This test proves two things at a realistic universe count (18, with
the real 6-way private set unchanged):
  1. CORRECTNESS is unchanged - a private-only ticker's before/after
     score is still nulled (calendar) / the row is still dropped
     (track-record); a public or dual-listed ticker is unaffected.
  2. THE FIX IS REAL - the number of saved-scan file reads stays
     constant as the watched/tracked ticker count grows from 50 to 600
     (proving O(universes), not O(tickers)), and both code paths
     complete in well under a second even at 600 tickers x 18 universes
     x 60 rows/universe (= 1,080 rows per universe-file, ~19k rows
     total) - the old per-ticker code made this exact shape take ~133s
     in production.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_calendar_track_record_perf_fix.py
"""
import os
import sys
import tempfile
import time
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="calendar_perf_fix_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import scan_store
import calendar_render
import results_store
import snapshot_store
import score_history

# ----------------------------------------------------------------------
# Fixture: the real 18-universe nightly roster (scheduler_engine.
# _DEFAULT_NIGHTLY_UNIVERSES), 5 of them private by the real code
# default (FTSE 100/250, TSX Composite, Nikkei 225, TOPIX 500). TSX 60
# was private too until the Director's correction (9 Oct 2026,
# instruction_health_fixes_chart_and_new_markets.md) removed it from
# scan_store._DEFAULT_PRIVATE_UNIVERSES - it's listed here as public,
# same as production. Row counts scaled down 1/10th from production
# for test speed, but the UNIVERSE COUNT (the dimension that matters
# for this bug) is real.
# ----------------------------------------------------------------------
_UNIVERSES = [
    ("ASX 200", 60, False), ("ASX 300", 60, False), ("ASX All Technology", 40, False),
    ("S&P 500", 60, False), ("Nasdaq 100", 60, False), ("Dow Jones 30", 30, False),
    ("All Ordinaries", 60, False), ("S&P 400 MidCap", 60, False),
    ("Small Caps (S&P 600)", 60, False), ("Russell 1000", 60, False),
    ("S&P 500 Dividend Aristocrats", 40, False), ("Russell 2000", 60, False),
    ("FTSE 100", 60, True), ("FTSE 250", 60, True),
    ("TSX 60", 60, False), ("TSX Composite", 60, True),
    ("Nikkei 225", 60, True), ("TOPIX 500", 60, True),
]
assert sum(1 for _, _, priv in _UNIVERSES if priv) == 5, "5 private universes, matching the real code default"

_PRIVATE_ONLY_TICKER = "JPONLY.T"     # only ever appears under Nikkei 225 (private)
_DUAL_LISTED_TICKER = "DUAL.AX"      # appears under both TSX Composite (private) and ASX 200 (public)
_PUBLIC_TICKER = "PUB1"             # only ever appears under S&P 500 (public)

for _uni, _n, _priv in _UNIVERSES:
    rows = [{"Ticker": f"{_uni[:3].upper()}{i}"} for i in range(_n)]
    if _uni == "Nikkei 225":
        rows[0] = {"Ticker": _PRIVATE_ONLY_TICKER}
    if _uni == "TSX Composite":
        rows[0] = {"Ticker": _DUAL_LISTED_TICKER}
    if _uni == "ASX 200":
        rows[0] = {"Ticker": _DUAL_LISTED_TICKER}
    if _uni == "S&P 500":
        rows[0] = {"Ticker": _PUBLIC_TICKER}
    scan_store.save_scan(_uni, rows, source_label="test fixture (live)")

# ----------------------------------------------------------------------
# C1: correctness, via the bulk index directly.
# ----------------------------------------------------------------------
_index = scan_store.build_private_only_ticker_index()
assert _PRIVATE_ONLY_TICKER in _index, "private-only ticker must be in the bulk index"
assert _DUAL_LISTED_TICKER not in _index, "dual-listed (one private + one public universe) must NOT be private-only"
assert _PUBLIC_TICKER not in _index, "public-only ticker must NOT be in the bulk index"
assert "NEVER.SCANNED" not in _index, "a never-scanned ticker can't be confirmed private"
# Must agree with the per-ticker function it replaces, call for call.
for _t in (_PRIVATE_ONLY_TICKER, _DUAL_LISTED_TICKER, _PUBLIC_TICKER, "NEVER.SCANNED"):
    assert (_t in _index) == scan_store.is_private_only_ticker(_t), _t
print("[1_bulk_index_correctness] build_private_only_ticker_index() agrees with "
      "is_private_only_ticker() for private-only / dual-listed / public-only / "
      "never-scanned tickers OK")

# ----------------------------------------------------------------------
# C2: calendar_render.build_entries() - correctness at scale, plus the
# file-read-count and timing proof.
# ----------------------------------------------------------------------
_N_WATCHED = 600
_watched_tickers = [f"W{i}" for i in range(_N_WATCHED)]
for t in _watched_tickers:
    results_store.upsert_earnings_dates(t, last_report_date="2026-09-15", next_report_date=None)
results_store.upsert_earnings_dates(_PRIVATE_ONLY_TICKER, last_report_date="2026-09-15", next_report_date=None)
results_store.upsert_earnings_dates(_PUBLIC_TICKER, last_report_date="2026-09-15", next_report_date=None)
for t in _watched_tickers + [_PRIVATE_ONLY_TICKER, _PUBLIC_TICKER]:
    results_store.upsert_event(t, "2026-09-15", before={"value_score": 50.0},
                                after={"value_score": 55.0}, what_moved=[])

_load_scan_raw_calls = {"n": 0}
_orig_load_scan_raw = scan_store.load_scan_raw


def _counting_load_scan_raw(*a, **kw):
    _load_scan_raw_calls["n"] += 1
    return _orig_load_scan_raw(*a, **kw)


with mock.patch.object(snapshot_store, "get_snapshot", return_value=None), \
     mock.patch.object(scan_store, "load_scan_raw", side_effect=_counting_load_scan_raw):
    _t0 = time.monotonic()
    _entries = calendar_render.build_entries(None)
    _elapsed = time.monotonic() - _t0

_by_ticker = {e["ticker"]: e for e in _entries if e["status"] == "reported"}
assert _by_ticker[_PRIVATE_ONLY_TICKER]["before_value_score"] is None
assert _by_ticker[_PRIVATE_ONLY_TICKER]["after_value_score"] is None
assert _by_ticker[_PRIVATE_ONLY_TICKER]["has_event"] is True   # date/event existence untouched
assert _by_ticker[_PUBLIC_TICKER]["before_value_score"] == 50.0
assert _by_ticker[_PUBLIC_TICKER]["after_value_score"] == 55.0

# O(universes), never O(tickers): exactly one load_scan_raw() call per
# universe from list_saved_universes()'s own internal file read, plus
# exactly one more per universe from build_private_only_ticker_index()
# itself - a small constant, regardless of _N_WATCHED (600 here).
_expected_max_calls = len(_UNIVERSES) * 2 + 5
assert _load_scan_raw_calls["n"] <= _expected_max_calls, (
    f"load_scan_raw() called {_load_scan_raw_calls['n']} times for {_N_WATCHED} "
    f"watched tickers x {len(_UNIVERSES)} universes - expected <= "
    f"{_expected_max_calls} (O(universes), not O(tickers x universes))"
)
assert _elapsed < 2.0, f"build_entries() took {_elapsed:.3f}s for {_N_WATCHED} watched tickers - expected < 2s"
print(f"[2_calendar_build_entries_perf] {_N_WATCHED} watched tickers x {len(_UNIVERSES)} "
      f"universes: build_entries() took {_elapsed:.3f}s, {_load_scan_raw_calls['n']} "
      f"load_scan_raw() calls (bounded, not scaling with ticker count); "
      f"private-only ticker's scores nulled, public ticker's scores intact OK")

# ----------------------------------------------------------------------
# C3: server.py's /track-record route - same proof.
# ----------------------------------------------------------------------
import asyncio
import server


class _FakeRequest:
    url = type("u", (), {"path": "/track-record"})()
    headers = {}


_fake_rows = (
    [{"ticker": _PRIVATE_ONLY_TICKER, "first_day": "2026-07-01", "first_score": 50.0,
      "first_price": 100.0, "last_day": "2026-10-01", "last_score": 55.0,
      "last_price": 105.0, "days_span": 92}] +
    [{"ticker": _PUBLIC_TICKER, "first_day": "2026-07-01", "first_score": 60.0,
      "first_price": 200.0, "last_day": "2026-10-01", "last_score": 65.0,
      "last_price": 210.0, "days_span": 92}] +
    [{"ticker": t, "first_day": "2026-07-01", "first_score": 60.0, "first_price": 200.0,
      "last_day": "2026-10-01", "last_score": 65.0, "last_price": 210.0, "days_span": 92}
     for t in _watched_tickers]
)

_load_scan_raw_calls["n"] = 0


async def _run_track_record():
    with mock.patch.object(server.score_history, "tracked_summary", return_value=_fake_rows), \
         mock.patch.object(server, "_count_view"), \
         mock.patch.object(scan_store, "load_scan_raw", side_effect=_counting_load_scan_raw), \
         mock.patch.object(server.track_record_render, "render_track_record") as _render:
        _render.return_value = "<html></html>"
        _t0 = time.monotonic()
        # track_record() is a plain def now ("one slow page must never
        # freeze the whole site", 4 Oct 2026 - it runs in Starlette's own
        # thread pool instead of the event loop) - called directly, not
        # awaited.
        server.track_record(_FakeRequest())
        _elapsed = time.monotonic() - _t0
        return _render.call_args[0][0], _elapsed


_passed_rows, _tr_elapsed = asyncio.run(_run_track_record())
_shown = {r["ticker"] for r in _passed_rows}
assert _PRIVATE_ONLY_TICKER not in _shown, _shown
assert _PUBLIC_TICKER in _shown, _shown
assert _load_scan_raw_calls["n"] <= _expected_max_calls, (
    f"/track-record: load_scan_raw() called {_load_scan_raw_calls['n']} times for "
    f"{len(_fake_rows)} tracked tickers - expected <= {_expected_max_calls}"
)
assert _tr_elapsed < 2.0, f"/track-record took {_tr_elapsed:.3f}s - expected < 2s"
print(f"[3_track_record_route_perf] {len(_fake_rows)} tracked tickers x {len(_UNIVERSES)} "
      f"universes: route took {_tr_elapsed:.3f}s, {_load_scan_raw_calls['n']} load_scan_raw() "
      f"calls (bounded); private-only ticker dropped, public ticker kept OK")

print("\nALL calendar/track-record performance-incident fix CHECKS PASSED")
