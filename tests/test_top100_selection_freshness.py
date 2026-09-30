"""
Top 100 selection freshness fix (owner-directed, 30 Sep 2026, Commit 2 of
instruction_growth_top100_freshness_fix.md).

Root cause (verified against live Railway logs): top100_engine.
select_top100_pool() used to keep each ticker's MAXIMUM Long Score across
every saved universe file with no freshness check at all - a weekday-
pinned universe (S&P 500 Dividend Aristocrats, Small Caps S&P 600) that
only gets nightly-repriced (never a real full rescan) kept winning ticker
slots with a stale valuation indefinitely, because reprice only recomputes
from the STORED Intrinsic Value, never re-running a full DCF.

This file covers:
  - freshest-candidate-wins (not highest Long Score)
  - derived-universe files excluded
  - degraded files excluded
  - orphaned-universe files excluded
  - all-candidates-stale -> freshest of them + stale_valuation flagged
  - lite vs full rows for the same ticker rank under the SAME formula
    (discovery_measured=False for every ticker, per section 2.2)
  - first-night "selection rule changed" strip suppression

Run: python3 tests/test_top100_selection_freshness.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_selection_freshness_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scan_store
import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _row(ticker, long_score, quality=70, mos=20.0, psychology=5.0, discovery=10.0,
          company_name=None, price=50.0, iv=55.0):
    return {
        "Ticker": ticker,
        "Company Name": company_name or f"{ticker} Co",
        "Quality": quality,
        "MOS %": mos,
        "Psychology": psychology,
        "Discovery (lite)": discovery,
        "Long Score": long_score,
        "Price": price,
        "Intrinsic Value": iv,
    }


def _save_universe(universe, rows, generated_at, degraded=False):
    """Writes a scan file directly via scan_store's own on-disk shape,
    bypassing save_scan()'s own datetime.now() so tests can control
    generated_at exactly."""
    payload = {
        "universe": universe,
        "source": "test",
        "generated_at": _iso(generated_at),
        "run_night": None,
        "rows": rows,
        "attention_lite": True,
        "degraded": degraded,
    }
    import json
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


# A fixed, real cadence map (mirrors _DEFAULT_NIGHTLY_UNIVERSES's own
# shape) so "orphaned" has something concrete to be orphaned FROM.
_FAKE_CADENCE = {"S&P 500": "daily", "ASX 200": "daily", "Nasdaq 100": "daily"}


def _with_fake_cadence(fn):
    return mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                              return_value=dict(_FAKE_CADENCE))(fn)


# ======================================================================
# CHECK 1: two files for one ticker - the OLDER file has the HIGHER Long
# Score, but the FRESHER file must still win (not the old max-score rule).
# ======================================================================
_now_dt = _now()
_save_universe("S&P 500", [_row("ABC", long_score=90.0)], _now_dt - timedelta(days=1))
_save_universe("Nasdaq 100", [_row("ABC", long_score=40.0)], _now_dt)

with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool1 = te.select_top100_pool(log=lambda *a, **k: None)

abc = next(r for r in pool1 if r["ticker"] == "ABC")
assert abc["universe"] == "Nasdaq 100", abc["universe"]
assert abc["value_score_source_row"] == 40.0, abc["value_score_source_row"]
print(f"[freshest_wins_not_highest_score] ABC: S&P 500 (1 day old, score 90) vs "
      f"Nasdaq 100 (fresh, score 40) -> Nasdaq 100 wins (universe={abc['universe']!r}, "
      f"stored score {abc['value_score_source_row']}) OK")

for u in ("S&P 500", "Nasdaq 100"):
    os.remove(scan_store._path(u))


# ======================================================================
# CHECK 2: a DERIVED universe file (in scheduler_engine.
# _DERIVED_UNIVERSE_PARENTS) is never eligible to win a slot, even when
# it's the freshest file that carries the ticker.
# ======================================================================
_DERIVED_NAME = next(iter(te.scheduler_engine._DERIVED_UNIVERSE_PARENTS))
_save_universe("S&P 500", [_row("DEF", long_score=30.0)], _now_dt - timedelta(days=5))
_save_universe(_DERIVED_NAME, [_row("DEF", long_score=99.0)], _now_dt)

with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE, **{_DERIVED_NAME: "daily"})):
    pool2 = te.select_top100_pool(log=lambda *a, **k: None)

deff = next(r for r in pool2 if r["ticker"] == "DEF")
assert deff["universe"] == "S&P 500", deff["universe"]
print(f"[derived_universe_excluded] DEF: {_DERIVED_NAME!r} (derived, fresh, score 99) "
      f"is ignored - S&P 500 (5 days old, score 30) wins by default -> "
      f"universe={deff['universe']!r} OK")

for u in ("S&P 500", _DERIVED_NAME):
    os.remove(scan_store._path(u))


# ======================================================================
# CHECK 3: a `degraded` file is never eligible, even fresh and high-
# scoring.
# ======================================================================
_save_universe("S&P 500", [_row("GHI", long_score=30.0)], _now_dt - timedelta(days=5))
_save_universe("Nasdaq 100", [_row("GHI", long_score=99.0)], _now_dt, degraded=True)

with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool3 = te.select_top100_pool(log=lambda *a, **k: None)

ghi = next(r for r in pool3 if r["ticker"] == "GHI")
assert ghi["universe"] == "S&P 500", ghi["universe"]
print(f"[degraded_file_excluded] GHI: Nasdaq 100 (degraded, fresh, score 99) is "
      f"ignored - S&P 500 (5 days old, score 30) wins -> universe={ghi['universe']!r} OK")

for u in ("S&P 500", "Nasdaq 100"):
    os.remove(scan_store._path(u))


# ======================================================================
# CHECK 4: an ORPHANED universe (saved on disk, but not in today's
# NIGHTLY_UNIVERSES cadence map) is never eligible.
# ======================================================================
_save_universe("S&P 500", [_row("JKL", long_score=30.0)], _now_dt - timedelta(days=5))
_save_universe("Retired Universe", [_row("JKL", long_score=99.0)], _now_dt)

with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool4 = te.select_top100_pool(log=lambda *a, **k: None)

jkl = next(r for r in pool4 if r["ticker"] == "JKL")
assert jkl["universe"] == "S&P 500", jkl["universe"]
print(f"[orphaned_universe_excluded] JKL: 'Retired Universe' (not in the current "
      f"cadence map, fresh, score 99) is ignored - S&P 500 (5 days old, score 30) "
      f"wins -> universe={jkl['universe']!r} OK")

for u in ("S&P 500", "Retired Universe"):
    os.remove(scan_store._path(u))


# ======================================================================
# CHECK 5: every candidate older than POOL_MAX_FULL_SCAN_AGE_DAYS - the
# freshest of them still wins (nothing dropped), but stale_valuation=1.
# ======================================================================
_save_universe("S&P 500", [_row("MNO", long_score=50.0)],
                _now_dt - timedelta(days=20))
_save_universe("Nasdaq 100", [_row("MNO", long_score=10.0)],
                _now_dt - timedelta(days=15))

with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool5 = te.select_top100_pool(log=lambda *a, **k: None)

mno = next(r for r in pool5 if r["ticker"] == "MNO")
assert mno["universe"] == "Nasdaq 100", mno["universe"]  # freshest of the two stale ones
assert mno["stale_valuation"] is True, mno["stale_valuation"]
print(f"[all_stale_freshest_wins_flagged] MNO: both candidates older than "
      f"{te.POOL_MAX_FULL_SCAN_AGE_DAYS}d - freshest (Nasdaq 100, 15d) still wins, "
      f"stale_valuation={mno['stale_valuation']} OK")

for u in ("S&P 500", "Nasdaq 100"):
    os.remove(scan_store._path(u))


# ======================================================================
# CHECK 6: lite vs full rows for the SAME ticker rank under the SAME
# formula - discovery_measured=False for every ticker regardless of the
# row's own original attention_lite state. A full-attention row's own
# STORED Long Score (computed with discovery_measured=True originally)
# must NOT be what wins ties/ranking here - the RECOMPUTED value_score
# is always discovery_measured=False for both.
# ======================================================================
import ranking_engine

_lite_row = _row("PQR", long_score=999.0, quality=80, mos=30.0, psychology=10.0,
                  discovery=5.0)  # Long Score itself is a dummy/ignored value
_full_row = _row("PQR", long_score=999.0, quality=80, mos=30.0, psychology=10.0,
                   discovery=5.0)
_expected = ranking_engine.calculate_long_score(80, 30.0, 10.0, 5.0, discovery_measured=False)
_recomputed_lite = te._recompute_value_score(_lite_row)
_recomputed_full = te._recompute_value_score(_full_row)
assert abs(_recomputed_lite - _expected) < 1e-9, (_recomputed_lite, _expected)
assert abs(_recomputed_full - _expected) < 1e-9, (_recomputed_full, _expected)
assert _recomputed_lite == _recomputed_full, (_recomputed_lite, _recomputed_full)
print(f"[lite_vs_full_same_formula] identical Quality/MOS/Psychology/Discovery inputs "
      f"from a nominally-lite row and a nominally-full row both recompute to the exact "
      f"same value_score ({_recomputed_lite:.4f}, discovery_measured=False) - the "
      f"original Long Score field (999.0, a dummy) is never read by the recompute OK")


# ======================================================================
# CHECK 7: first-night "selection rule changed" strip suppression -
# previous_pool() tagged under a different (or missing) pool_selection_
# rule than current_pool() must suppress the New/Dropped/Awaiting diff.
# ======================================================================
import top100_render as tr

# Checks 1-6 above each ran select_top100_pool(), which upserts into
# TODAY's as_of snapshot - wipe the table clean so THIS check's own two
# explicit snapshots are unambiguously the two most recent as_of dates
# current_pool()/previous_pool() will see.
with ts._conn() as _wipe_conn:
    _wipe_conn.execute("DELETE FROM top100_pool")

# Simulate a PRE-fix previous snapshot (no pool_selection_rule at all -
# every real pre-this-commit row) and a POST-fix current one.
ts.save_pool([{"ticker": "STU", "company_name": "STU Co", "universe": "S&P 500",
               "value_score": 50.0, "mos_pct": 10.0, "price": 10.0, "intrinsic_value": 11.0,
               "currency": "USD"}], "2026-09-28")
ts.save_pool([{"ticker": "STU", "company_name": "STU Co", "universe": "S&P 500",
               "value_score": 55.0, "mos_pct": 12.0, "price": 10.0, "intrinsic_value": 12.0,
               "currency": "USD", "generated_at": _iso(_now_dt),
               "pool_selection_rule": te.POOL_SELECTION_RULE}], "2026-09-29")

_captions = []
with mock.patch.object(tr, "st") as _fake_st:
    _fake_st.caption.side_effect = lambda text: _captions.append(text)
    enriched = tr._enriched_pool()
    tr._changes_strip(enriched, "en")

assert any("selection rule changed" in c for c in _captions), _captions
print(f"[first_night_strip_suppression] previous_pool() has no pool_selection_rule "
      f"(pre-fix row), current_pool() is tagged {te.POOL_SELECTION_RULE!r} -> "
      f"changes strip renders the rule-changed caption instead of New/Dropped/Awaiting: "
      f"{_captions!r} OK")


print("\nALL TOP 100 SELECTION FRESHNESS FIXTURES PASSED")
