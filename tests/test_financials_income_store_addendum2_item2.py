"""
Addendum 2 item 2 (Director, 5 Oct 2026) of instruction_financials_
income_store_and_top200_guard.md:

"One classifier: route auto_compounder_engine._ac_is_financials()
through financials_classifier so the Compounder View, Scanner and
Deep Dive agree for an overridden ticker (V-shaped fixture), switch ON
and OFF."

Before this fix, auto_compounder_engine._ac_is_financials(info) was a
hand-duplicated copy of the sector/industry rule with no `ticker`
param at all - it could never consult financials_classifier's
switch-gated override table, so with the switch ON, the Compounder
View kept calling an overridden ticker like V (Visa) financials-mode
while the Scanner (fcf_valuation_engine -> financials_classifier.
is_financials()) and Deep Dive (same path via resolver_engine) called
it standard-mode. This test proves all three now agree, in both
switch states, for the V-shaped fixture.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import auto_compounder_engine
import financials_classifier
import moat_engine


def _switch(on):
    if on:
        os.environ["FINANCIALS_STORE_LIVE"] = "1"
    else:
        os.environ.pop("FINANCIALS_STORE_LIVE", None)


# V-shaped fixture: Visa itself is in financials_classifier._OVERRIDE_
# OUT_TICKERS - Yahoo files it under "Financial Services" sector (so
# the pre-override rule says financials-mode), but Fable's principle
# (asset-light fee earner, no customer money through operating cash
# flow) overrides it to standard mode, switch ON only.
V_INFO = {
    "sector": "Financial Services",
    "industry": "Credit Services",
    "currency": "USD",
    "financialCurrency": "USD",
}


def _run():
    assert "V".strip().upper() in financials_classifier._OVERRIDE_OUT_TICKERS

    try:
        # ---- Switch OFF: override table never consulted, all three
        # consumers fall through to the pre-existing sector/industry
        # rule and agree (sector == "Financial Services" -> True) -
        # "change nothing" still holds for the switch-OFF case.
        _switch(False)
        compounder_off = auto_compounder_engine._ac_is_financials(V_INFO, ticker="V")
        scanner_off = financials_classifier.is_financials(V_INFO, ticker="V")
        deep_dive_off = moat_engine._is_financials(V_INFO, ticker="V")
        assert compounder_off is True, compounder_off
        assert compounder_off == scanner_off == deep_dive_off, (
            compounder_off, scanner_off, deep_dive_off,
        )
        print(f"[item2_switch_off_agree] V: Compounder={compounder_off} "
              f"Scanner={scanner_off} DeepDive={deep_dive_off} - all agree OK")

        # ---- Switch ON: override table fires, V flips to standard
        # mode (False) for the Scanner and Deep Dive. Before this fix,
        # the Compounder View's own hand-duplicated copy had no ticker
        # param and could never see the override table, so it would
        # have stayed True here, disagreeing with the other two - the
        # exact drift this item fixes.
        _switch(True)
        compounder_on = auto_compounder_engine._ac_is_financials(V_INFO, ticker="V")
        scanner_on = financials_classifier.is_financials(V_INFO, ticker="V")
        deep_dive_on = moat_engine._is_financials(V_INFO, ticker="V")
        assert scanner_on is False, scanner_on
        assert compounder_on == scanner_on == deep_dive_on, (
            compounder_on, scanner_on, deep_dive_on,
        )
        print(f"[item2_switch_on_agree] V: Compounder={compounder_on} "
              f"Scanner={scanner_on} DeepDive={deep_dive_on} - all agree OK "
              f"(flipped to standard mode by the override table)")

        # ---- No-ticker call sites (defensive): both switch states,
        # with no ticker, every consumer falls straight through to the
        # old rule unchanged - still agree.
        _switch(True)
        compounder_noticker = auto_compounder_engine._ac_is_financials(V_INFO)
        scanner_noticker = financials_classifier.is_financials(V_INFO)
        assert compounder_noticker == scanner_noticker == True, (
            compounder_noticker, scanner_noticker,
        )
        print("[item2_no_ticker_falls_through] no ticker given, switch ON -> "
              "both still agree via the unchanged sector/industry rule OK")
    finally:
        os.environ.pop("FINANCIALS_STORE_LIVE", None)

    print("\nALL ADDENDUM 2 ITEM 2 (ONE CLASSIFIER) CHECKS PASSED")


if __name__ == "__main__":
    _run()
