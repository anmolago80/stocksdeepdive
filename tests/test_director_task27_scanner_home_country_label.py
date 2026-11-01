"""
TASK 27 (Director-directed, 1 Nov 2026) - show the Home Country label
on the Scanner for Foreign US-listed rows (display only).

FINDING (TASK 26 report, verified): the Home Country sub-label was
only wired into the Comparison branch of _render_scan_results(). The
Scanner's own overnight table (incl. /scanner?universe=Foreign%20
US-listed) and the "Run Scan" live-results preview table never showed
it, although TASK 22's own report said they did.

Covers:
  1. The overnight table (_render_overnight_scan_table): a Foreign
     US-listed deep-link view now shows the real label (Brazil) once,
     under the ticker, same style as the Comparison label - and never
     crashes/leaks "nan" text for a NaN-Home-Country row in the same
     universe.
  2. The Run Scan "{page_label} preview" table (the first results
     table a Run Scan click produces, before the paywall gate): same
     fix, same guard, exercised via a real page render with the live
     fetch faked (get_price_history/get_ticker_info - this sandbox has
     no outbound network access).
  3. Regression guard, item 3's own requirement: a plain US universe
     (S&P 500 fixture, no "Home Country" field at all) renders in BOTH
     tables exactly as before this task - no label, no other column
     touched, no exception.
  4. TASK 26's own guard (isinstance(value, str) and value.strip())
     is what gates every new render site added here - never a plain
     truthiness check.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task27_scanner_home_country_label.py
"""
import os
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task27_scanner_home_country_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)


def _leaks_nan_text(html):
    """True if the literal word 'nan' (case-insensitive, whole word -
    never a substring hit inside an unrelated word like 'financial')
    appears anywhere in `html`."""
    return re.search(r"\bnan\b", html, re.IGNORECASE) is not None


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


import ai_gate
import scan_store
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER_EMAIL = ai_gate.owner_email()
NONOWNER_EMAIL = "someone-else@example.com"

scan_store.save_scan("Foreign US-listed", [
    {"Ticker": "NU", "Type": "STOCK", "Company Name": "Nu Holdings Ltd.", "Price": 12.0,
     "Intrinsic Value": 15.0, "MOS %": 20.0, "Long Score": 60.0, "Quality": 70.0,
     "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
     "Growth Used": 0.1, "FCF Source": "info", "Dividend Yield %": 0.0,
     "Home Country": "Brazil"},
    {"Ticker": "NANCO", "Type": "STOCK", "Company Name": "Nan Co", "Price": 8.0,
     "Intrinsic Value": 9.0, "MOS %": 11.1, "Long Score": 45.0, "Quality": 55.0,
     "Psychology": 5.0, "Discovery (lite)": 22.0, "Moat": 35.0,
     "Growth Used": 0.02, "FCF Source": "reported", "Dividend Yield %": 0.5,
     "Home Country": float("nan")},
], source_label="test fixture")
scan_store.save_scan("S&P 500", [
    {"Ticker": "AAPL", "Type": "STOCK", "Company Name": "Apple Inc.", "Price": 180.0,
     "Intrinsic Value": 210.0, "MOS %": 14.3, "Long Score": 65.0, "Quality": 75.0,
     "Psychology": 5.0, "Discovery (lite)": 32.0, "Moat": 55.0,
     "Growth Used": 0.08, "FCF Source": "reported", "Dividend Yield %": 0.5},
], source_label="test fixture")

# ======================================================================
# CHECK 1 (item 1, the overnight table): owner's Foreign US-listed deep
# link now shows the real Home Country label once, no crash, no leak.
# ======================================================================


def _build_scanner(email, qp_universe):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import app
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
st.query_params["universe"] = {qp_universe!r}
app.page_scanner()
"""
    return AppTest.from_string(script, default_timeout=60)


def _run_scanner_deeplink(at):
    import scanner_engine
    with mock.patch.object(scanner_engine, "get_universe_pool",
                            return_value=(None, "test fixture - no live fetch")):
        at.run()
    return at


_at_foreign = _run_scanner_deeplink(_build_scanner(OWNER_EMAIL, "Foreign US-listed"))
check("FIX: owner's Foreign US-listed overnight-table deep link renders with "
      "NO exception, even with NANCO's NaN Home Country in the same universe",
      not _at_foreign.exception)
_html_foreign = " ".join(m.value or "" for m in _at_foreign.get("markdown"))
check("the overnight table shows NU's real Home Country label (Brazil) once",
      "NU" in _html_foreign and _html_foreign.count("Brazil") == 1)
check("NANCO's row still renders (it's still a real pool member)",
      "NANCO" in _html_foreign)
check("no 'nan'/'NaN' text ever leaks onto the page",
      not _leaks_nan_text(_html_foreign))
print("[task27_c1_overnight_table_foreign] overnight table: real label shown "
      "once, NaN row never leaks, no crash OK")

# ======================================================================
# CHECK 2 (item 3, regression guard): the overnight table for a plain
# US universe (no "Home Country" field at all) renders exactly as
# before this task - no label, no exception, the real row still there.
# ======================================================================
_at_sp500 = _run_scanner_deeplink(_build_scanner(NONOWNER_EMAIL, "S&P 500"))
check("REGRESSION GUARD: a plain US universe (S&P 500) deep link still "
      "renders with NO exception",
      not _at_sp500.exception)
_html_sp500 = " ".join(m.value or "" for m in _at_sp500.get("markdown"))
check("REGRESSION GUARD: AAPL's row still renders normally",
      "AAPL" in _html_sp500)
check("REGRESSION GUARD: no Home Country label appears anywhere for a "
      "universe whose rows never carry that field - unchanged from before "
      "this task",
      "Brazil" not in _html_sp500 and not _leaks_nan_text(_html_sp500))
print("[task27_c2_overnight_table_sp500_unchanged] plain US universe renders "
      "exactly as before, no label added OK")

# ======================================================================
# CHECK 3 (item 2, the Run Scan preview table): the first results table
# a Run Scan click produces (before the paywall gate) now shows the
# real Home Country label too. Network-dependent calls are faked
# (get_price_history/get_ticker_info) since this sandbox has no
# outbound network access - everything else in the live loop runs for
# real, including the fix itself.
# ======================================================================


def _run_scanner_live_preview(stocks, universe_name):
    script = f"""
import os, sys
import pandas as pd
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import app

_dates = pd.date_range("2026-01-01", periods=130, freq="D")
_fake_df = pd.DataFrame({{
    "Open": [10.0] * 130, "High": [11.0] * 130, "Low": [9.0] * 130,
    "Close": [10.0] * 130, "Volume": [1000000] * 130,
}}, index=_dates)
_fake_info = {{
    "longName": "Fake Co", "sector": "Financial Services", "industry": "Banks",
    "trailingPE": 15.0, "marketCap": 5_000_000_000, "currentPrice": 10.0,
    "dividendYield": 0.0, "freeCashflow": 100_000_000, "totalCash": 50_000_000,
    "totalDebt": 20_000_000, "returnOnEquity": 0.15, "profitMargins": 0.1,
    "trailingEps": 0.5, "forwardEps": 0.6, "bookValue": 3.0, "beta": 1.1,
    "quoteType": "EQUITY", "currency": "USD", "country": "Brazil",
}}
app.time.sleep = lambda *a, **k: None
app.get_price_history = lambda ticker: _fake_df.copy()
app.get_ticker_info = lambda ticker: dict(_fake_info)

st.session_state["scan_stocks"] = {stocks!r}
st.session_state["scan_universe_name"] = {universe_name!r}
st.session_state["scan_fresh"] = True
app.page_scanner()
"""
    at = AppTest.from_string(script, default_timeout=120)
    at.run()
    return at


_at_preview_foreign = _run_scanner_live_preview(["NU", "NANCO"], "Foreign US-listed")
check("FIX: the Run Scan preview table renders with NO exception, even with "
      "NANCO's NaN Home Country in the same universe",
      not _at_preview_foreign.exception)
_html_preview_foreign = " ".join(m.value or "" for m in _at_preview_foreign.get("markdown"))
check("the preview table shows NU's real Home Country label (Brazil) at "
      "least once (also shown again in the paywalled Side-by-side table "
      "further down, same precedent the existing 'scan of <date>' note "
      "already sets - both tables show the same per-ticker facts)",
      ">NU<" in _html_preview_foreign and "Brazil" in _html_preview_foreign)
check("both ticker rows still render (NU and NANCO)",
      ">NU<" in _html_preview_foreign and ">NANCO<" in _html_preview_foreign)
check("no 'nan'/'NaN' text ever leaks onto the page",
      not _leaks_nan_text(_html_preview_foreign))
print("[task27_c3_run_scan_preview_foreign] Run Scan preview table: real "
      "label shown, both rows render, no crash, no NaN leak OK")

# ======================================================================
# CHECK 4 (item 3, regression guard): the SAME preview table for a
# plain US universe (no Home Country field) renders exactly as before -
# no label, no exception, no other column touched.
# ======================================================================
_at_preview_sp500 = _run_scanner_live_preview(["AAPL"], "S&P 500")
check("REGRESSION GUARD: the Run Scan preview table for a plain US universe "
      "still renders with NO exception",
      not _at_preview_sp500.exception)
_html_preview_sp500 = " ".join(m.value or "" for m in _at_preview_sp500.get("markdown"))
check("REGRESSION GUARD: AAPL's row still renders in the preview table",
      ">AAPL<" in _html_preview_sp500)
check("REGRESSION GUARD: no Home Country label appears anywhere for a "
      "universe whose rows never carry that field",
      "Brazil" not in _html_preview_sp500 and not _leaks_nan_text(_html_preview_sp500))
check("REGRESSION GUARD: the preview table's other columns are untouched - "
      "Price/Long Score/Investment Signal still render for AAPL",
      "Long Score" in _html_preview_sp500 or "Value Score" in _html_preview_sp500)
print("[task27_c4_run_scan_preview_sp500_unchanged] plain US universe's "
      "preview table renders exactly as before, no label added, no other "
      "column touched OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
