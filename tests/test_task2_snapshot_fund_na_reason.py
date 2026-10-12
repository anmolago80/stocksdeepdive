"""
Director's overnight queue, TASK 2 (12 Oct 2026): the public snapshot
page (/s/<ticker>) shows a bare "-" for a missing Intrinsic Value,
with no reason - unlike the Scanner table, which already shows a
plain reason for a FUND row (nightly_scan.py's own "Is Fund"/"Fund
Reason" fields, wired into the Scanner's Intrinsic Value cell by the
PUSH 2 follow-up commit, app.py's own na_reason= wiring at its
_money_cell() call site).

Display only - reuses the EXISTING stored "Fund Reason" string
(already plain English, already computed by nightly_scan.py, never
re-derived here) via snapshot_store._PUBLIC_FIELD_MAP (pure field-name
whitelisting, the same mechanism every other public field already
uses) and snapshot_render._stat_cells()'s own pre-existing "hint"
slot (the third tuple element, already used for Quality's "estimated"
note, Discovery's "price/volume attention only" note, moat's erosion
note) - no new UI concept, no new computation, nothing new stored.

IMPORTANT SCOPE FINDING, proven by CHECK 3 below rather than assumed:
a STLAP.PA-style row (Intrinsic Source == "pe-blend", Intrinsic Value
None, not a fund) has NO existing reason anywhere in this codebase to
reuse - auto_intrinsic_value_engine.get_intrinsic_value()'s own
docstring states this explicitly: "Returns (0, False) when there's no
positive EPS to value at all (the caller then shows N/A, which is not
a default - it's an honest 'unknown')" - no reason string is computed
or stored for this case, on the Scanner OR anywhere else (the
Scanner's own na_reason wiring in app.py only ever covers Is Fund and
price_unit_suspect - confirmed by reading that exact call site). So
this task's own "reuse the existing na_reason logic; do not write a
second one" rule means a pe-blend/no-value row is correctly LEFT
UNCHANGED here (still a bare "-", with no hint) - not a bug, not
something this commit should invent a new reason for.

price_unit_suspect is NOT wired here either, for a separate reason:
the Scanner's own version of that reason goes through
app.py._na_reason_for_viewer() (an owner/non-owner plain-words
translation, keyed on ai_gate.is_owner() - a Streamlit-session-only
concept). snapshot_render.py runs in the FastAPI process, a separate
process from the Streamlit app.py subprocess (see CLAUDE.md's own
architecture notes) - importing app.py there to reuse that one
function is not a safe or available option, and duplicating its
translation table would be writing a second one, which this task
explicitly forbids. Left unwired, flagged in the commit report for
the Director's decision, not silently built around either way.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_task2_snapshot_fund_na_reason.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshot_render
import snapshot_store

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


def _snap(ticker, data, universe="FTSE 100"):
    return {
        "ticker": ticker, "universe": universe, "data": data, "moat": None,
        "generated_at": "2026-10-12T03:00:00",
    }


# ======================================================================
# CHECK 1-2: a FUND row (STLAP.PA-shaped fixture's sibling - a real
# "Is Fund" row with a stored "Fund Reason") shows the SAME plain
# reason the Scanner already shows, where today it shows a bare "-".
# ======================================================================
_fund_data = {
    "Company Name": "Example Fund Co", "Price": 10.0, "Intrinsic Value": None,
    "MOS %": None, "Quality": None, "Psychology": 5.0, "Discovery (lite)": 30.0,
    "Long Score": None, "Valuation": None, "Type": "Fund",
    "Is Fund": True, "Fund Reason": "FNDX.PA is an investment fund - no company valuation is computed for funds.",
}
_fund_html = snapshot_render.render_snapshot(_snap("FNDX.PA", _fund_data), "https://stocksdeepdive.com")
check("BEFORE/AFTER: a fund row's Intrinsic value cell shows the plain-English 'Fund Reason' "
      "as a hint, reusing the exact stored string (no new text invented)",
      "FNDX.PA is an investment fund - no company valuation is computed for funds." in _fund_html)
check("the fund row's Intrinsic value itself still renders as '-' (value withheld, same as "
      "today) - only the EXPLANATION is new, the number/absence-of-number is unchanged",
      '<div class="val">-</div>' in _fund_html.replace("&#x27;", "'"))


# ======================================================================
# CHECK 3: a STLAP.PA-style pe-blend/no-value row (NOT a fund) is
# correctly LEFT UNCHANGED - no reason exists anywhere to reuse for
# this case (proven, not assumed - see auto_intrinsic_value_engine.
# get_intrinsic_value()'s own docstring), so this must still render a
# bare "-" with no hint, exactly as before this commit.
# ======================================================================
_peblend_data = {
    "Company Name": "Example SA", "Price": 42.0, "Intrinsic Value": None,
    "MOS %": None, "Quality": 55.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
    "Long Score": 40.0, "Valuation": None, "Type": "Operating company",
    "Is Fund": False, "Intrinsic Source": "pe-blend",
}
_peblend_html = snapshot_render.render_snapshot(_snap("STLAX.PA", _peblend_data), "https://stocksdeepdive.com")
check("a pe-blend/no-value, non-fund row's Intrinsic value still renders '-' with no hint "
      "div at all next to it - correctly unchanged, since no reason is computed or stored "
      "for this case anywhere in the codebase (not invented here)",
      '<div class="lbl">Intrinsic value</div><div class="val">-</div></div>' in
      _peblend_html.replace("&#x27;", "'"))


# ======================================================================
# CHECK 4: regression - a normal row with a real Intrinsic Value is
# completely unaffected (no hint leaking in where a real number
# renders).
# ======================================================================
_normal_data = {
    "Company Name": "Example PLC", "Price": 100.0, "Intrinsic Value": 120.0,
    "MOS %": 16.7, "Quality": 70.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
    "Long Score": 65.0, "Valuation": "Undervalued", "Type": "Operating company",
    "Is Fund": False,
}
_normal_html = snapshot_render.render_snapshot(_snap("NORM.L", _normal_data), "https://stocksdeepdive.com")
check("a normal row's Intrinsic value cell renders the real number with no stray hint",
      '<div class="lbl">Intrinsic value</div><div class="val">120</div></div>' in _normal_html)


# ======================================================================
# CHECK 5-6: snapshot_store.public_view() exposes "Is Fund"/"Fund
# Reason" under their public names, by pure presence-based whitelisting
# (no truthiness filtering - a False "Is Fund" must still come through).
# ======================================================================
_pub_fund = snapshot_store.public_view(_fund_data)
check("public_view() exposes is_fund=True and the fund_reason string verbatim",
      _pub_fund.get("is_fund") is True
      and _pub_fund.get("fund_reason") == _fund_data["Fund Reason"])
_pub_normal = snapshot_store.public_view(_normal_data)
check("public_view() exposes is_fund=False (presence-based, not truthiness-filtered) for a "
      "non-fund row, and omits fund_reason entirely (never present on that row)",
      _pub_normal.get("is_fund") is False and "fund_reason" not in _pub_normal)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
