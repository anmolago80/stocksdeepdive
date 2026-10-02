"""
Financials-mode DCF (owner-directed, 2 Oct 2026, VERSION A). Live
symptom: KNSL (an insurer) OCF ~US$1.0B vs net income ~US$412M - a
bank/insurer's operating cash flow includes float/deposit flows that
aren't shareholder cash, so the OCF-based DCF gives ~US$930/share while
PE Forward/PE Trailing/Rational Compounder cluster US$340-410. Every
financial's Scanner MOS/Value Score was inflated by this; a large share
of the 486 DCF-unreliable rows from the 2 Oct incident were financials.

Fix: financials_classifier.is_financials() (shared with moat_engine.py,
so the two valuation paths can never put the same ticker on different
sides of the line) plus fcf_valuation_engine.py's new
_financials_base_and_series() (net income substitutes for OCF-minus-
capex, reusing Task 10's outlier-median swap and Step 4's distorted-
year mechanism unchanged - just fed the net-income series) and the new
normalized_base_and_series() dispatcher - see that function's own
docstring for the exact algorithm and fallback.

Run: python3 tests/test_financials_mode_dcf.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve
import financials_classifier as fc


def _mk_cashflow_df(ocf, capex):
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


def _mk_income_df(net_income):
    cols = {}
    for i, v in enumerate(net_income):
        cols[f"202{6 - i}-06-30"] = [v]
    return pd.DataFrame(cols, index=["Net Income"])


# ======================================================================
# CHECK 1: KNSL-shaped fixture - price $334.5, 23.0M shares, net income
# $412M, discount 9.4%, perpetual 2%, growth 8.8% (market-cap-derived
# fade end rate lands at 4.66%, matching the task's own ~4.7% estimate -
# KNSL's $7.69B market cap sits between the $4.47B/5% and $22.4B/4%
# anchors in GROWTH_END_RATE_ANCHORS_USD). OCF is given a WILDLY
# different, swinging shape (the float/premium-flow distortion a real
# insurer shows) to prove the DCF tracks net income, not OCF.
# ======================================================================
KNSL_PRICE = 334.5
KNSL_SHARES = 23.0e6
KNSL_NET_INCOME = [412.0e6, 395.0e6, 360.0e6, 330.0e6, 300.0e6]
KNSL_OCF = [1000.0e6, 850.0e6, 1100.0e6, 700.0e6, 950.0e6]
KNSL_CAPEX = [-5.0e6] * 5
KNSL_CF = _mk_cashflow_df(KNSL_OCF, KNSL_CAPEX)
KNSL_INCOME = _mk_income_df(KNSL_NET_INCOME)
KNSL_INFO = {
    "sector": "Financial Services",
    "industry": "Insurance - Specialty",
    "sharesOutstanding": KNSL_SHARES,
    "currency": "USD",
    "financialCurrency": "USD",
    "currentPrice": KNSL_PRICE,
    "marketCap": KNSL_PRICE * KNSL_SHARES,
}

assert fc.is_financials(KNSL_INFO) is True

_base, _series, _src, _bn, _cb, _oneoff = fve.normalized_base_and_series(
    KNSL_CF, info=KNSL_INFO, income_df=KNSL_INCOME,
)
assert _src == "net_income_financials", _src
assert abs(_base - 412.0e6) < 1.0, _base  # tracks the net-income base, not any OCF figure
assert _oneoff["is_financials_mode"] is True, _oneoff
print(f"[knsl_base_is_net_income] financials-mode base={_base/1e6:.1f}M (source={_src!r}), "
      f"ignoring OCF's own swinging {KNSL_OCF} shape entirely OK")

_iv, _g, _meta = fve.dcf_intrinsic_value(
    "KNSL", info=KNSL_INFO, cashflow_df=KNSL_CF, currency="USD",
    discount_rate=0.094, perpetual_rate=0.02, growth_rate=0.088, income_df=KNSL_INCOME,
)
assert _meta.get("fcf_source") == "net_income_financials", _meta.get("fcf_source")
assert _meta.get("is_financials_mode") is True, _meta.get("is_financials_mode")
assert abs(_meta.get("growth_end_rate_used") - 0.0466) < 1e-4, _meta.get("growth_end_rate_used")
# Real engine output (recomputed, not hand-derived) sits a few percent
# below the owner's own back-of-envelope "~$385-390" estimate, same
# region and same order-of-magnitude fix as the live KNSL symptom.
assert 350.0 <= _iv <= 420.0, _iv

# The SAME cash-flow data run through the standard (non-financials) OCF
# path, to show the direction and size of the fix this task targets.
_std_info = dict(KNSL_INFO)
_std_info["sector"] = "Industrials"
_std_info["industry"] = "Diversified Industrials"
_iv_ocf, _g_ocf, _meta_ocf = fve.dcf_intrinsic_value(
    "KNSL_AS_INDUSTRIAL", info=_std_info, cashflow_df=KNSL_CF, currency="USD",
    discount_rate=0.094, perpetual_rate=0.02, growth_rate=0.088, income_df=KNSL_INCOME,
)
assert _meta_ocf.get("fcf_source") == "ocf-normcapex", _meta_ocf.get("fcf_source")
assert _iv_ocf > 2.0 * _iv, (_iv_ocf, _iv)  # OCF path inflates to roughly double+ - the KNSL symptom
print(f"[knsl_end_to_end_iv] financials-mode DCF = {_iv:.2f}/share (fcf_source={_meta.get('fcf_source')!r}), "
      f"vs the SAME cash-flow data run as a standard OCF ticker = {_iv_ocf:.2f}/share - "
      f"financials mode brings KNSL's DCF down into the PE-cluster region instead of the "
      f"OCF-inflated ~2x+ figure, matching the real KNSL symptom's direction OK")


# ======================================================================
# CHECK 2: bank-shaped fixture - OCF swings positive AND negative
# year to year (deposit/loan-book timing), net income dead flat ->
# base/series track net income only.
# ======================================================================
BANK_OCF = [500.0e6, -200.0e6, 650.0e6, -100.0e6, 400.0e6]
BANK_CAPEX = [-10.0e6] * 5
BANK_CF = _mk_cashflow_df(BANK_OCF, BANK_CAPEX)
BANK_NET_INCOME = [250.0e6, 248.0e6, 252.0e6, 249.0e6, 251.0e6]
BANK_INCOME = _mk_income_df(BANK_NET_INCOME)
BANK_INFO = {
    "sector": "Financial Services", "industry": "Banks - Regional",
    "sharesOutstanding": 100.0e6, "currency": "USD", "financialCurrency": "USD",
    "currentPrice": 40.0, "marketCap": 40.0 * 100.0e6,
}

_bank_base, _bank_series, _bank_src, _bank_bn, _bank_cb, _bank_oneoff = fve.normalized_base_and_series(
    BANK_CF, info=BANK_INFO, income_df=BANK_INCOME,
)
assert _bank_src == "net_income_financials", _bank_src
assert abs(_bank_base - 250.0e6) < 5.0e6, _bank_base  # near-flat net-income median, untouched by OCF's sign flips
print(f"[bank_tracks_net_income_not_ocf] OCF swings {BANK_OCF} (even negative years), net income "
      f"stays ~flat -> base={_bank_base/1e6:.1f}M tracks net income, source={_bank_src!r} OK")


# ======================================================================
# CHECK 3: REIT fixture - the 2 Oct 2026 exclusion. Yahoo often files a
# REIT's sector as "Financial Services" too (the real-world quirk this
# exclusion exists for) - industry containing "REIT" must still take
# the standard OCF path, not financials mode.
# ======================================================================
REIT_OCF = [300.0e6, 280.0e6, 260.0e6, 240.0e6, 220.0e6]
REIT_CAPEX = [-150.0e6] * 5
REIT_CF = _mk_cashflow_df(REIT_OCF, REIT_CAPEX)
REIT_NET_INCOME = [100.0e6, 90.0e6, 80.0e6, 70.0e6, 60.0e6]
REIT_INCOME = _mk_income_df(REIT_NET_INCOME)
REIT_INFO = {
    "sector": "Financial Services", "industry": "REIT - Retail",
    "sharesOutstanding": 50.0e6, "currency": "USD", "financialCurrency": "USD",
    "currentPrice": 20.0, "marketCap": 20.0 * 50.0e6,
}

assert fc.is_financials(REIT_INFO) is False
_reit_base, _reit_series, _reit_src, _reit_bn, _reit_cb, _reit_oneoff = fve.normalized_base_and_series(
    REIT_CF, info=REIT_INFO, income_df=REIT_INCOME,
)
assert _reit_src == "ocf-normcapex", _reit_src
assert _reit_oneoff.get("is_financials_mode", False) is False, _reit_oneoff
print(f"[reit_exclusion_keeps_ocf_path] sector='Financial Services' but industry='REIT - Retail' -> "
      f"is_financials()=False, base source stays {_reit_src!r} OK")


# ======================================================================
# CHECK 4: fewer than 2 usable positive net-income points -> OCF
# fallback, tagged source="ocf_fallback_financials" with
# is_financials_mode=True (financials_classifier still said yes - the
# fallback is a data-availability escape hatch, not a reclassification).
# ======================================================================
THIN_OCF = [80.0e6, 75.0e6, 90.0e6, 70.0e6, 85.0e6]
THIN_CAPEX = [-5.0e6] * 5
THIN_CF = _mk_cashflow_df(THIN_OCF, THIN_CAPEX)
THIN_NET_INCOME = [-10.0e6, 15.0e6, -5.0e6, -20.0e6, -8.0e6]  # only 1 positive point
THIN_INCOME = _mk_income_df(THIN_NET_INCOME)
THIN_INFO = {
    "sector": "Financial Services", "industry": "Insurance - Life",
    "sharesOutstanding": 30.0e6, "currency": "USD", "financialCurrency": "USD",
    "currentPrice": 15.0, "marketCap": 15.0 * 30.0e6,
}

assert fve._financials_base_and_series(THIN_INCOME) is None
_thin_base, _thin_series, _thin_src, _thin_bn, _thin_cb, _thin_oneoff = fve.normalized_base_and_series(
    THIN_CF, info=THIN_INFO, income_df=THIN_INCOME,
)
assert _thin_src == "ocf_fallback_financials", _thin_src
assert _thin_oneoff.get("is_financials_mode") is True, _thin_oneoff
print(f"[fewer_than_2_points_ocf_fallback] only 1 usable positive net-income point -> fell back to "
      f"the OCF path, source={_thin_src!r}, is_financials_mode={_thin_oneoff.get('is_financials_mode')} OK")


# ======================================================================
# CHECK 5: no scoring/selection leakage - the new fields never reach
# ranking_engine.calculate_long_score or top100_engine.composite_score.
# ======================================================================
import inspect

import ranking_engine
import top100_engine

for _mod, _name in ((ranking_engine, "calculate_long_score"), (top100_engine, "composite_score")):
    _fn = getattr(_mod, _name)
    _src = inspect.getsource(_fn)
    for _field in ("is_financials_mode", "net_income_financials", "ocf_fallback_financials"):
        assert _field not in _src, f"{_mod.__name__}.{_name} references {_field}!"
print("[no_scoring_leakage] is_financials_mode/net_income_financials/ocf_fallback_financials "
      "never referenced inside ranking_engine.calculate_long_score or top100_engine.composite_score OK")

print("FINANCIALS_MODE_DCF_SWEEP_DONE")
