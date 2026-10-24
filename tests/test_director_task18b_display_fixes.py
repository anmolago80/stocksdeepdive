"""
TASK 18b (Director-directed, 24 Oct 2026): three display fixes on the
evolution views - SHADOW/LIVE-switch wording only, no scoring change.

Covers the task's own listed tests:
  1. "$" escaping - every markdown-rendered string carrying a currency
     value in the Health & News/Progress holding details (manual-
     override caption, HOLD/ADD rationale, evolution_plain_english()'s
     dividends line) escapes "$" as "\\$", so Streamlit's markdown/KaTeX
     renderer never reads a pair of literal "$" as inline math.
  2. MOS label - with an IV override active, the label says "vs your
     value A$X.XX" and the model's own MOS gets its own line.
  3. GOLD-style collapse - a holding with NO annual statements at all
     gets ONE "Company accounts: not available..." line instead of five
     identical "No annual income statement available" lines; a holding
     merely awaiting its next report (OCL-style, fy_base==fy_now) is
     left exactly as before (NOT collapsed).

Switch off = byte-identical (no evolution code path touched at all).

ALL FIXTURES ARE SYNTHETIC/MOCKED - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task18b_display_fixes.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task18b_display_fixes_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("HEALTH_EVOLUTION_LIVE", None)

import datetime as _dt18b

import pandas as pd

import fundamentals_data
import portfolio_health_engine as phe
import portfolio_news_engine as pne

_TODAY18B = _dt18b.datetime.now(_dt18b.timezone.utc).date()


def _days_ago18b(n):
    return (_TODAY18B - _dt18b.timedelta(days=n)).isoformat()

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


_SNAPSHOT18B = {
    "revenue_growth": 0.09, "earnings_growth": 0.08, "profit_margin": 0.24, "roe": 0.16,
    "fcf_growth": 0.06, "debt_to_equity": 42.0, "intrinsic_value": 110.0, "price": 108.0,
    "dividend_rate": 2.6, "mos_pct": 13.4,
}
_BASELINE18B = {
    "revenue_growth": 0.07, "earnings_growth": 0.06, "profit_margin": 0.22, "roe": 0.14,
    "fcf_growth": 0.04, "debt_to_equity": 48.0, "intrinsic_value": 100.0, "price": 95.0,
    "dividend_rate": 2.4,
}
_MOCK_BUNDLE18B = {
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
    "info": {}, "dividends": {"dates": [_days_ago18b(400), _days_ago18b(50)], "amounts": [0.26, 0.26]},
}

# ======================================================================
# CHECK 1 (item 1): evolution_plain_english()'s Dividends line escapes
# every "$" - the exact bug reported ("$0.26 -> $0.26" rendered as
# garbled KaTeX because of the paired literal dollar signs). Dividend
# dates are relative to "today" (not a fixed calendar date) so the
# trailing-12-month windows land the same way whenever this test runs:
# one payment 400 days ago falls only in the buy-date window, one
# payment 50 days ago falls only in today's window - both 0.26, so
# base_ttm == now_ttm == 0.26 regardless of the real run date.
# ======================================================================
os.environ["HEALTH_EVOLUTION_LIVE"] = "1"
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE18B), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _evo = phe.compute_evolution(_SNAPSHOT18B, _BASELINE18B, "STOCK", 95.0, _days_ago18b(100), ticker="MOCKCO18B")
_lines = phe.evolution_plain_english(_evo)
_div_line = next((l for l in _lines if l.startswith("Dividends paid:")), None)
check("the Dividends line exists", _div_line is not None)
check("the Dividends line's two dollar signs are BOTH escaped (\\$, not a bare $)",
      _div_line is not None and _div_line.count("\\$") == 2 and "A\\$0.26" in _div_line)
check("the Dividends line never contains an UNescaped '$' (every '$' is preceded by a backslash)",
      _div_line is not None and _div_line.replace("\\$", "").count("$") == 0)
check("the expected exact wording (A\\$0.26 -> A\\$0.26, last 12 months) is produced",
      _div_line == "Dividends paid: A\\$0.26 -> A\\$0.26 (last 12 months)")
print(f"[task18b_c1_dividend_escaping] {_div_line!r} OK")

# ======================================================================
# CHECK 2 (item 3): a holding with NO annual statements at all (GOLD-
# style) collapses the five identical notes into ONE line; a holding
# merely awaiting its next report (OCL-style, fy_base==fy_now) is left
# EXACTLY as before - five separate lines, NOT collapsed.
# ======================================================================
_EMPTY_BUNDLE18B = {
    "income": pd.DataFrame(), "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
    "info": {}, "dividends": {"dates": [], "amounts": []},
}
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_EMPTY_BUNDLE18B), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _evo_gold = phe.compute_evolution(_SNAPSHOT18B, {}, "STOCK", 50.0, "2025-01-01", ticker="GOLD.AX")
_gold_lines = phe.evolution_plain_english(_evo_gold)
check("GOLD-style (no statements at all): exactly ONE 'Company accounts' line, not five",
      _gold_lines.count("Company accounts: not available for this holding (fund/commodity).") == 1)
check("GOLD-style: none of the five old per-component 'No annual income statement available' "
      "lines remain",
      not any(l.startswith(("Growth:", "Margins:", "ROIC:", "FCF margin:", "Net debt/EBITDA:"))
              for l in _gold_lines))

_SAME_FY_END18B = _days_ago18b(400)
_SAME_FY_BUNDLE18B = {
    "income": pd.DataFrame({_SAME_FY_END18B: {"Net Income": 500.0}}),
    "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
    "info": {}, "dividends": {"dates": [], "amounts": []},
}
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_SAME_FY_BUNDLE18B), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    # buy_date well after the fixture's own FY end (and its public-date
    # fallback) -> fy_base == fy_now, the only column (OCL-style).
    _evo_ocl = phe.compute_evolution(_SNAPSHOT18B, {}, "STOCK", 95.0, _days_ago18b(30), ticker="OCL.AX")
_ocl_lines = phe.evolution_plain_english(_evo_ocl)
_ocl_fund_lines = [l for l in _ocl_lines if l.startswith(("Growth:", "Margins:", "ROIC:", "FCF margin:", "Net debt/EBITDA:"))]
check("OCL-style (same FY, awaiting next report): the Director's own 'keep as-is' - still "
      "FIVE separate lines, never collapsed",
      len(_ocl_fund_lines) == 5)
check("OCL-style: every one of the five says 'No new annual report since purchase' (unchanged wording)",
      all("No new annual report since purchase" in l for l in _ocl_fund_lines))
check("'Company accounts: not available' never appears for the OCL-style (awaiting-report) case",
      "Company accounts: not available for this holding (fund/commodity)." not in _ocl_lines)
os.environ.pop("HEALTH_EVOLUTION_LIVE", None)
print("[task18b_c2_gold_collapse] GOLD-style collapses to one line; OCL-style (awaiting its "
      "next report) is left exactly as before, per the Director's own 'keep as-is' OK")

# ======================================================================
# CHECK 3 (items 1+2, integration): the Valuation block's override
# badge/MOS label/HOLD-ADD rationale under AppTest - the label says "vs
# your value A$X.XX", a "Model MOS:" caption appears, and no caption on
# the page contains an unescaped, paired "$".
# ======================================================================
import ai_gate
import portfolio_store as ps
from streamlit.testing.v1 import AppTest

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

_OWNER = ai_gate.owner_email()
ps.add_holding(_OWNER, "Main", "MOCKCO18B", name="Mock Co 18b", kind="STOCK", currency="AUD",
                shares=10.0, buy_price=95.0, buy_date="2025-06-01", baseline=dict(_BASELINE18B))
ps.set_iv_override(_OWNER, "MOCKCO18B", 12.0)
_REPO_ROOT18B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

os.environ["HEALTH_EVOLUTION_LIVE"] = "1"
_patches = [
    mock.patch.object(phe, "fetch_snapshot", return_value=dict(_SNAPSHOT18B, intrinsic_value=7.59)),
    mock.patch.object(pne, "_claim_fetch", return_value=False),
    mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE18B),
    mock.patch.object(phe, "_evolution_report_dates", return_value=[]),
]
for p in _patches:
    p.start()
try:
    script = f"""
import os, sys
sys.path.insert(0, {_REPO_ROOT18B!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
import portfolio_store as ps
import ai_gate
_email = ai_gate.owner_email()
_holdings = ps.get_holdings_all(_email)
_analyses = {{app._hkey(h): app._analyze_holding(h, email=_email) for h in _holdings}}
app._render_portfolio_health_news_tab(_email, "Main", _holdings, _analyses)
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"raised: {at.exception}"
    _all_captions = [c.value or "" for c in at.caption]
    _joined_captions = " | ".join(_all_captions)
    check("the MOS metric label mentions 'vs your value A$12.00' (the owner's override)",
          any("vs your value A$12.00" in (m.label or "") for m in at.get("metric")))
    check("a 'Model MOS:' caption is present, carrying the model's own MOS separately",
          any(c.startswith("Model MOS:") for c in _all_captions))
    check("no caption on the page contains an UNescaped, paired literal '$' "
          "(every '$' is preceded by a backslash)",
          not any(c.count("$") != c.count("\\$") for c in _all_captions))
finally:
    for p in _patches:
        p.stop()
    os.environ.pop("HEALTH_EVOLUTION_LIVE", None)
print("[task18b_c3_mos_label_and_escaping] the MOS label distinguishes 'your value' from the "
      "model's own MOS, and no caption carries an unescaped paired '$' OK")

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
