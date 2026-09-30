"""
Audit fixes, Commit 4 (owner-directed, 30 Sep 2026, per instruction_
tonight_sequence_v2.md step 2). Covers:

  - share_class_engine.whole_company_shares(): the "implied" tier now
    has an upper ratio ceiling (DUAL_CLASS_SHARE_RATIO_MAX = 3.0) and,
    for a known multi-class ticker, requires corroboration from a
    cheap targeted filed-diluted-shares fetch - a candidate that fails
    either check is rejected with a note instead of silently accepted.
  - auto_compounder_engine._reverse_split_suspected(): guards the same
    1.3x test against a reverse split showing up purely as a >30% jump
    in the two most recent "Ordinary Shares Number" balance-sheet
    columns.
  - fcf_valuation_engine.normalized_base_and_series(): the Task-10
    outlier-median swap no longer fires when the rising-capex
    (midpoint) guard already did - it used to compare a midpoint-basis
    base against an average-basis median and silently replace it,
    while still reporting capex_basis="midpoint (capex rising)".
  - capm_engine.resolve_discount_rate_by_market_cap(): a missing
    marketCap now also sets meta["defaulted"], which fcf_valuation_
    engine.dcf_intrinsic_value() propagates into its own outer
    meta["defaulted"]/meta["market_cap_missing"].

Run: python3 tests/test_audit_c4_valuation_guards.py
"""
import os
import sys
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import share_class_engine as sce
import auto_compounder_engine as ace
import fcf_valuation_engine as fve
import capm_engine


# ======================================================================
# CHECK 1: ratio 5x, uncorroborated (or above the sanity ceiling) -
# ignored, with a note.
# ======================================================================
info_5x = {"sharesOutstanding": 100_000, "impliedSharesOutstanding": 500_000}
with mock.patch.object(sce, "_fetch_diluted_shares", return_value=None):
    shares, flagged, source, note = sce.whole_company_shares(info_5x, ticker="GOOG")
assert shares == 100_000, shares
assert flagged is False, flagged
assert source is None, source
assert note is not None and "5.0x" in note, note
print("[c4_ratio_5x_ignored] a 5x candidate (above the 3.0x sanity ceiling) is "
      f"ignored, with a note: {note!r} OK")

# An arbitrary (non-SHARE_CLASS_PAIRS) ticker at ratio 5x is rejected the
# same way, on the ratio ceiling alone (no corroboration fetch attempted).
info_5x_unknown = {"sharesOutstanding": 100_000, "impliedSharesOutstanding": 500_000}
with mock.patch.object(sce, "_fetch_diluted_shares") as m_fetch:
    shares_u, flagged_u, source_u, note_u = sce.whole_company_shares(info_5x_unknown, ticker="ZZZZ")
assert shares_u == 100_000 and flagged_u is False and note_u is not None
assert not m_fetch.called, "an unlisted ticker must never trigger a corroboration fetch"
print("[c4_ratio_5x_unknown_ticker] an unlisted ticker's 5x candidate is rejected "
      "without any corroboration fetch attempted OK")


# ======================================================================
# CHECK 2: ratio 2x, corroborated by a known ticker's own filed diluted
# count - applied, flagged.
# ======================================================================
info_2x = {"sharesOutstanding": 100_000, "impliedSharesOutstanding": 200_000}
with mock.patch.object(sce, "_fetch_diluted_shares", return_value=205_000):
    shares2, flagged2, source2, note2 = sce.whole_company_shares(info_2x, ticker="GOOG")
assert shares2 == 200_000, shares2
assert flagged2 is True, flagged2
assert source2 == "implied", source2
assert note2 is None, note2
print("[c4_ratio_2x_corroborated] a 2x candidate corroborated within 15% by the "
      "ticker's own filed diluted count is applied, flagged OK")

# Same ratio, but the filed figure does NOT corroborate it (>15% off) -
# rejected, with a note.
with mock.patch.object(sce, "_fetch_diluted_shares", return_value=140_000):
    shares2b, flagged2b, source2b, note2b = sce.whole_company_shares(info_2x, ticker="GOOG")
assert shares2b == 100_000 and flagged2b is False
assert note2b is not None and "uncorroborated" in note2b, note2b
print("[c4_ratio_2x_uncorroborated] a 2x candidate NOT corroborated by the filed "
      f"count is rejected: {note2b!r} OK")

# A tier-1 "bundle" candidate (income_df already on hand) needs no
# separate corroboration - it IS the filed figure - only the ratio
# ceiling applies.
income_df_2x = pd.DataFrame(
    {"2026-06-30": [200_000]}, index=["Diluted Average Shares"])
shares2c, flagged2c, source2c, note2c = sce.whole_company_shares(
    {"sharesOutstanding": 100_000}, income_df=income_df_2x)
assert shares2c == 200_000 and flagged2c is True and source2c == "bundle" and note2c is None
print("[c4_bundle_tier_no_corroboration_needed] a tier-1 bundle candidate within "
      "bounds is applied without a separate corroboration check OK")


# ======================================================================
# CHECK 3: reverse-split guard - a >30% jump in the two most recent
# "Ordinary Shares Number" balance-sheet columns skips the whole
# dual-class test for that ticker.
# ======================================================================
balance_split = pd.DataFrame(
    {"2026-06-30": [10_000_000], "2025-06-30": [100_000_000]},
    index=["Ordinary Shares Number"],
)
bundle_split = {
    "info": {"sharesOutstanding": 10_000_000, "impliedSharesOutstanding": 30_000_000},
    "income": pd.DataFrame({"2026-06-30": [30_000_000]}, index=["Diluted Average Shares"]),
    "balance": balance_split,
}
assert ace._reverse_split_suspected(bundle_split) is True
shares3, flagged3 = ace._whole_company_shares(bundle_split)
assert shares3 == 10_000_000 and flagged3 is False, (shares3, flagged3)
print("[c4_reverse_split_skips_test] a >30% jump between the two most recent "
      "Ordinary Shares Number columns skips the dual-class test entirely, even "
      "though the income statement alone would otherwise have flagged it OK")

# A normal (stable) share count does NOT suspect a reverse split, and the
# dual-class test still applies normally.
balance_stable = pd.DataFrame(
    {"2026-06-30": [10_000_000], "2025-06-30": [9_800_000]},
    index=["Ordinary Shares Number"],
)
bundle_stable = {
    "info": {"sharesOutstanding": 10_000_000},
    "income": pd.DataFrame({"2026-06-30": [30_000_000]}, index=["Diluted Average Shares"]),
    "balance": balance_stable,
}
assert ace._reverse_split_suspected(bundle_stable) is False
shares3b, flagged3b = ace._whole_company_shares(bundle_stable)
assert shares3b == 30_000_000 and flagged3b is True
print("[c4_stable_shares_test_applies] a stable share count (<30% change) still "
      "runs the dual-class test normally OK")


# ======================================================================
# CHECK 4: capex midpoint-vs-median consistency - the ramp fixture from
# b958fb0's own bug: a rising-capex (midpoint) base must NOT be swapped
# out for an average-basis median, and capex_basis must keep reporting
# what was actually used.
# ======================================================================
def _mkdf(ocf, capex):
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


# Latest year: OCF spikes to 300, capex spikes to -100 (a real ramp).
# Prior 3 years: flat OCF 100, capex -20. avg_capex = -40, latest_capex
# = -100 -> triggers the midpoint guard (capex_basis="midpoint (capex
# rising)"), base = 300 + (-100 + -40)/2 = 230. The average-basis
# series/median (what the OLD buggy code compared `base` against) is
# [260, 60, 60, 60] -> median 60 - wildly different from 230 purely from
# the basis mismatch, which used to trigger the outlier swap and
# silently replace 230 with 60 while still claiming "midpoint".
RAMP_CF = _mkdf([300, 100, 100, 100], [-100, -20, -20, -20])
base, series, src, base_normalized, capex_basis, _oneoff_meta = fve.normalized_base_and_series(RAMP_CF)
assert capex_basis == "midpoint (capex rising)", capex_basis
assert base == 230, base
assert base_normalized is False, (
    "the outlier-median swap must NOT fire when the midpoint guard already did")
print(f"[c4_capex_midpoint_not_swapped] base={base} keeps the midpoint-guarded "
      f"figure (not the average-basis median 60), capex_basis={capex_basis!r} "
      "correctly agrees with what was actually used OK")

# Sanity: a non-rising-capex ticker still gets the ordinary outlier swap
# when its own (average-basis) base genuinely deviates from the median -
# unaffected by this fix.
NORMAL_OUTLIER_CF = _mkdf([10, 100, 100, 100], [-10, -10, -10, -10])
base_n, series_n, src_n, normalized_n, basis_n, _oneoff_meta_n = fve.normalized_base_and_series(NORMAL_OUTLIER_CF)
assert basis_n == "average", basis_n
assert normalized_n is True, "an ordinary (non-rising-capex) outlier must still swap to the median"
print(f"[c4_ordinary_outlier_swap_unaffected] a non-rising-capex outlier still "
      f"swaps to the median (base={base_n}) exactly as before this fix OK")


# ======================================================================
# CHECK 5: missing marketCap -> defaulted True (capm_engine and, end to
# end, fcf_valuation_engine.dcf_intrinsic_value()'s own meta).
# ======================================================================
with mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.04, "live")):
    rate, cap_meta = capm_engine.resolve_discount_rate_by_market_cap({"currency": "USD"}, "USD")
assert cap_meta["market_cap_missing"] is True, cap_meta
assert cap_meta["defaulted"] is True, cap_meta
print("[c4_missing_marketcap_defaulted] resolve_discount_rate_by_market_cap() "
      "sets both market_cap_missing and defaulted when marketCap is absent OK")

with mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.04, "live")):
    rate2, cap_meta2 = capm_engine.resolve_discount_rate_by_market_cap(
        {"currency": "USD", "marketCap": 50_000_000_000}, "USD")
assert cap_meta2["market_cap_missing"] is False
print("[c4_present_marketcap_not_defaulted] a real marketCap does not set either "
      "flag OK")

with mock.patch.object(fve, "normalized_base_and_series",
                        return_value=(1_000_000.0, [1_000_000.0] * 3, "ocf-normcapex", False, "average",
                                      {"fcf_base_source": "ocf-normcapex", "fcf_distorted_years": [], "fcf_base_raw": None})), \
     mock.patch.object(fve.capm_engine, "resolve_perpetual_rate", return_value=0.025), \
     mock.patch.object(fve.capm_engine, "get_growth_estimates_5y", return_value=(0.08, "ok")), \
     mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.04, "live")):
    iv, g, meta = fve.dcf_intrinsic_value(
        "TEST", info={"sharesOutstanding": 1_000_000, "currency": "USD"},
        cashflow_df=pd.DataFrame(), currency="USD",
    )
assert meta["market_cap_missing"] is True, meta
assert meta["defaulted"] is True, meta
print("[c4_end_to_end_defaulted] dcf_intrinsic_value()'s own outer meta propagates "
      "market_cap_missing into defaulted=True for the red 'estimated inputs' "
      "treatment OK")

print("SWEEP_DONE")
