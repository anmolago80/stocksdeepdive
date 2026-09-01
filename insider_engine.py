"""
insider_engine.py

Services batch, Part 2: insider dealings & buybacks - the fetch/parse half
(insider_store.py is pure storage). No Anthropic API call anywhere in this
module - every row is a filing/figure pulled from a public, structured
source and, where parseable, its own stated numbers.

Two independent sources, chosen by ticker suffix (a ticker never hits
both):
  - ASX (".AX" tickers): the same public announcements JSON endpoint
    portfolio_news_engine.py already uses, filtered to the five notice
    types Part 2 names (Appendix 3X/3Y/3Z director's-interest notices,
    3C/3E buy-back notices), then a best-effort PDF text extraction
    (pypdf) for each - if that extraction can't find the numbers, the
    filing is still stored with its title/date/link (see insider_store.
    add_filing's docstring), never dropped.
  - Everything else (US-listed tickers): SEC EDGAR's public submissions
    JSON for the issuer's own CIK, filtered to Form 4s, then a best-effort
    XML parse of each filing's primary document for the reporting owner/
    transaction code/shares/price. Requires a descriptive User-Agent with
    a contact address per SEC's own etiquette guidance, and stays well
    under its <=10 req/s guideline via a per-request sleep.

CAVEAT (flagged again in the batch's own final report): neither fetch
path can be exercised against live ASX/SEC data from this sandbox (no
outbound network here) - the JSON shapes and PDF/XML parsing were built
directly from each source's documented/publicly-known structure and
verified with direct unit tests against representative text, but the
owner should spot-check the first few real nightly refreshes after this
deploys, same as any integration that couldn't be run against the real
service before shipping.

TTL: insider_store.should_refetch(ticker, max_age_hours=24) gates every
call to refresh() below - the same per-key last-fetch-timestamp
convention portfolio_news_engine.py's news_fetch_log already uses.
"""

import io
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests

import insider_store

_UA = {"User-Agent": "StocksDeepDive/1.0 (+https://stocksdeepdive.com)"}
_SEC_UA = {"User-Agent": "StocksDeepDive research contact rationalcompounder@stocksdeepdive.com"}
_TIMEOUT = 10

REFETCH_STALE_HOURS = 24

# Per-refresh caps - bound how much work one ticker's refresh (or one
# night's whole universe pass) can do, so a busy insider calendar or a
# large nightly universe can never blow the time budget or SEC's rate
# guidance on their own. A ticker not fully covered simply gets the rest
# on its next refresh (nightly, or on-demand on its next stale Deep Dive
# view) - nothing here is a one-shot, all-or-nothing fetch.
MAX_FORM4_PER_REFRESH = 15
INSIDER_NIGHTLY_CAP = 60

_ASX_TITLE_MAP = (
    ("initial director's interest notice", "3X", "insider"),
    ("change of director's interest notice", "3Y", "insider"),
    ("final director's interest notice", "3Z", "insider"),
    ("daily share buy-back notice", "3E", "buyback"),
    ("notification of buy-back", "3C", "buyback"),
)

# Populated once per process by _cik_for_ticker() - SEC's own ticker->CIK
# map is ~10k rows and doesn't change intraday, so one fetch per process
# lifetime (not per ticker) is enough; a fresh scheduler/Streamlit process
# just re-fetches it once on first use.
_TICKER_CIK_MAP = None


def _num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_asx_date(s):
    if not s:
        return None
    s = str(s)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _match_asx_type(header):
    h = (header or "").lower()
    for needle, code, kind in _ASX_TITLE_MAP:
        if needle in h:
            return code, kind
    return None, None


# --------------------------------------------------------------------
# ASX: fetch + PDF parse
# --------------------------------------------------------------------

def _parse_director_notice_text(text):
    """Best-effort field extraction from an Appendix 3X/3Y/3Z's PDF text.
    Deliberately simple regexes against the form's well-known field
    labels - real filings vary in exact spacing/line breaks, so this will
    often come back partially or fully empty; the filing is still stored
    either way (see insider_store.add_filing)."""
    out = {"person": None, "action": None, "quantity": None, "price": None}
    m = re.search(r"Name of [Dd]irector\s*[:\n]?\s*([A-Z][A-Za-z'\.\-, ]{2,60})", text)
    if m:
        out["person"] = m.group(1).strip().splitlines()[0][:80]
    acq = re.search(r"Number acquired\s*[:\n]?\s*\+?\s*([\d,]+)", text)
    disp = re.search(r"Number disposed\s*[:\n]?\s*\+?\s*([\d,]+)", text)
    acq_n = _num(acq.group(1)) if acq else None
    disp_n = _num(disp.group(1)) if disp else None
    if acq_n:
        out["action"], out["quantity"] = "BUY", acq_n
    elif disp_n:
        out["action"], out["quantity"] = "SELL", disp_n
    val = re.search(r"Value/?\s*[Cc]onsideration\s*[:\n]?\s*\$?\s*([\d,\.]+)", text)
    if val and out["quantity"]:
        total_val = _num(val.group(1))
        if total_val:
            out["price"] = round(total_val / out["quantity"], 4)
    return out


def _parse_buyback_notice_text(text):
    """Best-effort field extraction from an Appendix 3C/3E's PDF text -
    same caveat as _parse_director_notice_text."""
    out = {"person": None, "action": "BUYBACK", "quantity": None, "price": None}
    n = re.search(r"[Nn]umber of \+?\s*securities bought back\s*[:\n]?\s*([\d,]+)", text)
    if n:
        out["quantity"] = _num(n.group(1))
    p = re.search(r"(?:[Hh]ighest|[Aa]verage) price paid[^$\d]{0,20}\$?\s*([\d,\.]+)", text)
    if p:
        out["price"] = _num(p.group(1))
    return out


def _extract_pdf_text(content, max_pages=4):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    text = ""
    for page in reader.pages[:max_pages]:
        try:
            text += (page.extract_text() or "") + "\n"
        except Exception:
            continue
    return text


def _parse_asx_pdf(url, kind, log=print):
    out = {"person": None, "action": None, "quantity": None, "price": None}
    try:
        r = requests.get(url, headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200 or not r.content:
            log(f"[insider_engine] ASX PDF fetch non-200/empty for {url}: "
                f"status={r.status_code} bytes={len(r.content or b'')}")
            return out
        text = _extract_pdf_text(r.content)
        if not text.strip():
            log(f"[insider_engine] ASX PDF had no extractable text for {url} "
                f"({len(r.content)} bytes fetched)")
            return out
        if kind == "insider":
            out.update(_parse_director_notice_text(text))
        else:
            out.update(_parse_buyback_notice_text(text))
        if not any(out.values()):
            log(f"[insider_engine] ASX PDF text extracted for {url} but no "
                f"fields matched the expected labels (kind={kind})")
    except Exception as e:
        log(f"[insider_engine] PDF parse failed for {url}: {e}")
    return out


def _update_asx_buyback_summary(ticker, filed_at, parsed):
    date_str = filed_at.strftime("%d %b %Y") if filed_at else "an unknown date"
    if parsed.get("quantity"):
        text = f"Latest buy-back notice filed {date_str}: {parsed['quantity']:,.0f} securities bought back."
        if parsed.get("price"):
            text = text[:-1] + f" at an average/highest price of AUD {parsed['price']:,.2f}."
    else:
        text = f"Latest buy-back notice filed {date_str} - see filing for figures."
    insider_store.set_buyback_summary(
        ticker, "asx_notice", text, amount=parsed.get("quantity"), currency="AUD",
        filed_at=filed_at.strftime("%Y-%m-%d") if filed_at else None,
    )


def refresh_asx(ticker, log=print):
    if not ticker.upper().endswith(".AX"):
        return
    code = ticker.split(".")[0].upper()
    try:
        r = requests.get(
            f"https://www.asx.com.au/asx/1/company/{code}/announcements",
            params={"count": 100, "market_sensitive": "false"},
            headers=_UA, timeout=_TIMEOUT,
        )
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            log(f"[insider_engine] {ticker}: ASX announcements fetch failed ({r.status_code})")
            insider_store.mark_fetched(ticker)
            return
        data = (r.json() or {}).get("data", []) or []
    except Exception as e:
        log(f"[insider_engine] {ticker}: ASX fetch error: {e}")
        return

    buyback_done = False
    matched = 0
    for a in data:
        header = a.get("header") or a.get("title") or ""
        code_type, kind = _match_asx_type(header)
        if not code_type:
            continue
        matched += 1
        link = a.get("url") or a.get("document_url") or ""
        if not link:
            continue
        filed_at = _parse_asx_date(a.get("document_date") or a.get("date"))
        parsed = _parse_asx_pdf(link, kind, log=log)
        insider_store.add_filing(
            ticker, "asx", code_type,
            filed_at=filed_at.strftime("%Y-%m-%d") if filed_at else None,
            person=parsed.get("person"), action=parsed.get("action"),
            quantity=parsed.get("quantity"), price=parsed.get("price"),
            link=link, raw_title=header,
        )
        if kind == "buyback" and not buyback_done:
            _update_asx_buyback_summary(ticker, filed_at, parsed)
            buyback_done = True
    # Distinguishes "endpoint/filter problem" from "this company simply has
    # no insider/buyback notices of the 5 tracked types" (e.g. a
    # majority-founder-owned company like OCL.AX, where a stable 60%+
    # holder who isn't trading can genuinely have zero recent director's-
    # interest CHANGE notices) - both looked identical ("No filings found")
    # on the page with no way to tell them apart until now.
    if not data:
        log(f"[insider_engine] {ticker}: ASX announcements endpoint returned 0 announcements total")
    elif matched == 0:
        log(f"[insider_engine] {ticker}: {len(data)} ASX announcement(s) fetched, "
            f"0 matched the tracked notice types - sample headers: "
            f"{[(a.get('header') or a.get('title') or '')[:60] for a in data[:5]]}")
    insider_store.mark_fetched(ticker)


# --------------------------------------------------------------------
# US: SEC EDGAR fetch + Form 4 XML parse
# --------------------------------------------------------------------

def _cik_for_ticker(ticker, log=print):
    global _TICKER_CIK_MAP
    if _TICKER_CIK_MAP is None:
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json",
                              headers=_SEC_UA, timeout=_TIMEOUT)
            if r.status_code == 200:
                data = r.json() or {}
                _TICKER_CIK_MAP = {
                    (v.get("ticker") or "").upper(): str(v.get("cik_str")).zfill(10)
                    for v in data.values() if v.get("ticker")
                }
            else:
                _TICKER_CIK_MAP = {}
        except Exception as e:
            log(f"[insider_engine] SEC ticker/CIK map fetch failed: {e}")
            _TICKER_CIK_MAP = {}
    # US tickers sometimes carry a dash-for-class-share suffix (BRK-B) that
    # SEC's own map lists differently (BRK.B or plain BRK-B) - try the
    # exact ticker first, then the dot/dash swap, before giving up.
    t = ticker.upper()
    if t in _TICKER_CIK_MAP:
        return _TICKER_CIK_MAP[t]
    alt = t.replace("-", ".") if "-" in t else t.replace(".", "-")
    return _TICKER_CIK_MAP.get(alt)


def _parse_form4(url, log=print):
    """Best-effort reporting-owner/transaction extraction from a Form 4's
    primary document. Every failure branch below LOGS why (status code,
    content-type, a short content snippet) rather than silently returning
    an all-None `out` - root-caused live on CPRT (2026-08-31): every one
    of its Form 4 rows came back with person/action/quantity/price all
    blank, and there was no log trail at all to tell a genuine fetch
    block (e.g. SEC's bot mitigation on www.sec.gov, which is stricter
    than the data.sec.gov API endpoint the filing LIST itself came from -
    CPRT's filing dates/links DID come through fine) apart from the
    filing simply not being raw XML (the one case this already
    anticipated). This logging is what turns the next real refresh into
    an actual diagnosis instead of another silent no-op."""
    out = {"person": None, "action": None, "quantity": None, "price": None}
    r = None
    try:
        r = requests.get(url, headers=_SEC_UA, timeout=_TIMEOUT)
        if r.status_code != 200 or not r.content:
            log(f"[insider_engine] Form 4 fetch non-200/empty for {url}: "
                f"status={r.status_code} bytes={len(r.content or b'')}")
            return out
        root = ET.fromstring(r.content)
        name_el = root.find(".//rptOwnerName")
        if name_el is not None and name_el.text:
            out["person"] = name_el.text.strip()[:80]
        code_el = root.find(".//transactionCode")
        code = code_el.text.strip() if code_el is not None and code_el.text else None
        if code == "P":
            out["action"] = "BUY"
        elif code == "S":
            out["action"] = "SELL"
        shares_el = root.find(".//transactionShares/value")
        if shares_el is not None and shares_el.text:
            out["quantity"] = _num(shares_el.text)
        price_el = root.find(".//transactionPricePerShare/value")
        if price_el is not None and price_el.text:
            out["price"] = _num(price_el.text)
        if out["person"] is None and code_el is None:
            log(f"[insider_engine] Form 4 XML for {url} parsed but had "
                f"neither rptOwnerName nor transactionCode - unexpected shape, "
                f"root tag={root.tag!r}")
    except ET.ParseError as e:
        # primaryDocument wasn't raw XML for this filing (e.g. an
        # XSL-rendered HTML link, a rate-limit/error page, or a redirect) -
        # leave unparsed, still stored/listed, but log what we actually
        # got back so this is diagnosable instead of a silent dead end.
        ctype = r.headers.get("Content-Type") if r is not None else None
        snippet = r.content[:200].decode("utf-8", "replace") if (r is not None and r.content) else ""
        log(f"[insider_engine] Form 4 XML parse failed for {url}: {e} - "
            f"status={r.status_code if r is not None else None} content-type={ctype} "
            f"snippet={snippet!r}")
    except Exception as e:
        log(f"[insider_engine] Form 4 parse failed for {url}: {e}")
    return out


def _update_us_buyback_summary(ticker, log=print):
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        cf = tk.cashflow
        if cf is None or cf.empty:
            return
        row = None
        for candidate in ("Repurchase Of Capital Stock", "Repurchase Of Common Stock",
                           "Common Stock Repurchased", "Repurchase Of Capital Stock "):
            if candidate in cf.index:
                row = cf.loc[candidate]
                break
        if row is None or row.empty:
            return
        latest_val = row.iloc[0]
        if latest_val is None:
            return
        amount = abs(float(latest_val))
        try:
            currency = (tk.info or {}).get("currency") or "USD"
        except Exception:
            currency = "USD"
        col = cf.columns[0]
        period_label = col.strftime("%Y") if hasattr(col, "strftime") else str(col)
        text = f"Repurchases, last FY ({period_label}): {currency} {amount:,.0f} (from the cash-flow statement)."
        insider_store.set_buyback_summary(ticker, "us_cashflow", text, amount=amount,
                                           currency=currency, filed_at=None)
    except Exception as e:
        log(f"[insider_engine] {ticker}: buyback cashflow lookup failed: {e}")


def refresh_sec(ticker, log=print):
    if ticker.upper().endswith(".AX"):
        return
    cik = _cik_for_ticker(ticker, log=log)
    if not cik:
        log(f"[insider_engine] {ticker}: no SEC CIK match - skipping")
        insider_store.mark_fetched(ticker)
        return
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                          headers=_SEC_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            log(f"[insider_engine] {ticker}: EDGAR submissions fetch failed ({r.status_code})")
            insider_store.mark_fetched(ticker)
            return
        payload = r.json() or {}
    except Exception as e:
        log(f"[insider_engine] {ticker}: EDGAR submissions error: {e}")
        return

    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []

    n_checked = 0
    for i, form in enumerate(forms):
        if form != "4":
            continue
        if n_checked >= MAX_FORM4_PER_REFRESH:
            break
        n_checked += 1
        if i >= len(accessions) or i >= len(docs):
            continue
        accession = accessions[i].replace("-", "")
        doc = docs[i]
        filed = dates[i] if i < len(dates) else None
        if not doc:
            continue
        link = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{doc}"
        # Root-caused live, 2026-08-31 (the same nightly run this new
        # logging was added to diagnose): SEC's submissions API returns
        # `primaryDocument` for ownership forms (3/4/5) WITH an
        # "xslF345X0N/" folder segment baked into the filename (e.g.
        # "xslF345X06/form4.xml") - that path is SEC's own server-side
        # XSLT-rendered HTML viewer, not the raw XML. Fetching it (as
        # `link` above does) returns HTTP 200 with content-type text/html
        # and a page titled "SEC FORM 4" - not a per-filing quirk, every
        # single Form 4 fetched this way tonight failed identically ("Form
        # 4 XML parse failed ... mismatched tag: line 29, column 16"),
        # confirmed by pulling the actual response snippets from Railway's
        # logs. The same accession folder also lists the identical
        # document under its bare filename with no xsl subfolder (verified
        # against a live EDGAR filing index page) - that is the real raw
        # XML. `link` (the human-facing SEC viewer URL, stored on the
        # filing and what a visitor clicks) is left as-is; only the URL
        # actually fetched for parsing is changed to the bare filename.
        parse_url = (f"https://www.sec.gov/Archives/edgar/data/"
                     f"{int(cik)}/{accession}/{doc.rsplit('/', 1)[-1]}")
        time.sleep(0.11)  # stay comfortably under SEC's <=10 req/s guidance
        parsed = _parse_form4(parse_url, log=log)
        insider_store.add_filing(
            ticker, "sec", "Form 4", filed_at=filed,
            person=parsed.get("person"), action=parsed.get("action"),
            quantity=parsed.get("quantity"), price=parsed.get("price"),
            link=link, raw_title=f"Form 4 - {parsed.get('person') or 'insider'}",
        )
    insider_store.mark_fetched(ticker)
    _update_us_buyback_summary(ticker, log=log)


# --------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------

def refresh(ticker, force=False, log=print):
    """Refresh insider/buyback data for one ticker if stale (>24h) or
    force=True. Returns True if a refresh actually ran. ASX tickers go
    through refresh_asx, everything else through refresh_sec - a ticker
    never hits both paths in one call."""
    if not ticker:
        return False
    if not force and not insider_store.should_refetch(ticker, max_age_hours=REFETCH_STALE_HOURS):
        return False
    try:
        if ticker.upper().endswith(".AX"):
            refresh_asx(ticker, log=log)
        else:
            refresh_sec(ticker, log=log)
    except Exception as e:
        log(f"[insider_engine] {ticker}: refresh failed: {e}")
    return True


def refresh_universe(rows, cap=INSIDER_NIGHTLY_CAP, log=print):
    """Nightly hook (Services batch Part 2): refresh insider/buyback data
    for up to `cap` stale tickers from tonight's scan rows - see
    INSIDER_NIGHTLY_CAP's own comment for why this is capped rather than
    covering a whole universe every night. A ticker not refreshed tonight
    keeps showing its last-known data until a later night (or an
    on-demand Deep Dive view) catches it."""
    checked = 0
    refreshed = 0
    for row in rows:
        if refreshed >= cap:
            break
        ticker = (row.get("Ticker") or "").strip().upper()
        if not ticker:
            continue
        checked += 1
        try:
            if refresh(ticker, log=log):
                refreshed += 1
        except Exception as e:
            log(f"[insider_engine] {ticker}: nightly refresh error: {e}")
    return {"checked": checked, "refreshed": refreshed}
