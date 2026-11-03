"""
TASK 29 (Director-directed, 3 Nov 2026, real incident - BCG.L): London
(.L) price history mixes pence and pounds, producing a false "99%
crash" 10y chart, garbage PE-by-year (3,431x/3,516x/3,616x/1,779x vs a
TTM of 25.56x), a nonsense PE Trailing intrinsic value (GBP 168.07 from
"Average P/E 1,807x"), and Retained Earnings "value created per $
retained" of -4,000.

ROOT CAUSE (fundamentals_data.py, confirmed in code): the old logic was
all-or-nothing - "if this ticker's info currency is GBp, divide EVERY
point of its price history by 100" - correct only when the WHOLE series
is consistently pence-quoted. BCG.L's own series switches units mid-
history (older months in pence, latest months in pounds), so a blanket
divide is wrong either way: a flat divide-all leaves the pence months
right but turns the pounds months into pennies; no divide leaves the
pence months 100x too high.

THE FIX (this task): per-point/per-row repair against a reliable
reference price - fundamentals_data.repair_mixed_unit_price_series()/
repair_mixed_unit_price_dataframe() - only a point whose ratio to the
reference falls in _PENCE_HISTORY_REPAIR_RATIO_RANGE (30x-300x) gets
divided by 100; a point that already agrees with the reference is left
alone. Wired into all three real fetch sites this task's own grep
found:
  1. fundamentals_data.get_bundle()'s own prices_10y (tk.history(
     period="10y", interval="1mo")), against info["currentPrice"].
  2. fundamentals_data.normalize_pence_quote()'s own history_df param
     (shared by nightly_scan.analyze_ticker_lite() and app.py's Deep
     Dive headline path), against info["currentPrice"].
  3. nightly_scan._reprice_row()'s own close_series (sliced from the
     reprice pass's chunked yf.download()), against row["Price"] (the
     prior full scan's own already-normalised price - there is no
     independent "current price" available at this site).
Plus a hardcoded "$" fix in compounder_ui._cp_price_chart's "10y
Average" chart annotation (now uses currency_format.format_money(),
the same shared per-ticker formatter Lists & display Commit 5 already
uses everywhere else in this file, instead of re-deriving the symbol),
and a BUNDLE_VERSION bump (6->7) so every cached .L bundle/Compounder
section rebuilds with the repaired series after deploy.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. "Verified" here means "verified against a synthetic,
hand-built fixture shaped like the real BCG.L incident", never
"verified live" - the Railway log's own "[units] <TICKER> history
repaired: N of M points..." lines are what confirm the live effect
after deploy (see this task's own REPORT BACK instruction).

Run: python3 tests/test_director_task29_london_pence_history.py
"""
import contextlib
import datetime
import io
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task29_pence_history_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import auto_compounder_engine as ace
import compounder_ui
import fundamentals_data as fd
import nightly_scan

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


def _capture_log():
    lines = []
    return lines, lines.append


# ======================================================================
# Shared fixture shape: BCG.L's real incident - 30 months in pence
# (~230, the pre-switch era) followed by 6 months in pounds (~2.30,
# matching the owner-confirmed current price GBP 2.30), with small
# genuine month-to-month wiggle on both sides so this isn't just a
# literal constant.
# ======================================================================
_BCG_REFERENCE = 2.30
_BCG_PENCE_MONTHS = [230.0, 228.5, 231.0, 229.0, 232.0, 230.5, 227.0, 233.0,
                     229.5, 231.5, 230.0, 228.0, 232.5, 229.0, 230.5, 231.0,
                     228.5, 230.0, 233.0, 229.0, 231.5, 230.0, 228.0, 232.0,
                     229.5, 230.5, 231.0, 229.0, 230.0, 228.5]
_BCG_POUND_MONTHS = [2.29, 2.31, 2.28, 2.32, 2.30, 2.30]
_BCG_MIXED_SERIES = _BCG_PENCE_MONTHS + _BCG_POUND_MONTHS  # 36 points total

# ======================================================================
# T1 (item 1/5): repair_mixed_unit_price_series() on the BCG.L-shaped
# mixed series - every pence-era point rescaled to pounds, every
# pounds-era point left alone, log line emitted with the exact format.
# ======================================================================
_log1, _append1 = _capture_log()
_repaired1 = fd.repair_mixed_unit_price_series(
    _BCG_MIXED_SERIES, _BCG_REFERENCE, ticker="BCG.L", log=_append1
)
check("FIX: all 30 pence-era points rescaled to the ~2.3 GBP range "
      "(no false '99% crash')",
      all(2.0 <= p <= 2.6 for p in _repaired1[:30]))
check("FIX: all 6 already-GBP points left completely alone (same object "
      "values, not re-divided into pennies)",
      _repaired1[30:] == _BCG_POUND_MONTHS)
check("FIX: the repaired series overall is flat in GBP terms - max/min "
      "ratio nowhere near the ~100x/99% 'crash' shape the bug produced",
      (max(_repaired1) / min(_repaired1)) < 1.3)
check('FIX: exactly one "[units] BCG.L history repaired: 30 of 36 points '
      'rescaled pence->GBP" log line emitted',
      _log1 == ["[units] BCG.L history repaired: 30 of 36 points "
                "rescaled pence->GBP"])
print("[task29_t1_mixed_series_repair] BCG.L-shaped mixed pence/GBP series "
      "repaired flat in GBP, correct log line OK")

# ======================================================================
# T2 (item 1): _PENCE_HISTORY_REPAIR_RATIO_RANGE boundaries (30x-300x,
# inclusive) - a point just inside the band is rescaled, a point just
# outside it (a genuine ~29x or ~301x move, not a unit bug) is not.
# ======================================================================
_lo, _hi = fd._PENCE_HISTORY_REPAIR_RATIO_RANGE
_ref = 1.0
_boundary_series = [_lo * _ref, _hi * _ref, (_lo - 0.1) * _ref, (_hi + 1.0) * _ref]
_repaired2 = fd.repair_mixed_unit_price_series(_boundary_series, _ref, ticker=None)
check("FIX: a point at the lower ratio-range boundary (30.0x) IS rescaled",
      abs(_repaired2[0] - (_lo * _ref) / 100.0) < 1e-9)
check("FIX: a point at the upper ratio-range boundary (300.0x) IS rescaled",
      abs(_repaired2[1] - (_hi * _ref) / 100.0) < 1e-9)
check("FIX: a point just BELOW the lower boundary (29.9x - a real move, "
      "not a unit bug) is left untouched",
      _repaired2[2] == (_lo - 0.1) * _ref)
check("FIX: a point just ABOVE the upper boundary (301x - a real move, "
      "not a unit bug) is left untouched",
      _repaired2[3] == (_hi + 1.0) * _ref)
print("[task29_t2_ratio_range_boundaries] 30x/300x ratio-range boundaries "
      "(inclusive) behave exactly as specified OK")

# ======================================================================
# T3 (item 1, edge case): no usable reference price. Default
# (fallback_divide_all=True) falls back to the OLD blanket "/100
# everything" behaviour (never worse than before this fix) and logs the
# fallback suffix; fallback_divide_all=False instead leaves the series
# untouched.
# ======================================================================
for _bad_ref in (None, float("nan"), 0.0, -5.0):
    _log3, _append3 = _capture_log()
    _repaired3 = fd.repair_mixed_unit_price_series(
        [100.0, 200.0], _bad_ref, ticker="XX.L", log=_append3
    )
    check(f"FIX: no usable reference ({_bad_ref!r}) falls back to dividing "
          f"the whole series by 100 (fallback_divide_all defaults True)",
          _repaired3 == [1.0, 2.0])
    check(f"FIX: the no-reference fallback log line carries its own "
          f"explanatory suffix ({_bad_ref!r})",
          len(_log3) == 1 and _log3[0].startswith(
              "[units] XX.L history repaired: 2 of 2 points rescaled "
              "pence->GBP (no reference price") )
_repaired3b = fd.repair_mixed_unit_price_series(
    [100.0, 200.0], None, ticker="XX.L", fallback_divide_all=False
)
check("FIX: fallback_divide_all=False leaves the series completely "
      "untouched when no reference price is available",
      _repaired3b == [100.0, 200.0])
print("[task29_t3_no_reference_fallback] no-reference edge case: safe "
      "fallback to the old behaviour by default, opt-out available OK")

# ======================================================================
# T4 (item 5, regression guard): an all-GBP series (every point already
# agrees with the reference, ratio ~1x throughout) is left byte-
# identical - never rescaled, no log line at all.
# ======================================================================
_log4, _append4 = _capture_log()
_all_gbp = [2.28, 2.30, 2.31, 2.29, 2.30]
_repaired4 = fd.repair_mixed_unit_price_series(_all_gbp, 2.30, ticker="GLEN.L", log=_append4)
check("REGRESSION GUARD: an all-GBP series is returned byte-identical",
      _repaired4 == _all_gbp)
check("REGRESSION GUARD: no '[units]' log line at all when nothing was "
      "rescaled", _log4 == [])
print("[task29_t4_all_gbp_untouched] an all-GBP series (never pence) is "
      "completely unaffected, no log noise OK")

# ======================================================================
# T5 (item 1/2): repair_mixed_unit_price_dataframe() - BCG.L-shaped
# mixed OHLC frame. Open/High/Low/Close/Adj Close all move TOGETHER per
# row (a single row's columns never get out of sync with each other).
# ======================================================================
_idx = pd.date_range("2023-09-01", periods=36, freq="MS")
_mixed_df = pd.DataFrame({
    "Open": [p * 0.995 for p in _BCG_MIXED_SERIES],
    "High": [p * 1.01 for p in _BCG_MIXED_SERIES],
    "Low": [p * 0.99 for p in _BCG_MIXED_SERIES],
    "Close": _BCG_MIXED_SERIES,
    "Adj Close": _BCG_MIXED_SERIES,
}, index=_idx)
_log5, _append5 = _capture_log()
_repaired_df5 = fd.repair_mixed_unit_price_dataframe(
    _mixed_df, _BCG_REFERENCE, ticker="BCG.L", log=_append5
)
check("FIX: Close column flat in GBP after repair (no false crash)",
      (_repaired_df5["Close"].max() / _repaired_df5["Close"].min()) < 1.3)
check("FIX: every OHLC column for the pence-era rows moved together "
      "(Open/High/Low/Adj Close all divided by 100 alongside Close, "
      "same rows)",
      all(
          abs(_repaired_df5["Open"].iloc[i] / _repaired_df5["Close"].iloc[i]
              - _mixed_df["Open"].iloc[i] / _mixed_df["Close"].iloc[i]) < 1e-9
          for i in range(36)
      ))
check("FIX: the pounds-era rows' OHLC are untouched (still the original "
      "~2.3-scale values)",
      list(_repaired_df5["Close"].iloc[30:]) == _BCG_POUND_MONTHS)
check('FIX: "[units] BCG.L history repaired: 30 of 36 points..." log line '
      "emitted for the dataframe path too",
      len(_log5) == 1 and _log5[0] == (
          "[units] BCG.L history repaired: 30 of 36 points rescaled "
          "pence->GBP"))
print("[task29_t5_mixed_dataframe_repair] BCG.L-shaped mixed OHLC frame "
      "repaired flat in GBP, every column moves together per-row OK")

# ======================================================================
# T6 (item 5, regression guard): an ALL-PENCE dataframe (consistently
# ~100x the reference throughout, e.g. SHEL.L/TSCO.L-shaped) ends up
# fully divided by 100 - the SAME OUTCOME the old blanket divide
# produced, just reached via the new per-row mechanism.
# ======================================================================
_all_pence_closes = [2600.0, 2610.0, 2605.0, 2615.0]
_all_pence_df = pd.DataFrame({
    "Open": _all_pence_closes, "High": [c * 1.01 for c in _all_pence_closes],
    "Low": [c * 0.99 for c in _all_pence_closes], "Close": _all_pence_closes,
}, index=pd.date_range("2026-07-01", periods=4, freq="MS"))
_repaired_df6 = fd.repair_mixed_unit_price_dataframe(_all_pence_df, 26.15, ticker="SHEL.L")
check("REGRESSION GUARD: an all-pence dataframe ends up fully divided by "
      "100 - identical outcome to the pre-TASK-29 blanket divide",
      list(_repaired_df6["Close"]) == [26.0, 26.1, 26.05, 26.15])
print("[task29_t6_all_pence_dataframe_same_outcome] a consistently pence-"
      "quoted dataframe repairs to the exact same result the old blanket "
      "divide produced OK")

# ======================================================================
# T7 (items 1-3, end-to-end): fundamentals_data.get_bundle() for a
# BCG.L-shaped ticker with a MIXED pence/GBP 10y monthly history -
# prices_10y comes out flat in GBP (no false 99% crash), and the
# "[units] BCG.L history repaired: ..." line is printed.
# ======================================================================
def _fake_ticker(info, income=None, balance=None, cashflow=None,
                  dividends=None, history_fn=None):
    income = income if income is not None else pd.DataFrame()
    balance = balance if balance is not None else pd.DataFrame()
    cashflow = cashflow if cashflow is not None else pd.DataFrame()
    dividends = dividends if dividends is not None else pd.Series(dtype=float)

    class _FT:
        def __init__(self, *_a, **_k):
            self.info = dict(info)
            self.income_stmt = income
            self.balance_sheet = balance
            self.cashflow = cashflow
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = dividends

        @property
        def fast_info(self):
            raise AttributeError

        def history(self, *a, **k):
            return history_fn() if history_fn else pd.DataFrame()

    return _FT


_bcg_info = {
    "currency": "GBp", "currentPrice": 230.0, "regularMarketPrice": 230.0,
    "marketCap": 2.30 * 1_000_000, "sharesOutstanding": 1_000_000,
    "trailingEps": 0.12,
}
_bcg_hist = pd.DataFrame({"Close": _BCG_MIXED_SERIES}, index=_idx)

_stdout7 = io.StringIO()
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_bcg_info, history_fn=lambda: _bcg_hist)
    with contextlib.redirect_stdout(_stdout7):
        _bundle7 = fd.get_bundle("BCG.L", force_refresh=True)
_printed7 = _stdout7.getvalue().splitlines()

check("FIX: get_bundle() normalises info currentPrice to GBP (230p -> "
      "£2.30)", abs(_bundle7["info"]["currentPrice"] - 2.30) < 1e-9)
_p7 = _bundle7["prices_10y"]["prices"]
check("FIX: get_bundle()'s own prices_10y is flat in GBP across the "
      "whole 10y series - no false '99% crash' from the pence-era "
      "months", len(_p7) == 36 and (max(_p7) / min(_p7)) < 1.3)
check("FIX: every one of the 36 points now sits near the real current "
      "price (GBP 2.30), not the old ~100-360 broken range",
      all(1.8 <= p <= 2.8 for p in _p7))
check('FIX: the "[units] BCG.L history repaired: 30 of 36 points..." '
      "line was actually printed (so a post-deploy log grep can count "
      "affected .L tickers, per this task's own REPORT BACK instruction)",
      any("[units] BCG.L history repaired: 30 of 36 points rescaled "
          "pence->GBP" in line for line in _printed7))
print("[task29_t7_get_bundle_end_to_end] get_bundle() BCG.L-shaped fixture: "
      "prices_10y flat in GBP, no crash, '[units]' log line printed OK")

# ======================================================================
# T8 (item 1, downstream sanity): feeding T7's own repaired prices_10y
# into auto_compounder_engine's real PE machinery gives a SANE P/E
# (tens, not thousands) - this is what the broken "3,431x/.../1,779x"
# PE-by-year and "Average P/E 1,807x" PE Trailing bug actually looked
# like, and what BCG.L's real TTM P/E of ~25.56x should look like once
# prices_10y stops lying about the price.
# ======================================================================
_eps_income = pd.DataFrame(
    {"2026-06-30": [0.12], "2025-06-30": [0.11]}, index=["Basic EPS"]
)
_bundle8 = {
    "info": {"currentPrice": 2.30, "trailingEps": 0.12},
    "income": _eps_income,
    "income_q": pd.DataFrame(),
    "prices_10y": _bundle7["prices_10y"],
    "meta": {},
}
_year_end8 = ace._year_end_prices(_bundle8["prices_10y"], _bundle8["income"])
check("FIX: year-end prices resolved from the repaired series are all "
      "near the real £2.30 price, not the broken ~£230 pence-scale "
      "figure the bug would have produced",
      all(1.8 <= p <= 2.8 for p in _year_end8.values()))
_avg_pe8 = ace._avg_pe_3pt(_bundle8)
check("FIX: PE Ratio Average computed from the repaired series is sane "
      "(tens, matching BCG.L's real ~25.56x TTM P/E) - not the broken "
      "1,807x/3,431x-scale garbage the mixed-unit series produced",
      _avg_pe8 is not None and 5.0 < _avg_pe8 < 50.0)
print(f"[task29_t8_pe_sanity_downstream] auto_compounder_engine PE-by-year/"
      f"PE-average computed from the repaired prices_10y is sane "
      f"(avg P/E={_avg_pe8:.2f}x), not the broken thousands-x figure OK")

# ======================================================================
# T9 (item 5, regression guard): an all-GBP ticker (never pence-
# flagged - currency is plain "GBP") is completely untouched by
# get_bundle() - the new elif branch never fires at all.
# ======================================================================
_glen_info = {"currency": "GBP", "currentPrice": 12.50,
              "marketCap": 12.50 * 1_000_000, "sharesOutstanding": 1_000_000,
              "trailingEps": 0.625}
_glen_closes = [12.0, 12.2, 12.4, 12.5]
_glen_hist = pd.DataFrame({"Close": _glen_closes},
                           index=pd.date_range("2026-07-01", periods=4, freq="MS"))
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_glen_info, history_fn=lambda: _glen_hist)
    _bundle9 = fd.get_bundle("GLEN.L", force_refresh=True)
check("REGRESSION GUARD: an all-GBP ticker's prices_10y is byte-identical "
      "- price_quote_unit is None, so the repair branch never runs",
      _bundle9["prices_10y"]["prices"] == _glen_closes)
print("[task29_t9_all_gbp_ticker_untouched] a plain-GBP (never pence-"
      "flagged) ticker's price history is completely unaffected OK")

# ======================================================================
# T10 (item 5, regression guard): a non-.L ticker (AAPL, USD) is
# completely untouched.
# ======================================================================
_aapl_info = {"currency": "USD", "currentPrice": 230.0,
              "marketCap": 230.0 * 1_000_000, "sharesOutstanding": 1_000_000,
              "trailingEps": 6.0}
_aapl_closes = [225.0, 228.0, 230.0]
_aapl_hist = pd.DataFrame({"Close": _aapl_closes},
                           index=pd.date_range("2026-08-01", periods=3, freq="MS"))
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_aapl_info, history_fn=lambda: _aapl_hist)
    _bundle10 = fd.get_bundle("AAPL", force_refresh=True)
check("REGRESSION GUARD: a non-.L USD ticker's prices_10y is byte-"
      "identical", _bundle10["prices_10y"]["prices"] == _aapl_closes)
print("[task29_t10_non_london_ticker_untouched] a non-.L (AAPL) ticker's "
      "price history is completely unaffected OK")

# ======================================================================
# T11 (item 5, regression guard): a CONSISTENTLY pence-quoted ticker
# (SHEL.L-shaped, every point ~100x the reference throughout, no unit
# switch) ends up with the SAME OUTCOME as the old blanket divide -
# fully rescaled to GBP - just reached via the new per-point mechanism.
# ======================================================================
_shel_info = {"currency": "GBp", "currentPrice": 2600.0,
              "marketCap": 26.0 * 1_000_000, "sharesOutstanding": 1_000_000,
              "trailingEps": 150.0}
_shel_closes = [2590.0, 2605.0, 2615.0, 2600.0]
_shel_hist = pd.DataFrame({"Close": _shel_closes},
                           index=pd.date_range("2026-08-01", periods=4, freq="MS"))
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_shel_info, history_fn=lambda: _shel_hist)
    _bundle11 = fd.get_bundle("SHEL.L", force_refresh=True)
check("REGRESSION GUARD: a consistently-pence ticker's prices_10y ends up "
      "fully divided by 100 - same outcome the old blanket divide gave",
      [round(p, 2) for p in _bundle11["prices_10y"]["prices"]]
      == [25.9, 26.05, 26.15, 26.0])
print("[task29_t11_all_pence_ticker_same_outcome] a consistently pence-"
      "quoted ticker (no unit switch) repairs to the exact same outcome "
      "the pre-TASK-29 blanket divide produced OK")

# ======================================================================
# T12 (item 1, end-to-end): fundamentals_data.normalize_pence_quote()'s
# own history_df path (shared by nightly_scan.analyze_ticker_lite() and
# app.py's Deep Dive headline) - BCG.L-shaped mixed OHLC frame repaired
# flat in GBP, log line emitted.
# ======================================================================
_log12, _append12 = _capture_log()
_info12 = {"currency": "GBp", "currentPrice": 230.0}
_info_out12, _hist_out12, _div_out12, _meta12 = fd.normalize_pence_quote(
    "BCG.L", dict(_info12), history_df=_mixed_df.copy(), log=_append12
)
check("FIX: normalize_pence_quote() repairs a mixed-unit history_df flat "
      "in GBP",
      (_hist_out12["Close"].max() / _hist_out12["Close"].min()) < 1.3)
check("FIX: normalize_pence_quote()'s own info normalisation still works "
      "(currentPrice 230p -> £2.30)",
      abs(_info_out12["currentPrice"] - 2.30) < 1e-9)
check('FIX: normalize_pence_quote() emits the same "[units] BCG.L history '
      'repaired..." log line via its own log= parameter',
      any("[units] BCG.L history repaired: 30 of 36 points rescaled "
          "pence->GBP" in line for line in _log12))
print("[task29_t12_normalize_pence_quote_history_df] normalize_pence_quote()"
      "'s own history_df path repairs a mixed-unit frame flat in GBP OK")

# ======================================================================
# T13 (item 5, regression guard): normalize_pence_quote() on an all-
# pence history_df (consistent, no unit switch) - same outcome as the
# old blanket divide.
# ======================================================================
_info13 = {"currency": "GBp", "currentPrice": 2600.0}
_, _hist_out13, _, _ = fd.normalize_pence_quote(
    "SHEL.L", dict(_info13), history_df=_shel_hist.copy()
)
check("REGRESSION GUARD: normalize_pence_quote() on a consistently-pence "
      "history_df still fully divides by 100 - same outcome as before",
      [round(c, 2) for c in _hist_out13["Close"]] == [25.9, 26.05, 26.15, 26.0])
print("[task29_t13_normalize_pence_quote_all_pence_same_outcome] a "
      "consistently pence-quoted history_df repairs to the same outcome "
      "the old blanket divide gave OK")

# ======================================================================
# T14 (item 1, end-to-end): nightly_scan._reprice_row()'s own repair -
# a mixed-unit 3mo close window, repaired against row["Price"] (the
# last FULL scan's own already-normalised GBP price), producing a sane
# repriced Price instead of a ~100x-inflated one.
# ======================================================================
_reprice_closes_mixed = [230.0, 229.0, 231.0, 2.29, 2.31, 2.30]  # a unit
# switch happening mid-window - the exact BCG.L-shaped symptom at this
# site too.
_hist_df14 = pd.DataFrame(
    {"Close": _reprice_closes_mixed, "Volume": [500_000] * 6},
    index=pd.date_range("2026-09-01", periods=6, freq="D"),
)
_row14 = {"Ticker": "BCG.L", "price_quote_unit": "GBp", "Price": 2.30,
          "Intrinsic Value": 3.00, "Quality": 60.0, "Discovery (lite)": 5.0}
_log14, _append14 = _capture_log()
_new_row14 = nightly_scan._reprice_row(_row14, _hist_df14, universe_attention_lite=True, log=_append14)
check("FIX: _reprice_row() repairs a mixed-unit window and lands on a "
      "sane GBP price (~£2.30), not a ~100x-inflated pence figure",
      _new_row14 is not None and 1.8 <= _new_row14["Price"] <= 2.8)
check('FIX: _reprice_row() emits its own "[units] BCG.L history '
      'repaired..." log line via the new log= parameter',
      any("[units] BCG.L history repaired" in line for line in _log14))
print(f"[task29_t14_reprice_row_mixed_unit] _reprice_row() BCG.L-shaped "
      f"mixed-unit window -> Price={_new_row14['Price']:.2f} (sane GBP), "
      f"log line emitted OK")

# ======================================================================
# T15 (item 5, regression guard): the pre-existing test_lists_display_
# commit2_history_pence.py T4 case (no "Price" field at all on the row)
# still falls back to the old blanket-divide outcome - never worse than
# before this task.
# ======================================================================
_hist_df15 = pd.DataFrame(
    {"Close": [3589.0, 3600.0, 3610.0, 3605.0, 3615.0, 3620.0],
     "Volume": [1_000_000] * 6},
    index=pd.date_range("2026-09-01", periods=6, freq="D"),
)
_row15 = {"Ticker": "TSCO.L", "price_quote_unit": "GBp", "Intrinsic Value": 40.0,
          "Quality": 60.0, "Discovery (lite)": 5.0}
_new_row15 = nightly_scan._reprice_row(_row15, _hist_df15, universe_attention_lite=True)
check("REGRESSION GUARD: a row with no prior Price (no reference "
      "available) still falls back to dividing the whole window by "
      "100 -> 36.20, same as before TASK 29",
      _new_row15 is not None and abs(_new_row15["Price"] - 36.20) < 1e-9)
print("[task29_t15_reprice_row_no_reference_same_outcome] the no-"
      "reference _reprice_row() case still lands on the pre-TASK-29 "
      "outcome OK")

# ======================================================================
# T16 (item 4): compounder_ui._cp_price_chart()'s "10y Average" chart
# annotation now uses the ticker's own currency symbol (currency_format.
# format_money()) instead of a hardcoded "$" - £ for a .L ticker, "$"
# still unchanged for a USD ticker.
# ======================================================================
_price_history16 = {
    "BCG.L": {"dates": ["2026-01-01", "2026-02-01"], "prices": [2.20, 2.30],
              "avg_10y": 2.1543},
    "AAPL": {"dates": ["2026-01-01", "2026-02-01"], "prices": [225.0, 230.0],
             "avg_10y": 198.76},
}
_fig_l = compounder_ui._cp_price_chart("BCG.L", _price_history16)
_ann_l = _fig_l.layout.annotations[0].text
check('FIX: a .L ticker\'s "10y Average" annotation shows £, not a '
      'hardcoded "$" (BCG.L GBP 2.15)',
      "£2.15" in _ann_l and "$" not in _ann_l)

_fig_usd = compounder_ui._cp_price_chart("AAPL", _price_history16)
_ann_usd = _fig_usd.layout.annotations[0].text
check("REGRESSION GUARD: a USD ticker's \"10y Average\" annotation still "
      "shows $198.76 - byte-identical to before this task",
      "$198.76" in _ann_usd)
print(f"[task29_t16_cp_price_chart_currency_symbol] _cp_price_chart() "
      f"annotation: .L shows '{_ann_l}' (£, no $), AAPL shows '{_ann_usd}' "
      f"(unchanged $) OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
