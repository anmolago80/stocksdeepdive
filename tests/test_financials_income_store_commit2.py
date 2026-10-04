"""
Commit 2 of instruction_financials_income_store_and_top200_guard.md
(4 Oct 2026, Director-directed) - the financials income store and its
nightly fill. With this commit alone nothing reads the store to value
anything; these checks cover the store itself (round trip) and the
three ways an entry gets filled, plus the budget/priority/circuit-
breaker/refresh/log-line mechanics of the paid pre-pass.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. No test here makes a live network call.

Run: python3 tests/test_financials_income_store_commit2.py
"""
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="financials_income_store_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import financials_income_store as fis
import fcf_valuation_engine as fve
import nightly_scan as ns
import scan_store
import fundamentals_data


def _mk_income_df(net_income, operating_income=None, da=None, cols=None):
    cols = cols or [f"202{5 - i}-12-31" for i in range(len(net_income))]
    data = {"Net Income Common Stockholders": net_income}
    if operating_income is not None:
        data["Operating Income"] = operating_income
    if da is not None:
        data["Reconciled Depreciation"] = da
    df = pd.DataFrame(data, index=cols).T
    return df


KNSL_INCOME = _mk_income_df([412.0, 380.0, 350.0, 300.0, 250.0])


# ======================================================================
# C1: round trip - save()/get(), currency_converted preserved, values
# match to the cent.
# ======================================================================
fis.save("KNSL", KNSL_INCOME, currency="USD", source="yfinance_bundle_oneoff",
         latest_cf_period="2025-12-31", currency_converted=True)
_entry = fis.get("KNSL")
assert _entry is not None
assert _entry["currency"] == "USD"
assert _entry["currency_converted"] is True
assert _entry["source"] == "yfinance_bundle_oneoff"
assert _entry["latest_cf_period"] == "2025-12-31"
assert _entry["stale"] is False
pd.testing.assert_frame_equal(
    _entry["income"].astype(float), KNSL_INCOME.astype(float), check_names=False)
print("[C1_round_trip] save()/get() round trip preserves currency/currency_converted/source/"
      "latest_cf_period/stale and the income table to the cent OK")

assert fis.get("NEVER_SAVED") is None
print("[C1_missing_ticker] get() on a ticker with no entry at all returns None OK")


# ======================================================================
# C2: the stored table, fed through the REAL normalized_base_and_series(),
# gives the identical intrinsic-value base/source/distorted-year result
# as feeding the same income_df directly - the store round trip changes
# nothing about the numbers.
# ======================================================================
_direct = fve.normalized_base_and_series(pd.DataFrame(), info={"sector": "Financial Services"},
                                          income_df=KNSL_INCOME)
_via_store = fve.normalized_base_and_series(pd.DataFrame(), info={"sector": "Financial Services"},
                                             income_df=fis.get("KNSL")["income"])
assert _direct[0] == _via_store[0], (_direct[0], _via_store[0])
assert _direct[2] == _via_store[2] == "net_income_financials", (_direct[2], _via_store[2])
assert _direct[5]["fcf_distorted_years"] == _via_store[5]["fcf_distorted_years"]
print(f"[C2_store_roundtrip_values_match] base={_direct[0]} source={_direct[2]!r} identical "
      "whether income_df came straight from the fixture or round-tripped through the store OK")


# ======================================================================
# C3: mark_stale()/refresh_if_newer() - "a stale series is still used
# until its refresh lands."
# ======================================================================
assert fis.mark_stale("NEVER_SAVED") is False
assert fis.mark_stale("KNSL") is True
assert fis.get("KNSL")["stale"] is True
assert fis.is_stale_or_missing("KNSL") is True
# A fresh save() clears the flag again.
fis.save("KNSL", KNSL_INCOME, currency="USD", source="yfinance_bundle_oneoff",
         latest_cf_period="2025-12-31", currency_converted=True)
assert fis.get("KNSL")["stale"] is False
print("[C3_mark_stale] mark_stale() flags an existing entry (no-op on a missing one); a fresh "
      "save() clears the flag again OK")

fis.refresh_if_newer("KNSL", "2025-12-31")
assert fis.get("KNSL")["stale"] is False, "same period as stored - not stale"
fis.refresh_if_newer("KNSL", "2026-12-31")
assert fis.get("KNSL")["stale"] is True, "a newer cash-flow period marks the stored entry stale"
assert fis.get("KNSL")["income"] is not None and not fis.get("KNSL")["income"].empty, \
    "still used (not deleted) until its refresh lands"
print("[C3_refresh_if_newer] a cash-flow period no newer than stored -> unchanged; a genuinely "
      "newer one -> marked stale, data left in place (still usable) OK")


# ======================================================================
# C4: Way 1 (free) - nightly_scan.analyze_ticker_lite()'s existing
# one-off-check fetch, for a financials-mode ticker, lands in the store.
# ======================================================================
for _f in os.listdir(fis._store_dir()):
    os.remove(os.path.join(fis._store_dir(), _f))

_dates = pd.date_range("2026-01-01", periods=90, freq="D")
_hist_df = pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(90)],
                          "Volume": [1_000_000] * 90}, index=_dates)
KO_CF = pd.DataFrame(
    {"2026-06-30": [588.0, -150.0], "2025-06-30": [1200.0, -150.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)


def _ytw_side_effect(fn, log, ticker, label, **kw):
    if label == "history":
        return _hist_df
    if label == "info":
        return {"currency": "USD", "sector": "Financial Services"}
    if label == "cashflow":
        return KO_CF
    if label == "one-off check income statement":
        return KNSL_INCOME
    return None


with mock.patch.object(ns, "_yf_call_with_retry") as _ytw, \
     mock.patch.object(ns, "resolve_quality_score", return_value=(70, "auto", False)), \
     mock.patch.object(ns, "resolve_intrinsic_value") as _riv, \
     mock.patch.object(ns, "resolve_stock_type", return_value=("COMPOUNDER", "auto", False)), \
     mock.patch.object(ns.reverse_dcf_engine, "compute", return_value={"ok": False}), \
     mock.patch.object(ns.trade_filter_engine, "calc_support_resistance",
                        return_value={"support20": 90, "resistance20": 110,
                                      "support60": 85, "resistance60": 115}), \
     mock.patch.object(ns.indicators_engine, "compute_indicators", return_value={"trend": "up"}), \
     mock.patch.object(ns.trade_filter_engine, "evaluate_trade", return_value={"signal": "-"}), \
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None):
    _ytw.side_effect = _ytw_side_effect
    _riv.return_value = (150.0, "dcf", 0.12, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "ok", "value_default": False,
        "fcf_distorted_years": [],
    })
    _row = ns.analyze_ticker_lite("WAY1TICK", log=lambda *a, **k: None)

assert _row is not None
_entry1 = fis.get("WAY1TICK")
assert _entry1 is not None, "Way 1 (free, already-fetched-via-one-off-check) should have saved an entry"
assert _entry1["source"] == "yfinance_bundle_oneoff", _entry1["source"]
assert _entry1["currency_converted"] is True
pd.testing.assert_frame_equal(_entry1["income"].astype(float), KNSL_INCOME.astype(float),
                               check_names=False)
print("[C4_way1_free_oneoff_fetch] a financials-mode ticker whose income statement the "
      "existing one-off check already fetched tonight lands in the store, no new call, "
      "currency_converted=True OK")


# ======================================================================
# C5: Way 2 (free) - a cached fundamentals bundle (no fresh one-off
# fetch this run) fills the store instead.
# ======================================================================
for _f in os.listdir(fis._store_dir()):
    os.remove(os.path.join(fis._store_dir(), _f))

STABLE_CF = pd.DataFrame(
    {"2026-06-30": [1000.0, -100.0], "2025-06-30": [980.0, -100.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
# Latest column must be at least as fresh as STABLE_CF's own latest
# column ("2026-06-30") for the Way 2 freshness check to accept it.
WAY2_INCOME = _mk_income_df([412.0, 380.0, 350.0, 300.0, 250.0],
                             cols=["2026-06-30", "2025-06-30", "2024-06-30",
                                   "2023-06-30", "2022-06-30"])
_cached_bundle = {
    "income": WAY2_INCOME, "info": {"currency": "AUD"},
    "meta": {"source": "yfinance"},
}


def _ytw_side_effect_way2(fn, log, ticker, label, **kw):
    if label == "history":
        return _hist_df
    if label == "info":
        return {"currency": "AUD", "sector": "Financial Services"}
    if label == "cashflow":
        return STABLE_CF
    return None  # needs_oneoff_check(STABLE_CF) is False - one-off fetch never called


with mock.patch.object(ns, "_yf_call_with_retry") as _ytw2, \
     mock.patch.object(ns, "resolve_quality_score", return_value=(70, "auto", False)), \
     mock.patch.object(ns, "resolve_intrinsic_value") as _riv2, \
     mock.patch.object(ns, "resolve_stock_type", return_value=("COMPOUNDER", "auto", False)), \
     mock.patch.object(ns.reverse_dcf_engine, "compute", return_value={"ok": False}), \
     mock.patch.object(ns.trade_filter_engine, "calc_support_resistance",
                        return_value={"support20": 90, "resistance20": 110,
                                      "support60": 85, "resistance60": 115}), \
     mock.patch.object(ns.indicators_engine, "compute_indicators", return_value={"trend": "up"}), \
     mock.patch.object(ns.trade_filter_engine, "evaluate_trade", return_value={"signal": "-"}), \
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=_cached_bundle):
    _ytw2.side_effect = _ytw_side_effect_way2
    _riv2.return_value = (150.0, "dcf", 0.12, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "ok", "value_default": False,
        "fcf_distorted_years": [],
    })
    _row2 = ns.analyze_ticker_lite("WAY2TICK", log=lambda *a, **k: None)

assert _row2 is not None
_entry2 = fis.get("WAY2TICK")
assert _entry2 is not None, "Way 2 (free, cached bundle) should have saved an entry"
assert _entry2["source"] == "yfinance_bundle_cached", _entry2["source"]
assert _entry2["currency"] == "AUD"
print("[C5_way2_free_cached_bundle] a financials-mode ticker with no fresh one-off fetch this "
      "run, but a cached yfinance bundle on disk, fills the store via the free cached-bundle "
      "path instead OK")

# A non-financials ticker (no sector/industry match) never touches the store at all.
for _f in os.listdir(fis._store_dir()):
    os.remove(os.path.join(fis._store_dir(), _f))
with mock.patch.object(ns, "_yf_call_with_retry") as _ytw3, \
     mock.patch.object(ns, "resolve_quality_score", return_value=(70, "auto", False)), \
     mock.patch.object(ns, "resolve_intrinsic_value") as _riv3, \
     mock.patch.object(ns, "resolve_stock_type", return_value=("COMPOUNDER", "auto", False)), \
     mock.patch.object(ns.reverse_dcf_engine, "compute", return_value={"ok": False}), \
     mock.patch.object(ns.trade_filter_engine, "calc_support_resistance",
                        return_value={"support20": 90, "resistance20": 110,
                                      "support60": 85, "resistance60": 115}), \
     mock.patch.object(ns.indicators_engine, "compute_indicators", return_value={"trend": "up"}), \
     mock.patch.object(ns.trade_filter_engine, "evaluate_trade", return_value={"signal": "-"}), \
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=_cached_bundle) as _peek:
    def _ytw_side_effect_nonfin(fn, log, ticker, label, **kw):
        if label == "history":
            return _hist_df
        if label == "info":
            return {"currency": "USD", "sector": "Technology"}
        if label == "cashflow":
            return STABLE_CF
        return None
    _ytw3.side_effect = _ytw_side_effect_nonfin
    _riv3.return_value = (150.0, "dcf", 0.12, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "ok", "value_default": False, "fcf_distorted_years": [],
    })
    ns.analyze_ticker_lite("NONFIN", log=lambda *a, **k: None)
assert fis.get("NONFIN") is None
print("[C5_nonfinancials_untouched] a non-financials ticker (sector Technology) never writes "
      "to the store, even with a cached bundle sitting right there OK")


# ======================================================================
# C6: budget cap + priority order (stale > public-due > public-other >
# private) + dedup across universes, via run_nightly_prepass().
# ======================================================================
for _f in os.listdir(fis._store_dir()):
    os.remove(os.path.join(fis._store_dir(), _f))
for _f in os.listdir(scan_store._data_dir()):
    os.remove(os.path.join(scan_store._data_dir(), _f))
os.environ.pop("PRIVATE_UNIVERSES", None)


def _mk_rows(tickers_scores, mode="financials"):
    return [{"Ticker": t, "Long Score": s, "Moat Mode": mode} for t, s in tickers_scores]


scan_store.save_scan("S&P 500", _mk_rows([("DUE_HI", 90), ("DUE_LO", 10), ("ALSOSTD", 50)],
                                          mode="financials"), source_label="fixture")
scan_store.save_scan("Russell 2000", _mk_rows([("OTHERPUB_HI", 80), ("OTHERPUB_LO", 5)]),
                      source_label="fixture")
scan_store.save_scan("FTSE 100", _mk_rows([("PRIV_HI", 99)]), source_label="fixture")
# ALSOSTD is non-financials in S&P 500's own saved row - override it standard so it's excluded.
scan_store.save_scan("S&P 500", _mk_rows([("DUE_HI", 90), ("DUE_LO", 10)], mode="financials")
                      + [{"Ticker": "ALSOSTD", "Long Score": 50, "Moat Mode": "standard"}],
                      source_label="fixture")
# DUE_HI also appears, at a lower score, in the weekly-cadence universe - dedup keeps it once,
# at its higher score's priority position.
scan_store.save_scan("Russell 2000", _mk_rows([("OTHERPUB_HI", 80), ("OTHERPUB_LO", 5),
                                                ("DUE_HI", 20)]), source_label="fixture")
# A ticker already fresh in the store must be skipped entirely (counted "from store").
fis.save("DUE_LO", KNSL_INCOME, currency="USD", source="yfinance_bundle_oneoff",
         latest_cf_period="2025-12-31", currency_converted=True)
# A stale ticker must be fetched FIRST, ahead of every "no entry" candidate.
fis.save("OTHERPUB_LO", KNSL_INCOME, currency="USD", source="yfinance_bundle_oneoff",
         latest_cf_period="2020-12-31", currency_converted=True)
fis.mark_stale("OTHERPUB_LO")

_candidates, _unis = fis.select_candidates(due_universes=["S&P 500"])
assert _candidates[0] == "OTHERPUB_LO", _candidates  # stale always first
# DUE_HI (public, due tonight, score 90) must outrank OTHERPUB_HI (public, not due, score 80).
assert _candidates.index("DUE_HI") < _candidates.index("OTHERPUB_HI"), _candidates
# Any public candidate must outrank the private one.
assert _candidates.index("OTHERPUB_HI") < _candidates.index("PRIV_HI"), _candidates
assert "ALSOSTD" not in _candidates, "non-financials row must never be a candidate"
assert _unis["DUE_HI"] == {"S&P 500", "Russell 2000"}, _unis["DUE_HI"]
print(f"[C6_priority_order] {_candidates} - stale first, then public-due, public-other, "
      "private last; a non-financials row excluded; a multi-universe ticker deduped OK")

RL_INCOME = _mk_income_df([50.0, 48.0, 46.0, 44.0, 42.0])


class _FakeSucceedingTicker:
    def __init__(self, *a, **kw):
        pass

    @property
    def income_stmt(self):
        return RL_INCOME


# budget=2, and 4 candidates genuinely need a paid fetch (OTHERPUB_LO
# stale, DUE_HI/OTHERPUB_HI/PRIV_HI no entry) - with every fetch
# SUCCEEDING (mocked, no network, no rate-limit), the cap itself - not a
# sandbox network failure - must be what stops it at exactly 2.
_budget_log = []
with mock.patch.object(fis.fundamentals_data, "peek_cached_bundle", return_value=None):
    with mock.patch("yfinance.Ticker", _FakeSucceedingTicker):
        _result = fis.run_nightly_prepass(["S&P 500"], log=_budget_log.append, budget=2)
assert _result["from_store"] == 1, _result   # DUE_LO, already fresh
assert _result["fetched"] == 2, _result      # exactly the budget, every fetch succeeding
assert _result["budget_deferred"] == 2, _result  # the other 2 of the 4 paid candidates
_log_lines = [ln for ln in _budget_log if ln.startswith("[scan] financials income:")]
assert len(_log_lines) == 1, _log_lines  # one universe passed in -> exactly one line
print(f"[C6_budget_cap_and_log_line] budget=2, every paid fetch SUCCEEDING -> {_result} - the "
      f"cap itself (not a fetch failure) stops it at exactly 2 fetched, log line "
      f"{_log_lines[0]!r} (one line for the one due-tonight universe passed in) OK")


# ======================================================================
# C7: circuit breaker - consecutive rate-limited failures stop the
# paid fetch loop for the rest of the night.
# ======================================================================
for _f in os.listdir(fis._store_dir()):
    os.remove(os.path.join(fis._store_dir(), _f))
scan_store.save_scan(
    "S&P 500",
    _mk_rows([(f"RL{i}", 100 - i) for i in range(ns.RATE_LIMIT_CONSECUTIVE_ABORT_THRESHOLD + 3)]),
    source_label="fixture",
)


class _FakeRateLimitedTicker:
    def __init__(self, *a, **kw):
        pass

    @property
    def income_stmt(self):
        raise Exception("429 rate limit")


_breaker_log = []
with mock.patch.object(fis.fundamentals_data, "peek_cached_bundle", return_value=None):
    with mock.patch("yfinance.Ticker", _FakeRateLimitedTicker), \
         mock.patch.object(ns, "time") as _time_mock:
        _time_mock.sleep = lambda *a, **k: None
        _result_rl = fis.run_nightly_prepass(["S&P 500"], log=_breaker_log.append, budget=1000)
assert _result_rl["breaker_tripped"] is True, _result_rl
assert any("circuit breaker tripped" in ln for ln in _breaker_log), _breaker_log
print(f"[C7_circuit_breaker] {ns.RATE_LIMIT_CONSECUTIVE_ABORT_THRESHOLD}+ consecutive "
      f"rate-limited fetches -> breaker_tripped=True, {_result_rl} - stops spending the paid "
      "budget for the rest of the night OK")


# ======================================================================
# C8: a fetch failure leaves the store untouched (no entry written, no
# existing entry corrupted).
# ======================================================================
for _f in os.listdir(fis._store_dir()):
    os.remove(os.path.join(fis._store_dir(), _f))
for _f in os.listdir(scan_store._data_dir()):
    os.remove(os.path.join(scan_store._data_dir(), _f))
scan_store.save_scan("S&P 500", _mk_rows([("FAILTICK", 50)]), source_label="fixture")


class _FakeFailingTicker:
    def __init__(self, *a, **kw):
        pass

    @property
    def income_stmt(self):
        raise Exception("not a rate limit - some other failure")


with mock.patch.object(fis.fundamentals_data, "peek_cached_bundle", return_value=None):
    with mock.patch("yfinance.Ticker", _FakeFailingTicker), \
         mock.patch.object(ns, "time") as _time_mock2:
        _time_mock2.sleep = lambda *a, **k: None
        _result_fail = fis.run_nightly_prepass(["S&P 500"], log=lambda *a: None, budget=1000)
assert fis.get("FAILTICK") is None, "a total fetch failure must never write a bad/empty entry"
assert _result_fail["budget_deferred"] == 1, _result_fail
print("[C8_fetch_failure_leaves_store_untouched] a total income_stmt fetch failure writes "
      "nothing to the store (counted budget-deferred, not fetched) OK")


print("\nALL FINANCIALS INCOME STORE COMMIT 2 CHECKS PASSED")
