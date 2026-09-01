"""
asx_announcements.py

Fix 11 (2026-09-01): shared ASX company-announcements fetcher, used by
both insider_engine.refresh_asx() and portfolio_news_engine's ASX news
feed - previously each had its own copy of the same fetch, both pointed
at the same now-dead endpoint (see below).

The old shared source, `https://www.asx.com.au/asx/1/company/{code}/
announcements`, returns 404 for every ticker (confirmed in last night's
Railway logs, dozens of tickers) - so ASX insider/buy-back rows and ASX
portfolio news have been empty sitewide. The replacement, verified LIVE
from the owner's own browser on 1 Sep 2026, is ASX's own public
announcements search page:

    https://www.asx.com.au/asx/v2/statistics/announcements.do
        ?by=asxCode&asxCode={code}&timeframe=D&period=M6

This returns a plain server-rendered HTML table (not JSON) - Date /
Price sens. / Headline - with each row's own <a> pointing at the PDF:
    https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do
        ?display=pdf&idsId={idsId}

CAVEAT, same standing one as every other source added this engagement
without this sandbox's own live network access to verify against: the
URL and column shapes above come from the owner's own live browser
testing (not this session's), so the request shape (headers, params) is
best-effort realistic-browser rather than independently confirmed here;
the parser below is written defensively (locates the table by its
header CONTENT - "date" and "headline" both present - not by position/
id/class) for exactly that reason, same discipline scanner_engine.py's
fetchers use for Wikipedia's own table-shape drift.

The ASX WAF (bot-mitigation layer) can return an HTTP 200 "Request
Rejected" page instead of the real table - this is explicitly NOT
"zero announcements": fetch() raises AsxFetchError for it (and for any
other non-200/network failure), so a caller can log/report it plainly
rather than showing "no filings found" for a company that may have
plenty.
"""

import re

import lxml.html
import requests

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/webp,image/apng,*/*;q=0.8"),
}
_TIMEOUT = 10

ANNOUNCEMENTS_URL = "https://www.asx.com.au/asx/v2/statistics/announcements.do"

# The ASX WAF's own rejection page title - HTTP 200, but the body is
# this, not a real announcements table. Checked against the first slice
# of the response body only (cheap, and the real page's <title> is also
# near the top) rather than the whole document.
_WAF_MARKER = "Request Rejected"

_PAGE_SIZE_SUFFIX_RE = re.compile(r"\s*\d+\s*pages?\s*\([\d.,]+\s*[KMkm]?[Bb]\)\s*$")
_IDS_ID_RE = re.compile(r"[?&]idsId=([0-9]+)", re.IGNORECASE)
_DATE_PREFIX_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})\s*(.*)$")


class AsxFetchError(Exception):
    """Raised for a detected FAILURE - network error, non-200, the WAF
    rejection page, or a response whose HTML has no table matching the
    expected Date/Headline shape at all (a bigger structural change than
    this parser's content-based matching can absorb). Deliberately
    distinct from a genuine empty result (a real company with zero
    announcements in the window), which is a normal empty list return,
    not an exception - callers must never conflate the two (see the
    module docstring)."""


def fetch(code, period="M6"):
    """Fetches and parses ASX's own announcements search page for `code`
    (bare ASX code, ".AX" suffix stripped if present) over `period`
    (ASX's own query param on this endpoint - "M6" = 6 months, matching
    the URL verified live). Returns a list of {date, time, headline,
    price_sensitive, pdf_url, idsId} dicts in the page's own (newest-
    first) order, or an empty list for a genuine zero-announcements
    result. Raises AsxFetchError for anything that must NOT be read as
    "no announcements" - see AsxFetchError's own docstring."""
    code = (code or "").strip().upper().replace(".AX", "")
    if not code:
        raise AsxFetchError("empty ASX code")
    try:
        resp = requests.get(
            ANNOUNCEMENTS_URL,
            params={"by": "asxCode", "asxCode": code, "timeframe": "D", "period": period},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
    except Exception as e:
        raise AsxFetchError(f"request failed: {e}") from e
    if resp.status_code != 200:
        raise AsxFetchError(f"HTTP {resp.status_code}")
    html = resp.text or ""
    if _WAF_MARKER in html[:4000]:
        raise AsxFetchError("ASX WAF rejection page ('Request Rejected')")
    return _parse_announcements_html(html)


def _split_date_time(cell_text):
    """'01/09/2026 8:26 AM' -> ('01/09/2026', '8:26 AM'). A cell with no
    recognizable leading D/M/YYYY date just comes back as (None, None) -
    a row this happens on is skipped by the caller rather than stored
    with a garbage date."""
    cell_text = (cell_text or "").strip()
    m = _DATE_PREFIX_RE.match(cell_text)
    if not m:
        return None, None
    date_part, rest = m.group(1), m.group(2).strip()
    return date_part, (rest or None)


def _parse_announcements_html(html):
    """Locates the announcements table by CONTENT (a header row whose
    text contains both "date" and "headline"), not by position, id, or
    class - this endpoint's exact markup couldn't be confirmed ahead of
    a live run (see the module docstring's caveat), so matching on the
    one thing genuinely unlikely to change - the columns actually being
    called "Date" and "Headline" - is the resilient choice, same
    discipline scanner_engine.py's own fetchers use for Wikipedia. Stops
    at the FIRST such table and returns whatever rows it has (possibly
    zero, for a genuinely empty result) - it does not keep hunting past
    a legitimate match. Raises AsxFetchError if NO table on the page
    matches at all (a bigger shape change than this can absorb, not a
    "zero announcements" result)."""
    try:
        doc = lxml.html.fromstring(html)
    except Exception as e:
        raise AsxFetchError(f"HTML parse failed: {e}") from e

    for table in doc.xpath("//table"):
        header_cells = table.xpath(".//tr[1]//th | .//tr[1]//td")
        header_text = " ".join((c.text_content() or "").strip().lower() for c in header_cells)
        if "date" not in header_text or "headline" not in header_text:
            continue

        out = []
        for row in table.xpath(".//tr")[1:]:
            cells = row.xpath("./td")
            if len(cells) < 3:
                continue
            date_part, time_part = _split_date_time(cells[0].text_content())
            if not date_part:
                continue
            sens_text = (cells[1].text_content() or "").strip()
            headline_cell = cells[2]
            headline = (headline_cell.text_content() or "").strip()
            headline = _PAGE_SIZE_SUFFIX_RE.sub("", headline).strip()
            if not headline:
                continue
            pdf_url, ids_id = None, None
            anchors = headline_cell.xpath(".//a[@href]")
            if anchors:
                href = anchors[0].get("href") or ""
                if href:
                    pdf_url = href if href.startswith("http") else f"https://www.asx.com.au{href}"
                    m = _IDS_ID_RE.search(href)
                    if m:
                        ids_id = m.group(1)
            out.append({
                "date": date_part,
                "time": time_part,
                "headline": headline,
                "price_sensitive": bool(sens_text),
                "pdf_url": pdf_url,
                "idsId": ids_id,
            })
        return out

    raise AsxFetchError("no Date/Headline announcements table found on the page")
