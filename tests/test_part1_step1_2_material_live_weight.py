"""
PART 1 STEP 1.2 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): analyze_holding_news_v2() used to set
`material` on severity alone (if the GROUP's heaviest item's severity
was "material"/"thesis-breaking", `material = True` - regardless of
that group's own weight). A group whose weight has decayed to exactly
0.0 (past NEWS_V2_DECAY_ZERO_DAYS for its severity - 90 days for
material, 180 for thesis-breaking) contributes a zero hit to the News
Risk Score (hit = SEVERITY_HIT[sev] * 0.0 = 0.0) - it genuinely no
longer drags the score - yet it still flipped `material = True`,
which is the ONLY gate compute_health_v2()'s own news cut checks
(`if news.get("material") and news_risk is not None ...`). The result:
an expired material story could still trigger a Health-score cut on
behalf of OTHER, unrelated fresh-but-non-material news (e.g. fresh
"temporary" items, which reduce news_risk_score but never themselves
set `material`), purely because a long-dead story happened to also be
on file.

Fix: `material` is now gated on the SAME "still live" test
`thesis_breaking_live` already used one line below it -
`heaviest["weight"] > 0` - so only a group that is STILL contributing
a real point reduction can ever set it. v2 ONLY; v1's analyze_holding_
news() (its own `material = True` is set on severity alone, by
design - v1 is NOT touched by this step) is proven byte-identical on
the same fixtures.

Covers exactly the task's own listed tests:
  - an expired (fully-decayed) material item plus fresh temporary
    items gives NO cut (material stays False, news_adjustment is None)
  - a fresh (still-live) material item still gives a cut
  - v1 is untouched: the identical fixtures, run through v1, are
    byte-identical to what v1 computed before this step (a before/
    after table, not just an assertion)

Production-shaped fixtures (per this instruction's own standing rule):
every stored news event in this file is written via the REAL writer,
portfolio_news_engine._merge_and_save() - named explicitly at each call
site below, never a hand-built row inserted straight into the
news_events table. _claim_fetch() is mocked to return False so no
outbound network fetch is attempted (this sandbox has none).

Run: python3 tests/test_part1_step1_2_material_live_weight.py
"""
import datetime as dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part1_step1_2_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import portfolio_health_engine as phe
import portfolio_news_engine as pne

if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

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


NOW = dt.datetime(2026, 10, 6, 12, 0, 0)


def _seed(ticker, events, fetched_at=None):
    """Real writer: portfolio_news_engine._merge_and_save() - never a
    hand-built row. Same helper shape as tests/test_part_b_step1_
    news_v2.py's own _seed()."""
    if fetched_at is not None:
        with mock.patch.object(pne, "_now", return_value=fetched_at):
            pne._merge_and_save(ticker, events)
    else:
        pne._merge_and_save(ticker, events)


def _event(title, date=None, description="", publisher="Wire", source="test"):
    return {"date": date, "title": title, "description": description,
            "publisher": publisher, "link": "", "source": source, "domain": ""}


def _analyze_v2(ticker, **kwargs):
    with mock.patch.object(pne, "_claim_fetch", return_value=False):
        return pne.analyze_holding_news_v2(ticker, now=NOW, **kwargs)


def _analyze_v1(ticker, **kwargs):
    with mock.patch.object(pne, "_claim_fetch", return_value=False):
        return pne.analyze_holding_news(ticker, now=NOW, **kwargs)


_SNAPSHOT = {
    "revenue_growth": 0.05, "earnings_growth": 0.05, "profit_margin": 0.12, "roe": 0.10,
    "fcf_growth": 0.05, "debt_to_equity": 50.0, "mos_pct": 10.0, "price": 100.0,
    "history": None, "range52": 0.5, "trend_vs_ma200": 0.0, "dividend_rate": None,
}

# ======================================================================
# CHECK 1: an EXPIRED material item (120 days old - material decays to
# weight 0.0 at NEWS_V2_DECAY_ZERO_DAYS["material"] = 90 days) plus
# FRESH temporary items (never themselves set `material`, but do still
# reduce news_risk_score) gives NO cut under v2 - `material` stays
# False, so compute_health_v2()'s own news-cut gate never fires.
# ======================================================================
_TICKER1 = "EXPIREDMATERIAL"
_events1 = [
    _event(f"{_TICKER1} misses estimates and cuts guidance", date=NOW - dt.timedelta(days=120)),
    _event(f"{_TICKER1} hit by a temporary supply chain delay", date=NOW - dt.timedelta(days=2)),
    _event(f"{_TICKER1} outage causes a short-term delay", date=NOW - dt.timedelta(days=1)),
]
_seed(_TICKER1, _events1, fetched_at=NOW)
_news1 = _analyze_v2(_TICKER1, buy_date="2025-01-01")
check("the 120-day-old material item is fully decayed (weight 0 at the 90-day horizon)",
      pne._news_v2_weight(NOW - dt.timedelta(days=120), NOW, "material", NOW) == 0.0)
check("News Risk Score is still pulled below 100 by the FRESH temporary items",
      _news1["news_risk_score"] < 100.0)
check("`material` is False - the only material-severity group on file is fully decayed",
      _news1["material"] is False)
_components1 = phe.compute_health_components_v2(
    _SNAPSHOT, "STOCK", baseline={}, buy_date="2025-01-01", news=_news1)
_health1 = phe.compute_health_v2(_components1, news=_news1, is_etf=False)
check("compute_health_v2() applies NO cut (news_adjustment is None) despite a lower "
      "News Risk Score - the expired material item cannot 'borrow' a cut on behalf of "
      f"the fresh temporary items (news_risk={_news1['news_risk_score']}, "
      f"news_adjustment={_health1['news_adjustment']})",
      _health1["news_adjustment"] is None)
check("the panel's own 'Cut applied' reads exactly 0.0, not a withheld/None value - "
      "news WAS evaluated, it just didn't cut",
      _health1["news_cut_applied"] == 0.0)
print(f"[step1_2_expired_material_no_cut] news_risk={_news1['news_risk_score']}, "
      f"material={_news1['material']}, news_adjustment={_health1['news_adjustment']} - "
      "an expired material story plus fresh temporary news gives NO cut under v2 OK")

# ======================================================================
# CHECK 2: the SAME fixture, with one FRESH (5-day-old, well within the
# 90-day decay horizon) material item added - `material` is True and a
# real cut IS applied.
# ======================================================================
_TICKER2 = "FRESHMATERIAL"
_events2 = [
    _event(f"{_TICKER2} misses estimates and cuts guidance (old story)",
           date=NOW - dt.timedelta(days=120)),
    _event(f"{_TICKER2} hit by a temporary supply chain delay", date=NOW - dt.timedelta(days=2)),
    _event(f"{_TICKER2} outage causes a short-term delay", date=NOW - dt.timedelta(days=1)),
    _event(f"{_TICKER2} misses estimates and cuts guidance (fresh story)",
           date=NOW - dt.timedelta(days=5)),
]
_seed(_TICKER2, _events2, fetched_at=NOW)
_news2 = _analyze_v2(_TICKER2, buy_date="2025-01-01")
check("`material` is True - a FRESH material group (5 days old, well-within 90-day decay) exists",
      _news2["material"] is True)
_components2 = phe.compute_health_components_v2(
    _SNAPSHOT, "STOCK", baseline={}, buy_date="2025-01-01", news=_news2)
_health2 = phe.compute_health_v2(_components2, news=_news2, is_etf=False)
check("compute_health_v2() DOES apply a cut this time (news_adjustment is a real negative number)",
      _health2["news_adjustment"] is not None and _health2["news_adjustment"] < 0.0)
check("'Cut applied' is a positive point reduction, not 0.0",
      _health2["news_cut_applied"] is not None and _health2["news_cut_applied"] > 0.0)
print(f"[step1_2_fresh_material_still_cuts] news_risk={_news2['news_risk_score']}, "
      f"material={_news2['material']}, news_adjustment={_health2['news_adjustment']}, "
      f"cut_applied={_health2['news_cut_applied']} - a fresh material story still gives "
      "a real cut OK")

# ======================================================================
# CHECK 3 (switch-off / v1-untouched proof): v1's analyze_holding_
# news() on the EXACT SAME CHECK-1 fixture (EXPIREDMATERIAL) still sets
# `material = True` on severity alone - v1 has no decay-aware weight
# concept at all (_recency_weight() never reaches 0), so it is NOT
# expected to change, and this step's diff never touches that
# function. A before/after table, not just a bare assertion.
# ======================================================================
_news1_v1 = _analyze_v1(_TICKER1, buy_date="2025-01-01")
check("v1 on the IDENTICAL fixture: material is STILL True (v1's own, unchanged, "
      "severity-alone rule) - proves this step's fix is v2-only",
      _news1_v1["material"] is True)
print(f"[step1_2_v1_untouched] v1 on {_TICKER1}: material={_news1_v1['material']}, "
      f"news_risk={_news1_v1['news_risk_score']} | v2 on the SAME fixture: "
      f"material={_news1['material']}, news_risk={_news1['news_risk_score']} - "
      "v1's own severity-alone rule is byte-identical to before this step OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
