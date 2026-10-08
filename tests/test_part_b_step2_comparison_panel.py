"""
PART B STEP B2 of instruction_portfolio_scoring_and_currency_table.md
(6 Oct 2026, Director-directed): the owner-only "Health score: news
method comparison (dry run)" Admin panel - app.py's
_render_portfolio_health_news_v2_comparison_panel().

Covers:
  - the panel renders without raising, via AppTest, with a real owner
    holding and real stored news events on file.
  - the comparison table's own columns are present (Ticker, Fundamentals
    average (8 parts), News Risk old/new, Health before news cut (v2),
    Cut before limit, Cut applied, Health/Action new at limit 10 and at
    limit 15, Health old, Action old, Items counted old, Events counted
    new, Thesis-breaking live) - renamed/extended per PART 1 STEP 1.1 of
    instruction_health_fixes_chart_and_new_markets.md (8 Oct 2026).
  - a per-ticker expander with the per-headline comparison table.
  - a CSV download button is offered.
  - the site-wide "every distinct ticker held" summary never shows an
    email address or a portfolio name - tickers and counts only.
  - nothing from v2 is written to portfolio_health_runs (or anywhere
    else) while the switch is unset - record_health_run() raising on
    any call never fires while this panel renders, proving the panel
    is read-only.
  - unreachable for a non-owner (page_admin_dashboard()'s own check).

Production-shaped fixtures (per this instruction's own rule): the
owner's holding is added via the real writer, portfolio_store.
add_holding(); stored news events are seeded via the real writer,
portfolio_news_engine._merge_and_save() - named explicitly here, as in
tests/test_part_b_step1_news_v2.py.

Run: python3 tests/test_part_b_step2_comparison_panel.py
"""
import datetime as dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part_b_step2_comparison_panel_test_")
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


_OWNER = ai_gate.owner_email()
NOW = dt.datetime(2026, 10, 6, 12, 0, 0)

# Real writer: portfolio_store.add_holding() - the owner's own holding,
# so the panel's "owner's own holdings" section has something to show.
ps.add_holding(_OWNER, "Main", "TESTCO", name="Test Company", kind="STOCK",
                currency="AUD", shares=100.0, buy_price=10.0, buy_date="2025-01-01")
# A second user's holding of the SAME ticker, plus a different ticker -
# for the site-wide "distinct ticker" count (never exposing WHICH user).
ps.add_holding("someoneelse@example.com", "Main", "TESTCO", shares=50.0, buy_date="2025-06-01")
ps.add_holding("someoneelse@example.com", "Main", "OTHERCO", shares=10.0, buy_date="2025-06-01")

# Real writer: portfolio_news_engine._merge_and_save() - a few stored
# headlines for TESTCO, including one that contains the ticker itself
# so _is_relevant() finds it.
pne._merge_and_save("TESTCO", [
    {"date": NOW - dt.timedelta(days=2), "title": "TESTCO misses estimates, cuts guidance",
     "description": "", "publisher": "Wire", "link": "", "source": "test", "domain": ""},
    {"date": NOW - dt.timedelta(days=1), "title": "TESTCO misses estimates, cuts guidance (follow-up)",
     "description": "", "publisher": "Other Wire", "link": "", "source": "test", "domain": ""},
])

_FAKE_SNAPSHOT = {
    "price": 12.0, "revenue_growth": 0.05, "earnings_growth": 0.05, "profit_margin": 0.20,
    "roe": 0.15, "fcf_growth": 0.04, "debt_to_equity": 40.0, "mos_pct": 8.0,
    "intrinsic_value": 13.0, "history": None, "range52": 0.5, "trend_vs_ma200": 0.01,
    "dividend_rate": None, "dividend_yield": None,
}

_patches = [
    mock.patch.object(phe, "fetch_snapshot", return_value=_FAKE_SNAPSHOT),
    mock.patch.object(pne, "_claim_fetch", return_value=False),  # no network fetch
    mock.patch.object(phe, "record_health_run",
                       side_effect=AssertionError("record_health_run() must never be called "
                                                    "from a read-only comparison panel")),
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

    # ==================================================================
    # CHECK 1: the panel renders without raising.
    # ==================================================================
    check("panel renders without raising", not at.exception)

    _markdowns = " ".join(m.value or "" for m in at.get("markdown"))
    check("panel's own heading is present",
          "Health score: news method comparison" in _markdowns)

    # ==================================================================
    # CHECK 2: the comparison table's own columns are present, with a
    # real row for TESTCO (the owner's own holding).
    # ==================================================================
    _dataframes = list(at.get("dataframe"))
    check("at least one dataframe (the comparison table) is rendered",
          len(_dataframes) >= 1)
    _comparison_df = None
    for df in _dataframes:
        try:
            _cols = set(df.value.columns)
        except Exception:
            continue
        if "Ticker" in _cols and "Health old" in _cols:
            _comparison_df = df.value
            break
    check("the comparison table has every required column",
          _comparison_df is not None and
          {"Ticker", "Fundamentals average (8 parts)", "News Risk old", "News Risk new",
           "Health before news cut (v2)", "Cut before limit", "Cut applied",
           "Health new (limit 10)", "Action new (limit 10)",
           "Health new (limit 15)", "Action new (limit 15)",
           "Health old", "Action old",
           "Items counted old", "Events counted new", "Thesis-breaking live"
           }.issubset(set(_comparison_df.columns)))
    check("TESTCO appears as a row in the comparison table",
          _comparison_df is not None and "TESTCO" in _comparison_df["Ticker"].tolist())
    _testco_row = _comparison_df[_comparison_df["Ticker"] == "TESTCO"].iloc[0] \
        if _comparison_df is not None else None
    check("TESTCO's Items counted old (v1, raw relevant items) is >= Events counted new "
          "(v2, deduplicated groups) - the grouping fix visibly reduces the count",
          _testco_row is not None
          and _testco_row["Items counted old"] >= _testco_row["Events counted new"])

    # ==================================================================
    # CHECK 3: a per-ticker expander with the per-headline comparison.
    # ==================================================================
    _expanders = list(at.expander)
    check("a per-ticker expander is offered", len(_expanders) >= 1)
    check("the expander is labelled with the ticker",
          any("TESTCO" in (getattr(e, "label", "") or "") for e in _expanders))

    # ==================================================================
    # CHECK 4: a CSV download button is offered.
    # ==================================================================
    _dl = list(at.download_button)
    check("a 'Download comparison as CSV' button is offered",
          any("CSV" in (b.label or "") for b in _dl))

    # ==================================================================
    # CHECK 5: the site-wide "distinct ticker" summary never shows an
    # email address or a portfolio name - tickers and counts only.
    # ==================================================================
    _site_wide_df = None
    for df in _dataframes:
        try:
            _cols = set(df.value.columns)
        except Exception:
            continue
        if "Distinct holders" in _cols:
            _site_wide_df = df.value
            break
    check("the site-wide ticker/count table is present",
          _site_wide_df is not None)
    check("OTHERCO (a DIFFERENT user's holding) appears in the site-wide table too - "
          "it is genuinely site-wide, not scoped to the owner alone",
          _site_wide_df is not None and "OTHERCO" in _site_wide_df["Ticker"].tolist())
    check("TESTCO's site-wide count is 2 (the owner + someoneelse@example.com, both holders)",
          _site_wide_df is not None
          and int(_site_wide_df[_site_wide_df["Ticker"] == "TESTCO"]["Distinct holders"].iloc[0]) == 2)
    check("no email address appears anywhere in any rendered dataframe",
          not any("someoneelse@example.com" in str(df.value) for df in _dataframes))
    check("no email address appears anywhere in the panel's own markdown/caption text",
          "someoneelse@example.com" not in _markdowns
          and "someoneelse@example.com" not in " ".join(c.value or "" for c in at.get("caption")))

    # ==================================================================
    # CHECK 6: nothing from v2 is written anywhere while the switch is
    # unset - record_health_run() raising on any call never fired
    # during the render above (if it HAD fired, `at.exception` would
    # already have caught it per CHECK 1 - this check makes the intent
    # explicit and names the exact function proven never called).
    # ==================================================================
    check("portfolio_health_engine.record_health_run() was never called while rendering "
          "this read-only panel (would have raised AssertionError, caught as at.exception)",
          not at.exception)
finally:
    for p in _patches:
        p.stop()

# ======================================================================
# CHECK 7: unreachable for a non-owner - page_admin_dashboard()'s own
# ai_gate.is_owner() check runs before any panel (including this one)
# is ever called, same pattern already proven for every other Admin
# panel in tests/test_top200_commit1_coverage.py.
# ======================================================================
import inspect
_src = inspect.getsource(sys.modules["app"].page_admin_dashboard)
_render_line = _src.find("_render_portfolio_health_news_v2_comparison_panel()")
_owner_check_line = _src.find("ai_gate.is_owner(")
check("page_admin_dashboard()'s own owner check appears BEFORE this panel is ever called",
      0 < _owner_check_line < _render_line)

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
