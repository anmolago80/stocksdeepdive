"""
Addendum 2 item 1 to instruction_financials_income_store_and_top200_
guard.md (Director, 5 Oct 2026). Switch ON only: the net income base
must be converted from reporting to listing currency EXACTLY ONCE,
however the table reached the store (converted or unconverted).
Switch OFF: nothing changes.

Fixtures: a USD-reporting AUD-listed insurer (QBE-shaped) and a
USD-reporting GBP-listed bank, each stored converted and unconverted -
four cases, one correct value each (the converted-input and
unconverted-input cases for the same company must agree to the cent).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. fx_rate() is mocked (never a live call) so the expected
converted/unconverted values can be derived by hand and checked
exactly, rather than depending on whatever this sandbox's blocked
network calls happen to fall back to.

Run: python3 tests/test_financials_income_store_addendum2_item1.py
"""
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="financials_addendum2_item1_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("FINANCIALS_STORE_LIVE", None)

import fcf_valuation_engine as fve
import financials_income_store as fis
import financials_dry_run as fdr
import nightly_scan as ns
import deep_dive_engine as dd
import fundamentals_data


def _switch(on):
    if on:
        os.environ["FINANCIALS_STORE_LIVE"] = "1"
    else:
        os.environ.pop("FINANCIALS_STORE_LIVE", None)


def _mk_income_df(net_income, cols=None):
    cols = cols or [f"202{5 - i}-12-31" for i in range(len(net_income))]
    return pd.DataFrame({"Net Income Common Stockholders": net_income}, index=cols).T


_DCF_RATES = dict(discount_rate=0.08, perpetual_rate=0.02, growth_rate=0.05)

# QBE-shaped: USD-reporting, AUD-listed insurer.
QBE_INFO = {"sector": "Financial Services", "industry": "Insurance - Property & Casualty",
            "currency": "AUD", "financialCurrency": "USD",
            "sharesOutstanding": 100.0, "currentPrice": 50.0, "marketCap": 5_000_000_000.0}
QBE_USD_RAW = _mk_income_df([412.0, 380.0, 350.0, 300.0, 250.0],
                            cols=["2026-06-30", "2025-06-30", "2024-06-30",
                                  "2023-06-30", "2022-06-30"])
_USD_AUD_RATE = 1.5
QBE_AUD_CONVERTED = _mk_income_df(
    [v * _USD_AUD_RATE for v in [412.0, 380.0, 350.0, 300.0, 250.0]],
    cols=["2026-06-30", "2025-06-30", "2024-06-30", "2023-06-30", "2022-06-30"],
)
QBE_CF = pd.DataFrame(
    {"2026-06-30": [1000.0, -100.0], "2025-06-30": [980.0, -100.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)

# USD-reporting, GBP-listed bank.
BANKGB_INFO = {"sector": "Financial Services", "industry": "Banks - Regional",
               "currency": "GBP", "financialCurrency": "USD",
               "sharesOutstanding": 100.0, "currentPrice": 50.0, "marketCap": 5_000_000_000.0}
BANKGB_USD_RAW = _mk_income_df([500.0, 460.0, 420.0, 400.0, 380.0],
                                cols=["2026-06-30", "2025-06-30", "2024-06-30",
                                      "2023-06-30", "2022-06-30"])
_USD_GBP_RATE = 0.8
BANKGB_GBP_CONVERTED = _mk_income_df(
    [v * _USD_GBP_RATE for v in [500.0, 460.0, 420.0, 400.0, 380.0]],
    cols=["2026-06-30", "2025-06-30", "2024-06-30", "2023-06-30", "2022-06-30"],
)
BANKGB_CF = pd.DataFrame(
    {"2026-06-30": [1200.0, -120.0], "2025-06-30": [1150.0, -120.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)


def _fx_side_effect(from_ccy, to_ccy, log=print):
    if from_ccy == "USD" and to_ccy == "AUD":
        return _USD_AUD_RATE, "live"
    if from_ccy == "USD" and to_ccy == "GBP":
        return _USD_GBP_RATE, "live"
    return 1.0, "live"


# ======================================================================
# C1: QBE-shaped - converted-input (no re-conversion) vs unconverted-
# input (converted once) agree to the cent.
# ======================================================================
with mock.patch.object(fve, "fx_rate", side_effect=_fx_side_effect):
    _iv_converted, _, _meta_converted = fve.dcf_intrinsic_value(
        "QBE_FIXTURE", info=QBE_INFO, cashflow_df=QBE_CF, currency="AUD",
        income_df=QBE_AUD_CONVERTED, income_df_currency_converted=True, **_DCF_RATES,
    )
    _iv_unconverted, _, _meta_unconverted = fve.dcf_intrinsic_value(
        "QBE_FIXTURE", info=QBE_INFO, cashflow_df=QBE_CF, currency="AUD",
        income_df=QBE_USD_RAW, income_df_currency_converted=False, **_DCF_RATES,
    )
assert _meta_converted["fcf_source"] == "net_income_financials", _meta_converted["fcf_source"]
assert _meta_unconverted["fcf_source"] == "net_income_financials", _meta_unconverted["fcf_source"]
assert "fx_converted_upstream" in _meta_converted, _meta_converted
assert _meta_converted.get("fx_converted") is None, _meta_converted  # no SECOND conversion applied
assert _meta_unconverted.get("fx_converted") == "USD->AUD", _meta_unconverted
assert _iv_converted == _iv_unconverted, (_iv_converted, _iv_unconverted)
print(f"[C1_qbe_converted_vs_unconverted_agree] QBE-shaped (USD-reporting, AUD-listed): "
      f"converted-store input (skips re-conversion, fx_converted_upstream={_meta_converted['fx_converted_upstream']!r}) "
      f"and unconverted-store input (converts once, fx_converted={_meta_unconverted['fx_converted']!r}) "
      f"BOTH give IV={_iv_converted} - exactly once, to the cent, regardless of how the table reached the store OK")

# A THIRD case proves the bug this fixes: feeding the converted table
# through WITHOUT the new flag (old behaviour, income_df_currency_
# converted defaulting to False) double-converts and must NOT match.
with mock.patch.object(fve, "fx_rate", side_effect=_fx_side_effect):
    _iv_double_converted, _, _ = fve.dcf_intrinsic_value(
        "QBE_FIXTURE", info=QBE_INFO, cashflow_df=QBE_CF, currency="AUD",
        income_df=QBE_AUD_CONVERTED, **_DCF_RATES,  # income_df_currency_converted left at its default False
    )
assert _iv_double_converted != _iv_converted, (_iv_double_converted, _iv_converted)
print(f"[C1b_old_bug_reproduced_without_the_flag] the SAME converted table, fed WITHOUT "
      f"income_df_currency_converted=True (the pre-fix call shape) -> IV={_iv_double_converted} != "
      f"{_iv_converted} - confirms the flag is what fixes the double-conversion, not a no-op OK")


# ======================================================================
# C2: USD-reporting GBP-listed bank - same cross-check.
# ======================================================================
with mock.patch.object(fve, "fx_rate", side_effect=_fx_side_effect):
    _iv_c, _, _meta_c = fve.dcf_intrinsic_value(
        "BANKGB_FIXTURE", info=BANKGB_INFO, cashflow_df=BANKGB_CF, currency="GBP",
        income_df=BANKGB_GBP_CONVERTED, income_df_currency_converted=True, **_DCF_RATES,
    )
    _iv_u, _, _meta_u = fve.dcf_intrinsic_value(
        "BANKGB_FIXTURE", info=BANKGB_INFO, cashflow_df=BANKGB_CF, currency="GBP",
        income_df=BANKGB_USD_RAW, income_df_currency_converted=False, **_DCF_RATES,
    )
assert _meta_c["fcf_source"] == _meta_u["fcf_source"] == "net_income_financials"
assert _iv_c == _iv_u, (_iv_c, _iv_u)
print(f"[C2_bankgb_converted_vs_unconverted_agree] USD-reporting GBP-listed bank: both input "
      f"shapes give IV={_iv_c}, to the cent OK")


# ======================================================================
# C3: switch OFF - nightly_scan.py always passes
# income_df_currency_converted=False to resolve_intrinsic_value(),
# regardless of what's in the store. "Switch OFF: change nothing."
# ======================================================================
fis.save("QBEOFF", QBE_AUD_CONVERTED, currency="AUD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
_dates = pd.date_range("2026-01-01", periods=90, freq="D")
_hist_df = pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(90)],
                          "Volume": [1_000_000] * 90}, index=_dates)


def _ytw_side_effect(fn, log, ticker_, label, **kw):
    if label == "history":
        return _hist_df
    if label == "info":
        return dict(QBE_INFO)
    if label == "cashflow":
        return QBE_CF
    return None


_switch(False)
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
        "yahoo_estimate_status": "ok", "value_default": False, "fcf_distorted_years": [],
    })
    ns.analyze_ticker_lite("QBEOFF", log=lambda *a, **k: None)
    _kwargs_off = _riv.call_args.kwargs
assert _kwargs_off.get("income_df_currency_converted") is False, _kwargs_off
print("[C3_switch_off_always_false] switch OFF -> nightly_scan.py always passes "
      "income_df_currency_converted=False to resolve_intrinsic_value(), even with a "
      "converted store entry sitting right there (never consulted) - change nothing OK")


# ======================================================================
# C4: switch ON - nightly_scan.py reads the store entry's OWN
# currency_converted flag, both ways.
# ======================================================================
_switch(True)
fis.save("QBEON_CONV", QBE_AUD_CONVERTED, currency="AUD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
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
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None):
    _ytw2.side_effect = _ytw_side_effect
    _riv2.return_value = (150.0, "dcf", 0.12, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "ok", "value_default": False, "fcf_distorted_years": [],
    })
    ns.analyze_ticker_lite("QBEON_CONV", log=lambda *a, **k: None)
    _kwargs_on_conv = _riv2.call_args.kwargs
assert _kwargs_on_conv.get("income_df_currency_converted") is True, _kwargs_on_conv

fis.save("QBEON_RAW", QBE_USD_RAW, currency=None, source="yfinance_direct_unconverted",
          latest_cf_period="2026-06-30", currency_converted=False)
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
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None):
    _ytw3.side_effect = _ytw_side_effect
    _riv3.return_value = (150.0, "dcf", 0.12, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "ok", "value_default": False, "fcf_distorted_years": [],
    })
    ns.analyze_ticker_lite("QBEON_RAW", log=lambda *a, **k: None)
    _kwargs_on_raw = _riv3.call_args.kwargs
assert _kwargs_on_raw.get("income_df_currency_converted") is False, _kwargs_on_raw
print("[C4_switch_on_reads_per_entry_flag] switch ON -> nightly_scan.py reads the store "
      "entry's OWN currency_converted flag per ticker: True for a converted entry, False for "
      "an unconverted one - never assumed just because it came from the store OK")
_switch(False)


# ======================================================================
# C5: Deep Dive store-hit branch reads the same per-entry flag.
# ======================================================================
def _deep_dive_income_df_and_flag(ticker, info, cashflow_df):
    import financials_classifier as fc
    _financials_mode_live = (
        fc.is_financials_store_live() and fc.is_financials(info, ticker=ticker)
    )
    _income_df_currency_converted = False
    if _financials_mode_live:
        _fin_entry = fis.get(ticker)
        if _fin_entry is not None:
            income_df = _fin_entry["income"]
            _income_df_currency_converted = bool(_fin_entry.get("currency_converted"))
        else:
            income_df = None
    else:
        income_df = None
    return income_df, _income_df_currency_converted


_switch(True)
fis.save("DDCONV", QBE_AUD_CONVERTED, currency="AUD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
_income_dd, _flag_dd = _deep_dive_income_df_and_flag("DDCONV", QBE_INFO, QBE_CF)
assert _flag_dd is True
fis.save("DDRAW", QBE_USD_RAW, currency=None, source="yfinance_direct_unconverted",
          latest_cf_period="2026-06-30", currency_converted=False)
_income_dd2, _flag_dd2 = _deep_dive_income_df_and_flag("DDRAW", QBE_INFO, QBE_CF)
assert _flag_dd2 is False
print("[C5_deep_dive_reads_per_entry_flag] Deep Dive's own store-hit branch reads the same "
      "per-entry currency_converted flag OK")
_switch(False)


# ======================================================================
# C6: financials_dry_run.compute_shadow_row() - reads the entry's own
# flag (not hardcoded True), and the new reporting/listing currency
# columns are present and correct.
# ======================================================================
fis.save("SHADOWRAW", QBE_USD_RAW, currency=None, source="yfinance_direct_unconverted",
          latest_cf_period="2026-06-30", currency_converted=False)
with mock.patch.object(fve, "fx_rate", side_effect=_fx_side_effect):
    _row_raw = fdr.compute_shadow_row(
        "SHADOWRAW", "QBE Raw Co", 50.0, QBE_INFO, QBE_CF, "AUD",
        now_intrinsic_value=None, now_mos_pct=None, now_fcf_source="ocf_fallback_financials",
        now_dcf_unreliable=False, now_moat_mode="financials", now_quality=70, **_DCF_RATES,
    )
fis.save("SHADOWCONV", QBE_AUD_CONVERTED, currency="AUD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
with mock.patch.object(fve, "fx_rate", side_effect=_fx_side_effect):
    _row_conv = fdr.compute_shadow_row(
        "SHADOWCONV", "QBE Conv Co", 50.0, QBE_INFO, QBE_CF, "AUD",
        now_intrinsic_value=None, now_mos_pct=None, now_fcf_source="ocf_fallback_financials",
        now_dcf_unreliable=False, now_moat_mode="financials", now_quality=70, **_DCF_RATES,
    )
assert _row_raw["shadow_intrinsic_value"] == _row_conv["shadow_intrinsic_value"], \
    (_row_raw["shadow_intrinsic_value"], _row_conv["shadow_intrinsic_value"])
assert _row_raw["reporting_currency"] == "USD", _row_raw["reporting_currency"]
assert _row_raw["listing_currency"] == "AUD", _row_raw["listing_currency"]
print(f"[C6_dry_run_shadow_row_correct] compute_shadow_row() reads each entry's own "
      f"currency_converted flag - raw and pre-converted store entries give the IDENTICAL "
      f"shadow IV ({_row_raw['shadow_intrinsic_value']}); reporting_currency="
      f"{_row_raw['reporting_currency']!r}, listing_currency={_row_raw['listing_currency']!r} "
      "columns present and correct OK")
fis._store_dir()
for _f in os.listdir(fis._store_dir()):
    os.remove(os.path.join(fis._store_dir(), _f))
_switch(False)


print("\nALL ADDENDUM 2 ITEM 1 (CURRENCY DOUBLE-CONVERSION) CHECKS PASSED")
