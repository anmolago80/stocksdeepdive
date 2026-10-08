"""
Director's extra small commit (8 Oct 2026, delivered alongside PUSH 1's
go-ahead message): "'Thesis intact N/N' counts companies only; funds
are excluded; if a portfolio holds no companies it shows 'n/a'. Test:
2 companies + 4 funds reads 2/2."

app.py's _render_portfolio_overview_tab() built _thesis_intact_count
against len(_holdings) (every holding, funds included, in the
denominator) and never excluded funds from the numerator either -
a fund's `thesis_breaking` flag is never set true (there's no thesis
to break), so every fund silently counted as "intact" alongside
whichever companies actually were. Now both the numerator and the
denominator only count non-ETF holdings (_is_etf is already how this
same function labels a holding "N/A (ETF)" in the Thesis/Moat columns
right above this metric, so this reuses that exact flag rather than
re-deriving fund-ness a second way). A portfolio with zero companies
shows "n/a" rather than a misleading "0/0".

Covers exactly the task's own listed test:
  - 2 companies (one thesis-intact, one NOT) + 4 funds -> "1/2", never
    counting the funds in either the numerator or the denominator
  - the instruction's own literal example - 2 companies where NEITHER
    is thesis-breaking + 4 funds -> reads "2/2"
  - a portfolio of funds only (0 companies) -> "n/a", never "0/0"
  - a portfolio of companies only, one thesis-breaking -> funds being
    absent entirely changes nothing (sanity: pure-company math unchanged)

Seeds via the real writer portfolio_health_engine.record_health_run()
(the production function _render_portfolio_overview_tab() itself calls
on every holding, every render) so this is a production-shaped
fixture, not a bypassed one.

Run: python3 tests/test_thesis_intact_companies_only.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="thesis_intact_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

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


from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _holding(ticker, kind="stock"):
    return {
        "portfolio": "Main", "ticker": ticker, "kind": kind, "currency": "AUD",
        "name": ticker, "shares": 10, "buy_price": 1.0, "buy_date": "2025-01-01",
        "baseline_date": "2025-01-01",
    }


def _analysis(thesis_breaking, is_etf, health=60.0):
    return {
        "snapshot": {"currency": "AUD"},
        "news": {"news_risk_score": 10.0},
        "components": {},
        "health": {"overall": health, "action": "Hold", "thesis_breaking": thesis_breaking, "red_flags": []},
        "progress": {"overall": 50.0, "verdict": "On track"},
        "is_etf": is_etf,
        "iv_override": None,
        "method": "test",
    }


def _run(holdings, analyses):
    _holdings_repr = repr(holdings)
    _analyses_repr = repr(analyses)
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
_holdings = {_holdings_repr}
_analyses_list = {_analyses_repr}
_analyses = {{app._hkey(h): a for h, a in zip(_holdings, _analyses_list)}}
app._render_portfolio_overview_tab("thesis_test@example.com", "Main", _holdings, _analyses)
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"_render_portfolio_overview_tab() raised: {at.exception}"
    return at


def _thesis_metric_value(at):
    for m in at.get("metric"):
        if m.label == "Thesis intact":
            return m.value
    return None


# ======================================================================
# CHECK 1: 2 companies (one intact, one thesis-breaking) + 4 funds ->
# "1/2" - funds never counted in either numerator or denominator.
# ======================================================================
_holdings1 = [
    _holding("COA"), _holding("COB"),
    _holding("FUNDA", kind="etf"), _holding("FUNDB", kind="etf"),
    _holding("FUNDC", kind="etf"), _holding("FUNDD", kind="etf"),
]
_analyses1 = [
    _analysis(False, False), _analysis(True, False),
    _analysis(False, True), _analysis(False, True), _analysis(False, True), _analysis(False, True),
]
_at1 = _run(_holdings1, _analyses1)
check('2 companies (1 intact, 1 breaking) + 4 funds reads "1/2"',
      _thesis_metric_value(_at1) == "1/2")

# ======================================================================
# CHECK 2: the instruction's own literal example - 2 companies, NEITHER
# thesis-breaking, + 4 funds -> "2/2".
# ======================================================================
_holdings2 = [
    _holding("COC"), _holding("COD"),
    _holding("FUNDE", kind="etf"), _holding("FUNDF", kind="etf"),
    _holding("FUNDG", kind="etf"), _holding("FUNDH", kind="etf"),
]
_analyses2 = [
    _analysis(False, False), _analysis(False, False),
    _analysis(False, True), _analysis(False, True), _analysis(False, True), _analysis(False, True),
]
_at2 = _run(_holdings2, _analyses2)
check('instruction\'s own test: 2 companies + 4 funds reads "2/2"',
      _thesis_metric_value(_at2) == "2/2")

# ======================================================================
# CHECK 3: a portfolio of funds only (0 companies) -> "n/a", never "0/0".
# ======================================================================
_holdings3 = [_holding("FUNDI", kind="etf"), _holding("FUNDJ", kind="etf")]
_analyses3 = [_analysis(False, True), _analysis(False, True)]
_at3 = _run(_holdings3, _analyses3)
check('a portfolio with zero companies reads "n/a", never "0/0"',
      _thesis_metric_value(_at3) == "n/a")

# ======================================================================
# CHECK 4: sanity - a companies-only portfolio with one thesis-breaking
# holding is unaffected by this change (no funds present at all).
# ======================================================================
_holdings4 = [_holding("COE"), _holding("COF"), _holding("COG")]
_analyses4 = [_analysis(False, False), _analysis(True, False), _analysis(False, False)]
_at4 = _run(_holdings4, _analyses4)
check('companies-only portfolio (1 of 3 breaking) still reads "2/3"',
      _thesis_metric_value(_at4) == "2/3")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
