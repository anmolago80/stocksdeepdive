"""
Director addendum 2, Part 1 of 2, item A (3 Oct 2026) - Japan lists,
findings from the live scan (12:01-12:31 UTC, 3 Oct): Nikkei 225 and
TOPIX 500 did NOT scan. Same rules as the Lists & display instruction.
HOLD ALL PUSHES still applies.

Director reply (3 Oct 2026, later the same day): facts verified
directly from the live pages -
  - Nikkei 225 (en.wikipedia.org/wiki/Nikkei_225): constituents are
    BULLETED LIST ITEMS grouped by sector, not a table - each entry
    reads like "Honda Motor Co., Ltd. (TYO: 7267)". Parse for the
    "TYO: <code>" pattern; require >= 200 codes or treat as failure.
  - TOPIX 500: JPX publishes "topixweight_j.csv" linked from
    https://www.jpx.co.jp/markets/indices/topix/ - columns コード,
    銘柄名, ニューインデックス区分 (values "TOPIX Core30"/"TOPIX
    Large70"/"TOPIX Mid400"/"TOPIX Small 1"/"TOPIX Small 2"). Never
    hard-code the file's folder path - fetch the index page and follow
    whichever link ends in topixweight_j.csv. Shift-JIS/cp932 encoded.
    Keep Core30/Large70/Mid400 rows; require >= 400.
Both fetchers rewritten against these facts (scanner_engine.py's own
module comment just above NIKKEI225_WIKI_URL/TOPIX_INDEX_PAGE_URL has
the full detail). Failure diagnostics (url/status/content_type/rows
parsed/first200) kept from this same item's original commit.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC ("say they are fixtures", per
the Director's own instruction) - this sandbox still has no outbound
network access to verify either real page/file directly; the real
proof is the first live scan.

Run: python3 tests/test_director_addendum2_part1a_japan_lists.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import scanner_engine as se


class _FakeResp:
    """Text/HTML response fake - .text/.content/.headers/.status_code,
    same shape fetch_nikkei225()/the TOPIX index-page fetch both read."""
    def __init__(self, status_code, text, content_type="text/html; charset=UTF-8"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(f"{self.status_code} error")


class _FakeCsvResp:
    """Raw-bytes response fake for the TOPIX CSV fetch - only .content
    (cp932-encoded bytes), .status_code and .headers are read; no
    .text/.raise_for_status (fetch_topix500() never calls either on
    the CSV response)."""
    def __init__(self, status_code, content_bytes, content_type="application/octet-stream"):
        self.status_code = status_code
        self.content = content_bytes
        self.headers = {"Content-Type": content_type}


# ======================================================================
# A1: fetch_nikkei225() logs the full diagnostic on an HTTP failure -
# url, status, content_type, rows parsed, first 200 chars.
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
      f"rows_parsed/first200 OK")


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
# A3: fetch_topix500() logs the same full diagnostic on a failure to
# even fetch the TOPIX index page, in addition to its own pre-existing
# required "[universe] TOPIX 500: constituent list unavailable (...) -
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
assert "TOPIX 500" in _diag_line and se.TOPIX_INDEX_PAGE_URL in _diag_line, _diag_line
assert "status=404" in _diag_line, _diag_line
assert "404 Not Found page body" in _diag_line, _diag_line
assert _required_line == (
    "[universe] TOPIX 500: constituent list unavailable "
    "(HTTP 404 fetching the TOPIX index page) - not scanning"
), _required_line
print(f"[A3_topix_diagnostic_plus_required_line] diagnostic={_diag_line!r}\n"
      f"    required(unchanged format)={_required_line!r} OK")


# ======================================================================
# B1 (Director's verified-facts reply, same day): fetch_nikkei225()
# against a bulleted-list fixture shaped like the real page - each
# entry "Company Name (TYO: <code>)", grouped under sector headers,
# >= 200 unique codes.
# ======================================================================
def _nikkei_bulleted_list_html(codes):
    sectors = ["Automobiles", "Electronics", "Banking", "Pharmaceuticals", "Retail"]
    body = []
    for i, code in enumerate(codes):
        if i % 45 == 0:
            body.append(f"<h3>{sectors[(i // 45) % len(sectors)]}</h3><ul>")
        body.append(f"<li>Company {code} Co., Ltd. (TYO: {code})</li>")
        if i % 45 == 44 or i == len(codes) - 1:
            body.append("</ul>")
    return f"<html><body>{''.join(body)}</body></html>"


_nikkei_codes_225 = [f"{1000 + i}" for i in range(225)]
_nikkei_fixture_html = _nikkei_bulleted_list_html(_nikkei_codes_225)

se.fetch_nikkei225.clear()
with mock.patch.object(se.requests, "get", return_value=_FakeResp(200, _nikkei_fixture_html)):
    df_nikkei = se.fetch_nikkei225()
assert df_nikkei is not None and len(df_nikkei) == 225, df_nikkei
assert set(df_nikkei["Ticker"]) == {f"{c}.T" for c in _nikkei_codes_225}, df_nikkei
print(f"[B1_nikkei_bulleted_list_fixture] 225 '(TYO: <code>)' bulleted-list entries -> "
      f"{len(df_nikkei)} tickers, all normalised to the .T suffix OK")


# ======================================================================
# B2: fewer than 200 codes found -> treated as a failure (never a
# partial list), diagnostic logs the actual count found.
# ======================================================================
_nikkei_codes_50 = [f"{2000 + i}" for i in range(50)]
_nikkei_fixture_html_short = _nikkei_bulleted_list_html(_nikkei_codes_50)

se.fetch_nikkei225.clear()
_short_lines = []
with mock.patch.object(se._log, "warning", side_effect=lambda msg: _short_lines.append(msg)), \
     mock.patch.object(se.requests, "get", return_value=_FakeResp(200, _nikkei_fixture_html_short)):
    df_nikkei_short = se.fetch_nikkei225()
assert df_nikkei_short is None, df_nikkei_short
assert len(_short_lines) == 1 and "rows_parsed=50" in _short_lines[0], _short_lines
print("[B2_nikkei_below_minimum] only 50 codes found (< 200) -> None, diagnostic "
      f"reports the real count: {_short_lines[0]!r} OK")


# ======================================================================
# B3: fetch_topix500() follows the topixweight_j.csv link found on the
# TOPIX index page (a RELATIVE href, resolved against the page's own
# URL), decodes the file as cp932, and keeps Core30/Large70/Mid400
# rows (dropping Small 1/Small 2) from the real column names/values.
# ======================================================================
def _topix_csv_cp932(n_core30, n_large70, n_mid400, n_small1, n_small2):
    rows = []
    code = 1300
    for _, group in (
        (n_core30, "TOPIX Core30"), (n_large70, "TOPIX Large70"),
        (n_mid400, "TOPIX Mid400"), (n_small1, "TOPIX Small 1"),
        (n_small2, "TOPIX Small 2"),
    ):
        for _i in range(_):
            rows.append(f"{code},テスト銘柄{code},{group}")
            code += 1
    csv_text = "\n".join(["コード,銘柄名,ニューインデックス区分"] + rows)
    return csv_text.encode("cp932")

_topix_index_page_html = (
    '<html><body><a href="/markets/indices/topix/tvdivq0000006pov-att/'
    'topixweight_j.csv">構成銘柄別ウエイト一覧</a></body></html>'
)
_topix_csv_bytes_ok = _topix_csv_cp932(30, 70, 400, 25, 25)


def _topix_get_ok(url, headers=None, timeout=None):
    if url == se.TOPIX_INDEX_PAGE_URL:
        return _FakeResp(200, _topix_index_page_html)
    assert url.endswith("topixweight_j.csv"), url
    assert url.startswith("https://www.jpx.co.jp/"), url  # relative href resolved absolute
    return _FakeCsvResp(200, _topix_csv_bytes_ok)


se.fetch_topix500.clear()
with mock.patch.object(se.requests, "get", side_effect=_topix_get_ok):
    df_topix = se.fetch_topix500()
assert df_topix is not None and len(df_topix) == 500, df_topix
print(f"[B3_topix_link_follow_cp932] index page's relative href resolved and followed, "
      f"cp932-decoded CSV parsed -> {len(df_topix)} Core30+Large70+Mid400 tickers "
      f"(Small 1/Small 2 dropped) OK")


# ======================================================================
# B4: fewer than 400 Core30/Large70/Mid400 rows -> treated as a
# failure (never a partial list).
# ======================================================================
_topix_csv_bytes_short = _topix_csv_cp932(10, 20, 30, 5, 5)


def _topix_get_short(url, headers=None, timeout=None):
    if url == se.TOPIX_INDEX_PAGE_URL:
        return _FakeResp(200, _topix_index_page_html)
    return _FakeCsvResp(200, _topix_csv_bytes_short)


se.fetch_topix500.clear()
_short_topix_lines = []
with mock.patch.object(se._log, "warning", side_effect=lambda lvl, *a: _short_topix_lines.append(lvl)), \
     mock.patch.object(se.requests, "get", side_effect=_topix_get_short):
    df_topix_short = se.fetch_topix500()
assert df_topix_short is None, df_topix_short
assert any("only 60 Core30/Large70/Mid400 row(s) found" in l for l in _short_topix_lines), _short_topix_lines
print("[B4_topix_below_minimum] only 60 matching rows (< 400) -> None, reason names "
      "the actual count OK")


# ======================================================================
# B5: the topixweight_j.csv link isn't found on the index page at all
# (JPX restructured the page) -> a clear, specific reason, never a
# guess at some other URL.
# ======================================================================
se.fetch_topix500.clear()
_nolink_lines = []
with mock.patch.object(se._log, "warning", side_effect=lambda lvl, *a: _nolink_lines.append(lvl)), \
     mock.patch.object(se.requests, "get",
                        return_value=_FakeResp(200, "<html><body>no matching link here</body></html>")):
    df_topix_nolink = se.fetch_topix500()
assert df_topix_nolink is None, df_topix_nolink
assert any("topixweight_j.csv link not found" in l for l in _nolink_lines), _nolink_lines
print("[B5_topix_link_not_found] index page fetched fine but no topixweight_j.csv "
      "link on it -> specific reason, no URL guessed OK")


print("\nALL Director addendum 2, Part 1, item A (Japan lists) + Part 2 item 2 "
      "(real Nikkei/TOPIX parsers) CHECKS PASSED")
print(
    "\nNOTE for the Director: B1-B5 above are run against SYNTHETIC fixtures "
    "shaped from the facts you verified directly (bulleted '(TYO: <code>)' list "
    "items for Nikkei 225; a relative topixweight_j.csv link followed from the "
    "TOPIX index page, cp932-decoded, コード/銘柄名/ニューインデックス区分 columns, "
    "'TOPIX Core30'/'TOPIX Large70'/'TOPIX Mid400' values kept, 'TOPIX Small 1'/"
    "'TOPIX Small 2' dropped) - this sandbox still has no outbound network access "
    "to verify either page/file directly. The real proof is the first live scan."
)
