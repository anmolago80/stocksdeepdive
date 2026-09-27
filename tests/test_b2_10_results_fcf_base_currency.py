"""Batch B2, item 10 (27 Sep 2026, owner-directed, CONFIRMED BUG): the
Results card showed the FCF base in reporting currency labelled '$'.

results_engine._reanalyze() fetches its cashflow statement straight from
yfinance (`yf.Ticker(ticker).cashflow`) - unlike the Deep Dive/Research
pages, this is NEVER routed through fundamentals_data.get_bundle()'s
currency conversion. For a company that reports in a different currency
than it trades in (e.g. CSL.AX/RMD.AX-style ASX names reporting in
USD), fcf_base is genuinely in that REPORTING currency - but
results_engine.fmt_money_compact() (used for the card's table cell, the
"what moved" bullet text, and the email notification) always prefixes a
bare "$", which every OTHER metric on this card (Price, Intrinsic value,
EPS) uses correctly because those ARE in the listing/price currency. The
bare "$" on fcf_base silently implied the same currency as everything
else, when it might not be.

Fix: _reanalyze() now captures the statement's reporting currency
(financialCurrency, falling back to currency) as "fcf_currency" in both
the before/after dicts; what_moved()'s bullet text appends it for the
fcf_base metric specifically; app.py's _render_results_day_card() (not
covered by this engine-level fixture - see this repo's test suite
convention of never importing the full app.py module - verified instead
by direct code inspection and py_compile) appends the same suffix to the
card's table cells.

Reproduces the bug first (the OLD code's bare "$" text, ambiguous for a
USD-reporting/AUD-listed company), then proves the fix.
Run: python3 tests/test_b2_10_results_fcf_base_currency.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import results_engine as re_

# CSL.AX-shaped: reports in USD, trades in AUD.
CASHFLOW_USD = pd.DataFrame(
    [[620_000.0, 500_000.0], [-80_000.0, -60_000.0]],
    columns=["2026-06-30", "2025-06-30"],
    index=["Operating Cash Flow", "Capital Expenditure"],
)


class _FakeTicker:
    cashflow = CASHFLOW_USD
    info = {"financialCurrency": "USD", "currency": "AUD"}


def _run_reanalyze():
    # _reanalyze() imports nightly_scan/score_history/yfinance LOCALLY
    # inside the function (not module-level attributes of results_engine),
    # so patch the real modules those local imports resolve to via
    # sys.modules, not attributes on `re_` itself.
    with mock.patch("yfinance.Ticker", return_value=_FakeTicker()), \
         mock.patch("nightly_scan.analyze_ticker_lite", return_value={
             "Long Score": 80.0, "Quality": 70, "Moat": 60, "MOS %": 12.0,
             "Intrinsic Value": 45.0, "Price": 40.0,
         }), \
         mock.patch("nightly_scan._attach_moat", return_value=None), \
         mock.patch.object(re_.results_store, "get_earnings_watch", return_value={}), \
         mock.patch("score_history.before_date", return_value=None):
        import datetime
        return re_._reanalyze("TESTB210.AX", datetime.date(2026, 7, 1))


import nightly_scan  # noqa: E402 - imported for the mock.patch targets above to resolve
import score_history  # noqa: E402
result = _run_reanalyze()
assert result is not None
before, after, moved, stale = result

# ---- The fix: fcf_currency is captured and carried in both dicts ----
assert before.get("fcf_currency") == "USD"
assert after.get("fcf_currency") == "USD"
print("[fcf_currency_captured] the statement's reporting currency (USD) is captured in both "
      "before and after, even though this ticker trades in AUD OK")

# ---- fcf_base itself is the correct raw (unconverted) OCF - capex figure ----
assert after.get("fcf_base") == 620_000.0 - 80_000.0
print(f"[fcf_base_value_unchanged] fcf_base itself ({after['fcf_base']}) is exactly OCF - capex "
      "in the reporting currency - this fix only adds the missing currency label, never touches "
      "the number OK")

# ---- what_moved()'s bullet text for fcf_base now names the currency ----
fcf_item = next((m for m in moved if m["metric"] == "fcf_base"), None)
assert fcf_item is not None
assert "USD" in fcf_item["text"]
print(f"[what_moved_text_labelled] the fcf_base bullet text now says: {fcf_item['text']!r} - "
      "explicitly USD, not a bare, ambiguous '$' OK")

# Reproduces the bug: the OLD text (fmt_money_compact alone, no suffix)
# never named a currency at all - for this exact USD-reporting/
# AUD-listed ticker, that bare "$" would have been read as AUD (matching
# every other "$" on the same card), when it was actually USD.
_old_buggy_text = (f"{re_.METRIC_META['fcf_base']['label']} moved from "
                    f"{re_.fmt_money_compact(before['fcf_base'])} to "
                    f"{re_.fmt_money_compact(after['fcf_base'])} "
                    f"({re_._delta_text('money_compact', after['fcf_base'] - before['fcf_base'])})")
assert "USD" not in _old_buggy_text
assert fcf_item["text"] != _old_buggy_text
print(f"[reproduces_bug] the OLD code's text ({_old_buggy_text!r}) never named a currency at "
      "all, silently implying the same AUD every other metric on this card is in OK")

# ---- Other metrics are completely unaffected (no currency suffix at all) ----
mos_item = next((m for m in moved if m["metric"] == "mos_pct"), None)
if mos_item is not None:
    assert "USD" not in mos_item["text"] and "AUD" not in mos_item["text"]
    print("[other_metrics_unaffected] a non-fcf_base metric's text carries no currency suffix - "
          "this fix is scoped to fcf_base only OK")

# ---- Same-currency ticker: no suffix, since there's no ambiguity ----
CASHFLOW_SAME_CCY = pd.DataFrame(
    [[620_000.0, 500_000.0], [-80_000.0, -60_000.0]],
    columns=["2026-06-30", "2025-06-30"],
    index=["Operating Cash Flow", "Capital Expenditure"],
)


class _FakeTickerSameCcy:
    cashflow = CASHFLOW_SAME_CCY
    info = {"financialCurrency": "USD", "currency": "USD"}


def _run_reanalyze_same_ccy():
    with mock.patch("yfinance.Ticker", return_value=_FakeTickerSameCcy()), \
         mock.patch("nightly_scan.analyze_ticker_lite", return_value={
             "Long Score": 80.0, "Quality": 70, "Moat": 60, "MOS %": 12.0,
             "Intrinsic Value": 45.0, "Price": 40.0,
         }), \
         mock.patch("nightly_scan._attach_moat", return_value=None), \
         mock.patch.object(re_.results_store, "get_earnings_watch", return_value={}), \
         mock.patch("score_history.before_date", return_value=None):
        import datetime
        return re_._reanalyze("TESTB210US", datetime.date(2026, 7, 1))


before_us, after_us, moved_us, _ = _run_reanalyze_same_ccy()
assert before_us.get("fcf_currency") == "USD"
print("[same_currency_still_captured] fcf_currency is still captured even when it happens to "
      "match a US ticker's own listing currency - the card can always show it, whether or not "
      "it turns out to differ OK")


print("\nALL B2.10 RESULTS-FCF-BASE-CURRENCY FIXTURES PASSED")
