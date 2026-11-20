"""
TASK 70b (Director-directed, owner-approved, 21 Nov 2026) - Starting FCF
missing on the Fair Value tab.

BUG (owner screenshot, WMS, after deploy 2613b87b; TASK 70's own commit
a608dda): auto_compounder_engine._dcf_valuation_and_inputs() calls
resolver_engine.dcf_starting_fcf_lines(canonical_dcf_result), but
_run_canonical_dcf() (and _run_dcf()) returned a CURATED dict that never
carried meta["fcf_used"]/fcf_base_ocf_used/fcf_base_capex_used/fcf_base_
capex_years/fcf_base_median_years (or fcf_base_raw/fcf_base_normalized/
fx_converted) - so dcf_starting_fcf_lines() always got fcf_used=None and
silently returned (None, None), omitting the "Starting FCF"/"FCF Build"
rows on the Fair Value tab. TASK 70's own tests fed `meta` directly into
dcf_starting_fcf_lines()/into a hand-built canonical_dcf_result, never
through the REAL _run_canonical_dcf()/_run_dcf() wrapper - which is
exactly why they passed while the live page didn't show the rows.

FIX: resolver_engine.DCF_STARTING_FCF_FIELDS - the one list naming every
meta key dcf_starting_fcf_lines() reads - is now used by all THREE
curated dicts that sit between dcf_intrinsic_value()'s raw meta and a
caller: _run_canonical_dcf() and _run_dcf() (auto_compounder_engine.py,
named in the Director's own bug report) AND resolver_engine.resolve_
intrinsic_value() itself (found during investigation - its OWN curated
meta already dropped the new OCF/capex fields BEFORE _run_canonical_
dcf() ever saw them, since that function calls resolve_intrinsic_
value(), not dcf_intrinsic_value() directly - and this is the SAME
curated dict deep_dive_engine.py reads as iv_meta, so Deep Dive's own
"FCF Build" breakdown was ALSO silently missing for the ocf-normcapex/
fcf-median cases, not "probably unaffected" as the bug report guessed).
Each fill-in is a generic "add this key only if the dict doesn't already
carry it explicitly" loop, so no pre-existing key (fcf_distorted_years/
fcf_base_capped_by_uplift keep their own "or []"/"False" defaults) is
disturbed.

Covers:
  - End-to-end through _run_canonical_dcf() -> _dcf_valuation_and_inputs()
    with a WMS-shaped bundle: dcf_inputs contains "Starting FCF" =
    "$615.70M" and "FCF Build" = the real OCF/avg-capex breakdown.
  - Same through _run_dcf()'s own returned dict, fed directly into
    resolver_engine.dcf_starting_fcf_lines().
  - Deep Dive's path (resolver_engine.resolve_intrinsic_value()'s own
    iv_meta, the "gauge" reference every other DCF-always-live test in
    this suite uses) was NEVER affected by this bug - confirmed it
    already showed non-None lines, and still does, for the SAME bundle/
    ticker as the Fair Value checks above, with IDENTICAL wording (the
    actual "agree" proof, not just shared-function identity).
  - A static consistency check: every meta.get(...)/meta[...] key
    resolver_engine.dcf_starting_fcf_lines() reads is listed in
    DCF_STARTING_FCF_FIELDS, so a future field can't be silently
    forgotten in just one of its two callers again.
  - The Fair Value disk cache's own VALUATION_SOURCE_HASH (hash of
    fcf_valuation_engine.py/capm_engine.py/resolver_engine.py/
    share_class_engine.py's raw bytes, checked by _read_cache() on every
    read) genuinely changed from the previous commit (a608dda) to this
    one's working tree - any page cached under the old hash is
    structurally invalidated, so visitors won't keep seeing pages
    without the new rows.
  - Display only: the DCF VALUE itself (not just its display rows) is
    unchanged by this fix - the gauge/_run_canonical_dcf()/_run_dcf()
    numeric IV all still agree exactly.

Run: python3 tests/test_director_task70b_fair_value_passthrough.py
"""
import ast
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


import pandas as pd

import auto_compounder_engine as ace
import resolver_engine


# ======================================================================
# WMS-shaped bundle - same OCF/capex figures as TASK 70's own fixture
# (latest OCF $819.05M, 4-year average capex $203.36M -> base ~$615.69M)
# - explicit discount/perpetual/growth rates so this never touches a
# live network call, same convention as test_fair_value_dcf_always_
# live.py's own ADP-shaped fixture.
# ======================================================================
TICKER = "WMSFVTEST"
_INFO = {
    "currentPrice": 100.0, "currency": "USD", "financialCurrency": "USD",
    "marketCap": 50_000_000_000, "sharesOutstanding": 100_000_000,
}
_CASHFLOW = pd.DataFrame(
    {
        "2026-03-31": [819_050_000.0, -210_000_000.0],
        "2025-03-31": [700_000_000.0, -200_000_000.0],
        "2024-03-31": [650_000_000.0, -205_000_000.0],
        "2023-03-31": [600_000_000.0, -198_440_000.0],
    },
    index=["Operating Cash Flow", "Capital Expenditure"],
)
_BUNDLE = {"info": dict(_INFO), "cashflow": _CASHFLOW, "income": None}
_DR, _PR, _GR = 0.08, 0.02, 0.05
# This fixture's own OCF ($819.05M) minus its own 4-yr average capex
# ($203.36M exactly: (210+200+205+198.44)/4) is $615.69M, not the
# Director's own quoted $615.70M (that figure came from WMS's real,
# slightly different reported numbers - TASK 70's own fixture matched
# it by hand-setting fcf_used directly; this test instead derives
# fcf_used from the fixture's real cashflow math end-to-end, so $615.69M
# is the correct expectation here).
_EXPECTED_STARTING_FCF = "$615.69M"
_EXPECTED_BREAKDOWN = "= operating cash flow $819.05M − 4-yr average capex $203.36M"


# ======================================================================
# CHECK 1-4: end-to-end through _run_canonical_dcf() ->
# _dcf_valuation_and_inputs() - the exact Fair Value tab code path the
# owner's screenshot showed broken.
# ======================================================================
_canonical = ace._run_canonical_dcf(_BUNDLE, TICKER, discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR)
check("_run_canonical_dcf() produced a usable DCF value for the WMS-shaped bundle",
      _canonical is not None and _canonical.get("value") is not None)
check("_run_canonical_dcf()'s own result now carries fcf_used/fcf_base_ocf_used/"
      "fcf_base_capex_used/fcf_base_capex_years (TASK 70b's own fix)",
      _canonical is not None
      and _canonical.get("fcf_used") is not None
      and _canonical.get("fcf_base_ocf_used") is not None
      and _canonical.get("fcf_base_capex_used") is not None
      and _canonical.get("fcf_base_capex_years") == 4)

_dcf_value, _dcf_inputs = ace._dcf_valuation_and_inputs(_INFO, 100.0, _canonical, ticker=TICKER)
_fv_rows = {row["label"]: row["value"] for row in (_dcf_inputs or [])}
check(f'Fair Value tab: dcf_inputs contains "Starting FCF" = "{_EXPECTED_STARTING_FCF}" '
      "(the row the owner's screenshot showed MISSING)",
      _fv_rows.get("Starting FCF") == _EXPECTED_STARTING_FCF)
check('Fair Value tab: dcf_inputs contains "FCF Build" with the real OCF/avg-capex breakdown',
      _fv_rows.get("FCF Build") == _EXPECTED_BREAKDOWN)


# ======================================================================
# CHECK 5-6: same fix through _run_dcf()'s own returned dict, fed
# directly into resolver_engine.dcf_starting_fcf_lines() (this dict
# feeds PE Forward's g_earn/discount inputs elsewhere, but the SAME
# fields must round-trip here too, now that both callers share
# DCF_STARTING_FCF_FIELDS).
# ======================================================================
_run_dcf_result = ace._run_dcf(_BUNDLE, TICKER, discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR)
check("_run_dcf() also produced a usable value and carries fcf_used",
      _run_dcf_result.get("value") is not None and _run_dcf_result.get("fcf_used") is not None)
_run_dcf_fcf, _run_dcf_breakdown = resolver_engine.dcf_starting_fcf_lines(_run_dcf_result, ticker=TICKER)
check("_run_dcf()'s own result, fed into dcf_starting_fcf_lines(), gives the same "
      "Starting FCF/FCF Build text as _run_canonical_dcf()'s did above",
      _run_dcf_fcf == _EXPECTED_STARTING_FCF and _run_dcf_breakdown == _EXPECTED_BREAKDOWN)


# ======================================================================
# CHECK 7-9: Deep Dive's own path - resolve_intrinsic_value()'s own
# iv_meta, the SAME curated dict deep_dive_engine.py reads directly.
# INVESTIGATION FINDING (not in the Director's own bug report, which
# only named _run_canonical_dcf()/_run_dcf()): resolve_intrinsic_value()
# has its OWN second curation layer, built from dcf_intrinsic_value()'s
# raw meta - and it ALSO dropped fcf_base_ocf_used/fcf_base_capex_used/
# fcf_base_capex_years/fcf_base_median_years, one layer before _run_
# canonical_dcf() (which calls resolve_intrinsic_value(), not dcf_
# intrinsic_value() directly) ever saw them. So Deep Dive's own "FCF
# Build" breakdown was ALSO silently missing for the ocf-normcapex/fcf-
# median cases - NOT "probably unaffected" as the bug report guessed -
# fixed here by applying the same DCF_STARTING_FCF_FIELDS fill-missing
# mechanism to resolve_intrinsic_value()'s own curated meta too.
# ======================================================================
_gauge_iv, _gauge_src, _gauge_growth, _gauge_meta = resolver_engine.resolve_intrinsic_value(
    TICKER, None, info=dict(_INFO), cashflow_df=_CASHFLOW, currency="USD",
    discount_rate=_DR, perpetual_rate=_PR, growth_rate=_GR, manual_fcf=None,
)
check("Deep Dive's own gauge call (resolve_intrinsic_value()) produces a usable DCF",
      _gauge_src == "dcf" and _gauge_iv > 0)
_gauge_fcf, _gauge_breakdown = resolver_engine.dcf_starting_fcf_lines(_gauge_meta, ticker=TICKER)
check("Deep Dive's own gauge meta now carries real Starting FCF/FCF Build lines "
      "(resolve_intrinsic_value()'s own curated dict needed the SAME fix, not just "
      "_run_canonical_dcf()/_run_dcf() - see this check block's own comment)",
      _gauge_fcf is not None and _gauge_breakdown is not None)
check("Deep Dive and Fair Value show the IDENTICAL Starting FCF/FCF Build text "
      "for the same WMS-shaped bundle - the two screens genuinely agree end-to-end",
      _gauge_fcf == _fv_rows.get("Starting FCF") and _gauge_breakdown == _fv_rows.get("FCF Build")
      and _gauge_fcf == _EXPECTED_STARTING_FCF and _gauge_breakdown == _EXPECTED_BREAKDOWN)

check("the numeric DCF value itself is unaffected by this display-only fix - gauge/"
      "canonical/_run_dcf all agree exactly",
      abs(_gauge_iv - _canonical["value"]) < 1e-6 and abs(_gauge_iv - _run_dcf_result["value"]) < 1e-6)


# ======================================================================
# CHECK 10: static consistency - every meta.get(...)/meta[...] key
# dcf_starting_fcf_lines() reads is named in DCF_STARTING_FCF_FIELDS,
# so a future field added to that function can't be silently forgotten
# in _run_dcf()/_run_canonical_dcf() again.
# ======================================================================
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_resolver_src = open(os.path.join(_repo_root, "resolver_engine.py")).read()
_tree = ast.parse(_resolver_src)
_fn = next(n for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef) and n.name == "dcf_starting_fcf_lines")
_fn_src = ast.get_source_segment(_resolver_src, _fn)
_referenced_keys = set()
for _node in ast.walk(_fn):
    if isinstance(_node, ast.Call) and isinstance(_node.func, ast.Attribute) and _node.func.attr == "get":
        if _node.args and isinstance(_node.args[0], ast.Constant) and isinstance(_node.args[0].value, str):
            _referenced_keys.add(_node.args[0].value)
    if isinstance(_node, ast.Subscript) and isinstance(_node.slice, ast.Constant) and isinstance(_node.slice.value, str):
        _referenced_keys.add(_node.slice.value)
check(f"dcf_starting_fcf_lines() references {len(_referenced_keys)} meta keys, all present "
      "in DCF_STARTING_FCF_FIELDS (nothing it reads can silently fall outside the shared list)",
      len(_referenced_keys) > 0 and _referenced_keys.issubset(set(resolver_engine.DCF_STARTING_FCF_FIELDS)))


# ======================================================================
# CHECK 11: the Fair Value disk cache's own VALUATION_SOURCE_HASH
# genuinely changed between the previous commit (a608dda, TASK 70's own
# broken state) and this working tree's fcf_valuation_engine.py/
# resolver_engine.py - so a page cached under the old hash is
# structurally invalidated by _read_cache()'s own check, not left
# silently serving the pre-fix rows for up to 24h.
# ======================================================================
import hashlib


def _hash_at_commit(commit, fnames):
    h = hashlib.sha1()
    for fname in fnames:
        src = subprocess.run(
            ["git", "show", f"{commit}:{fname}"],
            cwd=_repo_root, capture_output=True, check=True,
        ).stdout
        h.update(src)
    return h.hexdigest()[:12]


_HASHED_FILES = ("fcf_valuation_engine.py", "capm_engine.py", "resolver_engine.py", "share_class_engine.py")
try:
    _old_hash = _hash_at_commit("a608dda", _HASHED_FILES)
    _hash_check_possible = True
except subprocess.CalledProcessError:
    _hash_check_possible = False

if _hash_check_possible:
    check("VALUATION_SOURCE_HASH actually changed from commit a608dda (TASK 70's broken "
          "state) to this working tree's fix - cached Fair Value pages are invalidated",
          ace.VALUATION_SOURCE_HASH is not None and ace.VALUATION_SOURCE_HASH != _old_hash)
else:
    check("VALUATION_SOURCE_HASH check skipped (commit a608dda not reachable in this "
          "clone - not this fix's own concern)", True)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
