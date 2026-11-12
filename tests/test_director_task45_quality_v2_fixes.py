"""
TASK 45 (Director-directed, 12 Nov 2026, "needed before the 12 Nov
review" - Quality v2 is NOT going live until these are fixed and the
Director re-previews): fixes from the Director's own review of the
live 12 Nov Quality v2 preview.

Covers:
  1/2. quality_v2_engine.get_quality_score_v2()'s ROE had NO equity-
       sign check and NO plausibility clamp at all (unlike ROIC right
       next to it, already clamped to +-100%) - a negative/near-zero
       book equity produces a huge or sign-flipped ROE, inflating the
       score by up to +20 points. This is ONE root cause behind BOTH
       of the Director's reported symptoms: item 1's buyback-heavy
       operating companies (AAL/HCA/MTCH/SMG/CCI/INSE/KSPI) hit it via
       the per-fiscal-year statement path; item 2's mortgage REITs
       (ORC/NREF/REFI) hit the SAME unguarded ROE via the .info
       fallback path (financials_mode forces fy_dates=[] for them, so
       roe always falls straight to info.get("returnOnEquity")) - NOT
       a dash-matching bug in _is_mortgage_reit() (already handles
       "REIT—Mortgage" correctly, see test_director_task40_review_
       fixes.py's own CHECK 1, still passing). Fixed with ONE
       plausibility guard (_plausible_roe(), ROE_PLAUSIBLE_MAX = 1.0,
       the same ceiling ROIC already uses) applied consistently to
       both the per-FY statement value and the .info fallback value.
  3. nightly_scan._FUND_TICKER_OVERRIDE_IS_FUND now includes "SOI.L"
     (Schroder Oriental Income Fund), same hand-maintained-override
     mechanism as the existing VEIL.L/INVE-B.ST entries - this
     sandbox has no production access to confirm SOI.L's own exact
     stored Sector string, so the fix matches the EXISTING, already-
     proven pattern rather than guessing at a sector-name addition.
  5. top100_engine._quality_exception_admits() now excludes Energy/
     Basic Materials (QUALITY_EXCEPTION_EXCLUDED_SECTORS); a new
     _quality_exception_excluded_airline() catches Airlines (not its
     own Yahoo sector) via a fresh, cache-hit info lookup, called only
     for a row that already passed every other criterion.

Item 4 (re-run the preview, report the new turnover + top 20 entrants)
needs the Director's own re-preview against real production scan data
after this fix deploys - this sandbox has no network/production access
to run that preview itself; not attempted here as a fabricated number.

Run: python3 tests/test_director_task45_quality_v2_fixes.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task45_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)

import nightly_scan
import quality_v2_engine as qv2
import scan_store
import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

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
# CHECK 1 (items 1/2): _plausible_roe() - the plausibility guard itself.
# ======================================================================
check("a normal ROE (12%) passes through unchanged", qv2._plausible_roe(0.12) == 0.12)
check("exactly at the 100% ceiling still passes (inclusive)", qv2._plausible_roe(1.0) == 1.0)
check("just over the 100% ceiling (105%) is None (implausible)", qv2._plausible_roe(1.05) is None)
check("a large negative ROE (-150%) is also None - magnitude, not just high side",
      qv2._plausible_roe(-1.5) is None)
check("None in -> None out", qv2._plausible_roe(None) is None)
check("a non-numeric value never raises, returns None", qv2._plausible_roe("not a number") is None)
print("[task45_check1_plausible_roe] _plausible_roe() excludes any ROE whose magnitude "
      "exceeds 100%, in either direction, never raises on bad input OK")


# ======================================================================
# CHECK 2 (item 1): AAL/HCA-shaped case - a FY with non-positive equity
# is skipped entirely (not merely "falsy" - a NEGATIVE equity is
# truthy in Python and the OLD code's `if equity_by_fy.get(fy)` check
# let it straight through, producing a sign-flipped ROE).
# ======================================================================
import datetime as _dt

_fy1 = _dt.date(2025, 12, 31)
_fy2 = _dt.date(2024, 12, 31)
_fy3 = _dt.date(2023, 12, 31)


def _income_series(by_fy):
    class _Fake:
        def __init__(self, by_fy):
            self._by_fy = by_fy
    return _Fake(by_fy)


def _bundle_for(ni_by_fy, equity_by_fy, revenue_by_fy=None, debt_by_fy=None,
                 ocf_by_fy=None, capex_by_fy=None, info=None):
    """A synthetic bundle shaped so auto_compounder_engine.normalized_
    income_for_fy()/series_by_date() return exactly the fixed values
    this test wants, without needing a real statements DataFrame -
    monkeypatches those two functions for the duration of one call
    (restored by the caller's own `with` block)."""
    revenue_by_fy = revenue_by_fy or {fy: 1000.0 for fy in ni_by_fy}
    debt_by_fy = debt_by_fy or {}
    ocf_by_fy = ocf_by_fy or {}
    capex_by_fy = capex_by_fy or {}
    return {
        "income": "FAKE_INCOME", "balance": "FAKE_BALANCE", "cashflow": "FAKE_CASHFLOW",
        "info": info or {},
    }, ni_by_fy, equity_by_fy, revenue_by_fy, debt_by_fy, ocf_by_fy, capex_by_fy


def _run_v2_with_fixture(ticker, ni_by_fy, equity_by_fy, revenue_by_fy=None,
                          debt_by_fy=None, ocf_by_fy=None, capex_by_fy=None, info=None):
    bundle, ni_by_fy, equity_by_fy, revenue_by_fy, debt_by_fy, ocf_by_fy, capex_by_fy = _bundle_for(
        ni_by_fy, equity_by_fy, revenue_by_fy, debt_by_fy, ocf_by_fy, capex_by_fy, info)
    fy_dates = sorted(ni_by_fy.keys(), reverse=True)

    def _fake_normalized_income_for_fy(income, fy):
        return ni_by_fy.get(fy), False

    def _fake_series_by_date(df, field):
        if df == "FAKE_BALANCE" and field == "stockholders_equity":
            return equity_by_fy
        if df == "FAKE_BALANCE" and field == "total_debt":
            return debt_by_fy
        if df == "FAKE_INCOME" and field == "revenue":
            return revenue_by_fy
        if df == "FAKE_CASHFLOW" and field == "operating_cash_flow":
            return ocf_by_fy
        if df == "FAKE_CASHFLOW" and field == "capex":
            return capex_by_fy
        if df == "FAKE_CASHFLOW" and field == "free_cash_flow":
            return {}
        return {}

    with mock.patch.object(qv2.auto_compounder_engine, "normalized_income_for_fy",
                            side_effect=_fake_normalized_income_for_fy), \
         mock.patch.object(qv2.auto_compounder_engine, "series_by_date",
                            side_effect=_fake_series_by_date), \
         mock.patch.object(qv2, "_fy_dates", return_value=fy_dates):
        return qv2.get_quality_score_v2(ticker, bundle, info=info or {})


# AAL-shaped: positive net income, TINY positive equity across all 3
# years -> every FY's raw ROE is wildly implausible (huge, e.g. 500%+).
_score_aal, _meta_aal = _run_v2_with_fixture(
    "AAL",
    ni_by_fy={_fy1: 300.0, _fy2: 280.0, _fy3: 260.0},
    equity_by_fy={_fy1: 10.0, _fy2: 9.0, _fy3: 8.0},  # tiny positive -> ROE 2600-3250%
    info={"returnOnEquity": None},  # no plausible fallback either -> roe becomes 0
)
check("AAL-shaped case: every FY's ROE is implausible (huge) -> all excluded, "
      "roe falls back to 0 (no plausible .info value either) - never a huge/"
      "distorted score",
      _meta_aal["roe"] == 0 and "roe" in _meta_aal["fallback_fields"])
check("AAL-shaped case: the score is NOT inflated to near-100 by a bogus ROE "
      "contribution (other inputs are plain/neutral in this fixture)",
      _score_aal < 90)

# HCA-shaped: NEGATIVE equity (heavy buybacks) with POSITIVE net income
# -> old code's `if equity_by_fy.get(fy)` truthiness check let a
# negative number straight through, producing a sign-FLIPPED (negative)
# ROE that could still pass a naive "> 0 good" read, or - with a loss
# year instead - a POSITIVE huge ROE from two negatives.
_score_hca, _meta_hca = _run_v2_with_fixture(
    "HCA",
    ni_by_fy={_fy1: -50.0, _fy2: -40.0, _fy3: -30.0},  # a loss
    equity_by_fy={_fy1: -200.0, _fy2: -190.0, _fy3: -180.0},  # negative equity
    info={"returnOnEquity": None},
)
check("HCA-shaped case (negative equity, negative income - both negatives would "
      "sign-flip to a POSITIVE ratio under the old truthiness check): every FY "
      "is excluded (equity <= 0), roe falls back to 0, never a sign-flipped "
      "positive number",
      _meta_hca["roe"] == 0 and "roe" in _meta_hca["fallback_fields"])

# A GOOD, plausible year is still used normally - the fix never excludes
# a genuinely sane ROE.
_score_good, _meta_good = _run_v2_with_fixture(
    "GOODCO",
    ni_by_fy={_fy1: 15.0, _fy2: 14.0, _fy3: 13.0},
    equity_by_fy={_fy1: 100.0, _fy2: 95.0, _fy3: 90.0},  # ROE ~14-15%, plausible
    info={"returnOnEquity": 0.15},
)
check("a normal, plausible ROE (around 14-15%) is still computed from statements "
      "- never a fallback, never excluded",
      "roe" not in _meta_good["fallback_fields"] and 0.10 < _meta_good["roe"] < 0.20)
print("[task45_check2_statement_roe_fix] AAL/HCA-shaped cases (tiny-positive and "
      "negative equity respectively) are both correctly excluded from the "
      "statement-derived ROE, falling back to 0 rather than a huge/sign-flipped "
      "figure; a genuinely plausible ROE is completely unaffected OK")


# ======================================================================
# CHECK 3 (item 2): mortgage-REIT-shaped case (financials_mode forces
# fy_dates=[], so ROE ALWAYS takes the .info fallback path) - an
# implausible .info["returnOnEquity"] (ORC/NREF/REFI-shaped) is now
# guarded the SAME way; a plausible one is used as before.
# ======================================================================
_mreit_implausible_info = {"industry": "REIT - Mortgage", "returnOnEquity": 3.5}  # 350% - implausible
_score_mreit_bad, _meta_mreit_bad = qv2.get_quality_score_v2(
    "ORC", {"info": _mreit_implausible_info}, info=_mreit_implausible_info)
check("mortgage-REIT-shaped bundle (financials_mode=True) with an implausible "
      "350% .info ROE: roe becomes 0, not 3.5 - the SAME guard applied to the "
      "fallback path, confirming items 1/2 share one root cause and one fix",
      _meta_mreit_bad["financials_mode"] is True and _meta_mreit_bad["roe"] == 0)

_mreit_plausible_info = {"industry": "REIT - Mortgage", "returnOnEquity": 0.12}  # 12% - plausible
_score_mreit_good, _meta_mreit_good = qv2.get_quality_score_v2(
    "NLY", {"info": _mreit_plausible_info}, info=_mreit_plausible_info)
check("mortgage-REIT-shaped bundle with a PLAUSIBLE 12% .info ROE: used as-is, "
      "unaffected by this fix", _meta_mreit_good["roe"] == 0.12)
check("_is_mortgage_reit() itself is UNCHANGED and still correctly dash-agnostic "
      "(em dash 'REIT—Mortgage' still -> True) - this task's fix is the ROE "
      "guard, NOT a dash-matching change, confirming the Director's own dash "
      "hypothesis for item 2 was not the actual root cause",
      qv2._is_mortgage_reit({"industry": "REIT—Mortgage"}))
print("[task45_check3_mortgage_reit_roe_fix] a mortgage-REIT's own .info-fallback "
      "ROE is now guarded the same way as the statement path - the real root "
      "cause of ORC/NREF/REFI's inflated Quality v2 was the missing ROE "
      "plausibility guard reached via the financials-mode fallback, not a dash-"
      "matching bug in _is_mortgage_reit() (already correct, already tested) OK")


# ======================================================================
# CHECK 4 (item 3): SOI.L fund leak - added to the hand-maintained
# override set, same mechanism as VEIL.L/INVE-B.ST.
# ======================================================================
check("SOI.L is in the fund-ticker override set",
      "SOI.L" in nightly_scan._FUND_TICKER_OVERRIDE_IS_FUND)
check("the existing VEIL.L/INVE-B.ST overrides are untouched",
      {"VEIL.L", "INVE-B.ST"} <= nightly_scan._FUND_TICKER_OVERRIDE_IS_FUND)
print("[task45_check4_soi_fund_override] SOI.L added to the fund-ticker override "
      "set via the same hand-maintained mechanism as VEIL.L/INVE-B.ST OK")


# ======================================================================
# CHECK 5 (item 5): _quality_exception_admits() excludes Energy/Basic
# Materials by Sector; _quality_exception_excluded_airline() excludes
# Airlines via a fresh info look-up.
# ======================================================================
def _base_row(**overrides):
    row = {
        "Ticker": "BASE", "Price": 100.0, "Intrinsic Value": 350.0,  # 3.5x
        "Moat": 90.0, "Quality": 85.0, "FCF Source": "ocf-normcapex",
        "Share Count Flagged": False, "Trading Status": None,
        "Intrinsic Source": "dcf", "Sector": "Technology",
    }
    row.update(overrides)
    return row


check("a Technology-sector row (ADBE-shaped) is admitted - unaffected by the "
      "new sector exclusion", te._quality_exception_admits(_base_row()))
check("an Energy-sector row is NOT admitted, even though every other condition "
      "passes", not te._quality_exception_admits(_base_row(Sector="Energy")))
check("a Basic Materials-sector row (HMY/gold-mining-shaped) is NOT admitted",
      not te._quality_exception_admits(_base_row(Sector="Basic Materials")))
check("sector matching is case-insensitive", not te._quality_exception_admits(
    _base_row(Sector="ENERGY")))
check("a missing Sector is simply unaffected (not treated as excluded)",
      te._quality_exception_admits(_base_row(Sector=None)))

with mock.patch.object(te.fundamentals_data, "get_bundle",
                        return_value={"info": {"industry": "Airlines"}}):
    check("_quality_exception_excluded_airline(): Yahoo industry 'Airlines' -> True",
          te._quality_exception_excluded_airline("LTM", log=lambda *a: None))
with mock.patch.object(te.fundamentals_data, "get_bundle",
                        return_value={"info": {"industry": "Software - Application"}}):
    check("_quality_exception_excluded_airline(): a non-airline industry -> False",
          not te._quality_exception_excluded_airline("ADBE", log=lambda *a: None))
with mock.patch.object(te.fundamentals_data, "get_bundle", side_effect=RuntimeError("boom")):
    _logged_airline = []
    check("_quality_exception_excluded_airline(): a lookup failure never raises, "
          "returns False (fails open on this EXTRA check only) and logs it",
          not te._quality_exception_excluded_airline("XYZ", log=lambda m: _logged_airline.append(m))
          and any("airline-industry lookup failed" in m for m in _logged_airline))
print("[task45_check5_cyclical_exclusion_unit] Energy/Basic Materials excluded by "
      "Sector, Airlines excluded via a fresh (fail-open, non-raising) industry "
      "look-up, a Technology/missing-sector row completely unaffected OK")


# ======================================================================
# CHECK 6 (item 5): full _build_best_by_ticker() integration - LTM
# (Airlines, via industry look-up) and HMY (Basic Materials, via
# Sector) are excluded from the quality exception even with the gate
# forced on; ADBE-shaped stays admitted exactly as TASK 37 already
# proved.
# ======================================================================
def _dcf_unreliable_row(ticker, sector, **overrides):
    row = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Price": 100.0,
        "Intrinsic Value": 350.0, "MOS %": 50.0, "Long Score": 60.0,
        "Quality": 90.0, "Moat": 92.0, "Psychology": 5.0, "Discovery (lite)": 20.0,
        "FCF Source": "ocf-normcapex", "Share Count Flagged": False,
        "Trading Status": None, "Intrinsic Source": "dcf",
        "Is Fund": False, "price_unit_suspect": False, "Sector": sector,
        "DCF Unreliable": True,
    }
    row.update(overrides)
    return row


_ltm_row = _dcf_unreliable_row("LTM", "Industrials")  # Airlines is an industry, not this sector
_hmy_row = _dcf_unreliable_row("HMY", "Basic Materials")
_adbe_row = _dcf_unreliable_row("ADBE", "Technology")
scan_store.save_scan("S&P 500", [_ltm_row, _hmy_row, _adbe_row], source_label="test fixture")

with mock.patch.object(te.fundamentals_data, "get_bundle",
                        side_effect=lambda t: {"info": {"industry": "Airlines"}} if t == "LTM"
                        else {"info": {"industry": "Mining"}}):
    _best_cyclical, _ = te._build_best_by_ticker(log=lambda *a, **k: None, quality_exception_override=True)

check("LTM (Airlines, via a fresh industry look-up) is NOT admitted by the "
      "quality exception even with the gate forced on - excluded from best_by_"
      "ticker like any other unadmitted DCF-unreliable row",
      "LTM" not in _best_cyclical)
check("HMY (Basic Materials, via its own stored Sector) is NOT admitted either",
      "HMY" not in _best_cyclical)
check("ADBE (Technology) IS still admitted - the cyclical exclusion never "
      "touches a non-cyclical sector",
      "ADBE" in _best_cyclical and _best_cyclical["ADBE"]["quality_exception_admitted"] is True)
print("[task45_check6_full_integration] LTM/HMY are excluded from the quality "
      "exception end-to-end (Airlines via industry look-up, Basic Materials via "
      "Sector) even with the gate forced on; ADBE stays admitted exactly as "
      "before this task OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
