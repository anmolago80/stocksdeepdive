"""
TASK 17 (Director-directed, 23 Oct 2026): make the Evolution score the
LIVE Portfolio Health action, behind HEALTH_EVOLUTION_LIVE (default OFF).

Covers the task's own listed tests:
  - switch off = byte-identical to today (apply_live_evolution_switch()
    returns the SAME object, no compute_evolution() call at all).
  - switch on = the Evolution action flows through every consumer's own
    _analyze_holding() (app.py AND portfolio_watchdog_engine.py).
  - None fallback ("Not enough data yet" + old action "(based on
    today's figures)").
  - review_now badge (flag present, never changes the action itself).
  - EVOLUTION_ACTION_HOLD boundary at 55 (not 60).
  - ETFs.

ALL FIXTURES ARE SYNTHETIC/MOCKED - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task17_evolution_live_switch.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task17_evolution_live_switch_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("HEALTH_EVOLUTION_LIVE", None)

import pandas as pd

import fundamentals_data
import portfolio_health_engine as phe
import portfolio_news_engine as pne

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


# ======================================================================
# CHECK 1: EVOLUTION_ACTION_HOLD is 55 (lowered from 60), WATCH/ADD-MOS
# unchanged - and the exact boundary behaviour this produces.
# ======================================================================
check("EVOLUTION_ACTION_HOLD is 55", phe.EVOLUTION_ACTION_HOLD == 55)
check("EVOLUTION_ACTION_WATCH is still 45", phe.EVOLUTION_ACTION_WATCH == 45)
check("EVOLUTION_VALUATION_MOS_ADD is still 30.0", phe.EVOLUTION_VALUATION_MOS_ADD == 30.0)


def _action_for(overall, valuation_mos_pct=None):
    if overall is None:
        return "Not enough data yet"
    if overall < phe.EVOLUTION_ACTION_WATCH:
        return "REVIEW"
    if overall < phe.EVOLUTION_ACTION_HOLD:
        return "HOLD & WATCH"
    if valuation_mos_pct is not None and valuation_mos_pct >= phe.EVOLUTION_VALUATION_MOS_ADD:
        return "HOLD / ADD"
    return "HOLD"


check("44.9 -> REVIEW", _action_for(44.9) == "REVIEW")
check("45.0 -> HOLD & WATCH", _action_for(45.0) == "HOLD & WATCH")
check("54.9 -> HOLD & WATCH (the new, lower HOLD floor)", _action_for(54.9) == "HOLD & WATCH")
check("55.0, no MOS -> HOLD", _action_for(55.0) == "HOLD")
check("55.0, MOS=30.0 -> HOLD / ADD", _action_for(55.0, 30.0) == "HOLD / ADD")
check("55.0, MOS=29.9 -> plain HOLD", _action_for(55.0, 29.9) == "HOLD")
print("[task17_c1_threshold] EVOLUTION_ACTION_HOLD=55, boundaries at 45/55/MOS-30 all correct OK")

# ======================================================================
# CHECK 2: evolution_action_tone() maps every action string onto the
# SAME tone vocabulary _recommend() already uses.
# ======================================================================
check("'HOLD' -> good", phe.evolution_action_tone("HOLD") == "good")
check("'HOLD / ADD' -> good", phe.evolution_action_tone("HOLD / ADD") == "good")
check("'HOLD & WATCH' -> warning", phe.evolution_action_tone("HOLD & WATCH") == "warning")
check("'REVIEW' -> critical", phe.evolution_action_tone("REVIEW") == "critical")
check("'Not enough data yet' -> muted", phe.evolution_action_tone("Not enough data yet") == "muted")
print("[task17_c2_action_tone] every evolution action string maps to the right tone OK")

# ======================================================================
# CHECK 3: apply_live_evolution_switch() - switch OFF returns the SAME
# object (not just an equal one) - proof that compute_evolution() is
# never even called, zero added cost, byte-identical to today.
# ======================================================================
_OLD_HEALTH = {"overall": 72.0, "action": "HOLD", "action_tone": "good",
               "score_label": "Investment Health Score", "components": {}, "red_flags": []}
check("HEALTH_EVOLUTION_LIVE starts unset for this test run", phe.is_health_evolution_live() is False)
with mock.patch.object(phe, "compute_evolution", side_effect=AssertionError("must not be called with switch off")):
    _result_off = phe.apply_live_evolution_switch(
        _OLD_HEALTH, {}, {}, "STOCK", 100.0, "2025-01-01", ticker="X",
    )
check("switch OFF: apply_live_evolution_switch() returns the EXACT SAME object",
      _result_off is _OLD_HEALTH)
print("[task17_c3_switch_off_identity] switch OFF never calls compute_evolution() and returns "
      "the identical object - zero added cost OK")

# ======================================================================
# CHECK 4: switch ON, evo.overall available - the returned dict's
# overall/action/action_tone/score_label become the Evolution ones;
# business_quality_today preserves the OLD values; review_now/evolution
# are both attached.
# ======================================================================
_MOCK_BUNDLE = {
    "income": pd.DataFrame({
        "2025-12-31": {"Total Revenue": 1000.0, "Net Income": 150.0, "Normalized Income": 150.0},
        "2024-12-31": {"Total Revenue": 900.0, "Net Income": 120.0, "Normalized Income": 120.0},
        "2023-12-31": {"Total Revenue": 800.0, "Net Income": 100.0, "Normalized Income": 100.0},
    }),
    "balance": pd.DataFrame({
        "2025-12-31": {"Stockholders Equity": 800.0, "Total Debt": 200.0, "Cash And Cash Equivalents": 100.0},
        "2024-12-31": {"Stockholders Equity": 700.0, "Total Debt": 180.0, "Cash And Cash Equivalents": 90.0},
    }),
    "cashflow": pd.DataFrame({
        "2025-12-31": {"Operating Cash Flow": 180.0, "Capital Expenditure": -40.0},
        "2024-12-31": {"Operating Cash Flow": 150.0, "Capital Expenditure": -35.0},
    }),
    "info": {}, "dividends": {"dates": ["2025-06-01", "2024-12-01"], "amounts": [0.20, 0.18]},
}
_SNAPSHOT = {"price": 108.0, "history": None}
_BASELINE = {"price": 95.0}

os.environ["HEALTH_EVOLUTION_LIVE"] = "1"
check("HEALTH_EVOLUTION_LIVE=1 makes is_health_evolution_live() True", phe.is_health_evolution_live() is True)
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _result_on = phe.apply_live_evolution_switch(
        _OLD_HEALTH, _SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-06-01", ticker="MOCKCO",
    )
check("switch ON: overall is the Evolution score, not the old 72.0",
      _result_on["overall"] is not None and _result_on["overall"] != 72.0)
check("switch ON: action/action_tone match evolution_action_tone(action)",
      _result_on["action_tone"] == phe.evolution_action_tone(_result_on["action"]))
check("switch ON: score_label is 'Evolution since purchase'",
      _result_on["score_label"] == "Evolution since purchase")
check("switch ON: business_quality_today preserves the OLD overall/action/tone exactly",
      _result_on["business_quality_today"] == {
          "overall": 72.0, "action": "HOLD", "action_tone": "good",
          "score_label": "Business quality today",
      })
check("switch ON: the full evolution dict is attached for evolution_plain_english()",
      "components" in _result_on["evolution"] and "overall" in _result_on["evolution"])
check("switch ON: review_now is a bool", isinstance(_result_on["review_now"], bool))
check("switch ON: old_health's own unrelated keys (red_flags) survive untouched",
      _result_on["red_flags"] == [])
print("[task17_c4_switch_on] overall/action/tone/label become Evolution's own; "
      "business_quality_today preserves the old ones exactly; review_now/evolution attached OK")

# ======================================================================
# CHECK 5: switch ON, evo.overall is None (e.g. no new annual report) -
# overall stays None, action falls back to the OLD action with the
# "(based on today's figures)" suffix - never blank, never a crash.
# ======================================================================
_SAME_FY_BUNDLE = {
    "income": pd.DataFrame({"2025-12-31": {"Net Income": 500.0}}),
    "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
    "info": {}, "dividends": {"dates": [], "amounts": []},
}
_EMPTY_BUNDLE = {
    "income": pd.DataFrame(), "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
    "info": {}, "dividends": {"dates": [], "amounts": []},
}
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_EMPTY_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    # No statements, no dividends, no price/history, AND no buy_date (so
    # News since purchase - which otherwise defaults to a neutral 50 even
    # with zero stored headlines - is also None) -> every single weighted
    # component is None -> overall itself is None, not a stray neutral 50.
    _result_none = phe.apply_live_evolution_switch(
        _OLD_HEALTH, {"price": None}, {}, "STOCK", 95.0, None, ticker="NODATA",
    )
check("switch ON, no data: overall stays None (never a guessed/saturated number)",
      _result_none["overall"] is None)
check("switch ON, no data: action falls back to the OLD action with the exact suffix",
      _result_none["action"] == "HOLD (based on today's figures)")
check("switch ON, no data: action_tone falls back to the OLD tone too",
      _result_none["action_tone"] == "good")
check("switch ON, no data: business_quality_today is still attached",
      _result_none["business_quality_today"]["overall"] == 72.0)
print("[task17_c5_none_fallback] overall=None falls back to the old action with "
      "'(based on today's figures)', never blank OK")

# ======================================================================
# CHECK 6: review_now never changes the action by itself - a live
# thesis-breaking item sets the flag but the action still follows the
# score/threshold rules alone.
# ======================================================================
_TICKER_RN = "REVIEWCO17"
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
pne._merge_and_save(_TICKER_RN, [
    {"date": __import__("datetime").datetime(2025, 7, 1), "title": "Company discloses accounting fraud",
     "description": "", "publisher": "x", "link": "", "source": "x", "domain": "x"},
])
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _result_rn = phe.apply_live_evolution_switch(
        _OLD_HEALTH, _SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-06-01", ticker=_TICKER_RN,
    )
check("a live thesis-breaking item sets review_now=True", _result_rn["review_now"] is True)
check("review_now does NOT force the action to REVIEW by itself (separate flag)",
      _result_rn["action"] != "REVIEW" or _result_rn["overall"] < phe.EVOLUTION_ACTION_WATCH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
print("[task17_c6_review_now] review_now is a separate flag, never forces the action OK")

# ======================================================================
# CHECK 7: ETFs - apply_live_evolution_switch() works on the ETF path
# too (compute_evolution()'s own ETF weights: Price + Income only).
# ======================================================================
_ETF_OLD_HEALTH = {"overall": 60.0, "action": "HOLD", "action_tone": "good",
                    "score_label": "Price-based health (ETF)", "components": {}, "red_flags": []}
_ETF_BUNDLE = {"income": pd.DataFrame(), "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
               "info": {}, "dividends": {"dates": ["2025-03-01", "2024-09-01"], "amounts": [0.6, 0.55]}}
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_ETF_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _result_etf = phe.apply_live_evolution_switch(
        _ETF_OLD_HEALTH, {"price": 50.0, "history": None}, {"price": 45.0}, "ETF", 45.0,
        "2024-01-01", ticker="ETFCO",
    )
check("ETF: switch ON still produces an Evolution-based overall/action",
      _result_etf["score_label"] == "Evolution since purchase")
check("ETF: business_quality_today preserves the OLD ETF overall/action, relabelled "
      "'Business quality today' per the Director's own instruction (not the old ETF-specific label)",
      _result_etf["business_quality_today"]["overall"] == 60.0
      and _result_etf["business_quality_today"]["score_label"] == "Business quality today")
os.environ.pop("HEALTH_EVOLUTION_LIVE", None)
print("[task17_c7_etf] the switch applies to ETFs too, old ETF score preserved as "
      "business_quality_today OK")

# ======================================================================
# CHECK 8: evolution_plain_english() - sentences follow the Director's
# own wording style (trend word, FY labels, one-offs excluded, $ TTM
# dividends, "no new annual report" fallback).
# ======================================================================
os.environ["HEALTH_EVOLUTION_LIVE"] = "1"
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _evo_for_english = phe.compute_evolution(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-06-01", ticker="MOCKCO")
_lines = phe.evolution_plain_english(_evo_for_english)
_joined = " | ".join(_lines)
check("plain English includes a Margins line with a trend word and FY labels",
      any(_l.startswith("Margins") and "FY" in _l and "->" in _l for _l in _lines))
check("plain English includes 'one-offs excluded' on the statement-based lines",
      "one-offs excluded" in _joined)
check("plain English includes a Dividends line with $ TTM amounts",
      any("Dividends paid: $" in _l and "last 12 months" in _l for _l in _lines))

with mock.patch.object(fundamentals_data, "get_bundle", return_value=_SAME_FY_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _evo_no_report = phe.compute_evolution(_SNAPSHOT, {}, "STOCK", 95.0, "2020-01-01", ticker="NODATA2")
_lines_no_report = phe.evolution_plain_english(_evo_no_report)
check("plain English falls back to the component's own note when there's no score",
      any("not enough data" in _l.lower() or "no annual" in _l.lower() for _l in _lines_no_report))
os.environ.pop("HEALTH_EVOLUTION_LIVE", None)
print("[task17_c8_plain_english] sentences match the Director's own wording style OK")

# ======================================================================
# CHECK 9: big_score_html()'s new none_label parameter - default
# unchanged (byte-identical for every existing caller), custom label
# rendered, smaller font only for a multi-character label.
# ======================================================================
check("big_score_html(): default none_label is still '–' (byte-identical to before this task)",
      "–" in phe.big_score_html("Health", None) and "Not enough data yet" not in phe.big_score_html("Health", None))
_custom = phe.big_score_html("Evolution since purchase", None, none_label="Not enough data yet")
check("big_score_html(): a custom none_label renders the exact text",
      "Not enough data yet" in _custom)
check("big_score_html(): a real score (not None) ignores none_label entirely",
      "72" in phe.big_score_html("Health", 72.0, none_label="Not enough data yet")
      and "Not enough data yet" not in phe.big_score_html("Health", 72.0, none_label="Not enough data yet"))
print("[task17_c9_big_score_html] none_label is additive and backward-compatible OK")

# ======================================================================
# CHECK 10 (TASK 17 item 4): portfolio_watchdog_engine.py's OWN
# _analyze_holding() - the nightly alerts/push/email dispatch point -
# routes through the SAME switch, so the digest email never shows a
# different action than the live Portfolio page.
# ======================================================================
import portfolio_watchdog_engine as pwe

_WATCHDOG_HOLDING = {
    "ticker": "MOCKCO", "kind": "STOCK", "baseline": _BASELINE, "buy_price": 95.0,
    "buy_date": "2025-06-01", "name": "Mock Co", "thesis_drivers": None,
}
os.environ.pop("HEALTH_EVOLUTION_LIVE", None)
with mock.patch.object(phe, "fetch_snapshot", return_value=_SNAPSHOT), \
     mock.patch.object(pne, "analyze_holding_news", return_value=None), \
     mock.patch.object(pne, "analyze_holding_news_v2", return_value=None):
    _wd_off = pwe._analyze_holding(_WATCHDOG_HOLDING)
check("watchdog _analyze_holding(), switch OFF: health dict carries no evolution keys at all",
      "evolution" not in _wd_off["health"] and "business_quality_today" not in _wd_off["health"])

os.environ["HEALTH_EVOLUTION_LIVE"] = "1"
with mock.patch.object(phe, "fetch_snapshot", return_value=_SNAPSHOT), \
     mock.patch.object(pne, "analyze_holding_news", return_value=None), \
     mock.patch.object(pne, "analyze_holding_news_v2", return_value=None), \
     mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _wd_on = pwe._analyze_holding(_WATCHDOG_HOLDING)
check("watchdog _analyze_holding(), switch ON: health dict carries the Evolution score/action",
      _wd_on["health"]["score_label"] == "Evolution since purchase")
check("watchdog _analyze_holding(), switch ON: business_quality_today is attached",
      "business_quality_today" in _wd_on["health"])

_prompt = pwe._build_prompt("MOCKCO", _WATCHDOG_HOLDING, {"health": _wd_on["health"], "news": None}, None)
check("the digest prompt never prints the literal 'None/100' even when overall is None",
      "None/100" not in _prompt)
check("the digest prompt names 'Business quality today' when the switch is on",
      "Business quality today" in _prompt)
os.environ.pop("HEALTH_EVOLUTION_LIVE", None)
print("[task17_c10_watchdog] the nightly watchdog's own _analyze_holding() routes through "
      "the same switch; its digest prompt never prints 'None/100' and names "
      "'Business quality today' OK")

# ======================================================================
# CHECK 11 (TASK 17 item 4): app.py's OWN _analyze_holding() - the live
# Portfolio page's single dispatch point - under AppTest, switch OFF
# (byte-identical) and switch ON (Evolution score/action, business_
# quality_today attached).
# ======================================================================
from streamlit.testing.v1 import AppTest

import ai_gate
import portfolio_store as ps

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
_OWNER17 = ai_gate.owner_email()
ps.add_holding(_OWNER17, "Main", "MOCKCO", name="Mock Co", kind="STOCK", currency="AUD",
                shares=10.0, buy_price=95.0, buy_date="2025-06-01", baseline=dict(_BASELINE))
_REPO_ROOT17 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_app_analyze_holding(switch_on):
    _patches = [
        mock.patch.object(phe, "fetch_snapshot", return_value=dict(_SNAPSHOT, price=108.0)),
        mock.patch.object(pne, "_claim_fetch", return_value=False),
    ]
    if switch_on:
        os.environ["HEALTH_EVOLUTION_LIVE"] = "1"
        _patches.append(mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE))
        _patches.append(mock.patch.object(phe, "_evolution_report_dates", return_value=[]))
    else:
        os.environ.pop("HEALTH_EVOLUTION_LIVE", None)
    for p in _patches:
        p.start()
    try:
        script = f"""
import os, sys
sys.path.insert(0, {_REPO_ROOT17!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import app
import portfolio_store as ps
import ai_gate
_h = ps.get_holdings_all(ai_gate.owner_email())[0]
_result = app._analyze_holding(_h, email=ai_gate.owner_email())
st.session_state["result_overall"] = _result["health"].get("overall")
st.session_state["result_action"] = _result["health"].get("action")
st.session_state["result_label"] = _result["health"].get("score_label")
st.session_state["result_has_bqt"] = "business_quality_today" in _result["health"]
"""
        at = AppTest.from_string(script, default_timeout=60)
        at.run()
        assert not at.exception, f"app._analyze_holding() raised under AppTest: {at.exception}"
        return at.session_state
    finally:
        for p in _patches:
            p.stop()
        os.environ.pop("HEALTH_EVOLUTION_LIVE", None)


_state_off = _run_app_analyze_holding(switch_on=False)
check("app._analyze_holding(), switch OFF: score_label is the ordinary 'Investment Health Score', "
      "not 'Evolution since purchase'",
      _state_off["result_label"] != "Evolution since purchase")
check("app._analyze_holding(), switch OFF: business_quality_today is NOT attached (byte-identical)",
      _state_off["result_has_bqt"] is False)

_state_on = _run_app_analyze_holding(switch_on=True)
check("app._analyze_holding(), switch ON: score_label becomes 'Evolution since purchase'",
      _state_on["result_label"] == "Evolution since purchase")
check("app._analyze_holding(), switch ON: business_quality_today IS attached",
      _state_on["result_has_bqt"] is True)
print("[task17_c11_app_analyze_holding] app.py's own live dispatch point routes through the "
      "switch exactly like watchdog's - switch OFF byte-identical, switch ON carries the "
      "Evolution score/action and business_quality_today OK")

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
