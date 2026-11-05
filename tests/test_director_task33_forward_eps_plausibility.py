"""
TASK 33 (Director-directed, owner-reported, 5 Nov 2026): PE Forward
used an implausible Yahoo forwardEps with no sanity check (COKE live
case: forwardEps $38.94 vs normalised/diluted EPS $6.82 -> PE Forward
$836). COKE did a 10-for-1 split (effective 27 May 2025) - possibly a
stale/mis-scaled Yahoo estimate, NOT verified as the root cause; the
fix itself is a general plausibility guard, not a COKE-specific patch.

Root cause (verified): auto_compounder_engine._pe_forward_method()
used info["forwardEps"] whenever > 0, with no check against the
normalised EPS it already computes for its OWN "today's P/E"
multiple.

Fix: forward_eps is rejected (falls back to the existing trailing-EPS-
compounded-at-g_earn path, exactly as if Yahoo had no forward EPS at
all) whenever forward_eps / normalised_eps falls outside
[FORWARD_EPS_PLAUSIBILITY_MIN_RATIO, FORWARD_EPS_PLAUSIBILITY_MAX_RATIO]
= [0.5, 2.0]. One "[forward_eps] <TICKER> rejected ..." line is logged
via print() on rejection, and the caller (_build_fair_value()) shows an
explanatory "Forward EPS" note row in the PE Forward detail text.

Grep for every other use of info["forwardEps"] (TASK 33 item 2) found
exactly one other site: fundamentals_data._resolve_forward_eps_unit()
(GBp pence/pounds unit resolution) - it already has its own, different
ratio-based plausibility window (vs trailingEps, to pick a UNIT, not
to reject a value) and leaves forwardEps unset entirely on an
unresolvable ratio; it does not itself feed a valuation - whatever it
resolves (or leaves unset) flows into info["forwardEps"], which is
read ONLY by _pe_forward_method() (confirmed by full-codebase grep),
already fixed here. Deep Dive, the scanner's "analyst" growth source
(capm_engine.get_growth_estimates_5y(), a SEPARATE Yahoo growth_
estimates table, not forwardEps), and "forward P/E displays" were all
checked and do not read info["forwardEps"] anywhere - no change needed
there.

Covers the task's own listed tests:
  - COKE-shaped fixture (forwardEps 38.94, normalised 6.82, ratio
    5.7x) -> rejected, PE Forward falls back to the trailing-EPS-
    compounded path (a sane range, not $836), rejection note shown in
    the PE Forward detail text.
  - A normal ratio (1.1x) is unchanged - forwardEps still used.
  - Missing forwardEps is unchanged - straight to the trailing-EPS-
    compounded fallback, no rejection note (nothing to reject).
  - The negative-earnings path is unchanged (no usable normalised EPS
    at all -> "negative_earnings" reason, regardless of forwardEps).
  - The boundary ratios themselves (exactly 0.5x/2.0x -> NOT rejected;
    just outside -> rejected).
  - The exact "[forward_eps] ..." log line format.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task33_forward_eps_plausibility.py
"""
import io
import os
import sys
from contextlib import redirect_stdout

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace

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


def _mk_income_df(eps_newest_first):
    cols = {}
    for i, eps in enumerate(eps_newest_first):
        cols[f"{2026 - i}-06-30"] = [eps, 100.0, 10.0]
    return pd.DataFrame(cols, index=["Diluted EPS", "Operating Income", "Reconciled Depreciation"])


def _mk_bundle(eps_flat, price_now, forward_eps=None, currency="USD", symbol=None):
    """5 flat fiscal years (eps_flat/share, held constant so nothing is
    ever flagged a one-off distortion) with a matching flat price so
    the 5-yr-average P/E logic (irrelevant to this task) stays out of
    the way - same shape as test_pe_forward_5yr_average.py's own
    _mk_bundle(), trimmed to this file's own needs."""
    eps_5y = [eps_flat] * 5
    income = _mk_income_df(eps_5y)
    dates = [f"{2022 + i}-06-30" for i in range(5)]
    prices = [price_now] * 5
    info = {"currentPrice": price_now, "currency": currency, "sharesOutstanding": 100_000_000}
    if symbol:
        info["symbol"] = symbol
    if forward_eps is not None:
        info["forwardEps"] = forward_eps
    return {"info": info, "income": income, "prices_10y": {"dates": dates, "prices": prices}}


_G_EARN = 0.08
_DISCOUNT = 0.09


def _normalized_eps_for(bundle):
    neps = ace._normalized_eps(bundle)
    assert neps["source"] == "median5_clean", neps
    assert neps["distorted_years"] == [], neps
    return neps


# ======================================================================
# CHECK 1: implausible_forward_eps_ratio() - the shared helper itself.
# ======================================================================
check("implausible_forward_eps_ratio: 5.7x (COKE's own ratio) is implausible",
      ace.implausible_forward_eps_ratio(38.94, 6.82))
check("implausible_forward_eps_ratio: 1.1x is plausible",
      not ace.implausible_forward_eps_ratio(11.0, 10.0))
check("implausible_forward_eps_ratio: exactly 2.0x is NOT implausible (inclusive ceiling)",
      not ace.implausible_forward_eps_ratio(20.0, 10.0))
check("implausible_forward_eps_ratio: just over 2.0x (2.01x) IS implausible",
      ace.implausible_forward_eps_ratio(20.1, 10.0))
check("implausible_forward_eps_ratio: exactly 0.5x is NOT implausible (inclusive floor)",
      not ace.implausible_forward_eps_ratio(5.0, 10.0))
check("implausible_forward_eps_ratio: just under 0.5x (0.49x) IS implausible",
      ace.implausible_forward_eps_ratio(4.9, 10.0))
check("implausible_forward_eps_ratio: no normalised EPS to check against -> never implausible "
      "(nothing to reject it against)",
      not ace.implausible_forward_eps_ratio(38.94, None)
      and not ace.implausible_forward_eps_ratio(38.94, 0)
      and not ace.implausible_forward_eps_ratio(38.94, -1.0))
print("[task33_c1_plausibility_helper] implausible_forward_eps_ratio() matches the named "
      "[0.5, 2.0] window exactly, inclusive at both ends OK")


# ======================================================================
# CHECK 2: COKE-shaped fixture - forwardEps 38.94 vs normalised EPS
# 6.82 (ratio 5.7x) is rejected; PE Forward falls back to the trailing-
# EPS-compounded path (a sane range, not an $836-style blow-up); one
# "[forward_eps] ..." line is logged; a rejection note is returned.
# ======================================================================
_COKE_FORWARD_EPS = 38.94
_COKE_NORMALIZED_EPS = 6.82
_COKE_PRICE = 1400.0  # plausible COKE-ish price, not load-bearing
_coke_bundle = _mk_bundle(_COKE_NORMALIZED_EPS, _COKE_PRICE, forward_eps=_COKE_FORWARD_EPS,
                          symbol="COKE")
_coke_neps = _normalized_eps_for(_coke_bundle)

_log = io.StringIO()
with redirect_stdout(_log):
    _result_coke = ace._pe_forward_method(_coke_bundle, _G_EARN, _DISCOUNT, _coke_neps)
assert _result_coke is not None
(_coke_value, _coke_eps5, _coke_fair_pe, _coke_today_pe, _coke_year5, _coke_reason,
 _coke_fwd_used, _coke_source, _coke_rejected_note) = _result_coke

check("COKE: no 'reason' (a value was still produced, via the fallback)", _coke_reason is None)
check("COKE: forward_eps_used is None - the forwardEps branch did NOT fire",
      _coke_fwd_used is None)
check("COKE: Forecast EPS(yr5) is built from the TRAILING-EPS-compounded fallback "
      "(normalised EPS x (1+g)^5), never from the rejected forwardEps",
      abs(_coke_eps5 - _COKE_NORMALIZED_EPS * (1 + _G_EARN) ** 5) < 0.01)
check("COKE: the rejection note names the exact 5.7x ratio",
      _coke_rejected_note == "analyst forward EPS rejected: 5.7x normalised EPS")
_coke_hypothetical_unrejected_eps5 = _COKE_FORWARD_EPS * (1 + _G_EARN) ** 4
_coke_hypothetical_unrejected_value = (
    _coke_hypothetical_unrejected_eps5 * _coke_fair_pe / ((1 + _DISCOUNT) ** 5)
)
check("COKE: PE Forward value is MATERIALLY lower than what the rejected forwardEps would "
      "have produced (same fair_pe_5y/discount, forwardEps path) - the live $836-style "
      "blow-up this task exists to prevent",
      _coke_value is not None and _coke_value < _coke_hypothetical_unrejected_value * 0.4)
check("COKE: exactly one '[forward_eps] COKE rejected ...' line was logged, naming the "
      "forwardEps/normalised-EPS/ratio values",
      "[forward_eps] COKE rejected forwardEps=38.94 vs normalised EPS=6.82 (ratio 5.7x)"
      in _log.getvalue())
print(f"[task33_c2_coke_rejected] COKE-shaped forwardEps=38.94 vs normalised EPS=6.82 "
      f"(5.7x) rejected - PE Forward=${_coke_value:.2f} (sane), note={_coke_rejected_note!r} OK")

# End-to-end through _build_fair_value(): the note shows in the PE
# Forward detail text on the Fair Value tab.
_coke_dcf_result = {"growth": _G_EARN, "discount_rate": _DISCOUNT}
_coke_fv = ace._build_fair_value(_coke_bundle, "COKE", _coke_dcf_result, None)
_coke_pe_forward_rows = {r["label"]: r for r in _coke_fv["valuation_inputs"]["COKE"]["pe_forward"]}
check("COKE end-to-end: the Fair Value tab's PE Forward detail text includes a 'Forward "
      "EPS' row naming the rejection, flagged",
      "Forward EPS" in _coke_pe_forward_rows
      and _coke_pe_forward_rows["Forward EPS"]["value"] == _coke_rejected_note
      and _coke_pe_forward_rows["Forward EPS"].get("flagged") is True)
check("COKE end-to-end: the Fair Value tab's own pe_forward Intrinsic Value matches the "
      "direct _pe_forward_method() call above (same rejected-fallback value)",
      abs(_coke_fv["valuation_methods"]["COKE"]["pe_forward"] - _coke_value) < 0.01)
print("[task33_c2b_coke_detail_text_shown] the rejection note appears in _build_fair_value()'s "
      "own PE Forward detail text, exactly as the task asks OK")


# ======================================================================
# CHECK 3: a normal ratio (1.1x) is UNCHANGED - forwardEps still used,
# no rejection note, no log line.
# ======================================================================
_NORMAL_NORMALIZED_EPS = 10.0
_NORMAL_FORWARD_EPS = 11.0  # 1.1x
_normal_bundle = _mk_bundle(_NORMAL_NORMALIZED_EPS, 300.0, forward_eps=_NORMAL_FORWARD_EPS,
                            symbol="NORMAL")
_normal_neps = _normalized_eps_for(_normal_bundle)
_log2 = io.StringIO()
with redirect_stdout(_log2):
    _result_normal = ace._pe_forward_method(_normal_bundle, _G_EARN, _DISCOUNT, _normal_neps)
(_n_value, _n_eps5, _n_fair_pe, _n_today_pe, _n_year5, _n_reason, _n_fwd_used, _n_source,
 _n_rejected_note) = _result_normal
check("normal ratio (1.1x): forward_eps_used == 11.0 - the forwardEps branch DID fire",
      _n_fwd_used == _NORMAL_FORWARD_EPS)
check("normal ratio (1.1x): Forecast EPS(yr5) is built from forwardEps compounded 4 more "
      "years, not the trailing-EPS fallback",
      abs(_n_eps5 - _NORMAL_FORWARD_EPS * (1 + _G_EARN) ** 4) < 0.01)
check("normal ratio (1.1x): no rejection note", _n_rejected_note is None)
check("normal ratio (1.1x): nothing logged - no rejection happened", _log2.getvalue() == "")
print("[task33_c3_normal_ratio_unchanged] a plausible 1.1x forwardEps/normalised-EPS ratio "
      "is completely unaffected by this task OK")


# ======================================================================
# CHECK 4: missing forwardEps is UNCHANGED - straight to the trailing-
# EPS-compounded fallback, no rejection note (there is nothing to
# reject), no log line.
# ======================================================================
_missing_bundle = _mk_bundle(10.0, 300.0, forward_eps=None, symbol="MISSING")
_missing_neps = _normalized_eps_for(_missing_bundle)
_log3 = io.StringIO()
with redirect_stdout(_log3):
    _result_missing = ace._pe_forward_method(_missing_bundle, _G_EARN, _DISCOUNT, _missing_neps)
(_m_value, _m_eps5, _m_fair_pe, _m_today_pe, _m_year5, _m_reason, _m_fwd_used, _m_source,
 _m_rejected_note) = _result_missing
check("missing forwardEps: forward_eps_used is None (never had one)", _m_fwd_used is None)
check("missing forwardEps: Forecast EPS(yr5) is the trailing-EPS-compounded fallback",
      abs(_m_eps5 - 10.0 * (1 + _G_EARN) ** 5) < 0.01)
check("missing forwardEps: no rejection note - this is the ordinary no-forecast path, "
      "never a rejection", _m_rejected_note is None)
check("missing forwardEps: nothing logged", _log3.getvalue() == "")
print("[task33_c4_missing_forward_eps_unchanged] no forwardEps at all behaves exactly as "
      "before this task - no rejection note, no log line OK")


# ======================================================================
# CHECK 5: the negative-earnings path is UNCHANGED - no usable
# normalised EPS at all -> "negative_earnings", regardless of whether
# forwardEps itself was implausible or not.
# ======================================================================
_neg_income = _mk_income_df([-5.0] * 5)
_neg_bundle = {
    "info": {"currentPrice": 100.0, "currency": "USD", "sharesOutstanding": 100_000_000,
             "forwardEps": 38.94, "symbol": "NEG"},
    "income": _neg_income,
    "prices_10y": {"dates": [f"{2022 + i}-06-30" for i in range(5)], "prices": [100.0] * 5},
}
_neg_neps = ace._normalized_eps(_neg_bundle)
check("negative-earnings fixture: normalised EPS is itself non-positive (sanity)",
      (_neg_neps.get("value") or -1) <= 0)
_result_neg = ace._pe_forward_method(_neg_bundle, _G_EARN, _DISCOUNT, _neg_neps)
check("negative earnings: reason == 'negative_earnings', regardless of the implausible "
      "forwardEps also present on this fixture",
      _result_neg[5] == "negative_earnings")
check("negative earnings: value is None (method withheld, as before this task)",
      _result_neg[0] is None)
print("[task33_c5_negative_earnings_unchanged] no usable normalised EPS -> "
      "'negative_earnings', exactly as before this task, whether or not forwardEps was "
      "itself implausible OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
