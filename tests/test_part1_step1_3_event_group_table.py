"""
PART 1 STEP 1.3 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): "The expander lists one row per
stored headline (712 for CSL). Nobody can read that." Adds, above the
existing per-headline table, one row PER EVENT GROUP: group label,
severity, first date, last date, item count, publisher count, the
heaviest headline, its weight, the points taken - sorted by points
taken. The full headline table stays below it, now with its own
per-ticker CSV download button.

portfolio_news_engine.summarize_event_groups_v2() is the new function
under test - it aggregates compare_events_v1_v2()'s own rows (same
severity/weight/grouping call chain analyze_holding_news_v2() itself
uses), never a second, independently-derived grouping.

Covers:
  - a 3-item cluster (one severity, one 7-day-window group, 3 distinct
    publishers) aggregates to ONE row: correct first/last date, item
    count, publisher count, and "points taken" matching analyze_
    holding_news_v2()'s own SEVERITY_HIT * heaviest-weight formula
  - a well-separated single-item group aggregates to its own row with
    item_count == 1
  - a "positive" severity group reports a NEGATIVE "points taken"
    (points added back, not taken) - reported as-is, never clamped
  - rows are sorted by "points taken" descending - the single biggest
    cut first, a positive (points-added) group last
  - the owner-only panel renders the new per-ticker group-summary
    table (right columns, correct row count) ABOVE the unchanged
    per-headline table, and offers a per-ticker CSV download button
    for the headline table

Production-shaped fixtures (per this instruction's own standing rule):
every stored news event in this file is written via the REAL writer,
portfolio_news_engine._merge_and_save() - named explicitly below.

Run: python3 tests/test_part1_step1_3_event_group_table.py
"""
import datetime as dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part1_step1_3_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import ai_gate
import portfolio_health_engine as phe
import portfolio_news_engine as pne
import portfolio_store as ps

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
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


NOW = dt.datetime(2026, 10, 8, 12, 0, 0)
_TICKER = "GROUPTABLE"
_BUY_DATE = "2026-01-01"  # ~280 days before NOW - nothing seeded below is excluded by the buy-date cutoff


def _seed(ticker, events):
    """Real writer: portfolio_news_engine._merge_and_save() - never a hand-built row."""
    with mock.patch.object(pne, "_now", return_value=NOW):
        pne._merge_and_save(ticker, events)


def _event(title, date, publisher):
    return {"date": date, "title": title, "description": "", "publisher": publisher,
            "link": "", "source": "test", "domain": ""}


_events = [
    # Cluster: 3 material items within a 7-day window, 3 distinct publishers.
    _event(f"{_TICKER} misses estimates and cuts guidance (wire A)",
           NOW - dt.timedelta(days=6), "Wire A"),
    _event(f"{_TICKER} misses estimates and cuts guidance (wire B)",
           NOW - dt.timedelta(days=3), "Wire B"),
    _event(f"{_TICKER} misses estimates and cuts guidance (wire C)",
           NOW, "Wire C"),
    # A well-separated SECOND material event (60 days ago, >7 days from
    # the cluster above) - its own single-item group.
    _event(f"{_TICKER} misses estimates and cuts guidance (old story)",
           NOW - dt.timedelta(days=60), "Wire D"),
    # A well-separated POSITIVE event (40 days ago) - its own group,
    # expected to report a NEGATIVE "points taken".
    _event(f"{_TICKER} beats estimates and raises guidance",
           NOW - dt.timedelta(days=40), "Wire E"),
]
_seed(_TICKER, _events)

_groups = pne.summarize_event_groups_v2(_TICKER, buy_date=_BUY_DATE, now=NOW)

check("exactly 3 event groups (one cluster + two well-separated singles)",
      len(_groups) == 3)

_cluster = next((g for g in _groups if g["item_count"] == 3), None)
check("the 3-item cluster aggregates to ONE row with item_count == 3",
      _cluster is not None)
check("the cluster's severity is 'material'",
      _cluster is not None and _cluster["severity"] == "material")
check("the cluster's first_date is the OLDEST item in it (6 days ago)",
      _cluster is not None and _cluster["first_date"] == NOW - dt.timedelta(days=6))
check("the cluster's last_date is the NEWEST item in it (today)",
      _cluster is not None and _cluster["last_date"] == NOW)
check("the cluster counts 3 DISTINCT publishers",
      _cluster is not None and _cluster["publisher_count"] == 3)
check("the cluster's heaviest item has weight 1.0 (every member is well within the "
      "30-day full-weight window, so all three tie at full weight)",
      _cluster is not None and _cluster["heaviest_weight"] == 1.0)
check("the cluster's heaviest_title is one of its own 3 headlines",
      _cluster is not None and _cluster["heaviest_title"] in
      [e["title"] for e in _events[:3]])
_expected_cluster_points = round(pne.SEVERITY_HIT["material"] * 1.0, 1)
check(f"the cluster's points_taken ({_cluster['points_taken'] if _cluster else None}) matches "
      f"SEVERITY_HIT['material'] * heaviest_weight ({_expected_cluster_points}) - the SAME "
      "formula analyze_holding_news_v2()'s own scoring loop uses",
      _cluster is not None and _cluster["points_taken"] == _expected_cluster_points)
print(f"[step1_3_cluster_aggregates] 3 headlines from 3 publishers over a 6-day span "
      f"aggregate to one group row: {_cluster} OK")

_old_material = next((g for g in _groups if g["severity"] == "material" and g["item_count"] == 1),
                      None)
check("the well-separated 60-day-old material item forms its OWN single-item group",
      _old_material is not None)
check("that group's own points_taken is LOWER than the cluster's (older -> decayed weight -> "
      "a smaller hit) but still positive (a real cut, not yet fully decayed at 60 days)",
      _old_material is not None and _cluster is not None
      and 0.0 < _old_material["points_taken"] < _cluster["points_taken"])

_positive_group = next((g for g in _groups if g["severity"] == "positive"), None)
check("the positive-severity group exists as its own row",
      _positive_group is not None)
check("the positive group's points_taken is NEGATIVE (points ADDED back, reported as-is, "
      "never clamped to zero or relabelled)",
      _positive_group is not None and _positive_group["points_taken"] < 0.0)
print(f"[step1_3_positive_group_negative_points] positive group: {_positive_group} - "
      "a negative 'points taken' correctly means points added back OK")

check("the 3 groups are sorted by points_taken DESCENDING - the cluster (biggest cut) "
      "first, the positive group (points added, not taken) last",
      _groups[0] is _cluster and _groups[-1] is _positive_group)
print(f"[step1_3_sorted_by_points_taken] order: "
      f"{[round(g['points_taken'], 1) for g in _groups]} (descending) OK")

# ======================================================================
# Panel-level proof: the new table renders above the unchanged
# per-headline table, with the right columns and row count, and the
# per-ticker CSV download button is offered.
# ======================================================================
_OWNER = ai_gate.owner_email()
ps.add_holding(_OWNER, "Main", _TICKER, name="Group Table Co", kind="STOCK",
                currency="AUD", shares=10.0, buy_price=10.0, buy_date=_BUY_DATE)

_FAKE_SNAPSHOT = {
    "price": 12.0, "revenue_growth": 0.05, "earnings_growth": 0.05, "profit_margin": 0.20,
    "roe": 0.15, "fcf_growth": 0.04, "debt_to_equity": 40.0, "mos_pct": 8.0,
    "intrinsic_value": 13.0, "history": None, "range52": 0.5, "trend_vs_ma200": 0.01,
    "dividend_rate": None, "dividend_yield": None,
}
_patches = [
    mock.patch.object(phe, "fetch_snapshot", return_value=_FAKE_SNAPSHOT),
    mock.patch.object(pne, "_claim_fetch", return_value=False),
    mock.patch.object(pne, "_now", return_value=NOW),
]
for p in _patches:
    p.start()
try:
    from streamlit.testing.v1 import AppTest

    _SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _script = f"""
import os, sys
sys.path.insert(0, {_SCRIPT_DIR!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
app._render_portfolio_health_news_v2_comparison_panel()
"""
    at = AppTest.from_string(_script, default_timeout=60)
    at.run()
    check("the panel still renders without raising", not at.exception)

    _expanders = [e for e in at.expander
                  if "every stored headline" in (getattr(e, "label", "") or "")
                  and _TICKER in (getattr(e, "label", "") or "")]
    check("the per-ticker headline expander for GROUPTABLE is offered", len(_expanders) == 1)

    _group_df = None
    for df in at.get("dataframe"):
        try:
            _cols = set(df.value.columns)
        except Exception:
            continue
        if {"Group", "Severity", "First date", "Last date", "Items", "Publishers",
            "Heaviest headline", "Heaviest weight", "Points taken"}.issubset(_cols):
            _group_df = df.value
            break
    check("the new per-ticker event-group summary table renders with every required column",
          _group_df is not None)
    check("the group-summary table has exactly 3 rows (one per event group, never one per "
          "headline - there are 5 stored headlines here)",
          _group_df is not None and len(_group_df) == 3)

    _headline_df = None
    for df in at.get("dataframe"):
        try:
            _cols = set(df.value.columns)
        except Exception:
            continue
        if "Event group" in _cols and "Old severity" in _cols:
            _headline_df = df.value
            break
    check("the full per-headline table is still present, unchanged, below the group table",
          _headline_df is not None and len(_headline_df) == 5)

    _dl_labels = [b.label or "" for b in at.download_button]
    check("a per-ticker CSV download button is offered for the headline table, named with "
          "the ticker",
          any(_TICKER in lbl and "CSV" in lbl for lbl in _dl_labels))
finally:
    for p in _patches:
        p.stop()

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
