"""
TASK 42 item 2 (Director-directed, 8 Nov 2026): ELS.AX's Fair Value
tab DCF is STILL wrong after the TASK 40 deploy (A$1.53 vs Deep Dive's
A$2.23, confirmed live by the Director with a screenshot taken after
commit ff1aec3 was already deployed).

INVESTIGATION: pulled fresh [dcf_diag] ELS.AX lines from Railway,
timestamped MINUTES after the TASK 40 deploy (06:53-06:57 UTC, deploy
landed 06:43 UTC) - the EXACT same symptom as before TASK 40:
  path=deepdive ... fcf_base=16132395.22 ... fin_ccy=USD fx=1.456
  path=fairvalue ... fcf_base=11079942   ... fin_ccy=AUD fx=None
Critically, path=fairvalue's own cache_age=0.19191 - this bundle was
built FRESH, moments before, by code already running my TASK 40 fix
(fundamentals_data.BUNDLE_VERSION=10, the honest currency_converted/
currency_conversion_partial flag). So this is NOT a stale-cache replay
of the pre-fix bug - it's the SAME symptom, reproduced live, under the
fixed code.

ROOT CAUSE (deduced from the fcf_base/fin_ccy/fx triple, since fx=None
AND fcf_base=11079942 i.e. the RAW/unconverted figure rules out the
TASK 40 mechanism entirely - if get_bundle()'s own currency-conversion
block had run at all for this bundle, fin_ccy!=listing_ccy would be
true and _convert_statement_currency() would have been called on
cashflow; either it succeeded (fcf_base would be ~16M, matching
deepdive) or TASK 40's own fix would have flagged "currency_
conversion_partial" instead of silently claiming AUD. Neither
happened - fin_ccy reads as "AUD" (matching listing) and fx=None,
meaning fundamentals_data.get_bundle()'s own gate `if fin_ccy and
list_ccy and fin_ccy != list_ccy:` never even fired for THIS bundle -
its own info.get("financialCurrency") must have been missing/empty on
THIS particular live fetch, genuinely nothing to convert from this
bundle's own point of view):

deep_dive_engine.py fetches `info` via app.get_ticker_info(ticker) - a
COMPLETELY SEPARATE, independent yf.Ticker(ticker).info call (see that
module's own "COMPLETELY SEPARATE path" comment) from fundamentals_
data.get_bundle()'s own `tk.info` fetch. Two independent live calls to
the same (flaky) Yahoo quoteSummary endpoint for the SAME ticker,
seconds apart, can return different field COMPLETENESS - a known
yfinance/Yahoo-backend quirk, not a bug in this codebase's own
conversion arithmetic (TASK 40's fix is necessary but not sufficient:
it only catches a conversion that was ATTEMPTED and then silently
failed, never a case where the trigger condition for attempting one
was never met because financialCurrency was simply absent from this
particular fetch).

FIX: fundamentals_data.get_bundle() now retries its OWN info fetch
ONCE, narrowly, when financialCurrency is specifically missing from an
otherwise-populated info dict (currency present) for a ticker that
genuinely has statement history (statement_years > 0 - an ETF/fund
with no financials has nothing to report here, and retrying for every
such ticker would double a live call for no benefit). Logs
"[fin_ccy_retry] <ticker>: ..." either way (recovered, or still
missing after the retry) so this is now directly observable in
production instead of silently falling through to "nothing to
convert".

NOT VERIFIED LIVE: this sandbox has no network access, so whether the
retry actually recovers "USD" for ELS.AX on the next live view cannot
be confirmed here - only that the retry logic itself behaves exactly
as designed against a synthetic fixture reproducing the exact
observed symptom (financialCurrency missing on the first .info call,
present on a second one).

Run: python3 tests/test_director_task42_els_fin_ccy_retry.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task42_fin_ccy_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import fundamentals_data as fd

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


_ELS_INCOME = pd.DataFrame({"2025-06-30": [500_000.0]}, index=["Net Income"])
_ELS_BALANCE = pd.DataFrame({"2025-06-30": [5_000_000.0]}, index=["Total Stockholders Equity"])
_ELS_CASHFLOW = pd.DataFrame({"2025-06-30": [11_079_942.0]}, index=["Free Cash Flow"])

# First call's info: ELS.AX-shaped, currency present, financialCurrency
# MISSING - the exact live symptom.
_FIRST_INFO = {
    "currency": "AUD", "currentPrice": 1.43, "regularMarketPrice": 1.43,
    "marketCap": 1.43 * 226_243_359, "sharesOutstanding": 226_243_359,
}
# The retry's own info: the SAME ticker, now WITH financialCurrency -
# shaped like deep_dive_engine.py's own independent fetch, which
# reliably gets it.
_SECOND_INFO = dict(_FIRST_INFO, financialCurrency="USD")

_call_count = [0]


class _SpxStub:
    """get_bundle() also fetches "^GSPC" history for the Covariance/
    Correlation metrics, via the SAME mocked yf.Ticker - a real, unrelated
    call that must not be counted against (or alter the info of) the
    ticker actually under test here."""
    info = {}

    def history(self, *a, **k):
        return pd.DataFrame()


def _fake_ticker_sequence(ticker, *_a, **_k):
    if ticker == "^GSPC":
        return _SpxStub()
    _call_count[0] += 1
    is_first_call = _call_count[0] == 1

    class _FT:
        def __init__(self):
            self.info = dict(_FIRST_INFO if is_first_call else _SECOND_INFO)
            self.income_stmt = _ELS_INCOME
            self.balance_sheet = _ELS_BALANCE
            self.cashflow = _ELS_CASHFLOW
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = pd.Series(dtype=float)

        @property
        def fast_info(self):
            raise AttributeError

        def history(self, *a, **k):
            return pd.DataFrame()

    return _FT()


# ======================================================================
# CHECK 1: the exact live symptom, reproduced - first .info call is
# missing financialCurrency, retry recovers it, the bundle ends up with
# financialCurrency populated and the recovery flagged.
# ======================================================================
_call_count[0] = 0
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker_sequence
    _bundle = fd.get_bundle("ELS.AX", force_refresh=True)

check("get_bundle() called yf.Ticker() twice (the original fetch + "
      "exactly one retry, not an unbounded loop)",
      _call_count[0] == 2)
check("the bundle's own info now has financialCurrency='USD' - "
      "recovered by the retry, matching deep_dive_engine.py's own "
      "independent fetch for the same ticker",
      _bundle["info"].get("financialCurrency") == "USD")
check("the recovery is flagged 'financial_currency_retry_recovered' - "
      "visible in meta.flags, not a silent fix",
      "financial_currency_retry_recovered" in _bundle["meta"]["flags"])
check("'financial_currency_missing' is NOT also flagged (the retry "
      "succeeded, so this is not the permanently-missing case)",
      "financial_currency_missing" not in _bundle["meta"]["flags"])
print("[task42_check1_retry_recovers] a financialCurrency missing on "
      "the first live .info fetch but present on an immediate retry is "
      "now recovered and the recovery is logged/flagged, not silently "
      "accepted as 'nothing to convert' OK")


# ======================================================================
# CHECK 2: when the retry ALSO comes back empty (genuinely missing,
# not just a one-off flake), the ticker is flagged as such rather than
# retried forever or silently treated as fine.
# ======================================================================
def _fake_ticker_always_missing(*_a, **_k):
    class _FT:
        def __init__(self):
            self.info = dict(_FIRST_INFO)  # no financialCurrency, every call
            self.income_stmt = _ELS_INCOME
            self.balance_sheet = _ELS_BALANCE
            self.cashflow = _ELS_CASHFLOW
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = pd.Series(dtype=float)

        @property
        def fast_info(self):
            raise AttributeError

        def history(self, *a, **k):
            return pd.DataFrame()

    return _FT()


with mock.patch.object(fd, "yf") as _myf2:
    _myf2.Ticker.side_effect = _fake_ticker_always_missing
    _bundle2 = fd.get_bundle("NEVERHASFC.AX", force_refresh=True)

check("still missing after the retry -> flagged 'financial_currency_missing', "
      "never retried a second time (the retry is exactly ONE attempt)",
      "financial_currency_missing" in _bundle2["meta"]["flags"]
      and "financial_currency_retry_recovered" not in _bundle2["meta"]["flags"])
check("financialCurrency stays absent on the bundle's own info - never "
      "fabricated", not _bundle2["info"].get("financialCurrency"))
print("[task42_check2_genuinely_missing] a ticker where financialCurrency "
      "is genuinely unavailable (missing on both the original fetch and "
      "the retry) is flagged, not endlessly retried or silently assumed OK")


# ======================================================================
# CHECK 3: the retry is NARROWLY scoped - never fires for a ticker with
# no statement history at all (an ETF/fund genuinely has nothing to
# report here), so a normal fund-shaped ticker costs no extra call.
# ======================================================================
_call_count[0] = 0


def _fake_ticker_etf(ticker, *_a, **_k):
    if ticker == "^GSPC":
        return _SpxStub()
    _call_count[0] += 1

    class _FT:
        def __init__(self):
            self.info = {"currency": "USD", "currentPrice": 50.0}  # no financials, ETF-shaped
            self.income_stmt = pd.DataFrame()
            self.balance_sheet = pd.DataFrame()
            self.cashflow = pd.DataFrame()
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = pd.Series(dtype=float)

        @property
        def fast_info(self):
            raise AttributeError

        def history(self, *a, **k):
            return pd.DataFrame()

    return _FT()


with mock.patch.object(fd, "yf") as _myf3:
    _myf3.Ticker.side_effect = _fake_ticker_etf
    fd.get_bundle("SPYLIKE", force_refresh=True)

check("a no-statements (ETF-shaped) ticker never triggers the retry - "
      "yf.Ticker() called exactly once, not twice",
      _call_count[0] == 1)
print("[task42_check3_etf_no_retry] the retry is scoped to tickers that "
      "genuinely have statement history - a fund/ETF with none costs no "
      "extra live call OK")


# ======================================================================
# CHECK 4: the happy path (financialCurrency present on the FIRST
# fetch, the overwhelming majority of tickers) is completely
# unaffected - still exactly one yf.Ticker() call.
# ======================================================================
_call_count[0] = 0


def _fake_ticker_happy_path(ticker, *_a, **_k):
    if ticker == "^GSPC":
        return _SpxStub()
    _call_count[0] += 1

    class _FT:
        def __init__(self):
            self.info = dict(_SECOND_INFO)  # financialCurrency already present
            self.income_stmt = _ELS_INCOME
            self.balance_sheet = _ELS_BALANCE
            self.cashflow = _ELS_CASHFLOW
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = pd.Series(dtype=float)

        @property
        def fast_info(self):
            raise AttributeError

        def history(self, *a, **k):
            return pd.DataFrame()

    return _FT()


with mock.patch.object(fd, "yf") as _myf4:
    _myf4.Ticker.side_effect = _fake_ticker_happy_path
    _bundle4 = fd.get_bundle("NORMALTICKER", force_refresh=True)

check("financialCurrency already present on the first fetch -> no "
      "retry at all (exactly one yf.Ticker() call)", _call_count[0] == 1)
check("no retry-related flags added when nothing needed recovering",
      "financial_currency_retry_recovered" not in _bundle4["meta"]["flags"]
      and "financial_currency_missing" not in _bundle4["meta"]["flags"])
print("[task42_check4_happy_path_unaffected] the overwhelming majority "
      "of tickers (financialCurrency present on the first fetch) see no "
      "behavior change at all from this fix OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
