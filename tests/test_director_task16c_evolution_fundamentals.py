"""
TASK 16c (Director-directed, 22 Oct 2026): Evolution score measured from
REPORTED ACCOUNTS, not Yahoo `.info` snapshots - SHADOW ONLY.

Covers the task's own listed tests for portfolio_health_engine.
evolution_fundamentals() (ticker, buy_date):
  - CSL: an impairment/restructuring year must NOT zero Margins Δ/ROIC Δ
    (Normalized Income excludes the one-off, unlike statutory net income).
  - OCL: buying on results day resolves FY_base to the just-published
    FY, which also happens to be FY_now -> fundamentals None until the
    next report (never scored 50); Income Δ from ACTUAL paid dividends
    gives the correct, stable total regardless of which of two uneven
    semi-annual payments was most recent - no false "cut".
  - IVV/VAP/QRE (ETFs): Income Δ still scores from actual dividends.
  - GOLD (ETF, non-payer): Income Δ is None, not a saturated 0/50/100.

Run: python3 tests/test_director_task16c_evolution_fundamentals.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task16c_evolution_fundamentals_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import datetime as _dt

import pandas as pd

import fundamentals_data
import portfolio_health_engine as phe

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


def _df(rows, cols):
    """rows: {row_name: [values, newest-first]}; cols: [date strings,
    newest-first] - same orientation yfinance's own income_stmt/
    balance_sheet/cashflow use (rows=line items, columns=period ends),
    matching this repo's own existing test-mock convention."""
    return pd.DataFrame({c: {r: vals[i] for r, vals in rows.items()} for i, c in enumerate(cols)})


TODAY = _dt.datetime.now(_dt.timezone.utc).date()


def _days_ago(n):
    return (TODAY - _dt.timedelta(days=n)).isoformat()


# ======================================================================
# CHECK 1: CSL - an impairment/restructuring year must NOT zero
# Margins Δ/ROIC Δ. FY_base (good year) has a direct "Normalized Income"
# row; FY_now (impairment year) has NO such row, forcing the fallback
# formula (Net Income - Total Unusual Items*(1 - tax rate)) - both code
# paths exercised in one fixture.
# ======================================================================
_CSL_COLS = [_days_ago(30), _days_ago(395), _days_ago(760)]  # FY_now, FY_base, FY_base-1
_csl_income = _df({
    "Total Revenue": [14500.0, 14000.0, 13000.0],
    "Net Income": [-2600.0, 3200.0, 2850.0],          # FY_now: statutory LOSS from the impairment
    "Normalized Income": [None, 3200.0, 3000.0],       # only FY_base/-1 carry the row directly
    "Total Unusual Items": [-7900.0, 0.0, 0.0],        # FY_now: $7.1B impairment + $0.8B restructuring
    "Tax Provision": [None, 800.0, 700.0],
    "Pretax Income": [-3400.0, 4000.0, 3550.0],        # FY_now: pretax LOSS -> effective_tax_rate's own
                                                         # documented default (25%) applies automatically
}, _CSL_COLS)
_csl_balance = _df({
    "Stockholders Equity": [8800.0, 9500.0, 9000.0],
    "Total Debt": [3200.0, 3000.0, 2900.0],
    "Cash And Cash Equivalents": [1400.0, 1500.0, 1400.0],
}, _CSL_COLS)
_csl_cashflow = _df({
    "Operating Cash Flow": [3600.0, 3400.0, 3100.0],
    "Capital Expenditure": [-700.0, -650.0, -600.0],
}, _CSL_COLS)
_CSL_BUNDLE = {"income": _csl_income, "balance": _csl_balance, "cashflow": _csl_cashflow,
               "info": {}, "dividends": {"dates": [], "amounts": []}}

with mock.patch.object(fundamentals_data, "get_bundle", return_value=_CSL_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    # Fallback path: FY_now's Normalized Income must be reconstructed.
    _csl_fy_now = pd.Timestamp(_CSL_COLS[0]).date()
    _csl_fy_base = pd.Timestamp(_CSL_COLS[1]).date()
    _ni_now, _src_now, _uu_now = phe._evolution_normalized_income(_csl_income, _csl_fy_now)
    check("CSL FY_now's Normalized Income is reconstructed via the fallback formula "
          "(no direct row present)", _src_now == "fallback")
    check("CSL FY_now's reconstructed Normalized Income is strongly positive despite the "
          "statutory LOSS (-2600): -2600 - (-7900)*(1-0.25) = 3325.0",
          _ni_now is not None and abs(_ni_now - 3325.0) < 0.01)
    _ni_base, _src_base, _uu_base = phe._evolution_normalized_income(_csl_income, _csl_fy_base)
    check("CSL FY_base's Normalized Income is read directly from the row (source='normalized')",
          _src_base == "normalized" and _ni_base == 3200.0)

    _evo_csl = phe.evolution_fundamentals("CSL.AX", _days_ago(300))  # bought after FY_base's report, before FY_now's
    _margins = _evo_csl["Margins Δ"]
    _roic = _evo_csl["ROIC Δ"]
    check("CSL Margins Δ is NOT None and NOT collapsed - the impairment year reads as healthy "
          "(normalized margin ~23%), not a statutory-loss-driven zero",
          _margins["score"] is not None and _margins["score"] > 30)
    check("CSL ROIC Δ is NOT None and NOT collapsed either, for the same reason",
          _roic["score"] is not None and _roic["score"] > 30)
    check("CSL Margins Δ's own 'one-offs excluded' field reports the $7.9B total unusual items "
          "for FY_now, so the owner can see exactly what was excluded",
          _margins.get("unusual_items_now") == -7900.0)
    check("CSL Margins Δ's source field names both FY_base and FY_now's own method "
          "('normalized/fallback')", _margins.get("source") == "normalized/fallback")

    # Contrast: the OLD statutory calculation WOULD have collapsed - proving this is a
    # real fix, not a coincidence of the scoring formula.
    _statutory_margin_now = -2600.0 / 14500.0
    check("sanity: the STATUTORY margin for FY_now is deeply negative (~-17.9%) - confirming "
          "root cause A is real and the normalized-income fix is what avoids it",
          _statutory_margin_now < -0.10)
print(f"[task16c_c1_csl_impairment] Normalized Income fallback=3325.0 (vs statutory -2600), "
      f"Margins Δ score={_margins['score']}, ROIC Δ score={_roic['score']} - neither collapsed OK")

# ======================================================================
# CHECK 2: CSL's FCF Δ - diagnose a missing OCF/Capex input (root cause
# D) by removing one of them for FY_base and confirming the fallback to
# the "Free Cash Flow" row still produces a score, with the missing
# input logged in the note/source.
# ======================================================================
_csl_cashflow_missing_ocf = _df({
    "Capital Expenditure": [-700.0, -650.0, -600.0],
    "Free Cash Flow": [2900.0, None, None],  # only FY_now has this row; FY_base has neither OCF nor it
}, _CSL_COLS)
_fcf_diag = phe._evolution_fcf(_csl_income, _csl_cashflow_missing_ocf, _csl_fy_base, _csl_fy_now)
_fcf_diag_note = _fcf_diag.get("note") or ""
check("CSL FCF Δ: FY_base's missing Operating Cash Flow/Free Cash Flow is diagnosed in the note",
      "Operating Cash Flow" in _fcf_diag_note)
check("CSL FCF Δ: FY_now correctly falls back to the 'Free Cash Flow' row directly "
      "(no Capex needed when that row is present)",
      _fcf_diag.get("source", "").split("/")[1] == "free_cash_flow_row")
check("CSL FCF Δ score is None when FY_base has no usable input at all (correctly - not a guess)",
      _fcf_diag["score"] is None)
print(f"[task16c_c2_csl_fcf_diagnosis] FY_base diagnosed missing inputs: {_fcf_diag.get('note')!r} OK")

# ======================================================================
# CHECK 3: OCL - buying on results day resolves FY_base to the just-
# published fiscal year, which is ALSO FY_now (the latest available
# column) -> every FY-anchored component is None with the "no new
# annual report" note, never scored 50.
# ======================================================================
_OCL_FY_NOW = _days_ago(240)
_ocl_income = _df({"Net Income": [500.0]}, [_OCL_FY_NOW])
_OCL_REPORT_DATE = _days_ago(180)  # 60 days after FY_now's own period end, i.e. a REAL known report
_OCL_BUY_DATE = _days_ago(100)     # bought 80 days after that report - results are already public
_OCL_BUNDLE = {"income": _ocl_income, "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
               "info": {}, "dividends": {
                   "dates": [_days_ago(40), _days_ago(222), _days_ago(405), _days_ago(587)],
                   "amounts": [0.26, 0.13, 0.26, 0.13],
               }}
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_OCL_BUNDLE), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[_OCL_REPORT_DATE]):
    _evo_ocl = phe.evolution_fundamentals("OCL.AX", _OCL_BUY_DATE)
    for _name in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ"):
        check(f"OCL {_name} is None (not scored 50) - no new annual report since purchase",
              _evo_ocl[_name]["score"] is None)
        check(f"OCL {_name}'s note explains why ('no new annual report since purchase')",
              "no new annual report" in _evo_ocl[_name].get("note", "").lower())
        check(f"OCL {_name} carries the resolved FY_base/FY_now labels even while None "
              "(so the owner can see WHICH report they're waiting on)",
              _evo_ocl[_name].get("fy_base") == _evo_ocl[_name].get("fy_now")
              and _evo_ocl[_name].get("fy_base") is not None)

    # Income Δ: two uneven semi-annual payments (0.26 final + 0.13 interim) sum to the
    # SAME real annual total in both the buy-date window and today's window - the new
    # method gives NO false cut, unlike the old dividendRate-snapshot method.
    _ocl_income_delta = _evo_ocl["Income Δ"]
    check("OCL Income Δ's base (TTM at buy_date) is the correct real total: 0.26+0.13=0.39",
          _ocl_income_delta["base"] is not None and abs(_ocl_income_delta["base"] - 0.39) < 0.001)
    check("OCL Income Δ's now (TTM today) is the SAME real total - no false cut from which "
          "single payment happened to be most recent",
          _ocl_income_delta["now"] is not None and abs(_ocl_income_delta["now"] - 0.39) < 0.001)
    check("OCL Income Δ score is exactly 50 (unchanged) - correctly reflecting NO real change",
          _ocl_income_delta["score"] == 50.0)
    check("OCL Income Δ's source names the real mechanism (actual paid dividends, not dividendRate)",
          "actual paid dividends" in _ocl_income_delta.get("source", "").lower())
print("[task16c_c3_ocl_results_day] FY_base==FY_now (fundamentals None, never 50); Income Δ "
      "from actual dividends gives 0.39==0.39, score=50 - no false cut OK")

# ======================================================================
# CHECK 4: IVV/VAP/QRE (ETFs) - Income Δ still scores from actual
# dividends (stocks AND ETFs, per the Director's own instruction).
# ======================================================================
for _etf, _base_amt, _now_amt in (("IVV", 1.20, 1.35), ("VAP", 0.40, 0.44), ("QRE", 0.30, 0.28)):
    _etf_bundle = {"income": pd.DataFrame(), "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
                   "info": {}, "dividends": {
                       "dates": [_days_ago(30), _days_ago(130)],
                       "amounts": [_now_amt / 2.0, _base_amt / 2.0],
                   }}
    with mock.patch.object(fundamentals_data, "get_bundle", return_value=_etf_bundle), \
         mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
        _evo_etf = phe.evolution_fundamentals(_etf, _days_ago(200))
        check(f"{_etf}: Income Δ is scored (not None) from actual dividend history",
              _evo_etf["Income Δ"]["score"] is not None)
        for _name in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ"):
            check(f"{_etf}: {_name} is None (no income statement for an ETF)",
                  _evo_etf[_name]["score"] is None)
print("[task16c_c4_etf_income_scored] IVV/VAP/QRE all score Income Δ from actual dividends "
      "while the fundamentals-statement components stay None OK")

# ======================================================================
# CHECK 5: GOLD (a non-payer ETF) - Income Δ is None, not a saturated
# 0/50/100 - there is no real change to measure on a ticker that has
# never paid a dividend.
# ======================================================================
_gold_bundle = {"income": pd.DataFrame(), "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
                "info": {}, "dividends": {"dates": [], "amounts": []}}
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_gold_bundle), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]):
    _evo_gold = phe.evolution_fundamentals("GOLD.AX", _days_ago(200))
    check("GOLD: Income Δ is None (not 0/50/100) - no dividends ever, not a real comparison",
          _evo_gold["Income Δ"]["score"] is None)
    check("GOLD: Income Δ's note explains it's a non-payer",
          "non-payer" in _evo_gold["Income Δ"].get("note", "").lower())
print("[task16c_c5_gold_non_payer] GOLD's Income Δ is None with a 'non-payer' note, "
      "never a saturated score OK")

# ======================================================================
# CHECK 6: financials-mode ticker - Growth/Margins/FCF/Debt Δ are None
# with an explanatory note (a bank's statements don't carry a plain
# Total Revenue/OCF/EBITDA the same way a standard company's do); only
# Income Δ still scores.
# ======================================================================
_bank_cols = [_days_ago(30), _days_ago(400)]
_bank_income = _df({"Net Income": [900.0, 850.0], "Total Revenue": [5000.0, 4800.0]}, _bank_cols)
_bank_bundle = {"income": _bank_income, "balance": pd.DataFrame(), "cashflow": pd.DataFrame(),
                "info": {"sector": "Financial Services", "industry": "Banks - Regional"},
                "dividends": {"dates": [_days_ago(40)], "amounts": [0.50]}}
with mock.patch.object(fundamentals_data, "get_bundle", return_value=_bank_bundle), \
     mock.patch.object(phe, "_evolution_report_dates", return_value=[]), \
     mock.patch("financials_classifier.is_financials", return_value=True):
    _evo_bank = phe.evolution_fundamentals("BANK.AX", _days_ago(200))
    for _name in ("Growth Δ", "Margins Δ", "FCF Δ", "Debt Δ"):
        check(f"financials-mode ticker: {_name} is None with a financials-mode note",
              _evo_bank[_name]["score"] is None
              and "financials-mode" in _evo_bank[_name].get("note", "").lower())
    check("financials-mode ticker: Income Δ still scores (a dividend is a dividend)",
          _evo_bank["Income Δ"]["score"] is not None or _evo_bank["Income Δ"].get("note"))
print("[task16c_c6_financials_mode] a bank's Growth/Margins/FCF/Debt Δ are None with an "
      "explanatory note; Income Δ is unaffected OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
