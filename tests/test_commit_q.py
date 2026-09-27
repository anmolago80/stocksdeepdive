import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EBIT_FROM_PRETAX"] = "1"
import auto_compounder_engine as ace

COLS = ["2024-06-30 00:00:00", "2023-06-30 00:00:00", "2022-06-30 00:00:00"]


def mk(rows):
    return pd.DataFrame(rows, index=COLS).T


def newest_row(bundle, flags=None):
    rows = ace.ebit_year_rows(bundle, is_financials=False, flags=flags if flags is not None else [])
    y = list(rows.keys())[0]
    return y, rows[y], rows


# ---- SUL.AX FY23-shaped: 3 years, all show the OI+DA identity -> corrected
sul = mk({
    "Operating Income": [93.0, 80.0, 70.0],
    "Pretax Income": [379.4, 340.0, 300.0],
    "Net Non Operating Interest Income Expense": [-43.2, -40.0, -35.0],
    "Other Income Expense": [0.2, 0.1, 0.1],
    "Total Unusual Items": [None, None, None],
    "Reconciled Depreciation": [329.4, 300.0, 265.0],
})
y, r, rows = newest_row({"income": sul, "info": {"sector": "Real Estate"}})
assert abs(r["ebit"] - 422.4) < 0.05 and r["ticker_corrected"] is True
print("PASS: SUL.AX FY23-shaped -> corrected, 93.0+329.4=422.4")

# ---- JBH.AX FY23-shaped
jbh = mk({
    "Operating Income": [543.5, 500.0, 470.0],
    "Pretax Income": [747.1, 700.0, 650.0],
    "Net Non Operating Interest Income Expense": [-26.3, -25.0, -22.0],
    "Other Income Expense": [7.6, 5.0, 4.0],
    "Total Unusual Items": [None, None, None],
    "Reconciled Depreciation": [222.3, 220.0, 206.0],
})
y, r, rows = newest_row({"income": jbh, "info": {"sector": "Consumer Cyclical"}})
assert abs(r["ebit"] - 765.8) < 0.05 and r["ticker_corrected"] is True
print("PASS: JBH.AX FY23-shaped -> corrected, 543.5+222.3=765.8")

# ---- AAPL FY2025-shaped: Pretax reconciles directly to OI, no D&A gap -> unchanged
aapl = mk({
    "Operating Income": [133.05, 128.0, 120.0],
    "Pretax Income": [133.60, 128.5, 120.4],
    "Net Non Operating Interest Income Expense": [0.40, 0.35, 0.30],
    "Other Income Expense": [0.15, 0.15, 0.10],
    "Total Unusual Items": [None, None, None],
    "Reconciled Depreciation": [11.0, 10.5, 10.0],
})
y, r, rows = newest_row({"income": aapl, "info": {"sector": "Technology"}})
assert r["year_status"] == "matches_oi"
# Fixed 27 Sep 2026 (owner-directed regression-suite repair): exact `==`
# against a float built from a chain of subtractions (133.60 - 0.40 -
# 0.15) fails on ordinary floating-point drift (133.60 - 0.40 - 0.15 ==
# 133.04999999999998 in plain Python, not 133.05) - not a schema change,
# not a real behavioral difference, just an over-strict comparison.
# Tightened to a tolerance, same substance as every other float
# assertion in this suite.
assert abs(r["ebit"] - 133.05) < 1e-6 and r["ticker_corrected"] is False
print("PASS: AAPL FY2025-shaped -> unchanged at 133.05 (matches_oi, no bug)")

# ---- ECHO-shaped: a one-off write-down in the newest year's Other
# Income Expense (embedding what would also be Total Unusual Items),
# blowing Pretax out - neither OI nor OI+DA match -> stays unchanged
echo = mk({
    "Operating Income": [-91.0, 50.0, 45.0],
    "Pretax Income": [-9000.0, 55.0, 48.0],
    "Net Non Operating Interest Income Expense": [-5.0, -4.0, -3.5],
    "Other Income Expense": [-8850.0, 0.5, 0.3],
    "Total Unusual Items": [-8850.0, None, None],
    "Reconciled Depreciation": [10.0, 9.0, 8.0],
})
y, r, rows = newest_row({"income": echo, "info": {"sector": "Communication Services"}})
assert r["year_status"] == "unverified", r["year_status"]
assert r["ebit"] == r["operating_income_yf"] == -91.0
# Design note (fixed 27 Sep 2026, owner-directed regression-suite repair):
# this suite's own "assert r['ticker_corrected'] is False" no longer
# holds - not a schema rename, a genuine behavior change from Commit Q
# (ticker-level 3-years-must-agree gate) to Commit 2 (per-YEAR decision,
# gate removed - see ebit_year_rows()'s own docstring: "it no longer
# gates anything ... see below for why the ticker-level gate itself is
# gone"). In THIS fixture's own older years (2023/2022), the numbers
# happen to independently satisfy matches_oi_plus_da on their own terms
# (a property of this test's synthetic data, not of the one-off itself),
# so ticker_corrected is genuinely True now - correctly reflecting that
# SOME year for this ticker verified and moved a real number. The
# substantive thing this test was written to protect - the ONE-OFF
# YEAR ITSELF never gets corrected to a huge positive number - still
# holds and is asserted above via year_status/ebit on that specific
# year, which is what actually matters.
assert rows[y]["ticker_corrected"] is True
print("PASS: ECHO-shaped one-off year itself stays -91.0 (unverified, never corrected to a huge "
      "positive number) - ticker_corrected is True only because OTHER years in this fixture "
      "independently verify under Commit 2's per-year rule, not because the one-off was misread")

# ---- KHC-shaped: a big, persistent gap that is NOT a D&A double-count
# (e.g. goodwill impairment run through Other Income every year) -
# neither OI nor OI+DA matches any year -> unchanged, all "unverified"
khc = mk({
    "Operating Income": [4600.0, 4500.0, 4400.0],
    "Pretax Income": [14000.0, 13800.0, 13500.0],
    "Net Non Operating Interest Income Expense": [-500.0, -480.0, -460.0],
    "Other Income Expense": [-9200.0, -9100.0, -8950.0],  # goodwill impairment, not D&A
    "Total Unusual Items": [-9200.0, -9100.0, -8950.0],
    "Reconciled Depreciation": [800.0, 780.0, 760.0],  # real D&A - far short of closing the gap
})
y, r, rows = newest_row({"income": khc, "info": {"sector": "Consumer Defensive"}})
assert r["ticker_corrected"] is False
assert r["ebit"] == r["operating_income_yf"] == 4600.0
print("PASS: KHC-shaped persistent non-D&A gap -> stays 4600.0, never corrected")

# ---- CSL.AX-shaped: newest year alone LOOKS like it matches OI+DA (a
# coincidence / real anomaly), but the two prior years do NOT.
#
# Design note (fixed 27 Sep 2026, owner-directed regression-suite
# repair): this scenario originally asserted a ticker-level "fewer than
# 3 confirming years -> stays unchanged" gate. That gate was REMOVED by
# Commit 2 (see ebit_year_rows()'s own docstring: "why Commit Q's own
# ticker-level '3 years must match' gate is gone") - the design moved
# to a per-YEAR decision on purpose. The old assertion below
# (`len(matching) < 3 or r["ticker_corrected"] is False`) still executed
# without raising, but only VACUOUSLY - `len(matching) < 3` was true
# for this fixture's own numbers, so the `or` short-circuited and the
# right-hand clause (the actual, now-false claim that ticker_corrected
# stays False) was never evaluated. A real regression in this exact
# area - a single anomalous/possibly-fabricated year getting "corrected"
# on its own say-so - would have been INVISIBLE to this test. Rewritten
# to assert what Commit 2 actually, intentionally does now: the single
# matching year's own ebit DOES get corrected (to 17,000, the p_test/
# OI+DA value), precisely because there is no ticker-level gate left to
# block it. This is current, deliberate behavior, not a bug this test
# should be catching - flagging it here so it's visible rather than
# silently assumed.
csl = mk({
    "Operating Income": [4350.0, 4100.0, 3900.0],
    "Pretax Income": [16700.0, 4300.0, 4050.0],  # only the newest year is wildly different
    "Net Non Operating Interest Income Expense": [-200.0, -150.0, -140.0],
    "Other Income Expense": [-100.0, 50.0, 45.0],
    "Total Unusual Items": [None, None, None],
    "Reconciled Depreciation": [12650.0, 100.0, 95.0],  # newest year's DA looks fabricated/huge
})
y, r, rows = newest_row({"income": csl, "info": {"sector": "Healthcare"}})
print("CSL newest row:", r)
years = list(rows.keys())
matching = [yy for yy in years if rows[yy]["year_status"] == "matches_oi_plus_da"]
print("matching years:", matching)
assert matching == [y], matching  # only the newest year verifies, confirming this is a single-year case
assert r["year_status"] == "matches_oi_plus_da"
assert abs(r["ebit"] - 17000.0) < 0.05 and r["ticker_corrected"] is True
print("PASS: CSL.AX-shaped single-year anomaly IS corrected under Commit 2's per-year rule "
      "(17,000, matching p_test/OI+DA) even though the other two years never verify - the "
      "3-year ticker-level gate that used to block this was intentionally removed by Commit 2")

# ---- Financials mode: never touched regardless of switch
bank = mk({
    "Operating Income": [500.0, 480.0, 460.0],
    "Pretax Income": [700.0, 670.0, 640.0],
    "Net Non Operating Interest Income Expense": [-50.0, -48.0, -46.0],
    "Other Income Expense": [-150.0, -142.0, -134.0],
    "Total Unusual Items": [None, None, None],
    "Reconciled Depreciation": [400.0, 390.0, 380.0],
})
y, r, rows = newest_row({"income": bank, "info": {"sector": "Financial Services"}})
# force is_financials True explicitly this time
rows_fin = ace.ebit_year_rows({"income": bank, "info": {"sector": "Financial Services"}}, is_financials=True)
newest_fin = list(rows_fin.keys())[0]
assert rows_fin[newest_fin]["ebit"] == rows_fin[newest_fin]["operating_income_yf"] == 500.0
assert rows_fin[newest_fin]["ticker_corrected"] is False
print("PASS: financials mode never corrected regardless of the underlying numbers")

# ---- Switch OFF: everything stays at OI regardless of the numbers
os.environ.pop("EBIT_FROM_PRETAX", None)
import importlib
importlib.reload(ace)
rows_off = ace.ebit_year_rows({"income": sul, "info": {"sector": "Real Estate"}}, is_financials=False)
newest_off = list(rows_off.keys())[0]
assert rows_off[newest_off]["ebit"] == 93.0
assert rows_off[newest_off]["ticker_corrected"] is False
print("PASS: switch OFF (default) -> SUL.AX-shaped stays at raw 93.0, unaffected")

print("\nALL COMMIT Q TESTS PASSED")
