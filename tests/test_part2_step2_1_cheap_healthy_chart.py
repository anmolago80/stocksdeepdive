"""
PART 2 STEP 2.1 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): "Cheap vs healthy" shows every
holding. A live report showed a 6-holding portfolio (2 companies, 4
funds) rendering only 2 bubbles with no explanation, and a health=0
bubble clipped by the plot's own bottom edge with the "expensive &
weak" label printed over it.

app.py's _render_portfolio_cheap_healthy_map() now: places funds in
their own "Funds: no valuation" strip beside the main plot, by Health
Score only, sized by weight like the company bubbles, never inventing
an MOS%; pads the y-axis to [-10, 110] so no bubble at 0 or 100 is
clipped, and moves both corner annotations into that padding band
(never the real [0, 100] score range a bubble could occupy); and
counts every holding it can't place in one "N holding(s) not shown
above: ..." caption line, naming each one's own reason.

Covers exactly the task's own listed tests:
  - six holdings (2 companies + 4 funds), all with valid data: all SIX
    appear (2 in the main scatter, 4 in the fund strip) - no "not
    shown" line at all
  - a holding at Health 0 is fully drawn (plotted, not dropped; the
    y-axis range extends below 0 so its marker isn't clipped)
  - a holding with no Health Score at all is counted in the line
    under the chart, naming it, and never appears in either plot

Reads the actual rendered Plotly figure's own JSON spec (via AppTest's
plotly_chart element - proto.spec) rather than guessing from source,
so these assertions are against what Streamlit would actually send to
the browser.

This function takes already-computed `_rows`/`_analyses`/`_cmap` as
plain arguments (it does no fetching of its own) - no production
writer is needed for ITS OWN inputs; the fixture shapes below mirror
exactly what app.py's own _build_portfolio_rows()/_analyze_holding()
return (same field names), named in a comment at the point each is
built, per this instruction's "production-shaped fixtures" rule
applied to a function whose own inputs are pre-computed dicts rather
than a stored database row.

Run: python3 tests/test_part2_step2_1_cheap_healthy_chart.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part2_step2_1_test_")
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


def _row(ticker, pct_now=0.10):
    # Shaped exactly like app.py's _build_portfolio_rows() own row dict
    # (same field names) - only the fields _render_portfolio_cheap_
    # healthy_map() itself reads are populated; the rest of that real
    # function's output isn't needed by this one.
    return {"ticker": ticker, "portfolio": "Main", "label": ticker, "pct_now": pct_now}


def _analysis(health, is_etf, mos=None):
    # Shaped exactly like app.py's _analyze_holding() own return dict
    # (same field names: "health"/"is_etf"/"components").
    return {
        "health": ({"overall": health} if health is not None else {}),
        "is_etf": is_etf,
        "components": ({"Valuation": {"current": mos}} if mos is not None else {}),
    }


def _run(rows, analyses):
    _rows_repr = repr(rows)
    _analyses_repr = repr(analyses)
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
_rows = {_rows_repr}
_analyses = {{(r["portfolio"], r["ticker"]): a for r, a in zip(
    _rows, {_analyses_repr})}}
app._render_portfolio_cheap_healthy_map(_rows, _analyses, {{}})
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"_render_portfolio_cheap_healthy_map() raised: {at.exception}"
    return at


def _specs(at):
    return [json.loads(e.proto.spec) for e in at.get("plotly_chart")]


# ======================================================================
# CHECK 1: six holdings - 2 companies (both with MOS% and Health Score)
# + 4 funds (Health Score only, no MOS%) - ALL SIX appear, no
# "not shown" line.
# ======================================================================
_rows1 = [
    _row("COA", 0.20), _row("COB", 0.15),
    _row("FUNDA", 0.20), _row("FUNDB", 0.15), _row("FUNDC", 0.15), _row("FUNDD", 0.15),
]
_analyses1 = [
    _analysis(72.0, False, mos=18.0), _analysis(55.0, False, mos=-5.0),
    _analysis(60.0, True), _analysis(48.0, True), _analysis(70.0, True), _analysis(40.0, True),
]
_at1 = _run(_rows1, _analyses1)
_specs1 = _specs(_at1)
check("exactly one plotly chart is rendered (a single combined figure, main + fund strip)",
      len(_specs1) == 1)
_all_y = [y for trace in _specs1[0]["data"] for y in (trace.get("y") or [])]
_all_x_text = [t for trace in _specs1[0]["data"] for t in (trace.get("text") or [])]
check("all 6 tickers appear as a text label somewhere in the figure",
      all(t in _all_x_text for t in ("COA", "COB", "FUNDA", "FUNDB", "FUNDC", "FUNDD")))
check("the figure carries 2 traces (main company scatter + fund strip scatter)",
      len(_specs1[0]["data"]) == 2)
_company_trace = next(t for t in _specs1[0]["data"] if "COA" in (t.get("text") or []))
_fund_trace = next(t for t in _specs1[0]["data"] if "FUNDA" in (t.get("text") or []))
check("the company trace has exactly 2 points", len(_company_trace["text"]) == 2)
check("the fund trace has exactly 4 points", len(_fund_trace["text"]) == 4)
check("the fund trace's own x values are all the SAME (no MOS% axis for funds - placed "
      "by Health Score only)",
      len(set(_fund_trace["x"])) == 1)

_captions1 = " ".join(c.value or "" for c in _at1.get("caption"))
check("no 'not shown' line appears - all six holdings were placeable",
      "not shown above" not in _captions1)
print("[step2_1_six_holdings_all_appear] 2 companies + 4 funds -> 6 labelled points across "
      "2 traces, no holdings left out OK")

# ======================================================================
# CHECK 2: a holding at Health 0 is fully drawn (plotted, not
# dropped), and the y-axis range extends below 0 so it isn't clipped.
# ======================================================================
_rows2 = [_row("ZEROHEALTH", 0.50), _row("OTHERCO", 0.50)]
_analyses2 = [_analysis(0.0, False, mos=-20.0), _analysis(80.0, False, mos=30.0)]
_at2 = _run(_rows2, _analyses2)
_specs2 = _specs(_at2)
_trace2 = _specs2[0]["data"][0]
check("the Health-0 holding IS plotted (y=0 appears among the trace's own y values)",
      0.0 in _trace2["y"])
_yaxis2 = _specs2[0]["layout"].get("yaxis", {})
check("the y-axis range extends BELOW 0 (so a Health-0 marker isn't clipped by the "
      f"plot's own border) - range={_yaxis2.get('range')}",
      _yaxis2.get("range") is not None and _yaxis2["range"][0] < 0)
check("the y-axis range also extends ABOVE 100 (symmetric padding, so a Health-100 "
      "holding is equally protected)",
      _yaxis2.get("range") is not None and _yaxis2["range"][1] > 100)
_anns2 = _specs2[0]["layout"].get("annotations", [])
check("both corner annotations sit OUTSIDE the real [0, 100] score band (in the padding "
      "zone no real bubble can ever occupy), so they can never overlap a bubble",
      len(_anns2) == 2 and all(a["y"] < 0 or a["y"] > 100 for a in _anns2))
print(f"[step2_1_health_zero_not_clipped] y-axis range={_yaxis2.get('range')}, "
      "Health-0 holding plotted, annotations clear of the real score band OK")

# ======================================================================
# CHECK 3: a holding with NO Health Score at all is counted in the
# line under the chart, naming it, and never appears in either plot.
# ======================================================================
_rows3 = [_row("NOHEALTH", 0.33), _row("GOODCO", 0.33), _row("GOODFUND", 0.34)]
_analyses3 = [_analysis(None, False, mos=10.0), _analysis(60.0, False, mos=10.0),
              _analysis(55.0, True)]
_at3 = _run(_rows3, _analyses3)
_specs3 = _specs(_at3)
_all_text3 = [t for trace in _specs3[0]["data"] for t in (trace.get("text") or [])]
check("NOHEALTH never appears as a plotted point (no Health Score to place it by)",
      "NOHEALTH" not in _all_text3)
check("GOODCO and GOODFUND, which DO have everything they need, still appear",
      "GOODCO" in _all_text3 and "GOODFUND" in _all_text3)
_captions3 = " ".join(c.value or "" for c in _at3.get("caption"))
check("the 'not shown' line is present and names NOHEALTH with its own reason",
      "1 holding(s) not shown above" in _captions3 and "NOHEALTH" in _captions3
      and "no Health Score" in _captions3)
print("[step2_1_no_health_score_left_out] a holding with no Health Score is excluded "
      "from both plots and named in the under-chart line OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
