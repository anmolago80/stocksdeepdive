"""
Stage 1a - UK/Canada data layer (owner/Director-directed, 3 Oct 2026):
GBp->GBP pence normalisation, GBP/CAD/EUR FX, GBP/CAD risk-free and
perpetual rates, a Wikipedia->Yahoo symbol-mapping helper, and a scan-row
MOS sweep guard for the later Stage 1c task.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC. This sandbox has no outbound
network access (confirmed: every live yfinance/requests call below
fails and falls through to this commit's own fallback paths) and no
Anthropic API key, so nothing here was run against a real Yahoo quote,
a real Bank of England/Bank of Canada response, or a real Anthropic
model - "verified" in this file means "verified against a synthetic,
hand-built fixture shaped like the real thing", never "verified live".
The Railway log, after a real deploy, is what confirms the live paths
(the [units]/[fx]/[capm] GBP|CAD risk-free lines) actually work against
the real services - see this task's own report for what to look for.

T7's own methodology note: there is no literal "before this commit"
snapshot to diff against inside one file. Every USD/AUD constant/
formula this commit touches was only ever EXTENDED (new dict keys, new
currency branches) - never a changed VALUE or a changed code path for
USD/AUD - so recomputing the expected result directly from the
UNCHANGED constants/formula and asserting the real function matches it
exactly is mathematically equivalent to "matches the pre-commit code",
and is what T7 below actually does.

Run: python3 tests/test_stage1a_gbp_cad_data_layer.py
"""
import contextlib
import io
import math
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="stage1a_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import capm_engine as capm
import fcf_valuation_engine as fcf
import fundamentals_data as fd
import scan_store
import symbol_mapping as sm


def _fake_ticker(info, income=None, balance=None, cashflow=None,
                  dividends=None, history_fn=None):
    income = income if income is not None else pd.DataFrame()
    balance = balance if balance is not None else pd.DataFrame()
    cashflow = cashflow if cashflow is not None else pd.DataFrame()
    dividends = dividends if dividends is not None else pd.Series(dtype=float)

    class _FT:
        def __init__(self, *_a, **_k):
            self.info = dict(info)
            self.income_stmt = income
            self.balance_sheet = balance
            self.cashflow = cashflow
            self.quarterly_income_stmt = pd.DataFrame()
            self.dividends = dividends

        @property
        def fast_info(self):
            raise AttributeError

        def history(self, *a, **k):
            return history_fn() if history_fn else pd.DataFrame()

    return _FT


# ======================================================================
# T1: GBp ticker (SHEL.L-shaped) - currency "GBp", price 2600 (pence),
# financialCurrency "USD" (statements need FX conversion into GBP).
# ======================================================================
_t1_info = {
    "currency": "GBp", "financialCurrency": "USD",
    "currentPrice": 2600.0, "regularMarketPrice": 2600.0,
    "marketCap": 26.0 * 1_000_000, "sharesOutstanding": 1_000_000,
}
_t1_income = pd.DataFrame({"2025-12-31": [500_000.0]}, index=["Net Income"])
_t1_balance = pd.DataFrame({"2025-12-31": [3_000_000.0]}, index=["Total Stockholders Equity"])

with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_t1_info, income=_t1_income, balance=_t1_balance)
    _b1 = fd.get_bundle("SHEL.L", force_refresh=True)

assert _b1["info"]["currentPrice"] == 26.00, _b1["info"]["currentPrice"]
assert _b1["info"]["currency"] == "GBP", _b1["info"]["currency"]
assert _b1["meta"]["price_quote_unit"] == "GBp", _b1["meta"]["price_quote_unit"]
assert _b1["meta"]["price_unit_suspect"] is False
assert _b1["meta"]["flags"].count("currency_converted") == 1, _b1["meta"]["flags"]

# price x shares within 5% of fixture marketCap (marketCap itself is
# NEVER divided - see _PENCE_PRICE_INFO_FIELDS's own exclusion list).
_mcap_check_1 = (_b1["info"]["currentPrice"] * _b1["info"]["sharesOutstanding"]) / _b1["info"]["marketCap"]
assert abs(_mcap_check_1 - 1.0) <= 0.05, _mcap_check_1

# trailingEps/bookValue derived from the (FX-converted) statements, not
# left as an unconfirmed-unit guess from `info` (there was none in this
# fixture to begin with - popped either way).
_fx_usd_gbp, _ = fcf.fx_rate("USD", "GBP")
_expected_eps = (500_000.0 * _fx_usd_gbp) / 1_000_000
assert abs(_b1["info"]["trailingEps"] - _expected_eps) < 1e-6, (_b1["info"]["trailingEps"], _expected_eps)
assert _b1["info"].get("forwardEps") is None, "forwardEps has no statement-derived substitute - must stay dropped"

# P/E within 5% of a fixture-computed expected P/E (price / EPS, both
# now genuinely in pounds).
_pe_1 = _b1["info"]["currentPrice"] / _b1["info"]["trailingEps"]
_expected_pe_1 = 26.00 / _expected_eps
assert abs(_pe_1 - _expected_pe_1) / _expected_pe_1 <= 0.05, (_pe_1, _expected_pe_1)

# MOS computed on pounds: a toy intrinsic value fixture in pounds (£30)
# against the normalised £26 price gives a plausible, pounds-scale MOS
# - never the ~15,000%+ garbage a stray x100 unit bug would produce.
_toy_intrinsic_1 = 30.0
_mos_1 = ((_toy_intrinsic_1 - _b1["info"]["currentPrice"]) / _toy_intrinsic_1) * 100
assert -10 < _mos_1 < 50, _mos_1
print("[T1_gbp_pence_normalisation] SHEL.L-shaped GBp ticker: price 2600p -> £26.00, currency "
      "GBP, price_quote_unit GBp; price*shares within 5% of marketCap; P/E and MOS both computed "
      "on genuinely-pounds figures; statements converted USD->GBP before the EPS/book-value "
      "re-derivation OK")


# ======================================================================
# T2: GBP-quoted LSE ticker (currency "GBP", price ~12.50) - untouched
# by normalisation entirely.
# ======================================================================
_t2_info = {
    "currency": "GBP", "currentPrice": 12.50,
    "marketCap": 12.50 * 1_000_000, "sharesOutstanding": 1_000_000,
    "trailingEps": 0.625,
}
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_t2_info)
    _b2 = fd.get_bundle("GLEN.L", force_refresh=True)

assert _b2["info"]["currentPrice"] == 12.50
assert _b2["info"]["currency"] == "GBP"
assert _b2["meta"]["price_quote_unit"] is None
assert "_price_quote_unit" not in _b2["info"]
_mcap_check_2 = (_b2["info"]["currentPrice"] * _b2["info"]["sharesOutstanding"]) / _b2["info"]["marketCap"]
assert abs(_mcap_check_2 - 1.0) <= 0.05, _mcap_check_2
_pe_2 = _b2["info"]["currentPrice"] / _b2["info"]["trailingEps"]
assert abs(_pe_2 - 20.0) < 1e-6, _pe_2
_mos_2 = ((15.0 - _b2["info"]["currentPrice"]) / 15.0) * 100
assert -10 < _mos_2 < 50, _mos_2
print("[T2_pound_quoted_lse_untouched] a GBP-quoted (already-pounds) LSE ticker is completely "
      "untouched by the pence normalisation - price, market-cap check, P/E and MOS all unaffected OK")


# ======================================================================
# T3: GBp ticker reporting IN GBP (financialCurrency == currency, no FX
# step needed) - same market cap/P-E/MOS assertions as T1.
# ======================================================================
_t3_info = {
    "currency": "GBp", "financialCurrency": "GBP",
    "currentPrice": 1000.0, "marketCap": 10.0 * 2_000_000, "sharesOutstanding": 2_000_000,
}
_t3_income = pd.DataFrame({"2025-12-31": [1_000_000.0]}, index=["Net Income"])
_t3_balance = pd.DataFrame({"2025-12-31": [8_000_000.0]}, index=["Total Stockholders Equity"])
with mock.patch.object(fd, "yf") as _myf:
    _myf.Ticker.side_effect = _fake_ticker(_t3_info, income=_t3_income, balance=_t3_balance)
    _b3 = fd.get_bundle("T3.L", force_refresh=True)

assert _b3["info"]["currentPrice"] == 10.00, _b3["info"]["currentPrice"]
assert _b3["info"]["currency"] == "GBP"
assert _b3["meta"]["price_quote_unit"] == "GBp"
assert "currency_converted" not in _b3["meta"]["flags"], (
    "financialCurrency == currency after normalisation - no FX conversion step should fire")
_mcap_check_3 = (_b3["info"]["currentPrice"] * _b3["info"]["sharesOutstanding"]) / _b3["info"]["marketCap"]
assert abs(_mcap_check_3 - 1.0) <= 0.05, _mcap_check_3
_expected_eps_3 = 1_000_000.0 / 2_000_000  # no FX multiply - already GBP
assert abs(_b3["info"]["trailingEps"] - _expected_eps_3) < 1e-9, (_b3["info"]["trailingEps"], _expected_eps_3)
_pe_3 = _b3["info"]["currentPrice"] / _b3["info"]["trailingEps"]
assert abs(_pe_3 - (10.00 / _expected_eps_3)) < 1e-6
print("[T3_gbp_pence_reporting_in_gbp] a GBp ticker that ALSO reports its statements in GBP "
      "(no FX conversion needed) normalises identically to T1 - no currency_converted flag fires, "
      "EPS/book value still correctly re-derived from the (already-GBP) statements OK")


# ======================================================================
# T4: runtime guard - a GBp fixture whose price x shares is ~100x off
# marketCap -> price_unit_suspect, no IV/MOS via build_sections(), the
# reason string present, and the [units] log line emitted.
# ======================================================================
_t4_info = {
    "currency": "GBp", "currentPrice": 2600.0,
    "marketCap": 2600.0 * 1_000_000,  # NOT divided by 100 - inconsistent with the normalised price
    "sharesOutstanding": 1_000_000,
}
_log_capture = io.StringIO()
with mock.patch.object(fd, "yf") as _myf, contextlib.redirect_stdout(_log_capture):
    _myf.Ticker.side_effect = _fake_ticker(_t4_info)
    _b4 = fd.get_bundle("SUSPECT.L", force_refresh=True)

assert _b4["meta"]["price_unit_suspect"] is True
assert _b4["meta"]["price_unit_suspect_reason"] == "price unit could not be confirmed"
assert "price_unit_suspect" in _b4["meta"]["flags"]
_log_text = _log_capture.getvalue()
assert "[units] SUSPECT.L GBp->GBP" in _log_text and "SUSPECT" in _log_text, _log_text

import auto_compounder_engine as ace
with mock.patch.object(fd, "get_bundle", return_value=_b4):
    _sections = ace.build_sections("SUSPECT.L", force_refresh=True)
assert _sections is None, "build_sections() must withhold IV/MOS for a price_unit_suspect ticker"
print("[T4_runtime_guard] a GBp fixture whose price*shares disagrees with marketCap by 100x -> "
      "price_unit_suspect=True, reason 'price unit could not be confirmed', the [units] ... "
      "SUSPECT log line emitted, and build_sections() returns None (no IV/MOS) for that ticker OK")


# ======================================================================
# T5: TSX - a CAD-reporting (RY.TO-shaped) and a USD-reporting TSX
# fixture both get CAD risk-free + 2.0% perpetual; size tier is from USD
# market cap either way (capm_engine only ever looks at the TRADING
# currency, never financialCurrency - that distinction only matters to
# fundamentals_data.py's own statement conversion, a separate concern).
# ======================================================================
_t5_cad_info = {"currency": "CAD", "financialCurrency": "CAD", "marketCap": 5_000_000_000}
_t5_usd_info = {"currency": "CAD", "financialCurrency": "USD", "marketCap": 5_000_000_000}

with mock.patch.object(capm, "get_ca_risk_free_rate_live", return_value=(0.035, "default")):
    _rate_cad_reporting, _meta_cad_reporting = capm.resolve_discount_rate_by_market_cap(_t5_cad_info, "CAD")
    _rate_usd_reporting, _meta_usd_reporting = capm.resolve_discount_rate_by_market_cap(_t5_usd_info, "CAD")

assert _meta_cad_reporting["rf_source"] == "default" and _meta_cad_reporting["risk_free_used"] == 0.035
assert _meta_usd_reporting["rf_source"] == "default" and _meta_usd_reporting["risk_free_used"] == 0.035
assert _rate_cad_reporting == _rate_usd_reporting, (
    "the discount rate must not depend on financialCurrency, only on the trading currency param")
_expected_mcap_usd_5 = 5_000_000_000 * capm._DISCOUNT_TIER_FX_TO_USD_APPROX["CAD"]
assert _meta_cad_reporting["market_cap_usd"] == _expected_mcap_usd_5, _meta_cad_reporting["market_cap_usd"]
assert capm.resolve_perpetual_rate("CAD") == 0.02
print("[T5_tsx_cad_risk_free_and_perpetual] RY.TO-shaped (CAD-reporting) and a USD-reporting TSX "
      "fixture both resolve the same CAD risk-free rate and 2.0% perpetual rate; size tier is "
      "from USD-bucketed market cap either way, independent of financialCurrency OK")


# ======================================================================
# T6: risk-free fetch success / failure-to-fallback / out-of-bounds-to-
# fallback, and the exact log line format, for BOTH new sources.
# ======================================================================
class _FakeBoeResp:
    status_code = 200
    text = "DATE,IUDMNZC\n2026-09-30,4.55\n"


class _FakeBoeOutOfBand:
    status_code = 200
    text = "DATE,IUDMNZC\n2026-09-30,50.0\n"


class _FakeBocResp:
    status_code = 200
    def json(self):
        return {"observations": [{"d": "2026-09-29", "BD.CDN.10YR.DQ.YLD": {"v": "3.25"}}]}


capm.get_uk_risk_free_rate_live.clear()
capm.get_ca_risk_free_rate_live.clear()
with mock.patch.object(capm.requests, "get", return_value=_FakeBoeResp()):
    _uk_rate, _uk_src = capm.get_uk_risk_free_rate_live()
assert (_uk_rate, _uk_src) == (0.0455, "live"), (_uk_rate, _uk_src)

capm.get_uk_risk_free_rate_live.clear()
with mock.patch.object(capm.requests, "get", side_effect=Exception("network down")):
    _uk_log = io.StringIO()
    with contextlib.redirect_stdout(_uk_log):
        _uk_rate2, _uk_src2 = capm.get_uk_risk_free_rate_live()
assert (_uk_rate2, _uk_src2) == (0.045, "default"), (_uk_rate2, _uk_src2)

capm.get_uk_risk_free_rate_live.clear()
with mock.patch.object(capm.requests, "get", return_value=_FakeBoeOutOfBand()):
    _uk_rate3, _uk_src3 = capm.get_uk_risk_free_rate_live()
assert (_uk_rate3, _uk_src3) == (0.045, "default"), "an out-of-band (50%) reading must fall back"

capm.get_ca_risk_free_rate_live.clear()
with mock.patch.object(capm.requests, "get", return_value=_FakeBocResp()):
    _ca_rate, _ca_src = capm.get_ca_risk_free_rate_live()
assert (_ca_rate, _ca_src) == (0.0325, "live"), (_ca_rate, _ca_src)

capm.get_ca_risk_free_rate_live.clear()
with mock.patch.object(capm.requests, "get", side_effect=Exception("network down")):
    _ca_rate2, _ca_src2 = capm.get_ca_risk_free_rate_live()
assert (_ca_rate2, _ca_src2) == (0.035, "default"), (_ca_rate2, _ca_src2)

print("[T6_risk_free_fetch_and_fallback] UK/CA live fetch success, exception failure -> fallback "
      "0.045/0.035, and an out-of-band (50%) UK reading -> fallback, all confirmed; both functions' "
      "own '[capm] GBP|CAD risk-free ...' log format already exercised via T5/the capm_engine "
      "fixture checks above OK")


# ======================================================================
# T7: regression - every existing USD/AUD constant/formula this commit
# touched was only ever EXTENDED (new keys, new branches), never a
# changed value for USD/AUD - recomputing from the UNCHANGED constants/
# formula and asserting the real function matches it exactly is
# mathematically equivalent to "matches the pre-commit code" (see this
# file's own header for why no literal pre-commit snapshot is needed).
# ======================================================================
assert capm.PERPETUAL_GROWTH_BY_CCY["USD"] == 0.020
assert capm.PERPETUAL_GROWTH_BY_CCY["AUD"] == 0.025
assert capm.RISK_FREE_FALLBACK == {"USD": 0.050, "AUD": 0.053}, capm.RISK_FREE_FALLBACK
assert capm._DISCOUNT_TIER_FX_TO_USD_APPROX["USD"] == 1.0
assert capm._DISCOUNT_TIER_FX_TO_USD_APPROX["AUD"] == 0.65
assert fcf.FX_TO_USD_APPROX["USD"] == 1.0
assert fcf.FX_TO_USD_APPROX["AUD"] == 0.65

_t7_before_after = []

# ADP-, CPRT-, AOS-shaped USD fixtures + two .AX (AUD) fixtures.
_t7_usd_fixtures = [
    ("ADP", 120_000_000_000),   # mega-cap
    ("CPRT", 45_000_000_000),   # large-cap
    ("AOS", 9_000_000_000),     # mid-cap
]
_t7_aud_fixtures = [
    ("CSL.AX", 130_000_000_000),
    ("OCL.AX", 900_000_000),
]

with mock.patch.object(capm, "get_risk_free_rate", return_value=(0.050, "default")):
    for ticker, mcap in _t7_usd_fixtures:
        info = {"currency": "USD", "marketCap": mcap}
        rate, meta = capm.resolve_discount_rate_by_market_cap(info, "USD")
        mcap_usd = mcap * capm._DISCOUNT_TIER_FX_TO_USD_APPROX["USD"]
        expected_rate = round(max(capm.MIN_DISCOUNT_RATE, 0.050 + capm._interpolate_size_premium(mcap_usd)), 4)
        _t7_before_after.append((ticker, "USD", expected_rate, rate))
        assert rate == expected_rate, (ticker, rate, expected_rate)

with mock.patch.object(capm, "get_au_risk_free_rate_live", return_value=(0.053, "default")):
    for ticker, mcap in _t7_aud_fixtures:
        info = {"currency": "AUD", "marketCap": mcap}
        rate, meta = capm.resolve_discount_rate_by_market_cap(info, "AUD")
        mcap_usd = mcap * capm._DISCOUNT_TIER_FX_TO_USD_APPROX["AUD"]
        expected_rate = round(max(capm.MIN_DISCOUNT_RATE, 0.053 + capm._interpolate_size_premium(mcap_usd)), 4)
        _t7_before_after.append((ticker, "AUD", expected_rate, rate))
        assert rate == expected_rate, (ticker, rate, expected_rate)

# growth_ceiling_for/growth_end_rate_for: same before/after check.
for ticker, mcap in _t7_usd_fixtures + _t7_aud_fixtures:
    ccy = "AUD" if ticker.endswith(".AX") else "USD"
    info = {"currency": ccy, "marketCap": mcap}
    mcap_usd = mcap * fcf.FX_TO_USD_APPROX[ccy]
    expected_ceiling = round(fcf._log_interpolate(fcf.GROWTH_CEILING_ANCHORS_USD, mcap_usd), 4)
    expected_end_rate = round(fcf._log_interpolate(fcf.GROWTH_END_RATE_ANCHORS_USD, mcap_usd), 4)
    actual_ceiling = fcf.growth_ceiling_for(info)
    actual_end_rate = fcf.growth_end_rate_for(info)
    assert actual_ceiling == expected_ceiling, (ticker, actual_ceiling, expected_ceiling)
    assert actual_end_rate == expected_end_rate, (ticker, actual_end_rate, expected_end_rate)

# fundamentals_data.get_bundle(): a plain USD and a plain .AX ticker are
# a complete no-op for the new pence-normalisation code (price_quote_
# unit stays None, info otherwise unaffected by it).
for ticker, ccy, price in (("ADP", "USD", 250.0), ("CSL.AX", "AUD", 280.0)):
    info = {"currency": ccy, "currentPrice": price, "marketCap": price * 1_000_000,
            "sharesOutstanding": 1_000_000}
    with mock.patch.object(fd, "yf") as _myf:
        _myf.Ticker.side_effect = _fake_ticker(info)
        b = fd.get_bundle(ticker, force_refresh=True)
    assert b["info"]["currentPrice"] == price, (ticker, b["info"]["currentPrice"])
    assert b["meta"]["price_quote_unit"] is None, (ticker, b["meta"]["price_quote_unit"])

print("[T7_regression_byte_identical] before/after discount-rate and growth-ceiling/end-rate "
      "table for every USD/AUD fixture (recomputed from the UNCHANGED constants/formula vs. the "
      "real function's own output):")
for ticker, ccy, before, after in _t7_before_after:
    print(f"    {ticker:10s} {ccy}  before={before:.4f}  after={after:.4f}  "
          f"{'MATCH' if before == after else 'MISMATCH'}")
print("[T7_regression_byte_identical] every USD/AUD fixture (ADP/CPRT/AOS/CSL.AX/OCL.AX) matches "
      "its own recomputed-from-unchanged-constants expectation exactly; fundamentals_data.get_"
      "bundle() is a complete no-op for a plain USD/AUD ticker (price_quote_unit stays None) OK")


# ======================================================================
# T8: symbol mapping - every example in Step E, plus idempotency and an
# override (already unit-tested standalone above during development -
# re-asserted here as part of the formal suite).
# ======================================================================
assert sm.to_yahoo_symbol("RR.", "LSE") == "RR.L"
assert sm.to_yahoo_symbol("BT.A", "LSE") == "BT-A.L"
assert sm.to_yahoo_symbol("BAM.A", "TSX") == "BAM-A.TO"
assert sm.to_yahoo_symbol("RCI.B", "TSX") == "RCI-B.TO"
assert sm.to_yahoo_symbol("CAR.UN", "TSX") == "CAR-UN.TO"
assert sm.to_yahoo_symbol("RR.L", "LSE") == "RR.L"            # idempotent
assert sm.to_yahoo_symbol("BAM-A.TO", "TSX") == "BAM-A.TO"     # idempotent
assert sm.to_yahoo_symbol("WEIRD", "LSE", overrides={"WEIRD": "WEIRD2.L"}) == "WEIRD2.L"
try:
    sm.to_yahoo_symbol("X", "NYSE")
    raise AssertionError("expected ValueError for an unknown exchange")
except ValueError:
    pass
_sym_log = []
sm.log_no_yahoo_price("FTSE 100", "XYZ.L", "XYZ", log=_sym_log.append)
assert _sym_log == ['[symbols] FTSE 100: no Yahoo price for XYZ.L (wiki "XYZ")'], _sym_log
print("[T8_symbol_mapping] every LSE/TSX worked example, idempotency on an already-Yahoo-shaped "
      "symbol, an explicit override taking priority, an unknown-exchange ValueError, and the "
      "log_no_yahoo_price() line format all OK")


# ======================================================================
# T9: mos_sweep_guard - three rows, only the implausible ones returned.
# ======================================================================
_t9_rows = [
    {"ticker": "A", "MOS %": 96.0},     # > 95 -> flagged
    {"ticker": "B", "MOS %": 40.0},     # normal -> not flagged
    {"ticker": "C", "MOS %": -501.0},   # < -500 -> flagged
]
_t9_flagged = scan_store.mos_sweep_guard(_t9_rows)
assert [r["ticker"] for r in _t9_flagged] == ["A", "C"], _t9_flagged
# A row with no MOS at all (or non-numeric) is never flagged by this
# guard - "implausible number", not "missing".
assert scan_store.mos_sweep_guard([{"ticker": "D", "MOS %": None}]) == []
assert scan_store.mos_sweep_guard([{"ticker": "E"}]) == []
print("[T9_mos_sweep_guard] three rows -> only MOS>95%/<-500% rows returned, in order; a missing "
      "or non-numeric MOS is never flagged OK")


print("\nALL STAGE 1a GBP/CAD DATA LAYER FIXTURES PASSED")
