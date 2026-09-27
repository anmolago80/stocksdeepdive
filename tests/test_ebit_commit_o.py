import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_income(row_values_by_col, cols):
    """row_values_by_col: {row_label: [val_for_col0, val_for_col1, ...]}"""
    return pd.DataFrame(row_values_by_col, index=cols).T


COLS = ["2024-06-30 00:00:00", "2023-06-30 00:00:00", "2022-06-30 00:00:00"]

# ---- SUL.AX: FY23 (newest col here) and FY26-style older figures reused
# as a second year to prove the per-year derivation works across a whole
# series, not just the newest column. (Real SUL.AX FY26 hasn't happened
# yet on the calendar - reusing the user's verified FY26 numbers as the
# "prior year" column is just a vehicle to test two distinct years in one
# series; it is not a claim about SUL.AX's real FY22.)
sul_income = make_income(
    {
        "Operating Income": [93.0, -27.6, None],
        "Pretax Income": [379.4, 277.6, None],
        "Net Non Operating Interest Income Expense": [-43.2, -81.1, None],
        "Other Income Expense": [0.2, 1.4, None],
        "Total Unusual Items": [None, None, None],
        "Reconciled Depreciation": [329.4, 384.9, None],
    },
    COLS,
)
sul_bundle = {"income": sul_income, "info": {"sector": "Real Estate"}}

os.environ.pop("EBIT_FROM_PRETAX", None)
import auto_compounder_engine as ace

rows = ace.ebit_year_rows(sul_bundle, is_financials=False)
years = list(rows.keys())
print("SUL.AX years:", years)

fy23 = rows[years[0]]
fy26 = rows[years[1]]
print("SUL FY23 row:", fy23)
print("SUL FY26 row:", fy26)

# Schema note (fixed 27 Sep 2026, owner-directed regression-suite repair):
# Commit Q/2 renamed this suite's own "ebit_derived" field to "p_test" -
# same formula (pretax_income - net_interest - other_income), computed
# unconditionally every year regardless of switch state or match outcome
# (see ebit_year_rows()'s own docstring). This is a pure rename, not a
# behavior change - the assertions below are unchanged in substance.
assert abs(fy23["p_test"] - 422.4) < 0.05, fy23["p_test"]
assert abs(fy26["p_test"] - 357.3) < 0.05, fy26["p_test"]
assert abs(fy23["operating_income_yf"] - 93.0) < 0.001
assert abs(fy26["operating_income_yf"] - (-27.6)) < 0.001
# reconciliation identity: op_income_yf + reconciled_depreciation == p_test
assert abs((fy23["operating_income_yf"] + fy23["reconciled_depreciation"]) - fy23["p_test"]) < 0.05
assert abs((fy26["operating_income_yf"] + fy26["reconciled_depreciation"]) - fy26["p_test"]) < 0.05
# Confirm this is passing for the RIGHT reason - both years' own p_test
# actually reconciled against OI+DA (not just numerically close by
# coincidence of the test data).
assert fy23["year_status"] == "matches_oi_plus_da", fy23["year_status"]
assert fy26["year_status"] == "matches_oi_plus_da", fy26["year_status"]
print("PASS: SUL.AX FY23 -> 422.4m, FY26 -> 357.3m, both match the Operating Income + Reconciled Depreciation identity (year_status confirms matches_oi_plus_da, not a coincidental close number)")

# switch OFF (default): "ebit" field must stay yfinance's raw figure
assert fy23["ebit"] == fy23["operating_income_yf"] == 93.0
assert fy26["ebit"] == fy26["operating_income_yf"] == -27.6
print("PASS: switch OFF (default) -> ebit field == raw yfinance Operating Income, unchanged")

# ---- JBH.AX FY23
jbh_income = make_income(
    {
        "Operating Income": [543.5, None, None],
        "Pretax Income": [747.1, None, None],
        "Net Non Operating Interest Income Expense": [-26.3, None, None],
        "Other Income Expense": [7.6, None, None],
        "Total Unusual Items": [None, None, None],
        "Reconciled Depreciation": [222.3, None, None],
    },
    COLS,
)
jbh_bundle = {"income": jbh_income, "info": {"sector": "Consumer Cyclical"}}
jbh_rows = ace.ebit_year_rows(jbh_bundle, is_financials=False)
jbh_fy23 = jbh_rows[list(jbh_rows.keys())[0]]
print("JBH FY23 row:", jbh_fy23)
assert abs(jbh_fy23["p_test"] - 765.8) < 0.05, jbh_fy23["p_test"]
assert jbh_fy23["year_status"] == "matches_oi_plus_da", jbh_fy23["year_status"]
# "gap_pct" was never a real field of ebit_year_rows()'s own output (it's
# a bill_check_engine.py concept, unrelated) - computed inline here
# instead, same substance as the original check: the understatement is
# large enough that a laxer "<50%" threshold would have missed it.
gap = (jbh_fy23["p_test"] - jbh_fy23["operating_income_yf"]) / jbh_fy23["operating_income_yf"]
assert gap > 0.25, gap  # ~40% understatement - a "below 50%" threshold would have missed it
print(f"PASS: JBH.AX FY23 -> 765.8m derived (p_test), gap={gap:.1%} vs yfinance's 543.5m (a <50%% threshold would miss this)")

# ---- AAPL FY2025 (illustrative synthetic - NOT fetched from live data;
# this sandbox has no network access. Demonstrates the "no D&A gap"
# filer-structure case: Pretax already reconciles cleanly to Operating
# Income, so derived EBIT should land within 1% of yfinance's own
# Operating Income figure, same as the user's report that AAPL is
# unaffected by this bug. Real AAPL figures must be verified against
# production data via the Admin Dashboard's audit tool before switching
# EBIT_FROM_PRETAX on.)
op_income_yf_aapl = 133.05
aapl_income = make_income(
    {
        "Operating Income": [op_income_yf_aapl, None, None],
        # AAPL carries net INTEREST/OTHER INCOME (positive), not a D&A
        # double-count gap - Pretax = Operating Income + net interest
        # income + other income, so subtracting them back out returns
        # (approximately) the same Operating Income figure.
        "Pretax Income": [op_income_yf_aapl + 0.55, None, None],
        "Net Non Operating Interest Income Expense": [0.40, None, None],
        "Other Income Expense": [0.15, None, None],
        "Total Unusual Items": [None, None, None],
        "Reconciled Depreciation": [None, None, None],
    },
    COLS,
)
aapl_bundle = {"income": aapl_income, "info": {"sector": "Technology"}}
aapl_rows = ace.ebit_year_rows(aapl_bundle, is_financials=False)
aapl_fy = aapl_rows[list(aapl_rows.keys())[0]]
print("AAPL FY2025 row (illustrative synthetic, not live data):", aapl_fy)
gap_frac = abs(aapl_fy["p_test"] - op_income_yf_aapl) / op_income_yf_aapl
assert gap_frac <= 0.01, gap_frac
assert aapl_fy["year_status"] == "matches_oi", aapl_fy["year_status"]
print(f"PASS: AAPL-shape (no-D&A-gap) filer -> derived EBIT within {gap_frac:.2%} of yfinance Operating Income (year_status confirms matches_oi)")

# ---- Financials mode: derivation must be bypassed entirely, "ebit" ==
# raw yfinance Operating Income even with the switch ON.
os.environ["EBIT_FROM_PRETAX"] = "1"
import importlib
importlib.reload(ace)

bank_income = make_income(
    {
        "Operating Income": [50.0, None, None],
        "Pretax Income": [500.0, None, None],
        "Net Non Operating Interest Income Expense": [-10.0, None, None],
        "Other Income Expense": [0.0, None, None],
        "Total Unusual Items": [None, None, None],
        "Reconciled Depreciation": [None, None, None],
    },
    COLS,
)
bank_bundle = {"income": bank_income, "info": {"sector": "Financial Services"}}
bank_rows = ace.ebit_year_rows(bank_bundle, is_financials=True)
bank_fy = bank_rows[list(bank_rows.keys())[0]]
print("Bank row (switch ON, is_financials=True):", bank_fy)
assert bank_fy["ebit"] == bank_fy["operating_income_yf"] == 50.0, bank_fy
assert bank_fy["p_test"] == 510.0  # 500 - (-10) - 0 = 510, still computed for diagnostics, just not USED
print("PASS: financials mode bypasses the derived value in 'ebit' even with the switch ON (diagnostics still computed)")

# ---- Switch ON, non-financials: "ebit" must now pick up the derived value
sul_rows_on = ace.ebit_year_rows(sul_bundle, is_financials=False)
sul_fy23_on = sul_rows_on[list(sul_rows_on.keys())[0]]
assert sul_fy23_on["ebit"] == sul_fy23_on["p_test"] == sul_fy23_on["pretax_income"] - sul_fy23_on["net_interest"] - sul_fy23_on["other_income"]
print("PASS: switch ON + non-financials -> ebit field == p_test")

os.environ.pop("EBIT_FROM_PRETAX", None)
print("\nALL TESTS PASSED")
