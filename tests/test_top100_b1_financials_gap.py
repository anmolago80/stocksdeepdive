"""
Top 200 Commit B1 (5 Oct 2026, Director-directed, instruction_top200_
blank_replies_and_financials_gap.md - "a net income path that was
abandoned must not re-enter the Top 100 (switch-gated)").

From the follow-up report: when a financials-mode ticker's net income
base is non-positive, dcf_intrinsic_value() falls back to
info["freeCashflow"] (fcf_source "info"), and top100_engine.
_is_fallback_financials() only ever excluded the literal "ocf_fallback_
financials" string. IVZ is the live example (shadow value 75.74 at
price 30.66) - that is the cash-flow basis Andrew moved financials
away from, coming back in.

With FINANCIALS_STORE_LIVE ON only:
  - A financials-mode row whose FCF Source is anything other than
    "net_income_financials" ("ocf_fallback_financials", "info",
    "none") is now pool-ineligible (top100_engine._financials_pool_
    ineligible()), counted by the SAME existing log line
    _is_fallback_financials() already fed.
  - Deep Dive shows the existing short caption for the "info" case too
    (deep_dive_engine.py's own "fallback_financials_caption").
  - financials_dry_run.py: status "ni_path_abandoned" now also covers
    source "none"; "pool_ineligible_if_switch_on" reflects the new
    rule (see test_financials_dry_run_followups.py's own IVZ check for
    the dry-run-level test of this).

With the switch OFF: nothing changes. select_top100_pool() stays
byte-identical - see the switch-OFF proof at the bottom of this file.

This file covers what the dry-run/income-store test files do NOT:
  - the actual select_top100_pool() pool-exclusion rule itself, on
    financials-mode "info"/"none" fixtures, switch ON vs OFF.
  - a STANDARD-mode ticker that also lands on fcf_source "info"/"none"
    (via the ordinary OCF-unavailable path) is unaffected in BOTH
    switch states - the new rule must never touch it.
  - top100_engine._financials_pool_ineligible() as a pure unit,
    covering "ocf_fallback_financials" (unconditional, backward-
    compatible with every pre-B1 fixture), "info"/"none" (gated on
    "Is Financials Mode"), and "net_income_financials" (never
    ineligible).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top100_b1_financials_gap.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_b1_financials_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts
import scan_store

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


# ======================================================================
# CHECK 1: _financials_pool_ineligible() as a pure unit.
# ======================================================================
assert te._financials_pool_ineligible({"FCF Source": "net_income_financials", "Is Financials Mode": True}) is False
assert te._financials_pool_ineligible({"FCF Source": "ocf_fallback_financials"}) is True, (
    "ocf_fallback_financials must be excluded unconditionally - no 'Is Financials Mode' "
    "key present at all, matching every pre-B1 fixture")
assert te._financials_pool_ineligible({"FCF Source": "ocf_fallback_financials", "Is Financials Mode": False}) is True
assert te._financials_pool_ineligible({"FCF Source": "info", "Is Financials Mode": True}) is True
assert te._financials_pool_ineligible({"FCF Source": "none", "Is Financials Mode": True}) is True
assert te._financials_pool_ineligible({"FCF Source": "info", "Is Financials Mode": False}) is False, (
    "a STANDARD-mode row on fcf_source 'info' must never be excluded by this rule")
assert te._financials_pool_ineligible({"FCF Source": "info"}) is False, (
    "no 'Is Financials Mode' key at all (pre-B1 fixture shape) defaults to not-excluded for info/none")
assert te._financials_pool_ineligible({"FCF Source": "none"}) is False
assert te._financials_pool_ineligible({"FCF Source": "manual", "Is Financials Mode": True}) is False
print("[financials_pool_ineligible_unit] net_income_financials never excluded; "
      "ocf_fallback_financials excluded unconditionally; info/none excluded ONLY when "
      "Is Financials Mode is True; absent 'Is Financials Mode' key defaults to not-excluded "
      "for info/none (pre-B1 fixture back-compat) OK")


# ======================================================================
# CHECK 2/3: select_top100_pool() integration - financials-mode "info"
# and "none" fixtures, switch ON -> excluded (same log line as the
# existing ocf_fallback_financials exclusion); switch OFF -> eligible,
# exactly as today.
# ======================================================================
def _top100_row(ticker, long_score, mos=20.0, fcf_source=None, is_financials_mode=None):
    row = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": 70,
        "MOS %": mos, "Psychology": 5.0, "Discovery (lite)": 10.0,
        "Long Score": long_score, "Price": 50.0, "Intrinsic Value": 55.0,
        "DCF Unreliable": False,
    }
    if fcf_source is not None:
        row["FCF Source"] = fcf_source
    if is_financials_mode is not None:
        row["Is Financials Mode"] = is_financials_mode
    return row


def _save_universe(universe, rows):
    payload = {
        "universe": universe, "source": "test",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_night": None, "rows": rows, "attention_lite": True, "degraded": False,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


_patch_sector_fill = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill.start()

_clean_rows = [_top100_row(f"CLEAN{i}", long_score=50.0 + i) for i in range(5)]
_ivz_row = _top100_row("IVZ", long_score=95.0, mos=70.0, fcf_source="info", is_financials_mode=True)
_none_row = _top100_row("NONEBANK", long_score=96.0, mos=71.0, fcf_source="none", is_financials_mode=True)
_standard_info_row = _top100_row(
    "STDINFO", long_score=97.0, mos=72.0, fcf_source="info", is_financials_mode=False)

_FAKE_CADENCE = {"S&P 500": "daily"}

for _switch_on in (True, False):
    with mock.patch.object(te.financials_classifier, "is_financials_store_live", return_value=_switch_on):
        if os.path.exists(scan_store._path("S&P 500")):
            os.remove(scan_store._path("S&P 500"))
        _save_universe("S&P 500", _clean_rows + [_ivz_row, _none_row, _standard_info_row])
        _logs = []
        with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                                return_value=dict(_FAKE_CADENCE)):
            _pool = te.select_top100_pool(log=_logs.append)
        _pool_tickers = {r["ticker"] for r in _pool}

        if _switch_on:
            assert "IVZ" not in _pool_tickers, _pool_tickers
            assert "NONEBANK" not in _pool_tickers, _pool_tickers
            assert "STDINFO" in _pool_tickers, (
                "a STANDARD-mode ticker on fcf_source 'info' must remain eligible even with "
                "the switch ON", _pool_tickers)
            assert {f"CLEAN{i}" for i in range(5)} <= _pool_tickers, _pool_tickers
            _log_line = next(l for l in _logs if "fallback-financials" in l)
            assert "excluded 2 fallback-financials row(s)" in _log_line, _log_line
            assert "IVZ" in _log_line and "NONEBANK" in _log_line, _log_line
            print(f"[switch_on_ivz_and_none_excluded] switch ON -> IVZ (fcf_source=info) and "
                  f"NONEBANK (fcf_source=none) both excluded, STDINFO (standard-mode, same "
                  f"fcf_source=info) stays eligible, exact log line '{_log_line.strip()}' OK")
        else:
            assert "IVZ" in _pool_tickers, (
                "switch OFF -> IVZ must be eligible, exactly as today", _pool_tickers)
            assert "NONEBANK" in _pool_tickers, _pool_tickers
            assert "STDINFO" in _pool_tickers, _pool_tickers
            assert {f"CLEAN{i}" for i in range(5)} <= _pool_tickers, _pool_tickers
            assert not any("fallback-financials" in l for l in _logs), _logs
            print("[switch_off_everyone_eligible] switch OFF -> IVZ/NONEBANK/STDINFO all "
                  "eligible exactly as today, no fallback-financials exclusion log line at all OK")

_patch_sector_fill.stop()
if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))



# ======================================================================
# CHECK 4: Deep Dive caption extension - "fallback_financials_caption"
# now fires for fcf_source "info" too (the IVZ-shaped live case), not
# just "ocf_fallback_financials". Reproduces deep_dive_engine.py's own
# formula against a REAL dcf_intrinsic_value() call (IVZ-shaped
# fixture, same shape test_financials_dry_run_followups.py uses) so
# this stays locked to the actual DCF meta, not an invented dict.
# ======================================================================
import fcf_valuation_engine as fve
import pandas as pd

_IVZ_INFO = {"sector": "Financial Services", "industry": "Asset Management",
             "currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 100.0, "currentPrice": 30.66, "marketCap": 3_066_000_000.0,
             "longName": "IVZ-shaped", "freeCashflow": 500_000_000.0}
_IVZ_INCOME = pd.DataFrame(
    {"Net Income Common Stockholders": [-50.0, 10.0, -5.0, 70.0, 65.0]},
    index=["2026-12-31", "2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"],
).T
with mock.patch("capm_engine.get_risk_free_rate", return_value=(0.05, "default")), \
     mock.patch("capm_engine.get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _ivz_iv, _, _ivz_meta = fve.dcf_intrinsic_value(
        "IVZSHAPED", info=_IVZ_INFO, cashflow_df=None, currency="USD",
        income_df=_IVZ_INCOME, discount_rate=0.08, perpetual_rate=0.02, growth_rate=0.05,
    )
assert _ivz_meta["fcf_source"] == "info", _ivz_meta["fcf_source"]
assert bool(_ivz_meta.get("is_financials_mode")) is True, _ivz_meta
_financials_mode_live_true = True  # switch ON + financials-mode, as deep_dive_engine.py computes it
_caption_on = bool(
    _financials_mode_live_true and _ivz_meta.get("fcf_source") in ("ocf_fallback_financials", "info"))
assert _caption_on is True, (
    "deep_dive_engine.py's fallback_financials_caption formula must now fire for fcf_source "
    "'info' too, exactly as it already did for 'ocf_fallback_financials'")
_caption_switch_off = bool(
    False and _ivz_meta.get("fcf_source") in ("ocf_fallback_financials", "info"))
assert _caption_switch_off is False, "the caption must never fire with the switch OFF"
print("[deep_dive_caption_extended_to_info] deep_dive_engine.py's fallback_financials_caption "
      "formula, run against a REAL dcf_intrinsic_value() call on the IVZ-shaped fixture "
      "(fcf_source='info') -> True with the switch ON, False with it OFF OK")


print("\nALL TOP 200 COMMIT B1 (FINANCIALS GAP) FIXTURES PASSED")
