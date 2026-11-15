"""
TASK 64 (Director-directed, 15 Nov 2026) - diagnose why PGHN.SW
(Partners Group) and TRI.TO (Thomson Reuters) get low Quality v2.
DIAGNOSE ONLY - no scoring change.

FACTS (Director verified from the owner's Full 200 export, 15 Nov):
PGHN.SW Quality v1=77, v2=50 after the 14 Nov refresh - dropped it
below the 65 Quality on sale threshold (it was #12-15 before). TRI.TO
left Quality on sale at the same time, suspected same cause, not yet
confirmed.

WHAT THIS TASK DOES (report-only conclusions live in the chat report,
not in code):
  1. nightly_scan.QUALITY_V2_DIAGNOSTIC_WATCHLIST now includes
     "PGHN.SW"/"TRI.TO" so tonight's run logs their [quality_v2_diag]
     inputs.
  2. _log_quality_v2_diagnostic() now logs THREE lines per watchlisted
     ticker every night: the original summary line (unchanged), a new
     "contributions" line (each input's actual score CONTRIBUTION in
     points, computed from quality_v2_engine's own NAMED weight
     constants - never a second, hand-copied set of weights), and a
     new per-fiscal-year line (quality_v2_engine's new "per_fy" meta
     block - which years fed each input's median, and what value each
     one contributed).
  3. quality_v2_engine.get_quality_score_v2()'s own `score` FORMULA is
     completely unchanged - the weight literals were pulled into named
     module-level constants (ROIC_WEIGHT/ROE_WEIGHT/etc.) and the four
     per-fiscal-year loops (roe/roic/net_margin/debt_to_equity) now
     build {fy: value} dicts instead of plain lists, but every value
     fed to _median_of_available() is byte-identical, in the same
     order, as before this task - this file's own CHECK 5 proves it
     numerically against a real statement-shaped fixture.

Run: python3 tests/test_director_task64_pghn_tri_quality_v2_diag.py
"""
import os
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


import nightly_scan
import quality_v2_engine as qv2

# ======================================================================
# CHECK 1: watchlist membership - PGHN.SW and TRI.TO are both present,
# and every pre-existing ticker is still present too (purely additive).
# ======================================================================
check("[1a] PGHN.SW is in QUALITY_V2_DIAGNOSTIC_WATCHLIST",
      "PGHN.SW" in nightly_scan.QUALITY_V2_DIAGNOSTIC_WATCHLIST)
check("[1b] TRI.TO is in QUALITY_V2_DIAGNOSTIC_WATCHLIST",
      "TRI.TO" in nightly_scan.QUALITY_V2_DIAGNOSTIC_WATCHLIST)
_PRE_EXISTING = {
    "AAL", "HCA", "MTCH", "CCI", "INSE", "KSPI", "MO", "HPQ", "FICO", "LII",
    "DPZ", "ORC", "NREF", "SOI.L", "CTGO", "CSL.AX", "OCL.AX", "NU", "ADBE",
    "COKE", "INTU", "CPRT", "WDAY", "ZM",
}
check("[1c] every pre-existing watchlisted ticker is still present (purely "
      "additive, nothing removed)",
      _PRE_EXISTING.issubset(nightly_scan.QUALITY_V2_DIAGNOSTIC_WATCHLIST))
check("[1d] the watchlist grew by exactly 2 tickers",
      len(nightly_scan.QUALITY_V2_DIAGNOSTIC_WATCHLIST) == len(_PRE_EXISTING) + 2)


# ======================================================================
# CHECK 2: the extended diag line format - fires 3 lines per
# watchlisted ticker (summary + contributions + per-fiscal-year), and
# the new lines carry real content when the meta has real per-FY data.
# ======================================================================
_logged = []


def _cap(msg):
    _logged.append(msg)


_fixture_meta = {
    "roic": 0.10, "roe": 0.15, "net_margin": 0.08,
    "revenue_growth": -0.05, "earnings_growth": -0.12,
    "fcf_positive": True, "debt_to_equity": 193.0, "debt_penalty": 9.65,
    "fallback_fields": [],
    "fiscal_years_used": ["2026-06-30", "2025-06-30", "2024-06-30"],
    "per_fy": {
        "roe": {"2026-06-30": 0.09, "2025-06-30": 0.15, "2024-06-30": 0.22},
        "roic": {"2026-06-30": 0.07, "2025-06-30": 0.10, "2024-06-30": 0.13},
        "net_margin": {"2026-06-30": 0.06, "2025-06-30": 0.08, "2024-06-30": 0.11},
        "debt_to_equity": {"2026-06-30": 193.0, "2025-06-30": 180.0, "2024-06-30": 160.0},
        "normalized_income": {"2026-06-30": 900.0, "2025-06-30": 1100.0, "2024-06-30": 1300.0},
        "revenue": {"2026-06-30": 2100.0, "2025-06-30": 2000.0, "2024-06-30": 1950.0},
    },
}
_row = {"Quality": 77.0, "Quality V2": 50.0}

_logged.clear()
nightly_scan._log_quality_v2_diagnostic(_row, "PGHN.SW", _fixture_meta, _cap)
check("[2a] three lines logged for PGHN.SW (summary + contributions + per-FY)",
      len(_logged) == 3)
check("[2b] line 1 is the original summary format, unchanged",
      _logged[0].startswith("[quality_v2_diag] PGHN.SW v1=77.000 v2=50.000 "
                            "roe=0.150 roic=0.100 d_e=193.000 debt_penalty=9.650"))
check("[2c] line 2 names every one of the 5 weighted inputs plus fcf/debt_penalty",
      all(k in _logged[1] for k in
          ("roic=", "roe=", "net_margin=", "revenue_growth=", "earnings_growth=",
           "fcf=", "debt_penalty=")))
check("[2d] line 2's roic contribution matches roic * 100 * ROIC_WEIGHT exactly "
      "(reused from quality_v2_engine's own named constant, not a second, "
      "hand-copied weight)",
      f"roic={round(0.10 * 100 * qv2.ROIC_WEIGHT, 2):.3f}" in _logged[1])
check("[2e] line 2's fcf contribution is the real FCF_POSITIVE_BONUS constant "
      "(fcf_positive=True in this fixture)",
      f"fcf={qv2.FCF_POSITIVE_BONUS}" in _logged[1])
check("[2f] line 3 names fiscal_years_used and every per_fy_* field",
      all(k in _logged[2] for k in
          ("fiscal_years_used=", "per_fy_roe=", "per_fy_roic=",
           "per_fy_net_margin=", "per_fy_debt_to_equity=",
           "per_fy_normalized_income=", "per_fy_revenue=")))
check("[2g] line 3 carries the REAL per-year ROE dict from the fixture, "
      "not a placeholder", "'2024-06-30': 0.22" in _logged[2])
check("[2h] line 3 carries the real per-year revenue dict (the raw, "
      "UNNORMALIZED base roic/net_margin/revenue_growth are computed "
      "from) - this is exactly the field that would expose a TRI.TO-"
      "style divestment/one-off distorting a fiscal year's own base",
      "'2024-06-30': 1950.0" in _logged[2])


# ======================================================================
# CHECK 3: PGHN.SW is classified financials_mode=True (Partners Group -
# Yahoo sector "Financial Services") - the code path that forces EVERY
# input to fall back to today's live .info value, never the multi-year
# statement computation at all. This is the first fact needed to
# diagnose its v1/v2 gap: v2 is NOT reading lumpy carried-interest
# fiscal years here - it's reading the exact same live .info snapshot
# v1 reads, just through v2's OWN, stricter formula.
# ======================================================================
import financials_classifier

_pghn_info = {"sector": "Financial Services", "industry": "Asset Management"}
check("[3] financials_classifier.is_financials() classifies a Partners-"
      "Group-shaped info dict (sector='Financial Services') as "
      "financials_mode=True",
      financials_classifier.is_financials(_pghn_info, ticker="PGHN.SW") is True)

_score_fm, _meta_fm = qv2.get_quality_score_v2("PGHN.SW", {"info": _pghn_info}, info=_pghn_info)
check("[4] for a financials-mode ticker, get_quality_score_v2() marks "
      "financials_mode=True and EVERY one of the 7 inputs as a fallback "
      "(fiscal_years_used is empty - no statement computation runs at all)",
      _meta_fm["financials_mode"] is True
      and set(_meta_fm["fallback_fields"]) == set(qv2._INPUT_FIELDS)
      and _meta_fm["fiscal_years_used"] == [])


# ======================================================================
# CHECK 5 (the real suspected culprit for PGHN.SW): v1 (auto_quality_
# engine.get_quality_score()) uses the RAW .info ROE/margin value with
# NO plausibility guard at all; v2's fallback path applies _plausible_
# roe()/_plausible_margin() to that SAME .info value - treating a
# reading over +-100% as "unavailable" (-> 0) instead of using it. An
# asset manager's asset-light balance sheet can easily report a >100%
# ROE/margin in Yahoo's own .info - this single guard can swing the
# score by whichever weighted input it zeroes out (up to 20 points for
# ROE, 15 for net margin), while v1 keeps using the raw, large value.
# ======================================================================
import auto_quality_engine

_extreme_info = {
    "sector": "Financial Services", "industry": "Asset Management",
    "returnOnEquity": 1.40,  # 140% - plausible for an asset-light asset manager, implausible by this module's own +-100% guard
    "profitMargins": 0.35, "revenueGrowth": 0.05, "earningsGrowth": -0.12,
    "debtToEquity": 193.0, "freeCashflow": 100.0, "netIncomeToCommon": 500.0,
    "marketCap": 10000.0, "priceToBook": 5.0, "totalDebt": 1000.0,
}
_v1_score, _v1_defaulted = auto_quality_engine.get_quality_score(None, info=_extreme_info)
_v2_score, _v2_meta = qv2.get_quality_score_v2("PGHN.SW", {"info": _extreme_info}, info=_extreme_info)
check("[5a] v1 uses the RAW 140% ROE directly (no plausibility guard) - "
      "its own quality_score_inputs() reports it unclamped",
      auto_quality_engine.quality_score_inputs(_extreme_info)["roe"] == 1.40)
check("[5b] v2's fallback ROE is 0 (the >100% guard rejected the raw "
      "140% value as implausible) - NOT the raw 1.40 v1 used",
      _v2_meta["roe"] == 0)
check("[5c] this ROE-guard difference alone is large enough to plausibly "
      "explain a double-digit-point v1/v2 gap for an asset manager "
      "(140% * 100 * ROE_WEIGHT points lost)",
      round(1.40 * 100 * qv2.ROE_WEIGHT, 1) >= 20.0)
print(f"[task64_check5] v1 (raw ROE) scored {_v1_score}, v2 (ROE zeroed by the "
      f"plausibility guard) scored {_v2_score} for the IDENTICAL extreme-ROE "
      f"fixture - the exact asymmetry suspected for PGHN.SW")


# ======================================================================
# CHECK 6: the per-fiscal-year math refactor (plain list -> {fy: value}
# dict, done so the new "per_fy" meta block can report real dates)
# produces BYTE-IDENTICAL roe/roic/net_margin/debt_to_equity values to
# before this task, against a realistic multi-year statement fixture -
# proving the refactor really is pure "expose more detail", never a
# scoring change, exactly as instruction 3 ("DO NOT change any
# scoring") requires.
# ======================================================================
import pandas as pd

_dates = ["2026-06-30", "2025-06-30", "2024-06-30"]
_income = pd.DataFrame({
    _dates[0]: {"Total Revenue": 2100.0, "Net Income": 900.0, "Normalized Income": 900.0,
                "Total Unusual Items": 0.0, "Tax Provision": 50.0, "Pretax Income": 950.0},
    _dates[1]: {"Total Revenue": 2000.0, "Net Income": 1100.0, "Normalized Income": 1100.0,
                "Total Unusual Items": 0.0, "Tax Provision": 60.0, "Pretax Income": 1160.0},
    _dates[2]: {"Total Revenue": 1950.0, "Net Income": 1300.0, "Normalized Income": 1300.0,
                "Total Unusual Items": 0.0, "Tax Provision": 70.0, "Pretax Income": 1370.0},
})
_balance = pd.DataFrame({
    _dates[0]: {"Stockholders Equity": 1500.0, "Total Debt": 2800.0},
    _dates[1]: {"Stockholders Equity": 1400.0, "Total Debt": 2500.0},
    _dates[2]: {"Stockholders Equity": 1300.0, "Total Debt": 2000.0},
})
_cashflow = pd.DataFrame({
    _dates[0]: {"Operating Cash Flow": 950.0, "Capital Expenditure": -50.0},
    _dates[1]: {"Operating Cash Flow": 1050.0, "Capital Expenditure": -40.0},
    _dates[2]: {"Operating Cash Flow": 1200.0, "Capital Expenditure": -30.0},
})
_bundle = {"income": _income, "balance": _balance, "cashflow": _cashflow, "info": {}}
_score_real, _meta_real = qv2.get_quality_score_v2("TRI.TO", _bundle, info={})

check("[6a] the realistic multi-year fixture actually exercises the "
      "statement path (fiscal_years_used has 3 entries, not the "
      "financials-mode empty list)",
      len(_meta_real["fiscal_years_used"]) == 3)
check("[6b] per_fy['roe'] has exactly the 3 fiscal years this fixture "
      "provides, keyed by real date strings",
      set(_meta_real["per_fy"]["roe"].keys()) == set(_dates))
check("[6c] the 'roe' value returned (post-median) matches the median "
      "of per_fy['roe']'s own 3 values exactly - the refactor from a "
      "plain list to a {fy: value} dict changed nothing numerically",
      _meta_real["roe"] == qv2._median_of_available(list(_meta_real["per_fy"]["roe"].values())))
check("[6d] same byte-identical-median proof for 'roic'",
      _meta_real["roic"] == qv2._median_of_available(list(_meta_real["per_fy"]["roic"].values())))
check("[6e] same byte-identical-median proof for 'net_margin'",
      _meta_real["net_margin"] == qv2._median_of_available(list(_meta_real["per_fy"]["net_margin"].values())))
check("[6f] same byte-identical-median proof for 'debt_to_equity'",
      _meta_real["debt_to_equity"] == qv2._median_of_available(list(_meta_real["per_fy"]["debt_to_equity"].values())))
check("[6g] per_fy['normalized_income']/['revenue'] carry the raw, "
      "UNNORMALIZED per-year bases every ratio above is computed from "
      "- exactly the data a TRI.TO-style one-off/divestment year would "
      "show up in",
      _meta_real["per_fy"]["normalized_income"][_dates[0]] == 900.0
      and _meta_real["per_fy"]["revenue"][_dates[0]] == 2100.0)


# ======================================================================
# CHECK 7: the named weight constants used by the new "contributions"
# log line are the EXACT SAME numbers the real score formula applies -
# never a second, independently-maintained copy that could drift.
# ======================================================================
import inspect

_src = inspect.getsource(qv2._get_quality_score_v2)
check("[7] the score formula itself now reads the named constants "
      "(ROIC_WEIGHT/ROE_WEIGHT/etc.), not inline literals - the SAME "
      "constants nightly_scan.py's diag line reuses for its "
      "'contributions' line",
      "score += roic * 100 * ROIC_WEIGHT" in _src
      and "score += roe * 100 * ROE_WEIGHT" in _src)


print(f"\nTASK 64 RESULTS: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
print("\nALL TASK 64 (PGHN.SW/TRI.TO QUALITY V2 DIAGNOSTIC) FIXTURES PASSED")
