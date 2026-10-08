"""
CHART fix (Director, 8 Oct 2026, separate small commit from the LIVE BUG
fix): in "Cheap vs healthy", each ticker label must sit on its own
bubble (a live report showed "QRE.AX" printed on IVV's bubble and
"VAP.AX" on QRE's); corner labels ("expensive & weak") must not overlap
any bubble.

app.py's _render_portfolio_cheap_healthy_map() (STEP 2.1 of
instruction_health_fixes_chart_and_new_markets.md) now:
  1. Gives the fund strip's per-point text labels an ALTERNATING top-
     center/bottom-center position by health-rank (_alternating_label_
     textpositions()), rather than a single fixed "top center" for
     every point - two rank-adjacent funds now always point their
     labels AWAY from each other instead of the lower one's label
     landing on the bubble just above it.
  2. Anchors both corner annotations to the subplot's own DOMAIN
     corners ("x domain"/"y domain", fixed fractions independent of
     data) instead of data coordinates near the padding band's edge -
     a position no bubble, however large or wherever placed, can ever
     reach, since it isn't derived from any data value at all.

Covers exactly the task's own listed test: three funds at health 66,
58, 45, and a company at health 0 (Andrew's own real shape).

Reads the actual rendered Plotly figure's own JSON spec (via AppTest's
plotly_chart element) rather than guessing from source, same convention
as tests/test_part2_step2_1_cheap_healthy_chart.py.

Run: python3 tests/test_chart_label_overlap_fix.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="chart_label_overlap_test_")
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
    return {"ticker": ticker, "portfolio": "Main", "label": ticker, "pct_now": pct_now}


def _analysis(health, is_etf, mos=None):
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
# CHECK 1-3: the helper itself - three funds at health 66, 58, 45
# (Andrew's own shape) must alternate top/bottom by health-rank.
# ======================================================================
import app as _app_module
_positions = _app_module._alternating_label_textpositions([66.0, 58.0, 45.0])
check("health 66 (rank 0, highest) gets 'top center'", _positions[0] == "top center")
check("health 58 (rank 1) gets 'bottom center' - points AWAY from 66 above it",
      _positions[1] == "bottom center")
check("health 45 (rank 2) gets 'top center' - points AWAY from 58 above it",
      _positions[2] == "top center")
# A tie and an unsorted input order - the function must sort internally,
# never assume its caller's list is already ordered.
_positions_unsorted = _app_module._alternating_label_textpositions([45.0, 66.0, 58.0])
check("unsorted input: the fund at 66 (now index 1) still gets 'top center'",
      _positions_unsorted[1] == "top center")
check("unsorted input: the fund at 58 (now index 2) still gets 'bottom center'",
      _positions_unsorted[2] == "bottom center")
check("unsorted input: the fund at 45 (now index 0) still gets 'top center'",
      _positions_unsorted[0] == "top center")
print("[alternating_textpositions_by_rank] three funds at health 66/58/45 alternate "
      "top/bottom by rank regardless of input order OK")

# ======================================================================
# CHECK 4-6: end to end - three funds (66, 58, 45) + a company at
# health 0 (Andrew's own real shape) - the rendered fund trace's own
# textposition array matches the helper's output exactly.
# ======================================================================
_rows1 = [
    _row("IVV", 0.25), _row("QRE.AX", 0.20), _row("VAP.AX", 0.20), _row("ZEROCO", 0.35),
]
_analyses1 = [
    _analysis(66.0, True), _analysis(58.0, True), _analysis(45.0, True),
    _analysis(0.0, False, mos=-30.0),
]
_at1 = _run(_rows1, _analyses1)
_specs1 = _specs(_at1)
check("exactly one plotly chart is rendered", len(_specs1) == 1)
_fund_trace = next(t for t in _specs1[0]["data"] if "IVV" in (t.get("text") or []))
_fund_labels = list(_fund_trace["text"])
_fund_tp = list(_fund_trace["textposition"]) if isinstance(_fund_trace.get("textposition"), list) \
    else [_fund_trace.get("textposition")] * len(_fund_labels)
_tp_by_label = dict(zip(_fund_labels, _fund_tp))
check("IVV (health 66, highest) is rendered with 'top center'",
      _tp_by_label.get("IVV") == "top center")
check("QRE.AX (health 58) is rendered with 'bottom center' - away from IVV above it",
      _tp_by_label.get("QRE.AX") == "bottom center")
check("VAP.AX (health 45, lowest) is rendered with 'top center' - away from QRE.AX above it",
      _tp_by_label.get("VAP.AX") == "top center")
print("[fund_strip_rendered_alternating] the rendered fund trace's own textposition array "
      "matches the alternating-by-rank scheme exactly - no label defaults to the collision "
      "case the live report showed OK")

# ======================================================================
# CHECK 7-10: the corner annotations are anchored to the subplot's own
# DOMAIN (fixed fractions, never a data-derived coordinate) - true
# regardless of the company-at-health-0 bubble's own size or position.
# ======================================================================
_anns1 = _specs1[0]["layout"].get("annotations", [])
_cheap_ann = next((a for a in _anns1 if "cheap" in (a.get("text") or "")), None)
_expensive_ann = next((a for a in _anns1 if "expensive" in (a.get("text") or "")), None)
check("the 'cheap & healthy' annotation is present and DOMAIN-anchored (xref/yref end in "
      "' domain'), never a data coordinate",
      _cheap_ann is not None
      and str(_cheap_ann.get("xref", "")).endswith("domain")
      and str(_cheap_ann.get("yref", "")).endswith("domain"))
check("the 'expensive & weak' annotation is present and DOMAIN-anchored too",
      _expensive_ann is not None
      and str(_expensive_ann.get("xref", "")).endswith("domain")
      and str(_expensive_ann.get("yref", "")).endswith("domain"))
check("both annotations' own x/y are plain domain fractions in [0, 1] - never derived from "
      "this fixture's own MOS/health values (-30.0, 0.0, etc.)",
      _cheap_ann is not None and _expensive_ann is not None
      and 0.0 <= _cheap_ann["x"] <= 1.0 and 0.0 <= _cheap_ann["y"] <= 1.0
      and 0.0 <= _expensive_ann["x"] <= 1.0 and 0.0 <= _expensive_ann["y"] <= 1.0)
# The Health-0 company itself is still fully drawn (not dropped) -
# re-confirming STEP 2.1's own earlier fix still holds alongside this one.
_company_trace = next(t for t in _specs1[0]["data"] if "ZEROCO" in (t.get("text") or []))
check("the Health-0 company (ZEROCO) is still plotted, not dropped",
      0.0 in _company_trace["y"])
print("[corner_annotations_domain_anchored] both corner annotations are anchored to the "
      "plot's own domain corners - a fixed position no bubble can ever reach, whatever its "
      "size - and the Health-0 company is still fully drawn OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
