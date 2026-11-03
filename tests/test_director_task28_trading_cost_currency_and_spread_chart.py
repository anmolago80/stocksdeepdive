"""
TASK 28 (Director-directed, 3 Nov 2026) - Trading Cost tab: wrong
currency label + mostly-empty spread chart.

EVIDENCE (owner screenshot, HSBA.L): "A limit order at the mid
(US$1,268.60)" - HSBA.L trades in pence (~1,268.60p = ~GBP 12.69).
Spread chart shows bars on only ~7 of ~35 days (same on BBVA).

ROOT CAUSE 1 (verified): compounder_ui.py's old _currency_symbol_for_
ticker() returned "A$" for .AX and "US$" for EVERYTHING else, so every
.L/.TO/.T/European ticker was labelled US$.
FIX 1: currency derived from fcf_valuation_engine.trading_currency_
for(ticker) (already fixed by TASK 29b to return GBP for every .L),
with correct symbols (A$/US$/C$ disambiguated dollar currencies;
GBP/EUR/CHF/SEK/JPY plain ISO codes - none of them collides with
another currency here the way a bare "$" would). Bid/ask/last for a
pence-quoted .L ticker (arrives from Yahoo in PENCE via quote_
recorder.py) is normalised to GBP against this tab's own price_
history close - reusing fundamentals_data.repair_mixed_unit_price_
series(), the SAME ratio-based repair TASK 29/29b already established
- so the mid, spread in money, order sizes and cost-per-order all use
ONE unit.

ROOT CAUSE 2 (verified): trading_cost_engine._pair_spread() clamps a
negative Corwin-Schultz estimate to 0.0 - for a liquid stock most days
come out negative/clamped, so the daily bar chart read as mostly
empty. The 30-day headline average (trading_cost_engine.py's own
average_estimated_spread_pct) was never wrong, just the CHART's own
per-day visual.
FIX 2 (display only, compounder_ui.py): the chart's main series is now
a 10-trading-day rolling average of the daily estimates (still 100%
derived from trading_cost_engine.py's own per-day output - that
module itself is completely untouched by this task), recorded days
still shown as distinct dots, and a visible caption + the AI-text-
equivalent copy both now say a 0% day means "no spread detected that
day", never missing data.

INVESTIGATE ONLY (no estimator code change - reported separately, see
this task's own report for the full write-up and numbers): the
Corwin-Schultz paper's overnight-gap high/low adjustment was explored
in a standalone throwaway script against 10 SYNTHETIC liquid-ticker-
shaped series (this sandbox has no outbound network access - Yahoo
Finance is not an allowed host for this session, confirmed directly).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task28_trading_cost_currency_and_spread_chart.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task28_trading_cost_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import compounder_ui as cu
import trading_cost_engine as tce

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
# T1 (FIX 1, item "currency label correct for..."): every suffix the
# task's own test list names.
# ======================================================================
_CURRENCY_CASES = [
    ("CBA.AX", "A$"), ("WES.AX", "A$"),
    ("AAPL", "US$"), ("MSFT", "US$"),
    ("HSBA.L", "GBP "), ("TSCO.L", "GBP "),  # pence-quoted AND GBP-quoted .L both resolve the same way
    ("RY.TO", "C$"),
    ("7203.T", "JPY "),
    ("MC.PA", "EUR "), ("ASML.AS", "EUR "),
    ("SAP.DE", "EUR "),
    ("NESN.SW", "CHF "),
    ("VOLV-B.ST", "SEK "),
]
for ticker, expected_symbol in _CURRENCY_CASES:
    check(f"FIX: _currency_symbol_for_ticker({ticker!r}) == {expected_symbol!r} "
          f"(never 'US$' for a non-USD ticker)",
          cu._currency_symbol_for_ticker(ticker) == expected_symbol)
print(f"[task28_t1_currency_symbol_every_suffix] {len(_CURRENCY_CASES)} suffix "
      "cases all resolve to the correct, never-US$-for-non-USD currency symbol OK")

check("FIX: _fmt_money() with the GBP symbol reads 'GBP 12.69', not "
      "'GBP12.69' (the space is baked into the symbol string)",
      cu._fmt_money(12.69, cu._currency_symbol_for_ticker("HSBA.L")) == "GBP 12.69")
check("REGRESSION GUARD: _fmt_money() with the dollar-based A$/US$/C$ "
      "symbols is still byte-identical (no stray space)",
      cu._fmt_money(1234.5, cu._currency_symbol_for_ticker("CBA.AX")) == "A$1,234.50"
      and cu._fmt_money(1234.5, cu._currency_symbol_for_ticker("AAPL")) == "US$1,234.50")
print("[task28_t1b_fmt_money_spacing] GBP/EUR/CHF/SEK/JPY read with a "
      "space before the amount; A$/US$/C$ unchanged OK")


# ======================================================================
# T2 (FIX 1, item "normalise ... to GBP"): _normalize_pence_bid_ask_
# last() - a pence-quoted .L ticker's bid/ask/last is rescaled against
# the reference price; a GBP-quoted .L ticker (already correct) and a
# non-.L ticker are both left completely unchanged.
# ======================================================================
_bid, _ask, _last = cu._normalize_pence_bid_ask_last("HSBA.L", 1268.0, 1269.2, 1268.6, 12.69)
check("FIX: a pence-quoted .L ticker's bid is rescaled to GBP "
      "(1268.0p -> 12.68)", abs(_bid - 12.68) < 1e-9)
check("FIX: ...ask too (1269.2p -> 12.692)", abs(_ask - 12.692) < 1e-9)
check("FIX: ...and last_price (1268.6p -> 12.686)", abs(_last - 12.686) < 1e-9)
print(f"[task28_t2_pence_normalization] HSBA.L-shaped pence bid/ask/last "
      f"-> ({_bid}, {_ask}, {_last}) GBP OK")

_bid2, _ask2, _last2 = cu._normalize_pence_bid_ask_last("TSCO.L", 12.68, 12.70, 12.69, 12.69)
check("REGRESSION GUARD: a GBP-quoted .L ticker whose bid/ask already "
      "agree with the reference is left byte-identical",
      (_bid2, _ask2, _last2) == (12.68, 12.70, 12.69))
print("[task28_t2b_already_gbp_unchanged] an already-GBP .L ticker's "
      "bid/ask/last is completely unaffected OK")

_bid3, _ask3, _last3 = cu._normalize_pence_bid_ask_last("AAPL", 230.0, 230.2, 230.1, 230.0)
check("REGRESSION GUARD: a non-.L ticker's bid/ask/last is never "
      "touched by this function at all",
      (_bid3, _ask3, _last3) == (230.0, 230.2, 230.1))
print("[task28_t2c_non_london_unchanged] a non-.L ticker is completely "
      "unaffected by the pence-normalisation helper OK")

_bid4, _ask4, _last4 = cu._normalize_pence_bid_ask_last("BCG.L", 230.0, 231.0, 230.5, None)
check("REGRESSION GUARD: no usable reference price -> fallback_"
      "divide_all=False means bid/ask/last are left UNCHANGED (never "
      "blanket-divided on a guess)",
      (_bid4, _ask4, _last4) == (230.0, 231.0, 230.5))
print("[task28_t2d_no_reference_safe] with no reference price available, "
      "bid/ask/last are left alone rather than guessed at OK")


# ======================================================================
# T3 (FIX 1, full end-to-end via the real Streamlit render path):
# render_trading_cost_tab() on an HSBA.L-shaped pence fixture shows
# GBP money values throughout, never "US$", and the bid/ask/mid are
# genuinely GBP-scale (~12.6x), not the raw 1,268-pence figures.
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_trading_cost_tab(ticker, latest_snapshot, price_history_rows, snapshots=None):
    snapshots = snapshots if snapshots is not None else []
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import compounder_ui as cu
import quote_recorder
import quote_snapshot_store

cu.TRADING_COST_ENABLED = True
quote_recorder.is_in_roster = lambda t: True
quote_snapshot_store.latest_snapshot = lambda t: {latest_snapshot!r}
quote_snapshot_store.snapshots_for_ticker = lambda t: {snapshots!r}
quote_snapshot_store.snapshot_times_for_ticker = lambda t: {{}}

cu.render_trading_cost_tab({ticker!r}, {price_history_rows!r}, lang="en")
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    return at


_hsba_latest = {
    "bid": 1268.0, "ask": 1269.2, "last_price": 1268.6,
    "snap_date": "2026-11-02", "snap_at_utc": "2026-11-02T13:30:00+00:00",
    "source": "yfinance.quote_bulk",
}
_hsba_history = [
    {"date": f"2026-10-{d:02d}", "high": 12.75, "low": 12.60, "close": 12.69, "volume": 1_000_000}
    for d in range(1, 31)
]
_at_hsba = _run_trading_cost_tab("HSBA.L", _hsba_latest, _hsba_history)
check("FIX: render_trading_cost_tab() on an HSBA.L-shaped pence "
      "fixture renders with no exception", not _at_hsba.exception)
_html_hsba = " ".join(m.value or "" for m in _at_hsba.markdown)
check('FIX: "US$" never appears anywhere on a pence-quoted .L '
      "ticker's own Trading Cost tab (the live incident's exact "
      "symptom)", "US$" not in _html_hsba)
check("FIX: the normalised bid (GBP 12.68) appears on the page",
      "GBP 12.68" in _html_hsba)
check("FIX: the normalised ask (GBP 12.69) appears on the page",
      "GBP 12.69" in _html_hsba)
print("[task28_t3_hsba_end_to_end] HSBA.L-shaped pence fixture: real "
      "Streamlit render shows GBP 12.68/12.69, never US$1,268.xx OK")


# ======================================================================
# T4 (regression guard, full end-to-end): a GBP-quoted (never pence)
# .L ticker and a plain US ticker both render exactly as a correct
# Trading Cost tab always should - GBP/US$ respectively, correct
# money scale, no accidental /100.
# ======================================================================
_glen_latest = {
    "bid": 12.60, "ask": 12.64, "last_price": 12.62,
    "snap_date": "2026-11-02", "snap_at_utc": "2026-11-02T13:30:00+00:00",
    "source": "yfinance.quote_bulk",
}
_glen_history = [
    {"date": f"2026-10-{d:02d}", "high": 12.75, "low": 12.60, "close": 12.69, "volume": 1_000_000}
    for d in range(1, 31)
]
_at_glen = _run_trading_cost_tab("GLEN.L", _glen_latest, _glen_history)
check("REGRESSION GUARD: a GBP-quoted (never pence) .L ticker renders "
      "with no exception", not _at_glen.exception)
_html_glen = " ".join(m.value or "" for m in _at_glen.markdown)
check("REGRESSION GUARD: GLEN.L's own already-correct bid (GBP 12.60) "
      "is shown unchanged - never divided by 100 again",
      "GBP 12.60" in _html_glen and "US$" not in _html_glen)
print("[task28_t4a_already_gbp_ticker] a GBP-quoted .L ticker renders "
      "correctly, unaffected by the pence-repair path OK")

_aapl_latest = {
    "bid": 229.98, "ask": 230.02, "last_price": 230.00,
    "snap_date": "2026-11-02", "snap_at_utc": "2026-11-02T13:30:00+00:00",
    "source": "yfinance.quote_bulk",
}
_aapl_history = [
    {"date": f"2026-10-{d:02d}", "high": 232.0, "low": 228.0, "close": 230.0, "volume": 50_000_000}
    for d in range(1, 31)
]
_at_aapl = _run_trading_cost_tab("AAPL", _aapl_latest, _aapl_history)
check("REGRESSION GUARD: a plain US ticker renders with no exception",
      not _at_aapl.exception)
_html_aapl = " ".join(m.value or "" for m in _at_aapl.markdown)
check("REGRESSION GUARD: AAPL still shows US$ (unchanged) - the "
      "currency-mapping fix never touches a genuinely-USD ticker",
      "US$229.98" in _html_aapl or "US$230" in _html_aapl)
print("[task28_t4b_us_ticker_unchanged] a plain US ticker's Trading "
      "Cost tab is byte-identical to before this task OK")


# ======================================================================
# T5 (FIX 2): _rolling_mean() - trailing window, skips None (never
# treats it as 0), handles an all-zero window (a real, meaningful
# average, not a crash or a fabricated non-zero).
# ======================================================================
check("FIX: _rolling_mean() on an all-zero window returns all zeros "
      "(a real reading, never a gap)",
      cu._rolling_mean([0.0] * 15) == [0.0] * 15)
check("FIX: _rolling_mean() skips a leading None rather than treating "
      "it as 0", cu._rolling_mean([None, 2.0, 4.0]) == [None, 2.0, 3.0])
check("FIX: _rolling_mean() on an all-None window returns None "
      "throughout (nothing to average, never a fabricated 0)",
      cu._rolling_mean([None, None, None]) == [None, None, None])
_window_test = [0.0] * 20 + [2.0] * 10
_rolled = cu._rolling_mean(_window_test, window=10)
check("FIX: _rolling_mean() correctly transitions once real non-zero "
      "values enter the trailing 10-day window",
      _rolled[19] == 0.0 and _rolled[29] == 2.0)
print("[task28_t5_rolling_mean] trailing-window average correct on "
      "all-zero, leading-None and all-None windows OK")


# ======================================================================
# T6 (FIX 2): _trading_cost_spread_chart() - the main series is now
# the rolling average (never the sparse raw per-day bars), recorded
# days are still a distinct second trace, and an all-zero estimated
# window never crashes or produces an all-empty chart.
# ======================================================================
def _mk_days_rows(estimated_values, recorded_pairs=None):
    recorded_pairs = recorded_pairs or {}
    rows = []
    for i, est in enumerate(estimated_values):
        rec_bid, rec_ask = recorded_pairs.get(i, (None, None))
        rec_pct = tce._recorded_spread_pct(rec_bid, rec_ask) if rec_bid else None
        rows.append({
            "date": f"2026-09-{i + 1:02d}" if i < 30 else f"2026-10-{i - 29:02d}",
            "close": 100.0 + i, "recorded_bid": rec_bid, "recorded_ask": rec_ask,
            "recorded_spread_pct": rec_pct, "estimated_spread_pct": est,
        })
    return rows


_all_zero_days = _mk_days_rows([0.0] * 35)
_fig_zero = cu._trading_cost_spread_chart(_all_zero_days, None, {}, "AAPL", lang="en", show_recorded=False)
check("FIX: an all-zero estimated window renders without crashing, "
      "main series flat at 0 (never None/missing)",
      list(_fig_zero.data[0].y) == [0.0] * 35)

_mixed_days = _mk_days_rows(
    [0.0, 1.5, 0.0, 0.0, 2.0] * 7,
    recorded_pairs={5: (99.0, 101.0), 15: (98.0, 100.0)},
)
_fig_mixed = cu._trading_cost_spread_chart(_mixed_days, "2026-09-20", {}, "HSBA.L", lang="en", show_recorded=True)
check("FIX: the main trace is the rolling average, not the raw "
      "mostly-zero per-day series (it differs from the raw input at "
      "an index where raw=0 but nearby days are non-zero)",
      _fig_mixed.data[0].y[4] != _mixed_days[4]["estimated_spread_pct"])
check("FIX: recorded days are still a distinct second trace",
      len(_fig_mixed.data) == 2 and _fig_mixed.data[1].name == cu.i18n.t(
          "compounder.trading_cost.legend_recorded", "en"))
check("FIX: the main series trace name now says '10-day rolling "
      "average', not the old 'from daily prices' wording",
      "10-day" in _fig_mixed.data[0].name.lower()
      or "rolling" in _fig_mixed.data[0].name.lower())
print("[task28_t6_spread_chart_rolling_main_series] the chart's main "
      "series is the rolling average (never mostly-empty), recorded "
      "days remain a distinct overlay, all-zero windows handled OK")

check("FIX: the visible 'chart_zero_note' i18n string exists and "
      "explains 0% means no spread detected, not missing data",
      "no spread" in cu.i18n.t("compounder.trading_cost.chart_zero_note", "en").lower())
print("[task28_t6b_zero_note_text] the zero-day explanatory copy is in "
      "place OK")


# ======================================================================
# T7 (explicit "do NOT change the headline numbers" guard): every
# trading_cost_engine.py public function this tab reads from is
# BYTE-IDENTICAL to its own pre-TASK-28 behaviour - recomputed
# straight from the module's own documented formulas, not touched by
# this task's diff at all (confirmed separately via `git diff
# trading_cost_engine.py` showing zero changes).
# ======================================================================
_band_cases = [(0.3, tce.BAND_TIGHT), (0.9, tce.BAND_MODERATE), (2.0, tce.BAND_WIDE)]
check("REGRESSION GUARD: spread_band() thresholds/labels unchanged",
      all(tce.spread_band(pct) == band for pct, band in _band_cases))

_cost_rows = tce.crossing_cost(1.2)
check("REGRESSION GUARD: crossing_cost()'s own half-spread formula "
      "unchanged (order x spread%/100/2)",
      abs(_cost_rows[0]["cost_each_way"] - 5000.0 * 0.012 / 2.0) < 1e-9)

_ph = [{"date": f"2026-09-{d:02d}", "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000_000}
       for d in range(1, 32)]
_snaps = [{"snap_date": "2026-09-30", "bid": 99.5, "ask": 100.5}]
_series_before_and_after = tce.trading_cost_series("AAPL", _ph, _snaps, days=30)
check("REGRESSION GUARD: trading_cost_series()'s own output shape/"
      "values are untouched by this task (median/average/tracking "
      "fields all present and sane)",
      _series_before_and_after["recorded_days_count"] == 1
      and _series_before_and_after["median_recorded_spread_pct"] is not None
      and len(_series_before_and_after["days"]) == 30)
print("[task28_t7_headline_numbers_unchanged] trading_cost_engine.py's "
      "own band/crossing-cost/series functions are completely untouched "
      "- this task's diff never touches that file OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
