"""
Director's next round, item 2 (8 Oct 2026, instruction_health_fixes_
chart_and_new_markets.md): "Risk-free rate for EUR, CHF, SEK: report
first which official source each could use (ECB, SNB, Riksbank),
whether a live fetch is reliable, and what happens on failure. Build
it if a reliable official source exists, on the Bank of Canada
pattern (retry, one rate per scan run). Never a hard-coded number
without a date and a log line."

The report (sources/reliability/failure-mode for all three) is the
commit message's own text. This file covers what was built on the
strength of that report:
  - EUR: ECB SDW judged confident enough to build - a live fetch with
    retry/backoff AND an explicit per-scan-run cache, the exact same
    two mechanisms PART 3 Proposal 5 built for CAD.
  - CHF/SEK: no live source built in the FIRST pass (SNB/Riksbank
    almost certainly have one, but this sandbox can't verify either
    one's exact endpoint shape) - a DATED, LOGGED fallback instead,
    never a bare untraceable number.

Director's correction 2 (9 Oct 2026): CHF (SNB data.snb.ch) and SEK
(Riksbank api.riksbank.se) now get live fetches too, on the exact same
retry/backoff + one-rate-per-scan-run pattern as EUR/CAD - see capm_
engine.py's own module comment above _fetch_snb_chf_once()/_fetch_
riksbank_sek_once() for the UNVERIFIED-from-this-sandbox disclosure.
The dated CHF_SEK_RISK_FREE_FALLBACK is now the failure-mode BACKSTOP,
not the primary path - covered below too.

Run: python3 tests/test_nextround_item2_eur_chf_sek_risk_free.py
"""
import logging
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


_GOOD_CSV = (
    "KEY,TIME_PERIOD,OBS_VALUE\n"
    "IRS.M.I8.L.L40.CI.0000.EUR.N.Z,2026-08,2.60\n"
    "IRS.M.I8.L.L40.CI.0000.EUR.N.Z,2026-09,2.75\n"
)

# ======================================================================
# CHECK 1-2: _fetch_ecb_eur_once() - clean success, picks the LATEST
# date (never a position-based pick).
# ======================================================================
with mock.patch.object(ce.requests, "get", return_value=_resp(200, _GOOD_CSV)):
    rate, date_str, reason, transient = ce._fetch_ecb_eur_once()
    check("clean success: rate=0.0275 (the LATEST row, 2026-09, not the first)",
          abs(rate - 0.0275) < 1e-9 and date_str == "2026-09")

with mock.patch.object(ce.requests, "get", side_effect=ConnectionError("boom")):
    rate, date_str, reason, transient = ce._fetch_ecb_eur_once()
    check("connection error -> transient=True (worth retrying)", rate is None and transient is True)

# ======================================================================
# CHECK 3-5: get_eu_risk_free_rate_live() - retry/backoff, same shape
# as the CAD fetch (PART 3 Proposal 5).
# ======================================================================
with mock.patch.object(ce, "time") as _time_mod, \
     mock.patch.object(ce.requests, "get",
                        side_effect=[_resp(503), _resp(200, _GOOD_CSV)]) as _get:
    ce.get_eu_risk_free_rate_live.clear()
    rate, source = ce.get_eu_risk_free_rate_live()
    check("fails once (transient), succeeds on attempt 2 -> live, exactly 2 HTTP calls",
          source == "live" and abs(rate - 0.0275) < 1e-9 and _get.call_count == 2)
    check("backoff slept once, 2s", _time_mod.sleep.call_args_list == [mock.call(2)])

with mock.patch.object(ce, "time"), \
     mock.patch.object(ce.requests, "get", side_effect=ConnectionError("x")) as _get:
    ce.get_eu_risk_free_rate_live.clear()
    rate, source = ce.get_eu_risk_free_rate_live()
    check(f"every attempt fails -> falls back after exactly {ce._ECB_RETRY_ATTEMPTS} "
          "attempts, dated fallback",
          source == "default" and rate == ce.EUR_RISK_FREE_FALLBACK["EUR"]
          and _get.call_count == ce._ECB_RETRY_ATTEMPTS)

# ======================================================================
# CHECK 6-7: get_eu_risk_free_rate_for_run() - one fetch per run,
# reset_eu_risk_free_run_cache() clears it for the next run.
# ======================================================================
ce.reset_eu_risk_free_run_cache()
with mock.patch.object(ce, "get_eu_risk_free_rate_live", return_value=(0.027, "live")) as _live:
    ce.get_eu_risk_free_rate_for_run()
    ce.get_eu_risk_free_rate_for_run()
    ce.get_eu_risk_free_rate_for_run()
check("3 calls to get_eu_risk_free_rate_for_run() in the same run -> exactly 1 real fetch",
      _live.call_count == 1)
ce.reset_eu_risk_free_run_cache()
with mock.patch.object(ce, "get_eu_risk_free_rate_live", return_value=(0.030, "live")) as _live2:
    ce.get_eu_risk_free_rate_for_run()
check("after reset, the next run fetches fresh",
      _live2.call_count == 1)

# ======================================================================
# CHECK 8-9: _fetch_snb_chf_once() - clean success, latest-date pick;
# transient failure classification. Same shape as the ECB checks above.
# ======================================================================
_GOOD_SNB_CSV = (
    "DATE,VALUE\n"
    "2026-08-15,0.30\n"
    "2026-09-20,0.45\n"
)
with mock.patch.object(ce.requests, "get", return_value=_resp(200, _GOOD_SNB_CSV)):
    rate, date_str, reason, transient = ce._fetch_snb_chf_once()
    check("SNB clean success: rate=0.0045 (the LATEST row, 2026-09-20, not the first)",
          abs(rate - 0.0045) < 1e-9 and date_str == "2026-09-20")

with mock.patch.object(ce.requests, "get", side_effect=ConnectionError("boom")):
    rate, date_str, reason, transient = ce._fetch_snb_chf_once()
    check("SNB connection error -> transient=True (worth retrying)",
          rate is None and transient is True)

# ======================================================================
# CHECK 10-12: get_chf_risk_free_rate_live() - retry/backoff, same
# shape as EUR; falls back to the dated CHF number on exhaustion.
# ======================================================================
with mock.patch.object(ce, "time") as _time_mod, \
     mock.patch.object(ce.requests, "get",
                        side_effect=[_resp(503), _resp(200, _GOOD_SNB_CSV)]) as _get:
    ce.get_chf_risk_free_rate_live.clear()
    rate, source = ce.get_chf_risk_free_rate_live()
    check("CHF fails once (transient), succeeds on attempt 2 -> live, exactly 2 HTTP calls",
          source == "live" and abs(rate - 0.0045) < 1e-9 and _get.call_count == 2)
    check("CHF backoff slept once, 2s", _time_mod.sleep.call_args_list == [mock.call(2)])

with mock.patch.object(ce, "time"), \
     mock.patch.object(ce.requests, "get", side_effect=ConnectionError("x")) as _get:
    ce.get_chf_risk_free_rate_live.clear()
    rate, source = ce.get_chf_risk_free_rate_live()
    check(f"CHF every attempt fails -> falls back after exactly {ce._SNB_RETRY_ATTEMPTS} "
          "attempts, dated fallback",
          source == "default" and rate == ce.CHF_SEK_RISK_FREE_FALLBACK["CHF"]
          and _get.call_count == ce._SNB_RETRY_ATTEMPTS)

# ======================================================================
# CHECK 13-14: get_chf_risk_free_rate_for_run() - one fetch per run.
# ======================================================================
ce.reset_chf_risk_free_run_cache()
with mock.patch.object(ce, "get_chf_risk_free_rate_live", return_value=(0.004, "live")) as _live_chf:
    ce.get_chf_risk_free_rate_for_run()
    ce.get_chf_risk_free_rate_for_run()
    ce.get_chf_risk_free_rate_for_run()
check("3 calls to get_chf_risk_free_rate_for_run() in the same run -> exactly 1 real fetch",
      _live_chf.call_count == 1)
ce.reset_chf_risk_free_run_cache()
with mock.patch.object(ce, "get_chf_risk_free_rate_live", return_value=(0.005, "live")) as _live_chf2:
    ce.get_chf_risk_free_rate_for_run()
check("after reset, the next CHF run fetches fresh", _live_chf2.call_count == 1)

# ======================================================================
# CHECK 15-16: _fetch_riksbank_sek_once() - clean success, latest-date
# pick (JSON observations, not CSV); transient failure classification.
# ======================================================================
_GOOD_RIKSBANK_JSON = [
    {"date": "2026-08-20", "value": 1.90},
    {"date": "2026-09-25", "value": 2.00},
]
with mock.patch.object(ce.requests, "get", return_value=_resp(200, "")) as _get:
    _get.return_value.json = mock.Mock(return_value=_GOOD_RIKSBANK_JSON)
    rate, date_str, reason, transient = ce._fetch_riksbank_sek_once()
    check("Riksbank clean success: rate=0.02 (the LATEST row, 2026-09-25, not the first)",
          abs(rate - 0.02) < 1e-9 and date_str == "2026-09-25")

with mock.patch.object(ce.requests, "get", side_effect=ConnectionError("boom")):
    rate, date_str, reason, transient = ce._fetch_riksbank_sek_once()
    check("Riksbank connection error -> transient=True (worth retrying)",
          rate is None and transient is True)

# ======================================================================
# CHECK 17-19: get_sek_risk_free_rate_live() - retry/backoff, same
# shape as EUR/CHF; falls back to the dated SEK number on exhaustion.
# ======================================================================
with mock.patch.object(ce, "time") as _time_mod, \
     mock.patch.object(ce.requests, "get") as _get:
    _bad = _resp(503)
    _good = _resp(200, "")
    _good.json = mock.Mock(return_value=_GOOD_RIKSBANK_JSON)
    _get.side_effect = [_bad, _good]
    ce.get_sek_risk_free_rate_live.clear()
    rate, source = ce.get_sek_risk_free_rate_live()
    check("SEK fails once (transient), succeeds on attempt 2 -> live, exactly 2 HTTP calls",
          source == "live" and abs(rate - 0.02) < 1e-9 and _get.call_count == 2)
    check("SEK backoff slept once, 2s", _time_mod.sleep.call_args_list == [mock.call(2)])

with mock.patch.object(ce, "time"), \
     mock.patch.object(ce.requests, "get", side_effect=ConnectionError("x")) as _get:
    ce.get_sek_risk_free_rate_live.clear()
    rate, source = ce.get_sek_risk_free_rate_live()
    check(f"SEK every attempt fails -> falls back after exactly {ce._RIKSBANK_RETRY_ATTEMPTS} "
          "attempts, dated fallback",
          source == "default" and rate == ce.CHF_SEK_RISK_FREE_FALLBACK["SEK"]
          and _get.call_count == ce._RIKSBANK_RETRY_ATTEMPTS)

# ======================================================================
# CHECK 20-21: get_sek_risk_free_rate_for_run() - one fetch per run.
# ======================================================================
ce.reset_sek_risk_free_run_cache()
with mock.patch.object(ce, "get_sek_risk_free_rate_live", return_value=(0.02, "live")) as _live_sek:
    ce.get_sek_risk_free_rate_for_run()
    ce.get_sek_risk_free_rate_for_run()
    ce.get_sek_risk_free_rate_for_run()
check("3 calls to get_sek_risk_free_rate_for_run() in the same run -> exactly 1 real fetch",
      _live_sek.call_count == 1)
ce.reset_sek_risk_free_run_cache()
with mock.patch.object(ce, "get_sek_risk_free_rate_live", return_value=(0.021, "live")) as _live_sek2:
    ce.get_sek_risk_free_rate_for_run()
check("after reset, the next SEK run fetches fresh", _live_sek2.call_count == 1)

# ======================================================================
# CHECK 22-25: resolve_discount_rate_by_market_cap() - EUR/CHF/SEK all
# route through their own per-run cache now; CHF/SEK fall back to the
# dated fallback only when their own live fetch fails.
# ======================================================================
ce.reset_eu_risk_free_run_cache()
_eur_info = {"currency": "EUR", "marketCap": 50_000_000_000}
with mock.patch.object(ce, "get_eu_risk_free_rate_live", return_value=(0.027, "live")) as _live3:
    ce.resolve_discount_rate_by_market_cap(_eur_info, "EUR")
    ce.resolve_discount_rate_by_market_cap(_eur_info, "EUR")
check("two EUR tickers in the same run share exactly one real risk-free fetch",
      _live3.call_count == 1)

ce.reset_chf_risk_free_run_cache()
_chf_info = {"currency": "CHF", "marketCap": 50_000_000_000}
with mock.patch.object(ce, "get_chf_risk_free_rate_live", return_value=(0.0045, "live")) as _live_chf3:
    _rate_chf, _meta_chf = ce.resolve_discount_rate_by_market_cap(_chf_info, "CHF")
check("CHF routes through its own per-run cache when the live fetch succeeds - "
      "rf_source='live'",
      _meta_chf["rf_source"] == "live" and _live_chf3.call_count == 1)

ce.reset_chf_risk_free_run_cache()
with mock.patch.object(ce, "get_chf_risk_free_rate_live",
                        return_value=ce.get_chf_sek_risk_free_rate("CHF")):
    _rate_chf2, _meta_chf2 = ce.resolve_discount_rate_by_market_cap(_chf_info, "CHF")
check("CHF falls back to the dated number when its own live fetch fails - "
      f"rf_source='default', risk_free_used={ce.CHF_SEK_RISK_FREE_FALLBACK['CHF']}",
      _meta_chf2["rf_source"] == "default"
      and _meta_chf2["risk_free_used"] == ce.CHF_SEK_RISK_FREE_FALLBACK["CHF"])

ce.reset_sek_risk_free_run_cache()
_sek_info = {"currency": "SEK", "marketCap": 50_000_000_000}
with mock.patch.object(ce, "get_sek_risk_free_rate_live", return_value=(0.02, "live")) as _live_sek3:
    _rate_sek, _meta_sek = ce.resolve_discount_rate_by_market_cap(_sek_info, "SEK")
check("SEK routes through its own per-run cache when the live fetch succeeds - "
      "rf_source='live'",
      _meta_sek["rf_source"] == "live" and _live_sek3.call_count == 1)

ce.reset_sek_risk_free_run_cache()
with mock.patch.object(ce, "get_sek_risk_free_rate_live",
                        return_value=ce.get_chf_sek_risk_free_rate("SEK")):
    _rate_sek2, _meta_sek2 = ce.resolve_discount_rate_by_market_cap(_sek_info, "SEK")
check("SEK falls back to the dated number when its own live fetch fails - "
      f"rf_source='default', risk_free_used={ce.CHF_SEK_RISK_FREE_FALLBACK['SEK']}",
      _meta_sek2["rf_source"] == "default"
      and _meta_sek2["risk_free_used"] == ce.CHF_SEK_RISK_FREE_FALLBACK["SEK"])

# ======================================================================
# CHECK 26-27: "never a hard-coded number without a date and a log
# line" - the CHF/SEK fallback's own log line carries the date.
# ======================================================================
_logged = []
_handler = logging.Handler()
_handler.emit = lambda record: _logged.append(record.getMessage())
logging.getLogger("sdd.growth").addHandler(_handler)
ce.get_chf_sek_risk_free_rate("CHF")
logging.getLogger("sdd.growth").removeHandler(_handler)
check(f"CHF fallback log line carries the date ({ce.CHF_SEK_RISK_FREE_FALLBACK_AS_OF})",
      any(ce.CHF_SEK_RISK_FREE_FALLBACK_AS_OF in m and "CHF" in m for m in _logged))

def _raises_keyerror():
    try:
        ce.get_chf_sek_risk_free_rate("XYZ")
        return False
    except KeyError:
        return True


check("KeyError for a currency this dated-fallback function was never meant to cover",
      _raises_keyerror())


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
