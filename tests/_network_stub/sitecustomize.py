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
