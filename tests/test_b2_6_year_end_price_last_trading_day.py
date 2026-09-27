"""Batch B2, item 6 (27 Sep 2026, owner-directed, CONFIRMED BUG): year-end
prices were taken from the FIRST trading day of the month, not the LAST
trading day on or before each fiscal year-end.

fundamentals_data._monthly_series() used to build its ~10y "one point per
month" series with `hist_df["Close"].resample("MS").first()` - the FIRST
close of each calendar month, dated to that month's start.
auto_compounder_engine._year_end_prices() then matches each fiscal
year-end to the latest available point "on or before" that end date - so
for a fiscal year ending, say, 30 June, the closest point at or before
that date used to be the close from the FIRST trading day of June (as
early as 1 June), rather than the close nearest the real 30 June
year-end - up to a month of price drift feeding into PE Ratio, Fair
Value's PE-based method, Retained Earnings' Value Created chart, and
Cost of Capital's WACC-by-year.

The fix groups by (year, month) and keeps each month's LAST real trading
close - but labels that price with the CALENDAR month's own last day
(e.g. "2024-06-30"), not the real trading date the close happened on.
That label choice is deliberate, not a shortcut: it keeps a stock's
monthly grid and the S&P 500's monthly grid on IDENTICAL date strings for
the same (year, month) even when the two exchanges' actual last trading
day of a month differs (different public holidays) - _cov_corr() joins
the two series by exact date string, and a real-trading-date label would
silently break that join exactly when the calendars diverge (the same
class of bug the module's own tz-strip logic was written to prevent).

Reproduces the bug first (the OLD first-trading-day-of-month behavior
would have picked a materially different price than the fix does), then
proves the fix, then confirms both the downstream year-end matching in
_year_end_prices() and the cross-exchange join safety in _cov_corr()'s
own join logic benefit from it.
Run: python3 tests/test_b2_6_year_end_price_last_trading_day.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import fundamentals_data as fd
import auto_compounder_engine as ace

# A June 2024 trading month, weekdays only, price trending UP across the
# month ($100 on the 1st weekday up to $119 on the last) so first-vs-last
# day give a visibly, meaningfully different value - not a rounding blip.
_june_days = [d for d in pd.date_range("2024-06-01", "2024-06-28", freq="D") if d.weekday() < 5]
_june_closes = [100.0 + i for i in range(len(_june_days))]

# A second month (July) so the series has >1 point, same construction.
_july_days = [d for d in pd.date_range("2024-07-01", "2024-07-31", freq="D") if d.weekday() < 5]
_july_closes = [200.0 + i for i in range(len(_july_days))]

_all_days = _june_days + _july_days
_all_closes = _june_closes + _july_closes
HIST_DF = pd.DataFrame({"Close": _all_closes}, index=pd.DatetimeIndex(_all_days))

result = fd._monthly_series(HIST_DF)

# ---- The fix: June's point is PRICED at its LAST real trading day's
# close (28 June, a Friday - 29th/30th are the weekend) ----
assert result["prices"][0] == _june_closes[-1]  # 119.0, not the first trading day's 100.0
print(f"[last_trading_day_priced] June's point is priced at {_june_closes[-1]} (the real last "
      "trading day's close), not the first trading day's close OK")

# ---- But LABELED at the calendar month's own last day, not the real
# trading date (28th) - deliberate, for cross-exchange join safety ----
assert result["dates"][0] == "2024-06-30T00:00:00"
print("[labeled_at_calendar_month_end] the date label is 2024-06-30 (June's calendar month-end), "
      "not 2024-06-28 (the real trading date the close happened on) OK")

# Reproduces the bug: the OLD resample("MS").first() code would have
# produced 100.0 (June's first trading day's close) instead.
_old_buggy_price = _june_closes[0]  # 100.0
assert result["prices"][0] != _old_buggy_price
assert abs(result["prices"][0] - _old_buggy_price) > 15  # a real, material price difference
print(f"[reproduces_bug] the OLD code would have used {_old_buggy_price} (first trading day) - "
      f"a {result['prices'][0] - _old_buggy_price:.0f}-point difference from using the last "
      "trading day instead OK")

# ---- July's point likewise uses its own last real trading day's close ----
assert result["dates"][1] == "2024-07-31T00:00:00"
assert result["prices"][1] == _july_closes[-1]
print("[second_month_also_fixed] July's point is likewise priced at its own last real trading "
      "day's close, not just a one-off for the June fixture OK")

# ---- Downstream: _year_end_prices() actually benefits from this ----
# A fiscal year ending 2024-06-30 (the ASX-standard 30 June fiscal
# year-end) should now match June's LAST trading day close (119.0), not
# a stale first-of-month close.
income_df = pd.DataFrame({"2024-06-30": [1.0]}, index=["Basic EPS"])
year_end_prices = ace._year_end_prices(result, income_df)
assert year_end_prices.get("2024") == _june_closes[-1]
print(f"[year_end_prices_benefits] a 30 June fiscal year-end now correctly matches June's own "
      f"last-trading-day close ({_june_closes[-1]}), not a stale early-June open-of-month price "
      "OK")

# ---- Cross-exchange join safety: _cov_corr() joins a stock's series
# against the S&P 500's by exact date string. If a different exchange's
# last real trading day of June fell on a DIFFERENT date (e.g. a public
# holiday specific to that market), the calendar-month-end label still
# makes the two series share the exact same date string for June - a
# real-trading-date label would NOT have. ----
_alt_exchange_days = _june_days[:-1]  # this "exchange" stopped trading on the 27th, not the 28th
_alt_exchange_closes = _june_closes[:-1]
alt_df = pd.DataFrame({"Close": _alt_exchange_closes}, index=pd.DatetimeIndex(_alt_exchange_days))
alt_result = fd._monthly_series(alt_df)
assert alt_result["dates"][0] == result["dates"][0]
print("[cross_exchange_join_safe] two 'exchanges' whose real last trading day of June differs "
      "(28th vs 27th) still produce the IDENTICAL June date label - _cov_corr()'s date-string "
      "join between a stock and the S&P 500 survives a calendar mismatch that a real-trading-date "
      "label would have silently broken OK")

# ---- Safety: empty/missing input never crashes ----
assert fd._monthly_series(None) == {"dates": [], "prices": []}
assert fd._monthly_series(pd.DataFrame()) == {"dates": [], "prices": []}
print("[empty_input_safe] missing or empty history returns an empty series, not a crash OK")


print("\nALL B2.6 YEAR-END-PRICE-LAST-TRADING-DAY FIXTURES PASSED")
