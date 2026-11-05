"""TASK 30 (Director-directed, 4 Nov 2026).

Part A: "Recently added" badge on Top 200 cards - streak-start
computation (top100_store.current_streak_start_dates()), the badge map
(top100_render.recent_badge_map()), the env-var test gate
(TOP200_RECENT_BADGE_LIVE), and the missed-selection-night guard (A2).

Part B: returning-visitor measurement - the 30-day rotating-salt hash
(admin_metrics_store._visitor_hash_30d()), its write/read functions,
owner-session exclusion, and the fact that the EXISTING day-rotated
visitor figures are completely untouched.

Run: python3 tests/test_director_task30_badge_and_returning_visitors.py
"""
import datetime as _dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task30_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_store as ts
import top100_render as tr
import admin_metrics_store as ams

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _wipe_pool():
    with ts._conn() as conn:
        conn.execute("DELETE FROM top100_pool")


def _wipe_metrics():
    with ams._conn() as conn:
        for table in ("visitor_hashes_30d", "owner_visitor_hashes_30d",
                      "daily_visitor_hashes", "daily_human_hashes"):
            conn.execute(f"DELETE FROM {table}")


def _save(as_of, tickers):
    rows = [
        {"ticker": t, "company_name": t, "universe": "AU", "value_score": 50.0,
         "mos_pct": 10.0, "price": 1.0, "intrinsic_value": 1.1, "currency": "AUD"}
        for t in tickers
    ]
    ts.save_pool(rows, as_of)


os.environ["AUTH_COOKIE_SECRET"] = "test-secret-task30"


# ======================================================================
# PART A - CHECK 1: ticker present in every stored run gets the OLDEST
# queried snapshot date as its streak start.
# ======================================================================
_wipe_pool()
_dates = [f"2026-10-{d:02d}" for d in range(20, 30)]
for _d in _dates:
    _save(_d, ["AAA"])
_starts = ts.current_streak_start_dates(["AAA"])
assert _starts["AAA"] == _dates[0], _starts
print(f"[streak_present_every_run] AAA present in all {len(_dates)} stored runs -> "
      f"start = {_starts['AAA']} (oldest queried date) OK")


# ======================================================================
# PART A - CHECK 2: a ticker present only in the last 3 runs gets that
# 3rd-from-last date as its start; an always-present ticker in the same
# snapshots is unaffected.
# ======================================================================
_wipe_pool()
for _i, _d in enumerate(_dates):
    _tickers = ["AAA"]
    if _i >= len(_dates) - 3:
        _tickers.append("BBB")
    _save(_d, _tickers)
_starts = ts.current_streak_start_dates(["AAA", "BBB"])
assert _starts["AAA"] == _dates[0], _starts
assert _starts["BBB"] == _dates[-3], _starts
print(f"[streak_recent_entrant] BBB entered 3 runs ago -> start = {_starts['BBB']}; "
      f"AAA still oldest ({_starts['AAA']}) OK")


# ======================================================================
# PART A - CHECK 3: a ticker absent from the LATEST stored run is
# omitted entirely (it is not currently in the pool).
# ======================================================================
_wipe_pool()
for _i, _d in enumerate(_dates):
    # The LATEST snapshot still needs a row (OTHER) so its as_of date
    # remains a real, distinct stored snapshot - AAA is simply missing
    # from it, not the whole night missing (that's CHECK 5 below).
    _tickers = ["AAA"] if _i < len(_dates) - 1 else ["OTHER"]
    _save(_d, _tickers)
_starts = ts.current_streak_start_dates(["AAA"])
assert "AAA" not in _starts, _starts
print("[streak_absent_from_latest] AAA missing from latest run -> omitted from "
      "streak-starts map OK")


# ======================================================================
# PART A - CHECK 4: re-entry (left the pool, then came back) gets the
# NEW entry date, not the original one - "re-entries count" (A1).
# ======================================================================
_wipe_pool()
for _i, _d in enumerate(_dates):
    if _i < 3:
        _tickers = ["AAA"]
    elif _i < 6:
        # A real gap - the selection DID run these nights (OTHER is a
        # real stored snapshot), AAA simply wasn't in the pool then.
        _tickers = ["OTHER"]
    else:
        _tickers = ["AAA"]
    _save(_d, _tickers)
_starts = ts.current_streak_start_dates(["AAA"])
assert _starts["AAA"] == _dates[6], _starts
print(f"[streak_reentry] AAA left for 3 runs then came back -> start = "
      f"{_starts['AAA']} (new entry date, not the original) OK")


# ======================================================================
# PART A - CHECK 5 (A2 guard): a night where the selection itself did
# NOT run (no stored snapshot at all) must not reset everyone's streak.
# Only distinct STORED as_of dates are ever walked, so a missing night
# simply isn't in the queried date list.
# ======================================================================
_wipe_pool()
_dates_with_gap = [d for d in _dates if d != "2026-10-26"]
for _d in _dates_with_gap:
    _save(_d, ["AAA"])
_starts = ts.current_streak_start_dates(["AAA"])
assert _starts["AAA"] == _dates_with_gap[0], _starts
print(f"[missed_selection_night_guard] one night's selection never ran (no stored "
      f"snapshot) -> AAA's streak still reaches back to {_starts['AAA']}, undisturbed "
      f"OK")


# ======================================================================
# PART A - CHECK 6: no tickers / no snapshots -> empty map, never raises.
# ======================================================================
_wipe_pool()
assert ts.current_streak_start_dates([]) == {}
assert ts.current_streak_start_dates(["ZZZ"]) == {}
print("[streak_empty_inputs] no tickers and no snapshots both return {} OK")


# ======================================================================
# PART A - CHECK 7: badge map, gate CLOSED, non-owner viewer -> {}
# (no badges shown at all, regardless of any streak data).
# ======================================================================
_wipe_pool()
os.environ.pop("TOP200_RECENT_BADGE_LIVE", None)
tr._is_owner_for_refresh = lambda: False
tr._today_brisbane = lambda: _dt.date(2026, 11, 4)
for _d in ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]:
    _save(_d, ["AAA"])
_out = tr.recent_badge_map(["AAA"], "en")
assert _out == {}, _out
print("[badge_gate_closed_non_owner] gate unset, viewer not owner -> no badges at "
      "all OK")


# ======================================================================
# PART A - CHECK 8: badge map, gate CLOSED, OWNER viewer -> sees badge
# only for the recent entrant, not the long-standing member.
# ======================================================================
_wipe_pool()
os.environ.pop("TOP200_RECENT_BADGE_LIVE", None)
tr._is_owner_for_refresh = lambda: True
_badge_dates = [f"2026-10-{d:02d}" for d in range(26, 31)] + [
    "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04",
]
for _i, _d in enumerate(_badge_dates):
    _tickers = ["OLD"]
    if _i >= len(_badge_dates) - 3:
        _tickers.append("NEW")
    _save(_d, _tickers)
tr._today_brisbane = lambda: _dt.date(2026, 11, 4)
_out = tr.recent_badge_map(["OLD", "NEW"], "en")
assert "OLD" not in _out, _out
assert "NEW" in _out, _out
assert "Recently added" in _out["NEW"], _out["NEW"]
print(f"[badge_gate_closed_owner] gate unset, owner viewer -> OLD gets no badge, "
      f"NEW (3-run streak) gets: {_out['NEW']!r} OK")


# ======================================================================
# PART A - CHECK 9: badge map, gate LIVE ("1"), non-owner viewer ->
# badge shown to everyone.
# ======================================================================
_wipe_pool()
os.environ["TOP200_RECENT_BADGE_LIVE"] = "1"
tr._is_owner_for_refresh = lambda: False
for _d in ["2026-11-02", "2026-11-03", "2026-11-04"]:
    _save(_d, ["NEW"])
tr._today_brisbane = lambda: _dt.date(2026, 11, 4)
_out = tr.recent_badge_map(["NEW"], "en")
assert "NEW" in _out, _out
print("[badge_gate_live] TOP200_RECENT_BADGE_LIVE=1, non-owner viewer -> badge "
      "shown to everyone OK")


# ======================================================================
# PART A - CHECK 10: gate explicitly "0" behaves identically to unset
# (non-owner viewer sees nothing).
# ======================================================================
_wipe_pool()
os.environ["TOP200_RECENT_BADGE_LIVE"] = "0"
tr._is_owner_for_refresh = lambda: False
_save("2026-11-04", ["NEW"])
tr._today_brisbane = lambda: _dt.date(2026, 11, 4)
_out = tr.recent_badge_map(["NEW"], "en")
assert _out == {}, _out
print("[badge_gate_zero] TOP200_RECENT_BADGE_LIVE=0 behaves same as unset for a "
      "non-owner viewer OK")


# ======================================================================
# PART A - CHECK 11: EN + ES chip text.
# ======================================================================
_wipe_pool()
os.environ["TOP200_RECENT_BADGE_LIVE"] = "1"
tr._is_owner_for_refresh = lambda: False
_save("2026-11-04", ["NEW"])
tr._today_brisbane = lambda: _dt.date(2026, 11, 4)
_out_en = tr.recent_badge_map(["NEW"], "en")
_out_es = tr.recent_badge_map(["NEW"], "es")
assert "Recently added" in _out_en["NEW"], _out_en["NEW"]
assert "Añadido recientemente" in _out_es["NEW"], _out_es["NEW"]
print("[badge_en_es_text] EN chip says 'Recently added', ES chip says "
      "'Añadido recientemente' OK")


# ======================================================================
# PART A - CHECK 12: a ticker whose streak started outside the 7-day
# window gets no badge, even with the gate live.
# ======================================================================
_wipe_pool()
os.environ["TOP200_RECENT_BADGE_LIVE"] = "1"
tr._is_owner_for_refresh = lambda: False
_old_dates = [f"2026-10-{d:02d}" for d in range(25, 32)] + [
    "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04",
]
for _d in _old_dates:
    _save(_d, ["OLD"])
tr._today_brisbane = lambda: _dt.date(2026, 11, 4)
_out = tr.recent_badge_map(["OLD"], "en")
assert "OLD" not in _out, _out
print("[badge_outside_window] streak started well before the 7-day window -> no "
      "badge even with the gate live OK")

os.environ.pop("TOP200_RECENT_BADGE_LIVE", None)


# ======================================================================
# PART B - CHECK 13: _visitor_hash_30d() is stable for the SAME
# (ip, ua) pair across different days within the same ~30-day window.
# ======================================================================
_h1 = ams._visitor_hash_30d("1.2.3.4", "UA-A", "2026-11-01")
_h2 = ams._visitor_hash_30d("1.2.3.4", "UA-A", "2026-11-15")
assert _h1 == _h2, (_h1, _h2)
print("[hash30d_stable_same_window] same (ip, ua), different days same window -> "
      "same hash OK")


# ======================================================================
# PART B - CHECK 14: different (ip, ua) pairs hash differently.
# ======================================================================
_h1 = ams._visitor_hash_30d("1.2.3.4", "UA-A", "2026-11-01")
_h2 = ams._visitor_hash_30d("5.6.7.8", "UA-A", "2026-11-01")
assert _h1 != _h2, (_h1, _h2)
print("[hash30d_different_pairs] different ip, same day -> different hash OK")


# ======================================================================
# PART B - CHECK 15: the SAME (ip, ua) pair hashes differently once a
# ~30-day window boundary is crossed - the "rotating" part of the salt.
# ======================================================================
_day0 = ams._ROTATION_EPOCH.strftime("%Y-%m-%d")
_day1 = (ams._ROTATION_EPOCH + _dt.timedelta(days=31)).strftime("%Y-%m-%d")
_h1 = ams._visitor_hash_30d("1.2.3.4", "UA-A", _day0)
_h2 = ams._visitor_hash_30d("1.2.3.4", "UA-A", _day1)
assert _h1 != _h2, (_h1, _h2)
print("[hash30d_rotates_across_window] same (ip, ua) across a window boundary -> "
      "different hash OK")


# ======================================================================
# PART B - CHECK 16: the hash never contains the raw ip/ua, and is a
# plain sha256 hex digest (64 chars) - no raw IP stored/logged (B3).
# ======================================================================
_h = ams._visitor_hash_30d("203.0.113.9", "Mozilla/5.0 Secret", "2026-11-04")
assert "203.0.113.9" not in _h
assert "Secret" not in _h
assert len(_h) == 64, _h
print("[hash30d_no_raw_ip_ua] hash contains neither the raw IP nor UA substring, "
      "and is a plain 64-char sha256 hex digest OK")


# ======================================================================
# PART B - CHECK 17: the 30-day hash and the EXISTING day-rotated hash
# are different constructions - never collide for the same input.
# ======================================================================
_day_hash = ams._visitor_hash("1.2.3.4", "UA-A", "2026-11-04")
_hash_30d = ams._visitor_hash_30d("1.2.3.4", "UA-A", "2026-11-04")
assert _day_hash != _hash_30d, (_day_hash, _hash_30d)
print("[hash30d_differs_from_day_hash] day-rotated hash and 30-day-rotated hash "
      "for the same (ip, ua, day) are different values OK")


# ======================================================================
# PART B - CHECK 18: record + prune visitor_hashes_30d.
# ======================================================================
_wipe_metrics()
_h = ams._visitor_hash_30d("1.2.3.4", "UA-A", "2026-08-01")
ams.record_visitor_hashes_30d([("2026-08-01", _h)])
with ams._conn() as _conn:
    _n = _conn.execute(
        "SELECT COUNT(*) FROM visitor_hashes_30d WHERE day = ?", ("2026-08-01",)
    ).fetchone()[0]
assert _n == 1, _n
_pruned = ams.prune_old_visitor_hashes_30d(log=lambda *a, **k: None)
assert _pruned == 1, _pruned
with ams._conn() as _conn:
    _n = _conn.execute("SELECT COUNT(*) FROM visitor_hashes_30d").fetchone()[0]
assert _n == 0, _n
print("[record_and_prune_30d] write then prune (90+ days old) -> row written, "
      "then correctly pruned OK")


# ======================================================================
# PART B - CHECK 19: returning_visitor_stats() - unique 7d/30d, 2+-day
# returners, and the per-day new-vs-returning split.
# ======================================================================
_wipe_metrics()


def _day_ago(n):
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=n)).strftime("%Y-%m-%d")


_today_str = ams._today()
_visitor_a = ams._visitor_hash_30d("1.1.1.1", "UA-A", _today_str)
_visitor_b = ams._visitor_hash_30d("2.2.2.2", "UA-B", _today_str)
ams.record_visitor_hashes_30d([
    (_day_ago(2), _visitor_a),
    (_day_ago(0), _visitor_a),  # A returns -> seen on 2 distinct days
    (_day_ago(2), _visitor_b),  # B seen once only
])
_stats = ams.returning_visitor_stats()
assert _stats["unique_visitors_7d"] == 2, _stats
assert _stats["unique_visitors_30d"] == 2, _stats
assert _stats["returning_2plus_days"] == 1, _stats
assert len(_stats["daily"]) == 30, len(_stats["daily"])
_by_day = {d["day"]: d for d in _stats["daily"]}
assert _by_day[_day_ago(2)]["new"] == 2, _by_day[_day_ago(2)]
assert _by_day[_day_ago(0)]["returning"] == 1, _by_day[_day_ago(0)]
assert _by_day[_day_ago(0)]["new"] == 0, _by_day[_day_ago(0)]
print(f"[returning_visitor_stats_counts] 7d={_stats['unique_visitors_7d']}, "
      f"30d={_stats['unique_visitors_30d']}, "
      f"returning_2plus={_stats['returning_2plus_days']}, "
      f"daily split correct OK")


# ======================================================================
# PART B - CHECK 20 (B2): owner's own hash is excluded from every
# returning-visitor figure.
# ======================================================================
_wipe_metrics()
_owner_hash = ams._visitor_hash_30d("9.9.9.9", "UA-Owner", _today_str)
_other_hash = ams._visitor_hash_30d("8.8.8.8", "UA-Other", _today_str)
ams.record_visitor_hashes_30d([(_today_str, _owner_hash), (_today_str, _other_hash)])
ams.record_owner_visitor_hash(_owner_hash, _today_str)
_stats = ams.returning_visitor_stats()
assert _stats["unique_visitors_7d"] == 1, _stats
assert _stats["unique_visitors_30d"] == 1, _stats
print("[owner_hash_excluded] owner's own 30d hash recorded via "
      "record_owner_visitor_hash() -> excluded from unique-visitor counts, "
      "non-owner hash still counted OK")


# ======================================================================
# PART B - CHECK 21: no data at all -> all-zero/empty shape, never
# raises.
# ======================================================================
_wipe_metrics()
_stats = ams.returning_visitor_stats()
assert _stats["unique_visitors_7d"] == 0, _stats
assert _stats["unique_visitors_30d"] == 0, _stats
assert _stats["returning_2plus_days"] == 0, _stats
assert len(_stats["daily"]) == 30, _stats
assert all(d["new"] == 0 and d["returning"] == 0 for d in _stats["daily"]), _stats
print("[returning_visitor_stats_empty] no rows at all -> all-zero figures, a full "
      "30-day daily series of zeroes, never raises OK")


# ======================================================================
# PART B - CHECK 22: prune_old_owner_visitor_hashes_30d().
# ======================================================================
_wipe_metrics()
_old_day = ams._day_n_ago(ams.PRUNE_AFTER_DAYS + 5)
ams.record_owner_visitor_hash("deadbeef", _old_day)
with ams._conn() as _conn:
    _n = _conn.execute("SELECT COUNT(*) FROM owner_visitor_hashes_30d").fetchone()[0]
assert _n == 1, _n
_pruned = ams.prune_old_owner_visitor_hashes_30d(log=lambda *a, **k: None)
assert _pruned == 1, _pruned
print("[prune_owner_hashes_30d] a 95-day-old owner-hash row is pruned OK")


# ======================================================================
# PART B - CHECK 23 (B3): the EXISTING day-hash-based figures
# (daily_visitor_hashes / unique_visitors_for_day()) are completely
# unaffected by any of this task's new tables/writes.
# ======================================================================
_wipe_metrics()
_day_hash = ams._visitor_hash("1.2.3.4", "UA-A", _today_str)
ams.record_visitor_hashes([(_today_str, _day_hash)])
assert ams.unique_visitors_for_day(_today_str) == 1

_hash30 = ams._visitor_hash_30d("9.9.9.9", "UA-Z", _today_str)
ams.record_visitor_hashes_30d([(_today_str, _hash30)])
ams.record_owner_visitor_hash(_hash30, _today_str)

assert ams.unique_visitors_for_day(_today_str) == 1
print("[existing_day_figures_unaffected] record_visitor_hashes_30d()/"
      "record_owner_visitor_hash() leave unique_visitors_for_day() (the "
      "EXISTING day-hash figure) completely unchanged OK")


print("\nALL TASK 30 (BADGE + RETURNING-VISITOR) FIXTURES PASSED")
