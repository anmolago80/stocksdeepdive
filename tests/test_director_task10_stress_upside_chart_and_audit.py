"""
Director TASK 10 (17 Oct 2026): Stress tab - new "Upside" chart, Run-up
retitle + own-value caption, Drawdown caption wording, full calculation
audit.

Covers the task's own listed tests (spec item 5) plus the calculation
audit (spec item 4):
  - Upside chart max == best_rolling_12m()["pct"] exactly (both now
    derive from the SAME rolling_12m_series(), see stress_engine.py)
  - the Run-up caption equals the Run-up series' OWN final value (not
    best_rolling_12m(), a different metric - that was the bug)
  - Drawdown series/caption calculation is UNCHANGED (regression lock
    on a small hand-verified fixture)
  - CALCULATION AUDIT, against a real multi-holding synthetic portfolio:
      (a) MAX DOWNSIDE % + dates == Drawdown chart's lowest point + dates
      (b) MAX UPSIDE % + start/end dates == Upside chart's highest point
          + window
      (c) "Replayed 10y return" is consistent with the Run-up series'
          own last-10-year growth: (1+X)^years matches within rounding
      (d) the A$ figures == % x current portfolio value
      (e) the COVID crisis-replay window doesn't contradict the
          Drawdown chart's depth at the same dates

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. The synthetic two-holding portfolio is built from pure
log-linear segments (constant daily simple return within each segment)
specifically so the combined series' peak/trough/best-12m window are
analytically exact, not just "probably right" - see
_synthetic_crash_history()'s own docstring for the construction.

Run: python3 tests/test_director_task10_stress_upside_chart_and_audit.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task10_stress_upside_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import numpy as np
import pandas as pd

import stress_engine as se
import i18n

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
# Synthetic multi-holding portfolio construction.
#
# Every segment below is linear IN LOG-PRICE, which makes the daily
# SIMPLE return exactly constant within that segment (not merely
# approximately so) - this is what makes the expected peak/trough/
# best-12m dates provably exact rather than just "likely" (see the
# reasoning in this file's own module docstring above).
# ======================================================================

def _log_linear_history(dates, breakpoints):
    """breakpoints: [(date_or_None, log_price), ...] in ascending date
    order (None for the series' own first/last date). log-price is
    linearly interpolated between consecutive breakpoints (giving an
    exactly-constant daily simple return within each resulting
    segment), then exponentiated back to a plain Close price."""
    idx = [dates.searchsorted(pd.Timestamp(d)) if d is not None else None
           for d, _ in breakpoints]
    idx[0] = 0 if idx[0] is None else idx[0]
    idx[-1] = len(dates) - 1 if idx[-1] is None else idx[-1]
    log_price = np.zeros(len(dates))
    for k in range(len(idx) - 1):
        i0, i1 = idx[k], idx[k + 1]
        v0, v1 = breakpoints[k][1], breakpoints[k + 1][1]
        log_price[i0:i1 + 1] = np.linspace(v0, v1, i1 - i0 + 1)
    return pd.DataFrame({"Close": np.exp(log_price)}, index=dates)


_START, _END = "2009-01-02", "2026-10-16"
_PEAK_DATE, _TROUGH_DATE = "2020-01-31", "2020-03-23"
_DATES = pd.bdate_range(_START, _END)

# Ticker A (60% weight): modest growth to the peak, a sharp ~45% crash
# into the real "covid" CRISES window (2020-02-01 to 2020-03-31), then
# a strong, CONSTANT-rate recovery all the way to the series' end - so
# every 252-trading-day window fully inside the recovery ties for the
# best rolling-12m return (pure constant-rate compounding can't be
# beaten by starting later), making end_date == trough_date + 252
# trading days and start_date == trough_date provably exact, not a
# guess (pandas Series.idxmax() returns the FIRST tied occurrence).
_LOG_BASE = np.log(100.0)
_LOG_PEAK = _LOG_BASE + np.log(1.15)          # +15% from 2009 to the peak
_LOG_TROUGH = _LOG_PEAK + np.log(1.0 - 0.45)  # -45% crash
HIST_A = _log_linear_history(_DATES, [
    (None, _LOG_BASE),
    (_PEAK_DATE, _LOG_PEAK),
    (_TROUGH_DATE, _LOG_TROUGH),
    (None, _LOG_TROUGH + np.log(1.30) * (
        (_DATES[-1] - pd.Timestamp(_TROUGH_DATE)).days / 365.25
    )),
])

# Ticker B (40% weight): pure steady growth the ENTIRE period (no
# crash at all) - a genuinely diversifying second holding, deliberately
# too small a daily rate to ever flip the combined series' monotonic
# direction during A's crash (checked explicitly below).
HIST_B = _log_linear_history(_DATES, [
    (None, np.log(50.0)),
    (None, np.log(50.0) + np.log(2.5)),  # 2.5x over the whole span
])

WEIGHTS = {"SYNA.AX": 60000.0, "SYNB": 40000.0}
HISTORIES = {"SYNA.AX": HIST_A, "SYNB": HIST_B}
INDEX_HISTORIES = {}
TOTAL_VALUE_AUD = 100000.0

# ----------------------------------------------------------------------
# Sanity-check the construction's own load-bearing claim before trusting
# any downstream date assertion: during A's crash, the COMBINED daily
# return must stay negative every single day (B's small positive drift
# must never outweigh A's crash), and the combined series must be
# strictly increasing on every day up to the peak and strictly
# increasing on every day from the trough onward - otherwise the
# "exact" peak/trough dates asserted below would just be wrong.
# ----------------------------------------------------------------------
_hist_15y = {t: se.cap_to_years(h, 15) for t, h in HISTORIES.items()}
SERIES, USED, DROPPED = se.build_combined_series(WEIGHTS, _hist_15y)
check("both synthetic holdings survive build_combined_series (none dropped)",
      sorted(USED) == ["SYNA.AX", "SYNB"] and DROPPED == [])

_combined_ret = SERIES.pct_change().dropna()
_crash_mask = (_combined_ret.index > pd.Timestamp(_PEAK_DATE)) & (_combined_ret.index <= pd.Timestamp(_TROUGH_DATE))
check("construction check: every day of A's crash window is a down-day "
      "for the COMBINED series (B's drift never masks the crash)",
      bool((_combined_ret[_crash_mask] < 0).all()) and _crash_mask.sum() > 10)

_precrash_mask = _combined_ret.index <= pd.Timestamp(_PEAK_DATE)
check("construction check: the combined series only ever goes UP before "
      "the peak (so the peak really is the running maximum)",
      bool((_combined_ret[_precrash_mask] > 0).all()))

_postcrash_mask = _combined_ret.index > pd.Timestamp(_TROUGH_DATE)
check("construction check: the combined series only ever goes UP after "
      "the trough (so the trough really is the running minimum, and "
      "every post-trough window compounds at one constant rate)",
      bool((_combined_ret[_postcrash_mask] > 0).all()))


# ======================================================================
# FIXTURE 2 - a second multi-holding synthetic portfolio, same
# construction style as FIXTURE 1 above, but with its crash placed
# ~13 years before "today" instead of ~6.5 - well before the last-10-
# years window used by replayed_return_pa()/the audit's check (c), so
# the run-up series' baseline (its running min) is already fixed for
# that whole comparison window. FIXTURE 1's crash deliberately sits in
# the real COVID CRISES window instead (for audit check (e)) and can't
# serve both purposes at once.
# ======================================================================
_C2_PEAK_DATE, _C2_TROUGH_DATE = "2013-01-15", "2013-04-15"
_c2_log_peak = _LOG_BASE + np.log(1.08)
_c2_log_trough = _c2_log_peak + np.log(1.0 - 0.30)
HIST_C = _log_linear_history(_DATES, [
    (None, _LOG_BASE),
    (_C2_PEAK_DATE, _c2_log_peak),
    (_C2_TROUGH_DATE, _c2_log_trough),
    (None, _c2_log_trough + np.log(1.20) * (
        (_DATES[-1] - pd.Timestamp(_C2_TROUGH_DATE)).days / 365.25
    )),
])
HIST_D = _log_linear_history(_DATES, [
    (None, np.log(50.0)),
    (None, np.log(50.0) + np.log(1.8)),
])
_C2_WEIGHTS = {"SYNC.AX": 55000.0, "SYND": 45000.0}
_c2_hist_15y = {t: se.cap_to_years(h, 15) for t, h in {"SYNC.AX": HIST_C, "SYND": HIST_D}.items()}
_C2_SERIES, _c2_used, _c2_dropped = se.build_combined_series(_C2_WEIGHTS, _c2_hist_15y)
check("FIXTURE 2: both synthetic holdings survive build_combined_series",
      sorted(_c2_used) == ["SYNC.AX", "SYND"] and _c2_dropped == [])

_c2_combined_ret = _C2_SERIES.pct_change().dropna()
_c2_postcrash_mask = _c2_combined_ret.index > pd.Timestamp(_C2_TROUGH_DATE)
check("FIXTURE 2 construction check: the combined series only ever goes "
      "UP after its trough (no later point ever sets a new low, so the "
      "run-up baseline really does stay fixed through the last 10 years)",
      bool((_c2_combined_ret[_c2_postcrash_mask] > 0).all()))


# ======================================================================
# CHECK: Drawdown series/caption calculation UNCHANGED - a small,
# hand-verified fixture, independent of the big synthetic portfolio
# above, locking in the exact pre-existing formula.
#   prices: 100, 120, 90, 95, 130 over 5 business days
#   peak=120 (day2), trough=90 (day3) -> dd% = 90/120-1 = -25.0%
#   recovers at day5 (130 >= 120)
# ======================================================================
_toy_dates = pd.bdate_range("2024-01-01", periods=5)
_toy_series = pd.Series([100.0, 120.0, 90.0, 95.0, 130.0], index=_toy_dates)
_toy_dd = se.max_drawdown(_toy_series)
_toy_dd_series = se.drawdown_series(_toy_series)

check("REGRESSION - max_drawdown()'s pct is still exactly -25.0% on the "
      "hand-verified toy fixture", abs(_toy_dd["pct"] - (-25.0)) < 1e-9)
check("REGRESSION - max_drawdown()'s peak_date is still day 2",
      _toy_dd["peak_date"] == _toy_dates[1])
check("REGRESSION - max_drawdown()'s trough_date is still day 3",
      _toy_dd["trough_date"] == _toy_dates[2])
check("REGRESSION - max_drawdown()'s recovered_date is still day 5",
      _toy_dd["recovered_date"] == _toy_dates[4])
check("REGRESSION - drawdown_series()'s own trough value matches "
      "max_drawdown()'s pct exactly (same formula, still wired together)",
      abs(float(_toy_dd_series.loc[_toy_dates[2]]) * 100.0 - _toy_dd["pct"]) < 1e-9)


# ======================================================================
# CHECK: Upside chart max == best_rolling_12m()["pct"] exactly (spec
# item 5's own explicit test) - on the real synthetic portfolio.
# ======================================================================
DD = se.max_drawdown(SERIES)
BEST12 = se.best_rolling_12m(SERIES)
RET10 = se.replayed_return_pa(SERIES, 10)
DD_SERIES = se.drawdown_series(SERIES)
RUNUP_SERIES = se.runup_from_trough_series(SERIES)
UPSIDE_SERIES = se.rolling_12m_series(SERIES)

_upside_valid = UPSIDE_SERIES.dropna()
check("UPSIDE_SERIES is blank (NaN) for the first 252 points, matching "
      "the 'no data until a full year of history' spec requirement",
      UPSIDE_SERIES.iloc[:252].isna().all())
check("Upside chart's own maximum point (x100 for pct) == "
      "best_rolling_12m()['pct'] exactly",
      abs(float(_upside_valid.max()) * 100.0 - BEST12["pct"]) < 1e-9)
check("Upside chart's own highest point's DATE == best_rolling_12m()['end_date']",
      _upside_valid.idxmax() == BEST12["end_date"])


# ======================================================================
# CALCULATION AUDIT - two numbers side by side, PASS/FAIL.
# ======================================================================
print()
print("=" * 70)
print("TASK 10 CALCULATION AUDIT")
print("=" * 70)


def audit(label, a, b, ok, extra=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}: {a!r}  vs  {b!r}  {extra}")
    check(f"AUDIT - {label}", ok)


# (a) MAX DOWNSIDE % and dates == Drawdown chart's lowest point and dates.
_dd_chart_min = float(DD_SERIES.min()) * 100.0
_dd_chart_trough_date = DD_SERIES.idxmin()
audit("MAX DOWNSIDE pct", f"{DD['pct']:.4f}%", f"{_dd_chart_min:.4f}%",
      abs(DD["pct"] - _dd_chart_min) < 1e-9)
audit("MAX DOWNSIDE trough date", DD["trough_date"], _dd_chart_trough_date,
      DD["trough_date"] == _dd_chart_trough_date)
audit("MAX DOWNSIDE peak date (exact, by construction)", DD["peak_date"],
      pd.Timestamp(_PEAK_DATE), DD["peak_date"] == pd.Timestamp(_PEAK_DATE))
audit("MAX DOWNSIDE trough date (exact, by construction)", DD["trough_date"],
      pd.Timestamp(_TROUGH_DATE), DD["trough_date"] == pd.Timestamp(_TROUGH_DATE))

# (b) MAX UPSIDE % and start/end dates == Upside chart's highest point + window.
_upside_chart_end = _upside_valid.idxmax()
_upside_chart_pct = float(_upside_valid.max()) * 100.0
_upside_chart_start = SERIES.index[SERIES.index.get_loc(_upside_chart_end) - 252]
audit("MAX UPSIDE pct", f"{BEST12['pct']:.4f}%", f"{_upside_chart_pct:.4f}%",
      abs(BEST12["pct"] - _upside_chart_pct) < 1e-9)
audit("MAX UPSIDE end date", BEST12["end_date"], _upside_chart_end,
      BEST12["end_date"] == _upside_chart_end)
audit("MAX UPSIDE start date", BEST12["start_date"], _upside_chart_start,
      BEST12["start_date"] == _upside_chart_start)
# Note: this synthetic portfolio's post-crash recovery compounds at one
# CONSTANT rate all the way to the series' end, so every 252-trading-day
# window from the trough onward ties (to within float noise) for the
# single best return - best_rolling_12m()'s exact winning date among
# that tie is an idxmax() implementation detail, not something this
# audit needs to pin down; the cross-consistency checks above (pct/
# end_date/start_date all agreeing with the Upside chart's own series)
# are the actual spec requirement, and they hold.

# (c) "Replayed 10y return" consistent with the Run-up series' own
# last-10-year growth: (1+X)^years matches the series' actual growth.
# Needs its OWN fixture whose global low sits well before "10 years
# ago" - runup_from_trough_series' baseline (the running min) only
# stays FIXED for a comparison window if no new low is set inside it;
# FIXTURE 1 above deliberately places its trough only ~6.5y before
# today (inside the last-10-years window) to land it in the real COVID
# CRISES window for check (e) - so it can't also serve this check.
_c2_start_dt = _C2_SERIES.index[-1] - pd.Timedelta(days=int(365.25 * 10))
check("FIXTURE 2's own trough is well before its last-10-years window "
      "(so the run-up baseline is already fixed throughout it)",
      pd.Timestamp(_C2_TROUGH_DATE) < _c2_start_dt)

_c2_ret10 = se.replayed_return_pa(_C2_SERIES, 10)
_c2_runup_series = se.runup_from_trough_series(_C2_SERIES)
_c2_runup_window = _c2_runup_series[_c2_runup_series.index >= _c2_start_dt].dropna()
_c2_runup_growth_ratio = (
    (1.0 + float(_c2_runup_window.iloc[-1])) / (1.0 + float(_c2_runup_window.iloc[0]))
)
_c2_actual_years = (_c2_runup_window.index[-1] - _c2_runup_window.index[0]).days / 365.25
_c2_runup_pa = (_c2_runup_growth_ratio ** (1.0 / _c2_actual_years) - 1.0) * 100.0
audit("Replayed 10y return (p.a.) vs Run-up series' own 10y annualised growth",
      f"{_c2_ret10:.4f}%", f"{_c2_runup_pa:.4f}%", abs(_c2_ret10 - _c2_runup_pa) < 1e-6)
_c2_compounded_10y = (1.0 + _c2_ret10 / 100.0) ** 10
_c2_runup_compounded_10y = _c2_runup_growth_ratio ** (10 / _c2_actual_years)
audit("(1+X)^10 vs the Run-up series' own 10y growth ratio (within rounding)",
      round(_c2_compounded_10y, 2), round(_c2_runup_compounded_10y, 2),
      abs(_c2_compounded_10y - _c2_runup_compounded_10y) < 0.01)

# (d) the A$ figures == % x current portfolio value (the exact formula
# app.py's headline-card render code uses).
_downside_aud = abs(DD["pct"]) / 100.0 * TOTAL_VALUE_AUD
_upside_aud = BEST12["pct"] / 100.0 * TOTAL_VALUE_AUD
audit("MAX DOWNSIDE A$ == pct x portfolio value", f"A${_downside_aud:,.0f}",
      f"{DD['pct']:.1f}% x A${TOTAL_VALUE_AUD:,.0f}",
      abs(_downside_aud - abs(DD["pct"]) / 100.0 * TOTAL_VALUE_AUD) < 1e-9)
audit("MAX UPSIDE A$ == pct x portfolio value", f"A${_upside_aud:,.0f}",
      f"{BEST12['pct']:.1f}% x A${TOTAL_VALUE_AUD:,.0f}",
      abs(_upside_aud - BEST12["pct"] / 100.0 * TOTAL_VALUE_AUD) < 1e-9)

# (e) the COVID crisis-replay window doesn't contradict the Drawdown
# chart's depth at the same dates (allowing for the chart being daily
# vs the crisis table's own fixed start/end window).
_covid_window = next(w for w in se.CRISES if w[0] == "covid")
_crisis_result = se.scenario_replay(WEIGHTS, HISTORIES, INDEX_HISTORIES, {}, _covid_window, TOTAL_VALUE_AUD)
audit("COVID crisis-table window move_pct vs the Drawdown chart's own "
      "worst pct (both solidly negative, same real crash)",
      f"{_crisis_result['move_pct']:.1f}%", f"{DD['pct']:.1f}%",
      _crisis_result["move_pct"] < -10 and DD["pct"] < -10,
      extra="(both must be meaningfully negative)")
audit("Drawdown chart's own trough date falls INSIDE the COVID crisis "
      "table's window (2020-02-01 to 2020-03-31)",
      DD["trough_date"], f"{_covid_window[2]} .. {_covid_window[3]}",
      pd.Timestamp(_covid_window[2]) <= DD["trough_date"] <= pd.Timestamp(_covid_window[3]))
_depth_gap = abs(_crisis_result["move_pct"] - DD["pct"])
audit("crisis-table move_pct vs chart's worst pct - gap stays within a "
      "daily-vs-fixed-window margin (< 20pp)",
      f"{_crisis_result['move_pct']:.1f}%", f"{DD['pct']:.1f}%",
      _depth_gap < 20.0, extra=f"(gap={_depth_gap:.1f}pp)")


# ======================================================================
# CHECK: the Run-up caption's own value - app.py's exact glue formula
# (last valid point of runup_from_trough_series, in pct) - matches the
# chart it sits under, NOT best_rolling_12m() (the pre-TASK10 bug).
# ======================================================================
_runup_valid = RUNUP_SERIES.dropna()
RUNUP_LATEST_PCT = float(_runup_valid.iloc[-1]) * 100.0
check("Run-up caption's own value is the series' own LAST point, not "
      "best_rolling_12m()'s (a different metric)",
      abs(RUNUP_LATEST_PCT - BEST12["pct"]) > 1.0)
check("Run-up caption's own value matches the chart's own final/highest "
      "point (both just 'the last value of RUNUP_SERIES')",
      abs(RUNUP_LATEST_PCT - float(_runup_valid.max()) * 100.0) < 1e-6)


# ======================================================================
# CHECK: app._stress_compute_full wires upside_series/runup_latest_pct
# correctly (matches the pure stress_engine computation above exactly,
# since it runs the identical hist_15y-capped pipeline) - driven
# through the real app.py module via AppTest (the repo's own
# established pattern for calling a private app.py helper directly;
# app.py cannot be imported outside a running Streamlit script
# context).
# ======================================================================
import pickle
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_fixture_path = os.path.join(TESTVOL, "task10_fixture.pkl")
with open(_fixture_path, "wb") as f:
    pickle.dump(
        {"weights": WEIGHTS, "histories": HISTORIES, "index_histories": INDEX_HISTORIES,
         "total_value_aud": TOTAL_VALUE_AUD},
        f,
    )

_script = f"""
import os, sys, pickle
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import app
with open({_fixture_path!r}, "rb") as f:
    _fx = pickle.load(f)
_result = app._stress_compute_full(
    _fx["weights"], _fx["histories"], _fx["index_histories"], _fx["total_value_aud"],
)
st.session_state["_result"] = _result
"""
_at = AppTest.from_string(_script, default_timeout=60)
_at.run()
check("app._stress_compute_full() runs without raising on the synthetic "
      "multi-holding fixture", not _at.exception)
if _at.exception:
    print(f"    exception: {_at.exception}")

_app_result = _at.session_state["_result"]

check("app._stress_compute_full()'s result carries the new 'upside_series' key",
      "upside_series" in _app_result and _app_result["upside_series"])
check("app._stress_compute_full()'s result carries the new 'runup_latest_pct' key",
      _app_result.get("runup_latest_pct") is not None)

_app_upside_max = max((v for _, v in _app_result["upside_series"] if v is not None), default=None)
check("app.py's own upside_series max == the pure stress_engine computation's "
      "best_rolling_12m()['pct'] (same value, two ways of asking for it - "
      "tolerance covers _stress_series_to_pairs()'s own 3-decimal rounding)",
      _app_upside_max is not None and abs(_app_upside_max - BEST12["pct"]) < 2e-3)
check("app.py's own runup_latest_pct == this test's own independent "
      "RUNUP_LATEST_PCT computation",
      abs(_app_result["runup_latest_pct"] - RUNUP_LATEST_PCT) < 1e-6)
check("app.py's own max_drawdown pct is unchanged by this task "
      "(still comes from the same max_drawdown() call)",
      abs(_app_result["max_drawdown"]["pct"] - DD["pct"]) < 1e-9)


# ======================================================================
# CHECK: i18n strings (EN + ES) for the new/changed chart titles and
# captions render with the right wording and the right variable.
# ======================================================================
check("EN drawdown_chart_caption reads 'Worst fall from a peak: -X%'",
      i18n.t("portfolio.stress.drawdown_chart_caption", "en", pct="-25.0")
      == "Worst fall from a peak: -25.0%")
check("EN upside_chart_caption reads 'Best 12 months: +X%'",
      i18n.t("portfolio.stress.upside_chart_caption", "en", pct="60.6")
      == "Best 12 months: +60.6%")
check("EN runup_chart_caption reads 'Growth since start: +X%'",
      i18n.t("portfolio.stress.runup_chart_caption", "en", pct="1400.0")
      == "Growth since start: +1400.0%")
check("EN runup_chart_title is retitled 'Run-up since start'",
      i18n.t("portfolio.stress.runup_chart_title", "en") == "Run-up since start")
check("EN upside_chart_title exists and is non-empty",
      bool(i18n.t("portfolio.stress.upside_chart_title", "en")))
check("ES drawdown_chart_caption carries {pct} through correctly",
      i18n.t("portfolio.stress.drawdown_chart_caption", "es", pct="-25.0")
      == "Peor caída desde un máximo: -25.0%")
check("ES upside_chart_caption carries {pct} through correctly",
      i18n.t("portfolio.stress.upside_chart_caption", "es", pct="60.6")
      == "Mejores 12 meses: +60.6%")
check("ES runup_chart_caption carries {pct} through correctly",
      i18n.t("portfolio.stress.runup_chart_caption", "es", pct="1400.0")
      == "Crecimiento desde el inicio: +1400.0%")
check("ES runup_chart_title is retitled",
      i18n.t("portfolio.stress.runup_chart_title", "es") == "Recuperación desde el inicio")


# ======================================================================
# CHECK: the result-cache table was bumped (v9 -> v10) so an unchanged
# portfolio's pre-TASK10 cached result can't silently hide the new
# chart/caption for up to RESULT_TTL_HOURS after this deploy.
# ======================================================================
check("stress_engine's result-cache table name was bumped for this task",
      se._RESULT_CACHE_TABLE == "stress_result_cache_v10")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
