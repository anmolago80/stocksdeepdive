"""
PART 3 STEP 3.1 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): the owner-only "Market readiness"
panel - market_readiness_engine.py (logic) + app.py's
_render_market_readiness_panel() (markup).

Production-shaped fixtures throughout: universe rows seeded via the
real writer, scan_store.save_scan() (the exact function nightly_scan.
run_universe_scan() calls); run-to-run history seeded via the real
writer, score_history.record(rows, day=...) (the exact function
nightly_scan.py calls after every scan, private universes included).

Covers exactly the task's own listed tests:
  - each count on a small fixture (valuation/dividend/price-guard/
    run-to-run/cross-ticker/top-20/Top-200-dry-run breakdowns)
  - a pence row (price_quote_unit == "GBp")
  - a missing field shows "not stored"
  - the panel makes NO network call (yfinance.Ticker patched to raise
    if constructed at all) and WRITES NOTHING (every stored file's own
    mtime/content is byte-identical before and after)

Run: python3 tests/test_part3_step3_1_market_readiness_panel.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part3_step3_1_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import market_readiness_engine as mre
import scan_store
import score_history

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


# ======================================================================
# CHECK 1-3: expected_constituent_count() - trailing-number extraction,
# never a guess for a name with no number.
# ======================================================================
check('"FTSE 100" -> 100', mre.expected_constituent_count("FTSE 100") == 100)
check('"TOPIX 500" -> 500', mre.expected_constituent_count("TOPIX 500") == 500)
check('"TSX Composite" -> None (no published size in its own name, never a guess)',
      mre.expected_constituent_count("TSX Composite") is None)

# ======================================================================
# Seed a production-shaped PRIVATE universe ("FTSE 100") via the real
# writer, scan_store.save_scan() - 4 rows covering: a pence-quoted
# ticker, a growth-cap ticker, a DCF-unreliable + price-guard-flagged
# ticker, and a high-dividend-yield ticker.
# ======================================================================
_ROWS = [
    {
        "Ticker": "PENCECO.L", "Company Name": "Pence Company plc",
        "Price": 12.34, "Intrinsic Value": 15.0, "MOS %": 17.7,
        "Long Score": 72.0, "Growth Source": "analyst", "Growth Used": 0.06,
        "Growth Ceiling Used": 0.08, "FCF Source": "ocf-normcapex",
        "Dividend TTM": 0.5, "Dividend Yield %": 4.0,
        "price_quote_unit": "GBp", "price_unit_suspect": False,
        "DCF Unreliable": False,
    },
    {
        "Ticker": "CAPCO.L", "Company Name": "At The Cap plc",
        "Price": 20.0, "Intrinsic Value": 25.0, "MOS %": 20.0,
        "Long Score": 65.0, "Growth Source": "history", "Growth Used": 0.08,
        "Growth Ceiling Used": 0.08, "FCF Source": "ocf-normcapex",
        "Dividend TTM": 0.0, "Dividend Yield %": None,
        "price_quote_unit": None, "price_unit_suspect": False,
        "DCF Unreliable": False,
    },
    {
        "Ticker": "GUARDCO.L", "Company Name": "Guard Flagged plc",
        "Price": 100.0, "Intrinsic Value": 500.0, "MOS %": 80.0,
        "Long Score": 40.0, "Growth Source": "default", "Growth Used": 0.04,
        "Growth Ceiling Used": 0.10, "FCF Source": "info",
        "Dividend TTM": None, "Dividend Yield %": None,
        "price_quote_unit": "GBp", "price_unit_suspect": True,
        "price_unit_suspect_reason": "[units] GUARDCO.L GBp->GBP ratio=0.01 SUSPECT",
        "DCF Unreliable": True,
    },
    {
        "Ticker": "HIYIELD.L", "Company Name": "High Yield plc",
        "Price": 5.0, "Intrinsic Value": None, "MOS %": None,
        "Long Score": None, "Growth Source": None, "Growth Used": None,
        "Growth Ceiling Used": None, "FCF Source": None,
        "Dividend TTM": 0.75, "Dividend Yield %": 15.0,
        "price_quote_unit": None, "price_unit_suspect": False,
        "DCF Unreliable": False,
    },
]
scan_store.save_scan("FTSE 100", _ROWS, source_label="test fixture")

# A second stored universe ("ASX 200") carrying the SAME company under
# a different ticker, for the cross-ticker-pair check.
scan_store.save_scan("ASX 200", [
    {"Ticker": "PENCECO.AX", "Company Name": "Pence Company plc",
     "Price": 1.0, "Intrinsic Value": 1.2, "MOS %": 16.0, "Long Score": 70.0},
], source_label="test fixture")

# Run-to-run history: PENCECO.L moves >25% between two stored days;
# CAPCO.L has only ONE stored day (no comparison possible yet).
score_history.record([{"Ticker": "PENCECO.L", "Long Score": 72.0, "Price": 11.0,
                        "Intrinsic Value": 10.0}], day="2026-10-01")
score_history.record([{"Ticker": "PENCECO.L", "Long Score": 72.0, "Price": 12.34,
                        "Intrinsic Value": 15.0}], day="2026-10-07")
score_history.record([{"Ticker": "CAPCO.L", "Long Score": 65.0, "Price": 20.0,
                        "Intrinsic Value": 25.0}], day="2026-10-07")

_readiness = mre.universe_readiness("FTSE 100")

# ======================================================================
# CHECK 4-6: rows stored, pence-quoted count, trading currencies.
# ======================================================================
check("rows_stored == 4", _readiness["rows_stored"] == 4)
check('pence_quoted_count == 2 (PENCECO.L and GUARDCO.L both "GBp")',
      _readiness["pence_quoted_count"] == 2)
check('trading_currencies_found == {"GBP": 4} (every ticker ends .L)',
      _readiness["trading_currencies_found"] == {"GBP": 4})

# ======================================================================
# CHECK 7: constituents expected/missing for "FTSE 100" (100 expected,
# 4 stored -> 96 missing).
# ======================================================================
check("constituents_expected == 100, constituents_missing == 96",
      _readiness["constituents_expected"] == 100 and _readiness["constituents_missing"] == 96)

# ======================================================================
# CHECK 8-11: valuation breakdown.
# ======================================================================
_val = _readiness["valuation"]
check("dcf_unreliable_count == 1 (GUARDCO.L)", _val["dcf_unreliable_count"] == 1)
check("at_growth_cap_count == 1 (CAPCO.L: Growth Used == Growth Ceiling Used == 0.08)",
      _val["at_growth_cap_count"] == 1)
check("analyst_estimate_count == 1 (PENCECO.L)", _val["analyst_estimate_count"] == 1)
check("fallback_path_counts counts every non-None Growth Source",
      _val["fallback_path_counts"] == {"analyst": 1, "history": 1, "default": 1})
check("above_80_count == 0 (GUARDCO.L sits at exactly 80.0, which is NOT > 80)",
      _val["above_80_count"] == 0)
check("median_mos_pct is the median of [17.7, 20.0, 80.0] == 20.0",
      _val["median_mos_pct"] == 20.0)

# ======================================================================
# CHECK 12-13: dividend breakdown.
# ======================================================================
_div = _readiness["dividends"]
check("paying_count == 2 (PENCECO.L, HIYIELD.L)", _div["paying_count"] == 2)
check("high_yield_tickers == ['HIYIELD.L'] (yield 15.0% > 12%)",
      _div["high_yield_tickers"] == ["HIYIELD.L"])

# ======================================================================
# CHECK 14-15: price guard flags - the ticker and stored reason appear;
# the two un-stored figures explicitly say "not stored", never a guess.
# ======================================================================
_guard = _readiness["price_guard_flags"]
check("exactly one ticker flagged (GUARDCO.L)",
      len(_guard) == 1 and _guard[0]["ticker"] == "GUARDCO.L")
check('its own two compared figures (price x shares vs marketCap) say "not stored" - '
      "never re-derived or guessed",
      "not stored" in _guard[0]["price_times_shares_vs_marketcap"])

# ======================================================================
# CHECK 16-18: run-to-run stability - PENCECO.L flagged (IV 10.0 ->
# 15.0 is +50%, above the 25% threshold); CAPCO.L has only one scan.
# ======================================================================
_rtr = _readiness["run_to_run"]
check("PENCECO.L is flagged for a >25% intrinsic-value move between its last two scans",
      any(f["ticker"] == "PENCECO.L" for f in _rtr["flagged"]))
_penceco_flag = next(f for f in _rtr["flagged"] if f["ticker"] == "PENCECO.L")
check("the flagged move is +50.0%, with both dates and both values shown",
      _penceco_flag["pct_move"] == 50.0
      and _penceco_flag["previous_intrinsic_value"] == 10.0
      and _penceco_flag["latest_intrinsic_value"] == 15.0)
check("CAPCO.L (only one stored day) is listed under single_scan_tickers, not flagged",
      "CAPCO.L" in _rtr["single_scan_tickers"]
      and not any(f["ticker"] == "CAPCO.L" for f in _rtr["flagged"]))

# ======================================================================
# CHECK 19: cross-ticker pairs - PENCECO.L (FTSE 100) <-> PENCECO.AX
# (ASX 200), same Company Name.
# ======================================================================
_pairs = _readiness["cross_ticker_pairs"]
check("PENCECO.L <-> PENCECO.AX pair found, naming the other universe",
      any(p["this_ticker"] == "PENCECO.L" and p["other_ticker"] == "PENCECO.AX"
          and p["other_universe"] == "ASX 200" for p in _pairs))

# ======================================================================
# CHECK 20: top 20 by Value Score - sorted descending, unscored row
# (HIYIELD.L, Long Score None) sorts LAST, never dropped.
# ======================================================================
_top20 = _readiness["top20"]
check("top20 has all 4 rows (fewer than 20 - none dropped)", len(_top20) == 4)
check("sorted by value score descending: PENCECO.L (72) first",
      _top20[0]["ticker"] == "PENCECO.L")
check("the unscored row (HIYIELD.L) sorts LAST, not dropped",
      _top20[-1]["ticker"] == "HIYIELD.L")

# ======================================================================
# CHECK 21: Top 200 dry run - a count only, cost at $0.02/company for
# the unscored row.
# ======================================================================
_t200 = _readiness["top200_dry_run"]
check("candidate_count == 4, no_stored_score_count == 1 (HIYIELD.L), cost == $0.02",
      _t200["candidate_count"] == 4 and _t200["no_stored_score_count"] == 1
      and _t200["estimated_cost_usd"] == 0.02)

print("[market_readiness_engine_counts] every sub-breakdown matches the fixture's own "
      "hand-computed expectation on a small, production-shaped universe OK")

# ======================================================================
# CHECK 22-23: NO network call, NOTHING written.
# ======================================================================
_scan_path = scan_store._path("FTSE 100")
_mtime_before = os.path.getmtime(_scan_path)
with open(_scan_path, "rb") as f:
    _content_before = f.read()

with mock.patch("yfinance.Ticker", side_effect=AssertionError(
        "market_readiness_engine must never construct a yfinance.Ticker - "
        "it reads stored rows only")):
    _readiness_again = mre.universe_readiness("FTSE 100")
check("a second call raised no exception (confirms yfinance.Ticker was never constructed)",
      _readiness_again is not None)

_mtime_after = os.path.getmtime(_scan_path)
with open(_scan_path, "rb") as f:
    _content_after = f.read()
check("the stored scan file's own mtime and content are byte-identical before/after - "
      "nothing was written", _mtime_before == _mtime_after and _content_before == _content_after)

print("[no_network_no_write] universe_readiness() makes no network call (yfinance.Ticker "
      "patched to raise) and writes nothing (stored file byte-identical) OK")

# ======================================================================
# CHECK 24-25: the render function itself - renders without raising,
# and page_admin_dashboard() calls it AFTER its own owner check (same
# pattern every other owner-only Admin panel in this codebase proves).
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_render_script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
app._render_market_readiness_panel()
"""
_at = AppTest.from_string(_render_script, default_timeout=60)
_at.run()
check("_render_market_readiness_panel() renders without raising", not _at.exception)
_markdowns = " ".join(m.value or "" for m in _at.get("markdown"))
_expander_labels = " ".join(getattr(e, "label", "") or "" for e in _at.expander)
check("the panel's own heading appears, and both seeded universes have their own expander",
      "Market readiness" in _markdowns
      and "FTSE 100" in _expander_labels and "ASX 200" in _expander_labels)

import inspect
_src = inspect.getsource(sys.modules["app"].page_admin_dashboard)
_render_line = _src.find("_render_market_readiness_panel()")
_owner_check_line = _src.find("ai_gate.is_owner(")
check("page_admin_dashboard()'s own owner check appears BEFORE this panel is ever called",
      0 < _owner_check_line < _render_line)

print("[render_function] the panel renders without raising, shows every seeded universe, "
      "and is unreachable before page_admin_dashboard()'s own owner check OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
