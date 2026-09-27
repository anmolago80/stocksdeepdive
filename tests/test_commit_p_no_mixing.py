"""Commit P regression suite - "no mixing": a year the engine can't fully
verify must never silently produce a WRONG blended/hybrid EBIT number.

Design note (fixed 27 Sep 2026, owner-directed regression-suite repair):
this suite originally tested Commit P's own mechanism for that guarantee
- a year missing Pretax Income got DROPPED to None outright, even though
its raw yfinance Operating Income was on file, and ebit_ttm() would then
substitute an earlier year's value and flag the substitution.

Commit Q's later rewrite of ebit_year_rows() (see that function's own
docstring) intentionally replaced this: a year that can't be verified
(missing Pretax Income - "no_data" - or present but reconciling to
neither identity - "unverified") now falls back to its own raw
operating_income_yf instead of None, "unchanged (None if that row
itself isn't on file for this year - dropped, never guessed)". This is
a deliberate, documented design change, not a regression - a clean,
honest, real number (the company's own reported Operating Income) is a
better answer than dropping the year entirely, and it's still never a
"mixed" or partially-computed hybrid value.

This suite is rewritten below to test the SAME underlying guarantee
("never a wrong blended number") against the CURRENT, correct
mechanism: a year with missing Pretax Income falls back cleanly to its
own real operating_income_yf (verified via p_test/year_status, not just
a number that happens to match), never a partial/blended computation.
The "drop to None + TTM substitution + flag" mechanism itself is only
exercised now in the one case current code still uses it for: a year
missing BOTH Pretax Income AND its own raw Operating Income (nothing at
all to fall back to)."""
import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["EBIT_FROM_PRETAX"] = "1"
import auto_compounder_engine as ace

COLS = ["2024-06-30 00:00:00", "2023-06-30 00:00:00", "2022-06-30 00:00:00"]


def make_income(rows):
    return pd.DataFrame(rows, index=COLS).T


# SUL-shaped: FY24 (newest) and FY22 (oldest) have full data and derive
# to 422.4 (real SUL FY23 figures, reused here as two years of this
# test's own series). FY23 (middle) is MISSING Pretax Income entirely -
# the case under test - but its raw Operating Income (93.0) IS on file.
income = make_income(
    {
        "Operating Income": [93.0, 93.0, 93.0],  # yfinance's raw figure for EVERY year, deliberately identical
        "Pretax Income": [379.4, None, 379.4],    # FY23 (middle) missing
        "Net Non Operating Interest Income Expense": [-43.2, -43.2, -43.2],
        "Other Income Expense": [0.2, 0.2, 0.2],
        "Total Unusual Items": [None, None, None],
        "Reconciled Depreciation": [329.4, 329.4, 329.4],
    }
)
bundle = {"income": income, "info": {"sector": "Real Estate"}}

flags = []
rows = ace.ebit_year_rows(bundle, is_financials=False, flags=flags)
years = list(rows.keys())
print("years:", years)
newest, middle, oldest = years[0], years[1], years[2]

print("newest row:", rows[newest])
print("middle row (missing Pretax Income):", rows[middle])
print("oldest row:", rows[oldest])

assert rows[newest]["ebit"] == 422.4, rows[newest]
assert rows[oldest]["ebit"] == 422.4, rows[oldest]
# THE key assertion, current mechanism: no Pretax Income to test against
# -> year_status "no_data" -> falls back to its OWN real operating_income_yf
# (93.0) - a clean, honest, real number, never a partial/blended value,
# and never silently guessed at some OTHER year's derived figure (422.4).
assert rows[middle]["year_status"] == "no_data", rows[middle]["year_status"]
assert rows[middle]["p_test"] is None  # nothing to test - genuinely no data, not a failed test
assert rows[middle]["ebit"] == rows[middle]["operating_income_yf"] == 93.0, rows[middle]
assert rows[middle]["ebit"] != 422.4  # never silently mixed with the OTHER years' derived value
print("PASS: middle year (missing Pretax Income) falls back cleanly to its own raw 93.0 - "
      "never None, and never mixed with the other years' own derived 422.4")

# A "no_data" year is not itself alarming (there's simply nothing to
# test that year against) - Commit Q's design only flags an "unverified"
# year (data present but doesn't reconcile). Confirm no flag fires here.
print("flags:", flags)
assert flags == []
print("PASS: no flag line for a clean no_data fallback (nothing anomalous happened)")

# Confirm this also holds through ebit_series() (the [(year, value)] wrapper)
series = dict(ace.ebit_series(bundle, is_financials=False))
assert series[middle] == 93.0
assert series[newest] == 422.4
print("PASS: ebit_series() reflects the same clean fallback")

# ebit_ttm(): newest year IS derivable, so TTM should just be 422.4,
# no substitution needed (the missing year isn't the newest one here).
flags2 = []
val, estimated = ace.ebit_ttm(bundle, is_financials=False, flags=flags2)
print("ebit_ttm:", val, estimated, "flags:", flags2)
assert val == 422.4 and estimated is False
print("PASS: ebit_ttm unaffected when the missing year isn't the newest")

# Now the genuine "nothing to fall back to" case: the NEWEST year is
# missing BOTH Pretax Income AND its own raw Operating Income - this is
# the one scenario where current code still drops a year to None and
# ebit_ttm() must substitute an earlier year, flagging why.
income2 = make_income(
    {
        "Operating Income": [None, 93.0, 93.0],   # newest now ALSO missing
        "Pretax Income": [None, 379.4, 379.4],    # newest missing
        "Net Non Operating Interest Income Expense": [-43.2, -43.2, -43.2],
        "Other Income Expense": [0.2, 0.2, 0.2],
        "Total Unusual Items": [None, None, None],
        "Reconciled Depreciation": [329.4, 329.4, 329.4],
    }
)
bundle2 = {"income": income2, "info": {"sector": "Real Estate"}}
rows2 = ace.ebit_year_rows(bundle2, is_financials=False)
years2 = list(rows2.keys())
newest2 = years2[0]
assert rows2[newest2]["year_status"] == "no_data"
assert rows2[newest2]["operating_income_yf"] is None  # genuinely nothing on file to fall back to
assert rows2[newest2]["ebit"] is None
print(f"PASS: newest year ({newest2}) itself is None only when BOTH Pretax Income and its own "
      f"raw Operating Income are missing - there is truly nothing to fall back to")

flags3 = []
val2, estimated2 = ace.ebit_ttm(bundle2, is_financials=False, flags=flags3)
print("ebit_ttm (newest missing both):", val2, estimated2, "flags:", flags3)
assert val2 == 422.4
assert any("TTM figure taken from" in f and newest2 in f for f in flags3)
print("PASS: ebit_ttm falls back to the next available year and flags the substitution")

# Whole-ticker fallback: NO year at all has Pretax Income -> old path,
# every year uses raw yfinance Operating Income, no drops, no flags.
income3 = make_income(
    {
        "Operating Income": [93.0, 80.0, 70.0],
        "Pretax Income": [None, None, None],
        "Net Non Operating Interest Income Expense": [-43.2, -40.0, -35.0],
        "Other Income Expense": [0.2, 0.1, 0.1],
        "Total Unusual Items": [None, None, None],
        "Reconciled Depreciation": [329.4, 300.0, 265.0],
    }
)
bundle3 = {"income": income3, "info": {"sector": "Real Estate"}}
flags4 = []
rows3 = ace.ebit_year_rows(bundle3, is_financials=False, flags=flags4)
for y, r in rows3.items():
    assert r["ebit"] == r["operating_income_yf"], (y, r)
assert flags4 == []
print("PASS: whole-ticker fallback (no year derivable) uses the old path for every year, no drops/flags")

print("\nALL COMMIT P TESTS PASSED")
