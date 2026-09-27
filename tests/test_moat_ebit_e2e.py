import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COLS = ["2024-06-30 00:00:00", "2023-06-30 00:00:00", "2022-06-30 00:00:00", "2021-06-30 00:00:00"]


def df(rows):
    return pd.DataFrame(rows, index=COLS).T


income = df({
    "Revenue": [1000.0, 950.0, 900.0, 850.0],
    "Operating Income": [93.0, 80.0, 70.0, 60.0],
    "Pretax Income": [379.4, 340.0, 300.0, 260.0],
    "Net Non Operating Interest Income Expense": [-43.2, -40.0, -35.0, -30.0],
    "Other Income Expense": [0.2, 0.1, 0.1, 0.1],
    "Total Unusual Items": [None, None, None, None],
    "Reconciled Depreciation": [329.4, 300.0, 265.0, 230.0],
    "Pretax Income": [379.4, 340.0, 300.0, 260.0],
    "Tax Provision": [95.0, 85.0, 75.0, 65.0],
    "Net Income": [284.4, 255.0, 225.0, 195.0],
    "Gross Profit": [None, None, None, None],  # force operating-margin fallback
})

balance = df({
    "Stockholders Equity": [500.0, 480.0, 460.0, 440.0],
    "Total Debt": [200.0, 210.0, 220.0, 230.0],
    "Long Term Debt": [180.0, 190.0, 200.0, 210.0],
    "Cash": [50.0, 45.0, 40.0, 35.0],
})

info = {
    "sector": "Real Estate",
    "industry": "REIT - Diversified",
    "currentPrice": 10.0,
    "sharesOutstanding": 100_000_000,
    "marketCap": 1_000_000_000,
    "currency": "AUD",
}

bundle = {"income": income, "balance": balance, "info": info}

os.environ.pop("EBIT_FROM_PRETAX", None)
import moat_engine
import importlib
import auto_compounder_engine
importlib.reload(auto_compounder_engine)
importlib.reload(moat_engine)

# monkeypatch fundamentals_data.get_bundle and capm_engine to avoid network/complex deps
moat_engine.fundamentals_data.get_bundle = lambda ticker: bundle


def fake_capm(info, ccy):
    return 0.09, {"defaulted": False, "floored": False}


moat_engine.capm_engine.resolve_discount_rate = fake_capm

result_off = moat_engine.compute_moat("TEST.AX", force_refresh=True)
print("=== compute_moat, switch OFF ===")
print(result_off)
assert result_off["mode"] == "standard"

diag_off = moat_engine.compute_moat_diagnostics("TEST.AX")
print("\n=== diagnostics, switch OFF ===")
for row in diag_off["year_rows"]:
    print(row)
row0 = diag_off["year_rows"][0]
assert row0["operating_income"] == row0["operating_income_yf"] == 93.0, row0
# Schema note (fixed 27 Sep 2026, owner-directed regression-suite repair):
# Commit Q/2 renamed this diagnostic field from "ebit_derived" to
# "p_test" (same formula, computed unconditionally regardless of switch
# state - see moat_engine.py's own comment on why "operating_income" is
# the switch-aware field and p_test/oi_plus_da are the diagnostic
# reconciliation columns behind it). Pure rename, same substance.
assert abs(row0["p_test"] - 422.4) < 0.05, row0
assert row0["year_status"] == "matches_oi_plus_da", row0["year_status"]
assert any("business shrank" not in f for f in diag_off["flags"])
print("\nPASS: switch OFF -> diagnostics show p_test=422.4 (matches_oi_plus_da) but operating_income (the one actually used) stays 93.0 (yfinance raw)")

os.environ["EBIT_FROM_PRETAX"] = "1"
importlib.reload(auto_compounder_engine)
importlib.reload(moat_engine)
moat_engine.fundamentals_data.get_bundle = lambda ticker: bundle
moat_engine.capm_engine.resolve_discount_rate = fake_capm

result_on = moat_engine.compute_moat("TEST.AX", force_refresh=True)
print("\n=== compute_moat, switch ON ===")
print(result_on)

diag_on = moat_engine.compute_moat_diagnostics("TEST.AX")
print("\n=== diagnostics, switch ON ===")
for row in diag_on["year_rows"]:
    print(row)
row0_on = diag_on["year_rows"][0]
assert abs(row0_on["operating_income"] - 422.4) < 0.05, row0_on
assert row0_on["operating_income"] == row0_on["p_test"]
print("\nPASS: switch ON -> operating_income (the value actually used) now == p_test (422.4), not the old 93.0")

# Score should differ between switch off/on since NOPAT/ROIC/margins moved materially
print(f"\nMoat score OFF: {result_off['score']}  ON: {result_on['score']}")
assert result_off["score"] != result_on["score"]
print("PASS: Moat score materially differs between switch states, confirming the derivation actually feeds the pillars")

os.environ.pop("EBIT_FROM_PRETAX", None)
print("\nALL E2E TESTS PASSED")
