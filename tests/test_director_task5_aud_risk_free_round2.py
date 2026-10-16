"""
Director TASK 5 (16 Oct 2026, verified from production logs 16 Oct
03:39-03:45 UTC rescan): AUD risk-free rate, round 2.

_RBA_F2_CSV_URL ('.../csv/f02d-data.csv') returned HTTP 404 on every
scan - RBA statistical tables now follow the '.../statistics/tables/
csv/<table>-data.csv' naming pattern. Fixed in capm_engine.py:
  - _RBA_F2_CSV_URLS: tries "f2.1-data.csv" (F2.1, daily government
    bond yields) first, then "f2-data.csv" (F2, monthly) as a second
    candidate - same 10-year column matching and 5.30% last-resort
    fallback as before.
  - Shared staleness guard (_risk_free_value_is_stale(), 45 days):
    a "live" value whose own reported date is too old is rejected,
    same as "no in-band value" - tries the next URL, falls back if
    none work.
  - Every per-URL failure is logged via _log_risk_free_fetch_failure()
    with the exact URL and the first 300 characters of the response
    body, so production logs show the real cause.

Run: python3 tests/test_director_task5_aud_risk_free_round2.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capm_engine as ce

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


class _FakeResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


_TODAY = datetime.now(timezone.utc).date()


def _csv_with_date(d):
    return (
        "Title,Yields on 10 year Treasury Bond\n"
        "Series ID,FCMYGBAG10\n"
        "Units,Per cent per annum\n"
        f"{d.isoformat()},5.42\n"
    )


# ======================================================================
# CHECK 1: URL order - f2.1-data.csv first, then f2-data.csv.
# ======================================================================
check("_RBA_F2_CSV_URLS tries f2.1-data.csv first, then f2-data.csv",
      ce._RBA_F2_CSV_URLS == (
          "https://www.rba.gov.au/statistics/tables/csv/f2.1-data.csv",
          "https://www.rba.gov.au/statistics/tables/csv/f2-data.csv",
      ))
check("the old 404'ing f02d-data.csv URL is gone",
      "f02d-data.csv" not in " ".join(ce._RBA_F2_CSV_URLS))


# ======================================================================
# CHECK 2-3: first URL 404s, second URL succeeds -> live, from the
# SECOND url - proves the fallback-URL chain actually works, not just
# that the first URL alone still works.
# ======================================================================
_fresh_csv = _csv_with_date(_TODAY)
ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests, \
     mock.patch.object(ce._growth_logger, "warning") as _warn_spy:
    mock_requests.get.side_effect = [
        _FakeResp("Not Found", status_code=404),
        _FakeResp(_fresh_csv, status_code=200),
    ]
    rate, source = ce.get_au_risk_free_rate_live()
check("f2.1 404s, f2 succeeds -> live, exactly 2 HTTP calls (one per URL)",
      source == "live" and abs(rate - 0.0542) < 1e-9
      and mock_requests.get.call_count == 2)
_warn_text = " ".join(str(c) for c in _warn_spy.call_args_list)
check("the f2.1 404 failure is logged with its exact URL",
      "f2.1-data.csv" in _warn_text and "HTTP 404" in _warn_text)


# ======================================================================
# CHECK 4: both URLs fail -> the unchanged 5.30% fallback, logged with
# both URLs' own failure reasons (not just the last one).
# ======================================================================
ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests, \
     mock.patch.object(ce._growth_logger, "warning") as _warn_spy:
    mock_requests.get.side_effect = [
        _FakeResp("Not Found", status_code=404),
        _FakeResp("Not Found", status_code=404),
    ]
    rate, source = ce.get_au_risk_free_rate_live()
check("both URLs 404 -> falls back to the unchanged 5.30% constant",
      source == "default" and rate == ce.RISK_FREE_FALLBACK["AUD"] == 0.053)
_warn_text = " ".join(str(c) for c in _warn_spy.call_args_list)
check("both URLs' own failures are logged (not just the final one)",
      _warn_text.count("HTTP 404") >= 2
      and "f2.1-data.csv" in _warn_text and "f2-data.csv" in _warn_text)


# ======================================================================
# CHECK 5-6: staleness guard - a real, in-band value from the FIRST
# URL that is too old (>45 days) is rejected (not accepted as live),
# and the SECOND url's fresh value is used instead.
# ======================================================================
_stale_date = _TODAY - timedelta(days=60)
_stale_csv = _csv_with_date(_stale_date)
ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests, \
     mock.patch.object(ce._growth_logger, "warning") as _warn_spy:
    mock_requests.get.side_effect = [
        _FakeResp(_stale_csv, status_code=200),
        _FakeResp(_fresh_csv, status_code=200),
    ]
    rate, source = ce.get_au_risk_free_rate_live()
check("a 60-day-old value from f2.1 is rejected as stale, f2's fresh "
      "value is used instead -> live",
      source == "live" and abs(rate - 0.0542) < 1e-9)
_warn_text = " ".join(str(c) for c in _warn_spy.call_args_list)
check("the stale-value rejection is logged with 'stale value' and the "
      "f2.1 URL",
      "stale value" in _warn_text and "f2.1-data.csv" in _warn_text)


# ======================================================================
# CHECK 7: staleness guard - BOTH urls stale -> falls back, never
# silently accepts an old number as live.
# ======================================================================
ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests:
    mock_requests.get.side_effect = [
        _FakeResp(_stale_csv, status_code=200),
        _FakeResp(_stale_csv, status_code=200),
    ]
    rate, source = ce.get_au_risk_free_rate_live()
check("both URLs' values are stale -> falls back to the dated 5.30% "
      "constant, never serves a 60-day-old number as 'live'",
      source == "default" and rate == ce.RISK_FREE_FALLBACK["AUD"])


# ======================================================================
# CHECK 8: exactly-45-days-old is NOT stale (boundary, inclusive) -
# accepted as live.
# ======================================================================
_boundary_csv = _csv_with_date(_TODAY - timedelta(days=45))
ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests:
    mock_requests.get.return_value = _FakeResp(_boundary_csv, status_code=200)
    rate, source = ce.get_au_risk_free_rate_live()
check("a value exactly 45 days old is accepted as live (boundary is "
      "'> 45 days', not '>= 45 days')",
      source == "live" and abs(rate - 0.0542) < 1e-9)


# ======================================================================
# CHECK 9: a connection exception on the first URL still falls through
# to try the second URL (not an immediate crash/fallback).
# ======================================================================
ce.get_au_risk_free_rate_live.clear()
with mock.patch.object(ce, "requests") as mock_requests:
    mock_requests.get.side_effect = [
        Exception("connection refused"),
        _FakeResp(_fresh_csv, status_code=200),
    ]
    rate, source = ce.get_au_risk_free_rate_live()
check("a connection exception on f2.1 still falls through to try "
      "f2-data.csv, which succeeds -> live",
      source == "live" and abs(rate - 0.0542) < 1e-9)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
