"""Audit fix C2 (Fable finding V1, 10 Oct 2026, instruction_combined_
10oct.md PART C): a genuine DUAL-LISTED company (RIO.AX, NWS.AX,
JHX.AX, AMC.AX - the SAME economic interest listed on two separate
exchanges/registries, unlike SHARE_CLASS_PAIRS's dual-CLASS tickers)
has info["sharesOutstanding"] on its ASX line counting only that
exchange's own register, not the combined group total. That gap
routinely exceeds share_class_engine.DUAL_CLASS_SHARE_RATIO_MAX's own
3x ceiling - the existing tiers correctly reject a ratio that extreme
as "far more likely a bad Yahoo field" for everyone else, but for
these four explicitly-verified names it IS the real group total.

New, explicit-list-only path (share_class_engine._dual_listed_group_
shares()): checked before every existing tier, accepts info[
"impliedSharesOutstanding"] as the group share count ONLY when it
exceeds the 3x ceiling AND Yahoo's own marketCap/price independently
corroborates it within 10% - never on the ratio alone. Sets share_
count_note on acceptance (per this task's own explicit instruction),
unlike every other tier here (which only sets a note on rejection).

Expected live effect (owner's own report): these four ASX-listed
dual-listed names get their IV computed off the real (larger) group
share count instead of the understated ASX-only register - IV ~4x
lower, and should leave resolver_engine.dcf_looks_unreliable()'s
flagged list (today's inflated IV, divided by the UNDERSTATED shares,
can look like "more than 3x the current price" purely from the share-
count bug, not a real model/data problem).

Run: python3 tests/test_audit_c2_dual_listed_group_shares.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import share_class_engine as sce
import fcf_valuation_engine as fve
import resolver_engine

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


def _mk_cashflow_df(ocf, capex):
    cols = {}
    for i, (o, c) in enumerate(zip(ocf, capex)):
        cols[f"202{6 - i}-06-30"] = [o, c]
    return pd.DataFrame(cols, index=["Operating Cash Flow", "Capital Expenditure"])


# ======================================================================
# RIO.AX-shaped fixture: ASX-only register 900M shares, group total
# 3.6B (4x ratio - past the 3x ceiling), price $120, marketCap exactly
# price*group (432B) so marketCap/price reconstructs the group figure
# precisely (0% gap, comfortably inside the 10% tolerance).
# ======================================================================
RIO_LOCAL_SHARES = 900_000_000
RIO_GROUP_SHARES = 3_600_000_000   # 4.0x the local register
RIO_PRICE = 120.0
RIO_INFO = {
    "sharesOutstanding": RIO_LOCAL_SHARES,
    "impliedSharesOutstanding": RIO_GROUP_SHARES,
    "currentPrice": RIO_PRICE,
    "marketCap": RIO_PRICE * RIO_GROUP_SHARES,
    "currency": "AUD",
}

_shares, _flagged, _source, _note = sce.whole_company_shares(RIO_INFO, ticker="RIO.AX")
check("RIO.AX: group share count (3.6B, 4.0x) is accepted despite "
      "exceeding the 3x ceiling - corroborated by marketCap/price",
      _shares == RIO_GROUP_SHARES)
check("RIO.AX: flagged True, source 'dual_listed_group'",
      _flagged is True and _source == "dual_listed_group")
check("RIO.AX: share_count_note IS set on acceptance (this task's own "
      "explicit instruction - every other tier only sets a note on "
      "rejection)",
      _note is not None and "dual-listed" in _note.lower())

# ======================================================================
# Regression: the SAME extreme ratio for an ARBITRARY ticker NOT on
# the explicit list is still rejected exactly as before this fix -
# the new path is scoped strictly to the 4 named tickers.
# ======================================================================
_shares_other, _flagged_other, _source_other, _note_other = sce.whole_company_shares(
    RIO_INFO, ticker="RANDOMCO.AX",
)
check("an arbitrary ticker (not on DUAL_LISTED_TICKERS) with the exact "
      "same 4.0x ratio + corroborating marketCap is still REJECTED - "
      "falls back to plain sharesOutstanding, unchanged by this fix",
      _shares_other == RIO_LOCAL_SHARES and _flagged_other is False
      and _source_other is None)
check("...with the pre-existing 'above the sanity ceiling' rejection "
      "note, exactly as before this fix",
      _note_other is not None and "sanity ceiling" in _note_other)

# ======================================================================
# Regression: RIO.AX itself, but with NO corroborating marketCap (the
# cross-check genuinely can't run) - falls through to the pre-existing
# tiers too, never accepted on the ratio alone even for a listed name.
# ======================================================================
RIO_INFO_NO_CAP = dict(RIO_INFO)
RIO_INFO_NO_CAP.pop("marketCap")
_shares_nc, _flagged_nc, _source_nc, _note_nc = sce.whole_company_shares(
    RIO_INFO_NO_CAP, ticker="RIO.AX",
)
check("RIO.AX with no marketCap to cross-check against falls through "
      "to the ordinary rejection - never accepted on the ratio alone",
      _shares_nc == RIO_LOCAL_SHARES and _flagged_nc is False and _source_nc is None)

# ======================================================================
# Regression: RIO.AX with a marketCap that does NOT corroborate (e.g.
# a stale/bad marketCap field implying a totally different count) -
# also falls through, not accepted.
# ======================================================================
RIO_INFO_BAD_CAP = dict(RIO_INFO)
RIO_INFO_BAD_CAP["marketCap"] = RIO_PRICE * RIO_LOCAL_SHARES  # implies the LOCAL count, not group
_shares_bc, _flagged_bc, _source_bc, _note_bc = sce.whole_company_shares(
    RIO_INFO_BAD_CAP, ticker="RIO.AX",
)
check("RIO.AX whose marketCap implies the LOCAL count (not the group "
      "total) does NOT corroborate - rejected, not accepted",
      _shares_bc == RIO_LOCAL_SHARES and _flagged_bc is False and _source_bc is None)

# ======================================================================
# Regression: a ratio BELOW the 3x ceiling for a listed name goes
# through the ORDINARY tier-2 "implied" path unchanged (the new path
# only ever engages past the ceiling - see its own guard).
# ======================================================================
RIO_INFO_MODEST = dict(RIO_INFO)
RIO_INFO_MODEST["impliedSharesOutstanding"] = int(RIO_LOCAL_SHARES * 1.5)  # 1.5x, under the ceiling
RIO_INFO_MODEST["marketCap"] = RIO_PRICE * RIO_LOCAL_SHARES * 1.5
_shares_m, _flagged_m, _source_m, _note_m = sce.whole_company_shares(
    RIO_INFO_MODEST, ticker="RIO.AX",
)
check("RIO.AX with a modest 1.5x ratio (under the 3x ceiling) still "
      "goes through the ORDINARY tier-2 'implied' path, not the new "
      "dual-listed override",
      _shares_m == int(RIO_LOCAL_SHARES * 1.5) and _source_m == "implied")

for _other_ticker in ("NWS.AX", "JHX.AX", "AMC.AX"):
    _s, _f, _src, _n = sce.whole_company_shares(RIO_INFO, ticker=_other_ticker)
    check(f"{_other_ticker} is on the explicit list too - same group-share "
          f"acceptance as RIO.AX for the identical fixture shape",
          _s == RIO_GROUP_SHARES and _src == "dual_listed_group")


# ======================================================================
# Ticker-level IV effect, end to end through dcf_intrinsic_value():
# the SAME fixture, "RIO.AX" (fixed - group shares used) vs a ticker
# not on the list (representing this exact bug's pre-fix behaviour,
# since the code itself can no longer be toggled off for a real
# listed name) - same cashflow/info/price otherwise.
# ======================================================================
_OCF = [5_000_000_000.0, 4_800_000_000.0, 4_700_000_000.0, 4_900_000_000.0, 5_000_000_000.0]
_CAPEX = [-500_000_000.0] * 5
_CF = _mk_cashflow_df(_OCF, _CAPEX)
_COMMON = dict(cashflow_df=_CF, currency="AUD", discount_rate=0.08, perpetual_rate=0.025, growth_rate=0.04)

iv_buggy, _, meta_buggy = fve.dcf_intrinsic_value("RANDOMCO.AX", info=RIO_INFO, **_COMMON)
iv_fixed, _, meta_fixed = fve.dcf_intrinsic_value("RIO.AX", info=RIO_INFO, **_COMMON)

check("the fixed (group-shares) IV is meaningfully lower than the "
      "buggy (local-shares) IV for the identical fundamentals",
      0 < iv_fixed < iv_buggy)
_ratio = iv_buggy / iv_fixed
check(f"the magnitude matches the owner's own '~4x lower' report "
      f"(actual ratio: {_ratio:.2f}x, matching RIO_GROUP_SHARES/"
      f"RIO_LOCAL_SHARES = {RIO_GROUP_SHARES / RIO_LOCAL_SHARES:.2f}x)",
      3.5 < _ratio < 4.5)
check("share_count_source differs correctly between the two runs",
      meta_buggy.get("share_count_source") is None
      and meta_fixed.get("share_count_source") == "dual_listed_group")

print(f"  [ticker-level] RIO.AX-shaped fixture: old (buggy, ASX-only-register) "
      f"IV=${iv_buggy:.2f} AUD -> new (fixed, group shares) IV=${iv_fixed:.2f} AUD "
      f"({(iv_fixed - iv_buggy) / iv_buggy:+.1%})")

# Bonus confirmation (owner's own report, "should leave the DCF-
# unreliable list"): the understated-shares bug inflates IV enough
# that resolver_engine.dcf_looks_unreliable() (IV > 3x price) can
# false-positive purely from the share-count error at a price in this
# fixture's own realistic range - fixed removes that false positive at
# the same price. Illustrative of the DOWNSTREAM effect; the sanity-
# flag logic itself is untouched by this commit (that's C4/V4).
_check_price = 30.0  # a plausible price where the buggy IV (~4x too high) trips the
                      # sanity flag but the fixed one, correctly sized, does not
check(f"dcf_looks_unreliable() false-positives on the buggy (inflated) "
      f"IV (${iv_buggy:.2f}) at a plausible price (${_check_price:.2f}), and "
      f"does not on the fixed IV (${iv_fixed:.2f}) for the same price",
      resolver_engine.dcf_looks_unreliable(iv_buggy, _check_price)
      and not resolver_engine.dcf_looks_unreliable(iv_fixed, _check_price))

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
