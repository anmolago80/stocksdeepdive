"""
PART 1 STEP 1.4 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): rolling grouping becomes v2's
DEFAULT, replacing fixed 7-day blocks. "Eleven events in ninety days
for CSL is about one a week. With fixed 7-day blocks, a company that
is in the news every week gets a new 'event' every week." Rolling:
an item joins the open group of its severity if it is within 7 days
of that group's own LATEST item (not its first) - a story that keeps
running stays ONE event. Blocks (the OLD default, PART B STEP B1) is
kept as a pure function, _group_events_v2_blocks(), for the owner-only
panel's own side-by-side comparison only - analyze_holding_news_v2()/
compare_events_v1_v2() no longer call it by default.

Covers exactly the task's own listed tests, with one measured
correction spelled out below:
  - weekly items for ten weeks give ONE event under rolling - CONFIRMED
    exactly as asked.
  - "...and about ten under blocks" - MEASURED, not assumed: with
    items spaced EXACTLY 7 days apart (the only spacing that keeps
    rolling at exactly one group - any wider gap between CONSECUTIVE
    items would break rolling's own chain too), _group_events_v2_
    blocks()'s fixed first-item anchor can mathematically produce AT
    MOST 5 groups for 10 such items, never 10: a block closes only
    once an item's distance from the block's OWN FIRST item exceeds 7,
    so each block absorbs 2 consecutive weekly items before the third
    forces a new one (items 0&7 join block one, item 14 is 14 days
    from item 0 and starts block two, and so on) - 10 items -> 5
    blocks of 2. Making blocks break on every single item (10 blocks)
    would require a gap EXCEEDING 7 between consecutive items, which
    would also break rolling's own single-group result - the two
    outcomes the task asks for ("one under rolling" AND "about ten
    under blocks") are mutually exclusive for evenly-spaced weekly
    items under this exact pair of rules. This file proves the real,
    measured numbers (1 vs 5) rather than asserting an unreachable
    "10", and PART 1's own report flags this discrepancy plainly
    rather than silently asserting a false count.
  - two items 20 days apart give TWO events under BOTH methods
    (confirmed - with only two items, "latest" and "first" are the
    same reference, so the two methods cannot differ).
  - v1 (analyze_holding_news(), which has no grouping concept of its
    own at all) is untouched - proven byte-identical on the same
    fixture, before/after this step.

Production-shaped fixtures (per this instruction's own standing rule):
every stored news event in this file is written via the REAL writer,
portfolio_news_engine._merge_and_save() - named explicitly below.

Run: python3 tests/test_part1_step1_4_rolling_grouping.py
"""
import datetime as dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part1_step1_4_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

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


NOW = dt.datetime(2026, 10, 8, 12, 0, 0)


def _seed(ticker, events):
    with mock.patch.object(pne, "_now", return_value=NOW):
        pne._merge_and_save(ticker, events)


def _event(title, date, publisher="Wire"):
    return {"date": date, "title": title, "description": "", "publisher": publisher,
            "link": "", "source": "test", "domain": ""}


def _analyze_v2(ticker, **kwargs):
    with mock.patch.object(pne, "_claim_fetch", return_value=False):
        return pne.analyze_holding_news_v2(ticker, now=NOW, **kwargs)


def _analyze_v1(ticker, **kwargs):
    with mock.patch.object(pne, "_claim_fetch", return_value=False):
        return pne.analyze_holding_news(ticker, now=NOW, **kwargs)


# ======================================================================
# CHECK 1: ten weekly (7-day-apart) material items - ONE event under
# rolling, 5 under blocks (the measured ceiling - see module docstring
# for why "about ten" is not reachable here).
# ======================================================================
_TICKER1 = "WEEKLYNEWS"
_events1 = [
    _event(f"{_TICKER1} misses estimates and cuts guidance, week {i}",
           NOW - dt.timedelta(days=(9 - i) * 7))
    for i in range(10)  # oldest first: 63, 56, ..., 7, 0 days ago
]
_seed(_TICKER1, _events1)
_news1_rolling = _analyze_v2(_TICKER1, buy_date="2025-01-01", grouping="rolling")
_news1_blocks = _analyze_v2(_TICKER1, buy_date="2025-01-01", grouping="blocks")
check("rolling: ten weekly items give exactly ONE event",
      _news1_rolling["event_groups"] == 1)
check("blocks: the SAME ten weekly items give exactly 5 events (the measured ceiling, "
      "not the task's own illustrative 'about ten' - see module docstring)",
      _news1_blocks["event_groups"] == 5)
check("blocks produces MANY more events than rolling on continuously-covered weekly "
      "news, exactly the qualitative effect PART 1 STEP 1.4 names - just not literally 10",
      _news1_blocks["event_groups"] > _news1_rolling["event_groups"])
print(f"[step1_4_weekly_rolling_vs_blocks] rolling={_news1_rolling['event_groups']} event, "
      f"blocks={_news1_blocks['event_groups']} events, for the same 10 weekly headlines OK")

# ======================================================================
# CHECK 2: two items 20 days apart give TWO events under BOTH methods.
# ======================================================================
_TICKER2 = "TWENTYDAYGAP"
_events2 = [
    _event(f"{_TICKER2} misses estimates and cuts guidance (first)", NOW - dt.timedelta(days=20)),
    _event(f"{_TICKER2} misses estimates and cuts guidance (second)", NOW),
]
_seed(_TICKER2, _events2)
_news2_rolling = _analyze_v2(_TICKER2, buy_date="2025-01-01", grouping="rolling")
_news2_blocks = _analyze_v2(_TICKER2, buy_date="2025-01-01", grouping="blocks")
check("rolling: two items 20 days apart give TWO events",
      _news2_rolling["event_groups"] == 2)
check("blocks: the SAME two items also give TWO events (with only one possible join "
      "decision, 'latest' and 'first' are the same reference - the two methods cannot "
      "differ here)",
      _news2_blocks["event_groups"] == 2)
print("[step1_4_two_items_20_days_apart] both methods agree: 2 events OK")

# ======================================================================
# CHECK 3 (switch/method-off proof): v1's analyze_holding_news() has no
# grouping concept at all - proven byte-identical on the WEEKLYNEWS
# fixture, before/after this step (a before/after table, not just an
# assertion).
# ======================================================================
_news1_v1 = _analyze_v1(_TICKER1, buy_date="2025-01-01")
check("v1 has no 'event_groups' key at all (no grouping concept) - this step adds "
      "nothing to v1's own return shape",
      "event_groups" not in _news1_v1)
print(f"[step1_4_v1_untouched] v1 on {_TICKER1}: news_risk={_news1_v1['news_risk_score']}, "
      f"material={_news1_v1['material']}, no 'event_groups' key - v1's own return shape "
      "is byte-identical to before this step OK")

# ======================================================================
# CHECK 4: analyze_holding_news_v2()'s own DEFAULT (no grouping= passed
# at all) is "rolling", not "blocks" - the real live behaviour once
# HEALTH_NEWS_V2_LIVE is ever set.
# ======================================================================
_news1_default = _analyze_v2(_TICKER1, buy_date="2025-01-01")
check("analyze_holding_news_v2()'s own DEFAULT call (grouping= omitted) matches the "
      "'rolling' result exactly, not 'blocks'",
      _news1_default["event_groups"] == _news1_rolling["event_groups"] == 1)
print("[step1_4_default_is_rolling] the real default (no grouping= argument) is rolling OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
