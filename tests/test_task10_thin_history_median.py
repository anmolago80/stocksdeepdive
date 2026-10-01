"""
Task 10 thin-history median fix (owner-directed, 1 Oct 2026, Commit 3 of
instruction_dcf_unreliable_pool_step4_nightly.md, acting on the growth/
PE-forward task's own Part B diagnostic finding).

Fact: fcf_valuation_engine.normalized_base_and_series()'s Task 10
outlier-swap used `median = sorted(recent)[len(recent) // 2]` - for
exactly 2 comparison points (`recent`), index 1 is always the LARGER of
the two values. `base` is itself `recent[0]` whenever this branch runs
(capex_basis == "average", the only case this swap applies to), so the
old code effectively compared base against max(base, other) - zero
deviation whenever base ITSELF was the inflated one, meaning an
inflated latest-year OCF could never be swapped out even though this
check exists specifically to catch a distorted base.

Fix: with exactly 2 points, compare base against the single OTHER point
(`recent[1]`) directly, instead of against whichever of the two happens
to be larger - two-sided by construction (see fcf_valuation_engine.py's
own comment at the fix site for why this, not a literal unconditional
min(), is what makes BOTH directions actually swap). >=3 points keep
the real median, byte-identical to before. <2 points: unchanged (no
swap either way). FCF_OUTLIER_THRESHOLD (40%) is unchanged.

Run: python3 tests/test_task10_thin_history_median.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcf_valuation_engine as fve


def _mk_cashflow_df(ocf, capex):
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


# ======================================================================
# CHECK 1: two-point INFLATED-latest - base (latest year) well above the
# single prior year -> swap fires, base drops to the prior (correct,
# lower) reference.
# ======================================================================
_inflated_ocf = [200.0, 100.0]
_inflated_capex = [-20.0, -20.0]
_inflated_cf = _mk_cashflow_df(_inflated_ocf, _inflated_capex)
_base_i, _series_i, _src_i, _bn_i, _cb_i, _meta_i = fve.normalized_base_and_series(
    _inflated_cf, info={})
assert _bn_i is True, _bn_i
assert abs(_base_i - 80.0) < 1e-9, _base_i  # swapped to prior year (100 - 20 capex)
assert _cb_i == "average", _cb_i
print(f"[two_point_inflated_latest_swaps] latest OCF 200 vs single prior 100 (capex -20 "
      f"both years) -> base swaps from 180 (latest, inflated) to {_base_i:.1f} (the prior "
      "year, the correct reference) OK")

# ======================================================================
# CHECK 2: two-point DEPRESSED-latest - base (latest year) well below
# the single prior year -> swap fires THE OTHER WAY, base rises to the
# prior (correct, higher) reference. This is the case the OLD code
# could never produce a wrong non-swap for by coincidence, but confirms
# the fix is genuinely two-sided, not just flipped for one direction.
# ======================================================================
_depressed_ocf = [100.0, 200.0]
_depressed_capex = [-20.0, -20.0]
_depressed_cf = _mk_cashflow_df(_depressed_ocf, _depressed_capex)
_base_d, _series_d, _src_d, _bn_d, _cb_d, _meta_d = fve.normalized_base_and_series(
    _depressed_cf, info={})
assert _bn_d is True, _bn_d
assert abs(_base_d - 180.0) < 1e-9, _base_d  # swapped UP to prior year (200 - 20 capex)
print(f"[two_point_depressed_latest_swaps_other_way] latest OCF 100 vs single prior 200 "
      f"(capex -20 both years) -> base swaps from 80 (latest, depressed) to {_base_d:.1f} "
      "(the prior year, the correct higher reference) - two-sided confirmed OK")

# ======================================================================
# CHECK 3: three-point fixtures are byte-identical to the pre-fix
# formula (sorted(recent)[1], the real median of 3) - both a case where
# the swap fires and one where it doesn't, matching this module's own
# pre-existing test_dcf_outlier_guards.py fixture shapes.
# ======================================================================
_three_ocf_swaps = [50.0, 100.0, 100.0]  # base well below the other two -> swap
_three_capex = [-10.0] * 3
_three_cf_swaps = _mk_cashflow_df(_three_ocf_swaps, _three_capex)
_base_3s, _, _, _bn_3s, _, _ = fve.normalized_base_and_series(_three_cf_swaps, info={})
_expected_median_3s = sorted([v + c for v, c in zip(_three_ocf_swaps, _three_capex)])[1]
assert _bn_3s is True, _bn_3s
assert abs(_base_3s - _expected_median_3s) < 1e-9, (_base_3s, _expected_median_3s)
print(f"[three_point_swap_unchanged] 3-point fixture (50/100/100, swap fires) -> base="
      f"{_base_3s:.1f}, matching sorted(recent)[1]={_expected_median_3s:.1f} exactly, "
      "the pre-existing formula, byte-identical OK")

_three_ocf_stable = [95.0, 100.0, 105.0]  # close together -> no swap
_three_cf_stable = _mk_cashflow_df(_three_ocf_stable, _three_capex)
_base_3n, _, _, _bn_3n, _, _ = fve.normalized_base_and_series(_three_cf_stable, info={})
assert _bn_3n is False, _bn_3n
assert abs(_base_3n - (95.0 - 10.0)) < 1e-9, _base_3n  # unswapped raw base
print(f"[three_point_no_swap_unchanged] 3-point fixture (95/100/105, within 40%) -> no "
      f"swap, base={_base_3n:.1f} (the raw latest-year figure) OK")

# ======================================================================
# CHECK 4: fewer than 2 points (a single-year cash-flow statement) -
# unchanged, no swap possible either way (the `len(recent) >= 2` guard).
# ======================================================================
_one_ocf = [100.0]
_one_capex = [-10.0]
_one_cf = _mk_cashflow_df(_one_ocf, _one_capex)
_base_1, _, _, _bn_1, _, _ = fve.normalized_base_and_series(_one_cf, info={})
assert _bn_1 is False, _bn_1
assert abs(_base_1 - 90.0) < 1e-9, _base_1
print("[single_point_unchanged] only 1 year of cash-flow history -> no swap (len(recent) "
      "< 2 guard unchanged) OK")

print("\nALL TASK 10 THIN-HISTORY MEDIAN FIXTURES PASSED")
