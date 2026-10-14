"""
Director's follow-up queue, TASK 2 (14 Oct 2026): "Top 200 preview:
show who would actually enter." Root cause the Director named (verified
in code): select_top100_pool() keeps the top POOL_SIZE rows by
RECOMPUTED Value Score (top100_engine._recompute_value_score()); Claude
Research Score ("composite") only orders the preview list, so the
existing "rank vs pool" (Director's work queue TASK 1c, commit b490073)
cannot show which candidates would actually enter the pool.

This file proves, against the real production functions (never a
second, independently-derived formula):

  a. pool_value_score_cutoff() - the CURRENT pool's own 200th member's
     Value Score - reads top100_store.current_pool()'s own stored
     "value_score" column (the exact number _build_best_by_ticker()
     wrote via _recompute_value_score() at selection time), never
     recomputing it. None with no pool yet.
  b. preview_candidates_by_country() rows now carry "recomputed_value_
     score" (_recompute_value_score() off the SAME raw scan row, not a
     second formula - proven equal to a direct call on an identical row)
     and "enters_pool" (True/False once a pool exists; None with no pool
     yet - never a fabricated verdict), plus "would_enter_counts" per
     country.
  c. "would_displace": _pool_lowest_members(n) - the CURRENT pool's own
     bottom n members (ticker/country/value_score), for the Canada-alone
     count and the all-previewed-countries count.
  d. select_top100_pool()'s own return value is byte-identical whether
     or not a Top 200 preview is run first - this task's own instruction
     ("reuse the pool's own recompute; do not write a second formula...
     read-only... prove select_top100_pool() output is byte-identical").

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task2_top200_would_enter.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task2_top200_would_enter_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

import peer_context
import scan_store
import top100_engine as te
import top100_store

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


def _row(ticker, company_name, quality=55.0, mos_pct=16.7, psychology=5.0,
         discovery_lite=30.0, price=100.0, iv=120.0, long_score=60.0):
    return {
        "Ticker": ticker, "Company Name": company_name, "Price": price,
        "Intrinsic Value": iv, "MOS %": mos_pct, "Long Score": long_score,
        "Quality": quality, "Psychology": psychology, "Discovery (lite)": discovery_lite,
        "Moat": 50.0, "Growth Used": 0.05, "FCF Source": "reported",
        "Dividend Yield %": 2.0, "Is Fund": False, "price_unit_suspect": False,
        "Trading Status": None, "DCF Unreliable": False,
    }


# ======================================================================
# CHECK 1-2: no pool yet - pool_value_score_cutoff() is None, and
# preview rows show "enters_pool": None (never a fabricated verdict
# against a pool that doesn't exist).
# ======================================================================
check("no selection has ever run yet -> pool_value_score_cutoff() is None",
      te.pool_value_score_cutoff() is None)
check("no selection has ever run yet -> _pool_lowest_members(1) is []",
      te._pool_lowest_members(1) == [])

scan_store.save_scan("DAX", [_row("CANNOPOOL.TO", "Canada No Pool Yet")],
                      source_label="test fixture")
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX"
_preview0 = te.preview_candidates_by_country()
_row0 = next(r for rows in _preview0["by_country"].values() for r in rows if r["ticker"] == "CANNOPOOL.TO")
check("TASK 2: with no pool yet, a preview row's 'enters_pool' is None, not False",
      _row0["enters_pool"] is None)
check("TASK 2: with no pool yet, the preview's own 'pool_cutoff' is None",
      _preview0["pool_cutoff"] is None)
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)


# ======================================================================
# CHECK 5-14: seed a small CURRENT pool with a known cutoff, then prove
# "recomputed_value_score"/"enters_pool"/"would_enter_counts"/
# "would_displace" all derive from it via the real functions only.
# ======================================================================
if os.path.exists(top100_store.DB_PATH):
    os.remove(top100_store.DB_PATH)

_hi_raw = _row("CANHI.TO", "Canada Hi", quality=95.0, mos_pct=80.0, psychology=9.0, discovery_lite=90.0)
_lo_raw = _row("CANLO.TO", "Canada Lo", quality=10.0, mos_pct=1.0, psychology=1.0, discovery_lite=5.0)
_jp_raw = _row("JPNHI.T", "Japan Hi", quality=95.0, mos_pct=80.0, psychology=9.0, discovery_lite=90.0)
_hi_expected = te._recompute_value_score(_hi_raw)
_lo_expected = te._recompute_value_score(_lo_raw)
_jp_expected = te._recompute_value_score(_jp_raw)
check("sanity: the 'hi' fixture recomputes strictly higher than the 'lo' fixture",
      _hi_expected > _lo_expected)

_cutoff = (_hi_expected + _lo_expected) / 2.0

top100_store.save_pool(
    [{"ticker": "POOL1", "company_name": "Pool 1", "universe": "S&P 500",
      "value_score": _cutoff + 20, "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 110.0,
      "currency": "USD", "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0},
     {"ticker": "POOL2", "company_name": "Pool 2", "universe": "S&P 500",
      "value_score": _cutoff + 10, "mos_pct": 8.0, "price": 100.0, "intrinsic_value": 108.0,
      "currency": "USD", "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0},
     {"ticker": "POOL3", "company_name": "Pool 3 (lowest - the 200th member)", "universe": "S&P 500",
      "value_score": _cutoff, "mos_pct": 5.0, "price": 100.0, "intrinsic_value": 105.0,
      "currency": "USD", "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0}],
    as_of="2026-10-14",
)

check("TASK 2a: pool_value_score_cutoff() reads back the seeded pool's own lowest "
      "value_score (POOL3's), never recomputing it",
      te.pool_value_score_cutoff() == _cutoff)

scan_store.save_scan("DAX", [_row("CANHI.TO", "Canada Hi", quality=95.0, mos_pct=80.0,
                                             psychology=9.0, discovery_lite=90.0),
                                        _row("CANLO.TO", "Canada Lo", quality=10.0, mos_pct=1.0,
                                             psychology=1.0, discovery_lite=5.0)],
                      source_label="test fixture")
scan_store.save_scan("Nikkei 225", [_row("JPNHI.T", "Japan Hi", quality=95.0, mos_pct=80.0,
                                          psychology=9.0, discovery_lite=90.0)],
                      source_label="test fixture")
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX, Nikkei 225"
_preview = te.preview_candidates_by_country()

check("TASK 2: the preview's own 'pool_cutoff' matches pool_value_score_cutoff() exactly",
      _preview["pool_cutoff"] == _cutoff)

_canhi = next(r for r in _preview["by_country"]["Canada"] if r["ticker"] == "CANHI.TO")
_canlo = next(r for r in _preview["by_country"]["Canada"] if r["ticker"] == "CANLO.TO")
_jphi = next(r for r in _preview["by_country"]["Japan"] if r["ticker"] == "JPNHI.T")

check("TASK 2b: CANHI.TO's 'recomputed_value_score' matches a direct "
      "_recompute_value_score() call on an identical raw row (no second formula)",
      _canhi["recomputed_value_score"] == _hi_expected)
check("TASK 2b: CANLO.TO's 'recomputed_value_score' matches a direct call too",
      _canlo["recomputed_value_score"] == _lo_expected)
check("TASK 2b: CANHI.TO scores above the pool cutoff -> enters_pool True",
      _canhi["enters_pool"] is True)
check("TASK 2b: CANLO.TO scores below the pool cutoff -> enters_pool False",
      _canlo["enters_pool"] is False)
check("TASK 2b: JPNHI.T scores above the pool cutoff -> enters_pool True",
      _jphi["enters_pool"] is True)

check("TASK 2b: would_enter_counts['Canada'] == 1 (only CANHI.TO clears the cutoff)",
      _preview["would_enter_counts"]["Canada"] == 1)
check("TASK 2b: would_enter_counts['Japan'] == 1 (JPNHI.T clears the cutoff)",
      _preview["would_enter_counts"]["Japan"] == 1)

# ======================================================================
# CHECK: "would displace" - Canada alone (N=1), Japan alone (N=1, since
# TASK 3's own generalization below adds one scenario per ACTUAL
# previewed country, keyed by that country's own name - not just
# "canada" hardcoded) vs every previewed country (N=2) - the CURRENT
# pool's own bottom N members, read back, never resimulated.
# ======================================================================
_displace_canada = _preview["would_displace"]["Canada"]
_displace_japan = _preview["would_displace"]["Japan"]
_displace_all = _preview["would_displace"]["all"]
check("TASK 2c: Canada alone would displace exactly 1 pool member (POOL3, the lowest)",
      [d["ticker"] for d in _displace_canada] == ["POOL3"])
check("TASK 2c: Canada alone's displaced row carries the pool's own stored value_score",
      _displace_canada[0]["value_score"] == _cutoff)
check("TASK 2c: Japan alone would also displace exactly 1 pool member (POOL3, the lowest) "
      "(TASK 3's own per-country generalization, keyed by 'Japan', not hardcoded)",
      [d["ticker"] for d in _displace_japan] == ["POOL3"])
check("TASK 2c: every previewed country together (Canada + Japan, N=2) would displace "
      "the pool's own 2 lowest members (POOL3 then POOL2)",
      [d["ticker"] for d in _displace_all] == ["POOL2", "POOL3"])
check("TASK 2c: _pool_lowest_members() independently reproduces the same 2-member tail",
      [d["ticker"] for d in te._pool_lowest_members(2)] == ["POOL2", "POOL3"])
check("TASK 2c: a displaced row's 'country' comes from peer_context.market_for(), "
      "not a hand-picked label",
      _displace_canada[0]["country"] == peer_context.market_for("POOL3"))
check("TASK 3: would_displace has EXACTLY one scenario key per actual previewed country "
      "(Canada, Japan) plus the fixed 'all' catch-all - no hand-picked name list",
      set(_preview["would_displace"].keys()) == {"Canada", "Japan", "all"})

os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)


# ======================================================================
# CHECK: the early-return shape (switch unset) is UNCHANGED by TASK 2 -
# still exactly {"by_country": {}, "excluded": []}, no new keys - so a
# caller gating purely on "is this switch on" sees no difference from
# before this task.
# ======================================================================
check('preview_candidates_by_country() is still exactly {"by_country": {}, "excluded": []} '
      "when TOP200_PREVIEW_UNIVERSES is unset (TASK 2's new keys are NOT added here)",
      te.preview_candidates_by_country() == {"by_country": {}, "excluded": []})


# ======================================================================
# CHECK: byte-identical proof - select_top100_pool()'s own return value
# never depends on anything this task touched (pool_value_score_
# cutoff()/_pool_lowest_members() only READ top100_store.current_pool(),
# never write, and preview_candidates_by_country() is never called by
# select_top100_pool() or _build_best_by_ticker()).
# ======================================================================
_pool_before = te.select_top100_pool(log=lambda *a, **k: None)
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX, Nikkei 225"
te.preview_candidates_by_country()
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
_pool_after = te.select_top100_pool(log=lambda *a, **k: None)
check("select_top100_pool()'s own return value is byte-identical before and after "
      "running the Top 200 preview (TASK 2's additions are read-only)",
      json.dumps(_pool_before, sort_keys=True, default=str) ==
      json.dumps(_pool_after, sort_keys=True, default=str))


# ======================================================================
# CHECK: app._render_top200_preview_panel() end to end - the pool
# cutoff caption, each country's "would enter: N" in its own expander
# label, the "enters pool" column, and the new "Would displace"
# expander, all render with no exception (same AppTest harness tests/
# test_part5_step5_2_top200_preview.py's own CHECK 11-14 already use).
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ["TOP200_PREVIEW_UNIVERSES"] = "DAX, Nikkei 225"
import app
app._render_top200_preview_panel()
"""
at = AppTest.from_string(script, default_timeout=60)
at.run()
check("app._render_top200_preview_panel() raises no exception with TASK 2's new fields",
      not at.exception)
_captions = " ".join(c.value or "" for c in at.get("caption"))
check("the pool cutoff is shown once in a caption at the top of the panel",
      "pool cutoff" in _captions.lower() and f"{_cutoff:.1f}" in _captions)
_expander_labels = [e.label for e in at.get("expander")]
check("the Canada expander's own label carries its 'would enter' count",
      any("Canada" in l and "would enter: 1" in l for l in _expander_labels))
check("the Japan expander's own label carries its 'would enter' count",
      any("Japan" in l and "would enter: 1" in l for l in _expander_labels))
check("a 'Would displace' expander exists",
      any("Would displace" in l for l in _expander_labels))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
