"""
SECTION B, COMMIT B4 of instruction_top200_amendments_and_currency_view.md
(5 Oct 2026, Director-directed): the Portfolio currency-exposure table
beside the stress test - app._render_currency_exposure_table().

Covers exactly the task's own listed tests:
  - a fixture portfolio in three currencies, with the USD row checked
    against 250,000 (home currency) held: the task's own worked
    -2,418 / -17,650 / +14,992 figures at the tool's 5 Oct rates
  - a home-currency-only portfolio (no foreign holdings at all)
  - a pair with no history ("not available", never an estimate, and
    excluded from the total/share denominator rather than silently
    dropped from the table)

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_b4_portfolio_currency_exposure.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="b4_portfolio_currency_exposure_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import account_currency_store as acs
import currency_risk_engine as cre
import currency_view_engine as cve

if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FAKE_HISTORY = {"dates": ["2016-01-01", "2026-10-05"], "closes": [0.70, 0.6964], "stale": False}
_FAKE_STATS = {
    "today": 0.6964, "average": 0.7032, "sigma": 0.0461,
    "pct_vs_average": -1.0, "range_min": 0.6, "range_max": 0.8, "percentile": 50.0,
    "annualised_vol_pct": 5.0,
}

# USD holding: 1741 shares @ 100.00 USD = 174,100 USD native ->
# 174,100 / 0.6964 = 250,000.00 AUD exactly - the task's own "250,000
# held" fixture.
_HOLDINGS_3CCY = [
    {"portfolio": "Main", "ticker": "AAPL", "currency": "USD", "shares": 1741.0},
    {"portfolio": "Main", "ticker": "BHP.AX", "currency": "AUD", "shares": 1000.0},
    {"portfolio": "Main", "ticker": "SAP.DE", "currency": "EUR", "shares": 500.0},
]
_ANALYSES_3CCY = {
    ("Main", "AAPL"): {"snapshot": {"price": 100.0}},
    ("Main", "BHP.AX"): {"snapshot": {"price": 50.0}},
    ("Main", "SAP.DE"): {"snapshot": {"price": 80.0}},
}

_HOLDINGS_HOME_ONLY = [
    {"portfolio": "Main", "ticker": "BHP.AX", "currency": "AUD", "shares": 1000.0},
]
_ANALYSES_HOME_ONLY = {("Main", "BHP.AX"): {"snapshot": {"price": 50.0}}}


def _run(email, holdings, analyses, extra_env=None, mock_eur=False):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
import app
app._render_currency_exposure_table({holdings!r}, {analyses!r}, "en")
"""
    at = AppTest.from_string(script, default_timeout=60)
    for k, v in (extra_env or {}).items():
        os.environ[k] = v

    def _fake_fx_history(base, quote):
        if mock_eur or quote != "EUR":
            return _FAKE_HISTORY
        return None  # EUR pair genuinely has no cached history in this fixture

    _patches = [
        mock.patch.object(cre, "get_fx_history", side_effect=_fake_fx_history),
        mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS),
    ]
    for p in _patches:
        p.start()
    try:
        at.run()
    finally:
        for p in _patches:
            p.stop()
        for k in (extra_env or {}):
            os.environ.pop(k, None)
    assert not at.exception, f"_render_currency_exposure_table() raised: {at.exception}"
    return at


def _dataframe_rows(at):
    for df in at.dataframe:
        try:
            return df.value.to_dict("records")
        except Exception:
            return df.value
    return []


_OWNER = "anmolago@hotmail.com"
os.environ.pop("CURRENCY_VIEW_LIVE", None)

# ======================================================================
# CHECK 1: switch OFF, non-owner -> nothing renders at all.
# ======================================================================
_at1 = _run("someone-else@example.com", _HOLDINGS_3CCY, _ANALYSES_3CCY)
check("switch OFF, non-owner: no dataframe, no caption/button of this feature's own",
      len(_at1.dataframe) == 0
      and not any("currency" in (c.value or "").lower() for c in _at1.caption))

# ======================================================================
# CHECK 2: switch OFF, owner, no home currency set -> the B2 prompt in
# place of the table.
# ======================================================================
_at2 = _run(_OWNER, _HOLDINGS_3CCY, _ANALYSES_3CCY)
check("owner, no home currency: the B2 'set your home currency' prompt shown, no table",
      len(_at2.dataframe) == 0
      and any("Set your home currency to see the currency view." in (c.value or "") for c in _at2.caption))
print("[b4_gates] switch-off/non-owner renders nothing; no-home-currency shows the B2 "
      "prompt in place of the table OK")

# ======================================================================
# CHECK 3: the main fixture - three currencies, owner, home AUD. USD
# row matches the task's own worked figures (-2,418/-17,650/+14,992)
# within the instruction's own rounding tolerance; EUR has no history
# -> "not available", excluded from totals; AUD (home) row has no
# rate/scenario columns; the foreign total sums only USD (EUR excluded).
# ======================================================================
acs.set_home_currency(_OWNER, "AUD")
_at3 = _run(_OWNER, _HOLDINGS_3CCY, _ANALYSES_3CCY)
_rows3 = _dataframe_rows(_at3)
check("exactly 4 rows: USD, AUD, EUR, and the foreign total", len(_rows3) == 4)
_by_currency = {r["Currency"]: r for r in _rows3 if r["Currency"] in ("USD", "AUD", "EUR")}

_usd_row = _by_currency["USD"]
check("USD row value is AUD 250,000 (the task's own '250,000 held' fixture)",
      _usd_row["Value"] == "AUD 250,000")
check("USD average-scenario effect within $5 of the task's own -2,418",
      abs(float(_usd_row["If the rate reverts to average"].replace("AUD ", "").replace(",", "")) - (-2418)) <= 5)
check("USD +1sigma effect within $10 of the task's own -17,650",
      abs(float(_usd_row["If the rate reaches +1σ"].replace("AUD ", "").replace(",", "")) - (-17650)) <= 10)
check("USD -1sigma effect within $50 of the task's own +14,992 (the fixture's own "
      "average+-sigma can't reproduce the instruction's own four worked rates to all "
      "4 decimals simultaneously - see test_b2_currency_view_calc.py's own note on "
      "this same rounding-noise property)",
      abs(float(_usd_row["If the rate falls to -1σ"].replace("AUD ", "").replace(",", "")) - (14992)) <= 50)

_aud_row = _by_currency["AUD"]
check("home-currency (AUD) row has no rate-vs-average figure (not applicable, not unavailable)",
      _aud_row["Rate vs. 10-year average"] == "—")
check("home-currency (AUD) row value is its own native value, AUD 50,000",
      _aud_row["Value"] == "AUD 50,000")

_eur_row = _by_currency["EUR"]
check("EUR (no cached history) row says 'not available', never a guessed value",
      _eur_row["Value"] == "not available")
check("EUR row's share of portfolio is also withheld, not a guessed 0%",
      _eur_row["Share of portfolio"] == "—")

_total_row = [r for r in _rows3 if r["Currency"] == "Total foreign holdings"][0]
check("the foreign total is USD alone (AUD 250,000) - EUR excluded, never estimated in",
      _total_row["Value"] == "AUD 250,000")
check("the foreign total's share of portfolio is 250,000 / 300,000 = 83.3%",
      _total_row["Share of portfolio"] == "83.3%")
print("[b4_three_currency_fixture] USD row reproduces the task's own worked figures; "
      "the home-currency row needs no conversion; EUR (no history) is withheld from "
      "both its own row and every total/share figure OK")

# ======================================================================
# CHECK 3b (Director's 6 Oct 2026 follow-up): the caption's exact
# wording - explains counting is by LISTING currency, not underlying-
# asset currency, for a fund listed in one country holding assets in
# another. No dollar sign anywhere in it.
# ======================================================================
_caption_texts3 = [c.value for c in _at3.caption]
_exposure_caption3 = [c for c in _caption_texts3 if "listed in one country" in c]
check("exactly one caption carries the new 'counted by listing currency' wording",
      len(_exposure_caption3) == 1)
check("that caption's exact text matches the Director's own wording",
      _exposure_caption3 and _exposure_caption3[0] == (
          "Counted by the currency each holding is priced in. Funds listed in one "
          "country that hold assets in another (for example an Australian-listed "
          "fund of US shares) are counted in their listing currency here, although "
          "their value also moves with the exchange rate."
      ))
check("no dollar sign anywhere in the caption", "$" not in _exposure_caption3[0])
print("[b4_caption_wording] the currency-exposure table's caption explains listing-"
      "currency counting in the Director's exact wording, with no dollar sign OK")

# ======================================================================
# CHECK 4: a home-currency-only portfolio - no foreign holdings at
# all -> no "Total foreign holdings" row (nothing to total).
# ======================================================================
acs.set_home_currency("homeonly@example.com", "AUD")
_at4 = _run("homeonly@example.com", _HOLDINGS_HOME_ONLY, _ANALYSES_HOME_ONLY,
             extra_env={"CURRENCY_VIEW_LIVE": "1"})
_rows4 = _dataframe_rows(_at4)
check("home-currency-only portfolio: exactly one row (AUD), no foreign total row",
      len(_rows4) == 1 and _rows4[0]["Currency"] == "AUD")
print("[b4_home_only_portfolio] a portfolio with no foreign holdings shows just its own "
      "currency, with no spurious foreign-total row OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)
sys.exit(1 if failed else 0)
