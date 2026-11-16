"""
tests/_network_stub/sitecustomize.py

Director's overnight queue TASK 6 (14 Oct 2026, "stop live-network
attempts"): an earlier audit (Q3) found roughly 49 test files make an
incidental live network attempt - yfinance's own cookie/crumb warm-up,
inside yfinance.data.YfData._get_cookie_and_crumb() - that fails
gracefully in this sandbox (no outbound network: ConnectionError,
"Cookie/crumb fetch failed... continuing without crumb") but still
pays a real network-timeout round-trip on every single yfinance
request a test makes, before falling back to exactly the same
no-crumb path this stub jumps straight to.

TESTS ONLY, never production code: this file is auto-imported by
Python's own `site` module at interpreter startup, but ONLY when its
own directory is on PYTHONPATH - and the ONLY thing that ever sets
that is tests/run_sweep.sh (the committed sweep entry point this same
task adds). Running the app normally (`streamlit run app.py`,
`uvicorn server:app`, `python3 server.py`, or any test run directly
with a plain `python3 tests/test_X.py` and no PYTHONPATH override)
never imports this file at all, so production's own real yfinance
calls - and any direct, ad hoc test run - are completely untouched.

Stub: YfData._get_cookie_and_crumb() returns (None, 'basic')
immediately, skipping Yahoo's own cookie/consent/crumb network round-
trip entirely. This is the EXACT SAME (crumb=None, degraded-but-
working) outcome yfinance's own code already falls back to when the
real network attempt fails in this sandbox - this stub only skips the
wait to get there. No other yfinance request path is touched; a test
that mocks/patches yfinance itself (the overwhelming majority of this
suite's own fixture-based tests) is completely unaffected either way.
"""
try:
    from yfinance.data import YfData

    def _no_network_cookie_and_crumb(self, timeout=30):
        return None, self._cookie_strategy

    YfData._get_cookie_and_crumb = _no_network_cookie_and_crumb
except Exception:
    # yfinance isn't installed, or its internals changed shape since
    # this stub was written - never fail test collection over this,
    # the sweep just runs at its old (slower, real-network-attempting)
    # speed instead.
    pass

# TASK 66 (16 Nov 2026): top100_engine.submit_nightly_batch()/run_
# single_test_call() now call portfolio_news_engine.analyze_holding_
# news() directly (the "Recent news" headwind block) for every entrant
# that hasn't been news-fetched in the last REFETCH_STALE_HOURS - a
# brand-new synthetic ticker (as every pre-existing fixture pool uses,
# e.g. test_audit_c1_no_resubmit_cap.py's own 80-ticker big_pool) has
# never been fetched, so _claim_fetch() returns True and the real
# _fetch_all_feeds() runs: yfinance news, ASX announcements, GDELT,
# Google News and NewsAPI, each failing gracefully but paying its own
# real network round-trip - multiplied across every entrant in every
# affected test's own pool, this is the SAME class of incidental,
# gracefully-failing network cost the yfinance cookie/crumb stub above
# already exists to cut, just one level up the call stack. Stubbed at
# _fetch_all_feeds() specifically (not analyze_holding_news() itself)
# so a test that mocks _claim_fetch()/seeds _load_events() directly
# (test_part_b_step1_news_v2.py and friends, which exercise the real
# classification logic against pre-seeded fixture events) is completely
# unaffected - those never reach _fetch_all_feeds() at all, mocked
# _claim_fetch()=False or not, same as in production.
try:
    import os
    import sys

    # Unlike yfinance (a site-packages dependency, always importable),
    # portfolio_news_engine.py lives at the REPO ROOT - at this point in
    # interpreter startup (site processing, before any test script's own
    # `sys.path.insert(0, repo_root)` line has run) the repo root is NOT
    # yet on sys.path, so a bare `import portfolio_news_engine` here
    # raises ModuleNotFoundError. Add it explicitly (this file's own
    # grandparent directory) - the exact same path every test script
    # adds a moment later anyway, just done a little earlier here.
    _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    import portfolio_news_engine

    def _no_network_fetch_all_feeds(ticker, name, start_dt, end_dt=None):
        return []

    portfolio_news_engine._fetch_all_feeds = _no_network_fetch_all_feeds
except Exception:
    # Same "never fail test collection over this" rule as the yfinance
    # stub above - the sweep just runs at its old, slower speed.
    pass
