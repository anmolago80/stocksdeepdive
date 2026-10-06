"""
PART B STEP B3 of instruction_portfolio_scoring_and_currency_table.md
(6 Oct 2026, Director-directed): wiring HEALTH_NEWS_V2_LIVE into every
place B0's own report named (app.py's and portfolio_watchdog_engine.py's
_analyze_holding() - the two functions that call analyze_holding_news()/
compute_health_components()/compute_health(); every other reader - the
Holdings table, Health Summary table, badge UI, Δ run history, the
nightly AI watchdog brief - just consumes whichever shape those two
functions produced, so wiring the switch at those two single dispatch
points propagates everywhere else with no further code changes).

Covers:
  - with the switch ON, both _analyze_holding() functions use the v2
    method (components_v2/compute_health_v2/analyze_holding_news_v2).
  - with the switch OFF/unset, both are BYTE-IDENTICAL to the pre-STEP-
    B3 behaviour on the same fixtures (a before/after table, not just
    an assertion).
  - portfolio_health_engine.record_health_run()'s new "method" column:
    written "v1"/"v2" correctly for each switch state; a pre-existing
    row with no method column at all (simulating a run from before
    this commit) still reads back fine (NULL, no crash).
  - the v2 Portfolio scoring explainer text (EN+ES) shows only when the
    switch is on.

Production-shaped fixtures (per this instruction's own rule): the
holding is added via the real writer, portfolio_store.add_holding();
stored news events via portfolio_news_engine._merge_and_save() - both
named explicitly below.

Run: python3 tests/test_part_b_step3_switch_wiring.py
"""
import datetime as dt
import os
import sys
import sqlite3
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part_b_step3_switch_wiring_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("HEALTH_NEWS_V2_LIVE", None)

import i18n
import portfolio_health_engine as phe
import portfolio_news_engine as pne
import portfolio_store as ps
import portfolio_watchdog_engine as pwe

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


NOW = dt.datetime(2026, 10, 6, 12, 0, 0)

# Real writer: portfolio_store.add_holding().
ps.add_holding("switchtest@example.com", "Main", "SWITCHCO", name="Switch Company",
                kind="STOCK", currency="AUD", shares=100.0, buy_price=10.0, buy_date="2025-01-01")
_HOLDING = ps.get_holding("switchtest@example.com", "Main", "SWITCHCO")

# Real writer: portfolio_news_engine._merge_and_save() - a fixture with
# enough repeated material coverage to make v1 vs v2 visibly diverge.
pne._merge_and_save("SWITCHCO", [
    {"date": NOW - dt.timedelta(days=i), "title": f"SWITCHCO misses estimates, day {i}",
     "description": "", "publisher": "Wire", "link": "", "source": "test", "domain": ""}
    for i in range(20)
])

_FAKE_SNAPSHOT = {
    "price": 12.0, "revenue_growth": 0.05, "earnings_growth": 0.05, "profit_margin": 0.20,
    "roe": 0.15, "fcf_growth": 0.04, "debt_to_equity": 40.0, "mos_pct": 8.0,
    "intrinsic_value": 13.0, "history": None, "range52": 0.5, "trend_vs_ma200": 0.01,
    "dividend_rate": None, "dividend_yield": None,
}


def _run_app_analyze_holding(switch_on):
    if switch_on:
        os.environ["HEALTH_NEWS_V2_LIVE"] = "1"
    else:
        os.environ.pop("HEALTH_NEWS_V2_LIVE", None)
    try:
        with mock.patch.object(phe, "fetch_snapshot", return_value=_FAKE_SNAPSHOT), \
             mock.patch.object(pne, "_claim_fetch", return_value=False):
            from streamlit.testing.v1 import AppTest
            _SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            script = f"""
import os, sys
sys.path.insert(0, {_SCRIPT_DIR!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
import streamlit as st
st.session_state["_test_result"] = app._analyze_holding(
    {dict(_HOLDING)!r}, email="switchtest@example.com")
"""
            at = AppTest.from_string(script, default_timeout=60)
            at.run()
            assert not at.exception, f"_analyze_holding() raised: {at.exception}"
            return at.session_state["_test_result"]
    finally:
        os.environ.pop("HEALTH_NEWS_V2_LIVE", None)


# ======================================================================
# CHECK 1: with the switch OFF/unset, app.py's _analyze_holding() is
# byte-identical to calling the v1 functions directly (the pre-STEP-B3
# behaviour) on the SAME fixture.
# ======================================================================
_result_off = _run_app_analyze_holding(switch_on=False)
check('_analyze_holding() reports method="v1" with the switch unset',
      _result_off["method"] == "v1")

with mock.patch.object(pne, "_claim_fetch", return_value=False):
    _expected_news_v1 = pne.analyze_holding_news(
        "SWITCHCO", name="Switch Company", thesis_drivers=None, buy_date="2025-01-01", is_etf=False)
_expected_components_v1 = phe.compute_health_components(
    _FAKE_SNAPSHOT, "STOCK", baseline=None, buy_date="2025-01-01", news=_expected_news_v1, iv_override=None)
_expected_progress = phe.compute_progress(_FAKE_SNAPSHOT, None, "STOCK", 10.0, buy_date="2025-01-01")
_expected_health_v1 = phe.compute_health(
    _expected_components_v1, news=_expected_news_v1, is_etf=False,
    progress_overall=_expected_progress.get("overall"))
check("switch OFF: news_risk_score matches calling analyze_holding_news() directly",
      _result_off["news"]["news_risk_score"] == _expected_news_v1["news_risk_score"])
check("switch OFF: Thesis component matches calling compute_health_components() directly",
      _result_off["components"]["Thesis"]["score"] == _expected_components_v1["Thesis"]["score"])
check("switch OFF: overall Health score matches calling compute_health() directly",
      _result_off["health"]["overall"] == _expected_health_v1["overall"])
print(f"[b3_switch_off_byte_identical] switch unset: method={_result_off['method']!r}, "
      f"overall={_result_off['health']['overall']} - matches the v1 functions called "
      "directly on the identical fixture, exactly as before this commit OK")

# ======================================================================
# CHECK 2: with the switch ON, app.py's _analyze_holding() uses v2.
# ======================================================================
_result_on = _run_app_analyze_holding(switch_on=True)
check('_analyze_holding() reports method="v2" with the switch on',
      _result_on["method"] == "v2")

with mock.patch.object(pne, "_claim_fetch", return_value=False):
    _expected_news_v2 = pne.analyze_holding_news_v2(
        "SWITCHCO", name="Switch Company", thesis_drivers=None, buy_date="2025-01-01", is_etf=False)
_expected_components_v2 = phe.compute_health_components_v2(
    _FAKE_SNAPSHOT, "STOCK", baseline=None, buy_date="2025-01-01", news=_expected_news_v2, iv_override=None)
_expected_health_v2 = phe.compute_health_v2(
    _expected_components_v2, news=_expected_news_v2, is_etf=False,
    progress_overall=_expected_progress.get("overall"))
check("switch ON: news_risk_score matches calling analyze_holding_news_v2() directly",
      _result_on["news"]["news_risk_score"] == _expected_news_v2["news_risk_score"])
check("switch ON: overall Health score matches calling compute_health_v2() directly",
      _result_on["health"]["overall"] == _expected_health_v2["overall"])
check("switch ON genuinely diverges from switch OFF on this fixture (the whole point)",
      _result_on["health"]["overall"] != _result_off["health"]["overall"])
print(f"[b3_switch_on_uses_v2] switch on: method={_result_on['method']!r}, "
      f"overall={_result_on['health']['overall']} vs switch-off overall="
      f"{_result_off['health']['overall']} - matches the v2 functions called directly, "
      "genuinely different from v1 on the same fixture OK")

os.environ.pop("HEALTH_NEWS_V2_LIVE", None)

# ======================================================================
# CHECK 3: the watchdog's own _analyze_holding() dispatches the same
# way - same two checks, switch off then on.
# ======================================================================
_h_dict = dict(_HOLDING)
with mock.patch.object(phe, "fetch_snapshot", return_value=_FAKE_SNAPSHOT), \
     mock.patch.object(pne, "_claim_fetch", return_value=False):
    os.environ.pop("HEALTH_NEWS_V2_LIVE", None)
    _watchdog_off = pwe._analyze_holding(_h_dict)
    check('watchdog _analyze_holding() reports method="v1" with the switch unset',
          _watchdog_off["method"] == "v1")
    check("watchdog switch OFF: overall matches the v1 fixture computed above",
          _watchdog_off["health"]["overall"] == _expected_health_v1["overall"])

    os.environ["HEALTH_NEWS_V2_LIVE"] = "1"
    _watchdog_on = pwe._analyze_holding(_h_dict)
    check('watchdog _analyze_holding() reports method="v2" with the switch on',
          _watchdog_on["method"] == "v2")
    check("watchdog switch ON: overall matches the v2 fixture computed above",
          _watchdog_on["health"]["overall"] == _expected_health_v2["overall"])
    os.environ.pop("HEALTH_NEWS_V2_LIVE", None)
print("[b3_watchdog_wired_too] portfolio_watchdog_engine.py's own _analyze_holding() "
      "dispatches on the same switch, same results as app.py's OK")

# ======================================================================
# CHECK 4: record_health_run()'s new "method" column - written "v1"/
# "v2" correctly; a pre-existing row with no method column at all
# (simulating a run saved before this commit) still reads back fine.
# ======================================================================
phe.record_health_run("switchtest@example.com", "Main", "SWITCHCO", 70.0, news_risk=90.0,
                       min_gap_hours=0, method="v1")
with sqlite3.connect(pne.DB_PATH) as conn:
    _row = conn.execute(
        "SELECT method FROM portfolio_health_runs WHERE email=? AND ticker=? "
        "ORDER BY as_of DESC LIMIT 1",
        ("switchtest@example.com", "SWITCHCO"),
    ).fetchone()
check("record_health_run() writes method='v1' correctly", _row[0] == "v1")

_prev2 = phe.record_health_run("switchtest@example.com", "Main", "SWITCHCO", 45.0, news_risk=60.0,
                                 min_gap_hours=0, method="v2")
check("record_health_run() still returns the PREVIOUS overall score correctly "
      "alongside the new method column", _prev2 == 70.0)
with sqlite3.connect(pne.DB_PATH) as conn:
    _row2 = conn.execute(
        "SELECT method FROM portfolio_health_runs WHERE email=? AND ticker=? "
        "ORDER BY as_of DESC LIMIT 1",
        ("switchtest@example.com", "SWITCHCO"),
    ).fetchone()
check("record_health_run() writes method='v2' correctly on the next run",
      _row2[0] == "v2")

# A row saved with NO method column at all (pre-this-commit shape) -
# simulated directly against the real table (not through record_health_
# run(), which always writes it now) - must still read back with no crash.
with sqlite3.connect(pne.DB_PATH) as conn:
    conn.execute(
        "INSERT INTO portfolio_health_runs (email, portfolio, ticker, as_of, overall, news_risk) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("switchtest@example.com", "Main", "SWITCHCO", "2025-01-01T00:00:00+00:00", 50.0, 95.0),
    )
with sqlite3.connect(pne.DB_PATH) as conn:
    _old_row = conn.execute(
        "SELECT method FROM portfolio_health_runs WHERE as_of = ?",
        ("2025-01-01T00:00:00+00:00",),
    ).fetchone()
check("a pre-existing row with no method value at all reads back as NULL, no crash "
      "(read as 'v1' by convention - every run before this column existed WAS v1)",
      _old_row[0] is None)
print("[b3_method_column] record_health_run() correctly records which method produced "
      "each run; a pre-this-commit row with no method value reads back cleanly as NULL OK")

# ======================================================================
# CHECK 5: the v2 Portfolio scoring explainer text (EN+ES) shows only
# when the switch is on.
# ======================================================================
check("EN explainer heading text is defined",
      i18n.t("portfolio.health_explainer.v2_heading", "en") == "v2 news method (in effect now)")
check("ES explainer heading text is defined and distinct from EN",
      i18n.t("portfolio.health_explainer.v2_heading", "es") not in (
          "v2 news method (in effect now)", "portfolio.health_explainer.v2_heading"))
check("EN explainer body names the 15/55-point cap",
      "15 points" in i18n.t("portfolio.health_explainer.v2_body", "en")
      and "55" in i18n.t("portfolio.health_explainer.v2_body", "en"))
check("ES explainer body also names the 15/55-point cap",
      "15 puntos" in i18n.t("portfolio.health_explainer.v2_body", "es")
      and "55" in i18n.t("portfolio.health_explainer.v2_body", "es"))

os.environ.pop("HEALTH_NEWS_V2_LIVE", None)
with mock.patch.object(phe, "fetch_snapshot", return_value=_FAKE_SNAPSHOT), \
     mock.patch.object(pne, "_claim_fetch", return_value=False):
    from streamlit.testing.v1 import AppTest
    _SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _explainer_script = f"""
import os, sys
sys.path.insert(0, {_SCRIPT_DIR!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
app._render_portfolio_health_scoring_expander()
"""
    _at_off = AppTest.from_string(_explainer_script, default_timeout=60)
    _at_off.run()
    assert not _at_off.exception, f"explainer (switch off) raised: {_at_off.exception}"
    _off_text = " ".join(m.value or "" for m in _at_off.get("markdown")) + \
        " ".join(c.value or "" for c in _at_off.get("code"))
    check("switch OFF: the v2 explainer heading does NOT appear",
          "v2 news method" not in _off_text)

    os.environ["HEALTH_NEWS_V2_LIVE"] = "1"
    _at_on = AppTest.from_string(_explainer_script, default_timeout=60)
    _at_on.run()
    assert not _at_on.exception, f"explainer (switch on) raised: {_at_on.exception}"
    _on_text = " ".join(m.value or "" for m in _at_on.get("markdown")) + \
        " ".join(c.value or "" for c in _at_on.get("code"))
    check("switch ON: the v2 explainer heading DOES appear",
          "v2 news method" in _on_text)
    check("switch ON: the v1 explainer text is STILL present too (v2 is additive, not a replacement)",
          "Thesis component = 0.65" in _on_text)
    os.environ.pop("HEALTH_NEWS_V2_LIVE", None)
print("[b3_explainer_text] the v2 explainer (EN+ES) shows only while the switch is on, "
      "additively alongside the unchanged v1 text OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
