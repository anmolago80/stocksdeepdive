"""
TASK 26 (Director-directed, 1 Nov 2026) - Comparison page crash with a
Foreign US-listed ticker.

EVIDENCE (owner screenshot, live): Comparison of YOU, RELY, BKNG, PCAR,
WDAY, NU, NWPX -> "Side-by-side comparison" crashes: AttributeError:
'float' object has no attribute 'replace', app.py line 16981
html.escape(r["Home Country"]) inside _render_scan_results
(state_prefix="cmp").

ROOT CAUSE (verified): app.py checked `if r.get("Home Country"):`. In a
mixed comparison only the foreign ticker has a real Home Country; the
other rows can hold pandas NaN (a float) for that field once their own
saved scan row has passed through a pandas DataFrame somewhere
upstream - NaN is truthy, so html.escape(NaN) raised.

Covers:
  1. The Comparison crash site itself (app.py, _render_scan_results):
     a NaN-Home-Country row renders with no crash and no label; a real
     Home Country row still shows its label.
  2. Every other place a Home Country value is rendered/escaped or
     built into a ticker->country lookup, found by grepping app.py and
     top100_render.py:
     - app.py's own Scanner-side `_home_country_by_ticker` builder (the
       dict _render_scan_results() feeds into the live loop's row
       dicts) - a NaN-valued saved-scan row is excluded from the dict
       entirely rather than crashing downstream.
     - top100_render.py's _foreign_us_listed_home_country_by_ticker()
       (TASK 24) - same NaN-truthiness bug, same fix.
     - top100_render.py's _render_top20_tab() per-row home-country
       lookup - a belt-and-braces isinstance check at the actual
       html.escape() call site (_home_country_label_html()).
  3. Regression guards: the Scanner's own Foreign US-listed deep-link
     view (TASK 22b's own proven path) still shows a real Home Country
     label correctly - these fixes only exclude non-string values,
     never a genuine string one.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task26_home_country_nan_crash.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timezone


def _leaks_nan_text(html):
    """True if the literal word 'nan' (case-insensitive, whole word -
    never a substring hit inside an unrelated word like 'financial')
    appears anywhere in `html` - what str(float('nan')) would render
    as if a NaN Home Country ever reached the page as text instead of
    being excluded before it got anywhere near a render call."""
    return re.search(r"\bnan\b", html, re.IGNORECASE) is not None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task26_home_country_nan_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

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


import scan_store
import top100_engine as te
import top100_store as ts
import top100_render as tr
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ======================================================================
# CHECK 1 (the real crash): a Comparison of one Foreign US-listed
# ticker (real Home Country) plus two "US" tickers whose saved-scan
# rows carry NaN / a missing "Home Country" field renders with no
# exception, shows the real label once, and never leaks "nan"/"NaN" as
# text anywhere on the page.
# ======================================================================
scan_store.save_scan("TASK26 FOREIGN", [
    {"Ticker": "NUFAKE", "Type": "STOCK", "Company Name": "NU Fake Co", "Price": 12.0,
     "Intrinsic Value": 15.0, "MOS %": 20.0, "Long Score": 60.0, "Quality": 70.0,
     "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
     "Growth Used": 0.1, "FCF Source": "info", "Dividend Yield %": 0.0,
     "Home Country": "Brazil"},
], source_label="test fixture")
scan_store.save_scan("TASK26 USA", [
    {"Ticker": "USAFAKE1", "Type": "STOCK", "Company Name": "USA Fake Co 1", "Price": 50.0,
     "Intrinsic Value": 55.0, "MOS %": 9.0, "Long Score": 55.0, "Quality": 65.0,
     "Psychology": 5.0, "Discovery (lite)": 28.0, "Moat": 45.0,
     "Growth Used": 0.05, "FCF Source": "reported", "Dividend Yield %": 1.0,
     # The exact real-incident shape: a row from a universe that has NO
     # genuine Home Country, carrying NaN (not None, not a missing key)
     # for that field - what the Director's own root cause names.
     "Home Country": float("nan")},
    {"Ticker": "USAFAKE2", "Type": "STOCK", "Company Name": "USA Fake Co 2", "Price": 80.0,
     "Intrinsic Value": 88.0, "MOS %": 9.0, "Long Score": 52.0, "Quality": 62.0,
     "Psychology": 5.0, "Discovery (lite)": 26.0, "Moat": 42.0,
     "Growth Used": 0.04, "FCF Source": "reported", "Dividend Yield %": 1.2,
     # Belt-and-braces: the key simply absent entirely (an older saved
     # row from before "Home Country" existed at all).
     },
], source_label="test fixture")


def _run_comparison():
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import app
st.session_state["cmp_stocks"] = ["NUFAKE", "USAFAKE1", "USAFAKE2"]
st.session_state["cmp_fresh"] = True
app.page_comparison()
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    return at


_at_cmp = _run_comparison()
check("FIX: a Comparison mixing a real Home Country (Brazil) with NaN/missing "
      "Home Country rows renders with NO exception (the real incident's own "
      "AttributeError: 'float' object has no attribute 'replace')",
      not _at_cmp.exception)
_cmp_html = " ".join(m.value or "" for m in _at_cmp.get("markdown"))
check("the foreign ticker's real Home Country label (Brazil) is shown - "
      "TWICE on this page since TASK 27 (1 Nov 2026) added the same label "
      "to the '{page_label} preview' table (shared by Scanner AND "
      "Comparison), on top of the Side-by-side table this check originally "
      "covered alone - same precedent the existing 'scan of <date>' note "
      "already set by appearing in both tables",
      _cmp_html.count("Brazil") == 2)
check("no 'nan'/'NaN' text ever leaks onto the page from the NaN-valued rows",
      not _leaks_nan_text(_cmp_html))
print("[task26_c1_comparison_no_crash] mixed Comparison (real + NaN + missing "
      "Home Country) renders cleanly, real label shown (now twice as of "
      "TASK 27) OK")

# ======================================================================
# CHECK 2 (item 2, app.py's Scanner-side builder, _home_country_by_
# ticker): exercised via the Scanner's own LIVE results view
# (_render_scan_results(state_prefix="scan") - the actual code path
# that builder feeds, driven the same way the "Change universe & Run
# Scan" button does: scan_stocks/scan_universe_name/scan_fresh session
# state. At the time THIS task was done, Scanner's own results tables
# never rendered a Home Country sub-label at all (TASK 22's own report
# overclaimed this) - that gap was closed by TASK 27 (1 Nov 2026),
# which added the SAME label to the "{page_label} preview" table AND
# the overnight table, both now covered by tests/test_director_
# task27_scanner_home_country_label.py. This check's own job was
# always narrower than a label-visibility claim: proving the builder
# itself runs over a NaN-valued row without crashing the page, same
# "no exception" bar as the real incident - still true, and still
# worth keeping as its own check even after TASK 27 added the label
# this exact code path now also feeds.
# ======================================================================
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


def _run_scanner_live_results():
    script = f"""
import os, sys
import pandas as pd
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import app
# This sandbox has no outbound network access. The Scanner's own live
# "Run Scan" loop (unlike Comparison's _comparison_row_from_saved_scan
# shortcut) has no saved-scan fallback - every ticker needs a real
# get_price_history()/get_ticker_info() result or it's silently
# skipped ("if df.empty: continue"), never reaching the row dict (and
# the _home_country_by_ticker builder) this check exists to exercise.
# Faking those two calls (never _home_country_by_ticker or anything
# this task touches) is what lets both tickers actually reach the row-
# building/render stage; everything downstream still runs for real,
# including the builder's own isinstance check. app.time.sleep is also
# neutered - _fetch_with_retry()'s own real 0.5s/1.0s backoff between
# attempts (for whatever this doesn't mock, e.g. get_cashflow_df)
# would otherwise make this slow without changing any outcome.
app.time.sleep = lambda *a, **k: None
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
app.get_price_history = lambda ticker: _fake_df.copy()
app.get_ticker_info = lambda ticker: dict(_fake_info)
st.session_state["scan_stocks"] = ["NU", "NANCO"]
st.session_state["scan_universe_name"] = "Foreign US-listed"
st.session_state["scan_fresh"] = True
app.page_scanner()
"""
    at = AppTest.from_string(script, default_timeout=120)
    at.run()
    return at


_at_scanner = _run_scanner_live_results()
check("FIX: the Scanner's own live results view (_home_country_by_ticker's "
      "builder exercised for real) renders with NO exception even with a "
      "NaN-Home-Country row (NANCO) in the SAME universe",
      not _at_scanner.exception)
_scanner_html = " ".join(m.value or "" for m in _at_scanner.get("markdown"))
check("both tickers' rows still render (NU and NANCO) - the fix excludes a "
      "non-string value from the lookup dict, it never drops the row itself",
      ">NU<" in _scanner_html and ">NANCO<" in _scanner_html)
check("no 'nan'/'NaN' text ever leaks onto the page",
      not _leaks_nan_text(_scanner_html))
print("[task26_c2_scanner_live_results] Scanner's own live results view: no "
      "crash, both rows still render, no NaN leak OK")

# ======================================================================
# CHECK 3 (item 2, top100_render.py): _foreign_us_listed_home_country_
# by_ticker() excludes a NaN-valued row from classification entirely
# (same fix as app.py's own builder); _render_top20_tab()'s own belt-
# and-braces isinstance check at the render site never crashes even if
# a NaN somehow still reached it.
# ======================================================================
check("top100_render._foreign_us_listed_home_country_by_ticker(): NANCO (NaN "
      "Home Country) is excluded entirely",
      "NANCO" not in tr._foreign_us_listed_home_country_by_ticker())
check("top100_render._foreign_us_listed_home_country_by_ticker(): NU (real "
      "Home Country) is still correctly classified as foreign",
      tr._foreign_us_listed_home_country_by_ticker().get("NU") == "Brazil")

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _dims_all_scored():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


dims = _dims_all_scored()


def _save_score(ticker):
    ts.save_score(
        ticker=ticker, quarter=f"RP2026-11-01-{ticker}", model=te.MODEL_TOP100,
        rubric_version=te.RUBRIC_VERSION, dims=dims, not_rated=False,
        inversion_scenario=None, inversion_severity=None, prompt="{}", raw_response="{}",
    )


ts.save_pool(
    [
        {"ticker": "NU", "company_name": "Nu Holdings Ltd.", "universe": "Foreign US-listed",
         "value_score": 70.0, "mos_pct": 25.0, "price": 12.0, "intrinsic_value": 16.0,
         "currency": "USD", "psychology": 5.0, "sector": "Financial Services",
         "dividend_yield_pct": 0.0},
        {"ticker": "NANCO", "company_name": "Nan Co", "universe": "Foreign US-listed",
         "value_score": 45.0, "mos_pct": 11.1, "price": 8.0, "intrinsic_value": 9.0,
         "currency": "USD", "psychology": 5.0, "sector": "Industrials",
         "dividend_yield_pct": 0.5},
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)
_save_score("NU")
_save_score("NANCO")

_enriched_t26 = tr._enriched_pool()
_finer_t26 = tr._finer_industry_by_ticker()
_foreign_map_t26 = tr._foreign_us_listed_home_country_by_ticker()
_foreign_tickers_t26 = set(_foreign_map_t26)


def _run_direct_tab():
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import top100_render
enriched = top100_render._enriched_pool()
finer_industry = top100_render._finer_industry_by_ticker()
foreign_map = top100_render._foreign_us_listed_home_country_by_ticker()
foreign_tickers = set(foreign_map)
top100_render._render_top20_tab(enriched, 'en', finer_industry, 'value',
                                 currency='USD', only_tickers=foreign_tickers,
                                 home_country_by_ticker=foreign_map)
"""
    at = AppTest.from_string(script)
    at.run(timeout=30)
    return at


_at_t26_tab = _run_direct_tab()
check("FIX: the Top 20 Foreign US-listed tab renders with NO exception even "
      "with NANCO's NaN Home Country in the pool",
      not _at_t26_tab.exception)
_t26_tab_html = " ".join(m.value or "" for m in _at_t26_tab.get("markdown"))
check("REGRESSION GUARD: NU still shows its real Home Country label (Brazil) "
      "in the Top 20 Foreign US-listed tab",
      ">NU<" in _t26_tab_html and "Brazil" in _t26_tab_html)
check("NANCO (NaN Home Country, so never classified as foreign at all - "
      "the SAME outcome as having no Home Country - see _foreign_tickers_t26 "
      "above) is correctly absent from this FOREIGN-only tab's only_tickers "
      "filter, and never leaks a 'nan' label anywhere on the page either way",
      ">NANCO<" not in _t26_tab_html and not _leaks_nan_text(_t26_tab_html))
print("[task26_c3_top20_foreign_tab] no crash, NU's real label shown, NANCO "
      "correctly excluded (not provably foreign) and its NaN never leaks OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
