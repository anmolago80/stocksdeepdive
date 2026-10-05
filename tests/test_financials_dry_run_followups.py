"""
instruction_financials_dry_run_followups.md (Director, 5 Oct 2026) -
Addendum 2 item 7 of instruction_financials_income_store_and_top200_
guard.md: three follow-ups from the first real dry run (S&P 500, scan
of 4 Oct). Switch FINANCIALS_STORE_LIVE stays OFF throughout; nothing
here changes a live value.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. No test here makes a live network call.

Run: python3 tests/test_financials_dry_run_followups.py
"""
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="financials_dry_run_followups_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("FINANCIALS_STORE_LIVE", None)

import financials_classifier as fc
import financials_dry_run as fdr
import financials_income_store as fis
import fcf_valuation_engine as fve
import fundamentals_data
import capm_engine


def _mk_income_df(net_income, cols=None, label="Net Income Common Stockholders"):
    cols = cols or [f"202{5 - i}-12-31" for i in range(len(net_income))]
    return pd.DataFrame({label: net_income}, index=cols).T


def _clear_store():
    for f in os.listdir(fis._store_dir()):
        os.remove(os.path.join(fis._store_dir(), f))


_DCF_RATES = dict(discount_rate=0.08, perpetual_rate=0.02, growth_rate=0.05)


# ======================================================================
# Item 1 - a table where the substring fallback would pick the WRONG
# net income row (neither exact label present; a decoy row - "Net
# Income Discontinuous Operations" - comes before the real one in
# column order and matches the "net income" substring first).
# ======================================================================
_clear_store()
_decoy_df = pd.DataFrame(
    {"2026-12-31": [999.0], "2025-12-31": [888.0]},
    index=["Net Income Discontinuous Operations"],
)
_real_df = pd.DataFrame(
    {"2026-12-31": [50.0], "2025-12-31": [48.0]},
    index=["Net Income Attributable To Parent Diluted"],  # not in _NET_INCOME_LABELS
)
_decoy_table = pd.concat([_decoy_df, _real_df])
_label, _values = fve._row_with_label(_decoy_table, fve._NET_INCOME_LABELS)
assert _label == "Net Income Discontinuous Operations", _label
assert _values == [999.0, 888.0], _values
print(f"[item1_substring_fallback_risk_fixture] neither exact _NET_INCOME_LABELS entry present; "
      f"the decoy row ('Net Income Discontinuous Operations', appearing BEFORE the real income "
      f"row) wins the substring fallback -> ni_row_label={_label!r} ni_values={_values!r} - "
      "exactly the risk the instruction asked to surface, now visible on the dry run's own "
      "ni_row_label/ni_values columns rather than only inferable from the valuation OK")

# The SAME risk surfaced through compute_shadow_row()'s own ni_row_label/
# ni_values columns, end to end.
_DECOY_INFO = {"sector": "Financial Services", "industry": "Insurance - Life",
               "currency": "USD", "financialCurrency": "USD",
               "sharesOutstanding": 10.0, "currentPrice": 20.0, "marketCap": 200.0}
fis.save("DECOYTICK", _decoy_table, currency="USD", source="yfinance_bundle_cached", currency_converted=True)
with mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None), \
     mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.05, "default")), \
     mock.patch.object(capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _decoy_row = fdr.compute_shadow_row(
        "DECOYTICK", "DecoyCo", 20.0, _DECOY_INFO, None, "USD",
        None, None, "ocf_fallback_financials", False, "financials", 70,
    )
assert _decoy_row["ni_row_label"] == "Net Income Discontinuous Operations", _decoy_row["ni_row_label"]
assert _decoy_row["ni_values"] == [("2026-12-31", 999.0), ("2025-12-31", 888.0)], _decoy_row["ni_values"]
print("[item1_decoy_visible_in_dry_run] compute_shadow_row()'s own ni_row_label/ni_values "
      f"columns show the same decoy match end to end: {_decoy_row['ni_row_label']!r}, "
      f"{_decoy_row['ni_values']!r} OK")

# No-entry / no-match rows get (None, None), never a stale value from a
# previous call.
_clear_store()
assert fdr._ni_diagnostics(None) == (None, None)
_empty_df = pd.DataFrame({"2026-12-31": [1.0]}, index=["Revenue"])  # no net-income row at all
assert fve._row_with_label(_empty_df, fve._NET_INCOME_LABELS) == (None, None)
print("[item1_no_match_is_none] no store entry, or an income table with no net-income row at "
      "all, both give (None, None) - never a stale leftover value OK")


# ======================================================================
# Item 2 - a financials-mode ticker whose LATEST net-income year is
# negative (IVZ-shaped): the Task-10 outlier-median swap in _financials_
# base_and_series() can rescue a single bad year by swapping to the
# median of the last 3 - this fixture is deliberately shaped so even
# that swapped median stays negative, reaching dcf_intrinsic_value()'s
# own info["freeCashflow"] fallback (fcf_source "info").
# ======================================================================
_clear_store()
IVZ_INFO = {"sector": "Financial Services", "industry": "Asset Management",
            "currency": "USD", "financialCurrency": "USD",
            "sharesOutstanding": 100.0, "currentPrice": 30.66, "marketCap": 3_066_000_000.0,
            "longName": "IVZ-shaped", "freeCashflow": 500_000_000.0}
IVZ_INCOME = _mk_income_df(
    [-50.0, 10.0, -5.0, 70.0, 65.0],
    cols=["2026-12-31", "2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"],
)
fis.save("IVZTICK", IVZ_INCOME, currency="USD", source="yfinance_bundle_oneoff", currency_converted=True)

# Sanity: confirm the fixture genuinely still has >= 2 positive points
# (reaches the net-income dispatch at all) but a non-positive BASE
# (reaches the info fallback) - not the lt2_positive_years fallback.
_base, _series, _src, *_ = fve._financials_base_and_series(IVZ_INCOME)
assert _src == "net_income_financials", _src
assert _base <= 0, _base

with mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None), \
     mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.05, "default")), \
     mock.patch.object(capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _ivz_row = fdr.compute_shadow_row(
        "IVZTICK", "IVZCo", 30.66, IVZ_INFO, None, "USD",
        92.79, -201.0, "ocf_fallback_financials", True, "financials", 70,
        **_DCF_RATES,
    )
assert _ivz_row["shadow_fcf_source"] == "info", _ivz_row["shadow_fcf_source"]
assert _ivz_row["shadow_mode"] == "financials", _ivz_row["shadow_mode"]
assert _ivz_row["status"] == "ni_path_abandoned", _ivz_row["status"]
assert _ivz_row["shadow_reason"] is not None and "negative" in _ivz_row["shadow_reason"]
# Item 2's own pool-eligibility answer, as it stood BEFORE Commit B1
# (instruction_top200_blank_replies_and_financials_gap.md, 5 Oct
# 2026): fcf_source "info" was NOT "ocf_fallback_financials", so the
# Commit 4 pool-exclusion rule didn't catch it - this was exactly the
# gap B1 closed (IVZ was B1's own live example). pool_ineligible_if_
# switch_on is now True for this fixture: top100_engine._financials_
# pool_ineligible() excludes a financials-mode row on "info"/"none"
# too, not just the literal "ocf_fallback_financials" string.
assert _ivz_row["pool_ineligible_if_switch_on"] is True, _ivz_row
print(f"[item2_negative_latest_year_reaches_info] IVZ-shaped fixture: fcf_source="
      f"{_ivz_row['shadow_fcf_source']!r}, status={_ivz_row['status']!r}, "
      f"shadow_reason={_ivz_row['shadow_reason']!r} - and pool_ineligible_if_switch_on="
      f"{_ivz_row['pool_ineligible_if_switch_on']} (Commit B1: now excluded - this is the "
      "live gap B1 closed, this fixture IS the IVZ example) OK")
_clear_store()


# ======================================================================
# Item 3a - an override ticker (V) and a non-override ticker (a plain
# bank) through the new now_moat/shadow_moat columns, fed a bundle via
# fundamentals_data.peek_cached_bundle() (zero new network call).
# ======================================================================
def _mk_balance(cols):
    return pd.DataFrame({
        "Total Assets": [1000.0 - 10 * i for i in range(len(cols))],
        "Stockholders Equity": [100.0 - i for i in range(len(cols))],
        "Total Debt": [50.0] * len(cols),
        "Cash And Cash Equivalents": [20.0] * len(cols),
        "Invested Capital": [130.0 - i for i in range(len(cols))],
    }, index=cols).T


_cols = ["2026-12-31", "2025-12-31", "2024-12-31", "2023-12-31"]
V_INFO = {"sector": "Financial Services", "industry": "Credit Services",
          "currency": "USD", "financialCurrency": "USD",
          "sharesOutstanding": 100.0, "currentPrice": 300.0, "marketCap": 30_000_000_000.0,
          "longName": "V-shaped"}
# Operating Income/Pretax Income/Tax Provision/Total Revenue included -
# the standard-mode (ROIC/EBIT-based) pillars need these; a net-income-
# only income statement (as fed to the financials-mode path elsewhere
# in this file) leaves the ROIC path with nothing to compute from,
# which would make now_moat/shadow_moat differ for the wrong reason
# (one path simply having no data) rather than the real one (which
# pillars each mode actually uses).
V_INCOME = pd.DataFrame({
    "Net Income Common Stockholders": [20.0, 18.0, 16.0, 14.0],
    "Operating Income": [25.0, 23.0, 21.0, 19.0],
    "Pretax Income": [24.0, 22.0, 20.0, 18.0],
    "Tax Provision": [4.0, 4.0, 4.0, 4.0],
    "Total Revenue": [100.0, 95.0, 90.0, 85.0],
}, index=_cols).T
V_BALANCE = _mk_balance(_cols)
V_BUNDLE = {"income": V_INCOME, "balance": V_BALANCE, "cashflow": None,
            "info": V_INFO, "meta": {"source": "yfinance"}}

BANK_INFO = {"sector": "Financial Services", "industry": "Banks - Regional",
             "currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 100.0, "currentPrice": 50.0, "marketCap": 5_000_000_000.0,
             "longName": "BANK-shaped"}
BANK_INCOME = _mk_income_df([50.0, 48.0, 46.0, 44.0], cols=_cols)
BANK_BALANCE = _mk_balance(_cols)
BANK_BUNDLE = {"income": BANK_INCOME, "balance": BANK_BALANCE, "cashflow": None,
               "info": BANK_INFO, "meta": {"source": "yfinance"}}

fis.save("V", V_INCOME, currency="USD", source="yfinance_bundle_cached", currency_converted=True)
fis.save("BANKTICK", BANK_INCOME, currency="USD", source="yfinance_bundle_cached", currency_converted=True)

with mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=V_BUNDLE), \
     mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.05, "default")), \
     mock.patch.object(capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    # "V" (not a fixture-prefixed name) - the literal ticker the override
    # table matches against (financials_classifier._OVERRIDE_OUT_TICKERS).
    _v_row = fdr.compute_shadow_row("V", "V Inc", 300.0, V_INFO, None, "USD",
                                     100.0, 70.0, "net_income_financials", False, "financials", 80)
with mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=BANK_BUNDLE), \
     mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.05, "default")), \
     mock.patch.object(capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _bank_row = fdr.compute_shadow_row("BANKTICK", "BankCo", 50.0, BANK_INFO, None, "USD",
                                        40.0, 60.0, "net_income_financials", False, "financials", 70)

# V: override OUT with the switch ON -> now_moat (financials-mode ROE
# path) and shadow_moat (standard-mode ROIC path) must be computed and
# must DIFFER (the override genuinely changes which pillar inputs are
# used), both from the SAME bundle, zero new network call.
assert _v_row["now_moat"] is not None and _v_row["shadow_moat"] is not None, _v_row
assert _v_row["now_moat"] != _v_row["shadow_moat"], (
    "V's override must change the Moat computation, not just its label")
assert _v_row["shadow_mode_reason"] == "ticker_table_out", _v_row["shadow_mode_reason"]
print(f"[item3a_override_ticker_moat_columns] V (override OUT): now_moat={_v_row['now_moat']} "
      f"(financials-mode ROE path, sector-driven) != shadow_moat={_v_row['shadow_moat']} "
      f"(standard-mode ROIC path, override-driven), shadow_mode_reason="
      f"{_v_row['shadow_mode_reason']!r} OK")

# A non-override bank: now_moat == shadow_moat (same classification
# either way - shadow_mode equals now mode, so the shadow figures must
# simply equal the current ones, per the instruction's own rule).
assert _bank_row["now_moat"] is not None, _bank_row
assert _bank_row["now_moat"] == _bank_row["shadow_moat"], _bank_row
assert _bank_row["shadow_mode_reason"] == "existing_rule", _bank_row["shadow_mode_reason"]
assert _bank_row["shadow_intrinsic_value"] is not None
print(f"[item3a_non_override_ticker_unchanged] a plain bank (no override match): "
      f"now_moat == shadow_moat == {_bank_row['now_moat']} - where shadow mode equals the "
      "current mode, the shadow figures simply equal the current ones, exactly as the "
      "instruction requires OK")

# now_moat/shadow_moat are "n/a" (None) with no bundle cached - never
# an estimate.
with mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None), \
     mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.05, "default")), \
     mock.patch.object(capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _nobundle_row = fdr.compute_shadow_row("BANKTICK", "BankCo", 50.0, BANK_INFO, None, "USD",
                                            40.0, 60.0, "net_income_financials", False, "financials", 70)
assert _nobundle_row["now_moat"] is None and _nobundle_row["shadow_moat"] is None
print("[item3a_no_bundle_is_na] nothing cached for this ticker -> now_moat/shadow_moat both "
      "None (the caller renders 'n/a') - never an estimate OK")
_clear_store()


# ======================================================================
# Item 3b - Quality/Long Score grep-verify: does NOT use financials_
# classifier (so shadow_quality's removal, and now_moat/shadow_moat's
# addition, were the right call per the instruction's own branching
# rule).
# ======================================================================
import inspect
import auto_quality_engine
import ranking_engine
_aq_src = inspect.getsource(auto_quality_engine)
_re_src = inspect.getsource(ranking_engine)
assert "financials_classifier" not in _aq_src and "is_financials" not in _aq_src
assert "financials_classifier" not in _re_src and "is_financials" not in _re_src
print("[item3b_quality_long_score_grep] auto_quality_engine.py and ranking_engine.py (Quality, "
      "calculate_long_score()) contain no reference to financials_classifier/is_financials - "
      "confirmed from source, not just by inspection OK")


# ======================================================================
# Item 3c - every shadow_mode_reason value.
# ======================================================================
_REIT_INFO = {"industry": "REIT - Retail"}
assert fc.shadow_mode_reason(_REIT_INFO, ticker="O") == "reit_exclusion"
assert fc.shadow_mode_reason({"industry": "Credit Services"}, ticker="V") == "ticker_table_out"
assert fc.shadow_mode_reason({"industry": "Financial Data & Stock Exchanges"}, ticker="CME") == "ticker_table_in"
assert fc.shadow_mode_reason({"industry": "Financial Data & Stock Exchanges"}, ticker="COIN") == "industry_rule"
assert fc.shadow_mode_reason({"sector": "Financial Services", "industry": "Banks - Regional"}, ticker="JPM") == "existing_rule"
assert fc.shadow_mode_reason({"sector": "Technology"}, ticker="AAPL") == "existing_rule"
print("[item3c_every_shadow_mode_reason_value] reit_exclusion/ticker_table_out/ticker_table_in/"
      "industry_rule/existing_rule all reproduced from fixtures, including COIN/FDS-shaped "
      "(industry_rule alone, not the explicit ticker list) OK")
# is_financials_shadow() must still agree with the refactored helper -
# no behaviour change from factoring the reason out.
for _info, _t, _expect in [
    (_REIT_INFO, "O", False),
    ({"industry": "Credit Services"}, "V", False),
    ({"industry": "Financial Data & Stock Exchanges"}, "CME", True),
    ({"industry": "Financial Data & Stock Exchanges"}, "COIN", False),
    ({"sector": "Financial Services", "industry": "Banks - Regional"}, "JPM", True),
    ({"sector": "Technology"}, "AAPL", False),
]:
    assert fc.is_financials_shadow(_info, ticker=_t) is _expect, (_info, _t)
print("[item3c_shadow_reason_matches_shadow_verdict] is_financials_shadow()'s own boolean "
      "answer agrees with shadow_mode_reason()'s verdict for every one of the 6 fixtures above "
      "- the refactor changed nothing behavioural OK")


# ======================================================================
# Item: no new network call in the shadow computation, including the
# new Moat columns - yf.Ticker raising on construction must never fire,
# and the new Moat columns must not pull their own live data either
# (fundamentals_data.get_bundle - as opposed to peek_cached_bundle -
# would do a live fetch on a cache miss).
# ======================================================================
_clear_store()
fis.save("NONETTICK", BANK_INCOME, currency="USD", source="yfinance_bundle_cached", currency_converted=True)


class _ExplodingTicker:
    def __init__(self, *a, **kw):
        raise AssertionError("compute_shadow_row() made a live yfinance call")


with mock.patch("yfinance.Ticker", _ExplodingTicker), \
     mock.patch.object(fundamentals_data, "get_bundle", side_effect=AssertionError(
         "compute_shadow_row() must use peek_cached_bundle(), never get_bundle()")), \
     mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.05, "default")), \
     mock.patch.object(capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _no_net_row = fdr.compute_shadow_row(
        "NONETTICK", "NoNet Co", 50.0, BANK_INFO, None, "USD",
        None, None, "ocf_fallback_financials", False, "financials", 70,
    )
assert _no_net_row["ticker"] == "NONETTICK"
print("[no_new_network_call_with_moat_columns] compute_shadow_row() with the new now_moat/"
      "shadow_moat columns still never constructs yf.Ticker and never calls fundamentals_"
      "data.get_bundle() (only peek_cached_bundle(), which never fetches) - a forced "
      "AssertionError on either one never fires OK")
_clear_store()


# ======================================================================
# "The switch-OFF proof again, against commit 50f60b7: same nine
# fixtures, same seven columns, zero mismatches. Print the count."
# Same _load_module_from_git pattern as tests/test_director_addendum2_
# full_iv_before_after.py and tests/test_stage1a_fix_london_deep_
# dive.py's own T7.
# ======================================================================
import importlib.util
import subprocess

_BASE_COMMIT = "50f60b7"


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


print("\nALL FINANCIALS DRY RUN FOLLOW-UPS (ADDENDUM 2 ITEM 7) CHECKS PASSED")
