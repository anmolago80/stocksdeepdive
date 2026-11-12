"""
Director's TASK 49 (12 Nov 2026): change the Top 200 tab order to:

  Top 20 · Mixed | Quality on sale | Top 20 · Australia | Top 20 · USA |
  Top 20 · Foreign US-listed | Top 20 · United Kingdom | Top 20 · Canada |
  Top 20 · Europe | Full 200 | Near the cut-off

- "Quality on sale" directly after "Top 20 · Mixed".
- "Near the cut-off" last.
- Default selected tab stays "Top 20 · Mixed".
- If a tab is gated off, the remaining tabs keep this order.

This is a pure render-layer reorder (top100_render.py's new _tab_specs
ordered-list assembly in render_top100_page()) - no selection/scoring
change, so this file only checks tab LABELS/ORDER under every gate
combination, never tab contents (already covered by each feature's own
test file: TASK 7 for the market tabs, TASK 24 for Foreign US-listed,
TASK 42/44 for Quality on sale, TASK 48 for Near the cut-off).

Fixture pool: AAPL (USA), CSL.AX (Australia), RY.TO (Canada - exercises
one _new_market_tab_plan() tab), NU (Foreign US-listed scan row, Home
Country Brazil). This gives one tab from every conditional group at
once (foreign/market-plan), so every gate combination below produces a
genuinely different, checkable tab list.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task49_tab_order.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task49_tab_order_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import ai_gate
import scan_store
import top100_engine as te
import top100_store as ts
import top100_render as tr  # noqa: F401 (imported for _enriched_pool sanity check below)

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

OWNER_EMAIL = ai_gate.owner_email()
NONOWNER_EMAIL = "someone-else@example.com"

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


def _dims_all_scored():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


dims = _dims_all_scored()


def _save_score(ticker):
    ts.save_score(
        ticker=ticker, quarter=f"RP2026-09-30-{ticker}", model=te.MODEL_TOP100,
        rubric_version=te.RUBRIC_VERSION, dims=dims, not_rated=False,
        inversion_scenario=None, inversion_severity=None, prompt="{}", raw_response="{}",
    )


ts.save_pool(
    [
        {"ticker": "AAPL", "company_name": "Apple", "universe": "S&P 500", "value_score": 80.0,
         "mos_pct": 20.0, "price": 180.0, "intrinsic_value": 210.0, "currency": "USD",
         "psychology": 5.0, "sector": "Technology", "dividend_yield_pct": 0.5},
        {"ticker": "CSL.AX", "company_name": "CSL", "universe": "ASX 200", "value_score": 75.0,
         "mos_pct": 25.0, "price": 200.0, "intrinsic_value": 250.0, "currency": "AUD",
         "psychology": 5.0, "sector": "Health Care", "dividend_yield_pct": 1.5},
        {"ticker": "RY.TO", "company_name": "Royal Bank of Canada", "universe": "Fixture",
         "value_score": 70.0, "mos_pct": 15.0, "price": 120.0, "intrinsic_value": 140.0,
         "currency": "CAD", "psychology": 5.0, "sector": "Financial Services",
         "dividend_yield_pct": 3.0},
        {"ticker": "NU", "company_name": "Nu Holdings Ltd.", "universe": "Foreign US-listed",
         "value_score": 65.0, "mos_pct": 25.0, "price": 12.0, "intrinsic_value": 16.0,
         "currency": "USD", "psychology": 5.0, "sector": "Financial Services",
         "dividend_yield_pct": 0.0},
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)
for t in ("AAPL", "CSL.AX", "RY.TO", "NU"):
    _save_score(t)

scan_store.save_scan("Foreign US-listed", [
    {"Ticker": "NU", "Type": "STOCK", "Company Name": "Nu Holdings Ltd.", "Price": 12.0,
     "Intrinsic Value": 16.0, "MOS %": 25.0, "Long Score": 60.0, "Quality": 70.0,
     "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
     "Growth Used": 0.1, "FCF Source": "info", "Dividend Yield %": 0.0,
     "Home Country": "Brazil"},
], source_label="test fixture")

_enriched_sanity = tr._enriched_pool()
check("sanity: all 4 fixture rows are RATED (composite is not None)",
      all(r["composite"] is not None for r in _enriched_sanity))

from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_page(lang="en", email=NONOWNER_EMAIL, foreign_tab_live=None,
              quality_on_sale_live=None, sticky_buffer_live=None):
    def _env_line(name, value):
        return (
            f'os.environ[{name!r}] = {value!r}' if value is not None else
            f'os.environ.pop({name!r}, None)'
        )

    env_lines = "\n".join([
        _env_line("TOP20_FOREIGN_TAB_LIVE", foreign_tab_live),
        _env_line("TOP200_QUALITY_ON_SALE_LIVE", quality_on_sale_live),
        _env_line("TOP200_STICKY_BUFFER_LIVE", sticky_buffer_live),
    ])
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{env_lines}
import top100_render
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
top100_render.render_top100_page(lang={lang!r})
"""
    at = AppTest.from_string(script)
    at.run(timeout=30)
    assert not at.exception, f"render_top100_page(lang={lang!r}) raised: {at.exception}"
    return at


# ======================================================================
# CHECK 1: every gate OFF, non-owner - no Quality on sale, no Foreign,
# no Near the cut-off. Order: Mixed | Australia | USA | Canada | Full 200.
# ======================================================================
_at_all_off = _run_page(email=NONOWNER_EMAIL)
_labels_all_off = [tab.proto.label for tab in _at_all_off.tabs]
check("ALL GATES OFF, non-owner: exact tab order with every optional tab "
      "gated off (market-plan tab for Canada still present - unconditional)",
      _labels_all_off == ["Top 20 · Mixed", "Top 20 · Australia", "Top 20 · USA",
                           "Top 20 · Canada", "Full 200"])
check("ALL GATES OFF: 'Top 20 · Mixed' is tabs[0] - default selected tab stays Mixed",
      _labels_all_off[0] == "Top 20 · Mixed")
print("[task49_c1_all_gates_off] order correct, Mixed still first OK")

# ======================================================================
# CHECK 2: every gate ON, non-owner (every optional tab now present) -
# full Director-specified order with the one market-plan tab (Canada)
# standing in for "Top 20 · United Kingdom | Top 20 · Canada | Top 20 ·
# Europe" (_new_market_tab_plan()'s own ordering among those three is
# TASK 7's concern, untouched by this task - see that test file).
# ======================================================================
_at_all_on = _run_page(
    email=NONOWNER_EMAIL, foreign_tab_live="1", quality_on_sale_live="1", sticky_buffer_live="1")
_labels_all_on = [tab.proto.label for tab in _at_all_on.tabs]
check("ALL GATES ON, non-owner: 'Quality on sale' is directly after 'Top 20 · Mixed'",
      _labels_all_on.index("Quality on sale") == _labels_all_on.index("Top 20 · Mixed") + 1)
check("ALL GATES ON: 'Top 20 · Foreign US-listed' sits between USA and the "
      "market-plan tabs (right after 'Top 20 · USA')",
      _labels_all_on.index("Top 20 · Foreign US-listed")
      == _labels_all_on.index("Top 20 · USA") + 1)
check("ALL GATES ON: 'Near the cut-off' is the LAST tab",
      _labels_all_on[-1] == "Near the cut-off")
check("ALL GATES ON: 'Full 200' is directly before 'Near the cut-off'",
      _labels_all_on.index("Full 200") == _labels_all_on.index("Near the cut-off") - 1)
check("ALL GATES ON: exact full tab order",
      _labels_all_on == ["Top 20 · Mixed", "Quality on sale", "Top 20 · Australia",
                          "Top 20 · USA", "Top 20 · Foreign US-listed", "Top 20 · Canada",
                          "Full 200", "Near the cut-off"])
check("ALL GATES ON: 'Top 20 · Mixed' is still tabs[0] - default selected tab unchanged",
      _labels_all_on[0] == "Top 20 · Mixed")
print("[task49_c2_all_gates_on] full Director-specified order, Mixed still first OK")

# ======================================================================
# CHECK 3: ONLY Quality on sale on - inserted right after Mixed, every
# other tab's relative order (and adjacency) unaffected.
# ======================================================================
_at_qos_only = _run_page(email=NONOWNER_EMAIL, quality_on_sale_live="1")
_labels_qos_only = [tab.proto.label for tab in _at_qos_only.tabs]
check("ONLY Quality on sale ON: exact order - inserted right after Mixed, "
      "remaining tabs (Australia/USA/Canada/Full 200) keep their relative order",
      _labels_qos_only == ["Top 20 · Mixed", "Quality on sale", "Top 20 · Australia",
                            "Top 20 · USA", "Top 20 · Canada", "Full 200"])
print("[task49_c3_quality_on_sale_only] gated-off tabs' relative order preserved OK")

# ======================================================================
# CHECK 4: ONLY Foreign US-listed on - inserted right after USA.
# ======================================================================
_at_foreign_only = _run_page(email=NONOWNER_EMAIL, foreign_tab_live="1")
_labels_foreign_only = [tab.proto.label for tab in _at_foreign_only.tabs]
check("ONLY Foreign US-listed ON: exact order - inserted right after USA, "
      "no Quality on sale/Near the cut-off",
      _labels_foreign_only == ["Top 20 · Mixed", "Top 20 · Australia", "Top 20 · USA",
                                "Top 20 · Foreign US-listed", "Top 20 · Canada", "Full 200"])
print("[task49_c4_foreign_only] gated-off tabs' relative order preserved OK")

# ======================================================================
# CHECK 5: ONLY Near the cut-off on - appended last, after Full 200.
# ======================================================================
_at_ncu_only = _run_page(email=NONOWNER_EMAIL, sticky_buffer_live="1")
_labels_ncu_only = [tab.proto.label for tab in _at_ncu_only.tabs]
check("ONLY Near the cut-off ON: exact order - appended last, after Full 200, "
      "no Quality on sale/Foreign US-listed",
      _labels_ncu_only == ["Top 20 · Mixed", "Top 20 · Australia", "Top 20 · USA",
                            "Top 20 · Canada", "Full 200", "Near the cut-off"])
print("[task49_c5_near_cutoff_only] gated-off tabs' relative order preserved OK")

# ======================================================================
# CHECK 6: owner override - every gate unset still shows every optional
# tab to the owner, in the same full order as CHECK 2 (same gate-shape
# precedent as TASK 24/42/48's own "owner sees it regardless" tests).
# ======================================================================
_at_owner_off = _run_page(email=OWNER_EMAIL)
_labels_owner_off = [tab.proto.label for tab in _at_owner_off.tabs]
check("OWNER, every gate unset: owner still sees every optional tab, same full order",
      _labels_owner_off == ["Top 20 · Mixed", "Quality on sale", "Top 20 · Australia",
                             "Top 20 · USA", "Top 20 · Foreign US-listed", "Top 20 · Canada",
                             "Full 200", "Near the cut-off"])
print("[task49_c6_owner_override] owner sees every optional tab with every gate unset OK")

# ======================================================================
# CHECK 7: ES labels follow the same order (localization doesn't affect
# ordering - same label-order check, just with the ES label strings).
# ======================================================================
_at_all_on_es = _run_page(
    email=NONOWNER_EMAIL, lang="es", foreign_tab_live="1", quality_on_sale_live="1",
    sticky_buffer_live="1")
_labels_all_on_es = [tab.proto.label for tab in _at_all_on_es.tabs]
check("ALL GATES ON, ES: same tab order as EN, just localized labels",
      _labels_all_on_es == ["Top 20 · Mixto", "Calidad en oferta", "Top 20 · Australia",
                             "Top 20 · EE. UU.", "Top 20 · Extranjeras en EE. UU.",
                             "Top 20 · Canadá", "Las 200", "Cerca del corte"])
print("[task49_c7_es_order] ES tab order matches EN, localized labels OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
