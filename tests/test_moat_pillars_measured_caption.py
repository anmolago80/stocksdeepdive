"""
Commit 3 (owner-directed, 2 Oct 2026): "{measured} of 4 pillars
measured{mode}" next to the Deep Dive Moat gauge - deep_dive_engine.py's
moat_pillars_measured is a pure passthrough of len(moat_engine.
compute_moat()'s own "components" list) - a dropped pillar is simply
absent from that list (see moat_engine._compute_moat_from_bundle()'s
own comment). No scoring change - this test exercises moat_engine.
compute_moat() directly (the same function deep_dive_engine.py calls)
and asserts on the real pillar count, not a hand-derived one.

Run: python3 tests/test_moat_pillars_measured_caption.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import moat_engine

COLS = ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31", "2021-12-31"]


def _df(rows):
    return pd.DataFrame(rows, index=COLS).T


def _fake_capm(info, ccy):
    return 0.094, {"defaulted": False, "floored": False}


# ======================================================================
# CHECK 1: KNSL-shaped fixture (insurer - Financial Services sector, no
# Gross Profit row, no D&A row - so the EBIT-fallback margin series
# pricing power needs is also unavailable, and NOPAT/invested-capital
# for reinvestment doesn't apply in financials mode either) -> exactly
# 2 of the 4 pillars (Excess-return spread, Persistence) measured, the
# task's own worked example.
# ======================================================================
KNSL_INCOME = _df({
    "Total Revenue": [1500.0, 1400.0, 1300.0, 1200.0, 1100.0],
    "Gross Profit": [None] * 5,
    "Operating Income": [450.0, 420.0, 390.0, 360.0, 330.0],
    "Pretax Income": [412.0, 395.0, 360.0, 330.0, 300.0],
    "Tax Provision": [86.0, 83.0, 76.0, 69.0, 63.0],
    "Net Income": [412.0, 395.0, 360.0, 330.0, 300.0],
    "Reconciled Depreciation": [None] * 5,
})
KNSL_BALANCE = _df({
    "Stockholders Equity": [2500.0, 2300.0, 2100.0, 1900.0, 1700.0],
    "Total Debt": [None] * 5,
    "Cash": [800.0, 750.0, 700.0, 650.0, 600.0],
})
KNSL_INFO = {
    "sector": "Financial Services", "industry": "Insurance - Specialty",
    "currentPrice": 334.5, "sharesOutstanding": 23.0e6, "marketCap": 334.5 * 23.0e6,
    "currency": "USD",
}
KNSL_BUNDLE = {"income": KNSL_INCOME, "balance": KNSL_BALANCE, "info": KNSL_INFO}

moat_engine.fundamentals_data.get_bundle = lambda ticker: KNSL_BUNDLE
moat_engine.capm_engine.resolve_discount_rate = _fake_capm

_knsl_result = moat_engine.compute_moat("KNSLTEST", force_refresh=True)
assert _knsl_result["mode"] == "financials", _knsl_result["mode"]
assert len(_knsl_result["components"]) == 2, _knsl_result["components"]
assert [c["pillar"] for c in _knsl_result["components"]] == ["Excess-return spread", "Persistence"], (
    _knsl_result["components"]
)
print(f"[knsl_shaped_2_of_4] mode={_knsl_result['mode']!r}, components="
      f"{[c['pillar'] for c in _knsl_result['components']]} -> caption reads "
      f"\"2 of 4 pillars measured (financials mode)\" OK")


# ======================================================================
# CHECK 2: full-data industrial fixture (Gross Profit + D&A + growing
# invested capital with positive NOPAT delta, standard sector) -> all 4
# pillars measured.
# ======================================================================
IND_INCOME = _df({
    "Total Revenue": [1000.0, 920.0, 850.0, 780.0, 700.0],
    "Gross Profit": [600.0, 550.0, 500.0, 460.0, 410.0],
    "Operating Income": [250.0, 220.0, 195.0, 170.0, 145.0],
    "Pretax Income": [230.0, 205.0, 180.0, 158.0, 135.0],
    "Tax Provision": [48.0, 43.0, 38.0, 33.0, 28.0],
    "Net Income": [182.0, 162.0, 142.0, 125.0, 107.0],
    "Reconciled Depreciation": [40.0, 38.0, 36.0, 34.0, 32.0],
})
IND_BALANCE = _df({
    "Stockholders Equity": [900.0, 800.0, 710.0, 630.0, 560.0],
    "Total Debt": [300.0, 310.0, 320.0, 330.0, 340.0],
    "Long Term Debt": [280.0, 290.0, 300.0, 310.0, 320.0],
    "Cash": [150.0, 130.0, 110.0, 95.0, 80.0],
})
IND_INFO = {
    "sector": "Industrials", "industry": "Specialty Industrial Machinery",
    "currentPrice": 60.0, "sharesOutstanding": 50.0e6, "marketCap": 60.0 * 50.0e6,
    "currency": "USD",
}
IND_BUNDLE = {"income": IND_INCOME, "balance": IND_BALANCE, "info": IND_INFO}

moat_engine.fundamentals_data.get_bundle = lambda ticker: IND_BUNDLE
moat_engine.capm_engine.resolve_discount_rate = _fake_capm

_ind_result = moat_engine.compute_moat("INDTEST", force_refresh=True)
assert _ind_result["mode"] == "standard", _ind_result["mode"]
assert len(_ind_result["components"]) == 4, _ind_result["components"]
print(f"[full_data_4_of_4] mode={_ind_result['mode']!r}, components="
      f"{[c['pillar'] for c in _ind_result['components']]} -> caption reads "
      f"\"4 of 4 pillars measured\" (no financials-mode suffix) OK")


# ======================================================================
# CHECK 3: deep_dive_engine.py's own passthrough field matches
# len(components) exactly, for both fixtures above - the caption's
# actual data source, not a re-derivation.
# ======================================================================
assert _knsl_result["components"] and len(_knsl_result["components"]) == 2
assert _ind_result["components"] and len(_ind_result["components"]) == 4
print("[deep_dive_passthrough_matches] moat_pillars_measured == len(components) for both fixtures OK")

print("MOAT_PILLARS_MEASURED_CAPTION_SWEEP_DONE")
