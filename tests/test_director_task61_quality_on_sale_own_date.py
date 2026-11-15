"""
TASK 61 (Director-directed, 15 Nov 2026) - Quality on sale gets its own
"Recently added" date.

PROBLEM (Director verified at 0db7b76): the Quality on sale tab's
"Recently added" badge came from recent_badge_map() ->
top100_store.current_streak_start_dates() - i.e. Top 200 POOL membership,
never Quality on sale's own history (quality_on_sale_candidates() is
computed LIVE on every render, with no stored history at all). Example:
INTU had been on Quality on sale for days but showed "Recently added ·
14 Nov" purely because it joined the Top 200 pool that night.

FIX, covered here:
  1. New store (top100_store.top100_qos_pool /
     save_quality_on_sale_pool() / current_qos_streak_start_dates()) -
     same walk-the-stored-snapshots streak logic as the Top 200 pool's
     own current_streak_start_dates(), against its OWN table.
  2. First-ever-snapshot baseline rule: when only ONE as_of has ever been
     recorded, {} for every ticker (a date we can't prove would be
     misleading) - from the second recorded night on, ordinary streak
     logic applies (a baseline member whose streak walks all the way
     back to night one still gets that truthful date, same as the Top
     200 version already does).
  3. top100_render.qos_recent_badge_map() - recent_badge_map()'s own
     shape (same TOP200_RECENT_BADGE_LIVE/owner gate, same 7-day window)
     against the new store instead of the Top 200 one.
  4. render_top100_page() wiring: the Quality on sale tab's own call site
     now receives qos_recent_badge_map()'s own map (`_qos_badges`), never
     `_recent_badges` (Top 200 pool dates) - every OTHER tab keeps
     `_recent_badges`, unchanged. Same split for the tab's own Excel
     export "Recently added date" column (`_qos_streak_start_map` vs.
     `_streak_start_map`).
  5. top100_engine.select_top100_pool() now also calls
     top100_store.save_quality_on_sale_pool() right after its own
     save_pool() call, same `as_of`, with the SAME capped ticker list
     quality_on_sale_candidates() itself returns - and a failure there
     never breaks the (already-saved) nightly pool run.

Run: python3 tests/test_director_task61_quality_on_sale_own_date.py
"""
import inspect
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task61_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import datetime as _dt
import i18n
import top100_store as ts
import top100_render as tr
import top100_engine as te

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

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


def _wipe_qos():
    with ts._conn() as conn:
        conn.execute("DELETE FROM top100_qos_pool")


def _wipe_pool():
    with ts._conn() as conn:
        conn.execute("DELETE FROM top100_pool")


_dates = [f"2026-10-{d:02d}" for d in range(20, 30)]


# ======================================================================
# PART A - store layer: save_quality_on_sale_pool() / current_qos_
# streak_start_dates()
# ======================================================================

# CHECK A1: baseline night - only ONE as_of ever recorded -> {} for
# every ticker, even one present that single night (the Director's own
# "first night: no badge" rule).
_wipe_qos()
ts.save_quality_on_sale_pool(["AAA", "BBB"], _dates[0])
_starts = ts.current_qos_streak_start_dates(["AAA", "BBB"])
check("[A1] baseline night (one snapshot ever recorded) -> {} for every "
      "ticker, no misleading date", _starts == {})

# CHECK A2: second night on - continuous run since night 1 still gets
# night 1's own date (truthful - it HAS been there since then).
_wipe_qos()
for _d in _dates[:3]:
    ts.save_quality_on_sale_pool(["AAA"], _d)
_starts = ts.current_qos_streak_start_dates(["AAA"])
check(f"[A2] AAA present every recorded night since baseline -> streak "
      f"start = {_starts.get('AAA')} (night 1, truthful) OK",
      _starts.get("AAA") == _dates[0])

# CHECK A3: a genuine new entrant (present only in the last 3 of 10
# recorded nights) gets that 3rd-from-last date - the INTU-shaped case:
# long-standing QoS member (AAA) keeps its old date, new entrant (BBB)
# gets its own real one.
_wipe_qos()
for _i, _d in enumerate(_dates):
    _tickers = ["AAA"]
    if _i >= len(_dates) - 3:
        _tickers.append("BBB")
    ts.save_quality_on_sale_pool(_tickers, _d)
_starts = ts.current_qos_streak_start_dates(["AAA", "BBB"])
check(f"[A3] AAA (present every night) -> {_starts.get('AAA')} (oldest "
      f"recorded), BBB (new in the last 3) -> {_starts.get('BBB')} (3rd "
      f"from last)", _starts.get("AAA") == _dates[0] and _starts.get("BBB") == _dates[-3])

# CHECK A4: gap then re-entry gets the NEW entry date, not the original.
_wipe_qos()
for _i, _d in enumerate(_dates):
    if _i < 3:
        _tickers = ["AAA"]
    elif _i < 6:
        _tickers = ["OTHER"]  # real gap night - AAA simply absent
    else:
        _tickers = ["AAA"]
    ts.save_quality_on_sale_pool(_tickers, _d)
_starts = ts.current_qos_streak_start_dates(["AAA"])
check(f"[A4] AAA left for 3 nights then re-entered -> streak start = "
      f"{_starts.get('AAA')} (the re-entry date, not the original)",
      _starts.get("AAA") == _dates[6])

# CHECK A5: ticker absent from the LATEST recorded snapshot -> omitted
# entirely (not currently on the list).
_wipe_qos()
for _i, _d in enumerate(_dates):
    _tickers = ["AAA"] if _i < len(_dates) - 1 else ["OTHER"]
    ts.save_quality_on_sale_pool(_tickers, _d)
_starts = ts.current_qos_streak_start_dates(["AAA"])
check("[A5] AAA missing from the latest recorded night -> omitted from "
      "the streak-starts map", "AAA" not in _starts)

# CHECK A6: empty tickers / no snapshots at all -> {}, never raises.
_wipe_qos()
check("[A6a] no snapshots at all -> {}", ts.current_qos_streak_start_dates(["AAA"]) == {})
for _d in _dates[:3]:
    ts.save_quality_on_sale_pool(["AAA"], _d)
check("[A6b] empty ticker list -> {}", ts.current_qos_streak_start_dates([]) == {})

# CHECK A7: retention - saving beyond POOL_SNAPSHOT_RETENTION distinct
# as_of dates prunes down to the most recent `keep`, same retention as
# the Top 200 pool.
_wipe_qos()
_many_dates = [f"2026-09-{d:02d}" for d in range(1, 25)]
for _d in _many_dates:
    ts.save_quality_on_sale_pool(["AAA"], _d)
_kept = ts._distinct_qos_as_of_dates(limit=100)
check(f"[A7] {len(_many_dates)} nights saved -> only "
      f"{ts.POOL_SNAPSHOT_RETENTION} most-recent as_of dates retained "
      f"(got {len(_kept)})", len(_kept) == ts.POOL_SNAPSHOT_RETENTION)
check("[A7b] the retained dates are the most RECENT ones",
      set(_kept) == set(_many_dates[-ts.POOL_SNAPSHOT_RETENTION:]))

# CHECK A8: save_quality_on_sale_pool() upserts idempotently - calling
# it twice for the same (as_of, ticker) never raises / never duplicates.
_wipe_qos()
ts.save_quality_on_sale_pool(["AAA", "BBB"], "2026-11-01")
ts.save_quality_on_sale_pool(["AAA", "BBB"], "2026-11-01")
with ts._conn() as conn:
    _n = conn.execute(
        "SELECT COUNT(*) FROM top100_qos_pool WHERE as_of = ?", ("2026-11-01",)
    ).fetchone()[0]
check("[A8] saving the same (as_of, ticker) pair twice is idempotent "
      "(no duplicate rows)", _n == 2)


# ======================================================================
# PART B - render layer: qos_recent_badge_map()
# ======================================================================

# CHECK B1: gate closed, non-owner viewer -> {} (no badges at all).
_wipe_qos()
os.environ.pop("TOP200_RECENT_BADGE_LIVE", None)
tr._is_owner_for_refresh = lambda: False
for _i, _d in enumerate(_dates):
    _tickers = ["AAA"]
    if _i >= len(_dates) - 2:
        _tickers.append("NEW")
    ts.save_quality_on_sale_pool(_tickers, _d)
tr._today_brisbane = lambda: _dt.date(2026, 10, 29)
_out = tr.qos_recent_badge_map(["AAA", "NEW"], "en")
check("[B1] gate closed, non-owner -> {} regardless of streak data", _out == {})

# CHECK B2: gate closed, OWNER viewer -> only the genuinely new entrant
# is badged; the long-standing member is not.
tr._is_owner_for_refresh = lambda: True
_out = tr.qos_recent_badge_map(["AAA", "NEW"], "en")
check("[B2] gate closed, owner viewer -> long-standing AAA gets no badge",
      "AAA" not in _out)
check("[B2] gate closed, owner viewer -> genuinely new NEW gets a badge",
      "NEW" in _out and "Recently added" in _out["NEW"])

# CHECK B3: gate LIVE -> badge shown to a non-owner viewer too.
os.environ["TOP200_RECENT_BADGE_LIVE"] = "1"
tr._is_owner_for_refresh = lambda: False
_out = tr.qos_recent_badge_map(["NEW"], "en")
check("[B3] TOP200_RECENT_BADGE_LIVE=1, non-owner -> badge shown", "NEW" in _out)

# CHECK B4: a streak that started outside the 7-day window gets no badge
# even with the gate live.
_wipe_qos()
_old_window_dates = [f"2026-10-{d:02d}" for d in range(10, 30)]
for _d in _old_window_dates:
    ts.save_quality_on_sale_pool(["OLD"], _d)
tr._today_brisbane = lambda: _dt.date(2026, 10, 30)
_out = tr.qos_recent_badge_map(["OLD"], "en")
check("[B4] streak well outside the 7-day window -> no badge even with "
      "the gate live", "OLD" not in _out)
os.environ.pop("TOP200_RECENT_BADGE_LIVE", None)


# ======================================================================
# PART C - the INTU-shaped case: the Quality on sale badge and the Top
# 200 badge must stay COMPLETELY independent of each other.
# ======================================================================
_wipe_qos()
_wipe_pool()
tr._is_owner_for_refresh = lambda: True
os.environ.pop("TOP200_RECENT_BADGE_LIVE", None)
tr._today_brisbane = lambda: _dt.date(2026, 11, 4)

# INTU: on Quality on sale for many nights (long streak there), but only
# joined the Top 200 POOL the last 2 nights.
_top200_dates = [f"2026-10-{d:02d}" for d in range(26, 32)] + [
    "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04",
]
for _i, _d in enumerate(_top200_dates):
    _tickers = ["OTHER"]
    if _i >= len(_top200_dates) - 2:
        _tickers.append("INTU")
    ts.save_pool(
        [{"ticker": t, "company_name": t, "universe": "S&P 500", "value_score": 60.0,
          "mos_pct": 10.0, "price": 1.0, "intrinsic_value": 1.1, "currency": "USD"}
         for t in _tickers], _d)

_qos_dates = [f"2026-10-{d:02d}" for d in range(26, 32)] + [
    "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04",
]
for _d in _qos_dates:
    ts.save_quality_on_sale_pool(["INTU"], _d)  # present EVERY recorded night

_top200_badges = tr.recent_badge_map(["INTU"], "en")
_qos_badges = tr.qos_recent_badge_map(["INTU"], "en")
check("[C1] INTU just joined the Top 200 pool (2 nights) -> gets the "
      "Top 200 'Recently added' badge", "INTU" in _top200_badges)
check("[C2] INTU has been on Quality on sale every recorded night -> "
      "gets NO 'Recently added' badge on the Quality on sale map",
      "INTU" not in _qos_badges)
print("[task61_intu_case] Top 200 badge map and Quality on sale badge "
      "map disagree for INTU exactly as the Director's own example "
      "requires - proven independent OK")


# ======================================================================
# PART D - render_top100_page() wiring: source inspection confirming the
# Quality on sale tab's call site uses the NEW map/streak dict, and every
# other tab keeps the OLD (Top 200) ones, unchanged.
# ======================================================================
_src = inspect.getsource(tr.render_top100_page)

check("[D1] render_top100_page() computes _qos_badges via "
      "qos_recent_badge_map()", "_qos_badges = qos_recent_badge_map(" in _src)
check("[D2] the Quality on sale tab's _render_quality_on_sale_tab() call "
      "is passed recent_badge_map=_qos_badges",
      "recent_badge_map=_qos_badges" in _src)
# _recent_badges must still be threaded into every OTHER tab - same
# occurrence count as before this task (one assignment + one use per
# tab call site; TASK 59 confirmed 7 occurrences of the analogous
# quality_on_sale_rank_map=_qos_rank_map wiring across tab call sites -
# here we simply confirm _recent_badges is still used broadly and never
# replaced wholesale by _qos_badges).
check("[D3] _recent_badges (Top 200 dates) is still used by multiple "
      "OTHER tab call sites, not replaced everywhere",
      _src.count("recent_badge_map=_recent_badges") >= 6)
check("[D4] render_top100_page() computes its own Quality-on-sale-only "
      "streak map (_qos_streak_start_map) via current_qos_streak_start_"
      "dates(), for the Excel export's own 'added' column",
      "_qos_streak_start_map = top100_store.current_qos_streak_start_dates(" in _src)
check("[D5] the Quality on sale tab's own export button passes "
      "streak_start_map=_qos_streak_start_map",
      "streak_start_map=_qos_streak_start_map" in _src)


# ======================================================================
# PART E - _top200_export_row(): the shared Excel-row builder picks
# whichever streak_start_map dict it's given - proving the mechanism the
# wiring in Part D relies on.
# ======================================================================
_row = {"ticker": "INTU", "company_name": "Intuit", "sector": "Tech",
        "composite": 80.0, "value_score": 70.0, "mos_pct": 10.0,
        "price": 100.0, "currency": "USD", "intrinsic_value": 110.0,
        "score_row": {}}
with mock.patch.object(tr, "support_levels_store", mock.Mock(get=lambda t: {})), \
     mock.patch.object(tr, "drop_chip_store", mock.Mock(get=lambda t: {})), \
     mock.patch.object(tr, "_tradability_spread_pct", lambda t: None), \
     mock.patch.object(tr, "_sentiment_bucket", lambda p: None), \
     mock.patch.object(tr, "peer_context", mock.Mock(market_for=lambda t: "US")):
    _out_top200 = tr._top200_export_row(
        1, _row, quality_moat_map={}, top200_rank_map={}, qos_rank_map={},
        streak_start_map={"INTU": "2026-11-03"})
    _out_qos = tr._top200_export_row(
        1, _row, quality_moat_map={}, top200_rank_map={}, qos_rank_map={},
        streak_start_map={"INTU": "2026-10-20"})
check("[E1] _top200_export_row()'s 'Recently added date' column reflects "
      "whichever streak_start_map dict it's given (Top 200 shape)",
      _out_top200["Recently added date"] == "2026-11-03")
check("[E2] ...and a DIFFERENT map (the Quality on sale shape) produces a "
      "DIFFERENT date for the same ticker/row",
      _out_qos["Recently added date"] == "2026-10-20")


# ======================================================================
# PART F - engine wiring: select_top100_pool() also saves tonight's
# Quality on sale list, same as_of, same capped ticker list quality_on_
# sale_candidates() itself returns.
# ======================================================================
_wipe_qos()
_wipe_pool()
_FAKE_CADENCE = {"S&P 500": "daily", "ASX 200": "daily"}
_patch_sector_fill = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill.start()


def _pool_row(ticker, mos):
    return {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": 70,
        "MOS %": mos, "Psychology": 5.0, "Discovery (lite)": 10.0,
        "Long Score": 50.0, "Price": 50.0, "Intrinsic Value": 55.0,
    }


import json
import scan_store
from datetime import datetime, timezone

_us_rows = [_pool_row(f"US{i}", mos=float(i)) for i in range(60)]
_payload = {
    "universe": "S&P 500", "source": "test", "generated_at": datetime.now(timezone.utc).isoformat(),
    "run_night": None, "rows": _us_rows, "attention_lite": True, "degraded": False,
}
_tmp = scan_store._path("S&P 500") + ".tmp"
with open(_tmp, "w") as f:
    json.dump(_payload, f)
os.replace(_tmp, scan_store._path("S&P 500"))

_FAKE_QOS_CANDIDATES = {"candidates": [{"ticker": "US55"}, {"ticker": "US58"}, {"ticker": "US59"}]}

with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)), \
     mock.patch.object(te, "quality_on_sale_candidates", return_value=_FAKE_QOS_CANDIDATES):
    _pool = te.select_top100_pool(log=lambda *a, **k: None)

with ts._conn() as conn:
    _saved_rows = conn.execute("SELECT as_of, ticker FROM top100_qos_pool").fetchall()
_saved_tickers = {r[1] for r in _saved_rows}
_saved_dates = {r[0] for r in _saved_rows}
check("[F1] select_top100_pool() saved exactly quality_on_sale_candidates()'s "
      "own (mocked) ticker list to top100_qos_pool",
      _saved_tickers == {"US55", "US58", "US59"})
check("[F2] every saved row uses the SAME single as_of date (tonight's "
      "run), matching the Top 200 pool snapshot's own as_of",
      len(_saved_dates) == 1)

with ts._conn() as conn:
    _pool_as_of = {r[0] for r in conn.execute("SELECT DISTINCT as_of FROM top100_pool").fetchall()}
check("[F3] the Quality on sale snapshot's as_of is the SAME as_of the "
      "Top 200 pool snapshot itself used tonight",
      _saved_dates == _pool_as_of)

# CHECK F4: a failing quality_on_sale_candidates() call must never break
# the (already-saved) nightly Top 200 pool run.
_wipe_pool()
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)), \
     mock.patch.object(te, "quality_on_sale_candidates", side_effect=RuntimeError("boom")):
    try:
        _pool2 = te.select_top100_pool(log=lambda *a, **k: None)
        _raised = False
    except RuntimeError:
        _raised = True
check("[F4] a failing quality_on_sale_candidates() call during the "
      "nightly run does NOT raise / does not break select_top100_pool()",
      not _raised)
check("[F4b] ...and the Top 200 pool itself was still saved despite the "
      "Quality on sale snapshot failure",
      len(_pool2) > 0)

_patch_sector_fill.stop()
scan_store_path = scan_store._path("S&P 500")
if os.path.exists(scan_store_path):
    os.remove(scan_store_path)


# ======================================================================
# PART G - gate OFF (owner-override off, env unset): no Quality on sale
# badge at all, same as today, for both maps.
# ======================================================================
_wipe_qos()
os.environ.pop("TOP200_RECENT_BADGE_LIVE", None)
tr._is_owner_for_refresh = lambda: False
for _d in _dates[-2:]:
    ts.save_quality_on_sale_pool(["ZZZ"], _d)
tr._today_brisbane = lambda: _dt.date(2026, 10, 29)
check("[G1] gate off, non-owner -> qos_recent_badge_map() returns {} "
      "(no badge, same as today)", tr.qos_recent_badge_map(["ZZZ"], "en") == {})


print(f"\nTASK 61 RESULTS: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("\nALL TASK 61 (QUALITY ON SALE OWN DATE) FIXTURES PASSED")
