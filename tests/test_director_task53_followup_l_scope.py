"""
Director's TASK 53 follow-up (13 Nov 2026, live case): narrow the
implausible-peak guard to .L only.

drop_chip_engine.compute_peak_and_drop()'s own "peak_implausible"
guard (TASK 53, PEAK_TO_CURRENT_IMPLAUSIBLE_RATIO = 5.0) originally
applied to EVERY ticker on EVERY market - but "no real operating
company's genuine weekly-close peak is 5x+ its own current price" is
false for a genuine large multi-year collapse (a ZM-style -90% or a
DOCU-style -80% both clear a 5x peak/current ratio easily), so the
blanket guard was hiding exactly the kind of real drop "Quality on
sale" exists to surface, on every market OTHER than the LSE. The
pence/pounds unit-mixing bug this guard exists for (TASK 53, RR.L
live case) is specific to .L listings.

Fix: compute_peak_and_drop() takes a new `ticker` parameter; the
guard now only ever fires when `ticker` ends in ".L". Every real
caller (support_levels_job.py's nightly computation, point_in_time_
replay.py's fear_from_peak_as_of()/quality_on_sale_as_of()) now
threads its own `ticker` through - none of them changed WHAT ticker
they already had in scope, only that it's now passed one level
deeper. `ticker` defaults to None (never .L) so an omitted ticker can
never accidentally suppress a real drop.

Covers:
  1. compute_peak_and_drop(): a .L ticker at 28x (RR.L-shaped) still
     suppressed; the SAME 28x ratio on a non-.L ticker is NOT
     suppressed - the task's own named test case.
  2. A US ticker at 10x showing a real -90% drop (the task's own
     named "ZM-style" example) - drop_pct/chip_shown both correctly
     reflect the real collapse, never hidden.
  3. ticker=None (the old call shape, before this fix's own threading)
     never flags - omission fails toward NOT hiding data.
  4. Case-insensitivity (".l" lower-case) and non-.L suffixes that
     merely contain "L" (".AX", "L.US"-shaped nonsense) are correctly
     NOT treated as LSE tickers - substring matching would be wrong,
     only a genuine endswith(".L") on the upper-cased ticker counts.
  5. Every real call site now threads its own already-in-scope
     ticker through, proven by source inspection (support_levels_
     job.py's nightly compute; point_in_time_replay.py's two replay
     functions and their own two call sites inside replay_at_date()/
     the backtest loop).
  6. support_levels_job.py's new "[peak_scope_audit]" report (the
     task's own "report how many non-.L tickers currently have
     peak_implausible = True" ask) - a non-.L ticker whose ratio
     exceeds the floor is logged with its real drop_pct, confirming
     it is NOT suppressed; a .L ticker over the floor is NOT
     reported here (it's still correctly suppressed, so it carries no
     "wrongly hidden" drop to report).

This sandbox has no outbound network access (confirmed repeatedly
throughout this project) - no real production drop_chip_store data
(the actual current count/list of non-.L tickers with peak_
implausible=True under the OLD, pre-fix code) can be read or reported
from here. The fix makes that question permanently moot going forward
(peak_implausible is now structurally False for any non-.L ticker),
and the new [peak_scope_audit] log line (CHECK 6) answers it directly,
every night, from Railway's own logs once this deploys.

Run: python3 tests/test_director_task53_followup_l_scope.py
"""
import datetime
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task53_followup_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import drop_chip_engine as dce
import point_in_time_replay as pitr
import support_levels_job as slj

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


def _d(i):
    return datetime.date(2020, 1, 6) + datetime.timedelta(weeks=i)


# ======================================================================
# CHECK 1: the task's own named test - ".L at 28x stays suppressed; a
# US ticker at 10x shows -90%" (the 10x/-90% case is CHECK 2 below;
# this check does the ".L at 28x" half, plus proves the SAME 28x ratio
# on a non-.L ticker is NOT suppressed).
# ======================================================================
_rrl_closes = [13.0, 13.2, 393.42, 13.4, 13.5, 13.6, 13.70]  # ratio ~28.7x
_rrl_dates = [_d(i) for i in range(len(_rrl_closes))]

_l_result = dce.compute_peak_and_drop(_rrl_dates, _rrl_closes, ticker="RR.L")
check("1a: RR.L (.L) at ~28.7x peak/current ratio: peak_implausible=True, "
      "drop_pct suppressed (unchanged from TASK 53's own fix)",
      _l_result["peak_implausible"] is True and _l_result["drop_pct"] is None)

_non_l_same_ratio = dce.compute_peak_and_drop(_rrl_dates, _rrl_closes, ticker="RRSHAPED")
check("1b: the EXACT SAME 28.7x-ratio series, on a non-.L ticker: "
      "peak_implausible=False - the guard never fires outside .L",
      _non_l_same_ratio["peak_implausible"] is False)
check("1b: ...and its real (large) drop_pct now shows, not suppressed",
      _non_l_same_ratio["drop_pct"] is not None
      and _non_l_same_ratio["drop_pct"] == _non_l_same_ratio["drop_pct_raw"]
      and _non_l_same_ratio["drop_pct"] < -96.0)


# ======================================================================
# CHECK 2: the task's own named "a US ticker at 10x shows -90%" case -
# a ZM-style collapse (peak 100, current 10 -> exactly 10x, drop -90%).
# ======================================================================
_zm_closes = [100.0, 95.0, 90.0, 50.0, 30.0, 15.0, 10.0]
_zm_dates = [_d(i) for i in range(len(_zm_closes))]
_zm_result = dce.compute_peak_and_drop(_zm_dates, _zm_closes, ticker="ZM")
check("2: a ZM-shaped US ticker, peak 100 -> current 10 (10x ratio, a REAL "
      "-90% collapse): peak_implausible=False - never treated as a data artifact",
      _zm_result["peak_implausible"] is False)
check("2: ...drop_pct is the real -90.0%, shown not hidden",
      _zm_result["drop_pct"] is not None and abs(_zm_result["drop_pct"] - (-90.0)) < 1e-9)
check("2: ...and the drop chip itself would show (chip_shown True, well past "
      "the -20% threshold)",
      _zm_result["chip_shown"] is True)

# Same shape on a .L ticker (DOCU-style -80%, 5x ratio - just past the
# floor) IS suppressed, since .L tickers genuinely can carry this bug -
# confirms the scoping cuts exactly one way, not both.
_docu_l_closes = [100.0, 95.0, 90.0, 50.0, 30.0, 20.0]  # ratio exactly 5.0x -> not flagged
_docu_l_dates = [_d(i) for i in range(len(_docu_l_closes))]
_docu_l_result = dce.compute_peak_and_drop(_docu_l_dates, _docu_l_closes, ticker="DOCU.L")
check("2b: sanity - a plausible 5.0x-or-under ratio on a .L ticker is still "
      "NOT flagged either (the ratio floor itself, not just the .L scoping, "
      "still has to be cleared)",
      _docu_l_result["peak_implausible"] is False)
_docu_l_closes_over = [100.0, 95.0, 90.0, 50.0, 30.0, 19.0]  # ratio ~5.26x -> flagged
_docu_l_result_over = dce.compute_peak_and_drop(_docu_l_dates, _docu_l_closes_over, ticker="DOCU.L")
check("2c: a .L ticker genuinely past the 5x floor (5.26x) IS still flagged - "
      "the fix narrows WHICH tickers the guard can apply to, not whether the "
      "ratio floor itself still works for .L",
      _docu_l_result_over["peak_implausible"] is True
      and _docu_l_result_over["drop_pct"] is None)


# ======================================================================
# CHECK 3: ticker=None (the old call shape) never flags - omission
# fails toward NOT hiding data, never toward silently suppressing it.
# ======================================================================
_none_ticker_result = dce.compute_peak_and_drop(_rrl_dates, _rrl_closes)
check("3: ticker=None (the default, the pre-fix call shape) on a 28.7x-ratio "
      "series: peak_implausible=False - an omitted ticker can never "
      "accidentally suppress a real drop",
      _none_ticker_result["peak_implausible"] is False
      and _none_ticker_result["drop_pct"] is not None)


# ======================================================================
# CHECK 4: exact suffix matching - case-insensitive ".L", but never a
# substring/near-miss match on something merely containing "L".
# ======================================================================
_lower_l_result = dce.compute_peak_and_drop(_rrl_dates, _rrl_closes, ticker="rr.l")
check("4a: lower-case 'rr.l' is still recognised as a .L ticker (case-"
      "insensitive) - the guard fires",
      _lower_l_result["peak_implausible"] is True)

_ax_result = dce.compute_peak_and_drop(_rrl_dates, _rrl_closes, ticker="BHP.AX")
check("4b: a '.AX' ticker (ends in a letter, but not the literal '.L' suffix) "
      "is NOT treated as .L - the guard never fires",
      _ax_result["peak_implausible"] is False)

_contains_l_result = dce.compute_peak_and_drop(_rrl_dates, _rrl_closes, ticker="GOOGL")
check("4c: a ticker that merely CONTAINS the letter L (GOOGL) but doesn't "
      "END in '.L' is NOT treated as .L - endswith, never a substring match",
      _contains_l_result["peak_implausible"] is False)


# ======================================================================
# CHECK 5: every real call site now threads its own already-in-scope
# ticker through - source inspection (full end-to-end AppTest-style
# driving of the nightly job / replay tool needs heavy mocking with no
# behavioural proof beyond "the parameter is threaded", which source
# inspection answers directly - same precedent used elsewhere in this
# project for a hard-to-isolate wiring proof).
# ======================================================================
_slj_src = inspect.getsource(slj)
check("5a: support_levels_job.py's own nightly compute_peak_and_drop() call "
      "now passes ticker=t (the SAME ticker variable used two lines above "
      "for support_levels_engine.compute_support_levels())",
      "drop_chip_engine.compute_peak_and_drop(dates, closes, ticker=t)" in _slj_src)

_fear_src = inspect.getsource(pitr.fear_from_peak_as_of)
check("5b: point_in_time_replay.fear_from_peak_as_of() now accepts ticker= "
      "and threads it into compute_peak_and_drop()",
      "ticker=None" in inspect.signature(pitr.fear_from_peak_as_of).__str__()
      and "ticker=ticker" in _fear_src)

_qos_src = inspect.getsource(pitr.quality_on_sale_as_of)
check("5c: point_in_time_replay.quality_on_sale_as_of() now accepts ticker= "
      "and threads it into compute_peak_and_drop()",
      "ticker=None" in inspect.signature(pitr.quality_on_sale_as_of).__str__()
      and "ticker=ticker" in _qos_src)

_replay_src = inspect.getsource(pitr.replay_at_date)
check("5d: replay_at_date() passes its own `ticker` (the ticker it's "
      "replaying) into BOTH fear_from_peak_as_of() and quality_on_sale_as_of()",
      "fear_from_peak_as_of(daily_closes_with_dates or [], date, ticker=ticker)" in _replay_src
      and "ticker=ticker," in _replay_src)

_backtest_src = inspect.getsource(pitr)
check("5e: the backtest loop's own quality_on_sale_as_of() call (the OTHER "
      "call site, inside the Friday-by-Friday loop) also passes ticker=ticker",
      "dcf_unreliable=None, quality_exception_thresholds_met=None, ticker=ticker," in _backtest_src)


# ======================================================================
# CHECK 6: support_levels_job.py's new "[peak_scope_audit]" report -
# the task's own "report how many non-.L tickers currently have
# peak_implausible = True" ask, answered live every night from
# whatever tickers support_levels_job actually processes.
# ======================================================================
check("6a: support_levels_job.py computes a non-.L ticker list "
      "(_non_l_tickers_tonight) from the SAME `tickers` the nightly run "
      "already processes - not a second/separate universe",
      "_non_l_tickers_tonight = [t for t in tickers if isinstance(t, str) "
      "and not t.upper().endswith(\".L\")]" in _slj_src)
check("6b: it logs a '[peak_scope_audit]' line naming each flagged non-.L "
      "ticker's own peak/current/ratio/drop_pct/peak_implausible, so a "
      "reviewer can directly confirm none of them are ever suppressed",
      "[peak_scope_audit]" in _slj_src
      and "peak_implausible={_ne.get('peak_implausible')}" in _slj_src)
check("6c: the summary line names the SAME PEAK_TO_CURRENT_IMPLAUSIBLE_RATIO "
      "constant this whole task reused, never a second/drifted threshold",
      _slj_src.count("drop_chip_engine.PEAK_TO_CURRENT_IMPLAUSIBLE_RATIO") >= 3)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
