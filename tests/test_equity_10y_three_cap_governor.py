"""
Rational Compounder (Equity 10y): book growth rate = lowest of three
caps (owner decision, 3 Oct 2026, instruction_pe_forward_5y_
compounder_caps.md, Commit 2).

g_eq = max(0, min(hist_equity_cagr, roe x retention, g_earn + 0.10))
- replacing the old single earnings-shaped growth_ceiling_for() cap on
auto_compounder_engine._equity_growth_rate()'s own historical CAGR.
_equity_growth_rate() itself no longer clamps anything (returns the
raw, CoV<=0.60-gated CAGR); the three-way min and the floor both now
live in _equity_10y_method().

Run: python3 tests/test_equity_10y_three_cap_governor.py
"""
import os
import sys
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace


def _mk_bundle(equity, net_income, shares, roe=None, currency="USD"):
    info = {"sharesOutstanding": shares, "currency": currency}
    if roe is not None:
        info["returnOnEquity"] = roe
    return {
        "info": info,
        "balance": pd.DataFrame({"2026-06-30": [equity]}, index=["Stockholders Equity"]),
        "income": pd.DataFrame({"2026-06-30": [net_income]}, index=["Net Income"]),
    }


# ======================================================================
# KNSL-shaped fixture (the instruction's own worked example): E=1.6B,
# NI=412M, shares=23M, history=25.1%, ROE=25%, payout=3% (retention
# 97%) -> roe x retention = 24.25% ~= 24.3%, g_earn=8.8% -> +10pts =
# 18.8% -> g_eq = min(25.1, 24.25, 18.8) = 18.8%, governor
# "earnings_plus10". _equity_growth_rate()/_avg_payout_ratio_5y() are
# mocked here so this fixture exercises ONLY the new three-way-min/
# governor logic in isolation, with exact control over each candidate -
# both are covered by their own dedicated fixtures further down.
# ======================================================================
_KNSL_E, _KNSL_NI, _KNSL_SHARES = 1.6e9, 412e6, 23e6
_knsl_bundle = _mk_bundle(_KNSL_E, _KNSL_NI, _KNSL_SHARES, roe=0.25)
_knsl_g_earn = 0.088

with mock.patch.object(ace, "_equity_growth_rate", return_value=(0.251, None)), \
     mock.patch.object(ace, "_avg_payout_ratio_5y", return_value=0.03):
    _knsl_result = ace._equity_10y_method(_knsl_bundle, _knsl_g_earn)
assert _knsl_result is not None
_knsl_value, _knsl_g_eq, _knsl_discount, _knsl_flagged, _knsl_reason, _knsl_governor, _knsl_candidates = (
    _knsl_result
)
assert _knsl_reason is None, _knsl_reason
assert abs(_knsl_g_eq - 0.188) < 1e-9, _knsl_g_eq
assert _knsl_governor == "earnings_plus10", _knsl_governor
assert _knsl_flagged is True, _knsl_flagged  # governed by something other than plain history
assert abs(_knsl_candidates["history"] - 0.251) < 1e-9, _knsl_candidates
assert abs(_knsl_candidates["roe_x_retention"] - 0.2425) < 1e-9, _knsl_candidates
assert abs(_knsl_candidates["earnings_plus10"] - 0.188) < 1e-9, _knsl_candidates
print(f"[knsl_governor_earnings_plus10] history=25.1%, ROE×retention=24.25%, "
      f"earnings+10pts=18.8% -> g_eq={_knsl_g_eq:.3f}, governor={_knsl_governor!r} "
      f"(instruction's own target: min=18.8%, governor earnings+10pts) OK")

# Sanity check on the value itself: a HIGHER g_eq (today's 18.8% cap)
# must produce a HIGHER value than the OLD 15.1%-earnings-ceiling-
# capped figure the instruction cites (~$407) would have - confirms
# the new cap is actually driving the equity_term upward, not just
# computed and discarded.
with mock.patch.object(ace, "_equity_growth_rate", return_value=(0.151, None)), \
     mock.patch.object(ace, "_avg_payout_ratio_5y", return_value=0.03):
    # Force the OLD-style single cap by making history itself the
    # (already-capped) 15.1% figure, well below the other two
    # candidates, so the three-way min reproduces the pre-fix number.
    _old_cap_result = ace._equity_10y_method(_knsl_bundle, _knsl_g_earn)
_old_cap_value = _old_cap_result[0]
assert _knsl_value > _old_cap_value, (_knsl_value, _old_cap_value)
print(f"[knsl_value_higher_than_old_cap] old 15.1%-ceiling-capped value ${_old_cap_value:.2f} "
      f"(instruction's own worked example: ~=$407) -> new 18.8%-capped value ${_knsl_value:.2f} "
      "(instruction's own worked example: ~=$470), strictly higher as expected from a higher "
      "book growth rate - this fixture's simplified bundle (no normalized_eps/forward-EPS "
      "inputs) doesn't reproduce the instruction's exact dollar figures, only the g_eq/governor "
      "wiring and the resulting direction/magnitude of the value change OK")

# ======================================================================
# KO-shaped fixture: history=3%, ROE=40%, payout=75% (retention 25%)
# -> roe x retention = 10%, g_earn=5% -> +10pts = 15% -> g_eq =
# min(3, 10, 15) = 3%, governor "history" - value UNCHANGED from
# today (the old ceiling never bound at 3% either, so the pre- and
# post-fix g_eq are identical).
# ======================================================================
_ko_bundle = _mk_bundle(500e6, 50e6, 100e6, roe=0.40)
with mock.patch.object(ace, "_equity_growth_rate", return_value=(0.03, None)), \
     mock.patch.object(ace, "_avg_payout_ratio_5y", return_value=0.75):
    _ko_result = ace._equity_10y_method(_ko_bundle, 0.05)
_ko_value, _ko_g_eq, _ko_discount, _ko_flagged, _ko_reason, _ko_governor, _ko_candidates = _ko_result
assert _ko_reason is None, _ko_reason
assert abs(_ko_g_eq - 0.03) < 1e-9, _ko_g_eq
assert _ko_governor == "history", _ko_governor
assert _ko_flagged is False, _ko_flagged  # plain historical rate governed -> not an "estimate"
assert abs(_ko_candidates["roe_x_retention"] - 0.10) < 1e-9, _ko_candidates
assert abs(_ko_candidates["earnings_plus10"] - 0.15) < 1e-9, _ko_candidates
# Confirm "unchanged from today": forcing the SAME 3% through the old
# single-cap shape (nothing else can bind below it) gives the identical
# g_eq, hence the identical value.
with mock.patch.object(ace, "_equity_growth_rate", return_value=(0.03, None)), \
     mock.patch.object(ace, "_avg_payout_ratio_5y", return_value=None):
    _ko_result_no_div = ace._equity_10y_method(_ko_bundle, 0.05)
assert abs(_ko_result_no_div[1] - _ko_g_eq) < 1e-9  # retention=1.0 doesn't matter - history still wins
assert abs(_ko_result_no_div[0] - _ko_value) < 1e-6
print(f"[ko_governor_history] history=3%, ROE×retention=10%, earnings+10pts=15% -> "
      f"g_eq={_ko_g_eq:.3f}, governor={_ko_governor!r}, value unchanged whether or not the "
      "dividend/retention data is even present (history already the strict minimum) OK")

# ======================================================================
# No-dividend-history fixture: _avg_payout_ratio_5y() returns None
# (real code path, not mocked - see the dedicated fixture below) ->
# retention defaults to 1.0 -> roe x retention = roe itself.
# ======================================================================
with mock.patch.object(ace, "_equity_growth_rate", return_value=(0.20, None)):
    _no_div_result = ace._equity_10y_method(
        _mk_bundle(1e9, 100e6, 50e6, roe=0.30), 0.08)
_no_div_g_eq, _no_div_governor, _no_div_candidates = (
    _no_div_result[1], _no_div_result[5], _no_div_result[6])
assert abs(_no_div_candidates["roe_x_retention"] - 0.30) < 1e-9, _no_div_candidates  # retention=1.0
# candidates: history=20%, roe_x_retention=30% (bare ROE, retention=1.0
# with no dividend history at all), earnings_plus10=8%+10pts=18% -> min
# is earnings_plus10, unambiguously (no tie).
assert abs(_no_div_g_eq - 0.18) < 1e-9, _no_div_g_eq
assert _no_div_governor == "earnings_plus10", _no_div_governor
print(f"[no_dividend_history_retention_1] no dividends at all -> retention=1.0, "
      f"ROE×retention candidate = bare ROE (30.0%) -> g_eq={_no_div_g_eq:.3f}, "
      f"governor={_no_div_governor!r} OK")

# ======================================================================
# ROE missing fixture: that cap is skipped entirely (not merely
# defaulted) - only history and earnings+10pts compete.
# ======================================================================
with mock.patch.object(ace, "_equity_growth_rate", return_value=(0.50, None)), \
     mock.patch.object(ace, "_avg_payout_ratio_5y", return_value=0.0):
    _no_roe_result = ace._equity_10y_method(_mk_bundle(1e9, 100e6, 50e6, roe=None), 0.10)
_no_roe_g_eq, _no_roe_governor, _no_roe_candidates = (
    _no_roe_result[1], _no_roe_result[5], _no_roe_result[6])
assert _no_roe_candidates["roe_x_retention"] is None, _no_roe_candidates
assert abs(_no_roe_g_eq - 0.20) < 1e-9, _no_roe_g_eq  # min(50%, g_earn+10pts=20%) = 20%, ROE skipped
assert _no_roe_governor == "earnings_plus10", _no_roe_governor
print(f"[roe_missing_cap_skipped] returnOnEquity absent -> roe_x_retention candidate is None "
      f"(not a fabricated default) -> g_eq={_no_roe_g_eq:.3f} from the remaining two candidates "
      "only OK")

# ======================================================================
# Negative-history fixture: all three candidates negative (or the min
# of the available ones is negative) -> floored at 0, governor "floor".
# ======================================================================
with mock.patch.object(ace, "_equity_growth_rate", return_value=(-0.10, None)), \
     mock.patch.object(ace, "_avg_payout_ratio_5y", return_value=0.0):
    _neg_result = ace._equity_10y_method(_mk_bundle(1e9, 100e6, 50e6, roe=None), -0.20)
_neg_g_eq, _neg_governor = _neg_result[1], _neg_result[5]
assert abs(_neg_g_eq - 0.0) < 1e-9, _neg_g_eq
assert _neg_governor == "floor", _neg_governor
print(f"[negative_history_floored_at_zero] history=-10%, earnings+10pts=-10% (g_earn=-20%) -> "
      f"min is negative -> g_eq floored to {_neg_g_eq:.3f}, governor={_neg_governor!r} OK")

# ======================================================================
# _avg_payout_ratio_5y(): real (unmocked) code path - no dividend
# history at all -> None; a real 2-year dividend history against a
# matching 2-year clean EPS series -> the correct per-year-average
# payout ratio.
# ======================================================================
_income_df = pd.DataFrame(
    {
        "2026-06-30": [10.0, 100.0, 10.0], "2025-06-30": [10.0, 100.0, 10.0],
        "2024-06-30": [10.0, 100.0, 10.0],
    },
    index=["Diluted EPS", "Operating Income", "Reconciled Depreciation"],
)
_bundle_no_div = {"info": {}, "income": _income_df, "dividends": {"dates": [], "amounts": []}}
_neps_no_div = ace._normalized_eps(_bundle_no_div)
assert _neps_no_div["source"] == "median5_clean", _neps_no_div  # 3 clean years -> real growth_series
assert ace._avg_payout_ratio_5y(_bundle_no_div, _neps_no_div) is None
print("[avg_payout_ratio_no_dividend_history] empty dividends dict -> None (caller defaults "
      "retention to 1.0, never a fabricated 0% payout) OK")

_bundle_with_div = dict(_bundle_no_div)
_bundle_with_div["dividends"] = {
    # DPS 4.0 in 2026, 2.0 in 2025, no payment at all in 2024 - a real
    # non-payment year counts as a genuine 0% payout that year, not a
    # skipped one (see _avg_payout_ratio_5y's own docstring).
    "dates": ["2026-03-15", "2025-03-15"], "amounts": [4.0, 2.0],
}
_neps_with_div = ace._normalized_eps(_bundle_with_div)
_avg_payout = ace._avg_payout_ratio_5y(_bundle_with_div, _neps_with_div)
# EPS is flat $10/share every year -> payout ratios 4.0/10=0.40,
# 2.0/10=0.20, 0.0/10=0.00 -> average (0.40+0.20+0.00)/3 = 0.20.
assert abs(_avg_payout - 0.20) < 1e-9, _avg_payout
print(f"[avg_payout_ratio_real_history] DPS 4.0/2.0/0 (a real non-payment year included as "
      f"0%, not skipped) against flat $10 EPS over 3 fiscal years -> average payout ratio "
      f"{_avg_payout:.2f} (expected 0.20) OK")

print("\nALL EQUITY 10Y THREE-CAP GOVERNOR FIXTURES PASSED")
