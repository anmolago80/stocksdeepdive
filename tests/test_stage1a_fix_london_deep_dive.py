"""
Stage 1a-fix - London pence fix did not reach the Deep Dive headline;
guard misfires; two rate-fetch bugs (Director-directed, 3 Oct 2026).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC. This sandbox has no outbound
network access (confirmed below: every live yfinance/requests call
fails and falls through to this commit's own fallback paths) - nothing
here was run against a real Yahoo quote or a real Bank of England/Bank
of Canada response. The Railway log, after a real deploy, is what
confirms the live paths actually work against the real services - see
this fix's own report (section 6, "live check Andrew repeats") for
what to look for.

F1/F3: deep_dive_engine.analyze() and nightly_scan.analyze_ticker_lite()
are exercised END-TO-END (not just fundamentals_data.get_bundle()) on a
GBp fixture, proving the Deep Dive headline/scan row price, currency
label and MOS are all correct now - this is exactly the path Stage 1a's
own test suite never exercised (see T1 in the fix commit's own report
for why).

F2: fundamentals_data.get_bundle() is exercised with a WORKING fast_info
(Stage 1a's own T1-T4 fixtures all raise AttributeError from fast_info,
which is why the overlay/normalise ordering bug shipped past that
suite without being caught) to reproduce and then confirm the fix for
the live mcap_check=0.01 bug.

F4/F5: capm_engine's BoE/BoC live-fetch functions, mocking requests.get.

F6: fcf_valuation_engine.fx_rate()'s own live/fallback logging.

T7 ("real", not a recomputed-from-constants argument): the five USD/AUD
fixtures run against BOTH the code at commit 1f768ba (the commit
immediately before Stage 1a, loaded via `git show` into an isolated
sys.modules bubble - see _load_module_from_git()) and this commit's own
current working code, asserting byte-identical output and printing the
before/after table.

Run: python3 tests/test_stage1a_fix_london_deep_dive.py
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="stage1afix_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import capm_engine as capm
import deep_dive_engine
import fcf_valuation_engine as fcf
import fundamentals_data as fd
import nightly_scan


def _mk_cashflow_df(ocf, capex):
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


# ======================================================================
# F1/F3 E2E #1: deep_dive_engine.analyze() on a SHEL.L-shaped (T1-shaped)
# GBp fixture reporting in USD - the exact live symptom (pence price
# next to a genuinely-pounds Intrinsic Value, MOS -4624%) must not
# reproduce. get_ticker_info()/get_price_history()/get_cashflow_df()
# are passed as plain callables, exactly as app.py wires them.
# ======================================================================
_E1_SHARES = 1_000_000
_E1_PRICE_PENCE = 2600.0
_e1_info = {
    "currency": "GBp", "financialCurrency": "USD",
    "currentPrice": _E1_PRICE_PENCE, "marketCap": 26.0 * _E1_SHARES,
    "sharesOutstanding": _E1_SHARES, "longName": "SHEL-shaped",
}
_e1_cf = _mk_cashflow_df([14_000_000.0] * 5, [-1_400_000.0] * 5)
_e1_dates = pd.date_range("2026-04-01", periods=130, freq="D")
_e1_hist = pd.DataFrame({
    "Open": [_E1_PRICE_PENCE] * 130, "High": [_E1_PRICE_PENCE * 1.01] * 130,
    "Low": [_E1_PRICE_PENCE * 0.99] * 130, "Close": [_E1_PRICE_PENCE] * 130,
    "Volume": [1_000_000] * 130,
}, index=_e1_dates)

_e1_log = io.StringIO()
with contextlib.redirect_stdout(_e1_log):
    _e1_result = deep_dive_engine.analyze(
        "SHEL.L",
        get_price_history=lambda t: _e1_hist,
        get_ticker_info=lambda t: dict(_e1_info),
        get_cashflow_df=lambda t: _e1_cf,
        live_data=False, enable_social=False,
    )

assert _e1_result["error"] is None, _e1_result["error"]
assert _e1_result["price"] == 26.0, _e1_result["price"]
assert _e1_result["currency"] == "GBP", _e1_result["currency"]
assert _e1_result["price_unit_suspect"] is False, _e1_result["price_unit_suspect_reason"]
assert _e1_result["intrinsic_value"] > 0
assert -100 < _e1_result["mos"] < 95, _e1_result["mos"]
assert _e1_result["dcf_growth"] is not None and _e1_result["dcf_growth"] < 30, _e1_result["dcf_growth"]
_e1_log_text = _e1_log.getvalue()
assert "[units] SHEL.L GBp->GBP" in _e1_log_text and " ok" in _e1_log_text, _e1_log_text
print(f"[F1_F3_e2e_deep_dive] SHEL.L-shaped Deep Dive headline: price=£{_e1_result['price']:.2f} "
      f"(was 3,589.00 pence live, pre-fix), currency=GBP (never GBp), IV=£{_e1_result['intrinsic_value']:.2f}, "
      f"MOS={_e1_result['mos']:.1f}% (was -4624.2% live, pre-fix), guard ratio logged 'ok' OK")


# ======================================================================
# F1/F3 E2E #2: nightly_scan.analyze_ticker_lite() on a T3-shaped GBp
# fixture (reports in GBP, no FX step) - the scan-row path, a
# COMPLETELY separate raw fetch from deep_dive_engine's own (see this
# module's own docstring on why it duplicates rather than imports
# app.py's fetchers).
# ======================================================================
_E2_SHARES = 2_000_000
_E2_PRICE_PENCE = 1000.0
_e2_info = {
    "currency": "GBp", "financialCurrency": "GBP",
    "currentPrice": _E2_PRICE_PENCE, "marketCap": 10.0 * _E2_SHARES,
    "sharesOutstanding": _E2_SHARES, "longName": "T3-shaped",
}
_e2_cf = _mk_cashflow_df([3_000_000.0] * 5, [-300_000.0] * 5)
_e2_hist = pd.DataFrame({
    "Open": [_E2_PRICE_PENCE] * 130, "High": [_E2_PRICE_PENCE * 1.01] * 130,
    "Low": [_E2_PRICE_PENCE * 0.99] * 130, "Close": [_E2_PRICE_PENCE] * 130,
    "Volume": [500_000] * 130,
}, index=_e1_dates)


class _E2FakeTicker:
    def __init__(self, *_a, **_k):
        self.info = dict(_e2_info)
        self.cashflow = _e2_cf
        self.dividends = pd.Series(dtype=float)

    def history(self, *a, **k):
        return _e2_hist


_e2_log = io.StringIO()
with mock.patch.object(nightly_scan, "yf") as _myf, contextlib.redirect_stdout(_e2_log):
    _myf.Ticker.side_effect = _E2FakeTicker
    _e2_row = nightly_scan.analyze_ticker_lite("T3.L")

assert _e2_row is not None
assert _e2_row["Price"] == 10.0, _e2_row["Price"]
assert -100 < _e2_row["MOS %"] < 95, _e2_row["MOS %"]
assert _e2_row["Intrinsic Value"] > 0
_implied_growth = _e2_row.get("Implied Growth %")
assert _implied_growth is None or abs(_implied_growth) < 30, _implied_growth
_e2_log_text = _e2_log.getvalue()
assert "[units] T3.L GBp->GBP" in _e2_log_text and " ok" in _e2_log_text, _e2_log_text
print(f"[F1_F3_e2e_nightly_scan] T3.L-shaped scan row: price=£{_e2_row['Price']:.2f}, "
      f"MOS={_e2_row['MOS %']:.1f}%, implied growth={_implied_growth}% (finite, <30%), "
      f"guard ratio logged 'ok' OK")


# ======================================================================
# F2: fundamentals_data.get_bundle() with a WORKING fast_info (Stage
# 1a's own T1-T4 fixtures all raise AttributeError from fast_info,
# which is exactly why this ordering bug shipped past that suite) -
# reproduces the live SHEL.L mcap_check=0.01 symptom and confirms the
# fix (ratio now ~1.00, not 0.01).
# ======================================================================
_F2_SHARES = 1_000_000
_f2_info = {
    "currency": "GBp", "currentPrice": 2600.0, "regularMarketPrice": 2600.0,
    "marketCap": 26.0 * _F2_SHARES, "sharesOutstanding": _F2_SHARES,
}


class _F2FastInfo:
    def __getitem__(self, key):
        if key == "last_price":
            return 2600.0
        raise KeyError(key)


class _F2FakeTicker:
    def __init__(self, *_a, **_k):
        self.info = dict(_f2_info)
        self.income_stmt = pd.DataFrame()
        self.balance_sheet = pd.DataFrame()
        self.cashflow = pd.DataFrame()
        self.quarterly_income_stmt = pd.DataFrame()
        self.dividends = pd.Series(dtype=float)

    @property
    def fast_info(self):
        return _F2FastInfo()

    def history(self, *a, **k):
        return pd.DataFrame()


_f2_log = io.StringIO()
with mock.patch.object(fd, "yf") as _myf, contextlib.redirect_stdout(_f2_log):
    _myf.Ticker.side_effect = _F2FakeTicker
    _b_f2 = fd.get_bundle("SHEL.L", force_refresh=True)

_f2_ratio = (_b_f2["info"]["currentPrice"] * _b_f2["info"]["sharesOutstanding"]) / _b_f2["info"]["marketCap"]
assert abs(_f2_ratio - 1.0) <= 0.05, _f2_ratio
assert _b_f2["meta"]["price_unit_suspect"] is False, _b_f2["meta"]["price_unit_suspect_reason"]
assert _b_f2["info"]["marketCap"] == 26.0 * _F2_SHARES, (
    "marketCap must be recomputed from the ALREADY-divided £26.00 fresh price, "
    f"not the raw 2600p one: {_b_f2['info']['marketCap']}")
_f2_log_text = _f2_log.getvalue()
assert "ratio=1.00 ok" in _f2_log_text, _f2_log_text
print(f"[F2_overlay_ordering_fix] get_bundle() with a WORKING fast_info quote: "
      f"ratio={_f2_ratio:.2f} (was 0.01 live, pre-fix - the exact SHEL.L/TSCO.L symptom), "
      f"price_unit_suspect=False, marketCap correctly recomputed from the divided price OK")


# ======================================================================
# F4: Bank of England 403 - requests.get() must now be called WITH a
# browser User-Agent/Accept header; a 403 still falls back to 0.045.
# ======================================================================
class _Resp403:
    status_code = 403
    text = ""


capm.get_uk_risk_free_rate_live.clear()
with mock.patch.object(capm.requests, "get", return_value=_Resp403()) as _mget:
    _uk_rate_403, _uk_src_403 = capm.get_uk_risk_free_rate_live()
assert (_uk_rate_403, _uk_src_403) == (0.045, "default"), (_uk_rate_403, _uk_src_403)
_call_kwargs = _mget.call_args.kwargs
assert "headers" in _call_kwargs and "User-Agent" in _call_kwargs["headers"], (
    "F4 fix: the BoE request must send a browser User-Agent/Accept header", _call_kwargs)
print("[F4_boe_403_headers] a 403 response still falls back to 0.045, and requests.get() now "
      "sends a browser User-Agent/Accept header (the F4 fix) OK")


# ======================================================================
# F5: Bank of Canada - the MOST RECENT observation must be used, not
# whichever one happens to be first/last in the array. Five
# observations, deliberately shuffled so the latest date is in the
# MIDDLE of the list - a position-based pick (either end) would fail
# this, only a date-based pick passes.
# ======================================================================
class _BocMixedOrderResp:
    status_code = 200

    def json(self):
        return {"observations": [
            {"d": "2026-09-20", "BD.CDN.10YR.DQ.YLD": {"v": "3.10"}},
            {"d": "2026-09-30", "BD.CDN.10YR.DQ.YLD": {"v": "3.25"}},  # latest - in the middle
            {"d": "2026-09-18", "BD.CDN.10YR.DQ.YLD": {"v": "3.05"}},
            {"d": "2026-09-25", "BD.CDN.10YR.DQ.YLD": {"v": "3.15"}},
            {"d": "2026-09-22", "BD.CDN.10YR.DQ.YLD": {"v": "3.12"}},
        ]}


capm.get_ca_risk_free_rate_live.clear()
with mock.patch.object(capm.requests, "get", return_value=_BocMixedOrderResp()):
    _ca_rate_mixed, _ca_src_mixed = capm.get_ca_risk_free_rate_live()
# 3.25% (2026-09-30) is the chronologically latest of the five - a
# position-based pick (first OR last element) would have landed on
# 3.10% (2026-09-20) or 3.12% (2026-09-22) instead; only a date-based
# pick lands on 3.25%, proving this is genuinely date-driven.
assert (_ca_rate_mixed, _ca_src_mixed) == (0.0325, "live"), (_ca_rate_mixed, _ca_src_mixed)
print("[F5_boc_most_recent_observation] five observations in shuffled order, latest (2026-09-30, "
      "3.25%) in the MIDDLE of the array -> correctly picked by date (0.0325, 'live'), not by "
      "array position, which would have returned 3.10% or 3.12% instead (was picking a 9-day-"
      "stale reading live, pre-fix) OK")


# ======================================================================
# F6: fx_rate() now logs once per pair per UTC day, on a LIVE rate too
# (not only a fallback - live evidence showed zero "[fx] ..." lines for
# NTR.TO's own USD->CAD conversion because that fetch succeeded live).
# This sandbox has no network, so "live" is simulated via a mocked
# yf.Ticker history; the fallback case is exercised the same way the
# original Stage 1a suite already did (no mocking needed - the sandbox
# itself has no network).
# ======================================================================
fcf._fx_cache.clear()
fcf._fx_log_date.clear()


_fake_fx_hist = pd.DataFrame({"Close": [1.38, 1.39]})


class _FakeFxTicker:
    def __init__(self, *_a, **_k):
        pass

    def history(self, *a, **k):
        return _fake_fx_hist


_f6_log_live = []
with mock.patch.object(fcf, "yf") as _myf:
    _myf.Ticker.side_effect = _FakeFxTicker
    _rate_live, _src_live = fcf.fx_rate("USD", "CAD", log=_f6_log_live.append)
assert _src_live == "live", _src_live
assert any(line.startswith("[fx] USD->CAD live") for line in _f6_log_live), _f6_log_live

# A second call for the SAME pair on the SAME (simulated) day must NOT
# log again - the once-per-pair-per-day throttle.
_f6_log_live_2 = []
with mock.patch.object(fcf, "yf") as _myf:
    _myf.Ticker.side_effect = _FakeFxTicker
    fcf.fx_rate("USD", "CAD", log=_f6_log_live_2.append)
assert _f6_log_live_2 == [], (
    "a second fx_rate() call for the same pair on the same day must not re-log", _f6_log_live_2)

# Fallback case (no network in this sandbox - the pre-existing path):
fcf._fx_cache.clear()
fcf._fx_log_date.clear()
_f6_log_fallback = []
_rate_fb, _src_fb = fcf.fx_rate("GBP", "CAD", log=_f6_log_fallback.append)
assert _src_fb == "fallback", _src_fb
assert any("[fx] GBP->CAD fallback" in line and "live fetch failed" in line
           for line in _f6_log_fallback), _f6_log_fallback
print("[F6_fx_rate_logging] a live USD->CAD rate now logs '[fx] USD->CAD live 1.3900' (was silent "
      "live, pre-fix), throttled to once per pair per day, and a fallback still logs the original "
      "'... fallback ... (live fetch failed)' line OK")

# F6 (second half): confirm NTR.TO-shaped data (CAD trading, USD
# statements) is converted before FCF per share - fcf_valuation_engine.
# dcf_intrinsic_value()'s own financialCurrency->listing-currency block,
# fcf_valuation_engine.py:2090-2098 (fin_ccy/listing_ccy resolved, fx_
# rate() called, fcf multiplied by the result) - exercised here via a
# cashflow fixture with financialCurrency "USD" and currency "CAD".
_ntr_cf = _mk_cashflow_df([500_000_000.0] * 5, [-50_000_000.0] * 5)
_ntr_info = {
    "currency": "CAD", "financialCurrency": "USD",
    "sharesOutstanding": 100_000_000, "marketCap": 100.0 * 100_000_000,
}
fcf._fx_cache.clear()
_iv_ntr, _growth_ntr, _meta_ntr = fcf.dcf_intrinsic_value(
    "NTR.TO", info=_ntr_info, cashflow_df=_ntr_cf, currency="CAD",
)
assert _meta_ntr.get("fx_converted") == "USD->CAD", (
    "NTR.TO-shaped fixture (CAD trading, USD statements) must convert FCF before per-share "
    f"- fcf_valuation_engine.py dcf_intrinsic_value()'s fin_ccy/listing_ccy block (line ~2090): "
    f"{_meta_ntr}")
print("[F6_ntr_to_fx_conversion_confirmed] a CAD-trading/USD-reporting (NTR.TO-shaped) fixture's "
      "FCF is converted USD->CAD before FCF-per-share - fcf_valuation_engine.py dcf_intrinsic_"
      "value(), the fin_ccy/listing_ccy block at line ~2090-2098 - confirmed by meta['fx_converted'] "
      "== 'USD->CAD' OK")


# ======================================================================
# T6 additions: BoE 403 -> fallback (above, F4) already covers this;
# BoC with five observations -> the latest is used (above, F5) already
# covers this with a deliberately shuffled, non-edge-position latest
# date. Re-stated here per the instruction's own numbering.
# ======================================================================
print("[T6_additions] BoE 403->fallback and BoC five-observation latest-by-date selection both "
      "verified above (F4/F5) OK")


# ======================================================================
# T7, PROPERLY: the five USD/AUD fixtures run against the REAL code at
# commit 1f768ba (immediately before Stage 1a) AND this commit's own
# current code - not a recomputed-from-constants argument. Loads the
# old capm_engine.py/fcf_valuation_engine.py via `git show` into an
# isolated sys.modules bubble so "import capm_engine" inside the OLD
# fcf_valuation_engine.py resolves to the OLD capm_engine, not the
# current one.
# ======================================================================
def _load_module_from_git(path, module_name, commit):
    src = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = f"<git {commit}:{path}>"
    sys.modules[module_name] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


_PRE_STAGE1A_COMMIT = "1f768ba"
_t7_usd_fixtures = [("ADP", 120_000_000_000), ("CPRT", 45_000_000_000), ("AOS", 9_000_000_000)]
_t7_aud_fixtures = [("CSL.AX", 130_000_000_000), ("OCL.AX", 900_000_000)]

_saved_capm = sys.modules.pop("capm_engine", None)
_saved_fcf = sys.modules.pop("fcf_valuation_engine", None)
try:
    old_capm = _load_module_from_git("capm_engine.py", "capm_engine", _PRE_STAGE1A_COMMIT)
    old_fcf = _load_module_from_git("fcf_valuation_engine.py", "fcf_valuation_engine", _PRE_STAGE1A_COMMIT)
    assert not hasattr(old_capm, "GBP_CAD_RISK_FREE_FALLBACK"), (
        "sanity check: the commit loaded must genuinely predate Stage 1a's GBP/CAD additions")

    _old_results = {}
    with mock.patch.object(old_capm, "get_risk_free_rate", return_value=(0.050, "default")):
        for ticker, mcap in _t7_usd_fixtures:
            info = {"currency": "USD", "marketCap": mcap}
            rate, _meta = old_capm.resolve_discount_rate_by_market_cap(info, "USD")
            _old_results[(ticker, "discount")] = rate
    with mock.patch.object(old_capm, "get_au_risk_free_rate_live", return_value=(0.053, "default")):
        for ticker, mcap in _t7_aud_fixtures:
            info = {"currency": "AUD", "marketCap": mcap}
            rate, _meta = old_capm.resolve_discount_rate_by_market_cap(info, "AUD")
            _old_results[(ticker, "discount")] = rate
    for ticker, mcap in _t7_usd_fixtures + _t7_aud_fixtures:
        ccy = "AUD" if ticker.endswith(".AX") else "USD"
        info = {"currency": ccy, "marketCap": mcap}
        _old_results[(ticker, "ceiling")] = old_fcf.growth_ceiling_for(info)
        _old_results[(ticker, "end_rate")] = old_fcf.growth_end_rate_for(info)
finally:
    if _saved_capm is not None:
        sys.modules["capm_engine"] = _saved_capm
    else:
        sys.modules.pop("capm_engine", None)
    if _saved_fcf is not None:
        sys.modules["fcf_valuation_engine"] = _saved_fcf
    else:
        sys.modules.pop("fcf_valuation_engine", None)

# Now the CURRENT code (this commit) - re-import cleanly to be sure
# we're not accidentally still holding an old reference.
import capm_engine as new_capm
import fcf_valuation_engine as new_fcf

_new_results = {}
with mock.patch.object(new_capm, "get_risk_free_rate", return_value=(0.050, "default")):
    for ticker, mcap in _t7_usd_fixtures:
        info = {"currency": "USD", "marketCap": mcap}
        rate, _meta = new_capm.resolve_discount_rate_by_market_cap(info, "USD")
        _new_results[(ticker, "discount")] = rate
with mock.patch.object(new_capm, "get_au_risk_free_rate_live", return_value=(0.053, "default")):
    for ticker, mcap in _t7_aud_fixtures:
        info = {"currency": "AUD", "marketCap": mcap}
        rate, _meta = new_capm.resolve_discount_rate_by_market_cap(info, "AUD")
        _new_results[(ticker, "discount")] = rate
for ticker, mcap in _t7_usd_fixtures + _t7_aud_fixtures:
    ccy = "AUD" if ticker.endswith(".AX") else "USD"
    info = {"currency": ccy, "marketCap": mcap}
    _new_results[(ticker, "ceiling")] = new_fcf.growth_ceiling_for(info)
    _new_results[(ticker, "end_rate")] = new_fcf.growth_end_rate_for(info)

print(f"[T7_real_before_after] every USD/AUD fixture run against the REAL code at commit "
      f"{_PRE_STAGE1A_COMMIT} (before Stage 1a) and against this commit's current code:")
_t7_all_match = True
for ticker, mcap in _t7_usd_fixtures + _t7_aud_fixtures:
    for metric in ("discount", "ceiling", "end_rate"):
        before = _old_results[(ticker, metric)]
        after = _new_results[(ticker, metric)]
        match = before == after
        _t7_all_match = _t7_all_match and match
        print(f"    {ticker:10s} {metric:9s} before={before:.4f}  after={after:.4f}  "
              f"{'MATCH' if match else 'MISMATCH'}")
        assert match, (ticker, metric, before, after)
assert _t7_all_match
print("[T7_real_before_after] every metric MATCH - no USD/AUD behaviour changed by either Stage "
      "1a or this fix OK")


print("\nALL STAGE 1a-FIX LONDON DEEP DIVE FIXTURES PASSED")
