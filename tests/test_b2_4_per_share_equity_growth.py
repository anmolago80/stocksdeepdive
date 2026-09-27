"""Batch B2, item 4 (27 Sep 2026, owner-directed, CONFIRMED BUG): per-share
equity growth used TODAY's share count for every historical year, and
counted CAGR years from column COUNT instead of column DATES.

auto_compounder_engine._equity_growth_rate() divided every year's raw
stockholders' equity by info["sharesOutstanding"] (today's count) - a
company that issued or bought back shares over the window got a wrong
per-share series for every year except the newest. It also counted
elapsed years as len(equity_series)-1, understating a real gap in the
statement history.

fcf_valuation_engine.growth_from_history() had the identical years-
from-column-count issue; fixed with a NEW OPTIONAL `dates=` parameter,
NOT yet wired into any live caller (see that function's own docstring
for why) - so this file also confirms the live DCF path's behavior is
completely unchanged.

Reproduces each bug first, then proves the fix.
Run: python3 test_b2_4_per_share_equity_growth.py
"""
import datetime
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace
import fcf_valuation_engine as fve

# ---- _equity_growth_rate(): today's-share-count bug ----
# 3 fiscal years, newest first. Equity grows $100 -> $150 -> $200 (looks
# like healthy, steady growth on a RAW basis) but the company issued a
# LOT of new shares along the way: 50 -> 75 -> 100 shares. Per-share
# equity is therefore FLAT the whole time ($2.00/share every year) - the
# "growth" is entirely share dilution, not real per-share value creation.
EQUITY_DATES = [datetime.date(2026, 6, 30), datetime.date(2025, 6, 30), datetime.date(2024, 6, 30)]
EQUITY_VALUES = [200.0, 150.0, 100.0]  # newest first
SHARES_BY_DATE = {EQUITY_DATES[0]: 100.0, EQUITY_DATES[1]: 75.0, EQUITY_DATES[2]: 50.0}
TODAY_SHARES = 100.0  # info["sharesOutstanding"] - only correct for the NEWEST year

BUNDLE = {"balance": object(), "info": {"sharesOutstanding": TODAY_SHARES, "currency": "USD", "marketCap": 500_000_000}}


def _fake_series_with_dates(df, key):
    if key == "stockholders_equity":
        return list(zip(EQUITY_DATES, EQUITY_VALUES))
    if key == "ordinary_shares_number":
        return [(d, SHARES_BY_DATE[d]) for d in EQUITY_DATES]
    return []


with mock.patch.object(ace, "_series_with_dates", side_effect=_fake_series_with_dates), \
     mock.patch.object(fve, "growth_ceiling_for", return_value=0.20), \
     mock.patch.object(fve, "_coeff_of_variation", return_value=0.0):
    g_eq, capped = ace._equity_growth_rate(BUNDLE)

# Reproduces the bug: dividing every year by TODAY's 100 shares gives a
# per-share series of $1.00 / $1.50 / $2.00 - a fake ~41% CAGR from pure
# share-count arithmetic, even though per-share value never moved.
_old_buggy_per_share = [v / TODAY_SHARES for v in EQUITY_VALUES]
_old_buggy_cagr = (_old_buggy_per_share[0] / _old_buggy_per_share[-1]) ** (1 / 2) - 1
assert g_eq is not None
assert abs(g_eq - _old_buggy_cagr) > 0.05  # genuinely different from the old buggy figure
# The FIX: each year's own share count -> per-share equity is FLAT ($2.00
# every year) -> a true 0% growth rate (floored, per GROWTH_FLOOR).
assert abs(g_eq - 0.0) < 1e-9
print(f"[per_share_uses_own_year_shares] flat $2.00/share every year (true per-share value "
      f"never moved) correctly gives 0% growth, not the old buggy {_old_buggy_cagr:.4f} that "
      "dividing by today's 100 shares for every year would have produced OK")

# ---- _equity_growth_rate(): column-count vs real-date years ----
# Same 3 equity values, but this time the OLDEST column is 4 calendar
# years back (a missing year in between), not 2 - genuinely different n.
GAP_DATES = [datetime.date(2026, 6, 30), datetime.date(2025, 6, 30), datetime.date(2022, 6, 30)]


def _fake_series_with_dates_gap(df, key):
    if key == "stockholders_equity":
        return list(zip(GAP_DATES, EQUITY_VALUES))
    if key == "ordinary_shares_number":
        return [(d, TODAY_SHARES) for d in GAP_DATES]  # constant shares - isolates the date-vs-count fix
    return []


with mock.patch.object(ace, "_series_with_dates", side_effect=_fake_series_with_dates_gap), \
     mock.patch.object(fve, "growth_ceiling_for", return_value=0.20), \
     mock.patch.object(fve, "_coeff_of_variation", return_value=0.0):
    g_eq_gap, _ = ace._equity_growth_rate(BUNDLE)

# Reproduces the bug: len(equity_series)-1 = 2 "years", even though the
# real span (2022 -> 2026) is 4 calendar years - overstates the CAGR.
_old_buggy_n = 2
_old_buggy_cagr_gap = (200.0 / 100.0) ** (1 / _old_buggy_n) - 1
_correct_n = (GAP_DATES[0] - GAP_DATES[-1]).days / 365.25
_correct_cagr_gap = (200.0 / 100.0) ** (1 / _correct_n) - 1
assert g_eq_gap is not None
assert abs(g_eq_gap - min(_old_buggy_cagr_gap, 0.20)) > 0.01  # different from the column-count answer
assert abs(g_eq_gap - min(_correct_cagr_gap, 0.20)) < 1e-6
print(f"[years_from_real_dates] a 4-calendar-year gap (2022->2026, 3 columns) correctly uses "
      f"~4 real years, not the old buggy column-count of 2 (which would have overstated the "
      f"CAGR as {_old_buggy_cagr_gap:.4f} instead of {_correct_cagr_gap:.4f}) OK")

# ---- growth_from_history(): optional dates parameter, live path unchanged ----
FCF_HISTORY = [200.0, 150.0, 100.0]  # newest first, 3 points -> old code: years=2
FCF_DATES_REGULAR = [datetime.date(2026, 6, 30), datetime.date(2025, 6, 30), datetime.date(2024, 6, 30)]
FCF_DATES_GAPPED = [datetime.date(2026, 6, 30), datetime.date(2025, 6, 30), datetime.date(2022, 6, 30)]

# No dates passed (every EXISTING/live caller today) -> EXACT same
# column-count behavior as before this fix, byte-for-byte.
g_no_dates = fve.growth_from_history(FCF_HISTORY)
g_regular_dates = fve.growth_from_history(FCF_HISTORY, dates=FCF_DATES_REGULAR)
assert g_no_dates == (200.0 / 100.0) ** (1 / 2) - 1
# Regularly-spaced real dates agree CLOSELY (not bit-for-bit - 2 real
# calendar years is 730 or 731 days depending on the leap-year mix,
# never exactly 2*365.25) with the column-count answer.
assert abs(g_no_dates - g_regular_dates) < 0.001
print("[growth_from_history_backward_compatible] omitting dates (every live caller today) gives "
      "the exact unchanged column-count answer; regularly-spaced real dates agree with it "
      "anyway OK")

g_gapped_dates = fve.growth_from_history(FCF_HISTORY, dates=FCF_DATES_GAPPED)
_correct_fcf_years = (FCF_DATES_GAPPED[0] - FCF_DATES_GAPPED[-1]).days / 365.25
_correct_fcf_cagr = (200.0 / 100.0) ** (1 / _correct_fcf_years) - 1
assert abs(g_gapped_dates - _correct_fcf_cagr) < 1e-9
assert g_gapped_dates != g_no_dates  # a real gap DOES change the answer when dates are supplied
print("[growth_from_history_dates_fix_a_real_gap] when dates ARE supplied and reveal a real "
      "calendar gap, the CAGR correctly uses the true elapsed years, differing from the "
      "column-count answer OK")

# Mismatched-length dates (a caller passing garbage) -> safe fallback, never a crash or wrong math.
g_mismatched = fve.growth_from_history(FCF_HISTORY, dates=[datetime.date(2026, 1, 1)])
assert g_mismatched == g_no_dates
print("[growth_from_history_mismatched_dates_safe] dates of the wrong length fall back to the "
      "column-count behavior rather than raising or guessing OK")


print("\nALL B2.4 PER-SHARE-EQUITY-GROWTH FIXTURES PASSED")
