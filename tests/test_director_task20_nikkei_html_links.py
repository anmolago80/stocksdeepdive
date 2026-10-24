"""
TASK 20 (Director-directed, 24 Oct 2026) - fix Nikkei 225 constituent
fetch: scanner_engine.fetch_nikkei225() gets en.wikipedia.org/wiki/
Nikkei_225 fine (status=200, text/html, full page) but rows_parsed=0.
Root cause: the old _NIKKEI225_TYO_CODE_RE ran over the RAW HTML, where
Wikipedia renders each entry's exchange code as separate links
("(<a ...>TYO</a>: <a ...>7267</a>)"), so the literal "(TYO: 7267)" text
the old regex needed never appears in the source - only in the page's
rendered/visible text.

Fixed by matching _NIKKEI225_CODE_RE against _html_to_visible_text(resp.
text) (tags stripped, entities unescaped, whitespace collapsed) instead
of the raw body.

Covers the task's own listed tests:
  1. a fixture mimicking the REAL Wikipedia link markup (TYO and the
     code as separate <a> tags inside parentheses, sector headings,
     &nbsp; spacing) - must parse.
  2. the plain-text fixture (no nested <a> tags - already covered by
     tests/test_director_addendum2_part1a_japan_lists.py's own B1/B2)
     still parses the same way under the new regex/pipeline.
  3. an alphanumeric code (e.g. "285A", TSE's newer code format).
  4. < 200 codes found -> None, diagnostic extended with a 300-char
     plain-text window around the first "TYO" occurrence.
  5. no "TYO" anywhere in the visible text -> diagnostic says so
     explicitly ("TYO not found"), rather than a blank/confusing extra.

ALL FIXTURES ARE SYNTHETIC - this sandbox has no outbound network
access to verify against the real page; the real proof is the first
live scan (per the Director's own instruction, same as every other
Japan-universe test in this repo).

Run: python3 tests/test_director_task20_nikkei_html_links.py
"""
import builtins
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import scanner_engine as se

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


class _FakeResp:
    """Same shape as the addendum2 Part 1A test's own fake - .text/
    .content/.headers/.status_code, no live network involved."""
    def __init__(self, status_code, text, content_type="text/html; charset=UTF-8"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(f"{self.status_code} error")


def _nikkei_real_link_markup_html(codes, alphanumeric_tail=None):
    """Builds a fixture that mimics the REAL Wikipedia markup - the
    "TYO" label and the numeric code are each their OWN <a> tag inside
    one parenthesised group, exactly the shape the live diagnostic
    showed and the old regex (which looked for the literal text
    "(TYO: <code>)") could never match. Sector headings are <h2>/<h3>
    elements (also tags, also invisible to a raw-HTML regex); one
    entry's separator uses "&nbsp;" instead of a plain space, matching
    the Director's own "nbsp spacing" callout. `alphanumeric_tail`, if
    given, is appended as one more entry whose code is that exact
    string (e.g. "285A") instead of a plain digit code."""
    sectors = ["Automobiles", "Electronics", "Banking", "Pharmaceuticals", "Retail"]
    body = []
    for i, code in enumerate(codes):
        if i % 45 == 0:
            body.append(f"<h3>{sectors[(i // 45) % len(sectors)]}</h3><ul>")
        sep = "&nbsp;" if i == 0 else " "
        body.append(
            f'<li><a href="/wiki/Company_{code}">Company {code} Co., Ltd.</a> '
            f'(<a href="/wiki/Tokyo_Stock_Exchange" title="Tokyo Stock Exchange">TYO</a>:{sep}'
            f'<a href="/wiki/{code}" title="{code}">{code}</a>)</li>'
        )
        if i % 45 == 44 or i == len(codes) - 1:
            body.append("</ul>")
    if alphanumeric_tail:
        body.append(
            f'<li><a href="/wiki/Company_{alphanumeric_tail}">Some New Co., Ltd.</a> '
            f'(<a href="/wiki/Tokyo_Stock_Exchange" title="Tokyo Stock Exchange">TYO</a>: '
            f'<a href="/wiki/{alphanumeric_tail}" title="{alphanumeric_tail}">{alphanumeric_tail}</a>)</li>'
        )
    return f"<html><body>{''.join(body)}</body></html>"


# ======================================================================
# CHECK 1+3: the real link-markup fixture (TYO/code as separate <a>
# tags, sector headings, &nbsp; spacing) parses correctly, INCLUDING
# one alphanumeric code ("285A") mixed in with the numeric ones.
# ======================================================================
_codes_225 = [f"{3000 + i}" for i in range(224)]
_html_links = _nikkei_real_link_markup_html(_codes_225, alphanumeric_tail="285A")

se.fetch_nikkei225.clear()
with mock.patch.object(se.requests, "get", return_value=_FakeResp(200, _html_links)):
    df = se.fetch_nikkei225()
check("the real-link-markup fixture (TYO/code as separate <a> tags) is NOT rejected "
      "(this is the exact live-production bug: the old regex found 0 rows against this shape)",
      df is not None)
check("224 numeric codes + 1 alphanumeric code ('285A') -> 225 tickers total",
      df is not None and len(df) == 225)
check("every numeric code is normalised to the .T suffix",
      df is not None and set(f"{c}.T" for c in _codes_225).issubset(set(df["Ticker"])))
check("the alphanumeric TSE code '285A' is parsed and normalised to '285A.T' "
      "(symbol_mapping.to_yahoo_symbol's own documented TSE example shape)",
      df is not None and "285A.T" in set(df["Ticker"]))
print("[task20_c1c3_real_link_markup] the real Wikipedia <a>-tag-per-field markup "
      "(incl. an alphanumeric code) now parses OK")


# ======================================================================
# CHECK 2: the plain-text fixture (no nested <a> tags at all - just the
# literal "(TYO: <code>)" text, the shape every pre-existing Nikkei
# test in this repo already uses) still parses the same way.
# ======================================================================
def _nikkei_plain_text_html(codes):
    body = ["<h3>Automobiles</h3><ul>"]
    for code in codes:
        body.append(f"<li>Company {code} Co., Ltd. (TYO: {code})</li>")
    body.append("</ul>")
    return f"<html><body>{''.join(body)}</body></html>"


_codes_plain = [f"{4000 + i}" for i in range(225)]
_html_plain = _nikkei_plain_text_html(_codes_plain)

se.fetch_nikkei225.clear()
with mock.patch.object(se.requests, "get", return_value=_FakeResp(200, _html_plain)):
    df_plain = se.fetch_nikkei225()
check("the plain-text '(TYO: <code>)' fixture (no nested <a> tags) still parses under "
      "the new text-extraction pipeline",
      df_plain is not None and len(df_plain) == 225)
check("every plain-text code is normalised to the .T suffix",
      df_plain is not None and set(df_plain["Ticker"]) == {f"{c}.T" for c in _codes_plain})
print("[task20_c2_plain_text_kept] the plain-text fixture shape keeps working under "
      "the new pipeline OK")


# ======================================================================
# CHECK 4: < 200 codes found (real link markup, same as the live page
# shape) -> None, and the diagnostic is extended with a 300-char plain-
# text window around the first "TYO" occurrence.
# ======================================================================
_codes_short = [f"{5000 + i}" for i in range(60)]
_html_short = _nikkei_real_link_markup_html(_codes_short)

se.fetch_nikkei225.clear()
_short_lines = []
with mock.patch.object(se._log, "warning", side_effect=lambda msg: _short_lines.append(msg)), \
     mock.patch.object(se.requests, "get", return_value=_FakeResp(200, _html_short)):
    df_short = se.fetch_nikkei225()
check("< 200 codes (even under the new link-aware parser) -> still treated as a "
      "failure, never a partial list", df_short is None)
check("the diagnostic reports the real count (60)",
      len(_short_lines) == 1 and "rows_parsed=60" in _short_lines[0])
check("the diagnostic is extended with a 300-char plain-text window around the "
      "first 'TYO' occurrence (not blank, not just the raw-HTML preview)",
      len(_short_lines) == 1 and "extra=" in _short_lines[0]
      and "around first TYO" in _short_lines[0] and "Company 5000" in _short_lines[0])
print(f"[task20_c4_below_minimum_extended_diagnostic] {_short_lines[0]!r} OK")


# ======================================================================
# CHECK 5: no "TYO" anywhere in the fetched page's visible text ->
# diagnostic says so explicitly, not a confusing blank/empty window.
# ======================================================================
_html_no_tyo = "<html><body><h1>Nikkei 225</h1><p>Page under maintenance.</p></body></html>"
se.fetch_nikkei225.clear()
_no_tyo_lines = []
with mock.patch.object(se._log, "warning", side_effect=lambda msg: _no_tyo_lines.append(msg)), \
     mock.patch.object(se.requests, "get", return_value=_FakeResp(200, _html_no_tyo)):
    df_no_tyo = se.fetch_nikkei225()
check("a page with no 'TYO' anywhere -> None", df_no_tyo is None)
check("the diagnostic says 'TYO not found' explicitly, and the count is 0",
      len(_no_tyo_lines) == 1 and "rows_parsed=0" in _no_tyo_lines[0]
      and "TYO not found" in _no_tyo_lines[0])
print(f"[task20_c5_tyo_not_found] {_no_tyo_lines[0]!r} OK")


# ======================================================================
# CHECK 6: _html_to_visible_text()'s own fallback path (bs4 unavailable
# at the function's own `from bs4 import BeautifulSoup` call) produces
# the SAME result as the bs4 path, against the same real-link-markup
# sample - this is the "else" half of "BeautifulSoup get_text(' ') if
# available, else re.sub(...)" the task itself specifies.
# ======================================================================
_real_import = builtins.__import__


def _no_bs4(name, *args, **kwargs):
    if name == "bs4":
        raise ImportError("simulated: bs4 not installed")
    return _real_import(name, *args, **kwargs)


_fallback_sample = _nikkei_real_link_markup_html(["7267"], alphanumeric_tail="285A")
builtins.__import__ = _no_bs4
try:
    _fallback_text = se._html_to_visible_text(_fallback_sample)
finally:
    builtins.__import__ = _real_import
_fallback_codes = se._NIKKEI225_CODE_RE.findall(_fallback_text)
check("the regex-only fallback (bs4 unavailable) extracts the same codes as the "
      "bs4 path against the same real-link-markup sample",
      _fallback_codes == ["7267", "285A"])
print(f"[task20_c6_bs4_fallback] fallback text extraction (no bs4) -> {_fallback_codes} OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
