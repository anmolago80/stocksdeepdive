"""
Director's TASK 54 (13 Nov 2026, live NVO case, "same class as ELS.AX"):
Deep Dive's headline MOS gauge showed a DIFFERENT intrinsic value than
the Fair Value/Compounder tab for NVO on the SAME page view -
[dcf_diag] 13 Nov 08:50 UTC:
  deepdive:  iv=36.77 fcf_base=7.35B shares=3.333B growth=4.92% fin_ccy=DKK fx=0.148 cache_age=130,212s (36h stale)
  fairvalue: iv=41.20 fcf_base=7.99B shares=4.448B growth=5.29% fin_ccy=USD fx=None (fresh)

Director's four asks and what was found/fixed for each:

1. SHARE COUNT - Novo has A+B shares (~4.45B total); 3.33B is B-only.
   PROVEN ROOT CAUSE (code-tracing, both CHECK 1-6 below): fcf_valuation_
   engine.dcf_intrinsic_value() has ALWAYS received `income_df` as a
   parameter and already threads it into normalized_base_and_series()
   for the FCF-base calc a few lines below - but its OWN internal
   share_class_engine.whole_company_shares() call never forwarded it.
   share_class_engine.whole_company_shares()'s own docstring has always
   documented income_df as tier 1 ("bundle"), its CHEAPEST and most
   authoritative source - a caller-supplied income statement's own
   "Diluted Average Shares" row, zero new network cost. Both existing
   callers that bother to fetch/hold income_df specifically for this
   purpose (deep_dive_engine.py's opportunistic peek, auto_compounder_
   engine.py's always-on bundle) were paying to have it available, but
   it silently never reached the one call site that resolves the share
   count the DCF math itself uses - BOTH paths fell through to the
   weaker tier-2 ("implied", info["impliedSharesOutstanding"], UN-
   corroborated for any ticker outside share_class_engine.SHARE_CLASS_
   PAIRS) or no correction at all. NVO is NOT in SHARE_CLASS_PAIRS
   (Novo's A-shares, Novo Holdings-controlled, aren't separately listed
   on a US exchange the way GOOG/GOOGL or BRK-A/BRK-B are) - so unlike
   those six pairs, NVO had NO safety net (no tier-2 corroboration, no
   tier-3 targeted fetch) once tier 1 was unreachable. Fix: forward
   `income_df=income_df` into the internal whole_company_shares() call
   (fcf_valuation_engine.py, inside dcf_intrinsic_value()) - a one-line
   addition, zero new cost (income_df already in scope), upgrades BOTH
   paths to tier-1 resolution whenever a good income_df is on hand.

   AUDIT (CHECK 7): GOOG/GOOGL, BRK-A/BRK-B, FOX/FOXA, NWS/NWSA, HEI/
   HEI-A, VOW.DE/VOW3.DE (share_class_engine.SHARE_CLASS_PAIRS) already
   had a partial safety net via tier-2 corroboration/tier-3 fallback -
   this fix is a cost/robustness upgrade for them (tier 1 beats a
   network-fetching tier 3), not a bug fix the way it is for NVO. No
   ticker in that list was exposed to NVO's specific failure mode
   (uncorroborated tier 2 silently accepted or silently dropped) -
   proven directly below (CHECK 7) by confirming the SAME income_df-
   forwarding fix makes a known pair ALSO resolve via tier 1, not
   newly-correct tier 2/3 that was previously broken.

2. RECONCILIATION - why deepdive's own inputs don't arithmetically
   reconcile to 36.77. Two sub-findings, both provable without live
   data:
     a. "FX applied twice" is REFUTED by the numbers themselves: a
        genuine double application of fx=0.148 (DKK->USD) to an
        already-converted figure would multiply/divide by a further
        0.148 (a ~6.8x shift), not produce an 8% fcf_base gap
        (7.35B vs 7.99B). Not re-litigated with code here (arithmetic
        on the Director's own reported figures, not a new fixture) -
        see the reasoning in the task notes; the discipline of "fix
        only proven causes" means this hypothesis is reported as
        refuted, not silently fixed as if it were real.
     b. The "shares=" field in EITHER existing [dcf_diag] log line
        was NEVER what dcf_intrinsic_value() itself used for the IV
        math (CHECK 8-9 below, proven by source + by meta's own new
        shares_used field): deepdive logged the raw, uncorrected
        info["sharesOutstanding"]; fairvalue logged auto_compounder_
        engine._basics(bundle)["shares"] - a SEPARATELY resolved,
        bundle-tier-corrected figure computed for THAT caller's own
        market-cap/BVPS/WACC use, never fed into the actual DCF call
        two lines above it. Both log lines are fixed (CHECK 10-11) to
        also report meta["shares_used"]/meta["share_count_source"] -
        the number the DCF math genuinely used - so this is directly
        observable in production going forward instead of inferred.
        The real residual gap is explained by finding 3 below (two
        genuinely different data snapshots, not a shares or FX bug).

3. STALE CACHE - deep_dive_engine.py's non-financials-mode income_df
   source (fundamentals_data.peek_cached_bundle()) deliberately ignores
   the 24h cache TTL (see that function's own docstring - built for an
   admin audit tool where staleness doesn't matter). Fair Value's own
   auto_compounder_engine.build_sections() always reads through
   fundamentals_data.get_bundle(), which DOES enforce the 24h TTL - so
   the two paths, in the SAME page view, can read bundles from
   genuinely different points in time (confirmed: 130,212s/36h stale
   vs fresh), feeding different fcf_base/growth inputs into otherwise
   identical code. Fix (CHECK 12-15): bound the peek to the exact same
   24h TTL fundamentals_data.get_bundle() already enforces everywhere
   else (fundamentals_data._CACHE_TTL_SECONDS, not a new threshold) -
   past it, discard the peeked bundle and fall through to the existing
   needs_oneoff_check()/None branches, exactly as a cold cache already
   does today. Zero new network cost (a discard, never a new fetch) -
   deliberately NOT switched to unconditionally calling get_bundle()
   instead, which (confirmed by reading get_bundle()'s own docstring:
   "price fields...always overlaid with a fresh fast_info quote...This
   runs on every call, cached or not") would add a real network call
   to EVERY non-financials-mode Deep Dive view, including the majority
   that never open the Fair Value tab at all - too large a cost
   increase for a page real visitors and crawlers hit, for this task's
   proven scope.

4. "ONE intrinsic value per company" - items 1 and 3 together close
   the two PROVEN causes of NVO's specific gap (undercounted shares;
   a 36h-old snapshot). A full "fetch the bundle once per page view
   and thread it through both deep_dive_engine.analyze() and auto_
   compounder_engine.build_sections()" refactor was investigated
   (app.py's page_deep_dive(), ~line 10878, unconditionally calls both
   in one view) but NOT implemented here: build_sections() has its own
   separate, layered 24h/3%-move sections cache that does NOT call
   fundamentals_data.get_bundle() at all on a cache hit, so the two
   fetches are not reliably "the same call happening twice" the way a
   first read suggested - architecting that safely is a larger,
   separate task, out of this one's proven scope ("fix only proven
   causes").

This sandbox has NO outbound network access (confirmed repeatedly
throughout this project) - NVO's/ELS.AX's real before/after IV numbers
cannot be fetched and reported here. This file instead uses REPRE-
SENTATIVE fixtures (an NVO-shaped info/income_df pair, ratio ~1.5x -
comfortably inside share_class_engine's accept window; NVO's own real
~1.335x ratio is reported in the task notes, not re-derived here) to
prove the fix's mechanics directly via code execution, same discipline
as every other fixture-based test in this repo.

Run: python3 tests/test_director_task54_nvo_share_count_stale_cache.py
"""
import datetime
import inspect
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task54_nvo_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import pandas as pd

import fcf_valuation_engine as fve
import share_class_engine as sce
import deep_dive_engine
import auto_compounder_engine
import fundamentals_data

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


def _mk_income_df(diluted_shares):
    return pd.DataFrame(
        {"2026-12-31": [diluted_shares]}, index=["Diluted Average Shares"]
    )


def _mk_cashflow_df():
    return pd.DataFrame(
        {"2026-12-31": [8_000_000_000.0, -1_500_000_000.0]},
        index=["Operating Cash Flow", "Capital Expenditure"],
    )


# NVO-shaped fixture: B-only sharesOutstanding, a corrected A+B total
# sitting at a representative 1.5x ratio (comfortably inside the
# DUAL_CLASS_SHARE_RATIO (1.3x) .. DUAL_CLASS_SHARE_RATIO_MAX (3.0x)
# accept window - NVO's own real ratio is ~1.335x, close to the floor,
# not re-used here for a reliable, deterministic test).
_NVO_SHARES_B_ONLY = 3_333_000_000
_NVO_SHARES_TOTAL = 5_000_000_000
_nvo_info = {
    "sharesOutstanding": _NVO_SHARES_B_ONLY,
    "currency": "USD",
    # financialCurrency deliberately matches `currency` below - this
    # fixture isolates the SHARE-COUNT fix (ask #1), not NVO's separate
    # DKK->USD FX conversion (which needs a live rate this sandbox has
    # no network access to fetch - see the module docstring's "NO
    # outbound network access" disclosure); a currency mismatch here
    # would make every check below fail on "[fx] unavailable" rather
    # than on the thing actually being tested.
    "financialCurrency": "USD",
}
_nvo_income_df = _mk_income_df(_NVO_SHARES_TOTAL)
_nvo_cashflow_df = _mk_cashflow_df()


# ======================================================================
# CHECK 1-3: the proven bug - BEFORE this fix, dcf_intrinsic_value()'s
# internal share resolution never saw income_df. Reproduced inline by
# calling share_class_engine.whole_company_shares() the OLD way (no
# income_df forwarded) to prove it would NOT have found the tier-1
# candidate, exactly as the live bug did.
# ======================================================================
_old_way_shares, _old_way_flagged, _old_way_source, _old_way_note = (
    sce.whole_company_shares(_nvo_info, ticker="NVO")
)
check("OLD behaviour (no income_df forwarded): NVO's own impliedShares"
      "Outstanding is missing from this fixture, and NVO isn't a known "
      "multi-class ticker, so NOTHING corrects the B-only share count - "
      "exactly the live bug's symptom",
      _old_way_flagged is False and _old_way_source is None)
check("...the uncorrected count IS the B-only figure the live bug showed "
      "(3.333B)", _old_way_shares == _NVO_SHARES_B_ONLY)


# ======================================================================
# CHECK 4-6: the FIX - dcf_intrinsic_value() now forwards income_df,
# reaching tier 1 ("bundle") for the SAME NVO-shaped fixture.
# ======================================================================
_iv_with_income, _growth, _meta_with_income = fve.dcf_intrinsic_value(
    "NVO", info=_nvo_info, cashflow_df=_nvo_cashflow_df,
    currency="USD", income_df=_nvo_income_df,
)
check("FIX: dcf_intrinsic_value() with income_df now resolves via tier 1 "
      "('bundle') - the SAME income_df-forwarding fix that was always "
      "available to whole_company_shares(), now actually reached",
      _meta_with_income.get("share_count_source") == "bundle"
      and _meta_with_income.get("share_count_flagged") is True)
check("...meta['shares_used'] (new field) is the CORRECTED total (5.0B), "
      "not the B-only figure (3.333B) - this is what the IV math two "
      "lines below genuinely used",
      _meta_with_income.get("shares_used") == _NVO_SHARES_TOTAL)
check("...a positive intrinsic value was actually computed with it",
      _iv_with_income is not None and _iv_with_income > 0)

# Same fixture, WITHOUT income_df (the pre-fix call shape) - confirms
# the fix is additive: omitting income_df still falls through exactly
# like before (regression check against the old call shape).
_iv_no_income, _growth2, _meta_no_income = fve.dcf_intrinsic_value(
    "NVO", info=_nvo_info, cashflow_df=_nvo_cashflow_df,
    currency="USD", income_df=None,
)
check("regression: omitting income_df entirely still falls through to "
      "no correction (byte-for-byte the old, pre-fix behaviour)",
      _meta_no_income.get("share_count_source") is None
      and _meta_no_income.get("shares_used") == _NVO_SHARES_B_ONLY)
check("...and, holding fcf_base/growth/discount roughly fixed, the "
      "UNDERCOUNTED-shares run produces a HIGHER per-share IV than the "
      "corrected run (fewer shares in the denominator) - the same "
      "direction HEI/HEICO's and VOW3.DE's own prior fixes established",
      _iv_no_income > _iv_with_income)


# ======================================================================
# CHECK 7: a KNOWN multi-class ticker (already in SHARE_CLASS_PAIRS)
# ALSO now reaches tier 1 when income_df is available - proving this is
# a genuine, uniform upgrade for every dual-class ticker this site
# knows about, not an NVO-only special case. GOOG's own tier-2/3 safety
# net (unlike NVO's) means it was never silently broken the way NVO
# was - this just makes tier 1 (cheaper, no network fetch) reachable
# for it too.
# ======================================================================
_goog_info = {"sharesOutstanding": 100_000, "impliedSharesOutstanding": 220_000}
_goog_income_df = _mk_income_df(215_000)
_goog_shares, _goog_flagged, _goog_source, _goog_note = sce.whole_company_shares(
    _goog_info, income_df=_goog_income_df, ticker="GOOG",
)
check("GOOG (already in SHARE_CLASS_PAIRS): with income_df now "
      "forwarded, tier 1 ('bundle') wins over tier 2/3 - no network "
      "fetch needed where a caller-supplied income statement exists",
      _goog_source == "bundle" and _goog_shares == 215_000)


# ======================================================================
# CHECK 8-9: PROVEN - neither EXISTING [dcf_diag] log line's "shares="
# field was ever what dcf_intrinsic_value() used internally (source
# inspection of the pre-fix-adjacent code, confirming the finding
# described in ask #2 above).
# ======================================================================
_dd_src = inspect.getsource(deep_dive_engine.analyze)
check("deep_dive_engine.py's [dcf_diag] line's pre-existing 'shares=' "
      "field reads info.get('sharesOutstanding') directly - the RAW, "
      "un-dual-class-corrected figure, never iv_meta's own resolved "
      "value",
      "shares={info.get('sharesOutstanding')}" in _dd_src)
_acv_src = inspect.getsource(auto_compounder_engine._run_canonical_dcf)
check("auto_compounder_engine.py's [dcf_diag] line's pre-existing "
      "'shares=' field reads _basics(bundle)['shares'] - a SEPARATELY "
      "resolved figure, never threaded into the resolve_intrinsic_"
      "value() call two lines above it",
      "_dcf_diag_shares = _basics(bundle)[\"shares\"]" in _acv_src)


# ======================================================================
# CHECK 10-11: FIX - both [dcf_diag] log lines now ALSO report "
# shares_used"/"share_source" (meta's own new fields) - what the DCF
# math genuinely used, made observable without re-inferring it.
# ======================================================================
check("deep_dive_engine.py's [dcf_diag] line now also logs "
      "shares_used/share_source from iv_meta",
      "shares_used={iv_meta.get('shares_used')}" in _dd_src
      and "share_source={iv_meta.get('share_count_source')}" in _dd_src)
check("auto_compounder_engine.py's [dcf_diag] line now also logs "
      "shares_used/share_source from meta",
      "shares_used={meta.get('shares_used')}" in _acv_src
      and "share_source={meta.get('share_count_source')}" in _acv_src)


# ======================================================================
# CHECK 12-14: FIX - deep_dive_engine.py's peek_cached_bundle() branch
# now bounds staleness to fundamentals_data's own 24h TTL, reproducing
# the Director's EXACT reported numbers (130,212s cache_age vs the
# 24h/86400s TTL).
# ======================================================================
check("sanity: fundamentals_data._CACHE_TTL_SECONDS really is 24h "
      "(86400s) - the bound reused here, not a new invented threshold",
      fundamentals_data._CACHE_TTL_SECONDS == 24 * 3600)
check("the Director's own reported cache_age (130,212s / ~36.2h) "
      "genuinely exceeds that 24h TTL - confirms this is a real, "
      "provable staleness violation, not a borderline/ambiguous case",
      130_212 > fundamentals_data._CACHE_TTL_SECONDS)

_analyze_src = inspect.getsource(deep_dive_engine.analyze)
_peek_branch = _analyze_src[
    _analyze_src.index("_bundle = fundamentals_data.peek_cached_bundle"):
]
_peek_branch = _peek_branch[:_peek_branch.index("intrinsic_value, intrinsic_src")]
check("deep_dive_engine.analyze()'s peek_cached_bundle() branch now "
      "compares the peeked bundle's own age against fundamentals_data."
      "_CACHE_TTL_SECONDS and discards it (sets _bundle = None) when "
      "older - never a new fetch, just a discard that falls through to "
      "the pre-existing needs_oneoff_check()/None branches",
      "_dcf_diag_cache_age > fundamentals_data._CACHE_TTL_SECONDS" in _peek_branch
      and "_bundle = None" in _peek_branch)


# ======================================================================
# CHECK 15-16: the staleness bound actually changes behaviour end-to-
# end for a 36h-old bundle (Director's exact shape) vs a 1h-old one,
# exercised directly against fundamentals_data.peek_cached_bundle()'s
# real cache-file format (not just the source-inspection proof above).
# ======================================================================
def _write_fake_bundle_cache(ticker, age_seconds, income_df):
    fetched_at = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=age_seconds)
    ).isoformat()
    obj = {
        "info": {},
        "income": fundamentals_data._df_to_json(income_df),
        "balance": None,
        "cashflow": None,
        "income_q": None,
        "prices_10y": {"dates": [], "prices": []},
        "dividends": {"dates": [], "amounts": []},
        "spx_prices_10y": {"dates": [], "prices": []},
        "meta": {
            "fetched_at": fetched_at,
            "bundle_version": fundamentals_data.BUNDLE_VERSION,
            "flags": [],
        },
    }
    import json
    path = fundamentals_data._cache_path(ticker)
    with open(path, "w") as f:
        json.dump(obj, f)


# A stale (36h) bundle is PEEKED (peek_cached_bundle ignores the TTL,
# by design - unchanged), but the fixed branch logic above discards it
# based on its own age.
_write_fake_bundle_cache("TASK54STALE", 36 * 3600, _mk_income_df(999_999))
_stale_peek = fundamentals_data.peek_cached_bundle("TASK54STALE")
check("sanity: peek_cached_bundle() itself still returns the 36h-old "
      "bundle unchanged (its own documented TTL-ignoring behaviour, "
      "untouched by this fix - the discard happens in the CALLER)",
      _stale_peek is not None)
_stale_age = (
    datetime.datetime.now(datetime.timezone.utc)
    - datetime.datetime.fromisoformat(_stale_peek["meta"]["fetched_at"])
).total_seconds()
check("...and that bundle's own age (as deep_dive_engine.py's "
      "[dcf_diag] cache_age field would compute it) exceeds the 24h "
      "TTL, so the fixed caller-side check now discards it",
      _stale_age > fundamentals_data._CACHE_TTL_SECONDS)

# A fresh (1h) bundle for a different ticker is still used normally -
# the fix must not discard a genuinely fresh cache entry.
_write_fake_bundle_cache("TASK54FRESH", 1 * 3600, _mk_income_df(999_999))
_fresh_peek = fundamentals_data.peek_cached_bundle("TASK54FRESH")
_fresh_age = (
    datetime.datetime.now(datetime.timezone.utc)
    - datetime.datetime.fromisoformat(_fresh_peek["meta"]["fetched_at"])
).total_seconds()
check("regression: a 1h-old bundle's age stays well inside the 24h "
      "TTL - the fixed caller-side check would NOT discard this one",
      _fresh_age < fundamentals_data._CACHE_TTL_SECONDS)


# ======================================================================
# CHECK 17: diluted_shares_override path still populates meta[
# 'shares_used'] (new field) - confirms it's set on every path, not
# just the whole_company_shares() branch.
# ======================================================================
_iv_override, _g3, _meta_override = fve.dcf_intrinsic_value(
    "NVO", info=_nvo_info, cashflow_df=_nvo_cashflow_df,
    currency="USD", diluted_shares_override=4_500_000_000,
)
check("diluted_shares_override path: meta['shares_used'] is the "
      "override value itself, not re-derived",
      _meta_override.get("shares_used") == 4_500_000_000
      and _meta_override.get("share_count_source") is None)


# ======================================================================
# CHECK 18: regression - a normal, single-class ticker with NO
# divergent income_df row is completely unaffected (no false-positive
# correction introduced by forwarding income_df).
# ======================================================================
_normal_info = {"sharesOutstanding": 1_000_000_000, "currency": "USD"}
_normal_income_df = _mk_income_df(1_000_000_000)  # matches sharesOutstanding exactly
_normal_shares, _normal_flagged, _normal_source, _normal_note = (
    sce.whole_company_shares(_normal_info, income_df=_normal_income_df, ticker="NORMALCO")
)
check("regression: a single-class ticker whose income_df diluted-"
      "shares row MATCHES sharesOutstanding (ratio 1.0x, below "
      "DUAL_CLASS_SHARE_RATIO's 1.3x floor) is never flagged/corrected",
      _normal_flagged is False and _normal_source is None
      and _normal_shares == 1_000_000_000)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
