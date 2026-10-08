"""
Proposal 5 of the Director's PART 3 numbered fix-proposal round (8 Oct
2026, instruction_health_fixes_chart_and_new_markets.md, CANADA ONLY):
"one Bank of Canada rate per scan run - fetched once at the start of a
run, reused for every ticker in it." The retry/backoff half of this
proposal (committed separately as 5a4e790, mislabeled "Proposal 3" in
that commit - corrected here per the Director's own review) relied on
get_ca_risk_free_rate_live()'s own @st.cache_data(ttl=86400) 24h cache
to make this incidentally true; the Director's review made clear that
is not sufficient - this file covers the EXPLICIT, run-scoped mechanism
built on top of it.

Covers:
  - capm_engine.get_ca_risk_free_rate_for_run(): the first call in a
    "run" actually fetches; every subsequent call, however many, gets
    the exact same (rate, source) tuple with ZERO further calls to
    get_ca_risk_free_rate_live() - even across MULTIPLE calls to
    resolve_discount_rate_by_market_cap() for different CAD tickers.
  - reset_ca_risk_free_run_cache(): clears it, so the NEXT "run"
    fetches fresh.
  - nightly_scan.run_universe_scan() calls reset_ca_risk_free_run_
    cache() once, at its own top, before doing any per-ticker work -
    proof it's wired into the real nightly scan, not just a function
    nothing calls.
  - resolve_discount_rate_by_market_cap()'s CAD branch now calls
    get_ca_risk_free_rate_for_run(), never get_ca_risk_free_rate_live()
    directly - USD/AUD/GBP/JPY branches are untouched.

Run: python3 tests/test_fixproposal5_boc_one_rate_per_run.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capm_engine as ce

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
# CHECK 1-3: get_ca_risk_free_rate_for_run() - fetch once, reuse after.
# ======================================================================
ce.reset_ca_risk_free_run_cache()
with mock.patch.object(ce, "get_ca_risk_free_rate_live", return_value=(0.035, "live")) as _live:
    _r1 = ce.get_ca_risk_free_rate_for_run()
    _r2 = ce.get_ca_risk_free_rate_for_run()
    _r3 = ce.get_ca_risk_free_rate_for_run()
check("first call fetches: (0.035, 'live')", _r1 == (0.035, "live"))
check("second/third calls return the SAME tuple, with ZERO further calls to "
      "get_ca_risk_free_rate_live() - 3 calls to get_ca_risk_free_rate_for_run(), "
      "exactly 1 real fetch",
      _r2 == _r1 and _r3 == _r1 and _live.call_count == 1)

# ======================================================================
# CHECK 4: reset_ca_risk_free_run_cache() - the next "run" fetches fresh.
# ======================================================================
ce.reset_ca_risk_free_run_cache()
with mock.patch.object(ce, "get_ca_risk_free_rate_live", return_value=(0.040, "live")) as _live2:
    _r4 = ce.get_ca_risk_free_rate_for_run()
check("after reset_ca_risk_free_run_cache(), the NEXT run fetches fresh - a different "
      "mocked rate (0.040) now comes through, and the fetch actually fired",
      _r4 == (0.040, "live") and _live2.call_count == 1)

# ======================================================================
# CHECK 5-6: resolve_discount_rate_by_market_cap()'s CAD branch -
# MULTIPLE tickers in the same run share one BoC fetch; the retry/
# backoff logic itself (get_ca_risk_free_rate_live(), committed
# separately) only ever runs once per run, however many CAD tickers
# are scanned.
# ======================================================================
ce.reset_ca_risk_free_run_cache()
_cad_info = {"currency": "CAD", "marketCap": 5_000_000_000}
with mock.patch.object(ce, "get_ca_risk_free_rate_live", return_value=(0.038, "live")) as _live3:
    _rate_a, _meta_a = ce.resolve_discount_rate_by_market_cap(_cad_info, "CAD")
    _rate_b, _meta_b = ce.resolve_discount_rate_by_market_cap(_cad_info, "CAD")
    _rate_c, _meta_c = ce.resolve_discount_rate_by_market_cap(_cad_info, "CAD")
check("three DIFFERENT CAD tickers resolved in the same run all get the identical "
      "risk-free rate (0.038) and identical discount rate",
      _meta_a["risk_free_used"] == 0.038 and _meta_b["risk_free_used"] == 0.038
      and _meta_c["risk_free_used"] == 0.038 and _rate_a == _rate_b == _rate_c)
check("exactly ONE real call to get_ca_risk_free_rate_live() for all three tickers - "
      "'one Bank of Canada rate per scan run', proven at the real call site",
      _live3.call_count == 1)

# ======================================================================
# CHECK 7: nightly_scan.run_universe_scan() calls reset_ca_risk_free_
# run_cache() at its own top - wired into the real nightly scan.
# ======================================================================
import inspect
import nightly_scan
_src = inspect.getsource(nightly_scan.run_universe_scan)
check("run_universe_scan()'s own source calls capm_engine.reset_ca_risk_free_run_cache()",
      "capm_engine.reset_ca_risk_free_run_cache()" in _src)
# Confirm it's near the TOP of the function (before the main per-ticker loop
# begins), not buried somewhere irrelevant - the reset call's own line number
# should come before the function's first "for " ticker-loop line.
_lines = _src.splitlines()
_reset_line = next(i for i, l in enumerate(_lines) if "reset_ca_risk_free_run_cache()" in l)
_first_for_line = next((i for i, l in enumerate(_lines) if l.strip().startswith("for ")), None)
check("the reset call comes before the function's own first 'for' loop (i.e. before "
      "any per-ticker work has started)",
      _first_for_line is None or _reset_line < _first_for_line)

# ======================================================================
# CHECK 8: USD/AUD/GBP/JPY branches of resolve_discount_rate_by_
# market_cap() are untouched - a source check.
# ======================================================================
_rdm_src = inspect.getsource(ce.resolve_discount_rate_by_market_cap)
check('AUD branch still calls get_au_risk_free_rate_live() directly (no per-run cache '
      "for AUD - CANADA ONLY)",
      "get_au_risk_free_rate_live()" in _rdm_src)
check('GBP branch still calls get_uk_risk_free_rate_live() directly (no per-run cache '
      "for GBP either)",
      "get_uk_risk_free_rate_live()" in _rdm_src)
check("CAD branch calls get_ca_risk_free_rate_for_run(), never get_ca_risk_free_rate_live() "
      "directly",
      "get_ca_risk_free_rate_for_run()" in _rdm_src
      and "rf, rf_src = get_ca_risk_free_rate_live()" not in _rdm_src)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
