"""
Admin "Recompute this ticker now" (owner-directed, 3 Oct 2026) -
app.py's _render_recompute_ticker_panel(), wired into page_admin_
dashboard() after that function's own ai_gate.is_owner() check.

Invalidates and rebuilds ONE ticker's fundamentals_data bundle cache
via fundamentals_data.get_bundle(ticker, force_refresh=True) - the
same "admin rebuild action, mirroring compounder_data.json's own
rebuild pattern" that function's own docstring already documents.
Scoped to one ticker, one call set - never a universe-wide refresh.

Covers:
  - the cache file for a ticker is actually overwritten (the old
    cached value is gone, a fresh one takes its place) when force_
    refresh=True is used, with no live network call (yfinance.Ticker
    mocked)
  - the admin gate: same static-source-check pattern test_valuation_
    change_audit.py/test_imported_screen_admin_relocation.py already
    use for exactly this kind of admin-only section - a non-owner
    returns from page_admin_dashboard() before ever reaching the
    panel, so fully rendering the page for a non-admin needs no extra
    confidence over reading the real call graph.

Run: python3 tests/test_admin_recompute_ticker.py
"""
import json
import os
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="admin_recompute_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import fundamentals_data as fd

# ======================================================================
# CHECK 1: the cache key is actually removed and rebuilt - a stale
# cached bundle (old marketCap, old BUNDLE_VERSION-matching fetched_at)
# is completely replaced by a fresh one when force_refresh=True is
# used, with no live network call.
# ======================================================================
_TICKER = "KNSL"
_cache_path = fd._cache_path(_TICKER)
os.makedirs(os.path.dirname(_cache_path), exist_ok=True)

_stale = fd._bundle_to_cache({
    "info": {"marketCap": 1.0, "currentPrice": 1.0}, "income": None, "balance": None,
    "cashflow": None, "income_q": None, "prices_10y": {"dates": [], "prices": []},
    "dividends": {"dates": [], "amounts": []}, "spx_prices_10y": {"dates": [], "prices": []},
    "meta": {"fetched_at": "2020-01-01T00:00:00+00:00", "bundle_version": fd.BUNDLE_VERSION},
})
with open(_cache_path, "w") as f:
    json.dump(_stale, f)
assert fd.peek_cached_market_cap(_TICKER) == 1.0, "stale fixture didn't write correctly"


class _FakeTicker:
    def __init__(self, *_a, **_k):
        self.info = {"marketCap": 999.0, "currentPrice": 42.0, "sharesOutstanding": 1_000_000}
        self.dividends = mock.Mock(empty=True)

    @property
    def fast_info(self):
        raise AttributeError  # force the .info fallback path, no real network either way


with mock.patch.object(fd, "yf") as _mock_yf, \
     mock.patch.object(fd, "_fetch_yfinance_statements", return_value=(fd.pd.DataFrame(), fd.pd.DataFrame(), fd.pd.DataFrame())), \
     mock.patch.object(fd, "_fetch_yfinance_quarterly_income", return_value=fd.pd.DataFrame()), \
     mock.patch.object(fd, "_overlay_fresh_price", side_effect=lambda info, *a, **k: info):
    _mock_yf.Ticker.side_effect = _FakeTicker
    _fresh_bundle = fd.get_bundle(_TICKER, force_refresh=True)

assert _fresh_bundle is not None
assert _fresh_bundle["info"]["marketCap"] == 999.0, _fresh_bundle["info"]
# The cache file on disk now reflects the fresh fetch, not the stale
# one this check started with - "removed and rebuilt", not "appended
# to" or "left untouched alongside a new in-memory value".
assert fd.peek_cached_market_cap(_TICKER) == 999.0, (
    "the on-disk cache must be overwritten by force_refresh=True, not just the in-memory return value"
)
print("[cache_invalidated_and_rebuilt] a stale cached bundle (marketCap=1.0) is fully replaced "
      "by a fresh one (marketCap=999.0) via get_bundle(ticker, force_refresh=True) - no live "
      "network call needed (yfinance mocked) OK")

# ======================================================================
# CHECK 2: admin gate - _render_recompute_ticker_panel() is defined
# exactly once and called exactly once, from inside page_admin_
# dashboard() AFTER that function's own ai_gate.is_owner() early-return
# check - a non-owner returns before ever reaching it, so "non-admin
# sees nothing" needs no separate UI-click simulation to confirm.
# ======================================================================
_APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
with open(_APP_PATH, encoding="utf-8") as f:
    _APP_SRC = f.read()


def _function_span(src, name):
    start = re.search(rf"(?m)^def {re.escape(name)}\(", src)
    assert start, f"def {name}( not found in app.py"
    nxt = re.search(r"(?m)^(def |class )", src[start.end():])
    end = start.end() + nxt.start() if nxt else len(src)
    return src[start.start():end]


assert _APP_SRC.count("def _render_recompute_ticker_panel(") == 1
_admin_body = _function_span(_APP_SRC, "page_admin_dashboard")
assert "_render_recompute_ticker_panel()" in _admin_body
assert "ai_gate.is_owner(" in _admin_body
_owner_check_pos = _admin_body.index("ai_gate.is_owner(")
_panel_call_pos = _admin_body.index("_render_recompute_ticker_panel()")
assert _panel_call_pos > _owner_check_pos, (
    "the panel call must come AFTER the owner check (and its early-return) in "
    "page_admin_dashboard()'s body, so a non-owner never reaches it")
_calls = [ln for ln in _APP_SRC.splitlines() if ln.strip() == "_render_recompute_ticker_panel()"]
assert len(_calls) == 1, _calls
print("[admin_gate] _render_recompute_ticker_panel() is defined exactly once, called exactly "
      "once, from inside page_admin_dashboard() AFTER that function's own ai_gate.is_owner() "
      "check - a non-owner returns before ever reaching it (non-admin sees nothing) OK")

# ======================================================================
# CHECK 3: the panel's own backend call logs "[admin] recompute "
# <ticker>" and uses fundamentals_data.get_bundle with force_refresh=
# True - confirmed directly from the panel's source (not a second,
# drifting copy of the logic).
# ======================================================================
_panel_src_match = re.search(
    r"(?m)^def _render_recompute_ticker_panel\(.*?(?=^def |\Z)", _APP_SRC, re.S)
assert _panel_src_match
_panel_src = _panel_src_match.group(0)
assert 'print(f"[admin] recompute {_ticker}")' in _panel_src, _panel_src
assert "fundamentals_data.get_bundle(_ticker, force_refresh=True)" in _panel_src, _panel_src
print("[panel_logs_and_calls_force_refresh] _render_recompute_ticker_panel() logs "
      "'[admin] recompute <ticker>' and calls fundamentals_data.get_bundle(ticker, "
      "force_refresh=True) - one ticker, one call set, no universe-wide refresh OK")

print("\nALL ADMIN RECOMPUTE TICKER FIXTURES PASSED")
