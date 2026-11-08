"""
TASK 32 (Director-directed, owner request, 5 Nov 2026): a "through-the-
cycle" Quality score - SHADOW MODE / TEST ONLY, owner approval before
any go-live.

Covers the task's own listed tests:
  - CSL-shaped fixture (impairment year) -> v2 materially higher than v1.
  - A genuinely deteriorating business (3 weak years, no one-offs)
    stays low under v2 too.
  - financials-mode / ETF / fund handling unchanged (every input falls
    back to today's value, v2 == v1 for the same ticker).
  - Per-input fallback: a bundle missing ONE statement (balance sheet)
    only falls back the fields that actually need it.
  - top100_engine's additive quality/quality_v2/value_score_v2 fields
    never change "value_score" - select_top100_pool()'s own live
    selection is byte-identical whether or not scan rows carry a
    "Quality V2" field.
  - simulate_pool_v2()/quality_v2_movers(): the owner's "Quality v2
    preview" diff/movers logic, against a constructed scenario.
  - nightly_scan._attach_quality_v2(): wiring only, never touches the
    live "Quality"/"Quality Default" fields.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task32_quality_v2_shadow.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task32_quality_v2_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_BACKFILL_LIVE", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

import pandas as pd

import auto_quality_engine
import deep_dive_engine
import fundamentals_data
import nightly_scan
import quality_v2_engine as qv2
import scan_store
import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

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
# Fixture builders.
# ======================================================================
def _bundle(income_rows=None, balance_rows=None, cashflow_rows=None, info=None):
    def _df(rows):
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    return {
        "income": _df(income_rows), "balance": _df(balance_rows),
        "cashflow": _df(cashflow_rows), "info": info or {},
    }


_CSL_INCOME = {
    "2026-06-30": {"Total Revenue": 15000.0, "Net Income": -300.0, "Pretax Income": -200.0,
                   "Tax Provision": 50.0, "Total Unusual Items": -3500.0},
    "2025-06-30": {"Total Revenue": 14200.0, "Net Income": 3000.0, "Pretax Income": 4000.0,
                   "Tax Provision": 1000.0, "Total Unusual Items": 0.0},
    "2024-06-30": {"Total Revenue": 13500.0, "Net Income": 2850.0, "Pretax Income": 3800.0,
                   "Tax Provision": 950.0, "Total Unusual Items": 0.0},
}
_CSL_BALANCE = {
    "2026-06-30": {"Stockholders Equity": 16000.0, "Total Debt": 9000.0},
    "2025-06-30": {"Stockholders Equity": 17500.0, "Total Debt": 8500.0},
    "2024-06-30": {"Stockholders Equity": 16800.0, "Total Debt": 8200.0},
}
_CSL_CASHFLOW = {
    "2026-06-30": {"Operating Cash Flow": 3200.0, "Capital Expenditure": -900.0},
    "2025-06-30": {"Operating Cash Flow": 3000.0, "Capital Expenditure": -850.0},
    "2024-06-30": {"Operating Cash Flow": 2800.0, "Capital Expenditure": -800.0},
}
_CSL_INFO = {
    "returnOnEquity": 0.067, "profitMargins": 0.091, "revenueGrowth": 0.08,
    "earningsGrowth": -0.801, "debtToEquity": 55.0, "freeCashflow": 2300.0,
    "netIncomeToCommon": -300.0, "marketCap": 100000.0, "priceToBook": 6.0,
    "totalDebt": 9000.0,
}


# ======================================================================
# CHECK 1: CSL-shaped fixture (a one-off impairment year dragging v1's
# point-in-time ROE/margin/earnings-growth down) -> v2 is materially
# higher than v1, driven by the one-off-excluded Normalized Income.
# ======================================================================
_csl_bundle = _bundle(_CSL_INCOME, _CSL_BALANCE, _CSL_CASHFLOW, _CSL_INFO)
_v1_csl, _v1_csl_defaulted = auto_quality_engine.get_quality_score(None, info=_CSL_INFO)
_v2_csl, _meta_csl = qv2.get_quality_score_v2("CSL.AX", _csl_bundle)

check("CSL fixture: v1 (point-in-time, impairment year) is well below the "
      "Director's own hand-check range (30-55 estimate implied a Quality near "
      "~51-53, far below a healthy compounder)", _v1_csl < 60)
check("CSL fixture: v2 (through-the-cycle, one-off excluded) is MATERIALLY "
      "higher than v1 - at least 10 points", _v2_csl >= _v1_csl + 10)
check("CSL fixture: v2 used all 3 fiscal years, no per-input fallback needed "
      "(every statement present)", _meta_csl["fiscal_years_used"] == ["2026-06-30", "2025-06-30", "2024-06-30"]
      and _meta_csl["fallback_fields"] == [])
check("CSL fixture: v2's net margin (one-off excluded) is positive and "
      "healthy, unlike v1's raw -2% implied by reported net income",
      _meta_csl["net_margin"] > 0.10)
print("[task32_c1_csl_impairment_fixture] CSL-shaped impairment year: v2 "
      f"({_v2_csl}) materially higher than v1 ({_v1_csl}) OK")


# ======================================================================
# CHECK 2: a genuinely deteriorating business (3 real weak years, no
# one-off items at all) stays low under v2 too - the one-off exclusion
# must never manufacture quality that isn't there.
# ======================================================================
_weak_income = {
    "2026-06-30": {"Total Revenue": 9000.0, "Net Income": -400.0, "Pretax Income": -500.0,
                   "Tax Provision": 0.0, "Total Unusual Items": 0.0},
    "2025-06-30": {"Total Revenue": 10500.0, "Net Income": 100.0, "Pretax Income": 150.0,
                   "Tax Provision": 50.0, "Total Unusual Items": 0.0},
    "2024-06-30": {"Total Revenue": 12000.0, "Net Income": 600.0, "Pretax Income": 800.0,
                   "Tax Provision": 200.0, "Total Unusual Items": 0.0},
}
_weak_balance = {
    "2026-06-30": {"Stockholders Equity": 5000.0, "Total Debt": 6000.0},
    "2025-06-30": {"Stockholders Equity": 5400.0, "Total Debt": 5800.0},
    "2024-06-30": {"Stockholders Equity": 5800.0, "Total Debt": 5600.0},
}
_weak_cashflow = {
    "2026-06-30": {"Operating Cash Flow": -200.0, "Capital Expenditure": -300.0},
    "2025-06-30": {"Operating Cash Flow": 100.0, "Capital Expenditure": -250.0},
    "2024-06-30": {"Operating Cash Flow": 500.0, "Capital Expenditure": -300.0},
}
_weak_info = {
    "returnOnEquity": -0.08, "profitMargins": -0.044, "revenueGrowth": -0.14,
    "earningsGrowth": -5.0, "debtToEquity": 120.0, "freeCashflow": -500.0,
    "netIncomeToCommon": -400.0, "marketCap": 2000.0, "priceToBook": 1.5, "totalDebt": 6000.0,
}
_weak_bundle = _bundle(_weak_income, _weak_balance, _weak_cashflow, _weak_info)
_v2_weak, _meta_weak = qv2.get_quality_score_v2("WEAK.AX", _weak_bundle)
check("genuinely weak business: v2 stays low (<=40), one-off exclusion "
      "doesn't manufacture quality that isn't there", _v2_weak <= 40)
check("genuinely weak business: the profitability gate fired (negative "
      "median FCF) - score capped at PROFIT_GATE_CAP",
      _v2_weak <= qv2.PROFIT_GATE_CAP and _meta_weak["fcf_positive"] is False)
print(f"[task32_c2_genuinely_weak_business] 3 real weak years (no one-offs) -> "
      f"v2 stays low ({_v2_weak}) OK")


# ======================================================================
# CHECK 3: financials-mode handling unchanged - every input falls back
# to today's live value (portfolio_health_engine.py's Evolution engine
# already excludes financials-mode tickers from Growth/Margins/FCF/Debt
# for the exact same reason - "Total Revenue"/"OCF"/"Capex" don't mean
# the same thing for a bank). v2 ends up IDENTICAL to a plain v1 run on
# the same info, for a financials-mode ticker, even with full
# statements present.
# ======================================================================
_bank_info = dict(_CSL_INFO)  # reuse as a stand-in "info" - content doesn't matter here
_v1_bank, _ = auto_quality_engine.get_quality_score(None, info=_bank_info)
with mock.patch.object(qv2.financials_classifier, "is_financials", return_value=True):
    _v2_bank, _meta_bank = qv2.get_quality_score_v2("BANK.AX", _csl_bundle, info=_bank_info)
check("financials-mode ticker: flagged financials_mode=True",
      _meta_bank["financials_mode"] is True)
check("financials-mode ticker: every one of the 7 inputs fell back to today's value",
      set(_meta_bank["fallback_fields"]) == set(qv2._INPUT_FIELDS))
check("financials-mode ticker: v2 score == v1 score for the same info - "
      "statement-based computation is excluded entirely, not partially",
      _v2_bank == _v1_bank)
print("[task32_c3_financials_mode_unchanged] financials-mode ticker falls back "
      "entirely, v2 == v1 for the same info OK")


# ======================================================================
# CHECK 4: ETF/fund handling unchanged - no income statement at all ->
# every input falls back, defaulted shape matches auto_quality_engine's
# own "defaulted" case, v2 == v1.
# ======================================================================
_etf_info = {"returnOnEquity": None, "profitMargins": None}
_etf_bundle = _bundle(None, None, None, _etf_info)
_v1_etf, _v1_etf_defaulted = auto_quality_engine.get_quality_score(None, info=_etf_info)
_v2_etf, _meta_etf = qv2.get_quality_score_v2("IVV", _etf_bundle, info=_etf_info)
check("ETF/fund: no usable statements -> every input falls back (defaulted)",
      _meta_etf["defaulted"] is True and set(_meta_etf["fallback_fields"]) == set(qv2._INPUT_FIELDS))
check("ETF/fund: v2 == v1 for the same info - nothing is fabricated for a "
      "fund with no statements", _v2_etf == _v1_etf)
print("[task32_c4_etf_fund_unchanged] ETF/fund with no statements falls back "
      "entirely, v2 == v1 OK")


# ======================================================================
# CHECK 5: per-input fallback - a bundle with income + cashflow but NO
# balance sheet falls back ONLY the fields that need equity (ROE, ROIC,
# debt/equity), while margin/growth/FCF still compute from statements.
# ======================================================================
_no_balance_bundle = _bundle(_CSL_INCOME, None, _CSL_CASHFLOW, _CSL_INFO)
_v2_nb, _meta_nb = qv2.get_quality_score_v2("CSL.AX", _no_balance_bundle)
check("per-input fallback: ROE/ROIC/debt-to-equity fall back (no balance sheet)",
      set(_meta_nb["fallback_fields"]) == {"roe", "roic", "debt_to_equity"})
check("per-input fallback: net margin/growth/FCF still computed from statements, "
      "NOT fallen back", _meta_nb["net_margin"] is not None and _meta_nb["net_margin"] > 0.10)
print("[task32_c5_per_input_fallback] missing balance sheet only falls back "
      "the equity-dependent fields OK")


# ======================================================================
# CHECK 6: top100_engine's additive fields never change "value_score" -
# _build_best_by_ticker() carries "quality"/"quality_v2"/"value_score_v2"
# alongside the untouched "value_score".
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _scan_row(ticker, quality=60.0, quality_v2=None, mos=20.0, psychology=5.0, discovery=30.0):
    row = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Price": 100.0,
        "Intrinsic Value": 120.0, "MOS %": mos, "Long Score": 60.0,
        "Quality": quality, "Psychology": psychology, "Discovery (lite)": discovery,
        "Moat": 50.0, "Growth Used": 0.05, "FCF Source": "reported",
        "Dividend Yield %": 2.0, "Is Fund": False, "price_unit_suspect": False,
        "Trading Status": None, "DCF Unreliable": False,
        # Avoids top100_engine._fill_missing_sectors()'s own bounded
        # one-shot yfinance fallback (a real, slow, retried network
        # call per ticker with no Sector) - this sandbox has no
        # outbound network access, so every synthetic fixture row here
        # must carry its own Sector up front, same as every other
        # select_top100_pool()-exercising test in this suite.
        "Sector": "Industrials",
    }
    if quality_v2 is not None:
        row["Quality V2"] = quality_v2
        row["Quality V2 Meta"] = {"fiscal_years_used": [], "fallback_fields": [], "defaulted": False}
    return row


scan_store.save_scan("S&P 500", [_scan_row("AAA6", quality=60.0, quality_v2=80.0)],
                      source_label="test fixture")
_best6, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
_row6 = _best6["AAA6"]
check("additive field: 'quality' == the live Quality (60.0)", _row6["quality"] == 60.0)
check("additive field: 'quality_v2' == the shadow Quality V2 (80.0)", _row6["quality_v2"] == 80.0)
check("additive field: 'value_score_v2' is computed with quality_v2 in place of "
      "quality, same MOS/Psychology/Discovery, discovery_measured=False",
      _row6["value_score_v2"] == te.ranking_engine.calculate_long_score(
          80.0, 20.0, 5.0, 30.0, discovery_measured=False))
check("additive field: 'value_score' (live) is UNCHANGED - still built from the "
      "live Quality (60.0), never quality_v2",
      _row6["value_score"] == te.ranking_engine.calculate_long_score(
          60.0, 20.0, 5.0, 30.0, discovery_measured=False))
print("[task32_c6_additive_fields] quality/quality_v2/value_score_v2 are additive, "
      "'value_score' itself is untouched OK")


# ======================================================================
# CHECK 7: _recompute_value_score_v2() returns None when a row has no
# "Quality V2" at all (an older scan file, or a ticker the shadow
# computation hasn't reached).
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("S&P 500", [_scan_row("BBB6", quality=55.0, quality_v2=None)],
                      source_label="test fixture")
_best7, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
# TASK 40 (Director-directed, 8 Nov 2026, review finding): no Quality
# V2 -> "quality_v2" itself stays None (never fabricated), but
# "value_score_v2" now FALLS BACK to the live Quality (matching
# "value_score" exactly) instead of being None, and "quality_v2_
# missing" names why - see _recompute_value_score_v2()'s own
# docstring for the full reasoning.
check("no Quality V2 on this row -> quality_v2 stays None, quality_v2_missing is True, "
      "value_score_v2 falls back to the live value_score (never None)",
      _best7["BBB6"]["quality_v2"] is None and _best7["BBB6"]["quality_v2_missing"] is True
      and _best7["BBB6"]["value_score_v2"] == _best7["BBB6"]["value_score"])
print("[task32_c7_no_quality_v2_falls_back] a row with no Quality V2 falls back to the "
      "live Quality for value_score_v2 (flagged quality_v2_missing), never shows as "
      "dropped/blank in a direct read OK")


# ======================================================================
# CHECK 8: live selection is byte-identical whether or not scan rows
# carry a "Quality V2" field - the Director's own explicit test
# requirement.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_fixture_rows_no_v2 = [_scan_row(f"SEL{i}", quality=50.0 + i, quality_v2=None) for i in range(10)]
scan_store.save_scan("S&P 500", _fixture_rows_no_v2, source_label="test fixture")
_pool_without_v2 = te.select_top100_pool(log=lambda *a, **k: None)

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_fixture_rows_with_v2 = [_scan_row(f"SEL{i}", quality=50.0 + i, quality_v2=90.0 - i)
                         for i in range(10)]
scan_store.save_scan("S&P 500", _fixture_rows_with_v2, source_label="test fixture")
_pool_with_v2 = te.select_top100_pool(log=lambda *a, **k: None)


_NON_LIVE_FIELDS = (
    # Real wall-clock save_scan() timestamps - genuinely different
    # between these two separate scan_store.save_scan() calls,
    # regardless of "Quality V2".
    "generated_at", "source_universe_generated_at",
    # TASK 32's own additive shadow fields - these are SUPPOSED to
    # differ between the two scenarios (that's the entire point of
    # carrying quality_v2 data at all). Excluded here on purpose, so
    # this check proves what the Director's own instruction actually
    # asks for: the LIVE selection (ticker order + every live field,
    # above all "value_score" itself) is untouched - never that the
    # shadow fields themselves vanish.
    "quality", "quality_v2", "quality_v2_meta", "value_score_v2",
    # TASK 40 (Director-directed, 8 Nov 2026, review finding): same
    # additive-shadow-field status - "quality_v2_missing" is SUPPOSED
    # to differ between these two fixtures too (one has Quality V2,
    # one doesn't).
    "quality_v2_missing",
    # TASK 35 (Director-directed, 6 Nov 2026): the moat-blend shadow
    # fields are additive the exact same way - "value_score_v2_moat"
    # in particular depends on "Quality V2" (None here, a real number
    # in the "with v2" fixture), so it's SUPPOSED to differ between
    # these two scenarios too, same reasoning as "value_score_v2"
    # itself just above.
    "moat", "value_score_moat", "value_score_v2_moat",
)


def _strip_non_live_fields(pool):
    return [{k: v for k, v in row.items() if k not in _NON_LIVE_FIELDS} for row in pool]


check("select_top100_pool()'s own LIVE fields (ticker order, value_score, every other "
      "live field) are BYTE-IDENTICAL whether scan rows carry a 'Quality V2' shadow "
      "field or not - the live selection never reads it, only the additive shadow "
      "fields themselves (which are SUPPOSED to differ) change",
      _strip_non_live_fields(_pool_without_v2) == _strip_non_live_fields(_pool_with_v2))
print("[task32_c8_live_selection_byte_identical] select_top100_pool() is unaffected "
      "by the presence of 'Quality V2' on scan rows OK")


# ======================================================================
# CHECK 9: simulate_pool_v2() - a ticker with a much higher quality_v2
# enters the simulated pool; one with a much lower quality_v2 drops
# out; the ASX extension top-up responds to v2 scores too.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
# 199 plain members (comfortably inside the pool either way) + 2 swing
# tickers: HIGH_V2 (low live quality, high quality_v2 -> should ENTER
# under v2) and LOW_V2 (high live quality, low quality_v2 -> should
# DROP OUT under v2). Also two AU-only tickers to prove the extension
# responds to v2 too.
_sim_rows = [_scan_row(f"PAD{i}", quality=50.0, quality_v2=50.0) for i in range(199)]
_sim_rows.append(_scan_row("HIGH_V2", quality=5.0, quality_v2=95.0))
_sim_rows.append(_scan_row("LOW_V2", quality=95.0, quality_v2=5.0))
_sim_rows.append(_scan_row("AU_HIGH_V2.AX", quality=5.0, quality_v2=95.0))
_sim_rows.append(_scan_row("AU_LOW_V2.AX", quality=95.0, quality_v2=5.0))
scan_store.save_scan("S&P 500", _sim_rows, source_label="test fixture")

_sim = te.simulate_pool_v2(log=lambda *a, **k: None)
check("simulate_pool_v2: HIGH_V2 (low live quality, high quality_v2) enters the "
      "simulated v2 pool", "HIGH_V2" in _sim["pool_v2_tickers"])
check("simulate_pool_v2: LOW_V2 (high live quality, low quality_v2) is NOT in the "
      "simulated v2 pool", "LOW_V2" not in _sim["pool_v2_tickers"])
check("simulate_pool_v2: AU_HIGH_V2.AX enters the simulated v2 ASX extension "
      "(or the main v2 pool) - the AU top-up responds to v2 scores too",
      "AU_HIGH_V2.AX" in _sim["pool_v2_tickers"] | _sim["extension_v2_tickers"])
check("simulate_pool_v2: cutoff_v2 is a real number", isinstance(_sim["cutoff_v2"], float))
check("simulate_pool_v2: backfill_live reports False (TOP200_BACKFILL_LIVE unset)",
      _sim["backfill_live"] is False)
print("[task32_c9_simulate_pool_v2] simulated v2 pool correctly swaps a low-v1/"
      "high-v2 ticker in and a high-v1/low-v2 ticker out, extension included OK")

# select_top100_pool() must still be unaffected by having just run
# simulate_pool_v2() (read-only).
_live_before_sim = te.select_top100_pool(log=lambda *a, **k: None)
te.simulate_pool_v2(log=lambda *a, **k: None)
_live_after_sim = te.select_top100_pool(log=lambda *a, **k: None)
check("simulate_pool_v2() is read-only - select_top100_pool()'s own output is "
      "byte-identical before and after running it",
      _live_before_sim == _live_after_sim)
print("[task32_c9b_simulate_pool_v2_read_only] simulate_pool_v2() never perturbs "
      "the live selection OK")


# ======================================================================
# CHECK 10: quality_v2_movers() - delta/threshold/risers/fallers.
# ======================================================================
_rows_v2_for_movers = {
    "UP10": {"quality": 50, "quality_v2": 61, "quality_v2_meta": {}},   # +11, over threshold
    "UP_SMALL": {"quality": 50, "quality_v2": 55, "quality_v2_meta": {}},  # +5, under threshold
    "DOWN10": {"quality": 60, "quality_v2": 48, "quality_v2_meta": {}},  # -12, over threshold
    "NO_V2": {"quality": 50, "quality_v2": None, "quality_v2_meta": {}},  # excluded (no v2)
}
with mock.patch.object(te, "_quality_v2_main_driver", return_value=None):
    _movers = te.quality_v2_movers(_rows_v2_for_movers, min_delta=10, top_n=20,
                                    log=lambda *a, **k: None)
check("quality_v2_movers: moved_over_threshold counts exactly UP10 and DOWN10 (|delta|>10)",
      _movers["moved_over_threshold"] == 2)
check("quality_v2_movers: risers list contains UP10 and UP_SMALL, sorted descending by delta",
      [m["ticker"] for m in _movers["risers"]] == ["UP10", "UP_SMALL"])
check("quality_v2_movers: fallers list contains exactly DOWN10",
      [m["ticker"] for m in _movers["fallers"]] == ["DOWN10"])
check("quality_v2_movers: NO_V2 (no quality_v2 at all) never appears in either list",
      all(m["ticker"] != "NO_V2" for m in _movers["risers"] + _movers["fallers"]))
print("[task32_c10_quality_v2_movers] delta/threshold/risers/fallers computed "
      "correctly, a ticker with no v2 score is excluded OK")


# ======================================================================
# CHECK 11: _quality_v2_main_driver() picks the single term with the
# largest v2-vs-v1 weighted-contribution swing.
# ======================================================================
_driver_meta = {
    "roic": 0.30, "roe": 0.30, "net_margin": 0.30,
    "revenue_growth": 0.08, "earnings_growth": 0.08,
    "fcf_positive": True, "debt_to_equity": 40.0,
}
_driver_info = {
    # v1 ROE is 0.0 (a big gap from v2's 0.30 - the dominant swing by
    # construction); every other v1 term is set to equal (or very
    # nearly equal) its v2 counterpart - including ROIC, which needs
    # marketCap/priceToBook/totalDebt/netIncomeToCommon set so v1's
    # OWN derived ROIC (300/(1000/2 + 500) = 0.30) lands on the exact
    # same value as v2's, proving this isn't just "whichever term has
    # the biggest weight" but a genuine v1-vs-v2 comparison.
    "returnOnEquity": 0.0, "profitMargins": 0.29, "revenueGrowth": 0.08,
    "earningsGrowth": 0.08, "debtToEquity": 40.0, "freeCashflow": 100.0,
    "marketCap": 1000.0, "priceToBook": 2.0, "totalDebt": 500.0,
    "netIncomeToCommon": 300.0,
}
with mock.patch.object(te.fundamentals_data, "get_bundle", return_value={"info": _driver_info}):
    _driver = te._quality_v2_main_driver(
        "DRIVER_TEST", {"quality_v2_meta": _driver_meta}, log=lambda *a, **k: None)
check("main driver: ROE (the term with the largest v1-vs-v2 swing by construction) "
      "is correctly identified", _driver == "ROE")
print(f"[task32_c11_main_driver] main-driver lookup correctly picks the dominant "
      f"term ({_driver!r}) OK")


# ======================================================================
# CHECK 12: nightly_scan._attach_quality_v2() - wiring only, never
# touches the live "Quality"/"Quality Default" fields, never raises on
# a missing/failed bundle.
# ======================================================================
_row12 = {"Quality": 55, "Quality Default": False}
with mock.patch.object(nightly_scan.fundamentals_data, "get_bundle", return_value=_csl_bundle), \
     mock.patch.object(nightly_scan.quality_v2_engine, "get_quality_score_v2",
                        return_value=(77, {"fiscal_years_used": [], "fallback_fields": []})):
    nightly_scan._attach_quality_v2(_row12, "CSL.AX", log=lambda *a, **k: None)
check("_attach_quality_v2: 'Quality V2' set from the engine's own return value",
      _row12["Quality V2"] == 77)
check("_attach_quality_v2: live 'Quality'/'Quality Default' completely untouched",
      _row12["Quality"] == 55 and _row12["Quality Default"] is False)

_row12b = {"Quality": 55, "Quality Default": False}
with mock.patch.object(nightly_scan.fundamentals_data, "get_bundle", return_value=None):
    nightly_scan._attach_quality_v2(_row12b, "NOBUNDLE", log=lambda *a, **k: None)
check("_attach_quality_v2: a missing bundle leaves 'Quality V2' absent, never raises, "
      "never touches 'Quality'", "Quality V2" not in _row12b and _row12b["Quality"] == 55)
print("[task32_c12_attach_quality_v2_wiring] nightly_scan's attach function is purely "
      "additive and fails safe OK")


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
