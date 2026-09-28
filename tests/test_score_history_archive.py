"""
Owner decision (28 Sep 2026), "History reset - owner decision" - a one-
time, idempotent history reset for the DCF growth-fade cutover (71f183b,
committed 2026-09-28T09:44:00Z). Every score_history row dated before the
cutoff was computed with the OLD (flat-growth) DCF; the owner wants every
"over time" chart/figure to start fresh from the corrected valuation
method, WITHOUT losing the old data outright.

score_history.archive_rows_before(cutoff_day, archive_path): moves every
row with day < cutoff_day into a JSON file (merged/de-duplicated with
whatever's already there), then deletes those rows from the live table.
Returns the count moved on that call. Independently idempotent - a
second call finds nothing left to move and correctly no-ops (verified
below), no marker file needed for correctness (nightly_scan.
archive_pre_growth_fade_history() adds one anyway, purely to skip the
query on every boot after the first - not tested here, see its own
docstring).

Run: python3 tests/test_score_history_archive.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="score_history_archive_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import score_history as sh

# Fresh db for this run.
if os.path.exists(sh.DB_PATH):
    os.remove(sh.DB_PATH)

CUTOFF = "2026-09-28"
ARCHIVE_PATH = os.path.join(TESTVOL, "archive", "score_history_pre_2026-09-28.json")

# ======================================================================
# Seed rows: 3 tickers x 3 pre-cutoff days (old method) + 1 on-cutoff
# day (left alone by this cutoff - see the caller's own "day-only, can't
# tell which side of 09:44 UTC" comment) + 1 clearly-post-cutoff day.
# ======================================================================
PRE_DAYS = ["2026-09-25", "2026-09-26", "2026-09-27"]
ON_CUTOFF_DAY = "2026-09-28"
POST_DAY = "2026-09-29"

for _day in PRE_DAYS:
    sh.record(
        [{"Ticker": "AAA", "Long Score": 55.0, "Price": 10.0, "Quality": 4,
          "Moat": 3, "MOS %": 12.0, "Intrinsic Value": 11.2,
          "Valuation": "Fair value", "Moat Erosion": "Stable"},
         {"Ticker": "BBB", "Long Score": 61.0, "Price": 20.0}],
        day=_day,
    )
sh.record([{"Ticker": "AAA", "Long Score": 70.0, "Price": 11.0}], day=ON_CUTOFF_DAY)
sh.record([{"Ticker": "AAA", "Long Score": 72.0, "Price": 11.5}], day=POST_DAY)

with sh._conn() as _c:
    _total_before = _c.execute("SELECT COUNT(*) FROM score_history").fetchone()[0]
assert _total_before == 3 * 2 + 1 + 1, _total_before
print(f"[seed] {_total_before} rows seeded ({len(PRE_DAYS)} pre-cutoff days x 2 tickers, "
      "1 on-cutoff row, 1 post-cutoff row) OK")

# ======================================================================
# First call: archives exactly the 6 pre-cutoff rows (3 days x 2
# tickers), leaves the on-cutoff and post-cutoff rows live.
# ======================================================================
archived_1 = sh.archive_rows_before(CUTOFF, ARCHIVE_PATH)
assert archived_1 == 6, archived_1
print(f"[archives_pre_cutoff_rows] first call archived {archived_1} row(s) (expected 6) OK")

with sh._conn() as _c:
    _c.row_factory = None
    _remaining_days = sorted(r[0] for r in _c.execute("SELECT day FROM score_history").fetchall())
assert _remaining_days == [ON_CUTOFF_DAY, ON_CUTOFF_DAY, POST_DAY] or \
       sorted(set(_remaining_days)) == [ON_CUTOFF_DAY, POST_DAY], _remaining_days
assert all(d >= CUTOFF for d in _remaining_days), _remaining_days
assert ON_CUTOFF_DAY in _remaining_days and POST_DAY in _remaining_days
print(f"[on_and_post_cutoff_rows_untouched] live table still has the {ON_CUTOFF_DAY} and "
      f"{POST_DAY} rows - only strictly-before-cutoff rows were moved OK")

assert os.path.exists(ARCHIVE_PATH), ARCHIVE_PATH
with open(ARCHIVE_PATH, encoding="utf-8") as f:
    _archived_rows = json.load(f)
assert len(_archived_rows) == 6, len(_archived_rows)
_archived_pairs = {(r["day"], r["ticker"]) for r in _archived_rows}
_expected_pairs = {(d, t) for d in PRE_DAYS for t in ("AAA", "BBB")}
assert _archived_pairs == _expected_pairs, _archived_pairs
# Nothing was discarded - the archived AAA rows keep their real values,
# not just Ticker/day.
_aaa_2026_09_25 = next(r for r in _archived_rows if r["day"] == "2026-09-25" and r["ticker"] == "AAA")
assert _aaa_2026_09_25["long_score"] == 55.0, _aaa_2026_09_25
assert _aaa_2026_09_25["price"] == 10.0, _aaa_2026_09_25
assert _aaa_2026_09_25["mos_pct"] == 12.0, _aaa_2026_09_25
assert _aaa_2026_09_25["valuation_label"] == "Fair value", _aaa_2026_09_25
print("[archive_file_has_real_data] archive file holds the actual pre-cutoff values "
      "(long_score/price/mos_pct/valuation_label), not just ticker/day OK")

# ======================================================================
# Nothing left in score_history's own "over time" readers for pre-
# cutoff days - get()/series()/before_date() no longer find them.
# ======================================================================
assert sh.before_date("AAA", "2026-09-27") is None, sh.before_date("AAA", "2026-09-27")
_series_aaa = sh.series("AAA", limit_days=730)
assert all(r["day"] >= CUTOFF for r in _series_aaa), _series_aaa
print("[live_readers_see_no_pre_cutoff_data] before_date()/series() no longer return "
      "anything dated before the cutoff for a ticker that had pre-cutoff rows OK")

# ======================================================================
# Idempotency: second call on the same (now-cleared) live table is a
# correct no-op - archives nothing more, doesn't touch the file, doesn't
# raise, doesn't duplicate anything already archived.
# ======================================================================
_archive_mtime_before = os.path.getmtime(ARCHIVE_PATH)
import time as _time
_time.sleep(0.05)
archived_2 = sh.archive_rows_before(CUTOFF, ARCHIVE_PATH)
assert archived_2 == 0, archived_2
_archive_mtime_after = os.path.getmtime(ARCHIVE_PATH)
assert _archive_mtime_after == _archive_mtime_before, (_archive_mtime_before, _archive_mtime_after)
with open(ARCHIVE_PATH, encoding="utf-8") as f:
    _archived_rows_2 = json.load(f)
assert len(_archived_rows_2) == 6, len(_archived_rows_2)
print(f"[idempotent_second_call] second call archived {archived_2} row(s), archive file "
      "untouched (same mtime, still 6 rows, no duplicates) OK - safe to run twice")

# A third call (simulating a second reboot) behaves identically.
archived_3 = sh.archive_rows_before(CUTOFF, ARCHIVE_PATH)
assert archived_3 == 0, archived_3
print("[idempotent_third_call] third call also archives 0 - stable no-op forever OK")

# ======================================================================
# New post-cutoff data recorded AFTER the archival step (simulating
# tonight's/tomorrow's corrected-DCF scan writing fresh rows) is
# completely unaffected - the live table just keeps growing normally.
# ======================================================================
sh.record([{"Ticker": "AAA", "Long Score": 74.0, "Price": 11.8}], day="2026-09-30")
_series_after = sh.series("AAA", limit_days=730)
assert any(r["day"] == "2026-09-30" for r in _series_after), _series_after
print("[new_post_cutoff_rows_unaffected] recording a new row after the archive step works "
      "exactly as before - archival never touches ongoing recording OK")

# ======================================================================
# Merge/crash-safety: calling archive_rows_before() again with a NEW
# pre-cutoff row present (simulating a second, still-not-yet-cleared
# pre-cutoff row - e.g. a partially-applied earlier run) merges into the
# existing archive file rather than clobbering it or duplicating.
# ======================================================================
with sh._conn() as _c:
    _c.execute(
        "INSERT INTO score_history (day, ticker, long_score, price) VALUES (?, ?, ?, ?)",
        ("2026-09-24", "CCC", 40.0, 5.0),
    )
archived_4 = sh.archive_rows_before(CUTOFF, ARCHIVE_PATH)
assert archived_4 == 1, archived_4
with open(ARCHIVE_PATH, encoding="utf-8") as f:
    _archived_rows_3 = json.load(f)
assert len(_archived_rows_3) == 7, len(_archived_rows_3)
_pairs_3 = {(r["day"], r["ticker"]) for r in _archived_rows_3}
assert ("2026-09-24", "CCC") in _pairs_3
assert _expected_pairs <= _pairs_3  # the original 6 are still there, untouched
print("[merges_new_pre_cutoff_rows] a later call with one more pre-cutoff row merges it "
      "into the existing archive (7 total now) without disturbing the original 6 OK")

# ======================================================================
# nightly_scan's boot-time wrapper: marker-file guarded, calls through
# to score_history.archive_rows_before() with the right cutoff/path, and
# never raises even if the underlying call fails.
# ======================================================================
import nightly_scan

assert nightly_scan.GROWTH_FADE_HISTORY_CUTOFF_DAY == "2026-09-28"
_marker = nightly_scan._growth_fade_archive_marker_path()
if os.path.exists(_marker):
    os.remove(_marker)
_logged = []
nightly_scan.archive_pre_growth_fade_history(log=_logged.append)
assert os.path.exists(_marker), _marker
assert any("growth-fade history archive" in m for m in _logged), _logged
print("[nightly_scan_wrapper_writes_marker] archive_pre_growth_fade_history() writes its "
      "marker file and logs a summary line on first run OK")

_logged.clear()
nightly_scan.archive_pre_growth_fade_history(log=_logged.append)
assert _logged == [], _logged
print("[nightly_scan_wrapper_skips_on_marker] a second call with the marker present returns "
      "immediately - no query, no log line, matching cleanup_fix9_nan_data()'s own "
      "marker-file convention OK")

# ======================================================================
# Nothing else touched: archive_rows_before()/archive_pre_growth_fade_
# history() never reference scheduler config, scan selection, or Top 100
# - confirmed by source inspection, not just assumption.
# ======================================================================
import inspect

_archive_src = inspect.getsource(sh.archive_rows_before)
for _forbidden in ("NIGHTLY_UNIVERSES", "scheduler_engine", "top100", "_cfg("):
    assert _forbidden not in _archive_src, _forbidden

_wrapper_src = inspect.getsource(nightly_scan.archive_pre_growth_fade_history)
for _forbidden in ("NIGHTLY_UNIVERSES", "scheduler_engine", "top100", "run_universe_scan"):
    assert _forbidden not in _wrapper_src, _forbidden
print("[no_schedule_scan_top100_touched] neither function references scheduler config, scan "
      "selection, or Top 100 - source-level confirmation, not just assumption OK")

print("\nALL SCORE_HISTORY ARCHIVE FIXTURES PASSED")
