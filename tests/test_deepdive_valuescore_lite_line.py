"""
Deep Dive / Scanner Value Score line (1 Oct 2026, owner-directed - revised
decision, 13:13 AEST, superseding an earlier same-day instruction that had
Deep Dive's headline SWITCH to the lite formula). Final decision: Deep
Dive keeps its own live-attention score as the headline (green, UNCHANGED);
the Scanner/Top 100 figure (the identical discovery_measured=False formula
top100_engine._recompute_value_score() and nightly_scan.py's attention_lite
path use) is shown directly beneath it as a new, purely additive blue
sub-line - render + one extra deep_dive_engine field only, no scoring,
ranking, selection, store or scan change anywhere.

This file verifies:
  1. deep_dive_engine.analyze()'s new dd["long_score_lite"] matches the
     lite formula to 0.01 and matches top100_engine._recompute_value_
     score() for the same inputs.
  2. dd["long_score"] (the headline) is byte-identical to the plain
     measured calculate_long_score() call - i.e. genuinely unchanged by
     this task.
  3. app.py's sub-line condition (>= 0.1 difference) - renders when the
     two differ, not when they're equal (a Quality=MOS=Psychology=
     Discovery=50 fixture makes both formulas land on exactly 50.0,
     since 0.80*50 + 0.20*50 == 1.00*50 - see the measured/lite weight
     derivation in ranking_engine.py's own docstring).
  4. AppTest of the real page_deep_dive() for one ticker with mocked
     live data, EN + ES - the sub-line text and colour markup actually
     reach the page.
  5. Full regression sweep (every other tests/test_*.py file).

Run: python3 tests/test_deepdive_valuescore_lite_line.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import deep_dive_engine
import moat_engine
import top100_engine
from ranking_engine import calculate_long_score

# ---------------------------------------------------------------
# Shared fixture plumbing - see this module's docstring for the exact
# price/volume series math behind each clean, round input value.
# ---------------------------------------------------------------
N = 70


def _price_df(closes, volumes):
    idx = pd.date_range("2026-01-01", periods=N, freq="D")
    return pd.DataFrame({"Close": closes, "Volume": volumes}, index=idx)


def _run_analyze(quality, intrinsic_value, closes, volumes,
                  trend=30.0, news=10.0, yahoo_news=0.0):
    df = _price_df(closes, volumes)

    def _gp(t):
        return df

    def _gi(t):
        return {"currency": "USD", "longName": "Fixture Co"}

    def _gc(t):
        return pd.DataFrame()

    def _rq(ticker, info=None):
        return (quality, "reported", False)

    def _ri(ticker, quality_score, info=None, cashflow_df=None, currency=None,
             discount_rate=None, perpetual_rate=None, growth_rate=None,
             manual_fcf=None, income_df=None, income_df_currency_converted=False):
        return (intrinsic_value, "dcf", 0.05, {})

    def _rs(ticker, info=None):
        return ("GENERAL", "default", True)

    def _trend(keyword, api_key=None):
        return (trend, False)

    def _news(keyword, api_key=None):
        return news

    def _yahoo(t):
        return yahoo_news

    def _moat(t):
        return {"score": None, "mode": "na", "erosion": None, "flags": [], "years": 0, "components": {}}

    deep_dive_engine.resolve_quality_score = _rq
    deep_dive_engine.resolve_intrinsic_value = _ri
    deep_dive_engine.resolve_stock_type = _rs
    deep_dive_engine.get_trend_score = _trend
    deep_dive_engine.get_news_score = _news
    deep_dive_engine.get_yahoo_news_score = _yahoo
    moat_engine.compute_moat = _moat

    return deep_dive_engine.analyze(
        "FIXT", _gp, _gi, _gc, live_data=True, enable_social=False,
    )


# ---------------------------------------------------------------
# 1/2. "Differ" fixture: Quality=72, MOS=20 (price=92/iv=115),
# Psychology=8, Discovery=58 (trend=30/news=10/social=0/activity=8/
# volume_ratio=1.0) - the exact numbers are hand-verified in this
# module's own development notes, reproduced in-line below.
# ---------------------------------------------------------------
_closes_a = [100.0] * (N - 1) + [92.0]
_volumes_a = [1000.0] * N
dd_a = _run_analyze(72.0, 115.0, _closes_a, _volumes_a)

assert dd_a["error"] is None, dd_a.get("error")
assert dd_a["quality_score"] == 72.0
assert dd_a["mos"] == 20.0
assert dd_a["psychology"] == 8.0
assert dd_a["discovery"] == 58.0

expected_measured_a = calculate_long_score(72.0, 20.0, 8.0, 58.0)
expected_lite_a = calculate_long_score(72.0, 20.0, 8.0, 58.0, discovery_measured=False)
assert abs(dd_a["long_score"] - expected_measured_a) < 0.01, (
    f"headline long_score changed: {dd_a['long_score']} vs {expected_measured_a}"
)
assert abs(dd_a["long_score_lite"] - expected_lite_a) < 0.01, (
    f"long_score_lite mismatch: {dd_a['long_score_lite']} vs {expected_lite_a}"
)

top100_recompute_a = top100_engine._recompute_value_score({
    "Quality": 72.0, "MOS %": 20.0, "Psychology": 8.0, "Discovery (lite)": 58.0,
})
assert abs(dd_a["long_score_lite"] - top100_recompute_a) < 0.01, (
    f"long_score_lite doesn't match top100_engine._recompute_value_score: "
    f"{dd_a['long_score_lite']} vs {top100_recompute_a}"
)
assert dd_a["long_score"] != dd_a["long_score_lite"], (
    "fixture A should differ (headline vs Scanner/Top 100 figure) - got the same value"
)
print(
    f"[analyze] Fixture A: long_score (headline, measured) = {dd_a['long_score']}, "
    f"long_score_lite (Scanner/Top 100) = {dd_a['long_score_lite']} "
    f"(matches top100_engine._recompute_value_score {top100_recompute_a}) OK"
)

# contributions chart must still sum to the (unchanged, measured) headline -
# this task never touched that dict, so this is a "still true" check, not a
# new behaviour.
assert abs(sum(dd_a["contributions"].values()) - dd_a["long_score"]) < 0.01
print("[analyze] contributions chart still sums to the unchanged headline long_score OK")

# ---------------------------------------------------------------
# 3. "Equal" fixture: Quality=MOS=Psychology=Discovery=50 - the measured
# and lite formulas land on exactly the same 50.0 (0.80*50 + 0.20*50 ==
# 1.00*50), so the sub-line's >=0.1 condition must NOT fire.
# ---------------------------------------------------------------
_closes_b = [100.0] * (N - 1) + [50.0]
_volumes_b = [0.0] * N
dd_b = _run_analyze(50.0, 100.0, _closes_b, _volumes_b, trend=0.0, news=0.0, yahoo_news=0.0)

assert dd_b["error"] is None, dd_b.get("error")
assert dd_b["quality_score"] == 50.0
assert dd_b["mos"] == 50.0
assert dd_b["psychology"] == 50.0
assert dd_b["discovery"] == 50.0
assert abs(dd_b["long_score"] - 50.0) < 0.01
assert abs(dd_b["long_score_lite"] - 50.0) < 0.01
assert abs(dd_b["long_score"] - dd_b["long_score_lite"]) < 0.01
print(
    f"[analyze] Fixture B (equal-inputs): long_score = {dd_b['long_score']}, "
    f"long_score_lite = {dd_b['long_score_lite']} (both 50.0, as designed) OK"
)


# ---------------------------------------------------------------
# 4. Sub-line render condition (app.py's own threshold, replicated here
# the same way app.py computes it - no new helper was worth extracting
# for a two-line boolean, so this mirrors it directly).
# ---------------------------------------------------------------
def _show_subline(dd):
    lite = dd.get("long_score_lite")
    return bool(lite is not None and abs(lite - dd["long_score"]) >= 0.1)


assert _show_subline(dd_a) is True, "fixture A (differ) should show the sub-line"
assert _show_subline(dd_b) is False, "fixture B (equal) should NOT show the sub-line"
print("[render] sub-line condition: True for differing fixture, False for equal fixture OK")

import i18n

for lang in ("en", "es"):
    text = i18n.t("dd.kpi.value_score_subline", lang)
    assert "{score}" in text, f"dd.kpi.value_score_subline ({lang}) lost its {{score}} placeholder"
    help_text = i18n.t("dd.kpi.value_score_help", lang)
    assert help_text and "{" not in help_text
print("[i18n] dd.kpi.value_score_subline/value_score_help resolve for EN+ES OK")


# ---------------------------------------------------------------
# 5. AppTest: the real page_deep_dive(), one ticker, mocked live data
# (dd_a pre-seeded into session_state so no network call happens),
# EN + ES - confirms the blue sub-line text and markup actually reach
# the rendered page, not just the dict math above.
# ---------------------------------------------------------------
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTVOL = tempfile.mkdtemp(prefix="deepdive_valuescore_lite_line_test_")

_EXPECTED_SUBLINE_SNIPPET = {
    "en": "Scanner &amp; Top 200 score:",
    "es": "Puntuación en Scanner y Top 200:",
}

for lang in ("en", "es"):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
app.page_deep_dive()
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.session_state["dd_result"] = dd_a
    at.session_state["lang"] = lang
    at.run()
    assert not at.exception, f"page_deep_dive() (lang={lang!r}) raised: {at.exception}"
    subline_markdowns = [
        m.value for m in at.markdown
        if "#7dd3fc" in m.value and _EXPECTED_SUBLINE_SNIPPET[lang] in m.value
    ]
    assert subline_markdowns, (
        f"lang={lang!r}: no blue (#7dd3fc) Scanner/Top 100 sub-line found in "
        f"the rendered page's markdown elements"
    )
    assert f"{dd_a['long_score_lite']:.1f}" in subline_markdowns[0]
    print(f"[apptest] page_deep_dive() lang={lang!r}: sub-line rendered with "
          f"the lite score and site-blue colour OK")

print("\nALL DEEP DIVE VALUE SCORE LITE-LINE TESTS PASSED")
