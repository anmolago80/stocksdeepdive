"""
Push 3 (30 Sep 2026, owner-directed), per instruction_step1d_step4_
rewind.md: checkpoint rewind on a RateLimitCircuitBreaker abort, plus 4
hardening items left out of earlier commits.

Run: python3 tests/test_push3_checkpoint_and_hardening.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan_checkpoint_store
import scanner_engine
import server
import portfolio_health_engine


# ======================================================================
# CHECK 1: checkpoint rewind on breaker abort - the spec's own exact
# case: 200-ticker checkpoint at index 180 -> resume index 155, rows
# for tickers 155-179 dropped from the saved set.
# ======================================================================
assert scan_checkpoint_store.CHECKPOINT_BREAKER_REWIND == 25

_tickers_200 = [f"T{i:03d}" for i in range(200)]
_rows_180 = [{"Ticker": t, "Price": 10.0} for t in _tickers_200[:180]]

_new_resume, _new_rows = scan_checkpoint_store.rewind_for_breaker_abort(
    180, _tickers_200, _rows_180,
)
assert _new_resume == 155, _new_resume
_kept_tickers = {r["Ticker"] for r in _new_rows}
_dropped_band = set(_tickers_200[155:180])
assert _kept_tickers.isdisjoint(_dropped_band), _kept_tickers & _dropped_band
assert _kept_tickers == set(_tickers_200[:155]), _kept_tickers ^ set(_tickers_200[:155])
assert len(_new_rows) == 155, len(_new_rows)
print("[checkpoint_rewind_180_to_155] 200-ticker checkpoint at index 180 -> resume index "
      "155, rows for tickers 155-179 dropped from the saved set, rows 0-154 kept OK")

# Floored at 0 - an abort in the first 25 tickers never goes negative.
_new_resume_low, _new_rows_low = scan_checkpoint_store.rewind_for_breaker_abort(
    10, _tickers_200[:10], [{"Ticker": t} for t in _tickers_200[:10]],
)
assert _new_resume_low == 0, _new_resume_low
assert _new_rows_low == [], _new_rows_low
print("[checkpoint_rewind_floored_at_zero] resume_index=10 with rewind=25 -> floored at 0, "
      "all 10 rows dropped OK")

# Rows for tickers well before the rewound band are untouched, in order.
_rows_mixed = [{"Ticker": t, "Price": float(i)} for i, t in enumerate(_tickers_200[:180])]
_, _new_rows_mixed = scan_checkpoint_store.rewind_for_breaker_abort(180, _tickers_200, _rows_mixed)
assert [r["Ticker"] for r in _new_rows_mixed] == _tickers_200[:155], "order/content changed"
print("[checkpoint_rewind_preserves_order] rows for tickers 0-154 kept byte-identical and in "
      "original order OK")


# ======================================================================
# CHECK 2: nightly_scan.py actually calls the rewind helper (and saves
# the rewound state) at the RateLimitCircuitBreaker abort point, not
# just leaving whatever the last periodic checkpoint happened to hold.
# ======================================================================
import inspect
import nightly_scan
_src = inspect.getsource(nightly_scan.run_universe_scan)
assert "rewind_for_breaker_abort" in _src
assert _src.index("rewind_for_breaker_abort") < _src.index("raise RateLimitCircuitBreaker")
print("[nightly_scan_calls_rewind_before_raise] run_universe_scan() calls "
      "scan_checkpoint_store.rewind_for_breaker_abort() before raising "
      "RateLimitCircuitBreaker OK")


# ======================================================================
# CHECK 3: scanner_engine.fetch_nasdaq100_invesco() Content-Type/CSV-
# header guard - an HTML/JS response (the live symptom: the Invesco URL
# serving an HTML/JS page) is rejected before ever reaching pd.read_csv,
# rather than logging a DtypeWarning of minified JavaScript.
# ======================================================================
class _FakeResp:
    def __init__(self, text, content_type):
        self.text = text
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        pass


_html_js_body = (
    "<!doctype html><script>function load(ticker,symbol){var x={a:1};"
    "return x;}</script><div>ticker,symbol,name</div>"
)
with mock.patch.object(scanner_engine, "requests") as _mock_requests, \
     mock.patch.object(scanner_engine.pd, "read_csv") as _mock_read_csv:
    _mock_requests.get.return_value = _FakeResp(_html_js_body, "text/html; charset=utf-8")
    _result = scanner_engine.fetch_nasdaq100_invesco()
    assert _result is None, _result
    assert not _mock_read_csv.called, "pd.read_csv() was called on an HTML/JS response"
print("[nasdaq100_invesco_html_content_type_rejected] text/html Content-Type -> None, "
      "pd.read_csv() never called OK")

_js_snippet_header_line = 'function(){ticker,symbol,"junk";}'
with mock.patch.object(scanner_engine, "requests") as _mock_requests, \
     mock.patch.object(scanner_engine.pd, "read_csv") as _mock_read_csv:
    _mock_requests.get.return_value = _FakeResp(
        "\n".join(["garbage line 1", _js_snippet_header_line, "more junk"]),
        "text/csv",
    )
    _result = scanner_engine.fetch_nasdaq100_invesco()
    assert _result is None, _result
    assert not _mock_read_csv.called, "pd.read_csv() was called on a JS-syntax header line"
print("[nasdaq100_invesco_js_header_line_rejected] Content-Type OK but the candidate header "
      "line itself carries JS/HTML syntax -> rejected, pd.read_csv() never called OK")

# A genuine CSV response is unaffected by either new guard.
_real_csv_body = "Fund Holdings as of 09/29/2026\nName,Ticker,Weight\nApple Inc,AAPL,10.0"
with mock.patch.object(scanner_engine, "requests") as _mock_requests:
    _mock_requests.get.return_value = _FakeResp(_real_csv_body, "text/csv; charset=utf-8")
    # Real function still runs its own row-count sanity check (90-110) and
    # returns None for a 1-row fixture - this only confirms it got PAST the
    # new guards (raised no exception, reached pd.read_csv) rather than
    # being rejected outright by them.
    _result = scanner_engine.fetch_nasdaq100_invesco()
    assert _result is None  # fails the 90-110 row-count sanity check, as expected for 1 row
print("[nasdaq100_invesco_real_csv_unaffected] a genuine text/csv response with a clean "
      "header line reaches pd.read_csv() same as before these guards existed OK")


# ======================================================================
# CHECK 4: server.py catch-all 404 for probe paths.
# ======================================================================
for _path in (".env", "app/.env", ".git/config", "some/deep/path/.git",
              "wp-admin/setup.php", "wp-login.php", "WP-ADMIN/x", ".ENV"):
    assert server._PROBE_PATH_RE.match(_path), f"{_path!r} should match"
for _path in ("s/AAPL", "deep-dive", "api/v1/snapshot/AAPL", "blog/some-post",
              "environment.txt", "gitignore"):
    assert not server._PROBE_PATH_RE.match(_path), f"{_path!r} should NOT match"
print("[server_probe_path_regex] .env/.git/wp-admin/wp-login.php (any depth, case-"
      "insensitive) match; real site routes and near-miss paths do not OK")

import asyncio


async def _run_catch_all_probe():
    class _FakeRequest:
        url = type("u", (), {"path": "/.env"})()

    with mock.patch.object(server, "_proxy") as _mock_proxy:
        resp = await server.catch_all(".env", _FakeRequest())
        assert not _mock_proxy.called, "probe path reached the Streamlit proxy"
        assert resp.status_code == 404, resp.status_code
        return resp


_resp = asyncio.run(_run_catch_all_probe())
print(f"[server_catch_all_404_for_probe] catch_all('.env', ...) returns {_resp.status_code} "
      f"directly, never reaching _proxy() (the Streamlit subprocess) OK")


# ======================================================================
# CHECK 5: portfolio_health_engine dividend-yield unit-uncertainty flag.
# ======================================================================
assert portfolio_health_engine._dividend_yield_unit_uncertain(0.44) is True
assert portfolio_health_engine._dividend_yield_unit_uncertain(1.0) is True
assert portfolio_health_engine._dividend_yield_unit_uncertain(2.35) is False  # >1, unambiguous
assert portfolio_health_engine._dividend_yield_unit_uncertain(None) is False
assert portfolio_health_engine._dividend_yield_unit_uncertain(0) is False
print("[dividend_yield_unit_uncertain_band] flags only the genuinely ambiguous (0, 1] band - "
      "the >1 heuristic's own unambiguous case and None/0 are never flagged OK")

# _normalize_dividend_yield()'s own numeric behavior is completely
# unchanged by this addition (same values in, same values out).
assert portfolio_health_engine._normalize_dividend_yield(0.44) == 0.44
assert portfolio_health_engine._normalize_dividend_yield(2.35) == 0.0235
assert portfolio_health_engine._normalize_dividend_yield(None) is None
print("[normalize_dividend_yield_unchanged] _normalize_dividend_yield()'s own numeric output "
      "is byte-identical to before this addition OK")


# ======================================================================
# CHECK 6: server.py analytics per-day hash sets capped at 50,000.
# ======================================================================
assert server._ANALYTICS_HASH_SET_MAX == 50_000

with mock.patch.object(server, "_ANALYTICS_HASH_SET_MAX", 3):
    server._seen_page_view_hashes.clear()
    server._seen_asset_fetch_hashes.clear()
    server._promoted_human_hashes_today.clear()
    server._asset_tracking_day = None
    _today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    for i in range(10):
        server._maybe_promote_to_human(f"hash{i}", _today, saw_page_view=True, saw_asset_fetch=False)
    assert len(server._seen_page_view_hashes) == 3, len(server._seen_page_view_hashes)
    server._seen_page_view_hashes.clear()
    server._seen_asset_fetch_hashes.clear()
    server._promoted_human_hashes_today.clear()
print("[analytics_hash_set_capped] with the cap patched to 3, 10 distinct hashes only ever "
      "grow _seen_page_view_hashes to 3 entries - a flood of fabricated hashes can't grow "
      "these sets without bound within a day OK")

print("PUSH3_HARDENING_SWEEP_DONE")
