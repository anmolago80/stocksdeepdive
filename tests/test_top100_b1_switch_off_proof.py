"""
Top 200 Commit B1 - "Tests common to all four commits" (instruction_
top200_blank_replies_and_financials_gap.md, 5 Oct 2026): the switch-
OFF proof after B1, against commit 4f794c6 (Addendum 2 item 7) - same
nine fixtures, same seven columns as test_financials_dry_run_followups.
py's own proof against 50f60b7. Prints the mismatch count.

B1 never touches fcf_valuation_engine.py/capm_engine.py/financials_
classifier.py at all - its pool-eligibility rule lives in top100_
engine.py (_financials_pool_ineligible()), nightly_scan.py (the new
"Is Financials Mode" passthrough field), and financials_dry_run.py
(status/pool_ineligible_if_switch_on). dcf_intrinsic_value() itself is
byte-identical before and after B1 regardless of the switch, so this
proof is expected to show 0 mismatches - run anyway, per the
instruction's own standing test requirement for every commit in this
set.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. No test here makes a live network call.

Run: python3 tests/test_top100_b1_switch_off_proof.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="top100_b1_switch_off_proof_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("FINANCIALS_STORE_LIVE", None)

_BASE_COMMIT = "4f794c6"


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


def _mk_cf(ocf, capex, years=5, growth=1.03):
    cols = {}
    for i in range(years):
        cols[f"202{6 - i}-06-30"] = [ocf * (growth ** -i), capex * (growth ** -i)]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


def _mk_ni(net_income, years=5, growth=1.03):
    cols = {}
    for i in range(years):
        cols[f"202{6 - i}-06-30"] = [net_income * (growth ** -i)]
    return pd.DataFrame(cols, index=["Net Income"])


_STD_FIXTURES = [
    ("USD_FIXTURE", {"currency": "USD", "financialCurrency": "USD", "currentPrice": 180.0,
                      "marketCap": 80_000_000_000, "sharesOutstanding": 80_000_000_000 / 180.0},
     _mk_cf(2_000_000_000.0, -250_000_000.0), "USD"),
    ("AUD_FIXTURE.AX", {"currency": "AUD", "financialCurrency": "AUD", "currentPrice": 45.0,
                         "marketCap": 15_000_000_000, "sharesOutstanding": 15_000_000_000 / 45.0},
     _mk_cf(700_000_000.0, -90_000_000.0), "AUD"),
    ("GBP_FIXTURE.L", {"currency": "GBp", "financialCurrency": "GBP", "currentPrice": 4500.0,
                        "marketCap": 4500.0 / 100 * 2_000_000_000, "sharesOutstanding": 2_000_000_000},
     _mk_cf(900_000_000.0, -120_000_000.0), "GBp"),
    ("CAD_FIXTURE.TO", {"currency": "CAD", "financialCurrency": "CAD", "currentPrice": 95.0,
                         "marketCap": 95.0 * 900_000_000, "sharesOutstanding": 900_000_000},
     _mk_cf(3_200_000_000.0, -450_000_000.0), "CAD"),
    ("JPY_FIXTURE.T", {"currency": "JPY", "financialCurrency": "JPY", "currentPrice": 1850.0,
                        "marketCap": 1850.0 * 9_000_000_000, "sharesOutstanding": 9_000_000_000},
     _mk_cf(2_100_000_000_000.0, -500_000_000_000.0), "JPY"),
]
_FIN_FIXTURES = [
    ("USD_BANK_FIXTURE", {"currency": "USD", "financialCurrency": "USD", "currentPrice": 150.0,
                           "marketCap": 60_000_000_000, "sharesOutstanding": 60_000_000_000 / 150.0,
                           "sector": "Financial Services"}, _mk_ni(8_000_000_000.0), "USD"),
    ("AUD_BANK_FIXTURE.AX", {"currency": "AUD", "financialCurrency": "AUD", "currentPrice": 100.0,
                              "marketCap": 20_000_000_000, "sharesOutstanding": 20_000_000_000 / 100.0,
                              "sector": "Financial Services"}, _mk_ni(2_500_000_000.0), "AUD"),
    ("GBP_BANK_FIXTURE.L", {"currency": "GBp", "financialCurrency": "GBP", "currentPrice": 550.0,
                             "marketCap": 550.0 / 100 * 3_000_000_000, "sharesOutstanding": 3_000_000_000,
                             "sector": "Financial Services"}, _mk_ni(1_800_000_000.0), "GBp"),
    ("CAD_BANK_FIXTURE.TO", {"currency": "CAD", "financialCurrency": "CAD", "currentPrice": 135.0,
                              "marketCap": 135.0 * 1_400_000_000, "sharesOutstanding": 1_400_000_000,
                              "sector": "Financial Services"}, _mk_ni(5_600_000_000.0), "CAD"),
]
_RF_MOCKS = {"USD": "get_risk_free_rate", "AUD": "get_au_risk_free_rate_live",
             "GBp": "get_uk_risk_free_rate_live", "CAD": "get_ca_risk_free_rate_live",
             "JPY": "get_jp_risk_free_rate_live"}
_RF_RETVALS = {"USD": (0.050, "default"), "AUD": (0.053, "default"), "GBp": (0.045, "default"),
               "CAD": (0.038, "default"), "JPY": (0.012, "default")}


def _run_all(fcf_mod, capm_mod):
    results = {}
    for ticker, info, cf, ccy in _STD_FIXTURES:
        fn_name = _RF_MOCKS[ccy]
        with mock.patch.object(capm_mod, fn_name, return_value=_RF_RETVALS[ccy]), \
             mock.patch.object(capm_mod, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
            iv, growth_used, meta = fcf_mod.dcf_intrinsic_value(ticker, info=info, cashflow_df=cf, currency=ccy)
        price = info["currentPrice"]
        mos = ((iv - price) / iv) * 100 if iv and iv > 0 else None
        results[ticker] = {
            "iv": round(iv, 4) if iv else iv, "mos": round(mos, 4) if mos is not None else None,
            "discount": meta.get("discount_rate_used"),
            "growth": round(growth_used, 6) if growth_used else growth_used,
            "base": round(meta.get("fcf_used"), 2) if meta.get("fcf_used") else meta.get("fcf_used"),
            "fcf_source": meta.get("fcf_source"),
            "moat_mode": "financials" if meta.get("is_financials_mode") else "standard",
        }
    for ticker, info, inc, ccy in _FIN_FIXTURES:
        fn_name = _RF_MOCKS[ccy]
        with mock.patch.object(capm_mod, fn_name, return_value=_RF_RETVALS[ccy]), \
             mock.patch.object(capm_mod, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
            iv, growth_used, meta = fcf_mod.dcf_intrinsic_value(
                ticker, info=info, cashflow_df=None, currency=ccy, income_df=inc,
            )
        price = info["currentPrice"]
        mos = ((iv - price) / iv) * 100 if iv and iv > 0 else None
        results[ticker] = {
            "iv": round(iv, 4) if iv else iv, "mos": round(mos, 4) if mos is not None else None,
            "discount": meta.get("discount_rate_used"),
            "growth": round(growth_used, 6) if growth_used else growth_used,
            "base": round(meta.get("fcf_used"), 2) if meta.get("fcf_used") else meta.get("fcf_used"),
            "fcf_source": meta.get("fcf_source"),
            "moat_mode": "financials" if meta.get("is_financials_mode") else "standard",
        }
    return results


_ALL_TICKERS = [t for t, *_ in _STD_FIXTURES] + [t for t, *_ in _FIN_FIXTURES]

os.environ.pop("FINANCIALS_STORE_LIVE", None)
_saved_capm = sys.modules.pop("capm_engine", None)
_saved_fcf = sys.modules.pop("fcf_valuation_engine", None)
_saved_fc = sys.modules.pop("financials_classifier", None)
try:
    _load_module_from_git("financials_classifier.py", "financials_classifier", _BASE_COMMIT)
    old_capm = _load_module_from_git("capm_engine.py", "capm_engine", _BASE_COMMIT)
    old_fcf = _load_module_from_git("fcf_valuation_engine.py", "fcf_valuation_engine", _BASE_COMMIT)
    _before = _run_all(old_fcf, old_capm)
finally:
    for _name, _saved in (("financials_classifier", _saved_fc), ("capm_engine", _saved_capm),
                           ("fcf_valuation_engine", _saved_fcf)):
        if _saved is not None:
            sys.modules[_name] = _saved
        else:
            sys.modules.pop(_name, None)

os.environ.pop("FINANCIALS_STORE_LIVE", None)
import financials_classifier as _new_fc  # noqa: E402
import capm_engine as _new_capm  # noqa: E402
import fcf_valuation_engine as _new_fcf  # noqa: E402
assert not _new_fc.is_financials_store_live(), "switch must be OFF for this proof"
_after = _run_all(_new_fcf, _new_capm)

_fields = ("iv", "mos", "discount", "growth", "base", "fcf_source", "moat_mode")
_mismatches = []
for _ticker in _ALL_TICKERS:
    for _field in _fields:
        if _before[_ticker][_field] != _after[_ticker][_field]:
            _mismatches.append((_ticker, _field, _before[_ticker][_field], _after[_ticker][_field]))
print(f"\n[switch_off_proof_vs_{_BASE_COMMIT}] {len(_ALL_TICKERS)} fixtures x {len(_fields)} "
      f"columns = {len(_ALL_TICKERS) * len(_fields)} cells checked, switch OFF vs commit "
      f"{_BASE_COMMIT}: {len(_mismatches)} mismatch(es)")
for _t, _f, _b, _a in _mismatches:
    print(f"    MISMATCH: {_t} {_f}: before={_b!r} after={_a!r}")
assert not _mismatches, _mismatches
print(f"[switch_off_proof_vs_{_BASE_COMMIT}] 0 mismatches - switch OFF is byte-identical to "
      f"{_BASE_COMMIT} for every fixture, every column OK")
