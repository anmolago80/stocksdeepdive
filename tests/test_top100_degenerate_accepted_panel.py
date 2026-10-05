"""
Top 200 Commit A2 (5 Oct 2026, Director-directed, owner-approved 5 Oct -
"owner button to re-send the companies already locked out").

Companies saved as NOT RATED with degenerate_accepted=True (CL, WDAY
and PIC.AX on 5 Oct, and earlier ones) were produced by the blank-
request fault A1 fixes going forward - they will not be scored again
until their next quarterly result or 200 days unless an owner clears
them. This file covers the engine-side helpers behind the Admin panel
(app.py's _render_degenerate_accepted_panel(), not exercised here since
it is pure Streamlit glue with no logic of its own beyond what these
functions already return):

  - top100_engine.degenerate_accepted_rows(): lists ONLY degenerate_
    accepted rows for the current rubric; a genuine NOT RATED (three
    or more zero dimensions with real justifications, degenerate_
    accepted False) is never listed.
  - top100_engine.estimate_degenerate_resend_cost_usd(): a positive
    cost estimate for a non-empty list, 0.0 for an empty one.
  - top100_engine.clear_degenerate_accepted_rows(): deletes EXACTLY
    the rows it was given (not a re-query at click time), via the same
    top100_store.delete_score() path the 3 Oct sweep used, logs the
    exact owner-specified line, triggers no batch.
  - unreachable for non-owner: confirmed by reading app.py's own
    page_admin_dashboard() gate (ai_gate.is_owner() checked once, at
    the top of the page, before any panel - including this one - is
    reached) - a grep-verify, not a fixture, since AppTest-driving the
    full Admin Dashboard page is out of scope for this file.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_top100_degenerate_accepted_panel.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_degen_panel_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _dims(score=4, justification="j"):
    return {k: {"score": score, "justification": justification, "source_period": "FY25"}
            for k in te.DIMENSION_KEYS}


# A pool snapshot so degenerate_accepted_rows() can resolve company
# names (and so a dropped-out-of-pool ticker's fallback is exercised
# below).
ts.save_pool(
    [
        {"ticker": "DEGA1", "company_name": "Dega One Co", "universe": "us",
         "value_score": 50.0, "mos_pct": 0.1, "price": 10.0, "intrinsic_value": 11.0,
         "currency": "USD", "psychology": None, "sector": "Tech", "dividend_yield_pct": None},
        {"ticker": "LEGIT1", "company_name": "Legit One Co", "universe": "us",
         "value_score": 60.0, "mos_pct": 0.1, "price": 10.0, "intrinsic_value": 11.0,
         "currency": "USD", "psychology": None, "sector": "Tech", "dividend_yield_pct": None},
    ],
    "2026-10-05",
)

# A degenerate_accepted=True row (produced exactly as _save_degenerate_
# as_not_rated() produces one) - the thing this panel exists to list.
ts.save_score(
    ticker="DEGA1", quarter="D2026-10-05-DEGA1", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION,
    dims={k: {"score": None, "justification": "", "source_period": None} for k in te.DIMENSION_KEYS},
    not_rated=True, inversion_scenario=None, inversion_severity=None,
    degenerate_accepted=True, prompt="{}", raw_response="{}",
)
# A ticker no longer in the current pool, but still carrying an old
# degenerate_accepted row - must still be listed (company_name falls
# back to the ticker itself).
ts.save_score(
    ticker="DROPPEDOUT1", quarter="D2026-10-04-DROPPEDOUT1", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION,
    dims={k: {"score": None, "justification": "", "source_period": None} for k in te.DIMENSION_KEYS},
    not_rated=True, inversion_scenario=None, inversion_severity=None,
    degenerate_accepted=True, prompt="{}", raw_response="{}",
)
# A GENUINE NOT RATED row (>=3 zero dims, real justifications,
# degenerate_accepted False) - must NEVER be listed or deleted.
_legit_dims = _dims(score=0, justification="")
_legit_dims["ai_exposure"] = {"score": 3, "justification": "genuine partial signal", "source_period": "FY25"}
ts.save_score(
    ticker="LEGIT1", quarter="D2026-10-05-LEGIT1", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION, dims=_legit_dims,
    not_rated=True, inversion_scenario=None, inversion_severity=None,
    degenerate_accepted=False, prompt="{}", raw_response="{}",
)
# An ordinary, real score - must never be listed or deleted either.
ts.save_score(
    ticker="REALSCORE1", quarter="D2026-10-05-REALSCORE1", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION, dims=_dims(score=4, justification="solid"),
    not_rated=False, inversion_scenario="some scenario", inversion_severity=2,
    degenerate_accepted=False, prompt="{}", raw_response="{}",
)

# ======================================================================
# CHECK 1: degenerate_accepted_rows() lists ONLY the degenerate_
# accepted rows, with company_name resolved from the pool when
# available and falling back to the ticker itself otherwise.
# ======================================================================
_rows = te.degenerate_accepted_rows(te.MODEL_TOP100)
_tickers = {r["ticker"] for r in _rows}
assert _tickers == {"DEGA1", "DROPPEDOUT1"}, _tickers
_by_ticker = {r["ticker"]: r for r in _rows}
assert _by_ticker["DEGA1"]["company_name"] == "Dega One Co", _by_ticker["DEGA1"]
assert _by_ticker["DROPPEDOUT1"]["company_name"] == "DROPPEDOUT1", _by_ticker["DROPPEDOUT1"]
print("[degenerate_accepted_rows_lists_only_matching] only DEGA1/DROPPEDOUT1 listed (never "
      "LEGIT1 or REALSCORE1); company_name resolved from the pool or falls back to the "
      "ticker OK")

# ======================================================================
# CHECK 2: estimate_degenerate_resend_cost_usd() - positive for a non-
# empty list, exactly 0.0 for an empty one.
# ======================================================================
_cost = te.estimate_degenerate_resend_cost_usd(_rows)
assert _cost > 0, _cost
assert te.estimate_degenerate_resend_cost_usd([]) == 0.0
print("[estimate_degenerate_resend_cost] positive estimate for 2 rows, exactly 0.0 for an "
      "empty list OK")

# ======================================================================
# CHECK 3: clear_degenerate_accepted_rows() deletes EXACTLY the rows
# it was given (DEGA1, DROPPEDOUT1), never LEGIT1/REALSCORE1, logs the
# exact owner-specified line, and never touches the score_failures
# table (no batch is triggered by this action).
# ======================================================================
_log = []
_cleared = te.clear_degenerate_accepted_rows(_rows, te.MODEL_TOP100, log=_log.append)
assert sorted(_cleared) == ["DEGA1", "DROPPEDOUT1"], _cleared
assert ts.get_score("DEGA1", "D2026-10-05-DEGA1", te.MODEL_TOP100, te.RUBRIC_VERSION) is None
assert ts.get_score("DROPPEDOUT1", "D2026-10-04-DROPPEDOUT1", te.MODEL_TOP100, te.RUBRIC_VERSION) is None
assert ts.get_score("LEGIT1", "D2026-10-05-LEGIT1", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None, (
    "a genuine NOT RATED row must never be deleted by this action")
assert ts.get_score("REALSCORE1", "D2026-10-05-REALSCORE1", te.MODEL_TOP100, te.RUBRIC_VERSION) is not None, (
    "an ordinary real score must never be deleted by this action")
assert any(
    re.match(r"\[top100\] owner cleared 2 degenerate-accepted row\(s\): DEGA1, DROPPEDOUT1", ln)
    for ln in _log
), _log
assert ts.get_batch_state() is None, "this action must never submit or touch a batch"
print("[clear_degenerate_accepted_rows] deletes exactly DEGA1/DROPPEDOUT1, never LEGIT1/"
      "REALSCORE1, exact log line present, no batch touched OK")

# A second call with an already-empty list (what the panel would do if
# clicked again after clearing) is a no-op that logs nothing.
_log2 = []
_cleared2 = te.clear_degenerate_accepted_rows([], te.MODEL_TOP100, log=_log2.append)
assert _cleared2 == [] and _log2 == [], (_cleared2, _log2)
print("[clear_empty_noop] clearing an empty list is a complete no-op (nothing deleted, "
      "nothing logged) OK")

# ======================================================================
# CHECK 4: unreachable for non-owner - grep-verify app.py's own page_
# admin_dashboard() gates the WHOLE page (including this panel) behind
# ai_gate.is_owner(), before any panel is rendered.
# ======================================================================
_app_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")).read()
_page_start = _app_src.index("def page_admin_dashboard():")
_page_body = _app_src[_page_start:]  # page_admin_dashboard() is the file's last top-level def
assert "ai_gate.is_owner(paywall_engine.current_user_email())" in _page_body, (
    "page_admin_dashboard() must gate on ai_gate.is_owner() near its own top")
_owner_check_pos = _page_body.index("ai_gate.is_owner(paywall_engine.current_user_email())")
assert "    _render_degenerate_accepted_panel()" in _page_body, (
    "_render_degenerate_accepted_panel() must be CALLED from inside page_admin_dashboard()")
_panel_call_pos = _page_body.index("    _render_degenerate_accepted_panel()")
assert _panel_call_pos > _owner_check_pos, (
    "_render_degenerate_accepted_panel() must be called AFTER the owner gate")
print("[unreachable_for_non_owner] page_admin_dashboard() checks ai_gate.is_owner() before "
      "_render_degenerate_accepted_panel() is ever called - unreachable for a non-owner OK")

print("\nALL TOP 200 COMMIT A2 (DEGENERATE-ACCEPTED PANEL) FIXTURES PASSED")
