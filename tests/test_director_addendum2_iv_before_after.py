"""
Director addendum 2 reply (3 Oct 2026) - item 1(c): "the intrinsic-
value-level before/after table for every fixture."

Runs a representative USD/AUD/GBp/CAD/JPY fixture through the REAL
fcf_valuation_engine.dcf_intrinsic_value() loaded from commit 1d09107
(the last Lists & display commit, immediately before Director addendum
2's five commits A-E) and against this commit's current code - same
"_load_module_from_git" pattern as tests/test_stage1a_fix_london_deep_
dive.py's own T7. None of addendum 2's five commits (3d39afe/8dc866e/
851de3f/f4ccfff/cc29484) touch fcf_valuation_engine.py or capm_engine.py
at all (confirmed by `git diff --stat 1d09107 HEAD -- fcf_valuation_
engine.py capm_engine.py auto_compounder_engine.py` below, run as part
of this file) - this test is the direct empirical proof that backs
that grep-level claim up, at the intrinsic-value level itself.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_addendum2_iv_before_after.py
"""
import importlib.util
import os
import subprocess
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ======================================================================
# Grep-level confirmation first: no valuation-engine file changed
# between 1d09107 and HEAD across any of addendum 2's 5 commits.
# ======================================================================
_BASE_COMMIT = "1d09107"
_valuation_files = ["fcf_valuation_engine.py", "capm_engine.py", "auto_compounder_engine.py",
                     "resolver_engine.py", "deep_dive_engine.py", "moat_engine.py"]
_diff = subprocess.run(
    ["git", "diff", "--stat", _BASE_COMMIT, "HEAD", "--"] + _valuation_files,
    cwd=REPO_ROOT, capture_output=True, text=True, check=True,
).stdout.strip()
assert _diff == "", f"a valuation-engine file DID change between {_BASE_COMMIT} and HEAD:\n{_diff}"
print(f"[grep_confirmation] git diff --stat {_BASE_COMMIT} HEAD -- "
      f"{' '.join(_valuation_files)} is EMPTY - no valuation engine file touched OK")


def _mk_cashflow_df(ocf, capex, years=5, growth=1.0):
    import pandas as pd
    cols = {}
    for i in range(years):
        cols[f"202{6 - i}-06-30"] = [ocf * (growth ** -i), capex * (growth ** -i)]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


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


# Five fixtures spanning every currency this whole day's work (Lists &
# display + addendum 2) touched - USD/AUD as the untouched control
# group, GBp/CAD/JPY as the universes Lists & display Commit 1-6 and
# addendum 2 items A/B/D actually worked on.
_FIXTURES = [
    ("ADP", {
        "currency": "USD", "financialCurrency": "USD", "currentPrice": 250.0,
        "marketCap": 120_000_000_000, "sharesOutstanding": 120_000_000_000 / 250.0,
        "longName": "ADP-shaped",
    }, _mk_cashflow_df(3_000_000_000.0, -300_000_000.0, growth=1.03), "USD"),
    ("CSL.AX", {
        "currency": "AUD", "financialCurrency": "AUD", "currentPrice": 280.0,
        "marketCap": 130_000_000_000, "sharesOutstanding": 130_000_000_000 / 280.0,
        "longName": "CSL-shaped",
    }, _mk_cashflow_df(2_500_000_000.0, -300_000_000.0, growth=1.03), "AUD"),
    ("AZN_FIXTURE.L", {
        "currency": "GBp", "financialCurrency": "GBP", "currentPrice": 2600.0,
        "marketCap": 26.0 * 1_000_000_000, "sharesOutstanding": 1_000_000_000,
        "longName": "AZN-shaped",
    }, _mk_cashflow_df(14_000_000_000.0, -1_400_000_000.0, growth=1.03), "GBP"),
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

_RF_MOCKS = {
    "USD": ("get_risk_free_rate", (0.050, "default"), ("USD",)),
    "AUD": ("get_au_risk_free_rate_live", (0.053, "default"), ()),
    "GBP": ("get_uk_risk_free_rate_live", (0.045, "default"), ()),
    "CAD": ("get_ca_risk_free_rate_live", (0.038, "default"), ()),
    "JPY": ("get_jp_risk_free_rate_live", (0.012, "default"), ()),
}


def _run_fixtures(fcf_mod, capm_mod):
    results = {}
    for ticker, info, cf, ccy in _FIXTURES:
        fn_name, retval, _ = _RF_MOCKS[ccy]
        # get_growth_estimates_5y() is yfinance-backed (live analyst
        # estimates) - forced to "no coverage" so every fixture resolves
        # growth from its own cashflow trend (deterministic, no network,
        # no retry/backoff delay) instead of hanging on a live fetch
        # this sandbox can't complete anyway.
        patches = [
            mock.patch.object(capm_mod, fn_name, return_value=retval),
            mock.patch.object(capm_mod, "get_growth_estimates_5y", return_value=(None, "no_coverage")),
        ]
        for p in patches:
            p.start()
        try:
            iv, growth_used, meta = fcf_mod.dcf_intrinsic_value(
                ticker, info=info, cashflow_df=cf, currency=ccy,
            )
        finally:
            for p in patches:
                p.stop()
        results[ticker] = (
            round(iv, 6) if iv else iv, round(growth_used, 6) if growth_used else growth_used,
            meta.get("discount_rate_used"), meta.get("growth_ceiling_used"),
            meta.get("growth_end_rate_used"),
        )
    return results


# --- BEFORE: commit 1d09107 (end of Lists & display, before addendum 2) ---
_saved_capm = sys.modules.pop("capm_engine", None)
_saved_fcf = sys.modules.pop("fcf_valuation_engine", None)
try:
    old_capm = _load_module_from_git("capm_engine.py", "capm_engine", _BASE_COMMIT)
    old_fcf = _load_module_from_git("fcf_valuation_engine.py", "fcf_valuation_engine", _BASE_COMMIT)
    _before = _run_fixtures(old_fcf, old_capm)
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
_after = _run_fixtures(new_fcf, new_capm)


print("\n[iv_before_after] intrinsic-value-level before/after, every fixture:")
print(f"    {'ticker':16s} {'metric':11s} {'before':>18s} {'after':>18s}  match")
_all_match = True
_metrics = ("iv_per_share", "growth_used", "discount_rate", "growth_ceiling", "growth_end_rate")
for ticker, *_rest in _FIXTURES:
    for i, metric in enumerate(_metrics):
        b = _before[ticker][i]
        a = _after[ticker][i]
        match = b == a
        _all_match = _all_match and match
        print(f"    {ticker:16s} {metric:11s} {str(b):>18s} {str(a):>18s}  {'MATCH' if match else 'MISMATCH'}")
        assert match, (ticker, metric, b, a)

assert _all_match
print("\n[iv_before_after] every metric, every fixture: MATCH - Director addendum 2's "
      "5 commits (items A-E) move nothing at the intrinsic-value level OK")
