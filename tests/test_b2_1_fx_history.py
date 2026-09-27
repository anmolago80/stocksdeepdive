"""Batch B2, item 1 (27 Sep 2026, owner-directed, CONFIRMED BUG): historic
P/E and IV/BV history was converted at TODAY's fx rate for every year
of a statement DataFrame, not that year's own period-end rate
(fundamentals_data.py's old _convert_statement_currency(df, rate) took
one flat rate and multiplied the whole DataFrame by it).

Fix: currency_risk_engine.historical_fx_rate(base, quote, target_date)
(new) looks up the rate for a SPECIFIC date from the same cached daily-
close series the Currency Risk page already maintains; fundamentals_
data._convert_statement_currency() now calls it once per COLUMN, using
that column's own period-end date, instead of taking a single
pre-fetched rate.

Reproduces the bug first (proves a flat "today's rate for every year"
conversion would have been wrong whenever the fx rate moved between
years), then proves the fix (each column genuinely uses its own rate).
Run: python3 test_b2_1_fx_history.py
"""
import os
import sys
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import currency_risk_engine as cre
import fundamentals_data as fd


# ---- currency_risk_engine.historical_fx_rate(): the new lookup itself ----
_FAKE_HIST = {
    "dates": ["2022-06-30", "2023-06-28", "2024-06-28", "2025-06-30"],
    "closes": [1.40, 1.50, 1.35, 1.45],
}

with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HIST):
    # Exact date match.
    rate, source = cre.historical_fx_rate("USD", "AUD", "2023-06-28")
    assert rate == 1.50 and source == "historical"
    # A date between two cached closes -> the nearest one ON OR BEFORE it.
    rate, source = cre.historical_fx_rate("USD", "AUD", "2024-01-15")
    assert rate == 1.50 and source == "historical"  # still 2023-06-28's close
    # A pd.Timestamp works too (not just a plain string).
    rate, source = cre.historical_fx_rate("USD", "AUD", pd.Timestamp("2025-06-30"))
    assert rate == 1.45 and source == "historical"
print("[historical_fx_rate_lookup] exact match, nearest-before, and a pd.Timestamp input all "
      "resolve to the correct cached close OK")

with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HIST):
    # A date OLDER than the whole cached history -> no cached close
    # qualifies (nothing "on or before" it) -> live fallback.
    with mock.patch("fcf_valuation_engine.fx_rate", return_value=(1.42, "live")):
        rate, source = cre.historical_fx_rate("USD", "AUD", "2020-01-01")
assert rate == 1.42 and source == "live_fallback"
print("[historical_fx_rate_too_old] a target_date older than any cached close falls back to "
      "fcf_valuation_engine.fx_rate()'s live rate, not a crash or a wrong nearby year OK")

with mock.patch.object(cre, "get_fx_history", return_value=None):
    with mock.patch("fcf_valuation_engine.fx_rate", return_value=(1.42, "live")):
        rate, source = cre.historical_fx_rate("USD", "AUD", "2023-06-28")
assert rate == 1.42 and source == "live_fallback"
print("[historical_fx_rate_no_history] a total fetch failure (get_fx_history() -> None) falls "
      "back to the live rate OK")

rate, source = cre.historical_fx_rate("AUD", "AUD", "2023-06-28")
assert rate == 1.0 and source == "identity"
print("[historical_fx_rate_identity] same currency both sides -> 1.0, never even looks at "
      "cached history OK")


# ---- fundamentals_data._convert_statement_currency(): per-column, not flat ----
YEARS = [pd.Timestamp("2022-06-30"), pd.Timestamp("2023-06-30"), pd.Timestamp("2024-06-30")]
df = pd.DataFrame(
    {YEARS[0]: [100.0, 500_000_000], YEARS[1]: [110.0, 500_000_000], YEARS[2]: [120.0, 500_000_000]},
    index=["Net Income", "Diluted Average Shares"],
)

# Each YEAR gets a DIFFERENT fx rate - the whole point of this fix.
_RATES_BY_YEAR = {YEARS[0]: 1.30, YEARS[1]: 1.50, YEARS[2]: 1.40}


def _fake_historical_rate(base, quote, target_date):
    return _RATES_BY_YEAR[pd.Timestamp(str(target_date)[:10])], "historical"


with mock.patch.object(fd.currency_risk_engine, "historical_fx_rate", side_effect=_fake_historical_rate):
    converted = fd._convert_statement_currency(df, "USD", "AUD")

# Reproduces the bug first: a flat "today's rate" (say, YEARS[2]'s 1.40)
# applied to EVERY column would have given Net Income[YEARS[0]] = 140.0,
# not the CORRECT 130.0 (100 * that year's own 1.30 rate) - the two
# numbers must differ, or this fixture isn't actually testing anything.
_flat_rate_bug_value = 100.0 * _RATES_BY_YEAR[YEARS[2]]  # what the OLD code would have produced
assert _flat_rate_bug_value != converted.loc["Net Income", YEARS[0]]
assert _flat_rate_bug_value == 140.0

# Now prove the FIX: each column uses its OWN year's rate.
assert converted.loc["Net Income", YEARS[0]] == 100.0 * 1.30 == 130.0
assert converted.loc["Net Income", YEARS[1]] == 110.0 * 1.50 == 165.0
assert converted.loc["Net Income", YEARS[2]] == 120.0 * 1.40 == 168.0
print("[per_period_conversion] each column converted at ITS OWN period's rate (130/165/168), "
      "NOT the flat today's-rate value a pre-fix run would have given every year (140 for "
      f"{YEARS[0].date()}) OK")

# Non-monetary rows (share counts) still bypass conversion entirely, same as before this fix.
assert converted.loc["Diluted Average Shares", YEARS[0]] == 500_000_000
assert converted.loc["Diluted Average Shares", YEARS[2]] == 500_000_000
print("[non_monetary_rows_unconverted] a share-count row is left untouched in every column, "
      "same protection Audit fix 1.7 already established OK")

# Empty / None input still degrades gracefully.
assert fd._convert_statement_currency(pd.DataFrame(), "USD", "AUD").empty
assert fd._convert_statement_currency(None, "USD", "AUD") is None
print("[empty_input_safe] an empty or missing DataFrame is returned unchanged, not a crash OK")


print("\nALL B2.1 FX-HISTORY FIXTURES PASSED")
