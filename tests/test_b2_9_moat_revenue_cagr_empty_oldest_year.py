"""Batch B2, item 9 (27 Sep 2026, owner-directed, CONFIRMED BUG): the
Moat pricing-power pillar's revenue CAGR used the oldest column even when
it was empty.

moat_engine._pillar_pricing_power() used to compute
`oldest_rev = revenue_s.get(years_desc[-1])` - the OLDEST column by
POSITION, regardless of whether that specific year's revenue was
actually reported. The earliest year in a statement history is exactly
the one most likely to have a gap (a shorter reporting history, a data
source cutoff, etc.). When it was empty, the guard
(`if newest_rev and oldest_rev and ...`) correctly avoided a crash or a
wrong number, but it gave up on revenue_cagr ENTIRELY (None) - discarding
a real, computable CAGR the remaining, non-empty years could still
support, and silently knocking the pricing-power pillar's growth bonus
down a band (grow_full -> grow_mid) for no reason connected to the
company's actual revenue growth.

Reproduces the bug first (the OLD oldest-column-regardless-of-emptiness
logic would have given up on revenue_cagr, and the wrong growth_points
band as a result), then proves the fix walks back to the oldest
NON-EMPTY year and uses the TRUE span to that year.
Run: python3 tests/test_b2_9_moat_revenue_cagr_empty_oldest_year.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import moat_engine
import auto_compounder_engine as ace

# 5 fiscal years, newest first. The OLDEST year (Y1) has NO revenue
# reported at all (None) - the exact gap this bug is about. Y2 (the
# oldest NON-empty year) has a real, materially different revenue from
# the newest year, so the fix's CAGR is unambiguously nonzero and
# unambiguously different from "give up entirely".
YEARS = ["Y5", "Y4", "Y3", "Y2", "Y1"]
REVENUE = {"Y5": 200.0, "Y4": 170.0, "Y3": 140.0, "Y2": 120.0, "Y1": None}
# Flat 40% margin across every year that HAS revenue, so the held/
# stability/gm_change gates all stay neutral (>= -1) and the ONLY thing
# that can move growth_points is revenue_cagr itself.
GROSS_PROFIT = {"Y5": 80.0, "Y4": 68.0, "Y3": 56.0, "Y2": 48.0, "Y1": None}

REVENUE_SERIES = [(y, REVENUE[y]) for y in YEARS]
GP_SERIES = [(y, GROSS_PROFIT[y]) for y in YEARS]

INCOME_DF = object()  # opaque - every real read goes through the mocked _series/_find_row below


def _fake_series(df, key):
    if key == "revenue":
        return REVENUE_SERIES
    if key == "Gross Profit":
        return GP_SERIES
    return []


BUNDLE = {"income": INCOME_DF}


def _run():
    flags = []
    with mock.patch.object(ace, "_series", side_effect=_fake_series), \
         mock.patch.object(ace, "_find_row", return_value="Gross Profit"):
        return moat_engine._pillar_pricing_power(BUNDLE, is_financials=False, flags=flags, force_pricing_level=False)


points, pillar_max = _run()

# ---- The fix: revenue_cagr is computed from the oldest NON-EMPTY year
# (Y2, 120.0), spanning 3 years (Y5 at position 0 to Y2 at position 3) -
# (200/120)**(1/3) - 1 ~= 18.6%, comfortably over the 5% growth_points
# threshold -> growth_points = grow_full (5, level switch off). ----
_expected_cagr = (200.0 / 120.0) ** (1 / 3) - 1
assert _expected_cagr >= 0.05
# held_full(10) + stability_full(10) + grow_full(5) = 25, flat margin -> full marks on held/stability too.
assert points == 25
assert pillar_max == 25
print(f"[revenue_cagr_uses_oldest_nonempty_year] with Y1's revenue missing, the fix still finds "
      f"a real CAGR ({_expected_cagr:.1%}) from Y2 (the oldest year that actually has data), "
      f"giving the full growth bonus - points={points}/{pillar_max} OK")

# Reproduces the bug: the OLD code's oldest_rev = revenue_s.get(years_desc[-1])
# = REVENUE["Y1"] = None -> guard fails -> revenue_cagr stays None ->
# growth_points = grow_mid (2), not grow_full (5) - a real, wrong
# 3-point pillar-score difference purely from one empty column.
_old_buggy_oldest_rev = REVENUE[YEARS[-1]]
assert _old_buggy_oldest_rev is None
_old_buggy_points = 10 + 10 + 2  # held_full + stability_full + grow_mid
assert points != _old_buggy_points
print(f"[reproduces_bug] the OLD code's oldest_rev = revenue_s.get(years_desc[-1]) = "
      f"{_old_buggy_oldest_rev} (Y1's missing value) would have given up on revenue_cagr "
      f"entirely, scoring {_old_buggy_points}/25 instead of the fixed {points}/25 OK")

# ---- Sanity: when EVERY year but the newest is empty, the fix still
# gives up safely (no crash, no fabricated CAGR), same as before ----
REVENUE_ALL_EMPTY = {"Y5": 200.0, "Y4": None, "Y3": None, "Y2": None, "Y1": None}
GP_ALL_EMPTY = {"Y5": 80.0, "Y4": None, "Y3": None, "Y2": None, "Y1": None}
REVENUE_SERIES_EMPTY = [(y, REVENUE_ALL_EMPTY[y]) for y in YEARS]
GP_SERIES_EMPTY = [(y, GP_ALL_EMPTY[y]) for y in YEARS]


def _fake_series_empty(df, key):
    if key == "revenue":
        return REVENUE_SERIES_EMPTY
    if key == "Gross Profit":
        return GP_SERIES_EMPTY
    return []


with mock.patch.object(ace, "_series", side_effect=_fake_series_empty), \
     mock.patch.object(ace, "_find_row", return_value="Gross Profit"):
    flags_empty = []
    result_empty = moat_engine._pillar_pricing_power(
        BUNDLE, is_financials=False, flags=flags_empty, force_pricing_level=False,
    )
# Only one usable margin year (Y5) exists -> gm_series has len 1 -> pillar dropped entirely.
assert result_empty is None
print("[all_older_years_empty_safe] when every year but the newest is empty, the fix still "
      "safely drops the pillar (fewer than 2 usable margin years) rather than fabricating a "
      "CAGR from a single data point OK")


print("\nALL B2.9 MOAT-REVENUE-CAGR-EMPTY-OLDEST-YEAR FIXTURES PASSED")
