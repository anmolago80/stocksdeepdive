"""
Director reply (3 Oct 2026) - item 1: intrinsic-value before/after from
the DEPLOYED commit 9f54066 to this commit's current HEAD (all 12
commits since, not just addendum 2's 5). Fixtures: the 7 from the
earlier Director Q&A answer (ADP, CPRT, AOS, CSL.AX, OCL.AX, US_BANK,
AX_BANK.AX) plus GBp/CAD/JPY. Fields: IV, MOS, discount rate, growth,
base, base_source - plus a dedicated forwardEps-resolution check for
the GBp fixture (Commit 4's own mechanism), which is the one EXPECTED
mismatch.

Same "_load_module_from_git" pattern as tests/test_stage1a_fix_london_
deep_dive.py's T7 and tests/test_stage1b_private_universes.py's T9.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_addendum2_full_iv_before_after.py
"""
import importlib.util
import os
import subprocess
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DEPLOYED_COMMIT = "9f54066"  # the commit actually live in production


def _mk_cashflow_df(ocf, capex, years=5, growth=1.0):
    import pandas as pd
    cols = {}
    for i in range(years):
        cols[f"202{6 - i}-06-30"] = [ocf * (growth ** -i), capex * (growth ** -i)]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


def _mk_income_df(net_income, years=5, growth=1.0):
    import pandas as pd
    cols = {}
    for i in range(years):
        cols[f"202{6 - i}-06-30"] = [net_income * (growth ** -i)]
    return pd.DataFrame(cols, index=["Net Income"])


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


# Non-bank fixtures: (ticker, info, cashflow_df, currency)
_FIXTURES = [
    ("ADP", {
        "currency": "USD", "financialCurrency": "USD", "currentPrice": 250.0,
        "marketCap": 120_000_000_000, "sharesOutstanding": 120_000_000_000 / 250.0,
        "longName": "ADP-shaped",
    }, _mk_cashflow_df(3_000_000_000.0, -300_000_000.0, growth=1.03), "USD"),
    ("CPRT", {
        "currency": "USD", "financialCurrency": "USD", "currentPrice": 55.0,
        "marketCap": 45_000_000_000, "sharesOutstanding": 45_000_000_000 / 55.0,
        "longName": "CPRT-shaped",
    }, _mk_cashflow_df(1_100_000_000.0, -250_000_000.0, growth=1.03), "USD"),
    ("AOS", {
        "currency": "USD", "financialCurrency": "USD", "currentPrice": 75.0,
        "marketCap": 9_000_000_000, "sharesOutstanding": 9_000_000_000 / 75.0,
        "longName": "AOS-shaped",
    }, _mk_cashflow_df(450_000_000.0, -70_000_000.0, growth=1.03), "USD"),
    ("CSL.AX", {
        "currency": "AUD", "financialCurrency": "AUD", "currentPrice": 280.0,
        "marketCap": 130_000_000_000, "sharesOutstanding": 130_000_000_000 / 280.0,
        "longName": "CSL-shaped",
    }, _mk_cashflow_df(2_500_000_000.0, -300_000_000.0, growth=1.03), "AUD"),
    ("OCL.AX", {
        "currency": "AUD", "financialCurrency": "AUD", "currentPrice": 22.0,
        "marketCap": 900_000_000, "sharesOutstanding": 900_000_000 / 22.0,
        "longName": "OCL-shaped",
    }, _mk_cashflow_df(45_000_000.0, -8_000_000.0, growth=1.03), "AUD"),
    ("RY_FIXTURE.TO", {
        "currency": "CAD", "financialCurrency": "CAD", "currentPrice": 135.0,
        "marketCap": 135.0 * 1_400_000_000, "sharesOutstanding": 1_400_000_000,
        "longName": "RY-shaped",
    }, _mk_cashflow_df(16_000_000_000.0, -600_000_000.0, growth=1.03), "CAD"),
    ("7203_FIXTURE.T", {
        "currency": "JPY", "financialCurrency": "JPY", "currentPrice": 2850.0,
        "marketCap": 2850.0 * 13_000_000_000, "sharesOutstanding": 13_000_000_000,
        "longName": "Toyota-shaped",
    }, _mk_cashflow_df(4_700_000_000_000.0, -1_200_000_000_000.0, growth=1.03), "JPY"),
]

# Bank fixtures (financials mode - net income substitutes for FCF; no
# cashflow_df needed, income_df drives the base/series instead):
# (ticker, info, income_df, currency)
_BANK_FIXTURES = [
    ("US_BANK", {
        "currency": "USD", "financialCurrency": "USD", "currentPrice": 150.0,
        "marketCap": 60_000_000_000, "sharesOutstanding": 60_000_000_000 / 150.0,
        "longName": "US_BANK-shaped", "sector": "Financial Services",
    }, _mk_income_df(8_000_000_000.0, growth=1.03), "USD"),
    ("AX_BANK.AX", {
        "currency": "AUD", "financialCurrency": "AUD", "currentPrice": 100.0,
        "marketCap": 20_000_000_000, "sharesOutstanding": 20_000_000_000 / 100.0,
        "longName": "AX_BANK-shaped", "sector": "Financial Services",
    }, _mk_income_df(2_500_000_000.0, growth=1.03), "AUD"),
]

_RF_MOCKS = {
    "USD": "get_risk_free_rate", "AUD": "get_au_risk_free_rate_live",
    "GBP": "get_uk_risk_free_rate_live", "CAD": "get_ca_risk_free_rate_live",
    "JPY": "get_jp_risk_free_rate_live",
}
_RF_RETVALS = {
    "USD": (0.050, "default"), "AUD": (0.053, "default"), "GBP": (0.045, "default"),
    "CAD": (0.038, "default"), "JPY": (0.012, "default"),
}


def _run_all(fcf_mod, capm_mod):
    results = {}
    for ticker, info, cf, ccy in _FIXTURES:
        fn_name = _RF_MOCKS[ccy]
        with mock.patch.object(capm_mod, fn_name, return_value=_RF_RETVALS[ccy]), \
             mock.patch.object(capm_mod, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
            iv, growth_used, meta = fcf_mod.dcf_intrinsic_value(
                ticker, info=info, cashflow_df=cf, currency=ccy,
            )
        price = info["currentPrice"]
        mos = ((iv - price) / iv) * 100 if iv and iv > 0 else None
        results[ticker] = {
            "iv": round(iv, 4) if iv else iv,
            "mos": round(mos, 4) if mos is not None else None,
            "discount": meta.get("discount_rate_used"),
            "growth": round(growth_used, 6) if growth_used else growth_used,
            "base": round(meta.get("fcf_used"), 2) if meta.get("fcf_used") else meta.get("fcf_used"),
            "base_source": meta.get("fcf_source"),
        }
    for ticker, info, inc, ccy in _BANK_FIXTURES:
        fn_name = _RF_MOCKS[ccy]
        with mock.patch.object(capm_mod, fn_name, return_value=_RF_RETVALS[ccy]), \
             mock.patch.object(capm_mod, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
            iv, growth_used, meta = fcf_mod.dcf_intrinsic_value(
                ticker, info=info, cashflow_df=None, currency=ccy, income_df=inc,
            )
        price = info["currentPrice"]
        mos = ((iv - price) / iv) * 100 if iv and iv > 0 else None
        results[ticker] = {
            "iv": round(iv, 4) if iv else iv,
            "mos": round(mos, 4) if mos is not None else None,
            "discount": meta.get("discount_rate_used"),
            "growth": round(growth_used, 6) if growth_used else growth_used,
            "base": round(meta.get("fcf_used"), 2) if meta.get("fcf_used") else meta.get("fcf_used"),
            "base_source": meta.get("fcf_source"),
        }
    return results


_ALL_TICKERS = [t for t, *_ in _FIXTURES] + [t for t, *_ in _BANK_FIXTURES]

# --- BEFORE: the DEPLOYED commit 9f54066 ---
_saved_capm = sys.modules.pop("capm_engine", None)
_saved_fcf = sys.modules.pop("fcf_valuation_engine", None)
try:
    old_capm = _load_module_from_git("capm_engine.py", "capm_engine", _DEPLOYED_COMMIT)
    old_fcf = _load_module_from_git("fcf_valuation_engine.py", "fcf_valuation_engine", _DEPLOYED_COMMIT)
    _before = _run_all(old_fcf, old_capm)
finally:
    if _saved_capm is not None:
        sys.modules["capm_engine"] = _saved_capm
    else:
        sys.modules.pop("capm_engine", None)
    if _saved_fcf is not None:
        sys.modules["fcf_valuation_engine"] = _saved_fcf
    else:
        sys.modules.pop("fcf_valuation_engine", None)

# --- AFTER: this commit's current code ---
import capm_engine as new_capm
import fcf_valuation_engine as new_fcf
_after = _run_all(new_fcf, new_capm)


print("\n[full_iv_before_after] intrinsic-value-level before/after, 9f54066 -> HEAD, "
      "every fixture:")
_fields = ("iv", "mos", "discount", "growth", "base", "base_source")
_mismatches = []
for ticker in _ALL_TICKERS:
    for field in _fields:
        b = _before[ticker][field]
        a = _after[ticker][field]
        match = b == a
        if not match:
            _mismatches.append((ticker, field, b, a))
        print(f"    {ticker:14s} {field:11s} before={str(b):>20s}  after={str(a):>20s}  "
              f"{'MATCH' if match else 'MISMATCH'}")

print(f"\n[full_iv_before_after] {len(_mismatches)} mismatch(es) across "
      f"{len(_ALL_TICKERS)} fixtures x {len(_fields)} fields:")
for ticker, field, b, a in _mismatches:
    print(f"    MISMATCH: {ticker} {field}: before={b!r} after={a!r}")
if not _mismatches:
    print("    (none)")


# ======================================================================
# Dedicated forwardEps-resolution check for the GBp fixture - Commit
# 4's own mechanism (fundamentals_data._resolve_forward_eps_unit()),
# which does NOT exist at 9f54066 at all (confirmed below) - this is
# the ONE EXPECTED difference.
# ======================================================================
print("\n[forward_eps_resolution] GBp fixture (AZN_FIXTURE.L) - Commit 4's own mechanism:")

old_fd = _load_module_from_git("fundamentals_data.py", "fundamentals_data_old", _DEPLOYED_COMMIT)
assert not hasattr(old_fd, "_resolve_forward_eps_unit"), (
    "sanity check: _resolve_forward_eps_unit must NOT exist at the deployed commit")

_gbp_info_before = {"currency": "GBp", "forwardEps": 31.2}
_gbp_info_before, _ = old_fd._normalize_pence_price_fields(_gbp_info_before)
_before_forward_eps = _gbp_info_before.get("forwardEps")  # popped -> None

import fundamentals_data as new_fd
_gbp_info_after = {"currency": "GBp", "forwardEps": 31.2}
_gbp_info_after, _ = new_fd._normalize_pence_price_fields(_gbp_info_after)
_gbp_info_after["trailingEps"] = 0.27  # statement-derived, pounds (simulated)
_gbp_info_after, _unit = new_fd._resolve_forward_eps_unit("AZN_FIXTURE.L", _gbp_info_after)
_after_forward_eps = _gbp_info_after.get("forwardEps")

print(f"    forwardEps (raw pence input 31.2): before={_before_forward_eps!r} "
      f"(function doesn't exist at {_DEPLOYED_COMMIT} - dropped, unconfirmed unit) "
      f"after={_after_forward_eps!r} (resolved to pounds via Commit 4) "
      f"{'MISMATCH (expected)' if _before_forward_eps != _after_forward_eps else 'MATCH'}")
assert _before_forward_eps is None
assert _after_forward_eps == 0.312

print("\n[director_check] Expected: everything MATCH except PE Forward (GBp fixture, "
      "Commit 4) - " + ("CONFIRMED: only the forwardEps resolution differs, every DCF-"
      "level field (IV/MOS/discount/growth/base/base_source) matches across all "
      f"{len(_ALL_TICKERS)} fixtures." if not _mismatches else
      f"UNEXPECTED: {len(_mismatches)} additional mismatch(es) at the DCF level - see above."))
