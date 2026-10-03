"""
Director addendum 2, Part 1 of 2, item A (3 Oct 2026) - Japan lists,
findings from the live scan (12:01-12:31 UTC, 3 Oct): Nikkei 225 and
TOPIX 500 did NOT scan. Same rules as the Lists & display instruction.
HOLD ALL PUSHES still applies.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Covers: fetch_nikkei225()/fetch_topix500() log a full diagnostic
(url/status/content_type/tables_found/rows_parsed/first200) on
failure, via full browser headers - see this file's own report
comment at the bottom for why neither fetcher's PARSING logic was
changed, and no second Nikkei source or new TOPIX URL was added
(sandbox has no outbound network - confirmed directly).

Run: python3 tests/test_director_addendum2_part1a_japan_lists.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import scanner_engine as se


class _FakeResp:
    def __init__(self, status_code, text, content_type="text/html; charset=UTF-8"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(f"{self.status_code} error")


# ======================================================================
# A1: fetch_nikkei225() logs the full diagnostic on an HTTP failure -
# url, status, content_type, tables found, rows parsed, first 200 chars.
# ======================================================================
se.fetch_nikkei225.clear()
_lines = []
with mock.patch.object(se._log, "warning", side_effect=lambda msg: _lines.append(msg)), \
     mock.patch.object(se.requests, "get",
                        return_value=_FakeResp(404, "<html>Not Found - some body text here</html>")):
    result = se.fetch_nikkei225()
assert result is None, result
assert len(_lines) == 1, _lines
_line = _lines[0]
assert "Nikkei 225" in _line and se.NIKKEI225_WIKI_URL in _line, _line
assert "status=404" in _line, _line
assert "content_type=text/html" in _line, _line
assert "tables_found=" in _line and "rows_parsed=" in _line, _line
assert "Not Found - some body" in _line, _line
print(f"[A1_nikkei_diagnostic_on_404] {_line!r} carries url/status/content_type/"
      f"tables_found/rows_parsed/first200 OK")


# ======================================================================
# A2: fetch_nikkei225() sends full browser headers, not a bot UA.
# ======================================================================
_captured_headers = {}


def _capture_and_fail(url, headers=None, timeout=None):
    _captured_headers.update(headers or {})
    raise requests.ConnectionError("simulated: no network in this sandbox")


se.fetch_nikkei225.clear()
with mock.patch.object(se._log, "warning"), \
     mock.patch.object(se.requests, "get", side_effect=_capture_and_fail):
    se.fetch_nikkei225()
assert "Chrome" in _captured_headers.get("User-Agent", ""), _captured_headers
assert "Accept" in _captured_headers and "Accept-Language" in _captured_headers, _captured_headers
print(f"[A2_nikkei_browser_headers] fetch_nikkei225() sends a real browser UA + "
      f"Accept/Accept-Language, not the bot UA OK")


# ======================================================================
# A3: fetch_topix500() logs the same full diagnostic on an HTTP
# failure, in addition to its own pre-existing required
# "[universe] TOPIX 500: constituent list unavailable (...) -
# not scanning" line (unchanged format, still present).
# ======================================================================
se.fetch_topix500.clear()
_warn_lines = []
with mock.patch.object(se._log, "warning", side_effect=lambda msg: _warn_lines.append(msg)), \
     mock.patch.object(se.requests, "get",
                        return_value=_FakeResp(404, "<html>404 Not Found page body</html>")):
    result = se.fetch_topix500()
assert result is None, result
assert len(_warn_lines) == 2, _warn_lines
_diag_line, _required_line = _warn_lines
assert "TOPIX 500" in _diag_line and se.TOPIX_CONSTITUENTS_URL in _diag_line, _diag_line
assert "status=404" in _diag_line, _diag_line
assert "404 Not Found page body" in _diag_line, _diag_line
assert _required_line == "[universe] TOPIX 500: constituent list unavailable (HTTP 404) - not scanning", _required_line
print(f"[A3_topix_diagnostic_plus_required_line] diagnostic={_diag_line!r}\n"
      f"    required(unchanged)={_required_line!r} OK")


print("\nALL Director addendum 2, Part 1, item A (Japan lists) CHECKS PASSED")
print(
    "\nNOTE for the Director, per the instruction's own 'if it cannot [fetch], "
    "do not guess' rule: this sandbox has NO outbound network access - "
    "confirmed directly, both https://en.wikipedia.org/wiki/Nikkei_225 and "
    "https://www.jpx.co.jp/english/ return a 403 Forbidden from this sandbox's "
    "own proxy (not from the real site - no real HTTP exchange with either site "
    "ever happened). Per that rule: neither fetcher's PARSING logic was changed "
    "against a guessed page shape, no new/second live source was added for "
    "Nikkei 225, and no new TOPIX 500 URL was substituted - all three would "
    "require verifying a real response this sandbox cannot produce. What WAS "
    "done without needing live access: full diagnostic logging (url/status/"
    "content_type/tables_found/rows_parsed/first200) on failure for both "
    "fetchers, full-browser headers (not the bot UA) sent for both, and "
    "confirmation that both already have last-good-list persistence (added in "
    "Lists & display Commit 1) - see scanner_engine.py's get_universe_pool() "
    "Nikkei 225/TOPIX 500 branches. A second Nikkei 225 source and/or the "
    "correct current TOPIX 500 URL need either sandbox network access restored "
    "or the Director supplying the exact address(es) to use, same as how the "
    "FTSE/TSX iShares ETF URLs were supplied (and flagged unverified) for "
    "Commit 1."
)
