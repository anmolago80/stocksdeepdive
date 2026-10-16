"""
Director TASK 6 (16 Oct 2026, verified from production logs 16 Oct
03:39-03:45 UTC rescan): EUR/CHF/SEK risk-free rates, round 2.

a) EUR: the new host (data-api.ecb.europa.eu, TASK 3) resolves but the
   "I8" dataflow key 404s - Croatia joined the euro area in 2023, so
   the ECB now publishes the aggregate under "I9". Fixed: try I9
   first, then I8 as a fallback (capm_engine._ECB_SDW_URLS).

b) CHF: the live fetch was returning a real, in-band value that was
   nonetheless -0.10% "as at 2025-07-31" - a 14-month-old number (the
   SNB's "rendoblid" cube is frozen). Fixed: switched to the
   replacement cube "rendeiduebd" (researched, not independently
   confirmed - see capm_engine.py's own module comment), with
   tolerant client-side filtering to the 10-year maturity row (never
   a hard-coded, unconfirmed dimension code) via
   _SNB_CHF_10Y_MATURITY_HINTS.

c) SEK: the from/to Observations endpoint 404s. Fixed: switched to the
   SWEA API's "latest value" form, .../Observations/Latest/{id} - no
   date range needed, tolerant of either a single observation object
   or a list.

Cross-cutting for all three (and AUD, covered in its own TASK 5
test): the shared staleness guard (_risk_free_value_is_stale(), 45
days) rejects a value whose own date is too old, and every failure is
logged via _log_risk_free_fetch_failure() with the exact URL and the
first 300 characters of the response body. Every existing fallback
value is unchanged.

Run: python3 tests/test_director_task6_eur_chf_sek_risk_free_round2.py
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


def _resp(status=200, body=""):
    m = mock.Mock()
    m.status_code = status
    m.text = body
    return m


_TODAY = datetime.now(timezone.utc).date()


# ======================================================================
# a) EUR - I9 first, then I8 as a fallback.
# ======================================================================
check("EUR tries I9 first, then I8 as a fallback",
      "M.I9." in ce._ECB_SDW_URLS[0] and "M.I8." in ce._ECB_SDW_URLS[1])

_I9_SAMPLE_CSV = (
    f"KEY,TIME_PERIOD,OBS_VALUE\n"
    f"IRS.M.I9.L.L40.CI.0000.EUR.N.Z,{_TODAY.strftime('%Y-%m')},2.70\n"
)
ce.get_eu_risk_free_rate_live.clear()
with mock.patch.object(ce, "time"), \
     mock.patch.object(ce.requests, "get",
                        side_effect=[_resp(404, "not found"), _resp(404, "not found"),
                                     _resp(404, "not found"), _resp(200, _I9_SAMPLE_CSV)]) as _get:
    rate, source = ce.get_eu_risk_free_rate_live()
check("I9 404s on every attempt, I8 succeeds -> live, from the I8 URL "
      "(the 2nd candidate)",
      source == "live" and abs(rate - 0.027) < 1e-9
      and _get.call_args_list[-1][0][0] == ce._ECB_SDW_URLS[1])

# A fresh, in-band I9 value that is STALE (>45 days old) is rejected,
# not served as live - exactly the CHF "frozen cube" lesson applied to
# EUR too.
_stale_month = (_TODAY - timedelta(days=200)).strftime("%Y-%m")
_I9_STALE_CSV = f"KEY,TIME_PERIOD,OBS_VALUE\nIRS.M.I9.L.L40.CI.0000.EUR.N.Z,{_stale_month},2.60\n"
ce.get_eu_risk_free_rate_live.clear()
with mock.patch.object(ce, "time"), \
     mock.patch.object(ce.requests, "get",
                        return_value=_resp(200, _I9_STALE_CSV)) as _get:
    rate, source = ce.get_eu_risk_free_rate_live()
check("a stale I9 value (and equally stale I8 value, same mocked "
      "response) is rejected on both candidates -> falls back, never "
      "served as live",
      source == "default" and rate == ce.EUR_RISK_FREE_FALLBACK["EUR"])


# ======================================================================
# b) CHF - rendeiduebd cube, tolerant 10-year maturity filtering.
# ======================================================================
check("CHF URL now points at the rendeiduebd cube (frozen rendoblid "
      "replaced), filtered to CHF",
      "rendeiduebd" in ce._SNB_CHF_URL and "D0=CHF" in ce._SNB_CHF_URL)
check("the frozen rendoblid cube is no longer requested",
      "rendoblid" not in ce._SNB_CHF_URL)

_fresh_date = _TODAY.isoformat()
_MULTI_MATURITY_CSV = (
    "﻿\"CUBEID\";\"RENDEIDUEBD\"\n"
    "\"Date\";\"D1\";\"Value\"\n"
    f"\"{_fresh_date}\";\"2J\";\"1.10\"\n"
    f"\"{_fresh_date}\";\"5J\";\"0.80\"\n"
    f"\"{_fresh_date}\";\"10J\";\"0.55\"\n"
)
best_date, best_rate, reason = ce._parse_snb_chf_csv(_MULTI_MATURITY_CSV)
check("a multi-maturity response is filtered to the 10-year (D1=10J) "
      "row by tolerant matching, not the 2-year or 5-year rows",
      abs(best_rate - 0.0055) < 1e-9 and best_date == _fresh_date)

_NO_10Y_CSV = (
    "﻿\"CUBEID\";\"RENDEIDUEBD\"\n"
    "\"Date\";\"D1\";\"Value\"\n"
    f"\"{_fresh_date}\";\"2J\";\"1.10\"\n"
    f"\"{_fresh_date}\";\"5J\";\"0.80\"\n"
)
best_date2, best_rate2, reason2 = ce._parse_snb_chf_csv(_NO_10Y_CSV)
check("a response with a maturity column but NO 10-year row reports "
      "which maturities it actually saw (never silently guesses one), "
      "per the 'report which cube/series has it' instruction",
      best_rate2 is None and "2J" in reason2 and "5J" in reason2)

# Backward-compat: a response with NO maturity column at all (the
# pre-migration cube's own shape) is still parsed as before - the
# filter never assumes a column that isn't there.
_NO_MATURITY_COL_CSV = (
    "﻿\"CUBEID\";\"RENDOBLID\"\n"
    "\"Date\";\"Value\"\n"
    f"\"{_fresh_date}\";\"0.55\"\n"
)
best_date3, best_rate3, reason3 = ce._parse_snb_chf_csv(_NO_MATURITY_COL_CSV)
check("a response with no maturity/D1 column at all is parsed "
      "unfiltered, same as before this round's change",
      abs(best_rate3 - 0.0055) < 1e-9 and best_date3 == _fresh_date)

# Stale CHF value (the actual production symptom this round fixes):
# real, in-band, 10-year-matched, but too old -> rejected, falls back.
_stale_chf_date = (_TODAY - timedelta(days=80)).isoformat()
_STALE_CHF_CSV = (
    "﻿\"CUBEID\";\"RENDEIDUEBD\"\n"
    "\"Date\";\"D1\";\"Value\"\n"
    f"\"{_stale_chf_date}\";\"10J\";\"-0.10\"\n"
)
ce.get_chf_risk_free_rate_live.clear()
with mock.patch.object(ce, "time"), \
     mock.patch.object(ce.requests, "get", return_value=_resp(200, _STALE_CHF_CSV)):
    rate, source = ce.get_chf_risk_free_rate_live()
check("an 80-day-old CHF 10-year value (the real frozen-cube symptom) "
      "is rejected as stale, never served as live",
      source == "default" and rate == ce.CHF_SEK_RISK_FREE_FALLBACK["CHF"])


# ======================================================================
# c) SEK - the Latest endpoint, tolerant single-dict-or-list shape.
# ======================================================================
check("SEK URL now uses the Latest endpoint",
      ce._RIKSBANK_SEK_URL
      == "https://api.riksbank.se/swea/v1/Observations/Latest/SEGVB10YC")

_SINGLE_OBS = {"date": _TODAY.isoformat(), "value": 2.15}
with mock.patch.object(ce.requests, "get", return_value=_resp(200, "")) as _get:
    _get.return_value.json = mock.Mock(return_value=_SINGLE_OBS)
    rate, date_str, reason, transient, response_text = ce._fetch_riksbank_sek_once()
check("the Latest endpoint's single-observation-object shape (not a "
      "list) is parsed tolerantly",
      abs(rate - 0.0215) < 1e-9 and date_str == _TODAY.isoformat())

_stale_sek_date = (_TODAY - timedelta(days=90)).isoformat()
_STALE_SEK_OBS = {"date": _stale_sek_date, "value": 1.95}
ce.get_sek_risk_free_rate_live.clear()
with mock.patch.object(ce, "time"), \
     mock.patch.object(ce.requests, "get", return_value=_resp(200, "")) as _get:
    _get.return_value.json = mock.Mock(return_value=_STALE_SEK_OBS)
    rate, source = ce.get_sek_risk_free_rate_live()
check("a 90-day-old SEK value is rejected as stale, falls back to the "
      "dated constant",
      source == "default" and rate == ce.CHF_SEK_RISK_FREE_FALLBACK["SEK"])


# ======================================================================
# Cross-cutting: every existing fallback value is unchanged.
# ======================================================================
check("EUR_RISK_FREE_FALLBACK is unchanged (0.028)",
      ce.EUR_RISK_FREE_FALLBACK == {"EUR": 0.028})
check("CHF_SEK_RISK_FREE_FALLBACK is unchanged (CHF 0.004, SEK 0.020)",
      ce.CHF_SEK_RISK_FREE_FALLBACK == {"CHF": 0.004, "SEK": 0.020})
check("RISK_FREE_FALLBACK['AUD'] is unchanged (0.053)",
      ce.RISK_FREE_FALLBACK["AUD"] == 0.053)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
