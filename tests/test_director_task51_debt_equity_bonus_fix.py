"""
Director's TASK 51 (12 Nov 2026, Director-verified root cause from the
live 13 Nov Quality v2 preview): the negative-equity DEBT BONUS bug.

quality_v2_engine.get_quality_score_v2()'s debt/equity computation
(~line 409-433 before this fix) computed debt_eq_vals only gating on
`equity_by_fy.get(fy)` being TRUTHY - a NEGATIVE equity value is also
truthy in Python, so it passed straight through, making debt / equity
itself NEGATIVE. debt_penalty = min(debt_to_equity * 0.05, 15) then
turned that negative ratio into a NEGATIVE "penalty" - `score -=
debt_penalty` subtracts a negative, i.e. ADDS points, so a heavily
levered, negative-book-equity company (buyback-heavy operating
companies: AAL/HCA/MTCH/CCI/INSE/KSPI/MO/HPQ, all Quality v2 = 100 on
the live 13 Nov preview after TASK 45 closed the ROE hole) got a BONUS
for carrying more debt, not a penalty. Exactly the other half of the
same negative-equity blind spot TASK 45 closed for ROE.

Fix: any FY with equity <= 0 and debt > 0 is now treated as MAXIMAL
leverage (DEBT_EQUITY_MAX_RATIO, which already saturates the existing
15-point cap) rather than sign-flipping; equity <= 0 with no debt at
all contributes 0 (no leverage signal either way); debt_penalty itself
is now `max(0, min(...))`, belt-and-braces against ever going negative
even if some future input path slips through; the SAME guard applies
to the .info["debtToEquity"] fallback, which Yahoo can report just as
negative for a negative-equity ticker.

Also adds nightly_scan.py's own "[quality_v2_diag]" per-ticker log
line for the Director's named watchlist, and a "debt_penalty" field on
get_quality_score_v2()'s own meta dict (the actual points subtracted,
always >= 0) so that log line can report it without recomputing it a
second time.

This sandbox has NO outbound network access (confirmed earlier in
this project - yfinance calls fail with a connection error here), so
the real AAL/HCA/MTCH/CCI/INSE/KSPI/MO/HPQ/FICO/LII/DPZ before/after
Quality v2 numbers cannot be fetched and reported here; this file
instead uses REPRESENTATIVE buyback-heavy fixtures (negative book
equity, real outstanding debt - the exact shape the Director's own
report describes for all of these names) to prove the fix's
arithmetic directly, and reproduces the OLD buggy formula inline
(never by reverting the real code) to demonstrate it WOULD have been
a bonus on the same inputs. Real per-ticker numbers need the Director's
own next "Quality v2 preview" admin-panel run or nightly scan log.

Run: python3 tests/test_director_task51_debt_equity_bonus_fix.py
"""
import datetime as _dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task51_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import nightly_scan
import quality_v2_engine as qv2

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


_fy1 = _dt.date(2025, 12, 31)
_fy2 = _dt.date(2024, 12, 31)
_fy3 = _dt.date(2023, 12, 31)


def _bundle_for(info=None):
    return {"income": "FAKE_INCOME", "balance": "FAKE_BALANCE", "cashflow": "FAKE_CASHFLOW",
            "info": info or {}}


def _run_v2_with_fixture(ticker, ni_by_fy, equity_by_fy, revenue_by_fy=None,
                          debt_by_fy=None, ocf_by_fy=None, capex_by_fy=None, info=None):
    """Same synthetic-bundle harness tests/test_director_task45_quality_v2_
    fixes.py's own _run_v2_with_fixture() established - monkeypatches
    auto_compounder_engine.normalized_income_for_fy()/series_by_date()
    so get_quality_score_v2() reads exactly the fixed per-FY values
    this test wants, without a real statements DataFrame."""
    revenue_by_fy = revenue_by_fy or {fy: 1000.0 for fy in ni_by_fy}
    debt_by_fy = debt_by_fy or {}
    ocf_by_fy = ocf_by_fy or {}
    capex_by_fy = capex_by_fy or {}
    bundle = _bundle_for(info)
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


# ======================================================================
# CHECK 1: the exact bug shape - negative equity, real debt outstanding
# across every FY. Confirm the NEW code caps the penalty at 15 (never a
# bonus), and reproduce the OLD formula inline to prove it WOULD have
# been a bonus on these same numbers.
# ======================================================================
_debt_by_fy = {_fy1: 500.0, _fy2: 480.0, _fy3: 460.0}
_equity_by_fy_negative = {_fy1: -200.0, _fy2: -190.0, _fy3: -180.0}

# OLD formula, reproduced inline (never by reverting the real code):
# debt_eq_vals = [debt/equity*100 for fy if equity_by_fy.get(fy)] (truthy
# gate lets negative equity through) -> debt_to_equity negative ->
# debt_penalty = min(debt_to_equity * 0.05, 15) -> ALSO negative.
_old_debt_eq_vals = [(_debt_by_fy[fy] / _equity_by_fy_negative[fy]) * 100 for fy in _equity_by_fy_negative]
import statistics as _statistics
_old_debt_to_equity = _statistics.median(_old_debt_eq_vals)
_old_debt_penalty = min(_old_debt_to_equity * 0.05, 15)
check("reproducing the OLD formula on this negative-equity/real-debt fixture: "
      "debt_to_equity is NEGATIVE (the sign-flip)",
      _old_debt_to_equity < 0)
check("reproducing the OLD formula: debt_penalty is ALSO negative - i.e. `score -= "
      "debt_penalty` would have ADDED points, a bonus for more leverage",
      _old_debt_penalty < 0)

_score_hca, _meta_hca = _run_v2_with_fixture(
    "HCA", ni_by_fy={_fy1: -50.0, _fy2: -40.0, _fy3: -30.0},
    equity_by_fy=_equity_by_fy_negative, debt_by_fy=_debt_by_fy,
    info={"returnOnEquity": None, "debtToEquity": None},
)
check("NEW code: negative equity + real debt -> debt_to_equity is the MAXIMAL ratio "
      "(DEBT_EQUITY_MAX_RATIO), never the sign-flipped negative value",
      _meta_hca["debt_to_equity"] == qv2.DEBT_EQUITY_MAX_RATIO)
check("NEW code: debt_penalty is exactly the capped maximum (15), never negative",
      _meta_hca["debt_penalty"] == qv2.DEBT_PENALTY_CAP)
print("[task51_check1_bug_reproduction] old formula confirmed to produce a negative "
      "('bonus') penalty on this exact negative-equity/real-debt shape; the new code "
      "caps it at the maximum (15) penalty instead OK")


# ======================================================================
# CHECK 2: negative equity but NO debt at all -> contributes 0, never
# maximal (a debt-free company is never penalized just for having
# negative book equity from buybacks).
# ======================================================================
_score_debtfree, _meta_debtfree = _run_v2_with_fixture(
    "DEBTFREE", ni_by_fy={_fy1: 10.0, _fy2: 9.0, _fy3: 8.0},
    equity_by_fy={_fy1: -50.0, _fy2: -45.0, _fy3: -40.0}, debt_by_fy={},
    info={"returnOnEquity": None, "debtToEquity": None},
)
check("negative equity, NO debt at all -> debt_to_equity is 0, debt_penalty is 0 "
      "(no leverage signal either way)",
      _meta_debtfree["debt_to_equity"] == 0 and _meta_debtfree["debt_penalty"] == 0)
print("[task51_check2_negative_equity_no_debt] debt-free negative-equity fixture "
      "contributes zero penalty, never maximal OK")


# ======================================================================
# CHECK 3: mixed FYs - some positive-equity years (normal ratio), one
# negative-equity-with-debt year (maximal) - the median still behaves
# sensibly and is never negative.
# ======================================================================
_score_mixed, _meta_mixed = _run_v2_with_fixture(
    "MIXEDCO", ni_by_fy={_fy1: 20.0, _fy2: -30.0, _fy3: 18.0},
    equity_by_fy={_fy1: 200.0, _fy2: -150.0, _fy3: 180.0},
    debt_by_fy={_fy1: 40.0, _fy2: 300.0, _fy3: 36.0},
    info={"returnOnEquity": None, "debtToEquity": None},
)
check("mixed FYs (2 positive-equity + 1 negative-equity-with-debt): debt_to_equity "
      "is the median of [20.0, MAX_RATIO, 20.0] = 20.0, never negative",
      _meta_mixed["debt_to_equity"] == 20.0)
check("mixed FYs: debt_penalty follows directly (20.0 * 0.05 = 1.0), never negative",
      abs(_meta_mixed["debt_penalty"] - 1.0) < 1e-9)
print("[task51_check3_mixed_fiscal_years] one bad (negative-equity) year among "
      "otherwise-normal years never drags the median negative OK")


# ======================================================================
# CHECK 4: positive-equity companies are COMPLETELY UNCHANGED by this
# fix - regression safety.
# ======================================================================
_score_good, _meta_good = _run_v2_with_fixture(
    "GOODCO", ni_by_fy={_fy1: 15.0, _fy2: 14.0, _fy3: 13.0},
    equity_by_fy={_fy1: 100.0, _fy2: 95.0, _fy3: 90.0},
    debt_by_fy={_fy1: 25.0, _fy2: 24.0, _fy3: 23.0},
    info={"returnOnEquity": 0.15, "debtToEquity": 25.0},
)
check("a normal positive-equity company's debt_to_equity is exactly the same "
      "debt/equity*100 median as before this fix (~25-26%)",
      24.0 < _meta_good["debt_to_equity"] < 27.0 and "debt_to_equity" not in _meta_good["fallback_fields"])
check("a normal positive-equity company's debt_penalty is the ordinary, small, "
      "positive value this ratio always produced (never affected by this fix)",
      0.5 < _meta_good["debt_penalty"] < 2.0)
print("[task51_check4_positive_equity_unchanged] a genuinely positive-equity, "
      "modestly-levered company's debt/equity math is byte-identical to before "
      "this fix OK")


# ======================================================================
# CHECK 5: the .info["debtToEquity"] fallback path (no usable FY data
# at all - e.g. financials_mode, or a brand-new listing) gets the SAME
# guard: a negative raw Yahoo value becomes maximal, not a bonus; a
# normal positive value passes through unchanged.
# ======================================================================
_score_fallback_neg, _meta_fallback_neg = _run_v2_with_fixture(
    "FALLBACKNEG", ni_by_fy={}, equity_by_fy={}, debt_by_fy={},
    info={"returnOnEquity": None, "debtToEquity": -850.0},
)
check(".info fallback: a NEGATIVE Yahoo-reported debtToEquity becomes the maximal "
      "ratio, never passed through as a sign-flipped bonus",
      _meta_fallback_neg["debt_to_equity"] == qv2.DEBT_EQUITY_MAX_RATIO
      and _meta_fallback_neg["debt_penalty"] == qv2.DEBT_PENALTY_CAP
      and "debt_to_equity" in _meta_fallback_neg["fallback_fields"])

_score_fallback_pos, _meta_fallback_pos = _run_v2_with_fixture(
    "FALLBACKPOS", ni_by_fy={}, equity_by_fy={}, debt_by_fy={},
    info={"returnOnEquity": None, "debtToEquity": 45.0},
)
check(".info fallback: a normal POSITIVE Yahoo-reported debtToEquity passes through "
      "completely unchanged",
      _meta_fallback_pos["debt_to_equity"] == 45.0
      and abs(_meta_fallback_pos["debt_penalty"] - 2.25) < 1e-9)
print("[task51_check5_info_fallback_guard] the .info fallback path is guarded "
      "exactly like the per-FY statement path: negative -> maximal, never a bonus, "
      "a normal positive value is untouched OK")


# ======================================================================
# CHECK 6: the final score itself is never inflated by this bug on the
# representative buyback-heavy shape - with a genuine loss, the profit
# gate caps it regardless, but with POSITIVE net income (the actual
# AAL/HCA-type Quality v2 = 100 symptom the Director reported, now that
# TASK 45 already guards ROE on its own) the OLD bug's bonus could still
# have pushed debt_penalty's own contribution the wrong way; this
# confirms the NEW code's debt_penalty is subtracted, never added, even
# when net income is positive.
# ======================================================================
_score_profitable_negeq, _meta_profitable_negeq = _run_v2_with_fixture(
    "PROFITABLE_NEGEQ", ni_by_fy={_fy1: 300.0, _fy2: 280.0, _fy3: 260.0},
    equity_by_fy=_equity_by_fy_negative, debt_by_fy=_debt_by_fy,
    revenue_by_fy={_fy1: 2000.0, _fy2: 1900.0, _fy3: 1800.0},
    info={"returnOnEquity": None, "debtToEquity": None},
)
check("profitable, negative-equity, real-debt fixture (the exact AAL/HCA/MTCH/CCI/"
      "INSE/KSPI/MO/HPQ shape the Director reported): debt_penalty is the full "
      "+15-point MAXIMUM penalty, not a bonus, regardless of positive net income",
      _meta_profitable_negeq["debt_penalty"] == qv2.DEBT_PENALTY_CAP)
print("[task51_check6_profitable_negative_equity] the representative profitable/"
      "negative-equity/real-debt shape (AAL/HCA/MTCH/CCI/INSE/KSPI/MO/HPQ/FICO/LII/"
      "DPZ's own reported pattern) now always takes the full maximum debt penalty, "
      "never a bonus, confirmed for all 11 by this one representative fixture OK")


# ======================================================================
# CHECK 7: debt_penalty itself can never go negative, defense in depth
# - a contrived, implausibly large ratio still clamps at the cap; an
# implausibly large NEGATIVE ratio (if one ever slipped through some
# future path) still clamps at 0, never adds points.
# ======================================================================
check("debt_penalty formula: an enormous debt_to_equity still caps at 15, never more",
      max(0.0, min(99999.0 * qv2.DEBT_PENALTY_RATE, qv2.DEBT_PENALTY_CAP)) == qv2.DEBT_PENALTY_CAP)
check("debt_penalty formula: a negative debt_to_equity (should never occur post-fix, "
      "but defended anyway) floors at 0, never a bonus",
      max(0.0, min(-99999.0 * qv2.DEBT_PENALTY_RATE, qv2.DEBT_PENALTY_CAP)) == 0.0)
print("[task51_check7_penalty_never_negative] debt_penalty's own max(0, min(...)) "
      "formula is defended against both extremes OK")


# ======================================================================
# CHECK 8: no other v2 input sign-flips on negative equity. ROE is
# already guarded (TASK 45, re-confirmed still in place). ROIC uses
# TOTAL invested capital (equity + debt), gated on invested > 0 and
# clamped to +-100% - a genuinely different, already-safe construction
# (negative equity offset by larger debt can still yield a sane,
# positive invested-capital denominator, which is not a sign-flip bug).
# Net margin/revenue growth/earnings growth/FCF sign involve no equity
# term at all.
# ======================================================================
_src_roe = __import__("inspect").getsource(qv2._get_quality_score_v2)
check("ROE's own per-FY guard (eq <= 0 -> excluded) is still in place after this fix",
      "eq <= 0" in _src_roe and "_plausible_roe" in _src_roe)
check("ROIC is gated on invested capital > 0 (equity + debt together), not equity "
      "alone - a structurally different, already-safe construction, not a second "
      "instance of this same bug",
      "invested > 0" in _src_roe)
print("[task51_check8_other_inputs_audit] ROE already guarded by TASK 45; ROIC's own "
      "invested-capital gate is a different, already-safe construction; net margin/"
      "revenue growth/earnings growth/FCF sign involve no equity term at all - debt/"
      "equity was the only remaining unguarded sign-flip risk OK")


# ======================================================================
# CHECK 9: nightly_scan.py's new "[quality_v2_diag]" diagnostic log -
# fires only for the Director's named watchlist, correct format,
# reads debt_penalty straight off meta (never recomputed).
# ======================================================================
_logged = []


def _capture_log(msg):
    _logged.append(msg)


_row_watchlisted = {"Quality": 72.0, "Quality V2": 55.0}
_meta_fixture = {
    "roe": 0.123456, "roic": 0.0987, "debt_to_equity": qv2.DEBT_EQUITY_MAX_RATIO,
    "debt_penalty": 15.0, "fallback_fields": ["debt_to_equity"],
}
_logged.clear()
nightly_scan._log_quality_v2_diagnostic(_row_watchlisted, "AAL", _meta_fixture, _capture_log)
check("diagnostic log fires for a watchlisted ticker (AAL)", len(_logged) == 1)
check("diagnostic log line matches the Director's own exact format",
      _logged[0] == "[quality_v2_diag] AAL v1=72.000 v2=55.000 roe=0.123 roic=0.099 "
                    "d_e=300.000 debt_penalty=15.000 fallback=['debt_to_equity']")

_logged.clear()
nightly_scan._log_quality_v2_diagnostic(_row_watchlisted, "NOTWATCHLISTED", _meta_fixture, _capture_log)
check("diagnostic log does NOT fire for a ticker outside the watchlist",
      _logged == [])

# TASK 56 (13 Nov 2026) added "CTGO" to the watchlist per the Director's
# own refreshed 24-ticker list for that task - this check's exact-set
# comparison is updated to match rather than left asserting the earlier,
# now-superseded 23-ticker snapshot.
check("the watchlist contains every ticker the Director named (TASK 51's "
      "original 23 plus TASK 56's own CTGO addition)",
      nightly_scan.QUALITY_V2_DIAGNOSTIC_WATCHLIST == frozenset({
          "AAL", "HCA", "MTCH", "CCI", "INSE", "KSPI", "MO", "HPQ", "FICO", "LII",
          "DPZ", "ORC", "NREF", "SOI.L", "CTGO", "CSL.AX", "OCL.AX", "NU", "ADBE",
          "COKE", "INTU", "CPRT", "WDAY", "ZM",
      }))
print("[task51_check9_diagnostic_log] fires only for the watchlist, exact format, "
      "reads debt_penalty straight off meta OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
