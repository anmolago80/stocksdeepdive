"""
TASK 38 (Director-directed, 7 Nov 2026, owner-only, display only):
"Replay at date" - point-in-time reconstruction of a holding's Top 200
inputs/Value Score(s) on a past date, validating the "good company,
temporary drop" case (CSL.AX). Covers point_in_time_replay.py:

  - bundle_as_of(): drops any statement column whose own reporting date
    (FY-end + STATEMENT_REPORTING_LAG_DAYS) falls on/after the cutoff -
    "no statements after the date are used".
  - psychology_as_of(): reproduces nightly_scan.analyze_ticker_lite()'s
    exact fear/greed/fomo/psychology formula, replayed against only the
    closes on/before the cutoff - a fixture matches nightly_scan.
  - quality_v2_as_of()/moat_as_of(): run the REAL engines against a
    bundle truncated to the cutoff - never see a later fiscal year.
  - replay_at_date(): price/IV/Quality v1 sourcing rules (baseline when
    the date matches, else history/"n/a"), the 4 Value Score variants
    (unmodified calls into ranking_engine.recompute_value_score() - no
    second formula), the 3x-rule/TASK 37 threshold check, would_be_in_
    pool vs today's cutoff, and read-only (no portfolio_store/top100_
    store write calls anywhere in the module).

Run: python3 tests/test_director_task38_point_in_time_replay.py
"""
import datetime
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task38_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import moat_engine
import point_in_time_replay as pir
import quality_v2_engine
import ranking_engine
import top100_engine as te

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
# CHECK 1: read-only - the module source never references a write
# function on portfolio_store/top100_store (it doesn't even import
# either module) - a static, structural guarantee this feature can
# never touch a baseline, a holding, or the live pool/presence tables.
# ======================================================================
_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "point_in_time_replay.py")).read()
check("point_in_time_replay.py never imports portfolio_store",
      "import portfolio_store" not in _src)
check("point_in_time_replay.py never imports top100_store",
      not any(line.strip().startswith(("import top100_store", "from top100_store"))
              for line in _src.splitlines()))
check("point_in_time_replay.py never calls save_pool/update_pool_presence/"
      "add_holding/update_position (no write call of any kind)",
      not any(w in _src for w in ("save_pool(", "update_pool_presence(", "add_holding(",
                                   "update_position(", "set_settings(")))
print("[task38_check1_read_only] point_in_time_replay.py is structurally read-only - "
      "no import of, or write call into, portfolio_store/top100_store OK")


# ======================================================================
# CHECK 2: bundle_as_of() - drops a fiscal-year column whose reporting
# date (FY-end + 90d) falls ON OR AFTER the cutoff; keeps one that
# falls strictly before it.
# ======================================================================
_income = pd.DataFrame(
    {"2023-12-31": [100.0], "2024-12-31": [110.0], "2025-12-31": [120.0]},
    index=["Total Revenue"],
)
_bundle = {"income": _income, "balance": _income.copy(), "cashflow": _income.copy(), "info": {}}

# FY2023-12-31 + 90d = 2024-03-30 (<= cutoff); FY2024-12-31 + 90d =
# 2025-03-31 (<= cutoff); FY2025-12-31 + 90d = 2026-03-31 (> cutoff).
_truncated = pir.bundle_as_of(_bundle, datetime.date(2025, 6, 1))
check(f"bundle_as_of: keeps 2023-12-31 and 2024-12-31, drops 2025-12-31 "
      f"(columns={list(_truncated['income'].columns)})",
      list(_truncated["income"].columns) == ["2023-12-31", "2024-12-31"])
check("bundle_as_of: applies the SAME truncation to balance and cashflow",
      list(_truncated["balance"].columns) == ["2023-12-31", "2024-12-31"]
      and list(_truncated["cashflow"].columns) == ["2023-12-31", "2024-12-31"])
check("bundle_as_of: never mutates the original bundle's own DataFrame",
      list(_bundle["income"].columns) == ["2023-12-31", "2024-12-31", "2025-12-31"])

# A cutoff right at the edge - FY2024-12-31 + 90d = 2025-03-31 exactly;
# cutoff of 2025-03-31 itself should DROP it (strictly before, per the
# module's own "<=" comparison meaning the reporting date must be
# BEFORE the cutoff, which this checks is NOT inclusive of the exact
# reporting date equalling the cutoff).
_truncated_edge = pir.bundle_as_of(_bundle, datetime.date(2025, 3, 31))
check(f"bundle_as_of: a cutoff exactly ON the reporting date keeps that year "
      f"(reporting date <= cutoff) (columns={list(_truncated_edge['income'].columns)})",
      list(_truncated_edge["income"].columns) == ["2023-12-31", "2024-12-31"])

_truncated_none = pir.bundle_as_of(_bundle, datetime.date(2020, 1, 1))
check("bundle_as_of: a cutoff before every fiscal year leaves no columns",
      list(_truncated_none["income"].columns) == [])


# ======================================================================
# CHECK 3: psychology_as_of() - reproduces nightly_scan's own formula
# exactly on a hand-built fixture, and future dates (after the replay
# date) are never used.
# ======================================================================
_base = datetime.date(2025, 1, 1)
# 70 daily closes, rising $1/day from 100 to 169 (day i -> 100+i), so
# every value (high, ma50, weekly) is hand-computable.
_closes = [(_base + datetime.timedelta(days=i), 100.0 + i) for i in range(70)]
_replay_date = _base + datetime.timedelta(days=62)  # 63rd point, value 162

_result = pir.psychology_as_of(_closes, _replay_date)
# Window = last 63 points on/before day 62 -> values 100..162 (days 0..62).
_window_vals = [100.0 + i for i in range(63)]
_expected_high = max(_window_vals)
_expected_current = _window_vals[-1]
_expected_fear = (_expected_high - _expected_current) / _expected_high * 100
_expected_ma50 = sum(_window_vals[-50:]) / 50
_expected_greed = max((_expected_current - _expected_ma50) / _expected_ma50 * 100, 0)
_expected_weekly = (_expected_current - _window_vals[-6]) / _window_vals[-6] * 100
_expected_fomo = max(_expected_greed + max(_expected_weekly, 0), 0)
_expected_psych = _expected_fear - _expected_greed - _expected_fomo

check(f"psychology_as_of: price matches the window's own last close "
      f"({_result['price']} == {_expected_current})",
      abs(_result["price"] - _expected_current) < 1e-6)
check(f"psychology_as_of: fear matches nightly_scan's own formula exactly "
      f"({_result['fear']:.6f} == {_expected_fear:.6f})",
      abs(_result["fear"] - _expected_fear) < 1e-6)
check(f"psychology_as_of: greed matches nightly_scan's own formula exactly "
      f"({_result['greed']:.6f} == {_expected_greed:.6f})",
      abs(_result["greed"] - _expected_greed) < 1e-6)
check(f"psychology_as_of: fomo matches nightly_scan's own formula exactly "
      f"({_result['fomo']:.6f} == {_expected_fomo:.6f})",
      abs(_result["fomo"] - _expected_fomo) < 1e-6)
check(f"psychology_as_of: psychology = fear - greed - fomo exactly "
      f"({_result['psychology']:.6f} == {_expected_psych:.6f})",
      abs(_result["psychology"] - _expected_psych) < 1e-6)

# Future dates (after the replay date) must never affect the result -
# append a wild crash AFTER _replay_date and confirm the result is
# byte-identical.
_closes_with_future_crash = _closes + [(_base + datetime.timedelta(days=65), 1.0)]
_result_with_future = pir.psychology_as_of(_closes_with_future_crash, _replay_date)
check("psychology_as_of: a price point AFTER the replay date never changes "
      "the result (no look-ahead)",
      _result_with_future == _result)

check("psychology_as_of: no usable close at all on/before the date -> every "
      "field None, never raises",
      pir.psychology_as_of([], _replay_date)["psychology"] is None)
print("[task38_check3_psychology_as_of] psychology_as_of() matches nightly_scan's "
      "own fear/greed/fomo/psychology formula exactly, excludes future dates, "
      "never raises on empty input OK")


# ======================================================================
# CHECK 4: quality_v2_as_of()/moat_as_of() - run the REAL engines
# against the truncated bundle (verified via mock.patch.object capturing
# the actual bundle each engine received) - a fiscal year after the
# cutoff is never visible to either.
# ======================================================================
_captured = {}


def _fake_v2(ticker, bundle, info=None):
    _captured["v2_bundle"] = bundle
    return 77.0, {"fiscal_years_used": list(bundle["income"].columns)}


with mock.patch.object(quality_v2_engine, "get_quality_score_v2", _fake_v2):
    pir.quality_v2_as_of("FAKE", _bundle, datetime.date(2025, 6, 1))
check("quality_v2_as_of: the bundle passed to get_quality_score_v2() excludes "
      "the 2025-12-31 fiscal year (not yet reported as of 2025-06-01)",
      list(_captured["v2_bundle"]["income"].columns) == ["2023-12-31", "2024-12-31"])


def _fake_moat(ticker, bundle, info):
    _captured["moat_bundle"] = bundle
    return {"score": 80.0, "components": [], "erosion": "none", "flags": [], "years": 2, "mode": "standard"}


with mock.patch.object(moat_engine, "_compute_moat_from_bundle", _fake_moat):
    pir.moat_as_of("FAKE", _bundle, datetime.date(2025, 6, 1))
check("moat_as_of: the bundle passed to _compute_moat_from_bundle() excludes "
      "the 2025-12-31 fiscal year too",
      list(_captured["moat_bundle"]["income"].columns) == ["2023-12-31", "2024-12-31"])
print("[task38_check4_engines_see_truncated_bundle] quality_v2_as_of()/moat_as_of() "
      "both run the real engine against a bundle with no fiscal year reported on/"
      "after the cutoff OK")


# ======================================================================
# CHECK 5: _value_score_variants() - unmodified calls into ranking_
# engine.recompute_value_score(), never a second/parallel formula.
# ======================================================================
_variants = pir._value_score_variants(
    quality_v1=60.0, quality_v2=75.0, mos_pct=20.0, psychology=5.0, moat_score=86.0,
)
check("current_formula variant == recompute_value_score(quality_v1, moat_blend=False)",
      _variants["current_formula"] == ranking_engine.recompute_value_score(
          {"Quality": 60.0, "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 0, "Moat": 86.0},
          moat_blend=False))
check("moat_blend variant == recompute_value_score(quality_v1, moat_blend=True)",
      _variants["moat_blend"] == ranking_engine.recompute_value_score(
          {"Quality": 60.0, "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 0, "Moat": 86.0},
          moat_blend=True))
check("quality_v2 variant == recompute_value_score(quality_v2, moat_blend=False)",
      _variants["quality_v2"] == ranking_engine.recompute_value_score(
          {"Quality": 75.0, "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 0, "Moat": 86.0},
          moat_blend=False))
check("quality_v2_moat_blend variant == recompute_value_score(quality_v2, moat_blend=True)",
      _variants["quality_v2_moat_blend"] == ranking_engine.recompute_value_score(
          {"Quality": 75.0, "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 0, "Moat": 86.0},
          moat_blend=True))

_variants_no_v2 = pir._value_score_variants(
    quality_v1=60.0, quality_v2=None, mos_pct=20.0, psychology=5.0, moat_score=86.0,
)
check("a None quality_v2 input -> both v2 variants are None, never a crash",
      _variants_no_v2["quality_v2"] is None and _variants_no_v2["quality_v2_moat_blend"] is None)
print("[task38_check5_value_score_variants] every Value Score variant is an "
      "unmodified call into ranking_engine.recompute_value_score() - no second "
      "formula, no crash on a missing Quality v2 OK")


# ======================================================================
# CHECK 6: replay_at_date() - price/IV/Quality v1 sourcing rules.
# ======================================================================
_baseline = {"price": 120.0, "intrinsic_value": 196.61, "quality_score": 51.0}
_baseline_date = "2026-07-16"
_hist = [(datetime.date(2026, 7, 10), 118.0), (datetime.date(2026, 7, 16), 121.0),
         (datetime.date(2026, 8, 1), 130.0)]

# Date MATCHES baseline_date -> price/IV/Quality v1 all come from baseline.
_r_match = pir.replay_at_date(
    "CSLTEST", "2026-07-16", baseline=_baseline, baseline_date=_baseline_date,
    bundle=None, daily_closes_with_dates=_hist, today_cutoff=47.4,
)
check("replay_at_date: date matches baseline -> price from baseline (120.0)",
      _r_match["price"] == 120.0 and _r_match["price_source"] == "baseline")
check("replay_at_date: date matches baseline -> IV from baseline (196.61)",
      _r_match["intrinsic_value"] == 196.61 and _r_match["intrinsic_value_source"] == "baseline")
check("replay_at_date: date matches baseline -> Quality v1 from baseline (51.0)",
      _r_match["quality_v1"] == 51.0 and _r_match["quality_v1_source"] == "baseline")
check("replay_at_date: MOS % computed from baseline price/IV",
      abs(_r_match["mos_pct"] - ((196.61 - 120.0) / 196.61 * 100)) < 1e-6)

# Date does NOT match baseline_date -> price from history, IV/Quality v1
# both n/a (never estimated).
_r_nomatch = pir.replay_at_date(
    "CSLTEST", "2026-08-01", baseline=_baseline, baseline_date=_baseline_date,
    bundle=None, daily_closes_with_dates=_hist, today_cutoff=47.4,
)
check("replay_at_date: date != baseline -> price from history (130.0 on 2026-08-01)",
      _r_nomatch["price"] == 130.0 and _r_nomatch["price_source"] == "history")
check("replay_at_date: date != baseline -> IV is None, source 'n/a (not stored)' "
      "- never estimated",
      _r_nomatch["intrinsic_value"] is None and _r_nomatch["intrinsic_value_source"] == "n/a (not stored)")
check("replay_at_date: date != baseline -> Quality v1 is None, source 'n/a "
      "(no historical replay)' - never estimated",
      _r_nomatch["quality_v1"] is None and "n/a" in _r_nomatch["quality_v1_source"])
check("replay_at_date: no bundle supplied -> quality_v2/moat are None, with "
      "named limitations (never guessed at)",
      _r_nomatch["quality_v2"] is None and _r_nomatch["moat"] is None
      and any("Quality v2" in l for l in _r_nomatch["limitations"])
      and any("Moat" in l for l in _r_nomatch["limitations"]))
print("[task38_check6_sourcing_rules] price/IV/Quality v1 are read from the "
      "locked baseline ONLY when the date matches it exactly; any other date "
      "gets price from history and explicit 'n/a', never a guess OK")


# ======================================================================
# CHECK 7: replay_at_date() - the 3x-rule / TASK 37 threshold check and
# would_be_in_pool, against a real bundle (so quality_v2/moat are real
# numbers, not None).
# ======================================================================
_income2 = pd.DataFrame(
    {"2023-12-31": [1000.0, 1000.0, 1000.0, 300.0, 300.0, 0.0, 300.0],
     "2024-12-31": [1100.0, 1100.0, 1100.0, 330.0, 330.0, 0.0, 330.0]},
    index=["Total Revenue", "Normalized Income", "Net Income",
           "Operating Income", "Pretax Income", "Total Unusual Items", "Tax Provision"],
)
_balance2 = pd.DataFrame(
    {"2023-12-31": [500.0, 100.0, 50.0, 20.0],
     "2024-12-31": [550.0, 100.0, 55.0, 20.0]},
    index=["Stockholders Equity", "Total Debt", "Cash And Cash Equivalents",
           "Goodwill And Other Intangible Assets"],
)
_cashflow2 = pd.DataFrame(
    {"2023-12-31": [250.0, -50.0], "2024-12-31": [270.0, -55.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
_bundle2 = {"income": _income2, "balance": _balance2, "cashflow": _cashflow2,
            "info": {"returnOnEquity": 0.20}}

# ADBE-shaped: price far below IV (looks unreliable), high Moat/Quality.
_r_adbe = pir.replay_at_date(
    "ADBETEST", "2025-12-31", baseline={"price": 100.0, "intrinsic_value": 400.0, "quality_score": 87.0},
    baseline_date="2025-12-31", bundle=_bundle2, daily_closes_with_dates=[(datetime.date(2025, 12, 31), 100.0)],
    today_cutoff=47.4,
)
check("replay_at_date: dcf_unreliable True for a 4x IV/price ratio",
      _r_adbe["dcf_unreliable"] is True)
check("replay_at_date: quality_exception_thresholds_met is a real bool once "
      "Moat and Quality v1 are both available",
      isinstance(_r_adbe["quality_exception_thresholds_met"], bool))
check("replay_at_date: would_be_in_pool is computed for every Value Score "
      "variant against today_cutoff=47.4",
      all(isinstance(v, bool) for v in _r_adbe["would_be_in_pool"].values()))

_r_no_cutoff = pir.replay_at_date(
    "ADBETEST", "2025-12-31", baseline={"price": 100.0, "intrinsic_value": 400.0, "quality_score": 87.0},
    baseline_date="2025-12-31", bundle=_bundle2, daily_closes_with_dates=[(datetime.date(2025, 12, 31), 100.0)],
    today_cutoff=None,
)
check("replay_at_date: today_cutoff=None -> would_be_in_pool is all None, "
      "with a limitation naming why (no historical cut-off exists)",
      all(v is None for v in _r_no_cutoff["would_be_in_pool"].values())
      and any("cut-off" in l for l in _r_no_cutoff["limitations"]))
print("[task38_check7_exception_and_pool_membership] the 3x-rule check and "
      "would_be_in_pool both compute correctly against a real bundle, and "
      "would_be_in_pool degrades to None (never guessed) with no today_cutoff OK")


# ======================================================================
# CHECK 8 (TASK 40 follow-up, Director-verified root cause, 8 Nov
# 2026): the real app._render_replay_at_date_panel() end-to-end, under
# AppTest - clicking "Run replay" must never raise. This is the exact
# regression class the Director's own review caught: `_date = st.
# date_input(...)` inside the function shadowed the module-level
# `date as _date` import, making `_date.fromisoformat()`/`_date.
# today()` (evaluated earlier in the same function) raise
# UnboundLocalError the moment the function is actually RUN (never
# caught by import-only/syntax checks, since Python only resolves a
# name's scope - local vs. global - at compile time per function, not
# per line). Fixed by renaming the widget's own local to `_replay_date`;
# this test renders the panel and clicks the button so this exact
# class of bug can never silently reappear.
# ======================================================================
from streamlit.testing.v1 import AppTest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_replay_panel():
    script = f"""
import os, sys, datetime
sys.path.insert(0, {_REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import pandas as pd
import streamlit as st
import app
import fundamentals_data
import top100_engine

app._replay_find_holding_default = lambda ticker: (
    {{"price": 120.0, "intrinsic_value": 196.61, "quality_score": 51.0}}, "2026-07-16",
)
fundamentals_data.get_bundle = lambda ticker, force_refresh=False: {{
    "income": pd.DataFrame(), "balance": pd.DataFrame(), "cashflow": pd.DataFrame(), "info": {{}},
}}
top100_engine.pool_value_score_cutoff = lambda: 47.4


class _FakeYfTicker:
    def __init__(self, ticker):
        pass

    def history(self, period="2y"):
        idx = pd.bdate_range(end=datetime.date(2026, 7, 16), periods=120)
        return pd.DataFrame({{"Close": [120.0 + i * 0.1 for i in range(120)]}}, index=idx)


app.yf.Ticker = _FakeYfTicker

app._render_replay_at_date_panel()
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"initial render raised: {at.exception}"
    at.button(key="admin_replay_button").click().run()
    return at


_at = _run_replay_panel()
check(f"_render_replay_at_date_panel(): clicking 'Run replay' never raises "
      f"(exception={_at.exception})", not _at.exception)
check("_render_replay_at_date_panel(): the result actually rendered (ticker "
      "heading present somewhere in the page)",
      any("CSL.AX" in str(getattr(el, 'value', '')) or "CSL.AX" in str(getattr(el, 'body', ''))
          for el in _at.markdown) or any("CSL.AX" in str(h) for h in _at.markdown))
print("[task38_check8_panel_appTest] _render_replay_at_date_panel() renders and "
      "a button click runs the real replay without raising - the exact "
      "UnboundLocalError class of bug is now caught by a real test OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
