"""
Pool expansion 100 -> 200 (owner decision, 1 Oct 2026 22:21 AEST, Commit
5 of instruction_dcf_unreliable_pool_step4_nightly.md).

Why: the old cut-off at 100 sat near Value Score 50 and systematically
excluded fairly-priced compounders (CPRT at 45.5 was the live case); at
200 the cut-off is ~40 and the Research Score, not MOS, decides where a
company ranks within the pool.

This file covers the instruction's own test list:
  - a pool fixture of 250 valid rows -> 200 selected, ASX extension 40
  - expansion seeding (every ticker that enters the pool because of this
    expansion, i.e. in tonight's pool but not in the saved baseline
    pool) is seeded straight to consecutive_nights=3, and the seeding is
    a genuine one-off (a ticker that enters the pool on a LATER night,
    after the marker already exists, gets the ordinary consecutive_
    nights=1 a true newcomer always gets - it is never force-seeded to
    3 a second time)
  - MAX_NIGHTLY_SCORES honoured at 240 (and the derived per-UTC-day
    entrant cap at 480)
  - the EN/ES page title/nav/teaser strings read "Top 200"
  - every TOP100_* identifier is still present - no rename of modules,
    functions, env vars, store tables, keys or test names

Run: python3 tests/test_pool_expansion_v200.py
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="pool_expansion_v200_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import i18n
import scan_store
import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

_now_dt = datetime.now(timezone.utc)
_FAKE_CADENCE = {"S&P 500": "daily", "ASX 200": "daily"}

# Same "mock out the real yfinance sector-fill fallback" precedent as
# tests/test_dcf_unreliable_pool_exclusion.py - irrelevant to pool
# sizing, and slow/blocked in a sandboxed test run.
_patch_sector_fill = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill.start()


def _row(ticker, mos):
    return {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": 70,
        "MOS %": mos, "Psychology": 5.0, "Discovery (lite)": 10.0,
        "Long Score": 50.0, "Price": 50.0, "Intrinsic Value": 55.0,
    }


def _save_universe(universe, rows, generated_at=_now_dt):
    payload = {
        "universe": universe, "source": "test", "generated_at": generated_at.isoformat(),
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


# ======================================================================
# CHECK 1: a pool fixture of 250 valid (non-.AX) rows -> exactly 200
# selected by Value Score; a separate 45-row ASX fixture (all scored too
# low to make the global 200) -> the ASX extension tops up to exactly 40
# (TOP20_AU_TARGET, doubled 20->40 alongside POOL_SIZE).
# ======================================================================
assert te.POOL_SIZE == 200, te.POOL_SIZE
assert te.TOP20_AU_TARGET == 40, te.TOP20_AU_TARGET

_us_rows = [_row(f"US{i}", mos=float(i)) for i in range(250)]  # MOS 0..249, higher i = higher score
_au_rows = [_row(f"AU{i}.AX", mos=float(-50 + i)) for i in range(45)]  # MOS -50..-6, all below every US row
_save_universe("S&P 500", _us_rows)
_save_universe("ASX 200", _au_rows)

with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool1 = te.select_top100_pool(log=lambda *a, **k: None)

assert len(pool1) == 200, len(pool1)
pool1_tickers = {r["ticker"] for r in pool1}
# The 200 HIGHEST-mos US rows (US50..US249) must be exactly the pool -
# no .AX row (all scored lower) displaces any of them.
assert pool1_tickers == {f"US{i}" for i in range(50, 250)}, sorted(pool1_tickers)[:5]

extension1 = ts.current_asx_extension()
assert len(extension1) == 40, len(extension1)
# The 40 HIGHEST-mos AU rows out of the 45 available (AU5..AU44).
extension1_tickers = {r["ticker"] for r in extension1}
assert extension1_tickers == {f"AU{i}.AX" for i in range(5, 45)}, sorted(extension1_tickers)[:5]
print(f"[pool_250_fixture_selects_200] 250 US rows -> exactly {len(pool1)} selected (the "
      f"200 highest by Value Score); 45 AU rows (all below the global cut) -> ASX extension "
      f"tops up to exactly {len(extension1)} (TOP20_AU_TARGET=40) OK")

_clear("S&P 500", "ASX 200")


# ======================================================================
# CHECK 2: pool-expansion one-off persistence seed. A "yesterday" pool
# of 50 tickers is saved as the baseline; tonight's scan adds 10 more
# tickers that only fit because of the 100->200 expansion. Those 10 (and
# ONLY those 10) must be seeded straight to consecutive_nights=3.
#
# Idempotency: a FURTHER ticker added to a later run (after the one-off
# marker already exists) must NOT be force-seeded to 3 - it gets the
# ordinary consecutive_nights=1 every true newcomer gets.
# ======================================================================
# Fresh DB for this check - CHECK 1 above already saved a pool under
# today's real date, which would otherwise make ts.current_pool()
# return CHECK 1's own 200-ticker pool instead of the baseline this
# check needs to set up. CHECK 1's own select_top100_pool() call was
# also this test file's FIRST ever call, so (with no prior saved pool
# at all) it already fired the one-off expansion seed itself and wrote
# its marker - remove that too so THIS check gets to observe a fresh
# firing of the one-off seed.
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
if os.path.exists(te._pool_expansion_v200_marker_path()):
    os.remove(te._pool_expansion_v200_marker_path())

_old_tickers = [f"OLD{i}" for i in range(10)]
_new_tickers = [f"NEW{i}" for i in range(5)]

_baseline_rows = [
    {"ticker": t, "company_name": f"{t} Co", "universe": "S&P 500", "value_score": 80.0,
     "mos_pct": 40.0, "price": 100.0, "intrinsic_value": 150.0, "currency": "USD",
     "pool_selection_rule": "freshest_v1"}
    for t in _old_tickers
]
_yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
ts.save_pool(_baseline_rows, as_of=_yesterday)
assert {r["ticker"] for r in ts.current_pool()} == set(_old_tickers)

_night1_rows = (
    [_row(t, mos=50.0 + i) for i, t in enumerate(_old_tickers)] +
    [_row(t, mos=40.0 + i) for i, t in enumerate(_new_tickers)]
)
_save_universe("S&P 500", _night1_rows)

assert not os.path.exists(te._pool_expansion_v200_marker_path())
_logs2 = []
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool2 = te.select_top100_pool(log=_logs2.append)

tonight1_tickers = {r["ticker"] for r in pool2}
assert tonight1_tickers == set(_old_tickers) | set(_new_tickers), tonight1_tickers
assert os.path.exists(te._pool_expansion_v200_marker_path())

presence_after_night1 = ts.pool_presence_map(_old_tickers + _new_tickers)
for t in _new_tickers:
    assert presence_after_night1[t] == 3, (t, presence_after_night1[t])
_expansion_log = next(l for l in _logs2 if "pool expansion" in l)
assert f"seeded {len(_new_tickers)} expansion tickers" in _expansion_log, _expansion_log
print(f"[expansion_seed_marks_new_entrants] {len(_new_tickers)} tickers that only entered "
      f"because of the pool expansion are seeded straight to consecutive_nights=3; log line: "
      f"{_expansion_log!r} OK")

# A LATER run (marker already exists) adds one more ticker - it must
# NOT be force-seeded to 3; it gets the plain newcomer value of 1.
_later_tickers = _old_tickers + _new_tickers + ["LATER_NEWCOMER"]
_night2_rows = [_row(t, mos=float(i)) for i, t in enumerate(_later_tickers)]
_save_universe("S&P 500", _night2_rows)
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    te.select_top100_pool(log=lambda *a, **k: None)
presence_after_night2 = ts.pool_presence_map(["LATER_NEWCOMER"])
assert presence_after_night2["LATER_NEWCOMER"] == 1, presence_after_night2
print("[expansion_seed_is_one_off] a ticker that enters the pool on a LATER night (after the "
      "one-off marker already exists) is never force-seeded to 3 - it gets the ordinary "
      "consecutive_nights=1 every true newcomer gets OK")

_clear("S&P 500")


# ======================================================================
# CHECK 3: MAX_NIGHTLY_SCORES honoured at 240 (and the derived per-UTC-
# day entrant cap, 2 * MAX_NIGHTLY_SCORES, at 480).
# ======================================================================
assert te.MAX_NIGHTLY_SCORES == 240, te.MAX_NIGHTLY_SCORES
assert te.TOP100_MAX_ENTRANTS_PER_UTC_DAY == 480, te.TOP100_MAX_ENTRANTS_PER_UTC_DAY
print("[max_nightly_scores_240] MAX_NIGHTLY_SCORES=240 and the derived per-UTC-day entrant "
      "cap=480 OK")


# ======================================================================
# CHECK 4: EN/ES visible titles/headings/captions read "Top 200", not
# "Top 100" - nav label, homepage teaser heading/CTA, Deep Dive sub-
# line, changes-strip cutoff reason.
# ======================================================================
for lang in ("en", "es"):
    assert "Top 200" in i18n.t("nav.top100", lang), (lang, i18n.t("nav.top100", lang))
    assert "Top 200" in i18n.t("home.top100_teaser.heading", lang), lang
    assert "Top 200" in i18n.t("home.top100_teaser.cta", lang), lang
    assert "200" in i18n.t("top100.changes_strip_reason_cutoff", lang), lang
    assert "Top 100" not in i18n.t("nav.top100", lang), lang
print("[en_es_titles_read_top_200] nav label, homepage teaser heading/CTA, and the changes-"
      "strip cutoff reason all read \"Top 200\" (or \"200\") in both EN and ES, with no "
      "leftover \"Top 100\" text OK")


# ======================================================================
# CHECK 5: no TOP100_* identifier was renamed - every known module-
# level TOP100_* constant/flag this codebase relies on is still present
# under its EXACT original name (grep-style, via hasattr/attribute
# access rather than a literal grep, so this fails loudly on a rename
# rather than silently passing on a typo).
# ======================================================================
import top100_render as tr

_expected_top100_engine_constants = [
    "TOP100_FAILURE_RETRY_HOURS", "TOP100_FAILURE_MAX_ATTEMPTS",
    "TOP100_MAX_SUBMISSIONS_PER_UTC_DAY", "TOP100_MAX_ENTRANTS_PER_UTC_DAY",
    "TOP100_COMPANIES_PER_REQUEST",
]
for name in _expected_top100_engine_constants:
    assert hasattr(te, name), f"top100_engine.{name} is missing - was it renamed?"
assert hasattr(tr, "TOP100_PUBLIC"), "top100_render.TOP100_PUBLIC is missing - was it renamed?"

# Also confirm the env var name itself (TOP100_PUBLIC) is still what
# top100_render.py reads from os.environ - a grep of the source, not an
# attribute check, since the env var name is a string literal, not an
# importable symbol.
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "top100_render.py")) as f:
    _render_src = f.read()
assert re.search(r'os\.environ\.get\("TOP100_PUBLIC"\)', _render_src), (
    "top100_render.py no longer reads the TOP100_PUBLIC env var by its original name"
)
print(f"[top100_identifiers_unrenamed] every expected TOP100_* constant/flag/env-var name "
      f"({', '.join(_expected_top100_engine_constants)}, TOP100_PUBLIC) is present under its "
      f"exact original name OK")


_patch_sector_fill.stop()

print("\nALL POOL EXPANSION (100 -> 200) FIXTURES PASSED")
