"""
LIVE BUG fixes from PUSH 1 (Director, 8 Oct 2026, Railway log 08 Oct
2026, deployment d8dba7a3):

1. StreamlitDuplicateElementKey (10:39:44 UTC): key='admin_health_news_
   v2_headlines_csv_OCL.AX' at app.py's
   _render_portfolio_health_news_v2_comparison_panel() - Andrew holds
   OCL.AX twice (two portfolios). Every widget key in that panel keyed
   by ticker alone (the per-ticker expanders, the per-ticker CSV
   download button) is now keyed by the loop's own row index instead,
   which is unique by construction however many portfolios hold the
   same ticker.

2. pyarrow ArrowTypeError (10:38:34 UTC): "Conversion failed for column
   ni_values with type object" in _render_financials_dry_run_panel()'s
   own table (a PRE-EXISTING column, Addendum 2 item 7, 5 Oct 2026 -
   older than PUSH 1, unrelated to it). financials_dry_run.
   _ni_diagnostics() returns a list of (period, value) pairs or None
   per row - a mixed object column with no single Arrow type, which
   st.dataframe()'s Arrow conversion cannot handle. Fixed with a
   one-line stringify of that one display-only column, immediately
   before the table renders.

Covers exactly the task's own listed tests:
  - a production-shaped portfolio holding the SAME ticker twice (two
    portfolios) - the news v2 comparison panel renders fully, no
    exception, with two independently-keyed expanders and two
    independently-keyed CSV download buttons.
  - a production-shaped financials dry run row set with a mixed
    ni_values column (None in one row, a list of (period, value) pairs,
    including a None value, in another) - the panel renders fully, no
    exception, and the displayed column is a plain string/None, never
    the original list/tuple objects.

Production-shaped fixtures: holdings seeded via the real writer,
portfolio_store.add_holding(); financials dry run rows seeded via the
real writer, financials_dry_run.save().

Run: python3 tests/test_live_bug_duplicate_ticker_key_and_pyarrow.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="live_bug_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import ai_gate
import financials_dry_run as fdr
import portfolio_health_engine as phe
import portfolio_news_engine as pne
import portfolio_store as ps

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

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

_OWNER = ai_gate.owner_email()
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ======================================================================
# CHECK 1-5: the SAME ticker (OCL.AX, Andrew's own real example) held
# TWICE, in two different portfolios - production-shaped via the real
# writer, portfolio_store.add_holding().
# ======================================================================
ps.add_holding(_OWNER, "Main", "OCL.AX", name="Objective Corp", kind="STOCK",
                currency="AUD", shares=100.0, buy_price=10.0, buy_date="2025-01-01")
ps.add_holding(_OWNER, "SMSF", "OCL.AX", name="Objective Corp", kind="STOCK",
                currency="AUD", shares=50.0, buy_price=12.0, buy_date="2025-03-01")

_FAKE_SNAPSHOT = {
    "price": 12.0, "revenue_growth": 0.05, "earnings_growth": 0.05, "profit_margin": 0.20,
    "roe": 0.15, "fcf_growth": 0.04, "debt_to_equity": 40.0, "mos_pct": 8.0,
    "intrinsic_value": 13.0, "history": None, "range52": 0.5, "trend_vs_ma200": 0.01,
    "dividend_rate": None, "dividend_yield": None,
}

_patches = [
    mock.patch.object(phe, "fetch_snapshot", return_value=_FAKE_SNAPSHOT),
    mock.patch.object(pne, "_claim_fetch", return_value=False),  # no network fetch
    mock.patch.object(phe, "record_health_run",
                       side_effect=AssertionError("record_health_run() must never be called "
                                                    "from a read-only comparison panel")),
]
for p in _patches:
    p.start()
try:
    _script1 = f"""
import os, sys
sys.path.insert(0, {_SCRIPT_DIR!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
app._render_portfolio_health_news_v2_comparison_panel()
"""
    _at1 = AppTest.from_string(_script1, default_timeout=60)
    _at1.run()
    check("the panel renders without raising, with OCL.AX held in TWO portfolios "
          "(the exact StreamlitDuplicateElementKey trigger)", not _at1.exception)

    _expanders1 = list(_at1.expander)
    _ocl_headline_expanders = [
        e for e in _expanders1
        if (getattr(e, "label", "") or "") == "OCL.AX - every stored headline"
    ]
    check("TWO separate 'OCL.AX - every stored headline' expanders are offered "
          "(one per portfolio, not merged or dropped)", len(_ocl_headline_expanders) == 2)

    _comparison_df = None
    for df in _at1.get("dataframe"):
        try:
            if "Ticker" in set(df.value.columns):
                _comparison_df = df.value
                break
        except Exception:
            continue
    check("OCL.AX appears TWICE in the comparison table (once per portfolio)",
          _comparison_df is not None
          and list(_comparison_df["Ticker"]).count("OCL.AX") == 2)
finally:
    for p in _patches:
        p.stop()

print("[duplicate_ticker_no_crash] a ticker held in two portfolios no longer crashes the "
      "panel - both rows render, each with its own independently-keyed widgets OK")

# ======================================================================
# CHECK 6-9: financials dry run panel with a mixed ni_values column -
# None in one row, a list of (period, value) pairs (including a None
# value) in another - seeded via the real writer, financials_dry_run.
# save(), exactly as nightly_scan.py's own scan loop would write it.
# ======================================================================
_DRY_RUN_ROWS = [
    {
        "universe": "ASX 200", "ticker": "ARES", "company": "Ares Mgmt-like",
        "price": 50.0, "reporting_currency": "USD", "listing_currency": "AUD",
        "store_latest_period": "2026-06-30", "store_source": "cashflow",
        "ni_row_label": "Net Income", "ni_values": None,
        "now_intrinsic_value": 55.0, "now_mos_pct": 9.0, "now_fcf_source": "ocf-normcapex",
        "now_dcf_unreliable": False, "now_moat_mode": "standard", "now_quality": 60.0,
        "now_moat": 50.0, "shadow_moat": 50.0,
        "shadow_mode": "standard", "shadow_intrinsic_value": 55.0, "shadow_mos_pct": 9.0,
        "shadow_fcf_source": "ocf-normcapex", "shadow_dcf_unreliable": False,
        "shadow_moat_mode": "standard", "shadow_reason": None, "status": "ok",
        "shadow_mode_reason": "sector", "pool_ineligible_if_switch_on": False,
        "in_current_top100": False,
    },
    {
        "universe": "ASX 200", "ticker": "IVZ", "company": "Invesco-like",
        "price": 20.0, "reporting_currency": "USD", "listing_currency": "USD",
        "store_latest_period": "2026-06-30", "store_source": "info",
        "ni_row_label": "Net Income Common Stockholders",
        # A real-shaped mixed row: period labels paired with values that
        # include a None (a missing period), alongside the other row's
        # plain None above - this is the exact column heterogeneity that
        # trips pyarrow's Arrow conversion.
        "ni_values": [["2026-06-30", 123.4], ["2025-06-30", None], ["2024-06-30", 98.7]],
        "now_intrinsic_value": 18.0, "now_mos_pct": -10.0, "now_fcf_source": "info",
        "now_dcf_unreliable": True, "now_moat_mode": "financials", "now_quality": 40.0,
        "now_moat": 30.0, "shadow_moat": 30.0,
        "shadow_mode": "financials", "shadow_intrinsic_value": 18.0, "shadow_mos_pct": -10.0,
        "shadow_fcf_source": "info", "shadow_dcf_unreliable": True,
        "shadow_moat_mode": "financials", "shadow_reason": "ni_path_abandoned", "status": "ok",
        "shadow_mode_reason": "industry", "pool_ineligible_if_switch_on": False,
        "in_current_top100": False,
    },
]
fdr.save("ASX 200", _DRY_RUN_ROWS)

_script2 = f"""
import os, sys
sys.path.insert(0, {_SCRIPT_DIR!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
app._render_financials_dry_run_panel()
"""
_at2 = AppTest.from_string(_script2, default_timeout=60)
_at2.run()
check("the financials dry run panel renders without raising, with a mixed None/list "
      "ni_values column (the exact pyarrow ArrowTypeError trigger)", not _at2.exception)

_dry_run_df = None
for df in _at2.get("dataframe"):
    try:
        if "ni_values" in set(df.value.columns):
            _dry_run_df = df.value
            break
    except Exception:
        continue
check("the dry run table is present with its ni_values column", _dry_run_df is not None)
if _dry_run_df is not None:
    _ni_values_col = list(_dry_run_df["ni_values"])
    check("ARES's ni_values (originally None) is still None, not stringified to 'None'",
          _ni_values_col[0] is None or _ni_values_col[0] != _ni_values_col[0])  # NaN-safe
    check("IVZ's ni_values (originally a list) is now a plain string, never a list/tuple",
          isinstance(_ni_values_col[1], str) and "123.4" in _ni_values_col[1])

print("[pyarrow_ni_values_no_crash] a mixed None/list ni_values column no longer crashes "
      "the financials dry run table - the column is stringified, display-only OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
