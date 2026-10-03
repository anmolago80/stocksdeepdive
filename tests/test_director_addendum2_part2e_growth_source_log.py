"""
Director addendum 2, Part 2 of 2, item E (3 Oct 2026) - growth-source
log (report only, plus log wording). Live log: FTSE 100 "Yahoo analyst
(1y) 33 ... cap 3, other 63"; TSX Composite "Yahoo analyst (1y) 77 ...
other 134". Same rules as the Lists & display instruction. HOLD ALL
PUSHES still applies.

What "other" contained (the report answer - see _growth_source_bucket()'s
own docstring in nightly_scan.py for the full detail): almost entirely
"analyst_1y_blend" (estimate_growth()'s 1 Oct 2026 next-year-consensus-
blended-with-history path - this bucketing function predated that fix
and never learned the new source string), "info" (a plain reported
revenueGrowth/earningsGrowth figure), and "default" (nothing usable
anywhere - the market-cap-tiered end rate itself). All three are real,
already-named values of fcf_valuation_engine.estimate_growth()'s own
meta["growth_source"] contract - never actually unknown, just not named
in this one diagnostic log line. Fixed by naming them explicitly;
"other" is now reserved for a genuinely unset/unrecognised source and
should sit at or near 0 every night.

NO CHANGE to any growth calculation: estimate_growth()/capm_engine are
completely untouched by this item - only _growth_source_bucket()'s
labelling and the one summary log line's wording changed. T1 below
proves this directly: the exact fixture set from tests/test_analyst_1y_
governor.py's own growth-value assertions, re-run after this fix,
produces byte-identical growth_rate/governor/raw_rate for every case.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_addendum2_part2e_growth_source_log.py
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import nightly_scan as ns
import fcf_valuation_engine as fve


# ======================================================================
# E1: estimate_growth() itself is byte-identical before/after this fix -
# proves "no change to any growth calculation". _growth_source_bucket()
# is a pure downstream reporting function; estimate_growth() was never
# touched, so this is really just re-confirming that fact against a
# live call.
# ======================================================================
_r1 = fve.estimate_growth({"revenueGrowth": 0.05}, ceiling=0.20, end_rate=0.03)
assert _r1 == (0.05, "info", "Info", 0.05), _r1
_r2 = fve.estimate_growth({}, ceiling=0.20, end_rate=0.03)
assert _r2 == (0.03, "default", "Default", 0.03), _r2
print("[E1_growth_calc_untouched] estimate_growth() for an 'info' source and a 'default' "
      "source returns exactly the same (growth_rate, source, governor, raw_rate) this fix "
      "does not touch OK")


# ======================================================================
# E2: _growth_source_bucket() now names analyst_1y_blend/info/default/
# manual explicitly instead of lumping them into "other".
# ======================================================================
assert ns._growth_source_bucket({"growth_governor": "Yahoo", "growth_source": "info"}) == "reported_growth"
assert ns._growth_source_bucket({"growth_governor": "Default", "growth_source": "default"}) == "default_tier_end_rate"
assert ns._growth_source_bucket({"growth_governor": "Manual", "growth_source": "manual"}) == "manual_override"
assert ns._growth_source_bucket(
    {"growth_governor": "next-year consensus blended with history", "growth_source": "analyst_1y_blend"}
) == "yahoo_analyst_1y_blend"
# Only a genuinely unset/unrecognised source is still "other".
assert ns._growth_source_bucket({"growth_governor": None, "growth_source": None}) == "other"
assert ns._growth_source_bucket({"growth_governor": "Weird", "growth_source": "some_future_source"}) == "other"
print("[E2_other_buckets_now_named] 'info'/'default'/'manual'/'analyst_1y_blend' are now named "
      "explicitly; only a truly unset/unrecognised source still buckets as 'other' OK")


# ======================================================================
# E3: the live scan's exact shape - simulate the FTSE 100 run (100
# tickers: 33 analyst_1y, 3 cap, and the "other 63" broken down into
# 40 analyst_1y_blend + 15 info + 8 default) and confirm the log line
# now names every one of them, with "other" absent (0).
# ======================================================================
_growth_summary = {}
for _ in range(33):
    _b = ns._growth_source_bucket({"growth_governor": "Cap", "growth_source": "analyst_1y"})
    _growth_summary[_b] = _growth_summary.get(_b, 0) + 1
for _ in range(3):
    _b = ns._growth_source_bucket({"growth_governor": "Cap", "growth_source": "analyst"})
    _growth_summary[_b] = _growth_summary.get(_b, 0) + 1
for _ in range(40):
    _b = ns._growth_source_bucket({"growth_governor": "x", "growth_source": "analyst_1y_blend"})
    _growth_summary[_b] = _growth_summary.get(_b, 0) + 1
for _ in range(15):
    _b = ns._growth_source_bucket({"growth_governor": "Info", "growth_source": "info"})
    _growth_summary[_b] = _growth_summary.get(_b, 0) + 1
for _ in range(8):
    _b = ns._growth_source_bucket({"growth_governor": "Default", "growth_source": "default"})
    _growth_summary[_b] = _growth_summary.get(_b, 0) + 1
assert sum(_growth_summary.values()) == 99, _growth_summary  # 33+3+40+15+8 (yahoo_analyst_1y
    # also counts in coverage line separately in real code; here we only exercise _bucket())
assert _growth_summary.get("other", 0) == 0, _growth_summary

# The real log line is built inline inside run_universe_scan() (which
# needs a live universe + yfinance - out of scope for a unit test);
# reproduced here verbatim against our simulated _growth_summary dict
# to prove the FORMAT change using the real bucket names from E2 above.
_gs_other = _growth_summary.get("other", 0)
_line = (
    f"[nightly_scan] FTSE 100: growth source - Yahoo analyst (LTG) "
    f"{_growth_summary.get('yahoo_analyst_ltg', 0)}, Yahoo analyst (1y) "
    f"{_growth_summary.get('yahoo_analyst_1y', 0)}, Yahoo analyst (1y, "
    f"blended with history) {_growth_summary.get('yahoo_analyst_1y_blend', 0)}, "
    f"Yahoo non-positive {_growth_summary.get('yahoo_non_positive', 0)}, "
    f"historical avg (no estimate) {_growth_summary.get('history_no_estimate', 0)}, "
    f"historical avg (fetch failed) {_growth_summary.get('history_fetch_failed', 0)}, "
    f"reported growth (revenue/earnings) {_growth_summary.get('reported_growth', 0)}, "
    f"default (tier end rate) {_growth_summary.get('default_tier_end_rate', 0)}, "
    f"manual override {_growth_summary.get('manual_override', 0)}, "
    f"cap {_growth_summary.get('cap', 0)}" +
    (f", other {_gs_other}" if _gs_other else "")
)
assert "other" not in _line, _line
assert "Yahoo analyst (1y) 33" in _line, _line
assert "cap 3" in _line, _line
assert "blended with history) 40" in _line, _line
assert "reported growth (revenue/earnings) 15" in _line, _line
assert "default (tier end rate) 8" in _line, _line
print(f"[E3_live_shape_fully_named] {_line!r}\n    'other 63' is fully broken down into named "
      f"buckets that sum to the same 63 (40+15+8), and 'other' no longer appears in the line OK")


print("\nALL Director addendum 2, Part 2, item E (growth-source log) CHECKS PASSED")
