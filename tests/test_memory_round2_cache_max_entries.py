"""Memory addendum, round 2 (9 Oct 2026, Director-directed, build go),
item (b): max_entries=2000 on app.get_ticker_info and
app.get_price_history ONLY - option A from the Director's review of
MEMORY_COST_ADDENDUM_REPORT.md. No other cache changes THAT round
(options B/C/D/E - a blanket sweep, process restart, subprocess split -
explicitly NOT built THEN, per the Director's own instruction at the
time).

SUPERSEDED (TASK 21, Director-directed, 25 Oct 2026, memory): the
Director's own later task explicitly ordered the blanket sweep round 2
deliberately deferred - "add max_entries to every uncapped @st.cache_
data" app-wide, sized to each function's own key shape - so this file's
own two checks below were updated to assert THAT new state (every
cache_data decorator, in every .py file, now carries max_entries) rather
than continuing to assert the old "these two only" restriction, which
this later task intentionally lifted. The behavioural check (max_
entries actually evicts) is untouched - that mechanism never changed.

Two checks:
  1. Source-level: the @st.cache_data decorator immediately above
     get_ticker_info()/get_price_history() still carries max_entries=
     2000 specifically (unchanged by TASK 21), and - per TASK 21 - every
     OTHER @st.cache_data call site in app.py (and every other .py file
     in the repo) now ALSO carries its own max_entries (see tests/
     test_director_task21_memory_fixes.py for the per-function values
     and reasoning).
  2. Behavioural: st.cache_data's own max_entries mechanism actually
     evicts past the cap (proven against a throwaway function using
     the identical decorator arguments - calling the real get_ticker_
     info/get_price_history 2001 times would mean live network calls,
     which this sandbox can't make).

Updated for the incident fix (10 Oct 2026, Andrew-reported - Yahoo
quoteSummary 401s 12:38-12:42 UTC): the @st.cache_data decorator that
used to sit directly on get_ticker_info()/get_price_history() now sits
on new, private _get_ticker_info_cached()/_get_price_history_cached()
inner functions instead - get_ticker_info()/get_price_history() are
now thin, UNCACHED outer wrappers (same public names, same callers,
same return contract) that catch the exception the cached inner
function raises on a persistent fetch failure, so a failure is never
cached for its full 30-minute ttl - see app.py's own _fetch_with_retry
docstring (raise_on_failure) for the mechanism. max_entries=2000 moved
with the decorator onto the new inner names; nothing else changed.

Run: python3 tests/test_memory_round2_cache_max_entries.py
"""
import os
import re
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


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO_ROOT, "app.py"), encoding="utf-8") as f:
    _app_src = f.read()

_decorator_re = re.compile(r"(@st\.cache_data\([^)]*\))\s*\ndef (\w+)\(")
_all_cache_data_defs = _decorator_re.findall(_app_src)

check("app.py has at least the two targeted cache_data functions (now the private "
      "cached inner functions behind get_ticker_info()/get_price_history(), after "
      "the 10 Oct 2026 incident fix split each into a cached inner + uncached outer)",
      any(name == "_get_ticker_info_cached" for _, name in _all_cache_data_defs)
      and any(name == "_get_price_history_cached" for _, name in _all_cache_data_defs))

for _decorator, _name in _all_cache_data_defs:
    if _name in ("_get_ticker_info_cached", "_get_price_history_cached"):
        check(f"{_name}()'s own @st.cache_data decorator carries max_entries=2000",
              "max_entries=2000" in _decorator)
    else:
        # TASK 21 (25 Oct 2026): every other cache_data decorator in
        # app.py now carries ITS OWN max_entries too - the blanket sweep
        # this file's docstring explains was deferred, then later
        # ordered, by the Director.
        check(f"{_name}()'s own @st.cache_data decorator carries its own max_entries "
              "(TASK 21's later blanket sweep, superseding this round's 'option A only')",
              "max_entries" in _decorator)

# TASK 21 (25 Oct 2026): the INVERSE of this file's original check -
# every .py file in the repo with an @st.cache_data(...) decorator now
# has max_entries on EVERY one of them, not just app.py's original two.
# @st.cache_resource is deliberately OUT OF SCOPE here (and was never
# in scope for TASK 21's own instruction, which names "@st.cache_data"
# specifically) - every real cache_resource call site in this repo
# (paywall_engine.py x2, app.py x2) caches a process-wide SINGLETON
# resource (a DB handle, the background scheduler thread), not a
# growing per-key cache, so there is nothing for max_entries to bound.
# A plain prose mention (e.g. scheduler_engine._malloc_trim_and_log()'s
# own docstring, which references get_ticker_info's cap for context) is
# not a decorator and must not be flagged either way - the regex below
# only matches an actual decorator line, same as the app.py check above.
_bare_decorator_re = re.compile(r"^\s*@st\.cache_data\(([^)]*)\)\s*$", re.MULTILINE)
_bare_no_args_re = re.compile(r"^\s*@st\.cache_data\s*$", re.MULTILINE)
_missing_max_entries = []
for _root, _dirs, _files in os.walk(REPO_ROOT):
    if "/.git" in _root or "/tests" in _root:
        continue
    for _fn in _files:
        if not _fn.endswith(".py"):
            continue
        _path = os.path.join(_root, _fn)
        with open(_path, encoding="utf-8", errors="ignore") as _fh:
            _src = _fh.read()
        for _args in _bare_decorator_re.findall(_src):
            if "max_entries" not in _args:
                _missing_max_entries.append((_path, _args))
        if _bare_no_args_re.search(_src):
            _missing_max_entries.append((_path, "<bare @st.cache_data, no args at all>"))
check("TASK 21: every @st.cache_data decorator in every .py file in the repo now "
      "carries its own max_entries - no blanket-sweep stragglers left "
      "(@st.cache_resource is out of scope - see comment above)",
      _missing_max_entries == [])

# ---------------------------------------------------------------------
# Behavioural proof that max_entries actually evicts, using the exact
# same decorator shape (ttl + max_entries) on a throwaway function -
# proves the MECHANISM works; the source check above proves it's wired
# to the right two real functions.
# ---------------------------------------------------------------------
import streamlit as st

_call_count = {"n": 0}


@st.cache_data(ttl=1800, show_spinner=False, max_entries=3)
def _throwaway_capped(ticker):
    _call_count["n"] += 1
    return _call_count["n"]

for _t in ("A", "B", "C"):
    _throwaway_capped(_t)
check("3 distinct entries, cap=3: all 3 are cache hits on a second pass (no eviction yet)",
      _call_count["n"] == 3)
for _t in ("A", "B", "C"):
    _throwaway_capped(_t)
check("...confirmed: re-calling the same 3 tickers triggers no new underlying calls",
      _call_count["n"] == 3)

# A 4th distinct entry pushes past the cap - the least-recently-used
# entry ("A") must be evicted, so calling it again re-executes.
_throwaway_capped("D")
check("a 4th distinct entry is accepted (call count went up)", _call_count["n"] == 4)
_throwaway_capped("A")
check("max_entries=3 actually evicts: re-calling the LRU entry ('A') after a 4th distinct "
      "ticker re-executes the function body (cache miss) rather than serving a stale hit",
      _call_count["n"] == 5)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
