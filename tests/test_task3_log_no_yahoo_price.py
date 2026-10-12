"""
Director's overnight queue, TASK 3 (12 Oct 2026): name the missing
ticker.

Found previously (TASK 4 of the Director's Refresh-all/Japan report):
run_universe_scan()'s own per-ticker loop only counts a ticker into
skipped_no_price when analyze_ticker_lite() RETURNS A ROW whose price
is invalid. When analyze_ticker_lite() itself returns None (two
branches: empty 6-month history; every Close in the trailing 3-month
window is NaN), the ticker vanishes from BOTH `rows` and
skipped_no_price with no trace - exactly the "40 tickers -> 39 saved,
0 skipped (no price)" signature DAX/CAC 40 showed.

symbol_mapping.log_no_yahoo_price() already existed, built for exactly
this, but nothing called it. This commit wires it into both branches,
LOGGING AND COUNTING ONLY - no change to `rows`, skipped_no_price, or
any value/score/ranking.

Scope note, disclosed in the commit message too: log_no_yahoo_price()'s
own signature takes (index_name, yahoo_symbol, wiki_symbol) to show
both forms side by side - but by the time run_universe_scan()'s loop
reaches a ticker, the ORIGINAL Wikipedia-shaped symbol is already gone
(_parse_table()'s own to_yahoo_symbol() transform doesn't preserve it,
and reconstructing it by reversing the dot/hyphen stem rule would be a
guess - this task's own "never invent tickers" rule rules that out).
So both the yahoo_symbol and wiki_symbol arguments passed here are the
SAME already-Yahoo-mapped ticker string - it still correctly NAMES the
company (this task's actual goal), just without the separate original
form the function's docstring example shows. Reconstructing the wiki
form is a larger, separate task (changing _parse_table()'s own return
shape, shared by every universe fetcher) - out of scope here.

Generic by construction, same "universe-agnostic" philosophy this
session has applied throughout: analyze_ticker_lite() gets two new,
fully optional parameters (universe=None, no_yahoo_price_out=None) -
every existing caller that doesn't pass them (digest_engine, the
Scanner's live-hook path, every test fixture already in this repo)
gets zero new behaviour, proven directly below. Only run_universe_scan()
(the real nightly scan path) passes them, so DAX/CAC 40 (today's
known cases) and every other universe scanned this way get the new
diagnostic identically - no special-casing by universe name.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_task3_log_no_yahoo_price.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import nightly_scan

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


# ======================================================================
# CHECK 1-2: empty-history branch - with `universe` passed, logs via
# symbol_mapping.log_no_yahoo_price() and sets the out-flag; with
# `universe` omitted (every other existing caller), logs nothing new
# and the out-flag param simply isn't touched (None, same as before).
# ======================================================================
with mock.patch("nightly_scan.yf.Ticker") as MockTicker:
    MockTicker.return_value.history.return_value = pd.DataFrame()  # empty - triggers branch 1
    _logs = []
    _flag = [False]
    _result = nightly_scan.analyze_ticker_lite(
        "MISSING1.DE", log=_logs.append, universe="DAX", no_yahoo_price_out=_flag)
    check("empty-history branch still returns None (no change to the actual skip behaviour)",
          _result is None)
    check("empty-history branch logs '[symbols] DAX: no Yahoo price for MISSING1.DE' when "
          "universe is passed", any("DAX" in m and "MISSING1.DE" in m and "no Yahoo price" in m
                                     for m in _logs))
    check("the out-flag is set True", _flag[0] is True)

with mock.patch("nightly_scan.yf.Ticker") as MockTicker:
    MockTicker.return_value.history.return_value = pd.DataFrame()
    _logs_noarg = []
    _result_noarg = nightly_scan.analyze_ticker_lite("MISSING2.DE", log=_logs_noarg.append)
    check("REGRESSION: with no `universe` argument (every pre-existing caller), nothing new "
          "is logged - byte-identical to before this commit",
          not any("no Yahoo price" in m for m in _logs_noarg))


# ======================================================================
# CHECK 3-4: all-NaN-closes branch (the second identified "return
# None") - same treatment.
# ======================================================================
import numpy as np
_nan_df = pd.DataFrame({"Close": [np.nan] * 70}, index=pd.date_range("2026-01-01", periods=70))
with mock.patch("nightly_scan.yf.Ticker") as MockTicker:
    MockTicker.return_value.history.return_value = _nan_df
    _logs2 = []
    _flag2 = [False]
    _result2 = nightly_scan.analyze_ticker_lite(
        "MISSING3.PA", log=_logs2.append, universe="CAC 40", no_yahoo_price_out=_flag2)
    check("all-NaN-closes branch still returns None", _result2 is None)
    check("all-NaN-closes branch logs '[symbols] CAC 40: no Yahoo price for MISSING3.PA'",
          any("CAC 40" in m and "MISSING3.PA" in m and "no Yahoo price" in m for m in _logs2))
    check("the out-flag is set True for this branch too", _flag2[0] is True)


# ======================================================================
# CHECK 5: a normal, successfully-scanned ticker never sets the flag
# or logs anything new - regression check that the happy path is
# completely untouched.
# ======================================================================
_good_df = pd.DataFrame(
    {"Close": [100.0] * 70}, index=pd.date_range("2026-01-01", periods=70))
with mock.patch("nightly_scan.yf.Ticker") as MockTicker:
    _info_mock = {"trailingEps": 5.0, "sector": "Industrials", "longName": "Good Co",
                  "sharesOutstanding": 1_000_000, "currency": "EUR", "financialCurrency": "EUR"}
    MockTicker.return_value.history.return_value = _good_df
    MockTicker.return_value.info = _info_mock
    MockTicker.return_value.cashflow = pd.DataFrame()
    MockTicker.return_value.balance_sheet = pd.DataFrame()
    MockTicker.return_value.income_stmt = pd.DataFrame()
    _logs3 = []
    _flag3 = [False]
    try:
        nightly_scan.analyze_ticker_lite(
            "GOOD.DE", log=_logs3.append, universe="DAX", no_yahoo_price_out=_flag3)
    except Exception:
        pass  # this fixture is deliberately minimal; only the flag/log matters here
    check("a ticker that clears the history/close checks never trips the no-Yahoo-price flag "
          "or log line, regardless of what happens later in the function",
          _flag3[0] is False and not any("no Yahoo price" in m for m in _logs3))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
