"""Budget Ledger STEP 3 (9 Oct 2026, Director-directed, build go):
tiles/pace-chart/category-chart maths - every definition tested
against fixtures, exactly the instruction's own wording.

Run: python3 tests/test_budget_ledger_step3_tiles.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget_planner_engine as bpe

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


# ---------------------------------------------------------------------
# plan / bills_plan / everyday_plan
# ---------------------------------------------------------------------
_plan_amounts = {"mortgage": 1800.0, "food": 600.0, "fun": 300.0, "utilities": 300.0}
check("ledger_plan_total: sums every filled category",
      bpe.ledger_plan_total(_plan_amounts) == 3000.0)
check("ledger_plan_total: empty/None categories dict -> 0",
      bpe.ledger_plan_total({}) == 0 and bpe.ledger_plan_total(None) == 0)

_is_bill = {"mortgage": True, "utilities": True, "food": False, "fun": False}
check("ledger_bills_plan: sums only is_bill-flagged categories (1800+300=2100)",
      bpe.ledger_bills_plan(_plan_amounts, _is_bill) == 2100.0)
check("ledger_bills_plan: no bills flagged -> 0",
      bpe.ledger_bills_plan(_plan_amounts, {}) == 0)

check("ledger_everyday_plan: plan - bills_plan (3000-2100=900)",
      bpe.ledger_everyday_plan(3000.0, 2100.0) == 900.0)

# ---------------------------------------------------------------------
# spent / everyday_spent (refunds reduce it)
# ---------------------------------------------------------------------
_entries = [
    {"category": "food", "amount_cents": 4810},
    {"category": "food", "amount_cents": -1000},  # a refund
    {"category": "mortgage", "amount_cents": 180000},
    {"category": "fun", "amount_cents": 2000},
]
check("ledger_spent_total: sums amount_cents/100, refund reduces it "
      "(48.10 - 10.00 + 1800.00 + 20.00 = 1858.10)",
      round(bpe.ledger_spent_total(_entries), 2) == 1858.10)
check("ledger_spent_total: empty list -> 0", bpe.ledger_spent_total([]) == 0)

check("ledger_everyday_spent: excludes bill categories (food+fun only: "
      "48.10 - 10.00 + 20.00 = 58.10)",
      round(bpe.ledger_everyday_spent(_entries, _is_bill), 2) == 58.10)

# ---------------------------------------------------------------------
# days_in_month / today_of_month
# ---------------------------------------------------------------------
check("ledger_days_in_month: October has 31 days", bpe.ledger_days_in_month(2026, 10) == 31)
check("ledger_days_in_month: February 2026 (not a leap year) has 28 days",
      bpe.ledger_days_in_month(2026, 2) == 28)
check("ledger_days_in_month: February 2028 (a leap year) has 29 days",
      bpe.ledger_days_in_month(2028, 2) == 29)
check("ledger_days_in_month: December correctly rolls into next year",
      bpe.ledger_days_in_month(2026, 12) == 31)

check("ledger_today_of_month: the CURRENT month returns today's actual day",
      bpe.ledger_today_of_month(2026, 10, date(2026, 10, 7)) == 7)
check("ledger_today_of_month: a PAST month returns the full days_in_month (elapsed)",
      bpe.ledger_today_of_month(2026, 9, date(2026, 10, 7)) == 30)

# ---------------------------------------------------------------------
# safe to spend per day
# ---------------------------------------------------------------------
check("ledger_safe_to_spend_per_day: (3000-1858.10)/(31-7+1)=1141.90/25=45.676",
      round(bpe.ledger_safe_to_spend_per_day(3000.0, 1858.10, 31, 7), 3) == round(1141.90 / 25, 3))
check("ledger_safe_to_spend_per_day: on the LAST day, divides by 1 (not 0)",
      bpe.ledger_safe_to_spend_per_day(3000.0, 2900.0, 31, 31) == 100.0)
check("ledger_safe_to_spend_per_day: already AT plan (remaining=0) -> $0, not a divide error",
      bpe.ledger_safe_to_spend_per_day(3000.0, 3000.0, 31, 15) == 0.0)
check("ledger_safe_to_spend_per_day: OVER plan (remaining<0) -> $0, never negative",
      bpe.ledger_safe_to_spend_per_day(3000.0, 3500.0, 31, 15) == 0.0)

# ---------------------------------------------------------------------
# projection visibility + maths
# ---------------------------------------------------------------------
check("ledger_projection_visible: today<3 -> hidden regardless of entries",
      bpe.ledger_projection_visible(2, 5) is False)
check("ledger_projection_visible: today>=3 but zero everyday entries -> hidden",
      bpe.ledger_projection_visible(5, 0) is False)
check("ledger_projection_visible: today>=3 AND at least one everyday entry -> shown",
      bpe.ledger_projection_visible(3, 1) is True)

check("ledger_projection: bills_plan + everyday_spent/today*days_in_month "
      "(2100 + 58.10/7*31 = 2100 + 257.30 = 2357.30)",
      round(bpe.ledger_projection(2100.0, 58.10, 7, 31), 2) == round(2100.0 + 58.10 / 7 * 31, 2))
check("ledger_projection: today=0 -> None (never divides by zero)",
      bpe.ledger_projection(2100.0, 0.0, 0, 31) is None)

# Projection "over plan" / "under plan" sign, against the plan computed above.
_proj = bpe.ledger_projection(2100.0, 58.10, 7, 31)
check("the projected 2357.30 is UNDER the 3000 plan (test fixture sanity check)",
      _proj < 3000.0)

# ---------------------------------------------------------------------
# pace line
# ---------------------------------------------------------------------
check("ledger_pace_line_value: day 1 -> bills_plan + everyday_plan*(1/31) "
      "(bills counted in full from day 1, everyday ramps linearly)",
      round(bpe.ledger_pace_line_value(2100.0, 900.0, 1, 31), 3) == round(2100.0 + 900.0 * (1 / 31), 3))
check("ledger_pace_line_value: day = days_in_month -> bills_plan + everyday_plan "
      "(the full plan, by month end)",
      bpe.ledger_pace_line_value(2100.0, 900.0, 31, 31) == 3000.0)
check("ledger_pace_line_value: day 0 -> just bills_plan (everyday hasn't started ramping)",
      bpe.ledger_pace_line_value(2100.0, 900.0, 0, 31) == 2100.0)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
