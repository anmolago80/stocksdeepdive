"""
PART 1 STEP 1.5 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): "GOLD.AX is stored as kind STOCK
(from the desktop import)... For everyone else: Where the holdings
table is built, if the stored kind is STOCK and the data provider's
quote type says fund, show one small line under the table naming
those tickers... Never change a holding's kind automatically."

app.py's _render_portfolio_holdings_tab() now compares each holding's
STORED kind against fetch_snapshot()'s own "quote_type" field (the
SAME field the add-holding form's own auto-detect already reads) and,
for a STOCK-kind holding the provider reports as an ETF, shows one
caption naming it (i18n key portfolio.holdings.stock_as_fund_notice,
EN+ES) - display only, the stored holding is never touched.

Covers:
  - a STOCK-kind holding whose provider quote_type is "ETF" shows the
    notice, naming that ticker
  - a STOCK-kind holding whose provider quote_type is "EQUITY" (a
    real company) shows NO notice
  - an already-correctly-typed ETF-kind holding shows NO notice
    (nothing to flag - its own kind already agrees)
  - the holding's stored `kind` is NEVER changed by rendering this
    notice - re-read directly from portfolio_store after the render

Production-shaped fixtures (per this instruction's own standing rule):
every holding is added via the real writer, portfolio_store.
add_holding() - named explicitly below.

Run: python3 tests/test_part1_step1_5_stock_as_fund_notice.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part1_step1_5_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import ai_gate
import portfolio_store as ps

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)

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

# Real writer: portfolio_store.add_holding(). "GOLDLIKE" mirrors the
# real GOLD.AX case (kind STOCK, provider says ETF) named in the
# instruction; "REALCO" is a genuine company (kind STOCK, provider
# agrees - EQUITY); "REALETF" is already correctly typed ETF.
ps.add_holding(_OWNER, "Main", "GOLDLIKE", name="Gold-Like Fund", kind="STOCK",
                currency="AUD", shares=100.0, buy_price=10.0, buy_date="2025-01-01")
ps.add_holding(_OWNER, "Main", "REALCO", name="Real Company Ltd", kind="STOCK",
                currency="AUD", shares=50.0, buy_price=20.0, buy_date="2025-01-01")
ps.add_holding(_OWNER, "Main", "REALETF", name="Real ETF", kind="ETF",
                currency="AUD", shares=200.0, buy_price=5.0, buy_date="2025-01-01")

_holdings = ps.get_holdings_all(_OWNER)
check("all 3 fixture holdings are on file", len(_holdings) == 3)

# _analyses is shaped exactly like _analyze_holding()'s own return -
# {"snapshot": {...}, ...} - only "snapshot"/"quote_type" matters for
# this notice; every other key is a harmless filler so the surrounding
# render code (which reads health/news/etc. for the table's OTHER
# columns) doesn't crash.
_FILLER_HEALTH = {"overall": 50.0, "action": "HOLD", "action_tone": "good", "red_flags": [],
                   "thesis_breaking": False, "thesis_intact": True}
_FILLER_PROGRESS = {"overall": 50.0, "verdict": "Roughly flat", "components": {}}


def _hkey(h):
    return (h.get("portfolio"), h["ticker"])


def _analysis(quote_type, price=10.0):
    return {
        "snapshot": {"price": price, "quote_type": quote_type, "dividend_yield": None,
                     "currency": "AUD"},
        "news": None, "components": {}, "health": dict(_FILLER_HEALTH),
        "progress": dict(_FILLER_PROGRESS), "is_etf": False, "iv_override": None,
        "method": "v1",
    }


_analyses = {
    _hkey({"portfolio": "Main", "ticker": "GOLDLIKE"}): _analysis("ETF"),
    _hkey({"portfolio": "Main", "ticker": "REALCO"}): _analysis("EQUITY"),
    _hkey({"portfolio": "Main", "ticker": "REALETF"}): _analysis("ETF"),
}

from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
import ai_gate
import portfolio_store as ps

_owner = ai_gate.owner_email()
_holdings = ps.get_holdings_all(_owner)

def _hkey(h):
    return (h.get("portfolio"), h["ticker"])

_FILLER_HEALTH = {_FILLER_HEALTH!r}
_FILLER_PROGRESS = {_FILLER_PROGRESS!r}

def _analysis(quote_type, price=10.0):
    return {{
        "snapshot": {{"price": price, "quote_type": quote_type, "dividend_yield": None,
                     "currency": "AUD"}},
        "news": None, "components": {{}}, "health": dict(_FILLER_HEALTH),
        "progress": dict(_FILLER_PROGRESS), "is_etf": False, "iv_override": None,
        "method": "v1",
    }}

_analyses = {{
    _hkey({{"portfolio": "Main", "ticker": "GOLDLIKE"}}): _analysis("ETF"),
    _hkey({{"portfolio": "Main", "ticker": "REALCO"}}): _analysis("EQUITY"),
    _hkey({{"portfolio": "Main", "ticker": "REALETF"}}): _analysis("ETF"),
}}

app._render_portfolio_holdings_tab(_owner, "Main", _holdings, _analyses)
"""
at = AppTest.from_string(_script, default_timeout=60)
at.run()
check("the holdings tab renders without raising", not at.exception)

# Isolate the notice's OWN caption element - other captions on this
# page (e.g. "Max investment: REALETF...") legitimately mention ticker
# names too, so searching the whole page's concatenated captions would
# give false positives/negatives; only this one element's own text is
# the actual notice under test.
_notice_line = next((c.value for c in at.get("caption") if "Stored as a stock" in (c.value or "")), "")
check("the notice's own caption element is present at all",
      bool(_notice_line))
check("the notice names GOLDLIKE (STOCK-kind, provider says ETF)",
      "GOLDLIKE" in _notice_line)
check("the notice does NOT name REALCO (STOCK-kind, provider says EQUITY - a real company)",
      "REALCO" not in _notice_line)
check("the notice does NOT name REALETF (already correctly typed ETF - nothing to flag)",
      "REALETF" not in _notice_line)

# ======================================================================
# CHECK: the holding's stored `kind` is NEVER changed by rendering
# this notice - re-read directly from storage after the render.
# ======================================================================
_reloaded = {h["ticker"]: h for h in ps.get_holdings_all(_OWNER)}
check("GOLDLIKE's stored kind is STILL 'STOCK' after rendering the notice - never "
      "auto-changed",
      _reloaded["GOLDLIKE"]["kind"] == "STOCK")
check("REALCO's stored kind is unaffected too",
      _reloaded["REALCO"]["kind"] == "STOCK")
check("REALETF's stored kind is unaffected too",
      _reloaded["REALETF"]["kind"] == "ETF")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
sys.exit(1 if failed else 0)
