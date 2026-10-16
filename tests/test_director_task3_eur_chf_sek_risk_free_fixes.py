"""
Director task (16 Oct 2026): fix live risk-free rates for EUR, CHF,
SEK - all three were falling back to the fixed 8 Oct values every
scan (verified in production logs 13-15 Oct).

1. EUR: sdw-wsrest.ecb.europa.eu no longer resolves - switched to the
   ECB Data Portal host, data-api.ecb.europa.eu, same path/query.
2. CHF: the SNB CSV is BOM-prefixed, semicolon-delimited, quoted
   cells, with metadata lines before the real "Date"/"Value" header -
   not plain comma-CSV as originally assumed. _parse_snb_chf_csv()
   fixes this.
3. SEK: _RIKSBANK_SEK_SERIES_ID was a bond ISIN ("SE0001030583"), not
   a SWEA series id, so every request 404'd. Corrected to "SEGVB10YC"
   (Riksbank's own published series id for the 10-year Swedish
   government bond) - see capm_engine.py's own module comment for the
   sourcing and the "not independently confirmed with a live call"
   disclosure (this session's network policy blocks api.riksbank.se,
   same as every other host this task tried to reach).

Every existing fallback is unchanged - only the EUR host, the CHF
parser, and the SEK series id changed.

Run: python3 tests/test_director_task3_eur_chf_sek_risk_free_fixes.py
"""
import os
import sys
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


# ======================================================================
# 1. EUR - host swap only. The URL now points at the ECB Data Portal,
# never the retired sdw-wsrest host; the path/query/parsing logic is
# byte-identical to before.
# ======================================================================
check("EUR URLs now use the ECB Data Portal host (data-api.ecb.europa.eu)",
      all(u.startswith("https://data-api.ecb.europa.eu/") for u in ce._ECB_SDW_URLS))
check("EUR URLs no longer use the retired sdw-wsrest host",
      all("sdw-wsrest.ecb.europa.eu" not in u for u in ce._ECB_SDW_URLS))
check("EUR tries I9 (post-Croatia euro area) first, then I8 as a fallback",
      ce._ECB_SDW_URLS[0].endswith(
          "/service/data/IRS/M.I9.L.L40.CI.0000.EUR.N.Z"
          "?lastNObservations=6&format=csvdata"
      )
      and ce._ECB_SDW_URLS[1].endswith(
          "/service/data/IRS/M.I8.L.L40.CI.0000.EUR.N.Z"
          "?lastNObservations=6&format=csvdata"
      ))

_EUR_SAMPLE_CSV = (
    "KEY,TIME_PERIOD,OBS_VALUE\n"
    "IRS.M.I9.L.L40.CI.0000.EUR.N.Z,2026-09,2.60\n"
    "IRS.M.I9.L.L40.CI.0000.EUR.N.Z,2026-10,2.75\n"
)
with mock.patch.object(ce.requests, "get", return_value=_resp(200, _EUR_SAMPLE_CSV)) as _get_eur:
    rate, date_str, reason, transient, response_text = ce._fetch_ecb_eur_once()
    check("EUR parser still works unchanged against a recorded sample response "
          "(picks the latest row by date, not position)",
          abs(rate - 0.0275) < 1e-9 and date_str == "2026-10")
    _called_url = _get_eur.call_args[0][0]
    check("the actual requests.get() call (no explicit url given) defaults to "
          "the first candidate (I9), the new host",
          _called_url == ce._ECB_SDW_URLS[0])


# ======================================================================
# 2. CHF - _parse_snb_chf_csv() against a recorded-shape sample: UTF-8
# BOM, semicolon-delimited, quoted cells, cube metadata lines before
# the real header. First line is the EXACT fragment seen in production
# logs ('﻿"CUBEID";"RENDOBLID"'); the lines after it are this
# test's own representative shape of the metadata that precedes the
# real "Date"/"Value" header row in a genuine SNB cube export.
# ======================================================================
_CHF_SAMPLE_CSV = (
    "﻿\"CUBEID\";\"RENDOBLID\"\n"
    "\"CHARTTITLE\";\"Rendite der eidgenoessischen Obligationen, 10 Jahre\"\n"
    "\"D0\";\"D1\"\n"
    "\"Date\";\"Value\"\n"
    "\"2026-09-15\";\"0.30\"\n"
    "\"2026-10-10\";\"0.45\"\n"
)
best_date, best_rate, reason = ce._parse_snb_chf_csv(_CHF_SAMPLE_CSV)
check("CHF parser handles the BOM + semicolon + quoted-cell + metadata-"
      "lines shape and picks the latest row by date (not position)",
      abs(best_rate - 0.0045) < 1e-9 and best_date == "2026-10-10")

# The OLD parser (plain comma-split on the raw first line as header)
# would have read '﻿"CUBEID";"RENDOBLID"' as ONE comma-less cell -
# "DATE"/"VALUE" would never be found in it, so the bug this task
# fixes is genuinely reproduced: feed the exact same sample through
# the OLD column-detection rule (first line = header, comma-split) and
# confirm it would NOT have found a usable header.
_old_style_header = [h.strip().upper() for h in _CHF_SAMPLE_CSV.splitlines()[0].split(",")]
check("reproduces the original bug: the old plain-comma, first-line-is-"
      "header assumption never finds DATE/VALUE in the real response "
      "at all (proving this is a genuine fix, not a no-op)",
      "DATE" not in _old_style_header and "VALUE" not in _old_style_header)

# _fetch_snb_chf_once() end-to-end through the real parser via a mocked
# HTTP response - same recorded sample, through the full fetch path.
with mock.patch.object(ce.requests, "get", return_value=_resp(200, _CHF_SAMPLE_CSV)):
    rate, date_str, reason, transient, response_text = ce._fetch_snb_chf_once()
    check("_fetch_snb_chf_once() end-to-end: live rate/date extracted "
          "correctly from the recorded sample response",
          abs(rate - 0.0045) < 1e-9 and date_str == "2026-10-10")

# A response with NO Date/Value header anywhere (e.g. a genuinely
# malformed/unexpected cube) is treated as transient (worth retrying),
# same convention as every other "unexpected shape" case in this
# module - never confused with "clean response, no usable data".
_no_header_csv = "﻿\"CUBEID\";\"SOMETHINGELSE\"\n\"D0\";\"D1\"\n"
with mock.patch.object(ce.requests, "get", return_value=_resp(200, _no_header_csv)):
    rate, date_str, reason, transient, response_text = ce._fetch_snb_chf_once()
    check("a response with no Date/Value header at all is transient "
          "(worth retrying), not treated as a clean empty response",
          rate is None and transient is True)

# A malformed/empty body must never raise - same "tolerant" contract
# as every other parser in this module.
check("_parse_snb_chf_csv('') never raises on an empty body",
      ce._parse_snb_chf_csv("") == (None, None, "no usable row found"))


# ======================================================================
# 3. SEK - the series id itself. The old value was a bond ISIN
# ("SE0001030583"), which the SWEA Observations endpoint 404s on; the
# corrected id is Riksbank's own published SWEA series id for the
# 10-year Swedish government bond.
# ======================================================================
check("SEK series id is no longer the bond ISIN that 404'd",
      ce._RIKSBANK_SEK_SERIES_ID != "SE0001030583")
check("SEK series id is the corrected Riksbank SWEA id (SEGVB10YC)",
      ce._RIKSBANK_SEK_SERIES_ID == "SEGVB10YC")
check("the actual Observations URL is built from the corrected id, via the "
      "Latest endpoint (Director's round 2 fix)",
      ce._RIKSBANK_SEK_URL
      == "https://api.riksbank.se/swea/v1/Observations/Latest/SEGVB10YC")

# The JSON-parsing logic itself is untouched by this fix (the bug was
# purely the id string in the URL, not the response shape) - confirm
# it still works against a recorded sample observation payload.
_SEK_SAMPLE_JSON = [
    {"date": "2026-09-20", "value": 1.95},
    {"date": "2026-10-10", "value": 2.05},
]
with mock.patch.object(ce.requests, "get", return_value=_resp(200, "")) as _get_sek:
    _get_sek.return_value.json = mock.Mock(return_value=_SEK_SAMPLE_JSON)
    rate, date_str, reason, transient, response_text = ce._fetch_riksbank_sek_once()
    check("SEK parser (unchanged) still works against a recorded sample "
          "response, picking the latest observation by date",
          abs(rate - 0.0205) < 1e-9 and date_str == "2026-10-10")
    _called_sek_url = _get_sek.call_args[0][0]
    check("the actual requests.get() call used the corrected series id's URL",
          _called_sek_url == ce._RIKSBANK_SEK_URL)


# ======================================================================
# 4. Every existing fallback is unchanged by this task - the dated,
# logged backstop values/dates are untouched.
# ======================================================================
check("EUR_RISK_FREE_FALLBACK is unchanged (0.028)",
      ce.EUR_RISK_FREE_FALLBACK == {"EUR": 0.028})
check("EUR_RISK_FREE_FALLBACK_AS_OF is unchanged (2026-10-08)",
      ce.EUR_RISK_FREE_FALLBACK_AS_OF == "2026-10-08")
check("CHF_SEK_RISK_FREE_FALLBACK is unchanged (CHF 0.004, SEK 0.020)",
      ce.CHF_SEK_RISK_FREE_FALLBACK == {"CHF": 0.004, "SEK": 0.020})
check("CHF_SEK_RISK_FREE_FALLBACK_AS_OF is unchanged (2026-10-08)",
      ce.CHF_SEK_RISK_FREE_FALLBACK_AS_OF == "2026-10-08")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
