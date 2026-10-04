"""
Director addendum 2, Part 2 of 2, item C (3 Oct 2026) - private rows in
score_history. Live log: "[nightly_scan] FTSE 100: recorded 100 rows to
score_history" (also FTSE 250: 250, TSX 60: 59, TSX Composite: 219).
Same rules as the Lists & display instruction. HOLD ALL PUSHES still
applies.

Reader inventory (full detail in the final report; summary here):
score_history.py has NO universe column of its own (see its own
delete_bad_price_rows() docstring), so every reader's privacy decision
has to happen at the CALLER, keyed off the ticker alone via the new
scan_store.is_private_only_ticker() helper. Three public, unauthenticated
readers were found with no privacy check at all and are fixed here:
  1. server.py's /track-record route (score_history.tracked_summary()).
  2. calendar_render.build_entries() (/calendar) - before_value_score/
     after_value_score are read from score_history via results_store/
     results_engine, with the ticker/dates themselves left alone (those
     aren't private - an earnings date isn't score_history data).
  3. app.py's Deep Dive score-history caption/chart
     (_render_score_history_caption/_render_score_history_chart).
snapshot_render.py's /s/<ticker> reads are already protected upstream
(a private-only universe's scan never reaches snapshot_store.
build_snapshots_from_scan() - see scheduler_engine.py/nightly_scan.py's
own "must never produce a public /s/<ticker> page" comments) so no
change was needed there. Internal engine (alert_engine.py), admin-only
(valuation_audit_engine.py), and personal-email (digest_engine.py/
weekly_brief_engine.py) readers don't expose data to the public the way
the three above do, so they are intentionally left unchanged.

NO row is deleted from score_history anywhere in this fix - every
change here is read-side filtering of what a PUBLIC page displays.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_addendum2_part2c_private_score_history.py
"""
import asyncio
import os
import sys
import tempfile
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="addendum2_part2c_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import scan_store


def _save(universe, tickers):
    rows = [{"Ticker": t} for t in tickers]
    scan_store.save_scan(universe, rows, source_label="test fixture (live)")


# ======================================================================
# C1: scan_store.is_private_only_ticker() - the shared gate.
# ======================================================================
_save("FTSE 100", ["AZN.L", "SHEL.L"])       # private by code default
_save("S&P 500", ["AAPL", "MSFT"])           # public
_save("ASX 200", ["SHEL.L", "BHP.AX"])       # public - shares AZN.L? no, SHEL.L dual-listed fixture

assert scan_store.is_private_only_ticker("AZN.L") is True, \
    "AZN.L only in private FTSE 100 fixture -> private-only"
assert scan_store.is_private_only_ticker("SHEL.L") is False, \
    "SHEL.L is in BOTH private FTSE 100 and public ASX 200 fixtures -> not private-only"
assert scan_store.is_private_only_ticker("AAPL") is False, \
    "AAPL only in public S&P 500 fixture -> not private-only"
assert scan_store.is_private_only_ticker("NEVER.SCANNED") is False, \
    "a ticker in no saved scan at all -> can't confirm private, so not private-only"
print("[C1_is_private_only_ticker] private-only / dual-listed / public-only / "
      "never-scanned all classified correctly OK")


# ======================================================================
# C2: server.py's /track-record route filters tracked_summary() rows by
# is_private_only_ticker() before handing them to the renderer.
# ======================================================================
import server


class _FakeRequest:
    url = type("u", (), {"path": "/track-record"})()
    headers = {}


async def _run_track_record():
    _fake_rows = [
        {"ticker": "AZN.L", "first_day": "2026-07-01", "first_score": 50.0,
         "first_price": 100.0, "last_day": "2026-10-01", "last_score": 55.0,
         "last_price": 105.0, "days_span": 92},
        {"ticker": "AAPL", "first_day": "2026-07-01", "first_score": 60.0,
         "first_price": 200.0, "last_day": "2026-10-01", "last_score": 65.0,
         "last_price": 210.0, "days_span": 92},
    ]
    with mock.patch.object(server.score_history, "tracked_summary", return_value=_fake_rows), \
         mock.patch.object(server, "_count_view"), \
         mock.patch.object(server.track_record_render, "render_track_record") as _render:
        _render.return_value = "<html></html>"
        # track_record() is a plain def now ("one slow page must never
        # freeze the whole site", 4 Oct 2026) - called directly, not
        # awaited.
        server.track_record(_FakeRequest())
        passed_rows = _render.call_args[0][0]
    tickers_shown = {r["ticker"] for r in passed_rows}
    return tickers_shown


_shown = asyncio.run(_run_track_record())
assert "AZN.L" not in _shown, _shown
assert "AAPL" in _shown, _shown
print(f"[C2_track_record_route_filters_private] render_track_record() received "
      f"{_shown!r} - AZN.L (private-only) dropped, AAPL (public) kept OK")


# ======================================================================
# C3: calendar_render.build_entries() nulls before/after_value_score for
# a private-only ticker, but leaves the ticker/date/has_event alone
# (those aren't score_history data).
# ======================================================================
import calendar_render
import results_store
import snapshot_store

results_store.upsert_earnings_dates("AZN.L", last_report_date="2026-09-15", next_report_date=None)
results_store.upsert_earnings_dates("AAPL", last_report_date="2026-09-15", next_report_date=None)
results_store.upsert_event(
    "AZN.L", "2026-09-15",
    before={"value_score": 50.0}, after={"value_score": 55.0}, what_moved=[],
)
results_store.upsert_event(
    "AAPL", "2026-09-15",
    before={"value_score": 60.0}, after={"value_score": 65.0}, what_moved=[],
)

with mock.patch.object(snapshot_store, "get_snapshot", return_value=None):
    _entries = calendar_render.build_entries(tickers=["AZN.L", "AAPL"])

_by_ticker = {e["ticker"]: e for e in _entries if e["status"] == "reported"}
assert _by_ticker["AZN.L"]["before_value_score"] is None, _by_ticker["AZN.L"]
assert _by_ticker["AZN.L"]["after_value_score"] is None, _by_ticker["AZN.L"]
assert _by_ticker["AZN.L"]["has_event"] is True, _by_ticker["AZN.L"]  # date/event existence untouched
assert _by_ticker["AAPL"]["before_value_score"] == 60.0, _by_ticker["AAPL"]
assert _by_ticker["AAPL"]["after_value_score"] == 65.0, _by_ticker["AAPL"]
print("[C3_calendar_nulls_private_scores] AZN.L (private-only) keeps its earnings "
      "date/has_event but before/after_value_score are nulled; AAPL (public) "
      "keeps its real scores OK")


# ======================================================================
# C4: app.py's Deep Dive score-history caption/chart silently render
# nothing for a private-only ticker (same "silently do nothing"
# convention as "no history yet"), without even reading score_history.
# ======================================================================
import app as sdd_app

_caption_calls = []
with mock.patch.object(sdd_app.st, "caption", side_effect=lambda m: _caption_calls.append(m)), \
     mock.patch.object(sdd_app.score_history, "get") as _get_mock:
    sdd_app._render_score_history_caption("AZN.L", current_score=55.0)
assert _caption_calls == [], _caption_calls
assert not _get_mock.called, "score_history.get() should never be reached for a private-only ticker"
print("[C4_deepdive_caption_gated] _render_score_history_caption('AZN.L', ...) renders "
      "nothing and never even calls score_history.get() for a private-only ticker OK")

_series_calls = []
with mock.patch.object(sdd_app.score_history, "series") as _series_mock:
    sdd_app._render_score_history_chart("AZN.L")
assert not _series_mock.called, "score_history.series() should never be reached for a private-only ticker"
print("[C4_deepdive_chart_gated] _render_score_history_chart('AZN.L') never even calls "
      "score_history.series() for a private-only ticker OK")


print("\nALL Director addendum 2, Part 2, item C (private rows in score_history) CHECKS PASSED")
