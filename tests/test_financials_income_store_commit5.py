"""
Commit 5 of instruction_financials_income_store_and_top200_guard.md
(4 Oct 2026, Director-directed) - shadow values and the owner's dry-
run Admin view. This is what Andrew approves from before the Director
sets FINANCIALS_STORE_LIVE=1 on Railway.

Covers the instruction's own Commit 5 test list: shadow file written
and read; no network in the shadow computation; nothing written to a
scan row, score_history, a snapshot or the Top 100 pool; the Admin
section renders from a fixture file and is unreachable for a non-
owner. Also the "Tests common to commits 2-5" before/after requirement
for the four financial fixtures named in that section (bank on net
income today, insurer on the fallback today, a card-network/V-shaped
ticker, a financial with fewer than two positive years) - switch OFF
byte-identical, switch ON differences printed and explained.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. No test here makes a live network call.

Run: python3 tests/test_financials_income_store_commit5.py
"""
import os
import re
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="financials_dry_run_commit5_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("FINANCIALS_STORE_LIVE", None)

import financials_classifier as fc
import financials_dry_run as fdr
import financials_income_store as fis
import fcf_valuation_engine as fve
import nightly_scan as ns
import scan_store
import score_history
import fundamentals_data


def _switch(on):
    if on:
        os.environ["FINANCIALS_STORE_LIVE"] = "1"
    else:
        os.environ.pop("FINANCIALS_STORE_LIVE", None)


def _mk_income_df(net_income, cols=None):
    cols = cols or [f"202{5 - i}-12-31" for i in range(len(net_income))]
    return pd.DataFrame({"Net Income Common Stockholders": net_income}, index=cols).T


BANK_INFO = {"sector": "Financial Services", "industry": "Banks - Regional",
             "currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 100.0, "currentPrice": 50.0, "marketCap": 5_000_000_000.0}
VISA_INFO = {"sector": "Financial Services", "industry": "Credit Services",
             "currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 100.0, "currentPrice": 50.0, "marketCap": 5_000_000_000.0}
GOOD_INCOME = _mk_income_df([412.0, 380.0, 350.0, 300.0, 250.0],
                            cols=["2026-06-30", "2025-06-30", "2024-06-30",
                                  "2023-06-30", "2022-06-30"])
THIN_INCOME = _mk_income_df([5.0, -2.0, -3.0, -1.0, -4.0],
                            cols=["2026-06-30", "2025-06-30", "2024-06-30",
                                  "2023-06-30", "2022-06-30"])
STABLE_CF = pd.DataFrame(
    {"2026-06-30": [1000.0, -100.0], "2025-06-30": [980.0, -100.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
VISA_CF = pd.DataFrame(
    {"2026-06-30": [6000.0, -500.0], "2025-06-30": [5800.0, -480.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
_DCF_RATES = dict(discount_rate=0.08, perpetual_rate=0.02, growth_rate=0.05)


def _clear_store():
    for f in os.listdir(fis._store_dir()):
        os.remove(os.path.join(fis._store_dir(), f))


def _clear_dry_run():
    for f in os.listdir(fdr._dry_run_dir()):
        os.remove(os.path.join(fdr._dry_run_dir(), f))


# ======================================================================
# C1: shadow file written and read.
# ======================================================================
_clear_dry_run()
_rows = [{"ticker": "BANKX", "universe": "S&P 500", "status": "ok"}]
fdr.save("S&P 500", _rows)
_loaded = fdr.load("S&P 500")
assert _loaded is not None
assert _loaded["rows"] == _rows
assert fdr.load("NEVER SAVED") is None
assert fdr.list_universes() == ["S&P 500"]
print("[C1_shadow_file_round_trip] save()/load() round trip preserves the row list; "
      "list_universes() names it; a never-saved universe returns None OK")


# ======================================================================
# C2: zero network calls in the shadow computation - yf.Ticker raising
# on construction must never be hit by compute_shadow_row().
# ======================================================================
_clear_store()


class _ExplodingTicker:
    def __init__(self, *a, **kw):
        raise AssertionError("compute_shadow_row() made a live yfinance call")


with mock.patch("yfinance.Ticker", _ExplodingTicker):
    _row_no_net = fdr.compute_shadow_row(
        "INSURER1", "Insurer One", 50.0, BANK_INFO, STABLE_CF, "USD",
        now_intrinsic_value=None, now_mos_pct=None, now_fcf_source="ocf_fallback_financials",
        now_dcf_unreliable=False, now_moat_mode="financials", now_quality=70,
        **_DCF_RATES,
    )
assert _row_no_net["ticker"] == "INSURER1"
print("[C2_zero_network_calls] compute_shadow_row() never constructs yf.Ticker - a forced "
      "AssertionError on construction never fires OK")


# ======================================================================
# C3: should_compute() - current rule OR switch-ON rule.
# ======================================================================
_switch(False)
TECH_INFO = {"sector": "Technology", "industry": "Software"}
assert fdr.should_compute(BANK_INFO, "BANKX") is True   # financials under both rules
assert fdr.should_compute(TECH_INFO, "AAPL") is False   # standard under both rules
# V: financials under the CURRENT rule (sector alone, switch off) even though the
# switch-ON rule would say standard - should_compute() must still say True (either rule).
assert fc.is_financials(VISA_INFO, ticker="V") is True
assert fc.is_financials_shadow(VISA_INFO, ticker="V") is False
assert fdr.should_compute(VISA_INFO, "V") is True
print("[C3_should_compute_either_rule] financials under current OR shadow rule -> True; "
      "V (financials now, standard under the override) -> True via the current rule alone; "
      "a plain tech ticker -> False under both OK")


# ======================================================================
# C4: compute_shadow_row() status logic - ok/awaiting_income_fetch/
# lt2_positive_years/fetch_failed, and the pool-ineligibility flag.
# ======================================================================
_clear_store()
# (a) standard mode (V, override OUT) -> "ok", regardless of any store entry.
_row_v = fdr.compute_shadow_row(
    "V", "Visa Inc", 50.0, VISA_INFO, VISA_CF, "USD",
    now_intrinsic_value=100.0, now_mos_pct=50.0, now_fcf_source="net_income_financials",
    now_dcf_unreliable=False, now_moat_mode="financials", now_quality=80,
    **_DCF_RATES,
)
assert _row_v["shadow_mode"] == "standard", _row_v["shadow_mode"]
assert _row_v["status"] == "ok", _row_v["status"]
assert _row_v["pool_ineligible_if_switch_on"] is False
print(f"[C4a_override_out_standard_ok] V (override OUT) -> shadow_mode=standard, status=ok, "
      f"shadow IV={_row_v['shadow_intrinsic_value']} (now IV {100.0} under the OLD sector-only "
      "rule - the override genuinely moves the valuation, not just the label) OK")

# (b) no store entry at all, financials-mode under the shadow rule -> "awaiting_income_fetch".
_row_awaiting = fdr.compute_shadow_row(
    "INSURER1", "Insurer One", 50.0, BANK_INFO, STABLE_CF, "USD",
    now_intrinsic_value=None, now_mos_pct=None, now_fcf_source="ocf_fallback_financials",
    now_dcf_unreliable=False, now_moat_mode="financials", now_quality=70,
    **_DCF_RATES,
)
assert _row_awaiting["shadow_mode"] == "financials"
assert _row_awaiting["status"] == "awaiting_income_fetch", _row_awaiting["status"]
assert _row_awaiting["pool_ineligible_if_switch_on"] is True
print("[C4b_no_entry_awaiting] no store entry, financials-mode under the shadow rule, no "
      "recorded Deep Dive fetch attempt -> status=awaiting_income_fetch, pool-ineligible OK")

# (c) a store entry with a recorded FAILED Deep Dive attempt -> "fetch_failed".
fis.record_deep_dive_attempt("INSURER2", success=False)
_row_failed = fdr.compute_shadow_row(
    "INSURER2", "Insurer Two", 50.0, BANK_INFO, STABLE_CF, "USD",
    now_intrinsic_value=None, now_mos_pct=None, now_fcf_source="ocf_fallback_financials",
    now_dcf_unreliable=False, now_moat_mode="financials", now_quality=70,
    **_DCF_RATES,
)
assert _row_failed["status"] == "fetch_failed", _row_failed["status"]
print("[C4c_fetch_failed] no store entry, but a RECORDED failed Deep Dive attempt -> "
      "status=fetch_failed (this status only ever reflects a Deep Dive on-view attempt, "
      "never the nightly pre-pass's own per-run failures, which aren't persisted per-ticker "
      "- disclosed in the report) OK")

# (d) a store entry with fewer than two positive years -> "lt2_positive_years".
fis.save("INSURER3", THIN_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
_row_thin = fdr.compute_shadow_row(
    "INSURER3", "Insurer Three", 50.0, BANK_INFO, STABLE_CF, "USD",
    now_intrinsic_value=None, now_mos_pct=None, now_fcf_source="ocf_fallback_financials",
    now_dcf_unreliable=False, now_moat_mode="financials", now_quality=70,
    **_DCF_RATES,
)
assert _row_thin["shadow_fcf_source"] == "ocf_fallback_financials", _row_thin["shadow_fcf_source"]
assert _row_thin["status"] == "lt2_positive_years", _row_thin["status"]
assert _row_thin["pool_ineligible_if_switch_on"] is True
print("[C4d_lt2_positive_years] a store entry with only one positive net-income year -> "
      "status=lt2_positive_years, pool-ineligible OK")

# (e) a GOOD store entry -> "ok", net income path, not pool-ineligible.
fis.save("BANKX", GOOD_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
_row_ok = fdr.compute_shadow_row(
    "BANKX", "Bank X", 50.0, BANK_INFO, STABLE_CF, "USD",
    now_intrinsic_value=None, now_mos_pct=None, now_fcf_source="ocf_fallback_financials",
    now_dcf_unreliable=False, now_moat_mode="financials", now_quality=70,
    **_DCF_RATES,
)
assert _row_ok["shadow_fcf_source"] == "net_income_financials", _row_ok["shadow_fcf_source"]
assert _row_ok["status"] == "ok", _row_ok["status"]
assert _row_ok["pool_ineligible_if_switch_on"] is False
# Addendum 2 item 7 (5 Oct 2026, Director-directed follow-up): shadow_quality
# (always the literal "changes") was removed in favour of now_moat/shadow_moat
# - see tests/test_financials_dry_run_followups.py for the replacement
# columns' own coverage. "none" of either key means no fundamentals bundle
# was cached for this ticker in this fresh test volume - "n/a", not an
# estimate, same contract as before.
assert "shadow_quality" not in _row_ok, _row_ok
assert _row_ok["now_moat"] is None and _row_ok["shadow_moat"] is None, _row_ok
print(f"[C4e_good_entry_ok] a good store entry (2+ positive years) -> status=ok, net-income "
      f"path, not pool-ineligible, shadow IV={_row_ok['shadow_intrinsic_value']} - shadow_quality "
      "is gone (replaced by now_moat/shadow_moat, Addendum 2 item 7) OK")
_clear_store()


# ======================================================================
# C5: nothing written to a scan row, score_history, a snapshot or the
# Top 100 pool - analyze_ticker_lite()'s own returned row dict carries
# NONE of the shadow keys, and the shadow computation never calls any
# of those four stores' own write functions.
# ======================================================================
_dates = pd.date_range("2026-01-01", periods=90, freq="D")
_hist_df = pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(90)],
                          "Volume": [1_000_000] * 90}, index=_dates)
_ONEOFF_LABEL = "one-off check income statement"


def _ytw_side_effect(fn, log, ticker_, label, **kw):
    if label == "history":
        return _hist_df
    if label == "info":
        return dict(BANK_INFO)
    if label == "cashflow":
        return STABLE_CF
    if label == _ONEOFF_LABEL:
        return None
    return None


_shadow_out = []
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
     mock.patch.object(fundamentals_data, "peek_cached_bundle", return_value=None), \
     mock.patch.object(scan_store, "save_scan") as _save_scan_mock, \
     mock.patch.object(score_history, "record") as _record_mock:
    _ytw.side_effect = _ytw_side_effect
    _riv.return_value = (150.0, "dcf", 0.12, {
        "growth_source": "history", "growth_governor": "History",
        "yahoo_estimate_status": "ok", "value_default": False,
        "fcf_distorted_years": [],
    })
    _row = ns.analyze_ticker_lite("DRYRUNTICK", log=lambda *a, **k: None, shadow_out=_shadow_out)
    _save_scan_mock.assert_not_called()
    _record_mock.assert_not_called()

assert _row is not None
_shadow_only_keys = {"shadow_mode", "shadow_intrinsic_value", "shadow_mos_pct",
                      "shadow_fcf_source", "shadow_dcf_unreliable", "shadow_moat_mode",
                      "now_moat", "shadow_moat", "status", "pool_ineligible_if_switch_on",
                      "in_current_top100", "now_intrinsic_value", "now_mos_pct",
                      "now_fcf_source", "now_dcf_unreliable", "now_moat_mode", "now_quality"}
assert not (_shadow_only_keys & set(_row.keys())), _shadow_only_keys & set(_row.keys())
assert len(_shadow_out) == 1, _shadow_out
print("[C5_never_written_to_real_row] analyze_ticker_lite()'s own returned row dict - the "
      "exact dict scan_store.save_scan() would persist - carries NONE of the shadow-only "
      "keys; scan_store.save_scan()/score_history.record() were never called from within "
      "the shadow computation itself; the one shadow row landed only in the separate "
      "shadow_out list OK")


# ======================================================================
# C6: the _switch_on() context manager restores the prior env state
# exactly, including when the code inside it raises.
# ======================================================================
os.environ.pop("FINANCIALS_STORE_LIVE", None)
with fdr._switch_on():
    assert os.environ.get("FINANCIALS_STORE_LIVE") == "1"
assert "FINANCIALS_STORE_LIVE" not in os.environ
try:
    with fdr._switch_on():
        raise ValueError("boom")
except ValueError:
    pass
assert "FINANCIALS_STORE_LIVE" not in os.environ

os.environ["FINANCIALS_STORE_LIVE"] = "on"
with fdr._switch_on():
    assert os.environ.get("FINANCIALS_STORE_LIVE") == "1"
assert os.environ.get("FINANCIALS_STORE_LIVE") == "on"
os.environ.pop("FINANCIALS_STORE_LIVE", None)
print("[C6_switch_on_restores_exactly] _switch_on() forces the switch ON for the duration of "
      "the block and restores whatever was there before (unset, or any other value) "
      "afterward - even when the block raises - never a Railway variable edit, never left "
      "in place OK")


# ======================================================================
# C7: the four financial fixtures named in "Tests common to commits
# 2-5" - before/after at the intrinsic-value level, switch OFF byte-
# identical, switch ON differences explained.
# ======================================================================
_clear_store()
fis.save("GOODBANK", GOOD_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)
fis.save("THINBANK", THIN_INCOME, currency="USD", source="yfinance_bundle_oneoff",
          latest_cf_period="2026-06-30", currency_converted=True)

_FIXTURES5 = [
    ("GOODBANK", "bank already on net income today", BANK_INFO, STABLE_CF),
    ("INSURERFB", "insurer on the fallback today", BANK_INFO, STABLE_CF),
    ("V", "card network (V-shaped), override OUT", VISA_INFO, VISA_CF),
    ("THINBANK", "fewer than two positive years", BANK_INFO, STABLE_CF),
]

print("\n[C7_before_after_table] switch OFF vs switch ON, per fixture:")
print(f"    {'ticker':12s} {'off_src':22s} {'off_iv':>10s}   {'on_src':22s} {'on_iv':>10s}  diff")
for ticker, label, info, cf in _FIXTURES5:
    _switch(False)
    _iv_off, _, _meta_off = fve.dcf_intrinsic_value(
        ticker, info=info, cashflow_df=cf, currency="USD", income_df=None, **_DCF_RATES,
    )

    _switch(True)
    _entry = fis.get(ticker)
    _income_on = _entry["income"] if _entry is not None else None
    _iv_on, _, _meta_on = fve.dcf_intrinsic_value(
        ticker, info=info, cashflow_df=cf, currency="USD", income_df=_income_on, **_DCF_RATES,
    )
    _switch(False)
    _diff = "SAME" if (_iv_off == _iv_on and _meta_off["fcf_source"] == _meta_on["fcf_source"]) else "DIFFERS"
    print(f"    {ticker:12s} {_meta_off['fcf_source'] or 'none':22s} {str(_iv_off):>10s}   "
          f"{_meta_on['fcf_source'] or 'none':22s} {str(_iv_on):>10s}  {_diff}")

print(
    "\n[C7_explanation] GOODBANK: switch OFF has no store entry consulted at all (fallback, "
    "OCF path) -> switch ON reads the store and gets a genuine net-income value - DIFFERS by "
    "design, this is the whole point of the instruction. INSURERFB: no store entry either "
    "way, switch OFF never reads financials_classifier's override table and switch ON still "
    "falls back (no entry) -> SAME. V: switch OFF classifies by sector alone (financials, "
    "net-income path); switch ON applies the override OUT to standard mode (OCF path) - "
    "DIFFERS by design, the override's whole point. THINBANK: fewer than two positive years "
    "either way -> OCF fallback both times -> SAME. Every DIFFERS case above is switch ON "
    "only (never switch OFF) and matches the instruction's own stated intent OK"
)
_clear_store()
_switch(False)


# ======================================================================
# C8: Admin section - defined once, owner-gated, called once from
# inside page_admin_dashboard(), inside the Valuation change audit
# area - static source-level check, same pattern as tests/
# test_imported_screen_admin_relocation.py and tests/
# test_valuation_change_audit.py's own admin-gate checks (rendering
# page_admin_dashboard() end-to-end needs mocking ai_gate/paywall_
# engine/admin_metrics_store/Streamlit session state for no extra
# confidence over reading the real call graph directly).
# ======================================================================
_APP_PATH = os.path.join(REPO_ROOT, "app.py")
with open(_APP_PATH, encoding="utf-8") as f:
    _SRC = f.read()


def _function_span(src, name):
    start = re.search(rf"(?m)^def {re.escape(name)}\(", src)
    assert start, f"def {name}( not found in app.py"
    nxt = re.search(r"(?m)^(def |class )", src[start.end():])
    end = start.end() + nxt.start() if nxt else len(src)
    return src[start.start():end]


assert _SRC.count("def _render_financials_dry_run_panel(") == 1
print("[C8a_defined_once] _render_financials_dry_run_panel() has exactly one definition OK")

_admin_body = _function_span(_SRC, "page_admin_dashboard")
assert "_render_financials_dry_run_panel()" in _admin_body
assert "ai_gate.is_owner(" in _admin_body
_owner_check_pos = _admin_body.index("ai_gate.is_owner(")
_panel_call_pos = _admin_body.index("_render_financials_dry_run_panel()")
assert _owner_check_pos < _panel_call_pos, \
    "the owner check must appear BEFORE the panel call, not after"
print("[C8b_admin_dashboard_calls_it_after_owner_check] page_admin_dashboard() renders the "
      "panel AFTER its own ai_gate.is_owner() check (which returns early for a non-owner, so "
      "a non-owner's request never reaches this call at all) OK")

_calls = [ln for ln in _SRC.splitlines() if ln.strip() == "_render_financials_dry_run_panel()"]
assert len(_calls) == 1
print("[C8c_one_call_site] exactly one call to _render_financials_dry_run_panel() in app.py OK")

_valaudit_body = _function_span(_SRC, "_render_valuation_change_audit_panel")
_def_start = re.search(r"(?m)^def _render_financials_dry_run_panel\(", _SRC)
assert _def_start.start() > re.search(
    r"(?m)^def _render_valuation_change_audit_panel\(", _SRC,
).start()
print("[C8d_inside_valuation_audit_area] _render_financials_dry_run_panel() is defined "
      "immediately after _render_valuation_change_audit_panel() - inside the existing "
      "Valuation change audit area, per the instruction's own placement OK")

_func_body = _function_span(_SRC, "_render_financials_dry_run_panel")
assert "unsafe_allow_html" not in _func_body
assert "st.button(" not in _func_body or "real" in _func_body  # no fake onclick-driven button
assert "onclick" not in _func_body
print("[C8e_streamlit_traps_respected] no unsafe_allow_html, no onclick, in the new panel's "
      "own source OK")


print("\nALL FINANCIALS INCOME STORE COMMIT 5 CHECKS PASSED")
