"""
Commit 3 of instruction_financials_income_store_and_top200_guard.md
(4 Oct 2026, Director-directed) - classifier overrides, behind the
FINANCIALS_STORE_LIVE switch. Fable's principle: does customer,
policyholder or clearing money flow through operating cash flow? If
yes, financials mode; if the business is an asset-light fee earner,
operating cash flow is close to owner earnings and standard mode is
better.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. No test here makes a live network call.

Run: python3 tests/test_financials_classifier_overrides_commit3.py
"""
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="financials_classifier_overrides_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("FINANCIALS_STORE_LIVE", None)

import financials_classifier as fc
import fcf_valuation_engine as fve
import moat_engine
import pandas as pd


def _switch(on):
    if on:
        os.environ["FINANCIALS_STORE_LIVE"] = "1"
    else:
        os.environ.pop("FINANCIALS_STORE_LIVE", None)


# ======================================================================
# C1: the switch itself.
# ======================================================================
_switch(False)
assert fc.is_financials_store_live() is False
for val in ("0", "off", "OFF", "", "no"):
    os.environ["FINANCIALS_STORE_LIVE"] = val
    assert fc.is_financials_store_live() is False, val
for val in ("1", "on", "ON", "On"):
    os.environ["FINANCIALS_STORE_LIVE"] = val
    assert fc.is_financials_store_live() is True, val
os.environ.pop("FINANCIALS_STORE_LIVE", None)
print("[C1_switch] unset/0/off/no -> False; 1/on (any case) -> True OK")


# ======================================================================
# C2: switch OFF returns EXACTLY today's answer for every fixture,
# ticker given or not - the override table never applies.
# ======================================================================
_switch(False)
BANK_INFO = {"sector": "Financial Services", "industry": "Banks"}
VISA_INFO = {"sector": "Financial Services", "industry": "Credit Services"}
CME_INFO = {"sector": "Financial Services", "industry": "Financial Data & Stock Exchanges"}
REIT_INFO = {"sector": "Financial Services", "industry": "REIT - Diversified"}
TECH_INFO = {"sector": "Technology", "industry": "Software"}

for info, ticker, expected in [
    (BANK_INFO, None, True), (BANK_INFO, "COF", True),
    (VISA_INFO, "V", True),      # switch OFF: sector alone decides, override never consulted
    (CME_INFO, "CME", True),     # switch OFF: sector alone decides
    (REIT_INFO, "O", False),     # REIT exclusion, unconditional
    (TECH_INFO, "AAPL", False),
]:
    assert fc.is_financials(info, ticker=ticker) is expected, (info, ticker, expected)
print("[C2_switch_off_unchanged] switch OFF: V/CME classify purely by sector (True, override "
      "never consulted), REIT exclusion still fires, every other fixture unchanged OK")


# ======================================================================
# C3: switch ON - the override table, in order.
# ======================================================================
_switch(True)

# Ticker override OUT -> standard mode, even though sector says Financial Services.
for t in ("V", "MA", "PYPL", "SPGI", "MCO", "MSCI", "FICO", "CPU.AX"):
    assert fc.is_financials(VISA_INFO, ticker=t) is False, t
print("[C3_override_out] V/MA/PYPL/SPGI/MCO/MSCI/FICO/CPU.AX -> standard mode (False) with the "
      "switch ON, regardless of sector OK")

# Ticker override IN -> financials mode, even under the exchanges industry string.
for t in ("CME", "ICE", "NDAQ", "CBOE", "ASX.AX"):
    assert fc.is_financials(CME_INFO, ticker=t) is True, t
print("[C3_override_in] CME/ICE/NDAQ/CBOE/ASX.AX -> financials mode (True) with the switch ON, "
      "even under the 'Financial Data & Stock Exchanges' industry OK")

# The bare industry rule -> standard, for a ticker NOT in the IN table.
assert fc.is_financials(CME_INFO, ticker="SPGI_INDICES_FAKE") is False
print("[C3_industry_rule] 'Financial Data & Stock Exchanges' -> standard mode (False) for a "
      "ticker not in the IN table OK")

# REIT exclusion still checked FIRST, before the override table.
REIT_IN_TABLE_CONFLICT = {"sector": "Financial Services", "industry": "REIT - Diversified"}
assert fc.is_financials(REIT_IN_TABLE_CONFLICT, ticker="CME") is False, \
    "REIT exclusion must win even for a ticker that's also in the override IN table"
print("[C3_reit_wins_first] the REIT/real-estate exclusion fires before the override table is "
      "even consulted OK")

# No ticker given, switch ON -> falls straight to the existing rule (rule 5), override skipped.
assert fc.is_financials(VISA_INFO, ticker=None) is True   # sector alone -> True, override never reached
assert fc.is_financials(VISA_INFO, ticker="") is True
print("[C3_no_ticker_skips_override] switch ON but no ticker given -> override table skipped "
      "entirely, falls through to the existing sector/industry rule OK")

# Otherwise, the existing rule, unchanged - banks/insurance/credit-services-with-funded-
# receivables/asset managers/capital markets/mortgage finance/conglomerates stay financials.
assert fc.is_financials(BANK_INFO, ticker="COF") is True
assert fc.is_financials(TECH_INFO, ticker="AAPL") is False
print("[C3_existing_rule_unchanged] a bank (COF) stays financials, a tech company (AAPL) stays "
      "standard, with the switch ON and no override match OK")

_switch(False)


# ======================================================================
# C4: moat_engine and fcf_valuation_engine agree for every fixture in
# BOTH switch states - the whole point of sharing one classifier.
# ======================================================================
def _agree(info, ticker, switch_on):
    _switch(switch_on)
    a = moat_engine._is_financials(info, ticker=ticker)
    b = fve.financials_classifier.is_financials(info, ticker=ticker)
    _switch(False)
    return a, b


for info, ticker in [
    (BANK_INFO, "COF"), (VISA_INFO, "V"), (CME_INFO, "CME"), (REIT_INFO, "O"),
    (TECH_INFO, "AAPL"), (VISA_INFO, "SPGI"),
]:
    for switch_on in (False, True):
        a, b = _agree(info, ticker, switch_on)
        assert a == b, (info, ticker, switch_on, a, b)
print("[C4_moat_fve_agree] moat_engine._is_financials() and fcf_valuation_engine's own "
      "financials_classifier.is_financials() agree for every fixture, switch OFF and ON OK")


# ======================================================================
# C5: the override changes BOTH the moat mode AND the valuation base
# for the same ticker - normalized_base_and_series() dispatches on the
# identical ticker-aware decision moat_engine._compute_moat_from_bundle()
# would make.
# ======================================================================
VISA_NI = pd.DataFrame(
    {"2025-12-31": [5000.0], "2024-12-31": [4800.0], "2023-12-31": [4500.0]},
    index=["Net Income Common Stockholders"],
)
VISA_CF = pd.DataFrame(
    {"2025-12-31": [6000.0, -500.0], "2024-12-31": [5800.0, -480.0], "2023-12-31": [5500.0, -450.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)

_switch(False)
_base_off, _, _src_off, *_ = fve.normalized_base_and_series(VISA_CF, info=VISA_INFO, income_df=VISA_NI, ticker="V")
assert _src_off == "net_income_financials", _src_off  # switch OFF: V still financials by sector

_switch(True)
_base_on, _, _src_on, *_ = fve.normalized_base_and_series(VISA_CF, info=VISA_INFO, income_df=VISA_NI, ticker="V")
assert _src_on == "ocf_fallback_financials" or not _src_on.startswith("net_income"), _src_on
_switch(False)
print(f"[C5_override_changes_valuation_base] V: source={_src_off!r} (switch OFF, sector-driven "
      f"financials mode) vs {_src_on!r} (switch ON, override OUT to standard mode) - the "
      "override genuinely changes the valuation path, not just a label OK")


print("\nALL FINANCIALS CLASSIFIER OVERRIDES COMMIT 3 CHECKS PASSED")
