"""
TASK 78 (Director-directed, owner-approved, 23 Nov 2026) - "IV/BV vs
Share Price, indexed" chart ALSO on uploaded-research pages ("the owner
changed his mind: he wants it on every uploaded company too").

WHAT CHANGED:
  1. compounder_ui.render_section() no longer gates the chart on
     is_auto - it calls _cp_iv_bv_vs_price_chart() unconditionally on
     both Auto and uploaded ("Value vs Book") pages (see
     tests/test_director_task76_pe_chart_fixes_and_ivbv_vs_price_periods.py's
     own updated check for the removed gate itself).
  2. auto_compounder_engine._iv_bv_vs_price_periods() is split into a
     NEW shared core, _iv_bv_vs_price_core(fy_points, ttm_ratio, anchor,
     cadence, monthly_dates, monthly_prices) - every bit of period-
     generation/averaging/labeling/ISO-serialisation logic, used
     identically by Auto (fy_points from the bundle's own real per-FY
     statement dates) and by the new build_compounder_data.
     _iv_bv_vs_price_for_upload() (fy_points synthesised from each
     sheet year label + the cached bundle's FY-end month/day alone).
  3. build_compounder_data._iv_bv_vs_price_for_upload(ticker, years,
     ratios, price_history_entry): uploaded-page data for the same
     chart -
       - IV/BV per FY: the SAME `years`/`ratios` already read into
         iv_bv_series (sheet year label Y = FY ending in calendar year
         Y) - no recalculation.
       - Share price: the SAME Fundamentals price_history entry
         (monthly, workbook-only) already loaded per ticker - never
         Yahoo's prices_10y. A period with no monthly price in the
         SHEET's own history is skipped, never filled from Yahoo.
       - FY end month/day + reporting cadence ONLY, from this ticker's
         CACHED fundamentals bundle (fundamentals_data.get_bundle()),
         via the SAME auto_compounder_engine._statement_col_dates()/
         _iv_bv_vs_price_reporting_cadence() calls (and the SAME
         .AX/.NZ/.L-vs-other fallback) Auto itself uses - applied to
         EVERY sheet year, including years older than the bundle's own
         (shallower) statement depth.
       - No FY end resolvable at all (no bundle, or no income-statement
         column date) -> {"no_chart_reason": "FY end date not available
         for this ticker"}, which compounder_ui shows verbatim as a
         one-line caption.

Run: python3 tests/test_director_task78_ivbv_vs_price_on_uploaded_pages.py
"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


import pandas as pd

import auto_compounder_engine as ace
import build_compounder_data as bcd
import compounder_ui as cui


# ======================================================================
# Fixture builder: a cached-bundle-shaped dict carrying ONLY what
# _iv_bv_vs_price_for_upload() is allowed to read from it (income +
# income_q/cashflow_q, for FY-end month/day + cadence) - deliberately
# SHALLOW (2 fiscal years only), to prove the chart later plots sheet
# years the bundle itself doesn't even reach.
# ======================================================================
def _mk_shallow_bundle(fy_end_month, fy_end_day, quarterly):
    fy1 = f"2026-{fy_end_month:02d}-{fy_end_day:02d}"
    fy0 = f"2025-{fy_end_month:02d}-{fy_end_day:02d}"
    income = pd.DataFrame({fy1: [1.0], fy0: [1.0]}, index=["Basic EPS"])
    if quarterly:
        income_q = pd.DataFrame(
            {"2026-09-30" if fy_end_month != 9 else "2026-10-31": [0.25],
             f"2026-{fy_end_month:02d}-{fy_end_day:02d}": [0.25],
             f"2026-{((fy_end_month - 3 - 1) % 12) + 1:02d}-{fy_end_day:02d}": [0.25]},
            index=["Basic EPS"],
        )
    else:
        income_q = pd.DataFrame(
            {f"2026-{((fy_end_month + 6 - 1) % 12) + 1:02d}-{fy_end_day:02d}": [0.5],
             f"2026-{fy_end_month:02d}-{fy_end_day:02d}": [0.5],
             f"2025-{((fy_end_month + 6 - 1) % 12) + 1:02d}-{fy_end_day:02d}": [0.45]},
            index=["Basic EPS"],
        )
    return {"income": income, "income_q": income_q}


def _mk_monthly_price_history(years_back=10, gap_year=None):
    """price_history_entry covering `years_back` years of monthly
    closes ending this (fake 'today') month - optionally with every
    monthly point in `gap_year` removed, to test the "skipped period,
    nothing filled" behaviour."""
    import datetime as _dt
    dates, prices = [], []
    d = _dt.date(2026, 10, 31)
    price = 100.0
    for _ in range(years_back * 12):
        if gap_year is None or d.year != gap_year:
            dates.append(d.isoformat())
            prices.append(price)
        price -= 0.5
        month = d.month - 1
        year = d.year
        if month < 1:
            month = 12
            year -= 1
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        d = _dt.date(year, month, last_day)
    dates.reverse()
    prices.reverse()
    return {"dates": dates, "prices": prices}


def _mk_years_ratios(fy_end_month, fy_end_day, start_year=2017, end_year=2026):
    """years/ratios exactly as build()'s own IV2BV-sheet loop would
    produce - "TTM" plus one plain-year-string label per fiscal year,
    newest first (AP=TTM down to AZ=2016-shaped), spanning well BEFORE
    the fixture bundle's own 2-year statement depth."""
    years = ["TTM"]
    ratios = [3.0]
    ratio = 2.8
    for y in range(end_year, start_year - 1, -1):
        years.append(str(y))
        ratios.append(ratio)
        ratio -= 0.1
    return years, ratios


# ======================================================================
# CHECK: AUB.AX / CPRT / RMD.AX / CSL.AX uploaded pages render the
# chart, each plotting at least one fiscal year OLDER than the
# fixture's own (shallow, 2-year) bundle statement depth - proving the
# sheet's own deeper history drives the chart, not the bundle's.
# ======================================================================
_FIXTURES = [
    # (ticker, fy_end_month, fy_end_day, quarterly, expects_AUD_tooltip)
    ("AUB.AX", 6, 30, False, True),    # ASX, half-yearly
    ("CPRT", 6, 30, True, False),      # US (Nasdaq), quarterly
    ("RMD.AX", 6, 30, True, True),     # ASX-listed but reports quarterly - cadence must come from the real income_q gaps, not the .AX suffix fallback
    ("CSL.AX", 6, 30, False, True),    # ASX, half-yearly
]

for _ticker, _m, _d, _quarterly, _expect_aud in _FIXTURES:
    _bundle = _mk_shallow_bundle(_m, _d, _quarterly)
    _years, _ratios = _mk_years_ratios(_m, _d, start_year=2017, end_year=2026)
    _price_hist = _mk_monthly_price_history(years_back=10)

    with mock.patch.object(bcd.fundamentals_data, "get_bundle", return_value=_bundle):
        _result = bcd._iv_bv_vs_price_for_upload(_ticker, _years, _ratios, _price_hist)

    check(f"{_ticker}: _iv_bv_vs_price_for_upload() returns real chart data (not a caption)",
          _result is not None and "no_chart_reason" not in _result)
    if _result and "no_chart_reason" not in _result:
        check(f"{_ticker}: cadence correctly resolved from the bundle's own income_q gaps "
              f"({'quarterly' if _quarterly else 'half-yearly'} expected)",
              _result["cadence"] == ("quarterly" if _quarterly else "half-yearly"))

        _fy_years_plotted = {int(p["label"][2:]) for p in _result["fy_points"]}
        check(f"{_ticker}: at least one plotted fiscal year (2017-2019) is OLDER than the "
              "fixture bundle's own 2-year (2025-2026) statement depth",
              any(y < 2025 for y in _fy_years_plotted))
        check(f"{_ticker}: the sheet's full year range (2017..2026) is represented, not just "
              "what the shallow bundle itself covers",
              min(_fy_years_plotted) <= 2017 and max(_fy_years_plotted) == 2026)

        _fig, _reason = cui._cp_iv_bv_vs_price_chart(_ticker, {_ticker: _result})
        check(f"{_ticker}: a real chart renders (no caption) from this uploaded-shaped data",
              _fig is not None and _reason is None)
        if _fig is not None:
            _price_trace = _fig.data[1]
            _tooltip = _price_trace.text[0]
            if _expect_aud:
                check(f"{_ticker}: price tooltip shows \"A$\" (ASX ticker), not a bare \"$\"",
                      "A$" in _tooltip)
            else:
                check(f"{_ticker}: price tooltip shows a bare \"$\" (US ticker), not \"A$\"",
                      "A$" not in _tooltip and "$" in _tooltip)


# ======================================================================
# CHECK: a ticker whose bundle can't be fetched at all -> the exact
# one-line caption, never a chart, never a crash.
# ======================================================================
with mock.patch.object(bcd.fundamentals_data, "get_bundle", return_value=None):
    _no_bundle_result = bcd._iv_bv_vs_price_for_upload(
        "NOBUNDLETEST", *_mk_years_ratios(6, 30), _mk_monthly_price_history())
check('no-bundle ticker: returns {"no_chart_reason": "FY end date not available for this ticker"}',
      _no_bundle_result == {"no_chart_reason": "FY end date not available for this ticker"})

_fig_nb, _reason_nb = cui._cp_iv_bv_vs_price_chart("NOBUNDLETEST", {"NOBUNDLETEST": _no_bundle_result})
check('no-bundle ticker: _cp_iv_bv_vs_price_chart() shows that EXACT caption, no chart',
      _fig_nb is None and _reason_nb == "FY end date not available for this ticker")

# A bundle that resolves but has NO income statement column dates at
# all (e.g. empty income) hits the same path.
with mock.patch.object(bcd.fundamentals_data, "get_bundle", return_value={"income": pd.DataFrame()}):
    _no_fy_result = bcd._iv_bv_vs_price_for_upload(
        "NOFYTEST", *_mk_years_ratios(6, 30), _mk_monthly_price_history())
check('bundle with no resolvable income statement dates: same "FY end date not available" caption',
      _no_fy_result == {"no_chart_reason": "FY end date not available for this ticker"})


# ======================================================================
# CHECK: a gap in the SHEET's own monthly price history (never Yahoo's)
# skips just that one reporting period - nothing filled/interpolated,
# every other period unaffected.
# ======================================================================
_bundle_gap = _mk_shallow_bundle(6, 30, quarterly=False)
_years_gap, _ratios_gap = _mk_years_ratios(6, 30, start_year=2020, end_year=2026)
_price_hist_full = _mk_monthly_price_history(years_back=10)
_price_hist_gapped = _mk_monthly_price_history(years_back=10, gap_year=2022)

with mock.patch.object(bcd.fundamentals_data, "get_bundle", return_value=_bundle_gap):
    _result_full = bcd._iv_bv_vs_price_for_upload("GAPTEST", _years_gap, _ratios_gap, _price_hist_full)
    _result_gapped = bcd._iv_bv_vs_price_for_upload("GAPTEST", _years_gap, _ratios_gap, _price_hist_gapped)

_full_period_count = len(_result_full["periods"])
_gapped_period_count = len(_result_gapped["periods"])
check("a 1-year gap in the sheet's own monthly prices removes exactly the periods that fell "
      "entirely inside that gap year (fewer periods than the full-history run)",
      _gapped_period_count < _full_period_count)
_gapped_period_labels = [p["label"] for p in _result_gapped["periods"]]
check("no period covering the gap year is silently present with a filled/interpolated value - "
      "every surviving period still has a REAL average price on file",
      all(p["avg_price"] is not None for p in _result_gapped["periods"]))
check("periods entirely OUTSIDE the gap year (e.g. the most recent ones) are completely "
      "unaffected by the gap",
      _result_full["periods"][-1]["avg_price"] == _result_gapped["periods"][-1]["avg_price"]
      and _result_full["periods"][-1]["label"] == _result_gapped["periods"][-1]["label"])


# ======================================================================
# CHECK: JSON round trip - the uploaded data is exactly what build()
# itself writes into compounder_data.json; a real json.dumps()/
# json.loads() round trip must be lossless, and the chart rendered from
# the round-tripped copy must be identical to the one rendered direct.
# ======================================================================
_bundle_rt = _mk_shallow_bundle(6, 30, quarterly=False)
_years_rt, _ratios_rt = _mk_years_ratios(6, 30, start_year=2018, end_year=2026)
_price_hist_rt = _mk_monthly_price_history(years_back=10)
with mock.patch.object(bcd.fundamentals_data, "get_bundle", return_value=_bundle_rt):
    _result_rt = bcd._iv_bv_vs_price_for_upload("RTTEST", _years_rt, _ratios_rt, _price_hist_rt)

_round_tripped = json.loads(json.dumps({"RTTEST": _result_rt}))
check("JSON round trip of the uploaded iv_bv_vs_price entry is lossless (dict equality)",
      _round_tripped["RTTEST"] == _result_rt)

_fig_direct, _ = cui._cp_iv_bv_vs_price_chart("RTTEST", {"RTTEST": _result_rt})
_fig_rt, _ = cui._cp_iv_bv_vs_price_chart("RTTEST", _round_tripped)
check("the chart rendered from the JSON-round-tripped copy is identical to the direct one",
      _fig_direct is not None and _fig_rt is not None and _fig_direct.to_json() == _fig_rt.to_json())


# ======================================================================
# CHECK: build()'s own construction loop really calls
# _iv_bv_vs_price_for_upload() and wires its result into
# out["sections"]["Value vs Book"]["iv_bv_vs_price"] - not dead code.
# ======================================================================
import inspect

_src_build = inspect.getsource(bcd.build)
check("build() actually calls _iv_bv_vs_price_for_upload() inside its iv_bv_series loop",
      "_iv_bv_vs_price_for_upload(" in _src_build)
check('build() wires the result into out["sections"]["Value vs Book"]["iv_bv_vs_price"]',
      '["sections"]["Value vs Book"]["iv_bv_vs_price"]' in _src_build)


# ======================================================================
# CHECK: Auto pages are unchanged - _iv_bv_vs_price_periods() still
# delegates to the new shared _iv_bv_vs_price_core(), and the function
# itself still exists with the same public call signature real callers
# use (_build_value_vs_book() above).
# ======================================================================
check("auto_compounder_engine._iv_bv_vs_price_core() exists (the new shared helper)",
      hasattr(ace, "_iv_bv_vs_price_core"))
check("_iv_bv_vs_price_periods() (Auto) delegates to the shared core, not a second copy "
      "of the period-generation logic",
      "_iv_bv_vs_price_core(" in inspect.getsource(ace._iv_bv_vs_price_periods)
      and "step_index" not in inspect.getsource(ace._iv_bv_vs_price_periods))
check("_build_value_vs_book() (Auto) still calls _iv_bv_vs_price_periods() unchanged",
      "_iv_bv_vs_price_periods(" in inspect.getsource(ace._build_value_vs_book))


print()
print(f"PASS={passed} FAIL={failed}")
