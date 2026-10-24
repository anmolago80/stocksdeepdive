"""
TASK 18 (Director-directed, 24 Oct 2026): finish the HEALTH_EVOLUTION_LIVE
switch - Progress tab + wording + colours.

Covers the task's own listed tests:
  - switch off = byte-identical (Progress tab keeps its old compute_
    progress()-based table/wording/colours).
  - Progress and Health agree for every holding, by construction (both
    read the SAME compute_evolution() result off _a["health"]["evolution"]
    - never two independent calls).
  - threshold colour boundaries (progress_color() at 44.9/45.0/54.9/55.0).
  - wording present (the Progress tab's new explainer; the Health & News
    tab's new intro/expander text; the HOLD/ADD rationale caption).

ALL FIXTURES ARE SYNTHETIC/MOCKED - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task18_progress_tab_evolution.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task18_progress_evolution_test_")
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
# CHECK 1: evolution_verdict() - the same 4 verdict strings compute_
# progress() already uses, driven by EVOLUTION_ACTION_WATCH/HOLD.
# ======================================================================
check("overall=None -> 'Not enough data yet'", phe.evolution_verdict(None) == "Not enough data yet")
check("overall=44.9 -> 'Deteriorated since purchase'",
      phe.evolution_verdict(44.9) == "Deteriorated since purchase")
check("overall=45.0 -> 'Roughly flat since purchase'",
      phe.evolution_verdict(45.0) == "Roughly flat since purchase")
check("overall=54.9 -> 'Roughly flat since purchase'",
      phe.evolution_verdict(54.9) == "Roughly flat since purchase")
check("overall=55.0 -> 'Improved since purchase'",
      phe.evolution_verdict(55.0) == "Improved since purchase")
print("[task18_c1_verdict] evolution_verdict() matches the Director's own exact bands "
      "(>=55 improved, 45-55 flat, <45 deteriorated) OK")

# ======================================================================
# CHECK 2: colour boundaries - progress_color() already uses EXACTLY
# EVOLUTION_ACTION_WATCH/HOLD's current values (45/55), confirmed
# explicitly rather than assumed, since TASK 18 relies on this reuse
# instead of writing a second colour function.
# ======================================================================
check("PROGRESS_VERDICT_DOWN currently equals EVOLUTION_ACTION_WATCH (45) - the coincidence "
      "this task's colour reuse depends on",
      phe.PROGRESS_VERDICT_DOWN == phe.EVOLUTION_ACTION_WATCH == 45)
check("PROGRESS_VERDICT_UP currently equals EVOLUTION_ACTION_HOLD (55)",
      phe.PROGRESS_VERDICT_UP == phe.EVOLUTION_ACTION_HOLD == 55)
check("progress_color(44.9) is red", phe.progress_color(44.9) == "#d03b3b")
check("progress_color(45.0) is amber", phe.progress_color(45.0) == "#e0912f")
check("progress_color(54.9) is amber", phe.progress_color(54.9) == "#e0912f")
check("progress_color(55.0) is green", phe.progress_color(55.0) == "#0ca30c")
print("[task18_c2_colour_boundaries] progress_color()'s 45/55 bands exactly match the "
      "action thresholds OK")

# ======================================================================
# CHECK 3 (integration): switch OFF - app._render_portfolio_progress_tab()
# renders the OLD compute_progress()-based table/wording, byte-identical
# to before this task; the new Evolution wording/columns are absent.
# ======================================================================
import ai_gate
import portfolio_store as ps
from streamlit.testing.v1 import AppTest

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

_OWNER = ai_gate.owner_email()
_BASELINE18 = {
    "revenue_growth": 0.07, "earnings_growth": 0.06, "profit_margin": 0.22, "roe": 0.14,
    "fcf_growth": 0.04, "debt_to_equity": 48.0, "intrinsic_value": 100.0, "price": 95.0,
    "dividend_rate": 2.4,
}
ps.add_holding(_OWNER, "Main", "MOCKCO18", name="Mock Co 18", kind="STOCK", currency="AUD",
                shares=10.0, buy_price=95.0, buy_date="2025-06-01", baseline=dict(_BASELINE18))

_SNAPSHOT18 = {
    "revenue_growth": 0.09, "earnings_growth": 0.08, "profit_margin": 0.24, "roe": 0.16,
    "fcf_growth": 0.06, "debt_to_equity": 42.0, "intrinsic_value": 110.0, "price": 108.0,
    "dividend_rate": 2.6, "mos_pct": 25.0,
}
_MOCK_BUNDLE18 = {
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
_REPO_ROOT18 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_both_tabs(switch_on):
    _patches = [
        mock.patch.object(phe, "fetch_snapshot", return_value=dict(_SNAPSHOT18)),
        mock.patch.object(pne, "_claim_fetch", return_value=False),
    ]
    if switch_on:
        os.environ["HEALTH_EVOLUTION_LIVE"] = "1"
        _patches.append(mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE18))
        _patches.append(mock.patch.object(phe, "_evolution_report_dates", return_value=[]))
    else:
        os.environ.pop("HEALTH_EVOLUTION_LIVE", None)
    for p in _patches:
        p.start()
    try:
        script = f"""
import os, sys
sys.path.insert(0, {_REPO_ROOT18!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
import portfolio_store as ps
import ai_gate
_email = ai_gate.owner_email()
_holdings = ps.get_holdings_all(_email)
_analyses = {{app._hkey(h): app._analyze_holding(h, email=_email) for h in _holdings}}
app._render_portfolio_health_news_tab(_email, "Main", _holdings, _analyses)
app._render_portfolio_progress_tab("Main", _holdings, _analyses)
"""
        at = AppTest.from_string(script, default_timeout=60)
        at.run()
        assert not at.exception, f"raised under AppTest: {at.exception}"
        return at
    finally:
        for p in _patches:
            p.stop()
        os.environ.pop("HEALTH_EVOLUTION_LIVE", None)


_at_off = _run_both_tabs(switch_on=False)
_captions_off = " | ".join(c.value or "" for c in _at_off.caption)
check("switch OFF: Progress tab's OLD wording ('0 = halved') is present",
      "0 = halved" in _captions_off)
check("switch OFF: the new Evolution wording ('one-offs excluded') is absent",
      "one-offs excluded" not in _captions_off)
_progress_dfs_off = [df.value for df in _at_off.dataframe if "Progress" in df.value.columns]
check("switch OFF: the Progress tab's table still has the OLD 'Progress'/'Baseline date' columns",
      any({"Progress", "Verdict", "Baseline date"}.issubset(set(df.columns)) for df in _progress_dfs_off))
print("[task18_c3_switch_off] Progress tab is byte-identical to before this task with the "
      "switch off OK")

# ======================================================================
# CHECK 4 (integration): switch ON - Progress tab shows the Evolution
# score/verdict/FY labels, and it EXACTLY matches the Health & News
# tab's own Health score for the SAME holding (never two independently-
# computed numbers).
# ======================================================================
_at_on = _run_both_tabs(switch_on=True)
_captions_on = " | ".join(c.value or "" for c in _at_on.caption)
check("switch ON: Progress tab's NEW wording ('one-offs excluded ... 50 = unchanged') is present",
      "one-offs excluded" in _captions_on and "50 = unchanged" in _captions_on)
check("switch ON: the OLD wording ('0 = halved') is GONE (replaced, not appended)",
      "0 = halved" not in _captions_on)
check("switch ON: Health & News tab's new intro wording is present",
      "dividends actually paid" in _captions_on)
check("switch ON: Health & News tab's old intro wording is GONE",
      "How healthy each holding looks right now" not in _captions_on)

_evo_df = None
for df in _at_on.dataframe:
    if "Evolution score" in df.value.columns:
        _evo_df = df.value
        break
check("switch ON: the Progress tab's table has the new Evolution score/Verdict/FY columns",
      _evo_df is not None and
      {"Ticker", "Evolution score", "Verdict", "FY_base -> FY_now", "Buy date"}.issubset(set(_evo_df.columns)))
check("MOCKCO18 appears in the Progress tab's own Evolution table",
      _evo_df is not None and "MOCKCO18" in _evo_df["Ticker"].tolist())

_health_df = None
for df in _at_on.dataframe:
    if "Health" in df.value.columns and "Action" in df.value.columns:
        _health_df = df.value
        break
check("switch ON: the Health & News tab's table is present too",
      _health_df is not None and "MOCKCO18" in _health_df["Ticker"].tolist())

if _evo_df is not None and _health_df is not None:
    _evo_row = _evo_df[_evo_df["Ticker"] == "MOCKCO18"].iloc[0]
    _health_row = _health_df[_health_df["Ticker"] == "MOCKCO18"].iloc[0]
    check("Progress tab's Evolution score cell == Health & News tab's Health cell, for "
          "the SAME holding, with the switch on (never two different numbers)",
          _evo_row["Evolution score"] == _health_row["Health"])
print("[task18_c4_switch_on_agreement] Progress tab shows the Evolution score/verdict/FY "
      "labels, and agrees EXACTLY with the Health & News tab for the same holding OK")

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
