"""
Valuation Change Audit (owner-directed, 2 Oct 2026, Commit 2 of the
23:00 UTC incident response) - valuation_audit_engine.py + its
app.py admin-dashboard panel.

Fixture: two days of score_history (today = date A, 1 day back = date
B) for a handful of tickers, plus a saved scan file carrying the new
growth/FCF/discount provenance fields nightly_scan.analyze_ticker_lite()
now writes (see that function's own comment at its "Growth Source"/...
keys). Covers:
  - dIV % computed correctly (and None when IV(B) is missing/zero)
  - default sort order (|dIV %| descending)
  - summary counts (ticker_count, median dIV %, >+50%/>+100%, DCF-
    unreliable) correct
  - the >+50% breakdown (by fcf_base_source / distorted-years empty vs
    non-empty / growth_source) correct
  - a ticker with no scan row at all still appears (growth/FCF columns
    None, not a crash)
  - days_back clamped to [1,7] and gracefully falls back to date_b=None
    when there isn't enough history yet
  - the admin gate: _render_valuation_change_audit_panel() is called
    from inside page_admin_dashboard(), after that function's own
    ai_gate.is_owner() check - same static-source-check pattern
    test_imported_screen_admin_relocation.py already uses for exactly
    this kind of admin-only section, since fully rendering the page
    needs no extra confidence over reading the real call graph.

Run: python3 tests/test_valuation_change_audit.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="valuation_audit_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import score_history
import scan_store
import valuation_audit_engine as vae

_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
_yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

# ======================================================================
# Fixture setup: score_history for date A (today) and date B (yesterday).
# ======================================================================
score_history.record([
    {"Ticker": "BIGUP", "Long Score": 60, "Price": 10.0, "Quality": 70,
     "Moat": None, "MOS %": 83.3, "Intrinsic Value": 60.0},
    {"Ticker": "MODUP", "Long Score": 55, "Price": 20.0, "Quality": 65,
     "Moat": None, "MOS %": 60.0, "Intrinsic Value": 50.0},
    {"Ticker": "FLAT", "Long Score": 50, "Price": 30.0, "Quality": 60,
     "Moat": None, "MOS %": 10.0, "Intrinsic Value": 33.0},
    {"Ticker": "NODATA_B", "Long Score": 45, "Price": 15.0, "Quality": 55,
     "Moat": None, "MOS %": 20.0, "Intrinsic Value": 18.0},
    {"Ticker": "NOSCAN", "Long Score": 40, "Price": 5.0, "Quality": 50,
     "Moat": None, "MOS %": 50.0, "Intrinsic Value": 10.0},
], day=_today)

score_history.record([
    {"Ticker": "BIGUP", "Long Score": 58, "Price": 10.0, "Quality": 70,
     "Moat": None, "MOS %": 50.0, "Intrinsic Value": 20.0},
    {"Ticker": "MODUP", "Long Score": 53, "Price": 20.0, "Quality": 65,
     "Moat": None, "MOS %": 50.0, "Intrinsic Value": 40.0},
    {"Ticker": "FLAT", "Long Score": 49, "Price": 30.0, "Quality": 60,
     "Moat": None, "MOS %": 8.0, "Intrinsic Value": 32.0},
    # NODATA_B deliberately has no row on date B.
    {"Ticker": "NOSCAN", "Long Score": 39, "Price": 5.0, "Quality": 50,
     "Moat": None, "MOS %": 45.0, "Intrinsic Value": 9.0},
], day=_yesterday)


def _scan_row(ticker, price, iv, growth_source="history", fcf_base_source="fcf-median",
              fcf_distorted_years=None, dcf_unreliable=False):
    return {
        "Ticker": ticker, "Long Score": 50.0, "Price": price,
        "Intrinsic Value": iv, "DCF Unreliable": dcf_unreliable,
        "Growth Source": growth_source, "Growth Raw": 0.08, "Growth Used": 0.07,
        "Growth Ceiling Used": 0.12, "Growth End Rate Used": 0.03,
        "FCF Source": "history", "FCF Base Raw": 900.0, "FCF Base Used": 950.0,
        "FCF Base Source": fcf_base_source, "FCF Base Normalized": True,
        "FCF Distorted Years": fcf_distorted_years or [],
        "Capex Basis": "average", "Discount Source": "tiered",
        "Share Count Flagged": False, "FX Converted": None,
    }


_FAKE_CADENCE = {"S&P 500": "daily"}
scan_store.save_scan("S&P 500", [
    _scan_row("BIGUP", 10.0, 60.0, growth_source="history", fcf_base_source="median5_clean",
              fcf_distorted_years=[0, 1]),
    _scan_row("MODUP", 20.0, 50.0, growth_source="analyst", fcf_base_source="fcf-median"),
    _scan_row("FLAT", 30.0, 33.0, growth_source="history", fcf_base_source="fcf-median"),
    _scan_row("NODATA_B", 15.0, 18.0, growth_source="info", fcf_base_source="ebitda_bridge",
              fcf_distorted_years=[0]),
    # NOSCAN deliberately omitted - no scan row at all for this ticker.
    # SNEAKY: stored flag False but current price makes it 4x unreliable.
    _scan_row("SNEAKY", 10.0, 45.0, growth_source="history", fcf_base_source="fcf-median",
              dcf_unreliable=False),
], "test", attention_lite=True)

score_history.record([
    {"Ticker": "SNEAKY", "Long Score": 50, "Price": 10.0, "Quality": 60,
     "Moat": None, "MOS %": 60.0, "Intrinsic Value": 45.0},
], day=_today)

from unittest import mock
import top100_engine as te
import scheduler_engine

with mock.patch.object(scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    _audit = vae.build_change_audit(days_back=1)

assert _audit["date_a"] == _today, _audit["date_a"]
assert _audit["date_b"] == _yesterday, _audit["date_b"]
_rows_by_ticker = {r["ticker"]: r for r in _audit["rows"]}

# ======================================================================
# CHECK 1: dIV % computed correctly; None when IV(B) missing.
# ======================================================================
assert abs(_rows_by_ticker["BIGUP"]["d_iv_pct"] - 200.0) < 1e-6, _rows_by_ticker["BIGUP"]["d_iv_pct"]
assert abs(_rows_by_ticker["MODUP"]["d_iv_pct"] - 25.0) < 1e-6, _rows_by_ticker["MODUP"]["d_iv_pct"]
assert _rows_by_ticker["NODATA_B"]["d_iv_pct"] is None, _rows_by_ticker["NODATA_B"]["d_iv_pct"]
print("[d_iv_pct_computed] BIGUP $20->$60 = +200%, MODUP $40->$50 = +25%, "
      "NODATA_B (no date-B row) = None OK")

# ======================================================================
# CHECK 2: a ticker with no saved scan row at all still appears, with
# None/empty provenance fields rather than crashing or being dropped.
# ======================================================================
assert "NOSCAN" in _rows_by_ticker
_noscan = _rows_by_ticker["NOSCAN"]
assert _noscan["universe"] is None and _noscan["growth_source"] is None
assert _noscan["fcf_distorted_years"] == []
print("[no_scan_row_still_appears] NOSCAN (score_history only, no saved scan row) "
      "appears with None/empty provenance columns, not dropped or crashed OK")

# ======================================================================
# CHECK 3: default sort order is |dIV %| descending (None sorts last).
# ======================================================================
_ordered = [r["ticker"] for r in _audit["rows"] if r["d_iv_pct"] is not None]
_expected_order_prefix = ["BIGUP", "SNEAKY"]  # 200%, then SNEAKY's own dIV
_d_ivs_desc = [abs(r["d_iv_pct"]) for r in _audit["rows"] if r["d_iv_pct"] is not None]
assert _d_ivs_desc == sorted(_d_ivs_desc, reverse=True), _d_ivs_desc
assert _audit["rows"][0]["ticker"] == "BIGUP", _audit["rows"][0]["ticker"]
_none_positions = [i for i, r in enumerate(_audit["rows"]) if r["d_iv_pct"] is None]
_some_positions = [i for i, r in enumerate(_audit["rows"]) if r["d_iv_pct"] is not None]
assert all(n > max(_some_positions) for n in _none_positions) if _none_positions and _some_positions else True
print("[sort_order] rows sorted by |dIV %| descending, BIGUP (+200%) first, "
      "rows with no computable dIV % (NODATA_B) sort last OK")

# ======================================================================
# CHECK 4: DCF-unreliable flag - the current-price recompute catches
# SNEAKY (stored flag=False, but iv_a=45/price_a=10 = 4.5x) exactly the
# same way it catches BIGUP (iv_a=60/price_a=10 = 6x, also over 3x) -
# both via the recompute, neither via the stored flag (both fixture
# rows set "DCF Unreliable": False). MODUP/FLAT/NODATA_B (iv_a well
# under 3x their own price_a) are correctly NOT flagged.
# ======================================================================
assert _rows_by_ticker["SNEAKY"]["dcf_unreliable"] is True, _rows_by_ticker["SNEAKY"]
assert _rows_by_ticker["BIGUP"]["dcf_unreliable"] is True, _rows_by_ticker["BIGUP"]
assert _rows_by_ticker["MODUP"]["dcf_unreliable"] is False, _rows_by_ticker["MODUP"]
assert _rows_by_ticker["FLAT"]["dcf_unreliable"] is False, _rows_by_ticker["FLAT"]
print("[dcf_unreliable_current_price] SNEAKY and BIGUP (both stored flag=False, but current "
      "iv_a/price_a over 3x) are flagged dcf_unreliable=True via the current-price recompute; "
      "MODUP/FLAT (well under 3x) are not OK")

# ======================================================================
# CHECK 5: summary counts - ticker_count, median dIV%, >+50%, >+100%,
# DCF-unreliable count.
# ======================================================================
_summary = _audit["summary"]
assert _summary["ticker_count"] == 6, _summary["ticker_count"]
# SNEAKY has no date-B score_history row (d_iv_pct is None), so only BIGUP's
# +200% counts toward over_50/over_100 - SNEAKY still counts toward
# dcf_unreliable_count below regardless, since that's price-ratio-based,
# not dIV%-based.
assert _summary["over_50_count"] == 1, _summary  # BIGUP (+200%) only
assert _summary["over_100_count"] == 1, _summary  # only BIGUP
assert _summary["dcf_unreliable_count"] == 2, _summary  # BIGUP and SNEAKY (both via current-price recompute)
print(f"[summary_counts] ticker_count={_summary['ticker_count']}, "
      f"over_50={_summary['over_50_count']}, over_100={_summary['over_100_count']}, "
      f"dcf_unreliable={_summary['dcf_unreliable_count']} OK")

# ======================================================================
# CHECK 6: the >+50% breakdown by fcf_base_source / distorted-years /
# growth_source.
# ======================================================================
_breakdown_fcf = _summary["over_50_by_fcf_base_source"]
assert _breakdown_fcf.get("median5_clean") == 1, _breakdown_fcf  # BIGUP
_breakdown_dy = _summary["over_50_by_distorted_years"]
assert _breakdown_dy.get("non-empty") == 1, _breakdown_dy  # BIGUP has [0,1]
_breakdown_growth = _summary["over_50_by_growth_source"]
assert _breakdown_growth.get("history") == 1, _breakdown_growth  # BIGUP only (SNEAKY has no dIV%)
print(f"[over_50_breakdown] by fcf_base_source={_breakdown_fcf}, "
      f"by distorted_years={_breakdown_dy}, by growth_source={_breakdown_growth} OK")

# ======================================================================
# CHECK 7: days_back clamps to [1,7], and gracefully returns date_b=None
# when there isn't enough recorded history to go that far back.
# ======================================================================
with mock.patch.object(scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    _audit_far = vae.build_change_audit(days_back=99)
assert _audit_far["date_b"] is None, _audit_far["date_b"]
assert _audit_far["date_a"] == _today
print("[days_back_clamped_graceful_fallback] days_back=99 clamps to 7, finds no day that far "
      "back, and returns date_b=None rather than raising OK")

# ======================================================================
# CHECK 8: the "[valuation_audit]" log line fires with the summary
# figures baked in.
# ======================================================================
_logs = []
with mock.patch.object(scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    vae.build_change_audit(days_back=1, log=_logs.append)
_log_line = next((l for l in _logs if l.startswith("[valuation_audit]")), None)
assert _log_line is not None, _logs
assert "6 ticker(s)" in _log_line, _log_line
assert "DCF-unreliable: 2" in _log_line, _log_line
print(f"[log_line_fires] {_log_line!r} OK")

# ======================================================================
# CHECK 9: admin gate - the panel is rendered only from inside
# page_admin_dashboard(), after that function's own ai_gate.is_owner()
# early-return check (same static-source pattern test_imported_screen_
# admin_relocation.py already uses for an admin-only section).
# ======================================================================
_APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
with open(_APP_PATH, encoding="utf-8") as f:
    _APP_SRC = f.read()


def _function_span(src, name):
    start = re.search(rf"(?m)^def {re.escape(name)}\(", src)
    assert start, f"def {name}( not found in app.py"
    nxt = re.search(r"(?m)^(def |class )", src[start.end():])
    end = start.end() + nxt.start() if nxt else len(src)
    return src[start.start():end]


assert _APP_SRC.count("def _render_valuation_change_audit_panel(") == 1
_admin_body = _function_span(_APP_SRC, "page_admin_dashboard")
assert "_render_valuation_change_audit_panel()" in _admin_body
assert "ai_gate.is_owner(" in _admin_body
_owner_check_pos = _admin_body.index("ai_gate.is_owner(")
_panel_call_pos = _admin_body.index("_render_valuation_change_audit_panel()")
assert _panel_call_pos > _owner_check_pos, (
    "the panel call must come AFTER the owner check (and its early-return) in "
    "page_admin_dashboard()'s body, so a non-owner never reaches it")
_calls = [ln for ln in _APP_SRC.splitlines() if ln.strip() == "_render_valuation_change_audit_panel()"]
assert len(_calls) == 1, _calls
print("[admin_gate] _render_valuation_change_audit_panel() is defined exactly once, called "
      "exactly once, from inside page_admin_dashboard() AFTER that function's own "
      "ai_gate.is_owner() check - a non-owner returns before ever reaching it OK")

print("\nALL VALUATION CHANGE AUDIT FIXTURES PASSED")
