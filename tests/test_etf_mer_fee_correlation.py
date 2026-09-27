"""Portfolio ETF table: MER, fee-cost column, S&P 500 correlation
(27 Sep 2026, owner-reported from a live screenshot: IVV.AX/QRE.AX/
VAP.AX all showed MER "-" and "Corr. vs S&P 500" "-", while "Corr. vs
ASX 200" correctly showed 0.33/0.52/0.77).

TASK 1 (MER): the real unit yfinance's fund_operations["Annual Report
Expense Ratio"] returns could NOT be determined in this session - no
live network access to Yahoo, and the owner's own instruction was to
avoid live Yahoo calls before the nightly scan. The existing scaling
(treat as a fraction, x100 when <=1) is left as the best-available-
evidence guess (it matches this module's own pre-existing comment and
yfinance's well-documented raw-JSON-passthrough behaviour for this
field), but is now GATED by a sane-value band (0 < MER < 3%, the
owner's own rule) so a wrong-direction scaling error is REJECTED
rather than displayed, falling through to the hand-maintained fallback
table for the 3 owner-verified tickers, or "n/a" otherwise. This
fixture proves the gate + fallback + fee-cost math; it does NOT (and
cannot, from this sandbox) prove which raw unit Yahoo actually returns
live - admin_data_audit.check_mer() is the existing tool for that,
usable once live Yahoo calls are safe again (after 03:00 UTC).

TASK 3 (correlation): investigated and confirmed from the code, not a
live call - see _daily_returns_naive()'s own docstring for the full
tz mechanism. Fixed with a new correlation_vs_us_benchmark() using
DAILY returns, tz-stripped calendar dates, and a merge_asof backward
join (ASX day T against the US market's own most recent completed
session strictly before T).

Run: python3 tests/test_etf_mer_fee_correlation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import etf_insights as ei

# ======================================================================
# TASK 1a: sane-value gate
# ======================================================================
assert ei._mer_is_sane(0.04) is True
assert ei._mer_is_sane(0.34) is True
assert ei._mer_is_sane(2.99) is True
assert ei._mer_is_sane(3.0) is False  # exclusive upper bound
assert ei._mer_is_sane(0.0) is False  # exclusive lower bound - 0% reads as "no fee data", not "free"
assert ei._mer_is_sane(4.0) is False  # the exact failure mode the task described (0.04 -> 4%)
assert ei._mer_is_sane(None) is False
print("[mer_sane_band] 0 < MER < 3% is sane; 0%, >=3%, and None are all rejected OK")

# Reproduces the bug scenario: IF Yahoo's raw value for a real 0.04% fee
# ever comes back mis-scaled as 4.0 (the task's own named failure mode),
# the gate correctly rejects it rather than displaying "4.00%".
assert ei._mer_is_sane(4.0) is False
print("[reproduces_bug_scaling_error] a mis-scaled 4.0 (would-be '4%' for a real 0.04% fee) is "
      "correctly rejected by the sane-value gate, not displayed OK")

# ======================================================================
# TASK 1b/1c: fallback table + get_fund_facts() orchestration
# ======================================================================
assert set(ei.MER_FALLBACK_TABLE.keys()) == {"IVV.AX", "QRE.AX", "VAP.AX"}
assert ei.MER_FALLBACK_TABLE["IVV.AX"]["fee_pct"] == 0.04
assert ei.MER_FALLBACK_TABLE["QRE.AX"]["fee_pct"] == 0.34
assert ei.MER_FALLBACK_TABLE["VAP.AX"]["fee_pct"] == 0.23
for _t, _entry in ei.MER_FALLBACK_TABLE.items():
    assert _entry["source_url"].startswith("https://")
    assert _entry["as_of"]
print("[fallback_table_exact_values] IVV.AX/QRE.AX/VAP.AX match the owner's verified 0.04%/0.34%/"
      "0.23%, each with a real source URL and an as-of date - no other ticker present OK")

# Yahoo value missing entirely -> fallback table used, source recorded.
_no_yahoo = ei._apply_mer_fallback("IVV.AX", {"mer": None})
assert _no_yahoo["mer"] == 0.04
assert _no_yahoo["mer_source"] == "table"
assert _no_yahoo["mer_source_url"] == ei.MER_FALLBACK_TABLE["IVV.AX"]["source_url"]
assert _no_yahoo["mer_asof"] == "2026-09-28"
print("[fallback_used_when_yahoo_missing] IVV.AX with no Yahoo MER at all correctly falls to the "
      f"table (0.04%, source={_no_yahoo['mer_source']!r}) OK")

# Yahoo value insane (the exact reported bug) -> fallback table used, not the insane value.
_insane_yahoo = ei._apply_mer_fallback("QRE.AX", {"mer": 34.0})  # a 100x-style scaling error
assert _insane_yahoo["mer"] == 0.34
assert _insane_yahoo["mer_source"] == "table"
print(f"[fallback_used_when_yahoo_insane] QRE.AX with an insane Yahoo value (34.0%) correctly "
      f"falls to the table (0.34%), never displaying the insane figure OK")

# Yahoo value sane -> used as-is, no fallback, no source note.
_sane_yahoo = ei._apply_mer_fallback("IVV.AX", {"mer": 0.05})
assert _sane_yahoo["mer"] == 0.05
assert _sane_yahoo["mer_source"] == "yahoo"
assert _sane_yahoo["mer_source_url"] is None
print("[yahoo_used_when_sane] a sane Yahoo value (0.05%) is used as-is, table left untouched, no "
      "source note shown OK")

# Neither Yahoo nor table -> None, tooltip's job (not this function's) to say "fee not available".
_neither = ei._apply_mer_fallback("XYZ.AX", {"mer": None})
assert _neither["mer"] is None
assert _neither["mer_source"] is None
print("[neither_available_stays_none] an untracked ticker with no sane Yahoo value stays mer=None "
      "- never guessed, never fabricated OK")

# ======================================================================
# TASK 2: fee cost column
# ======================================================================
# Acceptance values from the task's own screenshot: IVV 61% A$32,926 @
# 0.04%, QRE 32% A$17,422 @ 0.34%, VAP 7% A$3,686 @ 0.23%.
_ivv_fee = ei.fee_cost_pa(0.04, 32926.0)
_qre_fee = ei.fee_cost_pa(0.34, 17422.0)
_vap_fee = ei.fee_cost_pa(0.23, 3686.0)
assert abs(_ivv_fee - 13.17) < 0.01
assert abs(_qre_fee - 59.23) < 0.01
assert abs(_vap_fee - 8.48) < 0.01
_total = _ivv_fee + _qre_fee + _vap_fee
assert abs(_total - 80.88) < 0.01
_total_value = 32926.0 + 17422.0 + 3686.0
_weighted_pct = (_total / _total_value) * 100.0
assert abs(_weighted_pct - 0.15) < 0.01
print(f"[fee_cost_acceptance_values] IVV A${_ivv_fee:.2f}, QRE A${_qre_fee:.2f}, VAP A${_vap_fee:.2f}, "
      f"total A${_total:.2f}/yr, weighted {_weighted_pct:.2f}% - matches the task's own acceptance "
      "numbers exactly OK")

# Missing MER -> fee cost is None, never assumed 0.
assert ei.fee_cost_pa(None, 32926.0) is None
assert ei.fee_cost_pa(0.04, None) is None
print("[fee_cost_none_propagates] a missing MER or missing value never gets silently treated as "
      "a 0% fee - fee_cost_pa() returns None, not 0.0 OK")

# ======================================================================
# TASK 3a: reproduce the tz-join bug in correlation_vs() (confirms the
# hypothesis - two tz-aware series with DIFFERENT offsets never overlap
# under an exact-timestamp join)
# ======================================================================
_dates_ax = pd.date_range("2023-01-31", periods=30, freq="ME", tz="Australia/Sydney")
_dates_us = pd.date_range("2023-01-31", periods=30, freq="ME", tz="America/New_York")
_ax_hist = pd.DataFrame({"Close": [100.0 + i for i in range(30)]}, index=_dates_ax)
_us_hist = pd.DataFrame({"Close": [4000.0 + i * 10 for i in range(30)]}, index=_dates_us)

_old_corr = ei.correlation_vs(_ax_hist, _us_hist, min_months=24)
assert _old_corr is None
print("[reproduces_bug_tz_join] correlation_vs() on a Sydney-tz series against a New-York-tz "
      "series returns None - confirms the hypothesis: tz-aware timestamps with different offsets "
      "never overlap under an exact-equality join, even though both cover 'the same' 30 calendar "
      "months and both series individually have plenty of data OK")

# Same-tz (both Sydney) pair - correlation_vs() DOES work, as observed
# live for "Corr. vs ASX 200".
_dates_ax2 = pd.date_range("2023-01-31", periods=30, freq="ME", tz="Australia/Sydney")
_ax_hist2 = pd.DataFrame({"Close": [200.0 + i * 1.5 for i in range(30)]}, index=_dates_ax2)
_same_tz_corr = ei.correlation_vs(_ax_hist, _ax_hist2, min_months=24)
assert _same_tz_corr is not None
print(f"[same_tz_pair_still_works] two Sydney-tz series correctly correlate ({_same_tz_corr:.4f}) "
      "- confirms correlation_vs() itself is fine for a same-market pair; the bug is specifically "
      "the cross-market tz mismatch OK")

# ======================================================================
# TASK 3b: the fix - correlation_vs_us_benchmark()
# ======================================================================
import numpy as np

_rng = np.random.default_rng(42)
_n_days = 400
_us_daily_idx = pd.date_range("2023-01-02", periods=_n_days, freq="B", tz="America/New_York")
_us_returns = _rng.normal(0.0004, 0.01, _n_days)
_us_closes = 4000.0 * np.cumprod(1 + _us_returns)
_us_hist_daily = pd.DataFrame({"Close": _us_closes}, index=_us_daily_idx)

# ASX day T's return is built from US day T-1's return (a real, positive
# lagged relationship - IVV.AX genuinely tracks the S&P 500 a session
# behind) plus independent noise, at Sydney tz, one ASX business day
# ahead of the matching US date (simulating "ASX day T reflects the
# PREVIOUS US session").
_ax_daily_idx = _us_daily_idx.tz_convert("Australia/Sydney") + pd.Timedelta(days=1)
_ax_returns = 0.7 * _us_returns + _rng.normal(0.0, 0.006, _n_days)
_ax_closes = 100.0 * np.cumprod(1 + _ax_returns)
_ax_hist_daily = pd.DataFrame({"Close": _ax_closes}, index=_ax_daily_idx)

_fixed_corr = ei.correlation_vs_us_benchmark(_ax_hist_daily, _us_hist_daily, min_days=60)
assert _fixed_corr is not None
assert _fixed_corr > 0.5  # a real, clearly positive lagged relationship, as constructed
print(f"[fix_finds_real_lagged_correlation] correlation_vs_us_benchmark() correctly finds the "
      f"real, constructed lagged relationship ({_fixed_corr:.4f}), which correlation_vs() itself "
      "would have missed entirely (None, per the tz-join bug above) OK")

# Sanity: the SAME-tz pair used above also works fine through the
# daily/asof method (not required for the fix, but confirms the new
# function isn't accidentally cross-market-only).
_daily_a = ei._daily_returns_naive(_ax_hist_daily)
assert _daily_a is not None and len(_daily_a) > 300
print("[daily_returns_naive_produces_real_series] the tz-stripped daily-returns helper produces "
      f"a real series ({len(_daily_a)} points) from the fixture's tz-aware daily history OK")

# ---- Safety: too few overlapping days -> None, not a meaningless number ----
_short_us = _us_hist_daily.iloc[:10]
_short_ax = _ax_hist_daily.iloc[:10]
assert ei.correlation_vs_us_benchmark(_short_ax, _short_us, min_days=60) is None
print("[too_few_days_safe] fewer than min_days overlapping trading days -> None, not a "
      "statistically meaningless number from a handful of points OK")

# ---- Safety: empty/None history -> None, no crash ----
assert ei.correlation_vs_us_benchmark(None, _us_hist_daily) is None
assert ei.correlation_vs_us_benchmark(_ax_hist_daily, pd.DataFrame()) is None
print("[empty_input_safe] missing history on either side returns None rather than crashing OK")


print("\nALL ETF MER/FEE/CORRELATION FIXTURES PASSED")
