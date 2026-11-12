"""
TASK 36 (Director-directed, owner-reported, 6 Nov 2026): "Support <Mon
YYYY>" chip on Top 200 cards - owner's TradingView support logic,
levels only (test stage, owner approval before go-live).

This file covers everything support_levels_engine.py's own test file
(test_director_task36_support_levels_engine.py) does not:
  - support_levels_job._weekly_bars_from_daily()'s .L pence-repair
    (runs on DAILY bars before resampling to weekly - TASK 29/29b/29c's
    own established order) vs. a non-".L" ticker's unrepaired pass-
    through.
  - support_levels_store.py's save()/get() round-trip, including date
    reconstruction and the "no entry" / corrupt-file cases.
  - the TOP200_SUPPORT_CHIP_LIVE gate matrix on top100_render.py's
    _support_chip_html() (owner-only unless flipped - the task's own
    explicit TEST GATE instruction, never set by this task), plus the
    purple "Support ..." / orange "No support below ..." chip text and
    its EN/ES month formatting.

Run: python3 tests/test_director_task36_support_levels_job_and_render.py
"""
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task36_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import support_levels_job as slj
import top100_store
import support_levels_store as sls
import top100_render as tr

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


def _daily_df(closes, start="2024-01-01"):
    """A minimal OHLC DataFrame (business-day index) for _weekly_bars_
    from_daily() - Open/High/Low all equal to Close for simplicity,
    since this test only cares about which VALUES survive pence-repair
    and resampling, not intraday OHLC spread."""
    idx = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx,
    )


# ======================================================================
# CHECK 1: _weekly_bars_from_daily() pence-repairs a ".L" ticker's
# daily Close series BEFORE resampling - reference_price in pounds
# (~12.00), daily closes in pence (~1200, ratio ~100x, inside
# fundamentals_data._PENCE_HISTORY_REPAIR_RATIO_RANGE 30-300) - the
# resulting weekly closes must land back near the reference price, not
# ~100x it.
# ======================================================================
_pence_closes = [1200.0 + i for i in range(20)]  # ~4 business weeks
_df_l = _daily_df(_pence_closes)
_dates, _highs, _lows, _closes = slj._weekly_bars_from_daily(_df_l, "HSBA.L", 12.0, log=lambda *a: None)
check("L_ticker_repaired_weekly_bars_nonempty", len(_closes) >= 1)
check(
    f"L_ticker_repaired_to_pounds_not_pence (closes={_closes})",
    all(5.0 <= c <= 20.0 for c in _closes),
)

# ======================================================================
# CHECK 2: a non-".L" ticker's daily series passes through UNREPAIRED
# even when its own Close/reference ratio would otherwise look pence-
# shaped (the repair function here is only ever invoked for ".L"
# tickers unconditionally - TASK 29's own rule).
# ======================================================================
_df_us = _daily_df(_pence_closes)
_dates_us, _highs_us, _lows_us, _closes_us = slj._weekly_bars_from_daily(
    _df_us, "AAPL", 12.0, log=lambda *a: None,
)
check(
    f"non_L_ticker_never_repaired (closes={_closes_us})",
    all(c >= 1000.0 for c in _closes_us),
)

# ======================================================================
# CHECK 3: None / empty DataFrame -> ([], [], [], []), never raises.
# ======================================================================
check("none_df_safe", slj._weekly_bars_from_daily(None, "ANY.L", 10.0) == ([], [], [], []))
check(
    "empty_df_safe",
    slj._weekly_bars_from_daily(pd.DataFrame(), "ANY.L", 10.0) == ([], [], [], []),
)

# ======================================================================
# CHECK 4: support_levels_store round-trip - save() then get()
# reconstructs date fields as real date objects; a ticker with no
# saved entry at all returns None, never raises.
# ======================================================================
_result = {
    "support_price": 12.40, "support_date": datetime.date(2025, 11, 3),
    "pct_above_support": 3.2,
    "next_support_price": 10.10, "next_support_date": datetime.date(2024, 6, 10),
    "drop_from_12m_high": -41.0, "has_support": True,
    "lowest_since_date": None, "lowest_since_price": None,
}
sls.save("OCL.AX", _result, currency="AUD")
_stored = sls.get("OCL.AX")
check("store_roundtrip_not_none", _stored is not None)
check("store_roundtrip_support_date_is_date_obj", isinstance(_stored["support_date"], datetime.date))
check("store_roundtrip_support_date_value", _stored["support_date"] == datetime.date(2025, 11, 3))
check("store_roundtrip_next_support_date_value", _stored["next_support_date"] == datetime.date(2024, 6, 10))
check("store_roundtrip_lowest_since_date_stays_none", _stored["lowest_since_date"] is None)
check("store_roundtrip_currency", _stored["currency"] == "AUD")
check("store_missing_ticker_returns_none", sls.get("NOPE.AX") is None)


# ======================================================================
# CHECK 5: TOP200_SUPPORT_CHIP_LIVE gate matrix on top100_render.
# _support_chip_html() - closed gate + non-owner viewer => "" (no chip
# AT ALL, matching the task's own TEST GATE instruction), even though
# a store entry exists for the ticker.
# ======================================================================
_row = {"ticker": "OCL.AX", "price": 12.80}
os.environ.pop("TOP200_SUPPORT_CHIP_LIVE", None)
tr._is_owner_for_refresh = lambda: False
_chip = tr._support_chip_html(_row, "en")
check("gate_closed_non_owner_no_chip", _chip == "")

# ======================================================================
# CHECK 6: closed gate + OWNER viewer => chip IS shown (owner always
# sees it, regardless of the gate - same shape as _recent_badge_live()'s
# own precedent).
# ======================================================================
tr._is_owner_for_refresh = lambda: True
_chip = tr._support_chip_html(_row, "en")
check("gate_closed_owner_sees_chip", _chip != "")
check("gate_closed_owner_chip_has_support_text", "Support" in _chip)

# ======================================================================
# CHECK 7: TOP200_SUPPORT_CHIP_LIVE=1 => shown to a NON-owner viewer too.
# ======================================================================
os.environ["TOP200_SUPPORT_CHIP_LIVE"] = "1"
tr._is_owner_for_refresh = lambda: False
_chip = tr._support_chip_html(_row, "en")
check("gate_live_non_owner_sees_chip", _chip != "")

# ======================================================================
# CHECK 8: gate explicitly "0" behaves identically to unset.
# ======================================================================
os.environ["TOP200_SUPPORT_CHIP_LIVE"] = "0"
tr._is_owner_for_refresh = lambda: False
_chip = tr._support_chip_html(_row, "en")
check("gate_zero_non_owner_no_chip", _chip == "")

# ======================================================================
# CHECK 9: no stored entry at all -> "" even with the gate wide open.
# ======================================================================
os.environ["TOP200_SUPPORT_CHIP_LIVE"] = "1"
_no_entry_row = {"ticker": "NOENTRY.AX", "price": 5.0}
_chip = tr._support_chip_html(_no_entry_row, "en")
check("no_store_entry_no_chip", _chip == "")

# ======================================================================
# CHECK 10: purple "Support" chip text matches the Director's own
# approved mock wording exactly - "Support Nov 2025 · A$12.40 · +3%
# above · -41% from 12-month high".
# ======================================================================
tr._is_owner_for_refresh = lambda: True
os.environ.pop("TOP200_SUPPORT_CHIP_LIVE", None)
_chip = tr._support_chip_html(_row, "en")
check(f"support_chip_text_en (chip={_chip!r})", "Support Nov 2025" in _chip)
check("support_chip_has_price", "A$12.40" in _chip)
check("support_chip_has_pct_above", "+3%" in _chip)
check("support_chip_has_drop", "-41%" in _chip)

# ======================================================================
# CHECK 11: orange "No support below" chip (has_support False) - exact
# wording "No support below - lowest since Mar 2020 · -58% from
# 12-month high".
# ======================================================================
_no_support_result = {
    "support_price": None, "support_date": None, "pct_above_support": None,
    "next_support_price": None, "next_support_date": None,
    "drop_from_12m_high": -58.0, "has_support": False,
    "lowest_since_date": datetime.date(2020, 3, 15), "lowest_since_price": 4.20,
}
sls.save("VAP.AX", _no_support_result, currency="AUD")
_no_support_row = {"ticker": "VAP.AX", "price": 9.50}
_chip = tr._support_chip_html(_no_support_row, "en")
check(f"no_support_chip_text_en (chip={_chip!r})", "No support below - lowest since Mar 2020" in _chip)
check("no_support_chip_has_drop", "-58%" in _chip)
check("no_support_chip_is_orange", "#fbbf24" in _chip)
check("support_chip_is_purple", "#c9a7f5" in tr._support_chip_html(_row, "en"))

# ======================================================================
# CHECK 12: ES month formatting - lowercase "nov 2025", not "Nov 2025".
# ======================================================================
_chip_es = tr._support_chip_html(_row, "es")
check(f"support_chip_month_es (chip={_chip_es!r})", "nov 2025" in _chip_es)
check("support_chip_month_es_not_capitalized", "Nov 2025" not in _chip_es)


# ======================================================================
# CHECK 13 (7 Nov 2026 follow-up, Director-directed): a ticker with too
# few weekly bars is now named in its own log line - the Director's
# own real production gap ("1 failed/skipped" with no ticker named).
# ======================================================================
_too_short_df = _daily_df([10.0, 10.1, 10.2, 10.3, 10.4])  # far fewer than 7 weekly bars
_logged = []
slj._weekly_bars_from_daily(_too_short_df, "SHORT.AX", 10.0, log=lambda *a: None)
# Exercise the real skip path inside compute_and_store_support_levels_for_pool()
# via a monkeypatched download so this ticker is the only candidate.
import unittest.mock as _mock
with _mock.patch.object(slj, "_pool_and_extension_tickers_with_currency",
                         return_value={"SHORT.AX": {"currency": "AUD", "price": 10.0}}):
    with _mock.patch.object(slj, "_download_chunk", return_value={"SHORT.AX": _too_short_df}):
        slj.compute_and_store_support_levels_for_pool(log=lambda m: _logged.append(m))
check(f"skip path now names the failing ticker and bar count (logged={_logged})",
      any("SHORT.AX: skipped - only" in m and "weekly bar(s) available" in m for m in _logged))


# ======================================================================
# CHECK 14 (TASK 44 item 1, Director-directed, 11 Nov 2026): _pool_and_
# extension_tickers_with_currency() now covers the FULL eligible
# nightly-scan candidate universe (top100_engine._build_best_by_
# ticker()'s own output), not only the current Top 200 pool + ASX
# extension - the "Quality on sale" tab's own coverage finding (a
# company that fell OUT of the 200 could never appear otherwise).
# Pool/extension membership is still included via a union (belt-and-
# braces), never REPLACED.
# ======================================================================
import top100_engine as _te_for_slj

_fake_best_by_ticker = {
    "OUTOFPOOL.AX": {"ticker": "OUTOFPOOL.AX", "currency": "AUD", "price": 3.0},
    "INPOOL.AX": {"ticker": "INPOOL.AX", "currency": "AUD", "price": 50.0},
}
with _mock.patch.object(_te_for_slj, "_build_best_by_ticker",
                         return_value=(_fake_best_by_ticker, 2)), \
     _mock.patch.object(top100_store, "current_pool",
                         return_value=[{"ticker": "INPOOL.AX", "currency": "AUD", "price": 50.0}]), \
     _mock.patch.object(top100_store, "current_asx_extension", return_value=[]):
    _widened = slj._pool_and_extension_tickers_with_currency(log=lambda *a: None)
check("TASK_44_item1_widened_ticker_set_includes_a_ticker_outside_the_pool_entirely "
      "(the exact CSL/OCL coverage case)",
      "OUTOFPOOL.AX" in _widened and _widened["OUTOFPOOL.AX"]["price"] == 3.0)
check("TASK_44_item1_widened_ticker_set_still_includes_the_real_pool_member_too",
      "INPOOL.AX" in _widened and _widened["INPOOL.AX"]["price"] == 50.0)

# _build_best_by_ticker() itself failing must never break this job -
# falls back to pool/extension alone, never raises.
with _mock.patch.object(_te_for_slj, "_build_best_by_ticker", side_effect=RuntimeError("boom")), \
     _mock.patch.object(top100_store, "current_pool",
                         return_value=[{"ticker": "INPOOL.AX", "currency": "AUD", "price": 50.0}]), \
     _mock.patch.object(top100_store, "current_asx_extension", return_value=[]):
    _fallback_only = slj._pool_and_extension_tickers_with_currency(log=lambda *a: None)
check("TASK_44_item1_a_build_best_by_ticker_failure_falls_back_to_pool_extension_only_"
      "never_raises",
      _fallback_only == {"INPOOL.AX": {"currency": "AUD", "price": 50.0}})
print("[task44_check14_widened_ticker_scope] support_levels_job.py's own ticker scope "
      "now covers the full eligible nightly-scan universe (union'd with pool/extension, "
      "never replacing it), and fails open to pool/extension alone if the wider lookup "
      "itself errors OK")


# ======================================================================
# CHECK 15 (TASK 44 item 2 follow-up, Director-directed, 12 Nov 2026):
# a ticker whose bulk chunk download comes back with too few weekly
# bars gets ONE single-ticker retry, recovered when the retry succeeds
# - root-causing the Director's own 28 "only 1 weekly bar" skips for
# large, long-listed liquid names (a batch-level hiccup, not a real
# data gap).
# ======================================================================
_good_long_df = _daily_df([10.0] * 120)  # ~24 weeks - comfortably >= 7
_short_df_retry = _daily_df([10.0] * 5)  # < 1 week


def _bulk_then_recovered_retry(tickers):
    tickers = list(tickers)
    if tickers == ["RETRY.AX"]:
        return {"RETRY.AX": _good_long_df}
    return {"OTHER.AX": _good_long_df, "RETRY.AX": _short_df_retry}


_logged_retry = []
with _mock.patch.object(slj, "_pool_and_extension_tickers_with_currency",
                         return_value={"RETRY.AX": {"currency": "AUD", "price": 10.0},
                                        "OTHER.AX": {"currency": "AUD", "price": 20.0}}), \
     _mock.patch.object(slj, "_download_chunk", side_effect=_bulk_then_recovered_retry):
    slj.compute_and_store_support_levels_for_pool(log=lambda m: _logged_retry.append(m))
check("a ticker short on the first bulk attempt is recovered via a single-ticker "
      "retry, logged as such",
      any("RETRY.AX: recovered via single-ticker retry" in m for m in _logged_retry))
check("the recovered ticker has a real stored support-levels entry (the retry's "
      "own data was actually used, not discarded)",
      sls.get("RETRY.AX") is not None)
check("a ticker that already had enough bars on the first attempt is never "
      "retried or logged as recovered",
      not any("OTHER.AX: recovered" in m for m in _logged_retry))

# A ticker still short even AFTER the retry is still correctly skipped -
# CHECK 13 above already covers this via the unchanged "skipped - only ..."
# log text; confirm here that the retry path itself was actually exercised
# (not just the original single-attempt skip) via an explicit call-count.
_retry_calls = []


def _always_short(tickers):
    _retry_calls.append(list(tickers))
    return {"STILLSHORT.AX": _short_df_retry}


_logged_still_short = []
with _mock.patch.object(slj, "_pool_and_extension_tickers_with_currency",
                         return_value={"STILLSHORT.AX": {"currency": "AUD", "price": 10.0}}), \
     _mock.patch.object(slj, "_download_chunk", side_effect=_always_short):
    slj.compute_and_store_support_levels_for_pool(log=lambda m: _logged_still_short.append(m))
check("a ticker still short after the retry makes exactly 2 download calls "
      "(the original bulk chunk, then the single-ticker retry)",
      _retry_calls == [["STILLSHORT.AX"], ["STILLSHORT.AX"]])
check("a ticker still short after the retry is logged with the updated "
      "'after a single-ticker retry' wording, and is NOT marked recovered",
      any("STILLSHORT.AX: skipped - only" in m and "after a single-ticker retry" in m
          for m in _logged_still_short)
      and not any("STILLSHORT.AX: recovered" in m for m in _logged_still_short))

# ======================================================================
# CHECK 16: a ticker with enough weekly bars on the FIRST attempt is
# never retried at all - exactly one download call total.
# ======================================================================
_single_attempt_calls = []


def _counting_download_chunk(tickers):
    _single_attempt_calls.append(list(tickers))
    return {"GOOD.AX": _good_long_df}


with _mock.patch.object(slj, "_pool_and_extension_tickers_with_currency",
                         return_value={"GOOD.AX": {"currency": "AUD", "price": 10.0}}), \
     _mock.patch.object(slj, "_download_chunk", side_effect=_counting_download_chunk):
    slj.compute_and_store_support_levels_for_pool(log=lambda *a: None)
check("a ticker with enough weekly bars on the first attempt makes exactly "
      "ONE download call (no wasted retry)",
      _single_attempt_calls == [["GOOD.AX"]])
print("[task44_followup_check15_16_support_levels_retry] a short-on-first-"
      "attempt ticker is recovered via one single-ticker retry (root-causing "
      "the Director's own 28 'only 1 weekly bar' skips as a batch-level "
      "hiccup); a ticker still short after the retry is still correctly "
      "skipped; a ticker with enough bars already is never retried OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
