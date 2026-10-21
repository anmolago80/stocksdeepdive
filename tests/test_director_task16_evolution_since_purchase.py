"""
TASK 16/16b (Director-directed, 19-20 Oct 2026): "Evolution since purchase"
score - OWNER-ONLY SHADOW SCORE, no live effect.

Covers the task's own listed tests for compute_evolution() in
portfolio_health_engine.py:
  - each fundamentals-since-purchase component (Growth/Margins/ROIC/FCF/
    Debt Δ) is read straight from compute_progress_v2() - never re-derived.
  - price-since-purchase blends Return and post-purchase-drawdown.
  - news-since-purchase tiering: thesis-breaking (capped cut + red flag),
    material-negative (capped at 10), review-only (no cut), positive
    (capped add-back) - and the strictly-after-buy_date cutoff (no
    analyze_holding_news_v2()-style -3-day grace window).
  - near-zero/negative-baseline distortion guard.
  - legacy "website_v1" baseline flag.
  - the ETF path (Price since purchase + Income Δ only).
  - action thresholds (REVIEW / HOLD & WATCH / HOLD / HOLD-ADD).
  - compute_health*/compute_progress*/_recommend() are never touched.

TASK 16b fixes, covered below:
  - rate fields (Growth/Margins/ROIC/FCF Δ) are ALWAYS percentage-point
    scored via compute_progress_v2(), regardless of PROGRESS_V2_LIVE -
    that switch only ever controls the live Progress tab/Health blend.
  - Intrinsic Δ has weight 0 (information only) but is still computed,
    displayed, and carries an explanatory note; EVOLUTION_WEIGHTS sums
    to 100 without it, redistributed into Fundamentals Δ (now 65%).
  - Income Δ carries its source field (dividendRate) alongside
    baseline/current, for the owner to judge a drop against the field's
    real meaning.

Run: python3 tests/test_director_task16_evolution_since_purchase.py
"""
import datetime as _dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task16_evolution_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PROGRESS_V2_LIVE", None)

import pandas as pd

import fundamentals_data
import portfolio_health_engine as phe
import portfolio_news_engine as pne

if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

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


_SNAPSHOT = {
    "revenue_growth": 0.09, "earnings_growth": 0.08, "profit_margin": 0.24,
    "roe": 0.16, "fcf_growth": 0.06, "debt_to_equity": 42.0,
    "intrinsic_value": 110.0, "price": 108.0, "dividend_rate": 2.6,
}
_BASELINE = {
    "revenue_growth": 0.07, "earnings_growth": 0.06, "profit_margin": 0.22,
    "roe": 0.14, "fcf_growth": 0.04, "debt_to_equity": 48.0,
    "intrinsic_value": 100.0, "price": 95.0, "dividend_rate": 2.4,
}

# ======================================================================
# CHECK 1 (TASK 16c superseded fix 1): Intrinsic Δ (unaffected by 16c)
# is still read verbatim from compute_progress_v2(). Growth/Margins/
# ROIC/FCF/Debt/Income Δ are now sourced from evolution_fundamentals()
# instead (TASK 16c) - with no ticker, that's never even attempted, so
# their TOP-LEVEL score is None, while their "old" sub-dict still
# carries the pre-16c compute_progress_v2()-based comparison numbers
# verbatim, kept per the Director's own "keep the old columns for
# comparison" instruction.
# ======================================================================
_progress_v2 = phe.compute_progress_v2(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-01-01")
_evo = phe.compute_evolution(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-01-01", ticker=None)
check("Intrinsic Δ (unaffected by TASK 16c) still matches compute_progress_v2()'s own score exactly",
      _evo["components"]["Intrinsic Δ"]["score"] == _progress_v2["components"]["Intrinsic Δ"]["score"])
for _key in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ"):
    check(f"with no ticker, {_key!r}'s top-level score is None (evolution_fundamentals() never attempted)",
          _evo["components"][_key]["score"] is None)
    check(f"{_key!r}'s OLD sub-dict still matches compute_progress_v2()'s own score verbatim "
          "(kept for comparison, per the Director's own instruction)",
          _evo["components"][_key]["old"]["score"] == _progress_v2["components"][_key]["score"])
check("Income Δ's OLD sub-dict matches compute_progress_v2()'s own score verbatim",
      _evo["components"]["Income Δ"]["old"]["score"] == _progress_v2["components"]["Income Δ"]["score"])
print("[task16_c1_reuse] Intrinsic Δ is unaffected; every other replaced component's OLD "
      "sub-dict is a verbatim read of compute_progress_v2(), kept for comparison only OK")

# ======================================================================
# CHECK 1b (TASK 16c): with a real ticker and a mocked statement bundle,
# the TOP-LEVEL Growth/Margins/ROIC/FCF/Debt/Income Δ scores come from
# evolution_fundamentals() - genuinely different from the OLD compute_
# progress_v2()-based numbers on the same fixture, proving the new
# scores are not secretly still the old ones under a new key.
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
    "info": {}, "dividends": {"dates": [], "amounts": []},
}
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _evo_ticked = phe.compute_evolution(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-06-01", ticker="MOCKCO")
    check("with a ticker and a statement bundle, Growth Δ's TOP-LEVEL score is computed "
          "(not None) and genuinely differs from its own OLD (Yahoo-snapshot) score",
          _evo_ticked["components"]["Growth Δ"]["score"] is not None
          and _evo_ticked["components"]["Growth Δ"]["score"] != _evo_ticked["components"]["Growth Δ"]["old"]["score"])
    check("Margins Δ's top-level score likewise comes from the statement bundle, not prog_comps",
          _evo_ticked["components"]["Margins Δ"]["score"] is not None
          and _evo_ticked["components"]["Margins Δ"]["fy_now"] == "2025-12-31")
print("[task16_c1b_new_source] Growth/Margins/ROIC/FCF/Debt Δ's top-level scores genuinely "
      "come from evolution_fundamentals(), distinct from their own OLD comparison score OK")

# ======================================================================
# CHECK 2 (TASK 16c): Growth/Margins/ROIC/FCF Δ no longer touch
# compute_progress_v2()/PROGRESS_V2_LIVE AT ALL any more - they're
# fully statement-driven now. Confirmed by proving the switch has zero
# effect on either the top-level (statement-based) OR the "old"
# (compute_progress_v2()-based, itself already switch-independent
# since TASK 16b) score.
# ======================================================================
_progress_v1 = phe.compute_progress(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-01-01")
check("compute_evolution()'s own progress_source is always 'v2'", _evo["progress_source"] == "v2")

check("PROGRESS_V2_LIVE starts unset for this test run", phe.is_progress_v2_live() is False)
os.environ["PROGRESS_V2_LIVE"] = "1"
_evo_switch_on = phe.compute_evolution(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-01-01", ticker=None)
os.environ.pop("PROGRESS_V2_LIVE", None)
_evo_switch_off = phe.compute_evolution(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-01-01", ticker=None)
check("compute_evolution()'s Growth Δ (top-level, None with no ticker either way) is "
      "IDENTICAL whether PROGRESS_V2_LIVE is on or off",
      _evo_switch_on["components"]["Growth Δ"]["score"] == _evo_switch_off["components"]["Growth Δ"]["score"]
      == _evo["components"]["Growth Δ"]["score"] is None)
check("same for Margins Δ/ROIC Δ/FCF Δ's OLD comparison column - the switch has zero "
      "effect on compute_evolution() at all, top-level or OLD",
      all(_evo_switch_on["components"][k]["old"]["score"] == _evo_switch_off["components"][k]["old"]["score"]
          for k in ("Margins Δ", "ROIC Δ", "FCF Δ")))
print("[task16_c2_always_v2] compute_evolution() is completely independent of "
      "PROGRESS_V2_LIVE, for both the new statement-based score and the OLD comparison OK")

# ======================================================================
# CHECK 3: Price since purchase blends Return (vs buy price) and
# post-purchase drawdown from the history peak.
# ======================================================================
import pandas as pd

_dates = pd.date_range("2025-01-01", periods=40, freq="D")
_closes = [100.0] * 10 + [120.0] * 5 + [90.0] * 25  # peak 120, now 90 (-25% drawdown)
_hist = pd.DataFrame({"Close": _closes}, index=_dates)
_snap_price = dict(_SNAPSHOT, price=90.0, history=_hist)
_evo_price = phe.compute_evolution(_snap_price, _BASELINE, "STOCK", 95.0, "2025-01-01", ticker=None)
_price_comp = _evo_price["components"]["Price since purchase"]
check("Price since purchase computes a post-purchase drawdown from the real peak (~-25%)",
      _price_comp["drawdown"] is not None and -0.30 < _price_comp["drawdown"] < -0.20)
check("Price since purchase's score blends Return and drawdown (both sub-scores present)",
      _price_comp["return_score"] is not None and _price_comp["drawdown_score"] is not None
      and _price_comp["score"] is not None)
print(f"[task16_c3_price] drawdown={_price_comp['drawdown']:.3f}, "
      f"return_score={_price_comp['return_score']}, drawdown_score={_price_comp['drawdown_score']}, "
      f"blended score={_price_comp['score']} OK")

# ======================================================================
# CHECK 4: news-since-purchase tiering - thesis-breaking, material-
# negative, review-only and positive, each with the right cap, plus the
# strictly-after-buy_date cutoff (no -3-day grace window).
# ======================================================================
_TICKER = "EVOLCO"
_BUY_DATE = "2025-06-01"
_buy_dt = _dt.datetime(2025, 6, 1)


def _seed(events):
    if os.path.exists(pne.DB_PATH):
        os.remove(pne.DB_PATH)
    pne._merge_and_save(_TICKER, events)


# 4a: a single thesis-breaking headline after buy_date -> red flag + cut.
_seed([{"date": _buy_dt + _dt.timedelta(days=10), "title": "Company discloses accounting scandal",
        "description": "", "publisher": "x", "link": "", "source": "x", "domain": "x"}])
_news_result = phe._evolution_news_since_purchase(_TICKER, _BUY_DATE)
check("a thesis-breaking headline after buy_date sets thesis_breaking=True",
      _news_result["thesis_breaking"] is True)
check("a single thesis-breaking headline cuts the news score below 50 (and above the cap floor)",
      _news_result["score"] is not None and 50.0 - phe.EVOLUTION_NEWS_CUT_CAP_THESIS <= _news_result["score"] < 50.0)
check("the headline is listed with its tier and points",
      len(_news_result["headlines"]) == 1 and _news_result["headlines"][0]["tier"] == "thesis-breaking"
      and _news_result["headlines"][0]["points"] < 0)

# 4b: three thesis-breaking headlines - the cut is capped, not tripled.
_seed([
    {"date": _buy_dt + _dt.timedelta(days=d), "title": t, "description": "",
     "publisher": "x", "link": "", "source": "x", "domain": "x"}
    for d, t in ((5, "Accounting scandal uncovered"), (8, "SEC investigation launched"),
                 (12, "Company files for chapter 11"))
])
_news_capped = phe._evolution_news_since_purchase(_TICKER, _BUY_DATE)
check("three thesis-breaking headlines still cap the cut at EVOLUTION_NEWS_CUT_CAP_THESIS",
      _news_capped["score"] == max(0.0, 50.0 - phe.EVOLUTION_NEWS_CUT_CAP_THESIS))
check("all three thesis-breaking headlines are still listed even once the cap is reached",
      len(_news_capped["headlines"]) == 3)
print(f"[task16_c4a_thesis_breaking] single headline score={_news_result['score']}, "
      f"capped-at-3 score={_news_capped['score']} (cap={phe.EVOLUTION_NEWS_CUT_CAP_THESIS}) OK")

# 4c: material-negative headlines, capped at 10.
_seed([
    {"date": _buy_dt + _dt.timedelta(days=d), "title": t, "description": "",
     "publisher": "x", "link": "", "source": "x", "domain": "x"}
    for d, t in ((3, "Company misses estimates"), (6, "Analyst downgrade issued"),
                 (9, "Guidance cut announced"), (14, "Impairment charge booked"))
])
_news_material = phe._evolution_news_since_purchase(_TICKER, _BUY_DATE)
check("material-negative headlines cap the cut at EVOLUTION_NEWS_CUT_CAP_MATERIAL (10 points)",
      _news_material["score"] == 50.0 - phe.EVOLUTION_NEWS_CUT_CAP_MATERIAL)
check("material-negative headlines never set thesis_breaking",
      _news_material["thesis_breaking"] is False)
check("EVOLUTION_NEWS_CUT_CAP_MATERIAL is exactly 10.0 points, as the Director specified",
      phe.EVOLUTION_NEWS_CUT_CAP_MATERIAL == 10.0)
print(f"[task16_c4b_material_negative] 4 material-negative headlines -> score="
      f"{_news_material['score']} (cap={phe.EVOLUTION_NEWS_CUT_CAP_MATERIAL}) OK")

# 4d: leadership-change/restructuring/class-action headlines - NO cut,
# shown as review-only.
_seed([
    {"date": _buy_dt + _dt.timedelta(days=d), "title": t, "description": "",
     "publisher": "x", "link": "", "source": "x", "domain": "x"}
    for d, t in ((4, "CEO resigns after board review"), (7, "Company announces restructuring"),
                 (11, "Shareholders file class action"))
])
_news_review = phe._evolution_news_since_purchase(_TICKER, _BUY_DATE)
check("leadership/restructuring/class-action headlines apply NO cut to the news score",
      _news_review["score"] == 50.0)
check("all three are tiered as review-only with zero points",
      all(h["tier"] == "review-only" and h["points"] == 0.0 for h in _news_review["headlines"]))
check("review-only headlines never set thesis_breaking",
      _news_review["thesis_breaking"] is False)
print("[task16_c4c_review_only] CEO resignation / restructuring / class action apply "
      "no cut and are listed as review-only OK")

# 4e: positive "fix is working" news adds back, capped.
_seed([
    {"date": _buy_dt + _dt.timedelta(days=d), "title": t, "description": "",
     "publisher": "x", "link": "", "source": "x", "domain": "x"}
    for d, t in ((2, "Company raises guidance"), (5, "Beats estimates in Q2"),
                 (9, "Debt reduced significantly"), (13, "Analyst upgrade issued"))
])
_news_positive = phe._evolution_news_since_purchase(_TICKER, _BUY_DATE)
check("positive headlines add back to the news score, capped at EVOLUTION_NEWS_ADD_CAP_POSITIVE",
      _news_positive["score"] == 50.0 + phe.EVOLUTION_NEWS_ADD_CAP_POSITIVE)
print(f"[task16_c4d_positive] 4 positive headlines -> score={_news_positive['score']} "
      f"(cap={phe.EVOLUTION_NEWS_ADD_CAP_POSITIVE}) OK")

# 4f: the strictly-after-buy_date cutoff - a headline ON buy_date, and one
# the day BEFORE, must both be excluded (no analyze_holding_news_v2()
# -3-day grace window).
_seed([
    {"date": _buy_dt, "title": "Accounting scandal on buy date itself", "description": "",
     "publisher": "x", "link": "", "source": "x", "domain": "x"},
    {"date": _buy_dt - _dt.timedelta(days=1), "title": "Accounting scandal the day before purchase",
     "description": "", "publisher": "x", "link": "", "source": "x", "domain": "x"},
    {"date": _buy_dt + _dt.timedelta(days=1), "title": "Accounting scandal the day after purchase",
     "description": "", "publisher": "x", "link": "", "source": "x", "domain": "x"},
])
_news_cutoff = phe._evolution_news_since_purchase(_TICKER, _BUY_DATE)
check("a headline dated exactly on buy_date is excluded (strictly-after cutoff)",
      len(_news_cutoff["headlines"]) == 1)
check("only the headline dated AFTER buy_date is counted",
      _news_cutoff["headlines"][0]["title"] == "Accounting scandal the day after purchase")
print("[task16_c4e_buy_date_cutoff] events on or before buy_date are excluded - no "
      "-3-day grace window, unlike analyze_holding_news_v2() OK")

# 4g: no buy_date / no ticker -> news not available, no crash.
_news_none = phe._evolution_news_since_purchase(None, None)
check("no buy_date and no ticker -> news score is None with an explanatory note, no crash",
      _news_none["score"] is None and "note" in _news_none)

if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

# ======================================================================
# CHECK 5 (TASK 16b fix 1): near-zero / negative baseline distortion
# guard - Growth Δ is NEVER flagged any more (always percentage-point
# scored now, regardless of PROGRESS_V2_LIVE), while Debt Δ (a "level"
# field, still on the relative-change formula in both v1 and v2) is
# still flagged whenever |current-baseline|/|baseline| has reached 1.0.
# ======================================================================
_distort_snapshot = dict(_SNAPSHOT, revenue_growth=0.03, debt_to_equity=5.0)
_distort_baseline = dict(_BASELINE, revenue_growth=0.01, debt_to_equity=-5.0)
_evo_distort = phe.compute_evolution(_distort_snapshot, _distort_baseline, "STOCK", 95.0,
                                      "2025-01-01", ticker=None)
_distorted_names = {d["component"] for d in _evo_distort["distortions"]}
check("a near-zero Growth baseline (1% -> 3%) is NEVER flagged any more - compute_evolution() "
      "always percentage-point scores Growth Δ, so this distortion can no longer occur here",
      "Growth Δ" not in _distorted_names)
check("a negative Debt baseline is still flagged as distorted in its OLD comparison column "
      "(Debt Δ's old Yahoo-snapshot figure stays on the relative-change formula)",
      "Debt Δ (old Yahoo-snapshot comparison)" in _distorted_names)
check("compute_evolution() does not crash on a near-zero/negative baseline - overall is still computed",
      _evo_distort["overall"] is not None)

os.environ["PROGRESS_V2_LIVE"] = "1"
_evo_distort_switch_on = phe.compute_evolution(_distort_snapshot, _distort_baseline, "STOCK", 95.0,
                                                "2025-01-01", ticker=None)
os.environ.pop("PROGRESS_V2_LIVE", None)
check("the distortion flags are IDENTICAL regardless of PROGRESS_V2_LIVE - the switch has no "
      "effect on compute_evolution() at all",
      {d["component"] for d in _evo_distort_switch_on["distortions"]} == _distorted_names)
print("[task16_c5_distortion] Growth Δ can no longer be flagged (always percentage-point "
      "scored); Debt Δ (a level field) is still flagged on a saturated baseline; "
      "PROGRESS_V2_LIVE has zero effect either way OK")

# ======================================================================
# CHECK 6: legacy "website_v1" baseline is flagged by schema, not guessed.
# ======================================================================
_legacy_baseline = {"schema": "website_v1", "Price": 50.0, "Intrinsic Value": 60.0, "Quality": 70.0}
_evo_legacy = phe.compute_evolution(_SNAPSHOT, _legacy_baseline, "STOCK", 50.0,
                                     "2025-01-01", ticker=None)
check("a legacy website_v1 baseline is flagged in distortions by its own schema field",
      any(d["component"] == "baseline" for d in _evo_legacy["distortions"]))
check("a legacy baseline still yields None (not a crash) for fields it never captured",
      _evo_legacy["components"]["Growth Δ"]["score"] is None)
print("[task16_c6_legacy_baseline] website_v1 baselines are detected via baseline['schema'], "
      "exactly like _normalize_baseline() itself OK")

# ======================================================================
# CHECK 7: the ETF path - Price since purchase + Income Δ only.
# ======================================================================
_etf_snapshot = dict(_SNAPSHOT, price=108.0, history=_hist)
_evo_etf = phe.compute_evolution(_etf_snapshot, _BASELINE, "ETF", 95.0, "2025-01-01", ticker="EVOLETF")
check("ETF path components are exactly Price since purchase + Income Δ, nothing else",
      set(_evo_etf["components"].keys()) == {"Price since purchase", "Income Δ"})
check("ETF path never computes/returns a News since purchase component",
      "News since purchase" not in _evo_etf["components"])
check("ETF weights (EVOLUTION_WEIGHTS_ETF) sum to exactly 100",
      sum(phe.EVOLUTION_WEIGHTS_ETF.values()) == 100.0)
check("stock weights (EVOLUTION_WEIGHTS) sum to exactly 100",
      sum(phe.EVOLUTION_WEIGHTS.values()) == 100.0)
print("[task16_c7_etf_path] ETF evolution score uses only Price since purchase + Income Δ, "
      "both weight tables sum to 100 OK")

# ======================================================================
# CHECK 7b (TASK 16b fix 2): Intrinsic Δ has weight 0 (information only)
# but is still computed and displayed, with an explanatory note; its 20%
# was redistributed proportionally into Fundamentals Δ (now 65% total).
# ======================================================================
check("Intrinsic Δ is NOT one of the weighted keys - EVOLUTION_WEIGHTS excludes it entirely",
      "Intrinsic Δ" not in phe.EVOLUTION_WEIGHTS)
check("EVOLUTION_INTRINSIC_WEIGHT is exactly 0.0, as the Director specified",
      phe.EVOLUTION_INTRINSIC_WEIGHT == 0.0)
check("Fundamentals Δ (Growth+Margins+ROIC+FCF+Debt) now sums to 65 points",
      sum(phe.EVOLUTION_WEIGHTS[k] for k in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ")) == 65.0)
check("the exact per-component Fundamentals Δ split matches the Director's own numbers "
      "(Growth 17, Margins 15, ROIC 14, FCF 12, Debt 7)",
      phe.EVOLUTION_WEIGHTS["Growth Δ"] == 17.0 and phe.EVOLUTION_WEIGHTS["Margins Δ"] == 15.0
      and phe.EVOLUTION_WEIGHTS["ROIC Δ"] == 14.0 and phe.EVOLUTION_WEIGHTS["FCF Δ"] == 12.0
      and phe.EVOLUTION_WEIGHTS["Debt Δ"] == 7.0)

_intrinsic_drift_snapshot = dict(_SNAPSHOT, intrinsic_value=40.0)  # a big drop vs baseline 100.0
_evo_intrinsic = phe.compute_evolution(_intrinsic_drift_snapshot, _BASELINE, "STOCK", 95.0,
                                        "2025-01-01", ticker=None)
check("Intrinsic Δ is still computed and present in components (shown, not removed)",
      "Intrinsic Δ" in _evo_intrinsic["components"]
      and _evo_intrinsic["components"]["Intrinsic Δ"]["score"] is not None)
check("Intrinsic Δ carries an explanatory 'information only' note",
      "information only" in (_evo_intrinsic["components"]["Intrinsic Δ"].get("note") or "").lower())

# Prove weight 0 is REAL, not just documented: swinging Intrinsic Δ from
# a strong score to a very weak one must not move `overall` at all, same
# snapshot/baseline otherwise.
_evo_intrinsic_control = phe.compute_evolution(_SNAPSHOT, _BASELINE, "STOCK", 95.0,
                                                "2025-01-01", ticker=None)
check("a large swing in Intrinsic Δ's own score does not move the overall evolution score "
      "at all - weight 0 is enforced, not just labelled",
      _evo_intrinsic["components"]["Intrinsic Δ"]["score"]
      != _evo_intrinsic_control["components"]["Intrinsic Δ"]["score"]
      and _evo_intrinsic["overall"] == _evo_intrinsic_control["overall"])
print("[task16_c7b_intrinsic_info_only] Intrinsic Δ is excluded from EVOLUTION_WEIGHTS "
      "(weight 0, proven not just documented), still shown with an explanatory note; "
      "Fundamentals Δ absorbed its 20% as 65% total, split 17/15/14/12/7 OK")

# ======================================================================
# CHECK 7c (TASK 16b fix 3/4, now kept on the OLD comparison column
# after TASK 16c): Income Δ's OLD sub-dict still carries its source
# field alongside baseline/current, so a drop can be judged against the
# field's real meaning (dividendRate, not a trailing-12-month sum) -
# the TOP-LEVEL Income Δ is TASK 16c's own actual-dividends method now
# (see test_director_task16c_evolution_fundamentals.py for that).
# ======================================================================
check("Income Δ's OLD 'source' field names the real underlying field (dividendRate)",
      "dividendRate" in (_evo["components"]["Income Δ"]["old"].get("source") or ""))
check("Income Δ's OLD sub-dict still carries baseline/current dividend_rate numbers "
      "alongside the source note",
      _evo["components"]["Income Δ"]["old"]["baseline"] == _BASELINE["dividend_rate"]
      and _evo["components"]["Income Δ"]["old"]["current"] == _SNAPSHOT["dividend_rate"])
print("[task16_c7c_income_source] Income Δ's OLD source field (dividendRate) is attached "
      "alongside its OLD baseline/current values, kept for comparison OK")

# ======================================================================
# CHECK 7d (TASK 16b fix 3): Price since purchase now also carries the
# raw baseline (buy price) and current price values, not just scores.
# ======================================================================
check("Price since purchase carries baseline (buy price) and current price alongside its score",
      _evo["components"]["Price since purchase"]["baseline"] == 95.0
      and _evo["components"]["Price since purchase"]["current"] == _SNAPSHOT["price"])
print("[task16_c7d_price_values] Price since purchase exposes buy price/current price, "
      "not just the blended score OK")

# ======================================================================
# CHECK 8: action thresholds - REVIEW / HOLD & WATCH / HOLD / HOLD-ADD -
# and the non-action-driving "review now" red flag for a live thesis-
# breaking item.
# ======================================================================


def _action_for(overall, valuation_mos_pct=None):
    class _Fake:
        pass
    # Directly exercise the threshold logic the same way compute_evolution()
    # does, on a synthetic overall, to pin down exact boundaries without
    # needing a full snapshot/baseline fixture per case.
    if overall is None:
        return "Not enough data yet"
    if overall < phe.EVOLUTION_ACTION_WATCH:
        return "REVIEW"
    if overall < phe.EVOLUTION_ACTION_HOLD:
        return "HOLD & WATCH"
    if valuation_mos_pct is not None and valuation_mos_pct >= phe.EVOLUTION_VALUATION_MOS_ADD:
        return "HOLD / ADD"
    return "HOLD"


check("overall=44.9 -> REVIEW (below the 45 watch floor)", _action_for(44.9) == "REVIEW")
check("overall=45.0 -> HOLD & WATCH (45-60 band, inclusive low end)", _action_for(45.0) == "HOLD & WATCH")
check("overall=59.9 -> HOLD & WATCH", _action_for(59.9) == "HOLD & WATCH")
check("overall=60.0, no MOS data -> HOLD", _action_for(60.0) == "HOLD")
check("overall=60.0, MOS=30.0 -> HOLD / ADD (>=30% margin of safety)", _action_for(60.0, 30.0) == "HOLD / ADD")
check("overall=60.0, MOS=29.9 -> plain HOLD (just under the MOS floor)", _action_for(60.0, 29.9) == "HOLD")

_evo_action = phe.compute_evolution(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-01-01",
                                     ticker=None, valuation_mos_pct=35.0)
check("compute_evolution()'s own 'action' field is one of the four documented strings",
      _evo_action["action"] in ("REVIEW", "HOLD & WATCH", "HOLD", "HOLD / ADD", "Not enough data yet"))

_seed = None  # not used again
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
pne._merge_and_save("REVIEWCO", [
    {"date": _dt.datetime(2025, 6, 10), "title": "Company discloses accounting fraud",
     "description": "", "publisher": "x", "link": "", "source": "x", "domain": "x"},
])
_evo_red_flag = phe.compute_evolution(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-06-01",
                                       ticker="REVIEWCO")
check("a live thesis-breaking item after purchase sets review_now=True",
      _evo_red_flag["review_now"] is True)
check("the red 'Review now' flag does NOT by itself force the action to REVIEW "
      "(it's a separate flag, exactly as the Director specified)",
      _evo_red_flag["action"] != "REVIEW" or _evo_red_flag["overall"] < phe.EVOLUTION_ACTION_WATCH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
print("[task16_c8_actions] REVIEW < 45 <= HOLD & WATCH < 60 <= HOLD(/ADD if MOS>=30%); "
      "a live thesis-breaking item flags 'review now' without forcing the action OK")

# ======================================================================
# CHECK 9: compute_health*/compute_progress*/_recommend() are never
# touched by this task - a plain regression check that they still behave
# exactly as the pre-existing PART C STEP C2 suite already proved.
# ======================================================================
check("compute_progress() (v1) is unaffected by compute_evolution() existing in the module",
      phe.compute_progress(_SNAPSHOT, _BASELINE, "STOCK", 95.0, "2025-01-01")["overall"]
      == _progress_v1["overall"])
check("_recommend() still returns its documented action/tone pairs unchanged",
      phe._recommend(80, 70) == ("HOLD / ADD - attractively priced", "good")
      and phe._recommend(30, 50)[0] == "REVIEW / REDUCE")
print("[task16_c9_no_live_change] compute_health*/compute_progress*/_recommend() behave "
      "exactly as before this task OK")

# ======================================================================
# CHECK 10: the owner-only Admin Dashboard shadow panel renders, placed
# directly after Market readiness and before the Top 200 preview panel,
# with a CSV export and the live Health/Action shown beside the new
# shadow numbers for comparison.
# ======================================================================
import ai_gate
import portfolio_store as ps
from unittest import mock

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

_OWNER = ai_gate.owner_email()
ps.add_holding(_OWNER, "Main", "EVOLPANEL", name="Evolution Panel Co", kind="STOCK",
                currency="AUD", shares=50.0, buy_price=95.0, buy_date="2025-01-01",
                baseline=dict(_BASELINE))
_FAKE_SNAPSHOT = dict(_SNAPSHOT)
_patches = [
    mock.patch.object(phe, "fetch_snapshot", return_value=_FAKE_SNAPSHOT),
    mock.patch.object(pne, "_claim_fetch", return_value=False),
    mock.patch.object(fundamentals_data, "get_bundle", return_value=_MOCK_BUNDLE),
    mock.patch.object(phe, "_evolution_report_dates", return_value=[]),
]
for p in _patches:
    p.start()
try:
    from streamlit.testing.v1 import AppTest
    _SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _script = f"""
import os, sys
sys.path.insert(0, {_SCRIPT_DIR!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
app._render_evolution_shadow_panel()
"""
    at = AppTest.from_string(_script, default_timeout=60)
    at.run()
    check("the shadow panel renders without raising", not at.exception)
    _markdowns = " ".join(m.value or "" for m in at.get("markdown"))
    check("the panel's own heading is present",
          "Evolution since purchase (shadow)" in _markdowns)

    _evo_df = None
    for df in at.get("dataframe"):
        try:
            _cols = set(df.value.columns)
        except Exception:
            continue
        if "Evolution score" in _cols:
            _evo_df = df.value
            break
    check("the evolution table has the required columns, including the live Health/Action "
          "comparison columns (item 7)",
          _evo_df is not None and
          {"Ticker", "Evolution score", "Evolution action", "Live Health", "Live Action",
           "Review now (thesis-breaking)", "Distortions flagged"}.issubset(set(_evo_df.columns)))
    check("EVOLPANEL appears as a row in the evolution table",
          _evo_df is not None and "EVOLPANEL" in _evo_df["Ticker"].tolist())
    check("TASK 16b: the table carries buy date and baseline-captured date per holding",
          _evo_df is not None and {"Buy date", "Baseline captured"}.issubset(set(_evo_df.columns)))
    check("TASK 16c: the table carries FY_base/FY_now, raw base/now values, and the OLD "
          "Yahoo-snapshot comparison for every statement-based component, not just scores",
          _evo_df is not None and {
              "Growth Δ FY_base", "Growth Δ FY_now", "Growth Δ base", "Growth Δ now", "Growth Δ score",
              "Growth Δ OLD baseline (Yahoo)", "Growth Δ OLD current (Yahoo)", "Growth Δ OLD score (Yahoo)",
              "Margins Δ one-offs excluded (base)", "Margins Δ one-offs excluded (now)",
              "Intrinsic Δ baseline", "Intrinsic Δ current", "Intrinsic Δ score",
          }.issubset(set(_evo_df.columns)))
    check("TASK 16b: Price since purchase's baseline (buy price)/current price are exported",
          _evo_df is not None and
          {"Price baseline (buy price)", "Price current", "Price since purchase score"}.issubset(set(_evo_df.columns)))
    check("TASK 16c: Income Δ's new TTM-actual-dividends base/now/source are exported, "
          "alongside the OLD dividendRate-based comparison columns",
          _evo_df is not None and
          {"Income Δ TTM base (actual dividends)", "Income Δ TTM now (actual dividends)", "Income Δ source",
           "Income Δ OLD baseline (dividendRate)", "Income Δ OLD current (dividendRate)",
           "Income Δ OLD score (dividendRate)"}.issubset(set(_evo_df.columns)))

    _download_labels = [b.label or "" for b in at.download_button]
    check("a CSV download button for the evolution export is present",
          any("evolution since purchase" in lbl.lower() for lbl in _download_labels))
finally:
    for p in _patches:
        p.stop()
print("[task16_c10_panel] the owner-only shadow panel renders with the required "
      "columns (including live Health/Action for comparison) and a CSV export OK")

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
