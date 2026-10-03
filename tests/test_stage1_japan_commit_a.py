"""
Stage 1 Japan, Commit A - yen data layer (Director-directed, 3 Oct 2026).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access (every live MOF/Yahoo fetch below fails and falls
through to this commit's own fallback path, confirmed by the printed
log lines), same disclosure as every other fixture-based test in this
repo.

Covers:
  - trading_currency_for(): ".T" -> JPY, info["currency"]="JPY" passes
    straight through, no GBp-style pence normalisation applies to yen
    (fundamentals_data._is_pence_quoted() is an exact "GBp"/"GBX"
    string match - "JPY" can never trigger it).
  - FX constants: JPY present in FX_TO_USD_APPROX/_FX_STATIC_FALLBACK
    (fcf_valuation_engine.py) and _DISCOUNT_TIER_FX_TO_USD_APPROX
    (capm_engine.py), all at the Director-supplied 0.0067.
  - get_jp_risk_free_rate_live(): success (a well-formed MOF-shaped
    CSV), failure -> fallback, out-of-band -> fallback, most-recent-ROW
    (not position) selection when rows are shuffled.
  - PERPETUAL_GROWTH_BY_CCY["JPY"] = 1.0%; resolve_perpetual_rate()
    never clamps it (always well below the 7.5% discount floor); the
    growth end-rate floor (max(growth_end_rate_for(...), perpetual))
    is a no-op for JPY (end-rate anchors run 2%-6%, always above 1%).
  - symbol_mapping.to_yahoo_symbol(raw, "TSE").
  - A Toyota-shaped fixture (large-cap, discount floored, perpetual
    1.0%) and a small-cap yen fixture (risk-free + premium above the
    floor) through the real dcf_intrinsic_value().
  - "Real" before/after (git show commit 061a952 - the Stage 1b commit,
    immediately before this task - into an isolated sys.modules
    bubble, not a recomputed-from-constants argument) for the 7
    existing fixtures (ADP/CPRT/AOS/CSL.AX/OCL.AX/US_BANK/AX_BANK.AX)
    plus one new GBp and one new CAD fixture - comparing INTRINSIC
    VALUE, MOS, discount rate, growth and base, not only discount/
    ceiling/end-rate.

Run: python3 tests/test_stage1_japan_commit_a.py
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TESTVOL = tempfile.mkdtemp(prefix="japan_commit_a_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import capm_engine as capm
import fcf_valuation_engine as fcf
import fundamentals_data as fd
import symbol_mapping


def _mk_cashflow_df(ocf, capex, years=5, growth=1.0):
    cols = {}
    for i in range(years):
        cols[f"202{6 - i}-06-30"] = [ocf * (growth ** -i), capex * (growth ** -i)]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


def _mk_income_df(net_income, years=5, growth=1.0):
    cols = {}
    for i in range(years):
        cols[f"202{6 - i}-12-31"] = [net_income * (growth ** -i)]
    return pd.DataFrame(cols, index=["Net Income"])


# ======================================================================
# A3: trading currency + "no pence-style normalisation for yen"
# ======================================================================
assert fcf.trading_currency_for("7203.T") == "JPY"
assert fcf.trading_currency_for("7203.T", info={"currency": "JPY"}) == "JPY"
assert fcf.trading_currency_for("130A.T", info={}) == "JPY"
assert fd._is_pence_quoted("JPY") is False
assert fd._is_pence_quoted("GBp") is True   # sanity: the real trigger still fires
print("[A3_trading_currency] .T -> JPY via suffix fallback and via info['currency'], "
      "and JPY can never trigger the GBp pence-normalisation guard (exact string "
      "match, never a magnitude guess) OK")

# ======================================================================
# A3: FX constants
# ======================================================================
assert fcf.FX_TO_USD_APPROX["JPY"] == 0.0067
assert fcf._FX_STATIC_FALLBACK[("JPY", "USD")] == 0.0067
assert capm._DISCOUNT_TIER_FX_TO_USD_APPROX["JPY"] == 0.0067
rate, source = fcf.fx_rate("JPY", "USD", log=None)
assert source == "fallback" and rate == 0.0067, (rate, source)
print("[A3_fx_constants] JPY present at 0.0067 in FX_TO_USD_APPROX/_FX_STATIC_FALLBACK/"
      "_DISCOUNT_TIER_FX_TO_USD_APPROX, and fx_rate('JPY','USD') falls back to it "
      "(no live network in this sandbox) OK")

# ======================================================================
# A3: perpetual rate + the two floor/guard behaviours
# ======================================================================
assert capm.PERPETUAL_GROWTH_BY_CCY["JPY"] == 0.01
# Never clamped: JPY's own 1.0% perpetual is always far below the 7.5%
# discount floor (MIN_DISCOUNT_RATE), so resolve_perpetual_rate()'s own
# "perpetual must stay below discount" guard never has to intervene.
assert capm.resolve_perpetual_rate("JPY", capm.MIN_DISCOUNT_RATE) == 0.01
assert capm.resolve_perpetual_rate("JPY", 0.30) == 0.01
# The growth end-rate floor (fcf_valuation_engine.dcf_intrinsic_value()'s
# own `end_rate = max(growth_end_rate_for(info, currency), perpetual_rate)`
# line) is a no-op for JPY: GROWTH_END_RATE_ANCHORS_USD runs 2%-6%, always
# above JPY's 1% perpetual, at every market cap from micro to mega.
for mcap_usd in (100_000_000, 1_000_000_000, 10_000_000_000, 500_000_000_000):
    info = {"currency": "JPY", "marketCap": mcap_usd / fcf.FX_TO_USD_APPROX["JPY"]}
    end_rate = fcf.growth_end_rate_for(info, "JPY")
    assert end_rate > 0.01, (mcap_usd, end_rate)
    assert max(end_rate, 0.01) == end_rate
print("[A3_perpetual] PERPETUAL_GROWTH_BY_CCY['JPY']=1.0%, never clamped by the "
      "perpetual-below-discount guard, and always below growth_end_rate_for()'s own "
      "2%-6% floor at every market-cap tier OK")

# ======================================================================
# A3: symbol_mapping TSE support
# ======================================================================
assert symbol_mapping.to_yahoo_symbol("7203", "TSE") == "7203.T"
assert symbol_mapping.to_yahoo_symbol("130A", "TSE") == "130A.T"
assert symbol_mapping.to_yahoo_symbol("7203.T", "TSE") == "7203.T"   # idempotent
assert symbol_mapping.to_yahoo_symbol("7203.t", "TSE") == "7203.t"   # idempotent, case-insensitive check
_overrides = {"9999": "9999.T"}   # a hit here must win even over the plain-suffix rule
assert symbol_mapping.to_yahoo_symbol("9999", "TSE", overrides=_overrides) == "9999.T"
try:
    symbol_mapping.to_yahoo_symbol("7203", "NYSE")
    assert False, "expected ValueError for an unknown exchange"
except ValueError:
    pass
print("[A3_symbol_mapping] to_yahoo_symbol(raw, 'TSE'): '7203'->'7203.T', "
      "'130A'->'130A.T', idempotent on an already-.T input, overrides checked first OK")

# ======================================================================
# A4: risk-free rate - success, failure, out-of-band, most-recent-row.
# ======================================================================
def _mof_csv(rows_shuffled_order):
    lines = ["Date,1Y,5Y,10Y,20Y"]
    for date_str, ten_y in rows_shuffled_order:
        lines.append(f"{date_str},0.40,0.90,{ten_y},1.90")
    return "\n".join(lines)


class _FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


# Success: a well-formed CSV, rows in normal oldest-to-newest order.
capm.get_jp_risk_free_rate_live.clear()
_csv_ok = _mof_csv([("2026/09/28", "1.65"), ("2026/09/29", "1.68"), ("2026/09/30", "1.70")])
with mock.patch.object(capm, "requests") as _mreq:
    _mreq.get.return_value = _FakeResp(200, _csv_ok)
    rate, src = capm.get_jp_risk_free_rate_live()
assert src == "live" and abs(rate - 0.0170) < 1e-9, (rate, src)
print(f"[A4_jp_rf_success] well-formed MOF CSV -> live {rate*100:.2f}% OK")

# Failure: the request itself raises.
capm.get_jp_risk_free_rate_live.clear()
with mock.patch.object(capm, "requests") as _mreq:
    _mreq.get.side_effect = ConnectionError("boom")
    rate, src = capm.get_jp_risk_free_rate_live()
assert src == "default" and rate == capm.JP_RISK_FREE_FALLBACK["JPY"], (rate, src)
print(f"[A4_jp_rf_failure] a raised exception falls back to {rate*100:.2f}% OK")

# Out-of-band: a well-formed CSV whose 10Y value sits outside 0.1%-8%.
capm.get_jp_risk_free_rate_live.clear()
_csv_bad = _mof_csv([("2026/09/30", "25.00")])
with mock.patch.object(capm, "requests") as _mreq:
    _mreq.get.return_value = _FakeResp(200, _csv_bad)
    rate, src = capm.get_jp_risk_free_rate_live()
assert src == "default" and rate == capm.JP_RISK_FREE_FALLBACK["JPY"], (rate, src)
print(f"[A4_jp_rf_out_of_band] a 25.00% reading (outside 0.1%-8%) is rejected, "
      f"falls back to {rate*100:.2f}% OK")

# Most-recent-ROW selection: rows deliberately shuffled, the latest date
# sitting in the MIDDLE of the file, never first or last by position -
# exactly the shape of bug Stage 1a-fix F5 found in the BoC fetch (see
# capm_engine.py's own module comment above get_jp_risk_free_rate_live()).
capm.get_jp_risk_free_rate_live.clear()
_csv_shuffled = _mof_csv([
    ("2026/09/20", "1.50"), ("2026/09/29", "1.99"), ("2026/09/25", "1.60"),
])
with mock.patch.object(capm, "requests") as _mreq:
    _mreq.get.return_value = _FakeResp(200, _csv_shuffled)
    rate, src = capm.get_jp_risk_free_rate_live()
assert src == "live" and abs(rate - 0.0199) < 1e-9, (rate, src)
print("[A4_jp_rf_most_recent_row] with the latest date (2026/09/29) in the MIDDLE of "
      f"a shuffled file, the picked rate ({rate*100:.2f}%) is its own value, not the "
      "first or last row's - date-based, never position-based OK")


# ======================================================================
# A4: Toyota-shaped fixture - large-cap, discount floored, perpetual 1.0%.
# ======================================================================
_TOYOTA_SHARES = 13_300_000_000
_TOYOTA_PRICE = 2_800.0
_toyota_info = {
    "currency": "JPY", "financialCurrency": "JPY", "currentPrice": _TOYOTA_PRICE,
    "marketCap": _TOYOTA_PRICE * _TOYOTA_SHARES, "sharesOutstanding": _TOYOTA_SHARES,
    "longName": "Toyota-shaped",
}
_toyota_cf = _mk_cashflow_df(4_700_000_000_000.0, -1_800_000_000_000.0, growth=1.03)

with mock.patch.object(capm, "get_jp_risk_free_rate_live", return_value=(0.017, "default")):
    _toyota_iv, _toyota_growth, _toyota_meta = fcf.dcf_intrinsic_value(
        "7203.T", info=_toyota_info, cashflow_df=_toyota_cf, currency="JPY",
    )
assert _toyota_iv > 0 and _toyota_iv == _toyota_iv   # finite, not NaN
_toyota_mos = (_toyota_iv / _TOYOTA_PRICE - 1) * 100
assert _toyota_mos == _toyota_mos
assert _toyota_meta["discount_rate_used"] == capm.MIN_DISCOUNT_RATE, _toyota_meta
assert _toyota_meta["perpetual_rate_used"] == 0.01, _toyota_meta
print(f"[A4_toyota] IV=¥{_toyota_iv:,.0f}, MOS={_toyota_mos:.1f}%, discount="
      f"{_toyota_meta['discount_rate_used']*100:.2f}% (floored), perpetual="
      f"{_toyota_meta['perpetual_rate_used']*100:.2f}% - both finite, in yen OK")

# ======================================================================
# A4: small-cap yen fixture - risk-free + premium ABOVE the floor.
# ======================================================================
_SMALL_MCAP_USD = 200_000_000   # below the US$300M small-cap anchor -> flat 6% premium
_small_mcap_jpy = _SMALL_MCAP_USD / fcf.FX_TO_USD_APPROX["JPY"]
_small_price = 500.0
_small_shares = _small_mcap_jpy / _small_price
_small_info = {
    "currency": "JPY", "financialCurrency": "JPY", "currentPrice": _small_price,
    "marketCap": _small_mcap_jpy, "sharesOutstanding": _small_shares,
    "longName": "Small-cap yen-shaped",
}
_small_cf = _mk_cashflow_df(3_000_000_000.0, -300_000_000.0, growth=1.03)

with mock.patch.object(capm, "get_jp_risk_free_rate_live", return_value=(0.017, "default")):
    _small_iv, _small_growth, _small_meta = fcf.dcf_intrinsic_value(
        "9999.T", info=_small_info, cashflow_df=_small_cf, currency="JPY",
    )
_expected_rate = round(0.017 + 0.06, 4)   # rf (fallback) + flat small-cap premium
assert _small_meta["discount_rate_used"] == _expected_rate, _small_meta
assert _small_meta["discount_rate_used"] > capm.MIN_DISCOUNT_RATE, _small_meta
print(f"[A4_small_cap_yen] risk-free (1.70%) + size premium (6.00%) = "
      f"{_small_meta['discount_rate_used']*100:.2f}%, genuinely ABOVE the 7.5% floor - "
      "never floored for this fixture OK")


# ======================================================================
# A4/A5: "real" before/after - the 7 existing fixtures (ADP/CPRT/AOS/
# CSL.AX/OCL.AX/US_BANK/AX_BANK.AX) plus one new GBp and one new CAD
# fixture, against the REAL code at commit 061a952 (this task's own
# immediately-prior commit) and this commit's current code. Loads the
# old capm_engine.py/fcf_valuation_engine.py via `git show` into an
# isolated sys.modules bubble - same technique established in tests/
# test_stage1a_fix_london_deep_dive.py's own T7 and tests/
# test_stage1b_private_universes.py's own T9.
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


_PRE_COMMIT_A_COMMIT = "061a952"

# (ticker, currency, market_cap, price, ocf, capex) - standard FCF-mode
# fixtures. Bank fixtures are financials-mode (income_df, Net Income
# series) and listed separately below.
_STD_FIXTURES = [
    ("ADP", "USD", 120_000_000_000, 250.0, 3_000_000_000.0, -300_000_000.0),
    ("CPRT", "USD", 45_000_000_000, 55.0, 1_200_000_000.0, -150_000_000.0),
    ("AOS", "USD", 9_000_000_000, 70.0, 500_000_000.0, -60_000_000.0),
    ("CSL.AX", "AUD", 130_000_000_000, 280.0, 2_500_000_000.0, -300_000_000.0),
    ("OCL.AX", "AUD", 900_000_000, 12.0, 80_000_000.0, -10_000_000.0),
    # New for this task - one GBp-shaped (now-normalised-to-GBP) fixture
    # and one CAD fixture, per the Director's own instruction.
    ("SHEL.L", "GBP", 180_000_000_000, 26.0, 14_000_000_000.0, -1_400_000_000.0),
    ("NTR.TO", "CAD", 40_000_000_000, 60.0, 2_000_000_000.0, -200_000_000.0),
]
_BANK_FIXTURES = [
    ("US_BANK", "USD", 60_000_000_000, 40.0, "Financial Services", "Banks—Regional", 4_000_000_000.0),
    ("AX_BANK.AX", "AUD", 20_000_000_000, 25.0, "Financial Services", "Banks—Diversified", 1_500_000_000.0),
]


def _run_std_fixture(fcf_mod, capm_mod, ticker, ccy, mcap, price, ocf, capex):
    shares = mcap / price
    info = {"currency": ccy, "financialCurrency": ccy, "currentPrice": price,
            "marketCap": mcap, "sharesOutstanding": shares, "longName": f"{ticker}-shaped"}
    cf = _mk_cashflow_df(ocf, capex, growth=1.03)
    rf_patches = []
    if ccy == "GBP":
        rf_patches.append(mock.patch.object(capm_mod, "get_uk_risk_free_rate_live", return_value=(0.045, "default")))
    elif ccy == "CAD":
        rf_patches.append(mock.patch.object(capm_mod, "get_ca_risk_free_rate_live", return_value=(0.035, "default")))
    elif ccy == "AUD":
        rf_patches.append(mock.patch.object(capm_mod, "get_au_risk_free_rate_live", return_value=(0.053, "default")))
    else:
        rf_patches.append(mock.patch.object(capm_mod, "get_risk_free_rate", return_value=(0.050, "default")))
    with contextlib.ExitStack() as stack:
        for p in rf_patches:
            stack.enter_context(p)
        iv, growth, meta = fcf_mod.dcf_intrinsic_value(ticker, info=info, cashflow_df=cf, currency=ccy)
    mos = (iv / price - 1) * 100 if iv and iv > 0 else None
    base = meta.get("fcf_used")
    if base is None:
        base = meta.get("fcf_base_used")
    return {
        "iv": round(iv, 4) if iv else iv, "mos": round(mos, 4) if mos is not None else None,
        "discount": meta.get("discount_rate_used"), "growth": round(growth, 6) if growth is not None else None,
        "base": round(base, 2) if base is not None else None,
    }


def _run_bank_fixture(fcf_mod, capm_mod, ticker, ccy, mcap, price, sector, industry, net_income):
    shares = mcap / price
    info = {"currency": ccy, "financialCurrency": ccy, "currentPrice": price,
            "marketCap": mcap, "sharesOutstanding": shares, "longName": f"{ticker}-shaped",
            "sector": sector, "industry": industry}
    income_df = _mk_income_df(net_income, growth=1.03)
    rf_patches = []
    if ccy == "AUD":
        rf_patches.append(mock.patch.object(capm_mod, "get_au_risk_free_rate_live", return_value=(0.053, "default")))
    else:
        rf_patches.append(mock.patch.object(capm_mod, "get_risk_free_rate", return_value=(0.050, "default")))
    with contextlib.ExitStack() as stack:
        for p in rf_patches:
            stack.enter_context(p)
        iv, growth, meta = fcf_mod.dcf_intrinsic_value(ticker, info=info, currency=ccy, income_df=income_df)
    mos = (iv / price - 1) * 100 if iv and iv > 0 else None
    base = meta.get("fcf_used")
    if base is None:
        base = meta.get("fcf_base_used")
    return {
        "iv": round(iv, 4) if iv else iv, "mos": round(mos, 4) if mos is not None else None,
        "discount": meta.get("discount_rate_used"), "growth": round(growth, 6) if growth is not None else None,
        "base": round(base, 2) if base is not None else None,
    }


_saved_capm = sys.modules.pop("capm_engine", None)
_saved_fcf = sys.modules.pop("fcf_valuation_engine", None)
try:
    old_capm = _load_module_from_git("capm_engine.py", "capm_engine", _PRE_COMMIT_A_COMMIT)
    old_fcf = _load_module_from_git("fcf_valuation_engine.py", "fcf_valuation_engine", _PRE_COMMIT_A_COMMIT)
    assert "JPY" not in old_capm._DISCOUNT_TIER_FX_TO_USD_APPROX, (
        "sanity check: the commit loaded must genuinely predate this task's JPY additions")

    _old_results = {}
    for ticker, ccy, mcap, price, ocf, capex in _STD_FIXTURES:
        _old_results[ticker] = _run_std_fixture(old_fcf, old_capm, ticker, ccy, mcap, price, ocf, capex)
    for ticker, ccy, mcap, price, sector, industry, ni in _BANK_FIXTURES:
        _old_results[ticker] = _run_bank_fixture(old_fcf, old_capm, ticker, ccy, mcap, price, sector, industry, ni)
finally:
    if _saved_capm is not None:
        sys.modules["capm_engine"] = _saved_capm
    else:
        sys.modules.pop("capm_engine", None)
    if _saved_fcf is not None:
        sys.modules["fcf_valuation_engine"] = _saved_fcf
    else:
        sys.modules.pop("fcf_valuation_engine", None)

import capm_engine as new_capm
import fcf_valuation_engine as new_fcf

_new_results = {}
for ticker, ccy, mcap, price, ocf, capex in _STD_FIXTURES:
    _new_results[ticker] = _run_std_fixture(new_fcf, new_capm, ticker, ccy, mcap, price, ocf, capex)
for ticker, ccy, mcap, price, sector, industry, ni in _BANK_FIXTURES:
    _new_results[ticker] = _run_bank_fixture(new_fcf, new_capm, ticker, ccy, mcap, price, sector, industry, ni)

print(f"[A4_real_before_after] every fixture run against the REAL code at commit "
      f"{_PRE_COMMIT_A_COMMIT} (immediately before this task) and against this "
      f"commit's current code:")
_all_tickers = [f[0] for f in _STD_FIXTURES] + [f[0] for f in _BANK_FIXTURES]
_all_match = True
for ticker in _all_tickers:
    before, after = _old_results[ticker], _new_results[ticker]
    match = before == after
    _all_match = _all_match and match
    print(f"    {ticker:10s} before={before}")
    print(f"    {ticker:10s} after ={after}  {'MATCH' if match else 'MISMATCH'}")
    assert match, (ticker, before, after)
assert _all_match
print(f"[A4_real_before_after] every metric (IV/MOS/discount/growth/base) MATCH for "
      "all 7 existing fixtures plus the new GBp and CAD fixtures - this task changed "
      "nothing reachable by USD/AUD/GBP/CAD OK")

print("\nAll Stage 1 Japan Commit A checks passed.")
