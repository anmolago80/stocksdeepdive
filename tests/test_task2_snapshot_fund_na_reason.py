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

UPDATE (Director's work queue, TASK 2, 13 Oct 2026 - "plain reason for
'no value' rows on public pages"): at the time this file was first
committed, a STLAP.PA-style row (Intrinsic Source == "pe-blend",
Intrinsic Value None, not a fund) genuinely had no reason anywhere in
this codebase to reuse, so CHECK 3 below correctly proved it was LEFT
UNCHANGED (still a bare "-", no hint) rather than something that
commit should invent a reason for. The Director's follow-up task asked
for exactly that reason: snapshot_render._intrinsic_value_hint() now
covers it (a new, named, plain sentence - "no positive per-share
earnings...", reusing auto_intrinsic_value_engine.get_intrinsic_value()'s
own documented mechanism, never a guessed number) and price_unit_
suspect (reusing the EXISTING stored price_unit_suspect_reason string,
now whitelisted through snapshot_store._PUBLIC_FIELD_MAP same as every
other field). CHECK 3 below is UPDATED to prove the fix, same scenario,
flipped expectation - not a new test. Full enumeration and the before/
after for STLAP.PA/7201.T/6963.T-style fixtures live in the dedicated
file this task added, tests/test_director_task2_pe_blend_no_value_
reasons.py.

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
# CHECK 3: TASK 2 fix - a STLAP.PA-style pe-blend/no-value row (NOT a
# fund) now shows a plain, named reason ("no positive per-share
# earnings...") instead of a bare "-" with nothing next to it. Same
# scenario this file originally proved was unchanged; the expectation
# is flipped to prove the fix, not a new test.
# ======================================================================
_peblend_data = {
    "Company Name": "Example SA", "Price": 42.0, "Intrinsic Value": None,
    "MOS %": None, "Quality": 55.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
    "Long Score": 40.0, "Valuation": None, "Type": "Operating company",
    "Is Fund": False, "Intrinsic Source": "pe-blend",
}
_peblend_html = snapshot_render.render_snapshot(_snap("STLAX.PA", _peblend_data), "https://stocksdeepdive.com")
check("TASK 2 fix: a pe-blend/no-value, non-fund row's Intrinsic value cell now carries the "
      "plain 'no positive per-share earnings' hint instead of a bare, unexplained '-'",
      "no positive per-share earnings" in _peblend_html)
check("the value itself is still '-' (withheld, unchanged) - only the explanation is new",
      '<div class="val">-</div>' in _peblend_html.replace("&#x27;", "'"))


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
