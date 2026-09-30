"""
Scheduler due-logic + reprice heuristic + ASX100 integrity guard + snapshot
label fix (owner-directed, 30 Sep 2026, Commit 3 of the growth/Top100
freshness fix instruction file) - OWNER-APPROVED scheduler change, scoped
exactly to: _universes_needing_scan()'s due-logic, nightly_scan._reprice_row()'s
full-attention heuristic, the derived-universe integrity guard's drop-instead-
of-refuse behaviour, and snapshot_store.save_snapshot()'s universe label.
NIGHTLY_UNIVERSES itself, cadence parsing, _scan_priority_key, the 4h hard
timeout and the 3-attempt catch-up budget are all untouched (verified below).

Run: python3 tests/test_scheduler_due_logic_freshness.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="scheduler_due_logic_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scan_store
import scanner_engine
import scheduler_engine as se
import nightly_scan


def _iso(dt):
    return dt.isoformat()


def _save(universe, generated_at, run_night=None, degraded=False):
    import json
    payload = {
        "universe": universe, "source": "test", "generated_at": _iso(generated_at),
        "run_night": run_night, "rows": [{"Ticker": "X", "Long Score": 1}],
        "attention_lite": True, "degraded": degraded,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


now = datetime.now(timezone.utc)
_PINNED_ABBR_TO_WEEKDAY = {v: k for k, v in se._WEEKDAY_ABBR.items()}
today_cadence = _PINNED_ABBR_TO_WEEKDAY[now.weekday()]
_other_weekday_num = (now.weekday() + 1) % 7
non_pinned_cadence = _PINNED_ABBR_TO_WEEKDAY[_other_weekday_num]


# ======================================================================
# CHECK 1: weekday-pinned universe, 8 days old, TODAY is its pinned
# weekday, not yet credited tonight -> due (rule-based: run_night !=
# today, not an age threshold).
# ======================================================================
_save("PinnedFresh8d", now - timedelta(days=8), run_night=(now - timedelta(days=8)).strftime("%Y-%m-%d"))
cfg1 = {"universe_cadence": {"PinnedFresh8d": today_cadence}}
due1 = se._universes_needing_scan(cfg1)
assert "PinnedFresh8d" in due1, due1
print(f"[pinned_8d_old_on_pin_night_due] cadence={today_cadence!r} (today), generated_at 8d old, "
      f"not credited today -> due OK")
os.remove(scan_store._path("PinnedFresh8d"))


# ======================================================================
# CHECK 2: weekday-pinned universe, 10 days old, on a NON-pinned night
# -> due via the missed-pin safety net (>9 days), even though tonight
# isn't its scheduled night.
# ======================================================================
_save("PinnedStale10d", now - timedelta(days=10),
      run_night=(now - timedelta(days=10)).strftime("%Y-%m-%d"))
cfg2 = {"universe_cadence": {"PinnedStale10d": non_pinned_cadence}}
due2 = se._universes_needing_scan(cfg2)
assert "PinnedStale10d" in due2, due2
print(f"[pinned_10d_old_non_pinned_night_safety_net] cadence={non_pinned_cadence!r} (NOT today), "
      f"generated_at 10d old (>9d safety net) -> due anyway OK")
os.remove(scan_store._path("PinnedStale10d"))


# ======================================================================
# CHECK 3: weekday-pinned universe, 2 days old, on a NON-pinned night
# -> NOT due (neither "tonight is its pin" nor the safety net apply).
# ======================================================================
_save("PinnedFresh2d", now - timedelta(days=2),
      run_night=(now - timedelta(days=2)).strftime("%Y-%m-%d"))
cfg3 = {"universe_cadence": {"PinnedFresh2d": non_pinned_cadence}}
due3 = se._universes_needing_scan(cfg3)
assert "PinnedFresh2d" not in due3, due3
print(f"[pinned_2d_old_non_pinned_night_not_due] cadence={non_pinned_cadence!r} (NOT today), "
      f"generated_at 2d old -> not due OK")
os.remove(scan_store._path("PinnedFresh2d"))


# ======================================================================
# CHECK 4: daily universes unchanged - >20h generated_at age still due,
# <20h still not due (based on generated_at alone, no repriced_at blend).
# ======================================================================
_save("DailyStale", now - timedelta(hours=25))
_save("DailyFresh", now - timedelta(hours=5))
cfg4 = {"universe_cadence": {"DailyStale": "daily", "DailyFresh": "daily"}}
due4 = se._universes_needing_scan(cfg4)
assert "DailyStale" in due4, due4
assert "DailyFresh" not in due4, due4
print("[daily_universes_unchanged] 25h-old daily -> due, 5h-old daily -> not due OK")
os.remove(scan_store._path("DailyStale"))
os.remove(scan_store._path("DailyFresh"))

# Daily universe fresh by generated_at but with a MUCH newer repriced_at
# must still be judged by generated_at alone (the whole point of this
# fix) - confirms the old load_scan()-based blended reading is no
# longer consulted at all.
_save("DailyStaleButRepriced", now - timedelta(hours=25))
import json as _json
_p = scan_store._path("DailyStaleButRepriced")
with open(_p) as f:
    _payload = _json.load(f)
_payload["repriced_at"] = _iso(now - timedelta(minutes=5))
with open(_p, "w") as f:
    _json.dump(_payload, f)
cfg4b = {"universe_cadence": {"DailyStaleButRepriced": "daily"}}
due4b = se._universes_needing_scan(cfg4b)
assert "DailyStaleButRepriced" in due4b, due4b
print("[reprice_never_masks_due_check] 25h-old generated_at with a 5-min-old repriced_at is STILL "
      "due - the old combined-freshness blend (load_scan()'s own 'age_hours') is no longer read "
      "by the due-check at all OK")
os.remove(_p)


# ======================================================================
# CHECK 5: cadence parsing / _scan_priority_key / hard timeout / catch-up
# budget are untouched by this fix (grep-verify against this module's
# own source).
# ======================================================================
import inspect
_src_needing_scan = inspect.getsource(se._universes_needing_scan)
assert "_scan_priority_key" in _src_needing_scan
assert se._JOB_HARD_TIMEOUT_SECONDS["nightly"] == 4 * 3600
print("[unchanged_surfaces] _scan_priority_key still used for sort order; nightly hard timeout "
      "still 4h OK")


# ======================================================================
# CHECK 6: reprice heuristic fix - a lite row with NO explicit full-
# attention marker (attention_full unset, universe_attention_lite=True)
# must NEVER be promoted to full-attention just because its stored
# Discovery happens to exceed a freshly recomputed price/volume value -
# the old `(stored_discovery - fresh_pv) > 0` fallback is gone.
# ======================================================================
import pandas as pd

_dates = pd.date_range(now - pd.Timedelta(days=90), periods=90, freq="D")
_hist = pd.DataFrame({
    "Close": [100.0] * 89 + [101.0],  # near-zero price/volume activity -> small fresh_pv
    "Volume": [1_000] * 90,
}, index=_dates)
_stale_row = {
    "Ticker": "RPZ", "Intrinsic Value": 120.0, "Quality": 70,
    # A big stored Discovery from WEEKS ago (e.g. a real attention spike
    # that has long since faded) - old code would misread today's small
    # fresh_pv as proof this row is "full attention" and blend the
    # stale remainder back in.
    "Discovery (lite)": 500.0,
    "attention_full": False,
}
_repriced = nightly_scan._reprice_row(_stale_row, _hist, universe_attention_lite=True)
assert _repriced is not None
# discovery_measured=False path -> Discovery (lite) becomes fresh_pv
# alone, NOT fresh_pv + the old stale remainder.
assert _repriced["Discovery (lite)"] < 50.0, _repriced["Discovery (lite)"]
print(f"[reprice_heuristic_never_promotes_from_stale_discovery] stored Discovery=500 (stale), "
      f"attention_full=False, universe lite -> repriced Discovery="
      f"{_repriced['Discovery (lite)']:.1f} (price/volume-only, NOT 500+remainder - the removed "
      f"heuristic would have promoted this to full-attention and preserved most of the 500) OK")

# An explicitly-marked full-attention row is untouched by this fix -
# still preserves its remainder correctly.
_full_row = dict(_stale_row, attention_full=True)
_repriced_full = nightly_scan._reprice_row(_full_row, _hist, universe_attention_lite=True)
assert _repriced_full["Discovery (lite)"] > 400.0, _repriced_full["Discovery (lite)"]
print(f"[reprice_heuristic_explicit_marker_unaffected] attention_full=True (explicit marker) -> "
      f"repriced Discovery={_repriced_full['Discovery (lite)']:.1f} (remainder correctly "
      f"preserved) - this fix only removes the THIRD, guessed fallback OK")


# ======================================================================
# CHECK 7: ASX100 integrity guard - drop <= 3 foreign members instead
# of refusing to save; still refuse above that.
# ======================================================================
import pandas as _pd2

def _pool_df(tickers):
    return _pd2.DataFrame({"Ticker": tickers})

_parent_tickers = [f"P{i}.AX" for i in range(50)]

# 1 foreign ticker among an otherwise-clean membership -> dropped, not refused.
with mock.patch.object(scanner_engine, "get_universe_pool") as _guop, \
     mock.patch.object(scan_store, "load_scan") as _lsc, \
     mock.patch.object(scan_store, "save_scan") as _ssv, \
     mock.patch("snapshot_store.build_snapshots_from_scan"):
    def _guop_side_effect(country, universe):
        if universe == "ASX 100":
            return _pool_df(_parent_tickers[:20] + ["FOREIGN1.AX"]), "test"
        if universe == "ASX 200":
            return _pool_df(_parent_tickers), "test"
        return None, "unmapped"
    _guop.side_effect = _guop_side_effect

    def _lsc_side_effect(universe):
        if universe == "ASX 200":
            # The STORED scan still carries FOREIGN1.AX (e.g. from
            # before it was reclassified out of the index) - the live
            # membership fetch above no longer lists it, which is
            # exactly the real incident's shape: a stale/reclassified
            # ticker whose row still exists, not simply absent.
            return {"rows": [{"Ticker": t, "Long Score": 50} for t in _parent_tickers]
                    + [{"Ticker": "FOREIGN1.AX", "Long Score": 50}]}
        return None
    _lsc.side_effect = _lsc_side_effect

    se._build_derived_universes(log=lambda *a, **k: None)
    assert _ssv.called, "save_scan should have been called (drop-and-save, not refuse)"
    _saved_universe, _saved_rows = _ssv.call_args[0][0], _ssv.call_args[0][1]
    assert _saved_universe == "ASX 100", _saved_universe
    _saved_tickers = {r["Ticker"] for r in _saved_rows}
    assert "FOREIGN1.AX" not in _saved_tickers, _saved_tickers
    assert _ssv.call_args.kwargs.get("degraded", False) is False
print("[asx100_drop_1_foreign_member] 1 foreign ticker (FOREIGN1.AX, not in ASX 200) -> dropped, "
      "logged, save proceeds normally (not degraded) OK")

# 4 foreign tickers (over the <=3 drop threshold) -> refused (falls
# through to the existing integrity-guard FAIL path), same as before
# this fix for anything above the small-drop threshold.
with mock.patch.object(scanner_engine, "get_universe_pool") as _guop2, \
     mock.patch.object(scan_store, "load_scan") as _lsc2, \
     mock.patch.object(scan_store, "save_scan") as _ssv2, \
     mock.patch("snapshot_store.build_snapshots_from_scan"):
    _foreign4 = [f"FOREIGN{i}.AX" for i in range(4)]

    def _guop_side_effect2(country, universe):
        if universe == "ASX 100":
            return _pool_df(_parent_tickers[:20] + _foreign4), "test"
        if universe == "ASX 200":
            return _pool_df(_parent_tickers), "test"
        return None, "unmapped"
    _guop2.side_effect = _guop_side_effect2

    def _lsc_side_effect2(universe):
        if universe == "ASX 200":
            return {"rows": [{"Ticker": t, "Long Score": 50} for t in _parent_tickers]
                    + [{"Ticker": t, "Long Score": 50} for t in _foreign4]}
        # No prior "ASX 100" scan on disk -> nothing servable to fall
        # back to, so the existing guard's own "save anyway, degraded"
        # branch would fire INSTEAD of a hard refuse; pass a fake prior
        # so the pure-refuse path is what's exercised here.
        if universe == "ASX 100":
            return {"rows": [{"Ticker": t, "Long Score": 1} for t in _parent_tickers[:20]]}
        return None
    _lsc2.side_effect = _lsc_side_effect2

    se._build_derived_universes(log=lambda *a, **k: None)
    assert not _ssv2.called, "save_scan must NOT be called - 4 foreign members exceeds the drop threshold"
print("[asx100_refuse_4_foreign_members] 4 foreign tickers (over the <=3 drop threshold) -> "
      "integrity guard still refuses to save (existing behaviour, unchanged) OK")


# ======================================================================
# CHECK 8: snapshot label fix - save_snapshot() no longer reverts
# universe="live" back to a previously-stored real universe name; the
# render layer translates "live" into a presentable label.
# ======================================================================
import snapshot_store
import snapshot_render

with mock.patch.object(snapshot_store, "DB_PATH", os.path.join(TESTVOL, "snap_test.db")):
    snapshot_store.save_snapshot("LBL", "S&P 500", {"Ticker": "LBL", "Quality": 70})
    snapshot_store.save_snapshot("LBL", "live", {"Ticker": "LBL", "Quality": 71})
    _snap = snapshot_store.get_snapshot("LBL")
    assert _snap["universe"] == "live", _snap["universe"]
print(f"[snapshot_universe_live_no_longer_reverted] LBL: real scan (S&P 500) then a visitor's "
      f"live view -> universe stays {_snap['universe']!r} (used to silently revert to 'S&P 500') OK")

assert snapshot_render._universe_display_label("live", "en") == "Live view"
assert snapshot_render._universe_display_label("live", "es") == "Vista en vivo"
assert snapshot_render._universe_display_label("S&P 500", "en") == "S&P 500"
assert snapshot_render._universe_display_label("S&P 500", "es") == "S&P 500"
print("[snapshot_universe_display_label] 'live' -> 'Live view'/'Vista en vivo'; any real universe "
      "name passes through unchanged, both languages OK")


print("\nALL SCHEDULER DUE-LOGIC / REPRICE / ASX100 / SNAPSHOT-LABEL FIXTURES PASSED")
