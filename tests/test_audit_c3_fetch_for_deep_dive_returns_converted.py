"""Audit fix C3 (Fable finding V3, 10 Oct 2026, instruction_combined_
10oct.md PART C): financials_income_store.fetch_for_deep_dive() used
to return the RAW (statement-currency) income_df it fetched directly
from yfinance, while its own helper (_save_direct_fetch()) saved a
CONVERTED copy of that same frame to the store. deep_dive_engine.py's
caller reads income_df_currency_converted straight off the STORE
entry's own currency_converted flag and threads it into dcf_
intrinsic_value() alongside the income_df RETURNED by fetch_for_deep_
dive() - so when the store entry correctly said currency_converted=
True, dcf_intrinsic_value() correctly SKIPPED re-converting... a frame
that, in fact, had never been converted at all (the raw one actually
handed back). The opposite shape of C1's double-conversion bug: here
a needed conversion is silently skipped entirely, understating or
overstating the DCF base by a full missed FX multiply depending on
direction.

Scope note (FINANCIALS_STORE_LIVE): this store/switch is currently
OFF in production (per the Director's own PART C item 3 description -
"currently unset, so no live effect") - this fix has no live
production effect today, but is proven correct here so it's ready the
moment the switch is turned on, and so Deep-Dive-path testing of this
module doesn't rely on a latent, silently-wrong return value.

Run: python3 tests/test_audit_c3_fetch_for_deep_dive_returns_converted.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="audit_c3_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import financials_income_store as fis
import fundamentals_data
import fcf_valuation_engine as fve

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


RAW_INCOME = pd.DataFrame(
    {"2026-06-30": [600.0, 60.0, 300.0], "2025-06-30": [580.0, 58.0, 290.0]},
    index=["Operating Income", "Reconciled Depreciation", "Net Income"],
)
# A deliberately distinguishable "converted" frame (x1.5, matching a
# hypothetical USD->AUD rate) - mocking _convert_statement_currency
# directly isolates fetch_for_deep_dive()'s OWN return-value bug from
# that function's own (separately-owned, separately-tested) per-column
# historical-FX machinery.
CONVERTED_INCOME = RAW_INCOME * 1.5

_BUNDLE_WITH_CCY = {"info": {"financialCurrency": "USD", "currency": "AUD"}}
_BUNDLE_NO_CCY = {"info": {}}


# ======================================================================
# CHECK 1: with a resolvable fin_ccy != listing_ccy, fetch_for_deep_
# dive() now returns the CONVERTED frame, not the raw one - and the
# store entry's own currency_converted flag agrees with what was
# actually returned (no more mismatch).
# ======================================================================
with mock.patch("yfinance.Ticker") as _mock_yf_ticker, \
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=_BUNDLE_WITH_CCY), \
     mock.patch.object(fundamentals_data, "_convert_statement_currency", return_value=CONVERTED_INCOME) as _mock_convert:
    _mock_yf_ticker.return_value.income_stmt = RAW_INCOME
    _result = fis.fetch_for_deep_dive("CONVTEST")

check("fetch_for_deep_dive() returns the CONVERTED frame, not the raw "
      "one it fetched - the core V3 fix",
      _result is not None and _result.equals(CONVERTED_INCOME) and not _result.equals(RAW_INCOME))
check("_convert_statement_currency() was actually called with the "
      "right currency pair",
      _mock_convert.call_args.args[1:] == ("USD", "AUD"))

_stored = fis.get("CONVTEST")
check("the store entry says currency_converted=True",
      _stored is not None and _stored.get("currency_converted") is True)
check("the store entry's own income frame IS the same converted frame "
      "fetch_for_deep_dive() returned - flag and object agree, the "
      "exact mismatch this fix closes",
      _stored["income"].equals(_result))


# ======================================================================
# CHECK 2: regression - with NO resolvable currency (no cached bundle/
# no currency fields), the unconverted branch is unaffected: the
# returned frame IS the raw fetch, and currency_converted=False -
# consistent, as it already was before this fix (this sub-case was
# never actually mismatched, since the unconverted branch always saved
# the same object it got).
# ======================================================================
with mock.patch("yfinance.Ticker") as _mock_yf_ticker2, \
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None):
    _mock_yf_ticker2.return_value.income_stmt = RAW_INCOME
    _result2 = fis.fetch_for_deep_dive("NOCCYTEST")

check("no resolvable currency -> returns the raw frame unchanged "
      "(regression, unaffected by this fix)",
      _result2 is not None and _result2.equals(RAW_INCOME))
_stored2 = fis.get("NOCCYTEST")
check("no resolvable currency -> currency_converted=False, consistent "
      "with the unconverted frame actually returned",
      _stored2 is not None and _stored2.get("currency_converted") is False
      and _stored2["income"].equals(_result2))


# ======================================================================
# CHECK 3: a failed fetch still returns None and records the attempt -
# completely unaffected by this fix (no income_df to convert at all).
# ======================================================================
with mock.patch("yfinance.Ticker", side_effect=ConnectionError("boom")):
    _result3 = fis.fetch_for_deep_dive("FAILTEST")
check("a failed fetch still returns None, unaffected by this fix",
      _result3 is None)
check("the attempt is still recorded on failure",
      fis.last_deep_dive_attempt("FAILTEST") is not None
      and fis.last_deep_dive_attempt("FAILTEST").get("success") is False)


# ======================================================================
# Ticker-level IV effect: the SAME income_df (RAW vs CONVERTED) fed
# into dcf_intrinsic_value() with income_df_currency_converted=True -
# simulating the pre-fix bug (flag says converted, but the raw frame
# was actually what got used) vs the post-fix correct pairing (flag
# AND frame now agree).
# ======================================================================
_cf_ocf = [500.0, 480.0, 470.0, 490.0, 500.0]
_cf_capex = [-50.0] * 5
_cols = {}
for i, (o, c) in enumerate(zip(_cf_ocf, _cf_capex)):
    _cols[f"202{6 - i}-06-30"] = [o, c]
_CF = pd.DataFrame(_cols, index=["Operating Cash Flow", "Capital Expenditure"])
_INFO = {"financialCurrency": "USD", "currency": "AUD", "sharesOutstanding": 100.0,
         "marketCap": 2_000_000_000, "sector": "Financial Services", "industry": "Insurance"}
_COMMON = dict(cashflow_df=_CF, currency="AUD", discount_rate=0.08, perpetual_rate=0.025,
               growth_rate=0.04, income_df_currency_converted=True)

iv_buggy, _, meta_buggy = fve.dcf_intrinsic_value(
    "DDTEST", info=_INFO, income_df=RAW_INCOME, **_COMMON,
)
iv_fixed, _, meta_fixed = fve.dcf_intrinsic_value(
    "DDTEST", info=_INFO, income_df=CONVERTED_INCOME, **_COMMON,
)
check("the pre-fix pairing (flag=True but still the RAW frame) and "
      "the post-fix pairing (flag=True, the ACTUALLY-converted frame) "
      "give different IVs for the identical fixture - the missed "
      "conversion has a real, non-zero effect",
      iv_buggy > 0 and iv_fixed > 0 and abs(iv_fixed - iv_buggy) > 0.01)
_ratio = iv_fixed / iv_buggy
check(f"the magnitude matches the fixture's own 1.5x conversion factor "
      f"(actual ratio: {_ratio:.2f}x)",
      1.4 < _ratio < 1.6)

print(f"  [ticker-level] Deep-Dive financials-mode fixture (FINANCIALS_STORE_LIVE "
      f"scenario): pre-fix (raw frame actually used) IV=${iv_buggy:.2f} AUD -> "
      f"post-fix (correctly-converted frame used) IV=${iv_fixed:.2f} AUD "
      f"({(iv_fixed - iv_buggy) / iv_buggy:+.1%})")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
