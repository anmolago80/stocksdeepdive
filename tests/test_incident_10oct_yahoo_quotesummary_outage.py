"""Incident fix (10 Oct 2026, Andrew-reported): Yahoo quoteSummary
401s "Invalid Crumb"/"User is unable to access this feature"
12:38-12:42 UTC, right after Andrew's manual rescan of TSX 60
finished and a full nightly-shaped reprice pass ran over all 22 other
universes. Four items, each diagnosed and fixed independently:

1. app.py's three @st.cache_data-wrapped yfinance fetchers (get_
   ticker_info/get_price_history/get_cashflow_df) used to cache a
   FAILED/empty result for the full 30-minute ttl once _fetch_with_
   retry() exhausted its own retries - a 4-minute outage therefore
   kept serving stale "no data" for up to 30 minutes after Yahoo
   recovered. Each is now split into a cached inner function that
   RAISES on a persistent failure (st.cache_data never caches a
   raised exception - same established pattern as get_country_mood()'s
   own "Raise so FAILURES never enter the 30-minute cache" comment)
   and a thin, uncached outer wrapper (same name, same callers, same
   return contract) that catches it and returns the exact same
   fallback every existing caller has always gotten. That exception is
   ALSO recorded in a short, separate failure-only memo (_recent_
   fetch_failure, 5-minute TTL - same two-tier-TTL idea as capm_
   engine.get_growth_estimates_5y()'s own _growth_estimate_cache) that
   short-circuits repeat calls for the SAME ticker within that window
   straight to the fallback with no new network attempt at all - a
   first version of this fix that cached nothing on failure regressed
   a real Deep Dive page render (several call sites for one ticker) to
   60+ seconds during an outage, proven directly against this repo's
   own test_deepdive_valuescore_lite_line.py AppTest.

2. deep_dive_engine.analyze() already had a "Market data is
   temporarily unavailable" message for a get_price_history() fetch
   failure (Commit 2, 27 Sep 2026) - but quoteSummary (.info) is a
   DIFFERENT Yahoo endpoint from price history (chart API), so an
   outage that hit only .info (exactly this incident) sailed straight
   through that check (get_price_history still succeeded) and fell
   through to computing a headline from an empty `info` dict: no
   company name, every info-dependent figure silently N/A, no message
   at all. The SAME "fetch_failed" treatment is now applied right
   after get_ticker_info() is called too, via a new get_ticker_info_
   failure_kind side-channel mirroring the existing price-history one.

3. A queued/manual rescan of ONE universe (app.py's Rescan-now admin
   control) used to still run scheduler_engine._run_nightly()'s full
   reprice pass - which measures "scanned tonight" against cfg
   ["universes"] (correctly narrowed to the rescan's own request) but
   "every real universe" against cfg["universe_cadence"] (NEVER
   narrowed) - so a rescan of ONE universe treated all 22 OTHERS as
   "not scanned tonight" and repriced every one of them: a yfinance
   batch download nobody asked for, piled on right after the rescan's
   own fetches. Now skipped entirely for a queued rescan (cfg
   ["is_queued_rescan"]=True) - the rescanned universe's own rows are
   already fresh, so there is nothing left for a reprice pass to do
   within that rescan's own requested scope.

4. nightly_scan.py already had two scan-safety guards (SCAN_
   COMPLETENESS_THRESHOLD, the rate-limit circuit breaker) that
   protect against too few/no ROWS - but a row is still produced
   (just with defaulted/estimated fundamentals) whenever price history
   succeeds even if .info totally failed, so BOTH existing guards
   reset on every produced row regardless of what fed it, and neither
   one catches an info-only outage like this one (same 100% "complete"
   row count, garbage underlying data). A new info_failure_rate check
   (INFO_FAILURE_RATE_DEGRADED_THRESHOLD=0.3, the owner's own "over
   30%" example) tracks .info fetch failures separately via a new
   info_failed_out passthrough and feeds the SAME existing "degraded ->
   don't overwrite a good prior scan" logic the completeness guard
   already uses.

Every check below also proves S&P 500 / ASX 200 output is unaffected
when Yahoo is healthy: a successful fetch is still cached exactly as
before, a healthy scan with low info-failure-rate behaves exactly as
before, and a regular (non-queued-rescan) nightly pass still runs its
reprice pass exactly as before.

Run: python3 tests/test_incident_10oct_yahoo_quotesummary_outage.py
"""
import inspect
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="incident_10oct_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import app
import deep_dive_engine
import nightly_scan
import scheduler_engine as se
import scan_store
import scanner_engine

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


# ======================================================================
# CHECK 1a: _fetch_with_retry(raise_on_failure=True) raises instead of
# returning `fallback` once every attempt is exhausted.
# ======================================================================
def _always_raises():
    raise ConnectionError("boom")


_raised = None
try:
    app._fetch_with_retry(_always_raises, "TICK", "test_label", fallback="FALLBACK",
                           attempts=1, raise_on_failure=True)
except Exception as e:
    _raised = e
check("_fetch_with_retry(raise_on_failure=True) raises on exhausted retries "
      "instead of returning the fallback",
      _raised is not None and "TICK" in str(_raised))

# Default behaviour (raise_on_failure=False, every existing caller that
# hasn't opted in) is completely unchanged - still returns `fallback`.
_result = app._fetch_with_retry(_always_raises, "TICK", "test_label", fallback="FALLBACK",
                                 attempts=1)
check("_fetch_with_retry() without raise_on_failure still returns the fallback "
      "(unchanged default behaviour)",
      _result == "FALLBACK")


# ======================================================================
# CHECK 1b: get_ticker_info()/get_price_history()/get_cashflow_df() -
# a persistent failure is NEVER cached (the very next call re-attempts
# live), but a SUCCESS still gets cached exactly as before (no
# regression when Yahoo is healthy).
# ======================================================================
class _FakeTickerAlwaysFails:
    @property
    def info(self):
        raise ConnectionError("quoteSummary 401")

    def history(self, period="6mo"):
        raise ConnectionError("chart 401")

    @property
    def cashflow(self):
        raise ConnectionError("fundamentals 401")


class _FakeTickerSucceeds:
    info = {"longName": "Test Co", "sharesOutstanding": 1000, "currency": "USD",
            "marketCap": 50000, "a": 1, "b": 2}

    def history(self, period="6mo"):
        return pd.DataFrame({"Close": [1.0, 2.0, 3.0]})

    cashflow = pd.DataFrame({"2026-06-30": [100.0]}, index=["Operating Cash Flow"])


_call_count = {"n": 0}


def _counting_ticker_factory(fake_cls):
    def _factory(ticker):
        _call_count["n"] += 1
        return fake_cls()
    return _factory


# Design note: a failure is NOT simply "never cached" - an earlier
# version of this fix tried that and it regressed a real Deep Dive
# page render to 60+ seconds (proven live against test_deepdive_
# valuescore_lite_line.py's own AppTest - several call sites for the
# SAME ticker, each re-running the full 3-attempt retry from scratch
# during an outage). Instead, a short, SEPARATE failure-only memo
# (_call_with_recent_failure_memo / _recent_fetch_failure, same two-
# tier-TTL idea as capm_engine.get_growth_estimates_5y()'s own
# _growth_estimate_cache) short-circuits repeat calls for the same
# ticker within _RECENT_FETCH_FAILURE_TTL_SECONDS straight to the
# fallback - no new network attempt - while still expiring in minutes,
# nowhere near the original 30-minute bug.
with mock.patch("app.yf.Ticker", side_effect=_counting_ticker_factory(_FakeTickerAlwaysFails)), \
     mock.patch("app.time.sleep"):
    app._recent_fetch_failure.clear()
    _call_count["n"] = 0
    _r1 = app.get_ticker_info("OUTAGETEST")
    _n_after_1 = _call_count["n"]
    _r2 = app.get_ticker_info("OUTAGETEST")
    _n_after_2 = _call_count["n"]

check("get_ticker_info() never raises to its caller on a persistent failure "
      "- returns the same {} fallback as always",
      _r1 == {} and _r2 == {})
check("get_ticker_info(): a second call WITHIN the short failure-memo window is "
      "short-circuited straight to the fallback - no new yf.Ticker() call at all "
      "(this is what keeps a multi-call-site page render fast during an outage)",
      _n_after_2 == _n_after_1)

# Once the short memo window has elapsed, the NEXT call re-attempts
# live (self-heals - never pinned to a stale failure the way the old
# 30-minute cache was). Simulated by backdating the memo's own
# timestamp rather than a real sleep.
with mock.patch("app.yf.Ticker", side_effect=_counting_ticker_factory(_FakeTickerAlwaysFails)), \
     mock.patch("app.time.sleep"):
    app._recent_fetch_failure[("get_ticker_info", "OUTAGETEST")] = (
        time.time() - app._RECENT_FETCH_FAILURE_TTL_SECONDS - 1
    )
    _n_before_3 = _call_count["n"]
    _r3 = app.get_ticker_info("OUTAGETEST")
    _n_after_3 = _call_count["n"]

check("get_ticker_info(): once the short failure-memo window has elapsed, the "
      "next call re-attempts live again (self-heals, never pinned the way the "
      "old 30-minute cache was) - and still never raises to its caller",
      _n_after_3 > _n_before_3 and _r3 == {})

check("the failure memo's own TTL is comfortably far under the original "
      "30-minute (1800s) bug this fix closes",
      app._RECENT_FETCH_FAILURE_TTL_SECONDS < 1800)

with mock.patch("app.yf.Ticker", side_effect=_counting_ticker_factory(_FakeTickerAlwaysFails)), \
     mock.patch("app.time.sleep"):
    app._recent_fetch_failure.clear()
    _call_count["n"] = 0
    _rp1 = app.get_price_history("OUTAGETEST2")
    _n_after_p1 = _call_count["n"]
    _rp2 = app.get_price_history("OUTAGETEST2")
    _n_after_p2 = _call_count["n"]

check("get_price_history() never raises to its caller on a persistent failure "
      "- returns the same empty DataFrame as always",
      _rp1.empty and _rp2.empty)
check("get_price_history(): a second call within the short window is also "
      "short-circuited - no new yf.Ticker() call",
      _n_after_p2 == _n_after_p1)

with mock.patch("app.yf.Ticker", side_effect=_counting_ticker_factory(_FakeTickerAlwaysFails)), \
     mock.patch("app.time.sleep"):
    app._recent_fetch_failure.clear()
    _call_count["n"] = 0
    _rc1 = app.get_cashflow_df("OUTAGETEST3")
    _n_after_c1 = _call_count["n"]
    _rc2 = app.get_cashflow_df("OUTAGETEST3")
    _n_after_c2 = _call_count["n"]

check("get_cashflow_df() never raises to its caller on a persistent failure",
      _rc1.empty and _rc2.empty)
check("get_cashflow_df(): a second call within the short window is also "
      "short-circuited - no new yf.Ticker() call",
      _n_after_c2 == _n_after_c1)

app._recent_fetch_failure.clear()

# A SUCCESS still caches exactly as before - the second call must NOT
# re-invoke yf.Ticker (S&P 500/ASX 200 behaviour when Yahoo is healthy
# is completely unaffected by this fix).
with mock.patch("app.yf.Ticker", side_effect=_counting_ticker_factory(_FakeTickerSucceeds)):
    _call_count["n"] = 0
    _s1 = app.get_ticker_info("HEALTHYTEST")
    _n_after_s1 = _call_count["n"]
    _s2 = app.get_ticker_info("HEALTHYTEST")
    _n_after_s2 = _call_count["n"]

check("get_ticker_info(): a SUCCESS is still cached exactly as before - the "
      "second call is a cache hit, no new yf.Ticker() call", _n_after_s2 == _n_after_s1)
check("get_ticker_info(): the successful result itself is unchanged/correct",
      _s1.get("longName") == "Test Co" and _s2.get("longName") == "Test Co")


# ======================================================================
# CHECK 2: deep_dive_engine.analyze() - a get_ticker_info() failure
# (quoteSummary down) while get_price_history() SUCCEEDS now gets the
# same "temporarily unavailable" message, via the new get_ticker_info_
# failure_kind callable.
# ======================================================================
_healthy_hist = pd.DataFrame(
    {"Open": [10.0] * 30, "High": [10.5] * 30, "Low": [9.5] * 30, "Close": [10.0] * 30,
     "Volume": [1000] * 30},
    index=pd.date_range("2026-08-01", periods=30, freq="D"),
)

_dd_result = deep_dive_engine.analyze(
    "QSTEST",
    get_price_history=lambda t: _healthy_hist,
    get_ticker_info=lambda t: {},
    get_cashflow_df=lambda t: pd.DataFrame(),
    get_price_history_failure_kind=lambda t: None,
    get_ticker_info_failure_kind=lambda t: "rate_limited",
)
check('analyze() reports "fetch_failed" (temporarily unavailable) when price '
      "history succeeds but .info failed - the exact 12:38-12:42 UTC incident shape",
      _dd_result.get("error_kind") == "fetch_failed"
      and "temporarily unavailable" in (_dd_result.get("error") or ""))

# Genuinely sparse/missing info for a real reason (not a fetch failure)
# must NOT trigger the message - unchanged fall-through behaviour.
_dd_result_notfound = deep_dive_engine.analyze(
    "QSTEST2",
    get_price_history=lambda t: _healthy_hist,
    get_ticker_info=lambda t: {},
    get_cashflow_df=lambda t: pd.DataFrame(),
    get_price_history_failure_kind=lambda t: None,
    get_ticker_info_failure_kind=lambda t: None,
)
check("analyze() does NOT report fetch_failed when info is genuinely empty "
      "(not a classified fetch failure) - falls through unchanged",
      _dd_result_notfound.get("error_kind") != "fetch_failed")

# A caller with no access to the new side-channel (get_ticker_info_
# failure_kind left at its None default) behaves exactly as before
# this fix - no crash, no new error path triggered by its absence.
_dd_result_nocb = deep_dive_engine.analyze(
    "QSTEST3",
    get_price_history=lambda t: _healthy_hist,
    get_ticker_info=lambda t: {},
    get_cashflow_df=lambda t: pd.DataFrame(),
    get_price_history_failure_kind=lambda t: None,
)
check("analyze() with no get_ticker_info_failure_kind callable at all "
      "degrades to the old behaviour, unchanged",
      _dd_result_nocb.get("error_kind") != "fetch_failed")


# ======================================================================
# CHECK 3: scheduler_engine._run_nightly()'s reprice pass is skipped
# for a queued rescan (is_queued_rescan=True), but runs exactly as
# before for a normal scheduled nightly pass.
# ======================================================================
reprice_calls = {"n": 0}


def _count_reprice(*a, **k):
    reprice_calls["n"] += 1


_base_cfg = {
    "universes": ["TSX 60"],
    "universe_cadence": {
        "TSX 60": {}, "S&P 500": {}, "ASX 200": {}, "FTSE 100": {},
    },
    "nightly_hour": 20,
    "top100_hour": 23,
}

with mock.patch.object(nightly_scan, "refresh_market_cap_ranking"), \
     mock.patch("scanner_engine.fetch_asx300"), \
     mock.patch("scanner_engine.fetch_allords"), \
     mock.patch("scanner_engine.fetch_asx_listed_companies"), \
     mock.patch("alert_engine.snapshot_previous_values", return_value={}), \
     mock.patch.object(nightly_scan, "run_universe_scan", return_value={"rows": []}), \
     mock.patch.object(nightly_scan, "run_imported_scan", return_value=None), \
     mock.patch("snapshot_store.build_snapshots_from_scan"), \
     mock.patch("alert_engine.check_universe_rows"), \
     mock.patch("insider_engine.refresh_universe"), \
     mock.patch.object(nightly_scan, "run_sector_topup"), \
     mock.patch.object(nightly_scan, "run_attention_topup"), \
     mock.patch.object(nightly_scan, "reprice_universe", side_effect=_count_reprice), \
     mock.patch.object(se, "_build_derived_universes"), \
     mock.patch("alert_engine.run_extra_ticker_pass"), \
     mock.patch("alert_engine.send_batched_notifications"), \
     mock.patch("results_engine.check_results_day"):
    reprice_calls["n"] = 0
    _queued_logs = []
    se._run_nightly({**_base_cfg, "is_queued_rescan": True}, _queued_logs.append,
                     run_night="2026-10-10")

check("a queued rescan (is_queued_rescan=True) of ONE universe does NOT reprice "
      "the other 3 universes in universe_cadence - this is the exact bug the "
      "10 Oct incident report describes",
      reprice_calls["n"] == 0)
check('the skip is logged, explaining why (never silent)',
      any("reprice pass: skipped" in line and "queued/manual rescan" in line
          for line in _queued_logs))

with mock.patch.object(nightly_scan, "refresh_market_cap_ranking"), \
     mock.patch("scanner_engine.fetch_asx300"), \
     mock.patch("scanner_engine.fetch_allords"), \
     mock.patch("scanner_engine.fetch_asx_listed_companies"), \
     mock.patch("alert_engine.snapshot_previous_values", return_value={}), \
     mock.patch.object(nightly_scan, "run_universe_scan", return_value={"rows": []}), \
     mock.patch.object(nightly_scan, "run_imported_scan", return_value=None), \
     mock.patch("snapshot_store.build_snapshots_from_scan"), \
     mock.patch("alert_engine.check_universe_rows"), \
     mock.patch("insider_engine.refresh_universe"), \
     mock.patch.object(nightly_scan, "run_sector_topup"), \
     mock.patch.object(nightly_scan, "run_attention_topup"), \
     mock.patch.object(nightly_scan, "reprice_universe", side_effect=_count_reprice), \
     mock.patch.object(se, "_build_derived_universes"), \
     mock.patch("alert_engine.run_extra_ticker_pass"), \
     mock.patch("alert_engine.send_batched_notifications"), \
     mock.patch("results_engine.check_results_day"):
    reprice_calls["n"] = 0
    _normal_logs = []
    se._run_nightly(dict(_base_cfg), _normal_logs.append, run_night="2026-10-10")

check("a REGULAR scheduled nightly pass (no is_queued_rescan flag) still reprices "
      "every universe not scanned tonight, exactly as before this fix - unchanged "
      "behaviour, S&P 500/ASX 200's own real nightly pass is unaffected",
      reprice_calls["n"] == 3)


# ======================================================================
# CHECK 4a: analyze_ticker_lite()'s new info_failed_out flag - set
# only on a TOTAL .info failure, never on a genuine-but-sparse info
# dict, and never when the row itself failed for an unrelated reason
# (no price data at all - returns None before .info is even reached).
# ======================================================================
_cf_ok = pd.DataFrame(
    {"2026-06-30": [500.0, -50.0], "2025-06-30": [480.0, -48.0], "2024-06-30": [460.0, -46.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)


class _NightlyFakeTicker:
    def __init__(self, hist, info_raises=False, info_value=None):
        self._hist = hist
        self._info_raises = info_raises
        self._info_value = info_value if info_value is not None else {}
        self.fast_info = {}

    def history(self, period="6mo"):
        return self._hist

    @property
    def info(self):
        if self._info_raises:
            raise ConnectionError("quoteSummary 401")
        return self._info_value

    @property
    def cashflow(self):
        return _cf_ok


_hist_ok = pd.DataFrame(
    {"Open": [10.0] * 60, "High": [10.5] * 60, "Low": [9.5] * 60,
     "Close": [10.0] * 60, "Volume": [100000] * 60},
    index=pd.date_range("2026-07-01", periods=60, freq="D"),
)

with mock.patch("nightly_scan.yf.Ticker", return_value=_NightlyFakeTicker(_hist_ok, info_raises=True)), \
     mock.patch("nightly_scan.time.sleep"):
    _flag = [False]
    _row = nightly_scan.analyze_ticker_lite("INFOFAILTEST", log=lambda *a: None, info_failed_out=_flag)

check("analyze_ticker_lite(): a total .info failure sets info_failed_out[0]=True",
      _flag[0] is True)
check("analyze_ticker_lite(): a row is STILL produced (price history succeeded) - "
      "this is exactly why the old completeness/breaker guards never caught this",
      _row is not None)

with mock.patch("nightly_scan.yf.Ticker",
                 return_value=_NightlyFakeTicker(_hist_ok, info_raises=False, info_value={"sector": "Tech"})), \
     mock.patch("nightly_scan.time.sleep"):
    _flag2 = [False]
    _row2 = nightly_scan.analyze_ticker_lite("INFOOKTEST", log=lambda *a: None, info_failed_out=_flag2)

check("analyze_ticker_lite(): a genuinely sparse-but-successful info dict does "
      "NOT set info_failed_out - only a total fetch failure does",
      _flag2[0] is False)

with mock.patch("nightly_scan.yf.Ticker",
                 return_value=_NightlyFakeTicker(pd.DataFrame(), info_raises=True)), \
     mock.patch("nightly_scan.time.sleep"):
    _flag3 = [False]
    _row3 = nightly_scan.analyze_ticker_lite("NOPRICETEST", log=lambda *a: None, info_failed_out=_flag3)

check("analyze_ticker_lite(): no price history at all returns None before .info "
      "is even reached - info_failed_out stays False (a different failure mode)",
      _row3 is None and _flag3[0] is False)


# ======================================================================
# CHECK 4b: run_universe_scan()'s new info_failure_rate guard - an
# info-only outage across most tickers (price succeeds, info fails)
# is flagged degraded and does NOT overwrite a good prior scan, even
# though every ticker still produced a row (100% "complete" by the
# old completeness guard alone).
# ======================================================================
import pandas as pd  # noqa: F811 (already imported above; explicit for clarity here)

fake_pool_df = pd.DataFrame({"Ticker": [f"T{i}.AX" for i in range(10)]})
PRIOR_GOOD_SCAN = {"rows": [{"Ticker": f"T{i}.AX", "Long Score": 50.0} for i in range(10)],
                    "source": "test", "degraded": False}


def _make_mostly_info_failed(n_info_failed):
    """7/10 (or however many requested) tickers get info_failed_out=True
    but STILL return a row (price succeeded) - every ticker produces a
    row either way, so completeness is 100% and the OLD guard alone
    would see nothing wrong."""
    def _side_effect(ticker, attention_lite=True, discount_rate=None, perpetual_rate=None,
                      growth_rate=None, manual_fcf=None, log=print, rate_limited_out=None,
                      growth_summary_out=None, oneoff_summary_out=None, shadow_out=None,
                      info_failed_out=None):
        idx = int(ticker[1:].split(".")[0])
        if info_failed_out is not None and idx < n_info_failed:
            info_failed_out[0] = True
        return {"Ticker": ticker, "Price": 10.0, "Long Score": 50.0}
    return _side_effect


with mock.patch("scanner_engine.get_universe_pool", return_value=(fake_pool_df, "test source")), \
     mock.patch.object(nightly_scan, "analyze_ticker_lite", side_effect=_make_mostly_info_failed(7)), \
     mock.patch("scan_checkpoint_store.load", return_value=None), \
     mock.patch.object(scan_store, "load_scan", return_value=PRIOR_GOOD_SCAN), \
     mock.patch.object(scan_store, "save_scan") as _save_scan:
    _degraded_logs = []
    _result = nightly_scan.run_universe_scan("Dow Jones 30", log=_degraded_logs.append, run_night="2026-10-10")

check("7/10 (70%) tickers with a failed .info fetch -> run_universe_scan() does "
      "NOT save (keeps the last known-good scan), even though every ticker "
      "produced a row (100% complete by the old row-count guard alone)",
      _result is None and not _save_scan.called)
check("the info-failure-rate reason is logged clearly",
      any("failed a .info fetch" in line or "had a failed .info fetch" in line
          for line in _degraded_logs))

# Healthy case: well under 30% info failures -> saves normally, S&P
# 500/ASX 200 behaviour when Yahoo is healthy is completely unaffected.
with mock.patch("scanner_engine.get_universe_pool", return_value=(fake_pool_df, "test source")), \
     mock.patch.object(nightly_scan, "analyze_ticker_lite", side_effect=_make_mostly_info_failed(1)), \
     mock.patch("scan_checkpoint_store.load", return_value=None), \
     mock.patch.object(scan_store, "load_scan", return_value=PRIOR_GOOD_SCAN), \
     mock.patch.object(scan_store, "save_scan", return_value={"rows": [], "degraded": False}) as _save_scan_ok:
    _healthy_logs = []
    _result_ok = nightly_scan.run_universe_scan("Dow Jones 30", log=_healthy_logs.append, run_night="2026-10-10")

check("1/10 (10%) info failures - well under the 30% threshold - saves "
      "normally, exactly as before this fix (healthy-Yahoo behaviour unchanged)",
      _save_scan_ok.called)
check("save_scan() was called with degraded=False for the healthy case",
      _save_scan_ok.call_args.kwargs.get("degraded") is False)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
