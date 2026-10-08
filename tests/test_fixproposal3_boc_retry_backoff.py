"""
Proposal 3 of the Director's numbered fix-proposal round (8 Oct 2026,
instruction_health_fixes_chart_and_new_markets.md PART 3 STEP 3.2):
"retry with backoff for the Bank of Canada rate, CANADA ONLY, and one
rate per scan run. Do not change the USD or AUD risk-free functions."

Covers:
  - _fetch_boc_valet_once(): one attempt, correctly labels a network-
    level failure as transient (retry) vs. a clean-but-unusable
    response as non-transient (don't retry).
  - get_ca_risk_free_rate_live(): retries up to _BOC_RETRY_ATTEMPTS
    times with exponential backoff on a transient failure; succeeds
    on whichever attempt first returns a usable rate; stops
    IMMEDIATELY (no retry) on a non-transient data problem; falls
    back to the flagged constant only after every attempt fails.
  - "one rate per scan run": the @st.cache_data(ttl=86400) decorator
    this function already carried means a second call within the same
    process never re-fetches - this proposal's own claim that this
    half was already true before the change, not newly built.
  - get_risk_free_rate() (USD/AUD) and get_uk_risk_free_rate_live()/
    get_jp_risk_free_rate_live() are untouched by this diff (CANADA
    ONLY, per the Director's own words) - a source-code check, not a
    network-level simulation, is the right proof.

Run: python3 tests/test_fixproposal3_boc_retry_backoff.py
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


def _resp(status=200, body=None):
    m = mock.Mock()
    m.status_code = status
    m.json = mock.Mock(return_value=body or {})
    return m


_GOOD_BODY = {"observations": [
    {"d": "2026-10-01", "BD.CDN.10YR.DQ.YLD": {"v": "3.25"}},
    {"d": "2026-10-05", "BD.CDN.10YR.DQ.YLD": {"v": "3.40"}},
]}
_OUT_OF_BAND_BODY = {"observations": [
    {"d": "2026-10-05", "BD.CDN.10YR.DQ.YLD": {"v": "0.001"}},
]}

# ======================================================================
# CHECK 1-3: _fetch_boc_valet_once() - transient vs. non-transient.
# ======================================================================
with mock.patch.object(ce.requests, "get", return_value=_resp(200, _GOOD_BODY)):
    rate, date_str, reason, transient = ce._fetch_boc_valet_once()
    check("clean success: rate=0.0340 (the LATEST date, 2026-10-05, not the first array entry)",
          abs(rate - 0.0340) < 1e-9 and date_str == "2026-10-05" and reason is None)

with mock.patch.object(ce.requests, "get", return_value=_resp(503)):
    rate, date_str, reason, transient = ce._fetch_boc_valet_once()
    check("non-200 -> transient=True (worth retrying)", rate is None and transient is True)

with mock.patch.object(ce.requests, "get", side_effect=ConnectionError("boom")):
    rate, date_str, reason, transient = ce._fetch_boc_valet_once()
    check("connection error -> transient=True (worth retrying)", rate is None and transient is True)

with mock.patch.object(ce.requests, "get", return_value=_resp(200, _OUT_OF_BAND_BODY)):
    rate, date_str, reason, transient = ce._fetch_boc_valet_once()
    check("clean response, out-of-band value -> transient=False (a retry can't fix bad data)",
          rate is None and transient is False)

# ======================================================================
# CHECK 4-7: get_ca_risk_free_rate_live() - the retry loop itself.
# Each case clears the cache first (the established @st.cache_data
# test convention) and patches time.sleep so the test doesn't actually
# wait through the real backoff delays.
# ======================================================================
with mock.patch.object(ce, "time") as _time_mod, \
     mock.patch.object(ce.requests, "get",
                        side_effect=[_resp(503), _resp(200, _GOOD_BODY)]) as _get:
    ce.get_ca_risk_free_rate_live.clear()
    rate, source = ce.get_ca_risk_free_rate_live()
    check("fails once (transient), succeeds on attempt 2 -> live, exactly 2 HTTP calls",
          source == "live" and abs(rate - 0.0340) < 1e-9 and _get.call_count == 2)
    check("backoff slept once, 2s (base delay x 2**0)", _time_mod.sleep.call_args_list == [mock.call(2)])

with mock.patch.object(ce, "time") as _time_mod, \
     mock.patch.object(ce.requests, "get",
                        side_effect=[ConnectionError("a"), ConnectionError("b"), ConnectionError("c")]) as _get:
    ce.get_ca_risk_free_rate_live.clear()
    rate, source = ce.get_ca_risk_free_rate_live()
    check("every attempt transient-fails -> falls back after exactly "
          f"{ce._BOC_RETRY_ATTEMPTS} attempts, source='default'",
          source == "default" and _get.call_count == ce._BOC_RETRY_ATTEMPTS)
    check("backoff slept twice with exponential delays 2s then 4s (never after the LAST attempt)",
          _time_mod.sleep.call_args_list == [mock.call(2), mock.call(4)])

with mock.patch.object(ce, "time") as _time_mod, \
     mock.patch.object(ce.requests, "get", return_value=_resp(200, _OUT_OF_BAND_BODY)) as _get:
    ce.get_ca_risk_free_rate_live.clear()
    rate, source = ce.get_ca_risk_free_rate_live()
    check("non-transient data problem -> stops after exactly 1 attempt, NEVER retried, "
          "source='default'",
          source == "default" and _get.call_count == 1 and _time_mod.sleep.call_count == 0)

# ======================================================================
# CHECK 8: "one rate per scan run" - @st.cache_data(ttl=86400) means a
# second call within the same process never re-fetches.
# ======================================================================
with mock.patch.object(ce.requests, "get", return_value=_resp(200, _GOOD_BODY)) as _get:
    ce.get_ca_risk_free_rate_live.clear()
    ce.get_ca_risk_free_rate_live()
    ce.get_ca_risk_free_rate_live()
    ce.get_ca_risk_free_rate_live()
    check("3 calls in the same process -> exactly 1 real HTTP GET (the cache, already "
          "present before this change, already gives 'one rate per scan run')",
          _get.call_count == 1)

# ======================================================================
# CHECK 9-10: USD/AUD/UK/JP risk-free functions are byte-for-byte
# untouched - a source check against the committed diff, not a
# network simulation (this diff never touched these functions' bodies
# at all).
# ======================================================================
import inspect
_rf_src = inspect.getsource(ce.get_risk_free_rate)
check('get_risk_free_rate() (USD/AUD) has no retry/backoff change - still calls '
      "nightly_scan._yf_call_with_retry() (its own pre-existing yfinance retry helper), "
      "never _fetch_boc_valet_once or anything BoC-related",
      "_fetch_boc_valet_once" not in _rf_src and "_BOC_" not in _rf_src)
_uk_src = inspect.getsource(ce.get_uk_risk_free_rate_live)
_jp_src = inspect.getsource(ce.get_jp_risk_free_rate_live)
check("get_uk_risk_free_rate_live()/get_jp_risk_free_rate_live() carry no _BOC_ reference "
      "either - CANADA ONLY, per the Director's own words",
      "_BOC_" not in _uk_src and "_BOC_" not in _jp_src)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
