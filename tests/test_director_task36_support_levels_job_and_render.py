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


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
