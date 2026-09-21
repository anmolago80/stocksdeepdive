"""
scanner_engine.py

Universe data for the Stock Scanner page: Country -> Universe -> (optional)
Sector filter -> a plain ticker list, fed straight into the same scan/
results pipeline Comparison already uses (_render_scan_page in app.py).

Ported from the original desktop app's universe_engine.py, trimmed down to
real, well-known indices the public site offers - each one maps to ONE
real index, never a blend of two (the original had several "USA Momentum
(S&P 500 + Nasdaq 100)"-style combined universes; those are deliberately
not carried over here, so picking a universe always means exactly one
real, well-known index and nothing gets double-counted), plus (Fix 8a, AI
fixes round 2, 2026-08-31) a handful of DERIVED universes built by
filtering an already-fetched parent pool rather than a scrape of their
own (labeled as such in get_universe_pool's source string):

  Australia: ASX 200, ASX 300 (derived), All Ordinaries (derived),
             ASX Small Ordinaries (derived), ASX 100, ASX 50, ASX 20,
             ASX All Technology (derived)
  USA:       S&P 500, Nasdaq 100, Russell 2000, Small Caps (S&P SmallCap
             600), S&P 400 MidCap, Russell 1000, S&P 1500 (derived)

Index containment (20 Sep 2026): ASX 300 and All Ordinaries used to be
their own independent live scrapes (asx300list.com/allordslist.com) but
those turned out to be a frozen 28 April 2021 snapshot each - verified
live 20 Sep 2026 (both GQG.AX, listed since 2021, and GGP.AX were
missing). Both are now DERIVED: the real, live Wikipedia ASX 200 plus a
market-cap-ranked tail from the ASX's own official listed-companies CSV
(fetch_asx_listed_companies()) - real, current membership for the first
200, an honest approximation (not verified S&P index membership) past
that. See fetch_asx300()/fetch_allords()'s own docstrings.

Every non-derived fetcher is a live scrape (Wikipedia constituent tables,
or an iShares ETF holdings export for Russell 2000/1000) cached for 24h
via st.cache_data, with a small static/derived fallback if the live
source is unreachable or its page structure has changed - so Run Scan
always returns SOMETHING rather than a silent empty universe. The source
actually used is always shown back to the user (see get_universe_pool's
second return value) so it's never ambiguous whether a scan is running on
live, derived or fallback data. The five new source URLs added in round 2
(Dow 30, S&P 400, Russell 1000, All Ordinaries, ASX 20/50/100) could not
be live-verified from either dev environment used to build this - see
their own constants' comments above for that caveat; their first real
test is the actual nightly run after this deploys.

9 Sep 2026 (owner-reported bug): that caveat turned out to matter for Dow
Jones 30 - fetch_dow30()'s target Wikipedia page no longer carries a
"Components" table matching what the scraper looks for, so it never
resolved any tickers and the universe never had a first successful
nightly scan. Removed from USA_UNIVERSES/the Scanner's popular pills/the
nightly cadence (replaced by Russell 2000, believed at the time to be the
most resilient US fetcher here); fetch_dow30()/DOW30_WIKI_URL/its
get_universe_pool() branch are left in place below, just unreferenced, in
case Wikipedia's page structure gets fixed later.

Part 52 (16 Sep 2026): that belief turned out to be wrong too - the
production nightly scheduler's own logs showed "Russell 2000: no tickers
resolved (Web scrape unavailable)" on every attempt, and Russell 3000
(its derived union with Russell 1000) with it. Root cause: ishares.com
blocks requests from the production datacentre outright, and the disk
cache (fetch_russell2000()'s/fetch_russell1000()'s own last-resort
fallback) had therefore never once been populated - there was no
fallback under the fallback. Fixed per-universe: Russell 1000 gained a
live Wikipedia fallback (fetch_russell1000_wikipedia(), which also feeds
sector_cache_store for free); Russell 2000 has no size-matched public
constituent table anywhere, so it gained a repo-committed static holdings
snapshot instead (_r2k_static_fallback_df() - a real, once-downloaded,
verified ~1,957-row export, not a hand-picked list, honestly labelled
with its as-of date and expected to go stale until manually refreshed at
the next reconstitution). Both fetchers now also try the live iShares
CSV with browser-like headers and one retry first (_fetch_ishares_csv) -
cheap, might start working again someday. See _resolve_russell1000()/
_resolve_russell2000() for the full per-universe fallback chains, and
Part 52's own report for what was actually verified live before this
shipped.
"""

import concurrent.futures
import io
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

import alert_engine
import market_cap_engine
import sector_cache_store
import source_health_store

_log = logging.getLogger("sdd.scanner")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StocksDeepDiveBot/1.0; +https://stocksdeepdive.com)"}

# Part 52: iShares blocks the production datacentre outright (confirmed via
# nightly scheduler logs - "Russell 2000: no tickers resolved (Web scrape
# unavailable)" on every attempt), not just a bare-Python-UA rejection - so
# this alone is "cheap, might start working" rather than a guaranteed fix,
# per the Part's own framing. Real Chrome UA/Accept/Accept-Language/Referer
# instead of _HEADERS' honest-bot string above, used only for the two
# iShares CSV exports (IWB/IWM) - every other fetch in this module keeps
# its existing _HEADERS untouched.
_ISHARES_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.ishares.com/",
}


def _fetch_ishares_csv(url, timeout=20, retries=1, backoff=1.5):
    """GET an iShares holdings-CSV export with _ISHARES_BROWSER_HEADERS and
    one retry after a short backoff - a blocked/rate-limited response is
    sometimes transient, and this is nearly free to try. Raises on final
    failure; callers already wrap this in their own try/except, same as
    every other requests.get call in this module."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=_ISHARES_BROWSER_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff)
    raise last_exc

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP600_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
ASX200_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_200"

# Fix 8a, AI fixes round 2 (2026-08-31). Every URL below follows the
# exact same "Wikipedia constituent table, or an iShares ETF holdings
# CSV" pattern the six sources above already use - see get_universe_
# pool()'s dispatch and each fetch_*() function for the specific
# fallback chain per universe. IMPORTANT CAVEAT (stated plainly rather
# than silently assumed): neither this dev sandbox nor the owner's
# device has live network access to Wikipedia/iShares/asx300list.com
# (a standing constraint for this whole engagement - see every prior
# fix that touched a live data source), so these five new URLs/sources
# could not be fetched and test-parsed against real HTML the way the
# six existing ones originally were. Each new fetcher below reuses the
# same defensive _parse_table() (keyword-matched columns, min/max-row
# guards) and the same graceful multi-level fallback discipline as the
# existing fetchers, so a broken/changed source degrades to a fallback
# universe rather than a 500 or a silently-empty scan - but their FIRST
# real test is the actual nightly run after this deploys. Report this
# clearly rather than claiming false certainty.
DOW30_WIKI_URL = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
SP400_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
ASX20_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_20"
ASX50_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_50"

# No dedicated Wikipedia constituent-table page could be confirmed for
# S&P/ASX 100 (searched during development; the obvious candidate URL
# redirects to a generic ASX overview article, not a constituent
# table) - fetch_asx100() below tries it anyway (harmless if it 404s or
# fails the row-count guard) and falls back to ASX 200 (live) if it
# doesn't pan out, exactly like fetch_asx300()'s own existing fallback
# chain. Kept as a named constant so a real source can be swapped in
# later without hunting through the function body.
ASX100_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_100"


# iShares Russell 1000 ETF (IWB) holdings export - same mechanism as
# IWM_HOLDINGS_CSV_URL below (Russell 2000). iShares' shorter
# query-string-only URL form (no numeric product-id path segment)
# redirects to the real export; requests.get follows redirects by
# default, same as every other _get()/direct-requests call in this
# module.
IWB_HOLDINGS_CSV_URL = (
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
    "?fileType=csv&fileName=IWB_holdings&dataType=fund"
)

# Part 52.1: Russell 1000 Wikipedia fallback, tried after the live iShares
# IWB export fails. NOT "Russell 1000 Index" (https://en.wikipedia.org/
# wiki/Russell_1000_Index) - that article only describes the index and
# carries no constituent table. This is the dedicated list article,
# live-verified during this Part's development (WebSearch -> WebFetch,
# then the full page fetched and test-parsed against real HTML - see
# Part 52's own report): a "Company / Symbol / GICS Sector / GICS
# Sub-Industry" table, ~1,000 rows.
RUSSELL1000_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_Russell_1000_companies"

# Part 34.4 (11 Sep 2026): S&P 500 Dividend Aristocrats' own Wikipedia
# article - live-verified during development via WebSearch->WebFetch
# (this dev sandbox has no direct network route to Wikipedia, same
# constraint as every other fetcher's own comment in this file - see
# above): the page carries a real "Ticker symbol / Company / Sector"
# table, 69 rows at verification time, so min_rows/max_rows below are
# set with real headroom either side of that (not a guess).
DIVIDEND_ARISTOCRATS_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P_500_Dividend_Aristocrats"

# Index containment (20 Sep 2026): asx300list.com/allordslist.com - the
# old ASX 300/All Ordinaries sources - turned out to be a frozen 28 April
# 2021 snapshot (verified live 20 Sep 2026: both GQG.AX, listed since
# 2021, and GGP.AX were absent from them). Replaced entirely by
# fetch_asx_listed_companies() below - the ASX's own official CSV
# directory of every listed company, ranked by market cap to fill out
# the tail past Wikipedia's live ASX 200 - see fetch_asx300()/
# fetch_allords()'s own comments for the new construction.
#
# Commit I (21 Sep 2026): that replacement - www.asx.com.au/asx/research/
# ASXListedCompanies.csv - turned out to be the SAME failure mode one
# level up: it never once passed _check_asx_listed_companies() (cross_
# source: 8 live ASX 200 tickers missing, including DNL/Dyno Nobel and
# SGH/Seven Group Holdings), and manual inspection showed the file's own
# header carries the CURRENT date while its ROWS are frozen months
# behind - still listing INCITEC PIVOT as IPL (renamed DNL), BLOCK INC.
# as SQ2, SEVEN GROUP HOLDINGS as SVW (renamed SGH). A fresh timestamp
# over stale content, not a dead endpoint - the row-count/canary/cross-
# source checks below still do the real work; only the URL and its
# column layout change here. Replaced by the file behind asx.com.au's
# own company directory page (markitdigital, the vendor behind ASX's
# market-data widgets) - unofficial (asx.com.au itself doesn't document
# it as a public API), so every existing health check stays on it and
# last-known-good keeps gating what actually gets served, exactly like
# the source it replaces. Columns per the ASX's own directory page (live-
# verified by the owner, not from this sandbox - see _fetch_asx_listed_
# companies_raw()'s own docstring for the standing "no live network
# route" caveat every fetcher in this module already carries): "ASX
# code","Company name","GICs industry group","Listing date","Market Cap".
ASX_LISTED_COMPANIES_CSV_URL = "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"

# iShares Russell 2000 ETF (IWM) public holdings export. Best-effort - iShares
# occasionally changes this URL format.
IWM_HOLDINGS_CSV_URL = (
    "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
)

# Static, tiny emergency fallback if the S&P 500 scrape itself is down -
# better than returning nothing at all.
_SP500_STATIC_FALLBACK = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "BRK-B", "UNH"]

# Part 34.4 (11 Sep 2026): Dow Jones 30 hardcoded static fallback - the
# Dow's OWN Wikipedia article has never carried a parseable "Components"
# table (confirmed again during this Part's development, live, via
# WebSearch->WebFetch - matches fetch_dow30()'s own long-standing
# comment), so unlike every other universe here, its live scrape can
# never be trusted to eventually work; the universe needs a real,
# maintained fallback rather than just "try again next deploy". This 30-
# ticker list is the current membership AS OF 29 JUN 2026 (Alphabet/
# GOOGL replaced Verizon/VZ that day - S&P Dow Jones Indices press
# release, cross-checked against a second independent source) - update
# this constant + comment by hand on the next reconstitution, same as
# any other dated static list in this codebase. Tried AFTER
# fetch_dow30()'s own scrape attempt in get_universe_pool() below, never
# instead of it, so a future fix to Wikipedia's page structure is picked
# up automatically without this constant needing to be removed.
_DOW30_STATIC_FALLBACK = [
    "NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "JPM", "WMT", "V", "JNJ", "CSCO",
    "CVX", "KO", "CAT", "MRK", "UNH", "PG", "GS", "HD", "AXP", "IBM",
    "AMGN", "CRM", "DIS", "MCD", "BA", "MMM", "SHW", "TRV", "HON", "NKE",
]

# Local ASX 200 fallback map, used only if the live ASX 200 scrape fails -
# hand-grouped by sector so the Sector filter still works on fallback data.
ASX_SECTOR_MAP = {
    "CBA.AX": "Banks", "NAB.AX": "Banks", "WBC.AX": "Banks",
    "ANZ.AX": "Banks", "BOQ.AX": "Banks", "BEN.AX": "Banks",

    "BHP.AX": "Mining & Resources", "RIO.AX": "Mining & Resources",
    "FMG.AX": "Mining & Resources", "MIN.AX": "Mining & Resources",
    "S32.AX": "Mining & Resources", "NIC.AX": "Mining & Resources",
    "ILU.AX": "Mining & Resources", "IGO.AX": "Mining & Resources",

    "NST.AX": "Gold", "EVN.AX": "Gold", "GOR.AX": "Gold",

    "WDS.AX": "Energy", "STO.AX": "Energy", "ORG.AX": "Energy", "VEA.AX": "Energy",

    "CSL.AX": "Healthcare", "COH.AX": "Healthcare", "RMD.AX": "Healthcare",
    "SHL.AX": "Healthcare", "PME.AX": "Healthcare", "RHC.AX": "Healthcare",
    "VNT.AX": "Healthcare",

    "MQG.AX": "Financials", "ASX.AX": "Financials", "SUN.AX": "Financials",
    "QBE.AX": "Financials", "MFG.AX": "Financials", "CHC.AX": "Financials",
    "HMC.AX": "Financials", "ING.AX": "Financials", "MPL.AX": "Financials",
    "NWL.AX": "Financials", "PNI.AX": "Financials", "SOL.AX": "Financials",
    "ZIP.AX": "Financials",

    "GMG.AX": "Property & REITs", "SCG.AX": "Property & REITs",
    "GPT.AX": "Property & REITs", "DXS.AX": "Property & REITs",
    "VCX.AX": "Property & REITs", "LLC.AX": "Property & REITs",
    "SGP.AX": "Property & REITs",

    "WOW.AX": "Consumer", "COL.AX": "Consumer", "WES.AX": "Consumer",
    "JBH.AX": "Consumer", "HVN.AX": "Consumer", "TPW.AX": "Consumer",
    "ARB.AX": "Consumer", "DMP.AX": "Consumer", "MTS.AX": "Consumer",
    "NEC.AX": "Consumer", "TAH.AX": "Consumer",

    "XRO.AX": "Technology", "SEK.AX": "Technology", "CAR.AX": "Technology",
    "REA.AX": "Technology", "WTC.AX": "Technology", "NXL.AX": "Technology",
    "IRE.AX": "Technology",

    "BXB.AX": "Industrials", "TCL.AX": "Industrials", "QUB.AX": "Industrials",
    "ORI.AX": "Industrials", "CPU.AX": "Industrials", "SVW.AX": "Industrials",

    "TLS.AX": "Telecommunications", "TPG.AX": "Telecommunications",
    "CNU.AX": "Telecommunications",

    "APA.AX": "Infrastructure", "ALD.AX": "Infrastructure",

    "FLT.AX": "Travel & Leisure", "QAN.AX": "Travel & Leisure",

    "JHX.AX": "Materials", "BLD.AX": "Materials", "CSR.AX": "Materials",
    "AMC.AX": "Materials", "BSL.AX": "Materials",

    "LTR.AX": "Lithium & Battery Metals", "LYC.AX": "Lithium & Battery Metals",
    "PDN.AX": "Lithium & Battery Metals", "SFR.AX": "Lithium & Battery Metals",
    "YAL.AX": "Lithium & Battery Metals",

    "IAG.AX": "Insurance",

    "AIA.AX": "Other", "IPH.AX": "Other",
}


def _find_column(columns, keywords):
    for col in columns:
        low = str(col).lower()
        for kw in keywords:
            if kw in low:
                return col
    return None


def _normalize_us_ticker(ticker):
    return str(ticker).strip().replace(".", "-").upper()


def _normalize_asx_ticker(ticker):
    t = str(ticker).strip().upper()
    return t if t.endswith(".AX") else f"{t}.AX"


def _parse_table(html_text, ticker_keywords, sector_keywords, normalize_fn, min_rows=1, max_rows=None):
    """
    Scans every table on the page for the first one with a ticker-like
    column, returned as a ['Ticker','Sector'] frame. `min_rows` guards
    against a small "Top N Holdings"-style summary table (which can also
    have a ticker + sector column) being mistaken for the real, full
    constituent table - any table shorter than min_rows is skipped.

    `max_rows` (Fix 8a, AI fixes round 2, 2026-08-31): an optional upper
    bound, for sources whose real constituent count is well-known and
    small (e.g. Dow 30) - tightens the match beyond min_rows alone so an
    unrelated, larger table elsewhere on the same page can't be silently
    mistaken for the real one just because it also happens to have a
    ticker-like column and enough rows. None (the default) keeps every
    existing caller's behaviour unchanged.
    """
    try:
        tables = pd.read_html(io.StringIO(html_text))
    except Exception:
        return None

    for table in tables:
        ticker_col = _find_column(table.columns, ticker_keywords)
        sector_col = _find_column(table.columns, sector_keywords)

        if ticker_col is None:
            continue

        cols = [ticker_col] + ([sector_col] if sector_col else [])
        df = table[cols].copy()
        df.columns = ["Ticker", "Sector"] if sector_col else ["Ticker"]
        df = df.dropna(subset=["Ticker"])

        if df.empty or len(df) < min_rows:
            continue
        if max_rows is not None and len(df) > max_rows:
            continue

        df["Ticker"] = df["Ticker"].apply(normalize_fn)
        if "Sector" in df.columns:
            df["Sector"] = df["Sector"].astype(str).str.strip()
        else:
            df["Sector"] = None

        return df[["Ticker", "Sector"]]

    return None


def _get(url):
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_sp500():
    try:
        html = _get(SP500_WIKI_URL)
    except Exception:
        return None
    return _parse_table(html, ["symbol", "ticker"], ["gics sector", "sector"], _normalize_us_ticker, min_rows=400)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_sp600():
    try:
        html = _get(SP600_WIKI_URL)
    except Exception:
        return None
    return _parse_table(html, ["symbol", "ticker"], ["gics sector", "sector"], _normalize_us_ticker, min_rows=400)


def _parse_nasdaq100_table(html_text):
    """Fix 10 (2026-09-01): a resilient parse purpose-built for
    Wikipedia's Nasdaq-100 page, whose constituents table has changed
    shape/heading more than once - root cause of "no tickers resolved
    (Web scrape unavailable)" on all 3 nightly attempts the doc reported.
    _parse_table() alone (the shared helper every other fetcher uses)
    matches the first table with a ticker-like column and enough rows -
    too loose for this specific page, which has other tables (recent
    changes, sector weighting) that can also have a ticker-like column.
    Two things tightened here instead:

      1. Table located by HTML id="constituents" FIRST when present -
         Wikipedia's own most stable identifier for this specific table -
         before falling back to keyword/shape matching across every
         table on the page.
      2. A candidate table's header must contain BOTH a ticker-like
         column ("ticker"/"symbol") AND a company-name column
         ("company") - ticker-alone isn't enough to tell the real
         constituents table apart from an unrelated one nearby.

    Sanity-checked to 90-110 rows either way (the real count sits around
    100, with occasional constituent changes) - a match outside that
    band is "wrong table", not "the index shrank to 40 stocks". Returns
    a ['Ticker','Sector'] frame, or None if nothing on the page matches
    all of the above."""
    try:
        id_tables = pd.read_html(io.StringIO(html_text), attrs={"id": "constituents"})
    except (ValueError, ImportError):
        id_tables = []
    # id-matched table(s) first, then every table on the page as a
    # fallback pass - so an id="constituents" hit is always tried before
    # falling back to header-keyword matching.
    candidates = list(id_tables) + list(_safe_read_html(html_text))

    for table in candidates:
        cols_low = [str(c).lower() for c in table.columns]
        has_ticker = any(("ticker" in c or "symbol" in c) for c in cols_low)
        has_company = any("company" in c for c in cols_low)
        if not (has_ticker and has_company):
            continue
        ticker_col = _find_column(table.columns, ["ticker", "symbol"])
        if ticker_col is None:
            continue
        sector_col = _find_column(table.columns, ["gics sector", "sector"])
        cols = [ticker_col] + ([sector_col] if sector_col else [])
        df = table[cols].copy()
        df.columns = ["Ticker", "Sector"] if sector_col else ["Ticker"]
        df = df.dropna(subset=["Ticker"])
        if not (90 <= len(df) <= 110):
            continue
        df["Ticker"] = df["Ticker"].apply(_normalize_us_ticker)
        df["Sector"] = df["Sector"].astype(str).str.strip() if "Sector" in df.columns else None
        return df[["Ticker", "Sector"]]
    return None


def _safe_read_html(html_text):
    try:
        return pd.read_html(io.StringIO(html_text))
    except Exception:
        return []


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_nasdaq100():
    try:
        html = _get(NASDAQ100_WIKI_URL)
    except Exception:
        return None
    return _parse_nasdaq100_table(html)


# Fix 10 (2026-09-01) fallback #2: Invesco's own QQQ holdings CSV export -
# QQQ tracks the Nasdaq-100 essentially 1:1, same mechanism family as the
# iShares IWM/IWB holdings CSVs already used above for Russell 2000/1000.
# CAVEAT, stated plainly rather than assumed (same standing constraint as
# every other URL added without live-network verification this
# engagement - see the module docstring): this URL could not be fetched
# and test-parsed against Invesco's real CSV shape from either dev
# environment used to build this - its first real test is the actual
# nightly run after this deploys.
NASDAQ100_INVESCO_CSV_URL = (
    "https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0"
    "?audienceType=Investor&action=download&ticker=QQQ"
)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_nasdaq100_invesco():
    """Fix 10 fallback #2 for Nasdaq 100 - see NASDAQ100_INVESCO_CSV_URL's
    comment for the live-verification caveat. Because the exact header
    row position/column names in Invesco's export couldn't be confirmed
    ahead of time, the header is located defensively - the first line (of
    the first 30) that looks like a real CSV header with a ticker-like
    column - rather than assumed via a hardcoded skiprows count the way
    fetch_russell2000()/fetch_russell1000() do for iShares' (already-
    confirmed) export shape. Returns None on any failure - no reachable
    URL, no header found, no ticker column, or a resulting row count
    outside the same 90-110 sane-count band _parse_nasdaq100_table() uses -
    so get_universe_pool() falls further back to the static list below."""
    try:
        resp = requests.get(NASDAQ100_INVESCO_CSV_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        raw_lines = resp.text.splitlines()
    except Exception:
        return None

    header_idx = None
    for i, line in enumerate(raw_lines[:30]):  # header block is at most a few rows
        low = line.lower()
        if ("ticker" in low or "symbol" in low) and "," in line:
            header_idx = i
            break
    if header_idx is None:
        return None

    try:
        raw = pd.read_csv(io.StringIO("\n".join(raw_lines[header_idx:])), on_bad_lines="skip")
    except Exception:
        return None

    ticker_col = _find_column(raw.columns, ["ticker", "symbol"])
    if ticker_col is None:
        return None

    df = raw[[ticker_col]].copy()
    df.columns = ["Ticker"]
    df = df.dropna(subset=["Ticker"])
    _t = df["Ticker"].astype(str).str.strip().str.upper()
    df = df[~_t.str.contains("CASH|USD|NET ASSETS", na=False) & (_t.str.len() <= 6) & (_t.str.len() > 0)]
    if not (90 <= len(df) <= 110):
        return None

    df["Ticker"] = df["Ticker"].apply(_normalize_us_ticker)
    df["Sector"] = None
    return df[["Ticker", "Sector"]]


# Fix 10 (2026-09-01) fallback #3, last resort if BOTH the Wikipedia
# scrape and the Invesco CSV are unavailable/unparseable: a static
# best-effort Nasdaq-100 constituent list committed in the repo, same
# spirit as _SP500_STATIC_FALLBACK above but a real ~100-ticker list
# instead of a 10-ticker token one - "a real one is better", per the doc.
# Same caveat as every static list here: index membership changes over
# time and this was not live-verified against the current official
# constituent list - it exists purely so a scan never comes back
# completely empty, not as an authoritative source.
_NASDAQ100_STATIC_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO", "TSLA", "COST",
    "NFLX", "ADBE", "PEP", "CSCO", "AMD", "TMUS", "INTC", "CMCSA", "TXN", "QCOM",
    "AMGN", "HON", "INTU", "AMAT", "BKNG", "ISRG", "VRTX", "SBUX", "GILD", "ADI",
    "MDLZ", "REGN", "LRCX", "PANW", "ADP", "MU", "PYPL", "SNPS", "CDNS", "KLAC",
    "MELI", "CSX", "MAR", "ORLY", "CTAS", "ABNB", "CRWD", "FTNT", "NXPI", "MNST",
    "PCAR", "ROP", "WDAY", "PAYX", "ODFL", "DXCM", "KDP", "AEP", "EXC", "XEL",
    "CHTR", "IDXX", "KHC", "FAST", "EA", "VRSK", "CTSH", "BIIB", "GEHC", "DDOG",
    "TTD", "TEAM", "ON", "ZS", "MRVL", "ANSS", "GFS", "ILMN", "WBD", "DLTR",
    "ALGN", "SIRI", "ENPH", "LULU", "JD", "PDD", "ASML", "BKR", "CDW", "CPRT",
    "CCEP", "TTWO", "MDB", "WBA", "ROST", "ZM", "DASH", "ARM", "SMCI", "GEN",
]


def _r2k_cache_path():
    """Last-good Russell 2000 constituent list, persisted to the Railway
    Volume (or this directory locally) - iShares occasionally changes its
    holdings-export URL, and the fallback for that shouldn't be an empty
    universe when yesterday's list is sitting right there."""
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, "russell2000_cache.csv")


def _disk_cache_label(universe_name, cache_path):
    """Part 52: the disk-cache fallback used to share the SAME "Web scrape
    unavailable" label as total failure (get_universe_pool never actually
    distinguished "served yesterday's real list" from "served nothing") -
    this makes it honest, and states the list's own as-of date (the
    cache file's mtime - it's rewritten only when a fetch actually
    succeeds, so mtime IS the list's real age) same as the static file's
    own dated label below."""
    try:
        as_of = datetime.fromtimestamp(os.path.getmtime(cache_path), tz=timezone.utc).strftime("%d %b %Y")
        return f"{universe_name} scrape unavailable - last cached list (as of {as_of}) instead"
    except OSError:
        return f"{universe_name} scrape unavailable - last cached list instead"


def _clean_ishares_holdings_df(raw, min_rows=1, max_rows=None):
    """Shared cleaning for an iShares holdings-CSV export already read into
    a DataFrame (skiprows=9 past the metadata block) - used by BOTH the
    live IWM/IWB fetch and the Russell 2000 static-file fallback (Part
    52.2), so there is zero behavioural drift between "live" and
    "static" data quality.

    Drops cash/FX placeholder rows, the literal "-" ticker iShares uses
    for unlisted/escrow/private-placement lines (a real $ holding but not
    a tradeable, scannable ticker - found and fixed during this Part's
    verification against the real ~1,957-row IWM export, where it was
    silently surviving the old CASH/USD-only filter), and anything
    longer than 6 chars. (An earlier version also dropped any ticker
    containing "-" entirely, which silently removed legitimate class
    shares - small-cap indices do contain them - so this only excludes
    the exact "-" placeholder, not every ticker containing a dash.)

    Returns None if there's no ticker-like column, nothing survives
    cleaning, or the resulting row count falls outside [min_rows,
    max_rows] (max_rows=None skips that upper check) - the same min/max-
    row guard discipline _parse_table() already uses for Wikipedia
    tables, applied here for CSV exports."""
    ticker_col = _find_column(raw.columns, ["ticker"])
    if ticker_col is None:
        return None

    df = raw[[ticker_col]].copy()
    df.columns = ["Ticker"]
    df = df.dropna(subset=["Ticker"])
    _t = df["Ticker"].astype(str).str.strip().str.upper()
    df = df[
        ~_t.str.contains("CASH|USD", na=False)
        & (_t.str.len() > 0) & (_t.str.len() <= 6)
        & (_t != "-")
    ]
    if df.empty or len(df) < min_rows:
        return None
    if max_rows is not None and len(df) > max_rows:
        return None

    df["Ticker"] = df["Ticker"].apply(_normalize_us_ticker)
    df["Sector"] = None
    return df[["Ticker", "Sector"]].drop_duplicates(subset="Ticker").reset_index(drop=True)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_russell2000():
    """Best-effort LIVE attempt only. iShares publishes IWM's full holdings
    as a downloadable CSV; the export has several metadata rows before
    the real header. Returns a ['Ticker','Sector'] frame (Sector is
    always None - this export's own Sector column is coarser than GICS
    and isn't wired up here) or None on ANY failure.

    Part 52: no internal fallback anymore - iShares blocks the
    production datacentre outright (nightly scheduler logs: "Russell
    2000: no tickers resolved (Web scrape unavailable)" on every
    attempt), so this now uses _fetch_ishares_csv's browser-like headers
    + one retry (cheap, might start working) and returns None straight
    away on failure. _resolve_russell2000() below owns the full disk-
    cache/static-file chain, so THIS function's return value alone tells
    get_universe_pool whether the LIVE path specifically worked, for an
    accurate source label - the old version conflated "live worked" and
    "disk cache worked" into the same return value."""
    try:
        text = _fetch_ishares_csv(IWM_HOLDINGS_CSV_URL)
        raw = pd.read_csv(io.StringIO(text), skiprows=9, on_bad_lines="skip")
    except Exception:
        return None

    out = _clean_ishares_holdings_df(raw)
    if out is None:
        return None
    try:
        out.to_csv(_r2k_cache_path(), index=False)
    except OSError:
        pass
    return out


def _r2k_static_cache_path():
    """The repo-committed emergency fallback file (Part 52.2) - distinct
    from _r2k_cache_path() above, which is a RUNTIME artifact that only
    exists after some night's LIVE scrape has succeeded at least once.
    Production has never had that happen (iShares blocks the datacentre
    outright), so the disk cache alone left Russell 2000 permanently
    empty. This file ships in the repo instead."""
    return os.path.join(os.path.dirname(__file__), "russell2000_static_2026-09-14.csv")


# Part 52.2: as-of date for the static file's own honest label, kept as a
# named constant next to the file itself so the two can never drift apart
# silently - update both together (filename + this constant + the file's
# contents) by hand at the next reconstitution.
RUSSELL2000_STATIC_AS_OF = "14 Sep 2026"


def _r2k_static_fallback_df():
    """Parses the repo-committed static IWM holdings snapshot (see
    _r2k_static_cache_path()'s own comment for why this exists) through
    the EXACT SAME _clean_ishares_holdings_df() the live CSV path uses -
    same skiprows=9 metadata-block skip, same cleaning, same output
    shape - so there's zero behavioural drift between "live" and
    "static" Russell 2000 data. min_rows=1500/max_rows=2500 brackets the
    real, verified row count (1,957 constituents as of 14 Sep 2026 - see
    Part 52's own report for how this file was obtained and verified)
    with headroom either side, same guard discipline as every other
    fetcher in this module."""
    try:
        with open(_r2k_static_cache_path(), "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    try:
        raw = pd.read_csv(io.StringIO(text), skiprows=9, on_bad_lines="skip")
    except Exception:
        return None
    return _clean_ishares_holdings_df(raw, min_rows=1500, max_rows=2500)


def _resolve_russell2000():
    """(df, source_label) for Russell 2000, trying each source in order
    (Part 52.2): live iShares IWM CSV -> yesterday's disk cache -> the
    repo-committed static snapshot. The live fetch persists to the disk
    cache on success (see fetch_russell2000() above) so a single success
    on ANY night gives every later cold-start a real fallback before
    ever reaching the static file. Shared by get_universe_pool's own
    "Russell 2000" branch (which needs the label) and _russell3000_df()
    (which only needs the df - Russell 3000 gets this whole chain "for
    free" simply by calling this instead of the old bare
    fetch_russell2000())."""
    df = fetch_russell2000()
    if df is not None:
        return df, "iShares IWM ETF holdings (live)"
    df = _r2k_from_disk_cache()
    if df is not None:
        return df, _disk_cache_label("Russell 2000", _r2k_cache_path())
    df = _r2k_static_fallback_df()
    if df is not None:
        return df, f"iShares export unavailable - static holdings list (as of {RUSSELL2000_STATIC_AS_OF})"
    return None, "Web scrape unavailable"


# -----------------------------------------------------------------
# Index containment (20 Sep 2026): the AU universes form a strict chain -
# ASX 20 subset ASX 50 subset ASX 100 subset ASX 200 subset ASX 300 subset
# All Ordinaries - so every ticker in a smaller index MUST also appear in
# every larger one. Each fetcher above scrapes its own INDEPENDENT source
# (a different Wikipedia page or third-party list per universe), so
# there's no structural guarantee two adjacent sources agree - one can
# simply be a stale/incomplete snapshot relative to the other (e.g.
# GQG.AX present in the live ASX 300 source but absent from the ASX 200
# one, or vice versa, depending on which page is currently more current).
# The existing sector-merge steps below only ever ANNOTATE rows that are
# already present; they never add a missing row, so a gap like that
# persisted silently through to the scan/plot output.
#
# _asx_backfill_missing_subset_tickers() below fixes this at the source:
# called from every fetch_* function that has a smaller sibling in the
# chain, right after that function's own source fetch (and, where one
# exists, before the sector-merge step - sector-merge still runs
# afterward for the whole frame, backfilled rows included). One shared
# helper rather than five copies of the same union logic.
# -----------------------------------------------------------------

def _asx_backfill_missing_subset_tickers(superset_df, subset_df, superset_label, subset_label):
    """Enforces `subset_label` subset `superset_label` by unioning any
    ticker `subset_df` has that `superset_df` is missing into
    `superset_df` - carrying the ticker's own Sector across. A caller's
    later sector-merge step (fetch_asx300/fetch_allords) can still
    overwrite that Sector with a more authoritative source; this only
    guarantees the ticker itself is present.

    Returns `superset_df` unchanged (including None) if either frame is
    None/empty or nothing is actually missing - never raises, same
    fail-open convention as every other fetcher in this module. Logs one
    INFO line with the backfilled count whenever it does something, so a
    growing number of missing constituents is visible in the logs
    instead of silently accumulating."""
    if superset_df is None or subset_df is None or subset_df.empty:
        return superset_df
    missing = subset_df[~subset_df["Ticker"].isin(set(superset_df["Ticker"]))]
    if missing.empty:
        return superset_df
    _log.info(
        "scanner_engine: %s was missing %d ticker(s) that %s contains - "
        "backfilled: %s",
        superset_label, len(missing), subset_label, ", ".join(sorted(missing["Ticker"])),
    )
    return pd.concat([superset_df, missing[["Ticker", "Sector"]]], ignore_index=True)


_ASX200_SOURCE_NAME = "ASX 200 (Wikipedia)"

# Same +/-15% drift convention as _ASX_CSV_DRIFT_BAND/_MARKET_CAP_ROW_
# COUNT_DRIFT_BAND below - wide enough for ordinary index reconstitution,
# narrow enough to catch a collapsed or broken scrape.
_ASX200_DRIFT_BAND = 0.15


def _fetch_asx200_raw():
    """Fetch + parse only - no health check, no last-known-good fallback
    (see fetch_asx200() below, the public wrapper every other function
    in this module actually calls). Same fail-open contract this always
    had: None on any fetch/parse failure, or if the parsed table came
    back with not one single row carrying a Sector (the shape-changed-
    entirely case a plain row-count floor wouldn't catch)."""
    try:
        html = _get(ASX200_WIKI_URL)
    except Exception:
        return None
    df = _parse_table(html, ["code", "ticker", "symbol"], ["sector", "industry"], _normalize_asx_ticker, min_rows=150)
    if df is not None and df["Sector"].notna().sum() == 0:
        return None
    return df


def _check_asx200(df):
    """Commit G (20 Sep 2026): row_count (drift vs last-known-good) and
    sector_coverage (a structurally-valid-but-content-broken scrape can
    still clear the row-count floor - e.g. Wikipedia keeps the table
    shape but drops the Sector column's real values). Same {check_name:
    {"ok":, "detail":}} shape as _check_asx_listed_companies() /
    _rebuild_market_cap_ranking()'s own checks; never raises."""
    checks = {}
    prior = source_health_store.get(_ASX200_SOURCE_NAME)
    prior_count = (prior or {}).get("last_good_row_count")
    if prior_count:
        lo, hi = prior_count * (1 - _ASX200_DRIFT_BAND), prior_count * (1 + _ASX200_DRIFT_BAND)
        ok = lo <= len(df) <= hi
        checks["row_count"] = {
            "ok": ok,
            "detail": f"{len(df)} row(s) vs last-known-good {prior_count} (expected {int(lo)}-{int(hi)})",
        }
    else:
        checks["row_count"] = {"ok": True, "detail": "skipped - no last-known-good on record yet"}

    have_sector = int(df["Sector"].notna().sum())
    coverage = (have_sector / len(df)) if len(df) else 0.0
    checks["sector_coverage"] = {
        "ok": coverage >= 0.5,
        "detail": f"{have_sector}/{len(df)} row(s) carry a Sector ({coverage:.0%}, band >= 50%)",
    }
    return checks


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx200():
    """Public wrapper - every OTHER function in this module (and
    _AU_CONTAINMENT_CHAIN below) calls this, never _fetch_asx200_raw()
    directly. Commit G (20 Sep 2026): Wikipedia used to be a single
    point of failure for THREE universes - this one directly, plus ASX
    300/All Ordinaries, which both derive their tail from
    _asx_non200_by_marketcap() past whatever this function returns (see
    that function's own comment) - with the old asx300list.com/
    allordslist.com independent fallback sources gone entirely (see this
    module's header comment), one Wikipedia outage or page-structure
    change emptied all three together, with nothing to fall back to.

    Same last-known-good pattern as fetch_asx_listed_companies() (Commit
    2) and _rebuild_market_cap_ranking() (Commit D/F): a clean pass (no
    parse failure, every _check_asx200() check ok) backfills in ASX 100
    (unchanged from the original behavior) and becomes the new last-
    known-good; a failed pass falls back to the last-known-good snapshot
    instead - already backfilled, from when IT was saved - and alerts
    the owner once per new failure, not on every day it stays down."""
    df = _fetch_asx200_raw()
    checks = (_check_asx200(df) if df is not None
             else {"parse": {"ok": False, "detail": "fetch or parse failed entirely"}})
    all_ok = df is not None and all(c["ok"] for c in checks.values())

    if all_ok:
        merged = _asx_backfill_missing_subset_tickers(df, fetch_asx100(), "ASX 200", "ASX 100")
        source_health_store.record_success(_ASX200_SOURCE_NAME, merged.to_dict("records"), checks)
        return merged

    prior = source_health_store.get(_ASX200_SOURCE_NAME)
    was_already_stale = bool(prior and prior.get("stale"))
    reason = "fetch/parse failed" if df is None else "failed health check(s)"
    source_health_store.record_failure(_ASX200_SOURCE_NAME, checks, reason)
    if not was_already_stale:
        try:
            alert_engine.send_source_health_alert(_ASX200_SOURCE_NAME, checks, reason)
        except Exception:
            _log.exception("scanner_engine: source-health alert send failed for %s", _ASX200_SOURCE_NAME)
    _log.warning("scanner_engine: %s failed health check (%s) - serving last-known-good instead",
                _ASX200_SOURCE_NAME, reason)
    return source_health_store.last_good_dataframe(_ASX200_SOURCE_NAME)


_ASX_CSV_SOURCE_NAME = "ASX Listed Companies CSV"
_MARKET_CAP_RANKING_SOURCE_NAME = "ASX market-cap ranking (non-ASX 200)"

# Every source name that gets health-tracked via source_health_store -
# single list the Admin Dashboard's "Source health" table (app.py's
# page_admin_dashboard()) reads, so a future source added to this
# tracking only needs listing here, not in app.py too. Commit D (20 Sep
# 2026) added the market-cap ranking alongside the ASX CSV added in
# Commit 2; Commit G (20 Sep 2026) adds the live ASX 200 Wikipedia
# scrape - Wikipedia was a single point of failure for THREE universes
# (ASX 200 directly, plus ASX 300/All Ordinaries which both derive their
# tail from it) with nothing to fall back to.
TRACKED_HEALTH_SOURCES = [_ASX200_SOURCE_NAME, _ASX_CSV_SOURCE_NAME, _MARKET_CAP_RANKING_SOURCE_NAME]

# +/-15% of last-known-good's own row count - wide enough that ASX's
# normal daily churn (new listings, delistings, corporate actions) never
# trips it, narrow enough to catch the two failure shapes the task's own
# "Why" names: a collapse (~2,500 -> a dozen rows) or an HTML error page
# that happens to parse as some other, much smaller, valid-shaped table.
_ASX_CSV_DRIFT_BAND = 0.15

# Tickers known to have been listed AFTER the old asx300list.com/
# allordslist.com sources' own frozen date (28 April 2021) - exactly the
# fact that exposed the freeze in the first place (see fetch_asx300()'s
# own comment). Present in a genuinely current CSV; absent from anything
# still stuck on or before that date. Commit G (20 Sep 2026): widened
# from a single ticker (GQG.AX) to five, spanning 2021-2025 listing
# dates, and the check below now passes if ANY of them are present, not
# all - a single hardcoded canary means an acquisition, delisting, or
# rename of that ONE company (GQG itself, say) would mark this source
# permanently stale and alert forever, for a reason that has nothing to
# do with whether the CSV is actually current. All five verified live
# via web search as still ASX-listed as of 20 Sep 2026 (one candidate,
# Arcadium Lithium/LTM.AX, was deliberately excluded after search
# confirmed Rio Tinto's acquisition delisted it in March 2025 - exactly
# the failure mode a single-ticker canary is vulnerable to).
_ASX_CSV_CANARY_TICKERS = ["GQG.AX", "GGP.AX", "RDX.AX", "NEM.AX", "ACL.AX"]

# Commit I (21 Sep 2026): a SECOND, independent canary - the check class
# that would have caught the failure mode _ASX_CSV_CANARY_TICKERS above
# doesn't. That one only tests PRESENCE of names known to have existed
# since 2021-2025; a source frozen any time AFTER all five of those
# listing dates still carries every one of them and passes it cleanly,
# even though its own content can be months stale RIGHT NOW - exactly
# what was found live: ASXListedCompanies.csv's header carried TODAY's
# date while its rows still listed INCITEC PIVOT as IPL (renamed Dyno
# Nobel/DNL), BLOCK INC. as SQ2, and SEVEN GROUP HOLDINGS as SVW
# (renamed SGH) - all three 2025 renames. A header date proves nothing;
# this checks the ROWS two ways at once:
#   - MUST be present: a code that only exists post-rename (DNL, L1G -
#     L1 Group, another 2025 listing-identity change) - absent from
#     ANY snapshot older than these renames, present in a genuinely
#     current one.
#   - MUST be absent: the OLD code each of those same renames retired
#     (IPL, SQ2, SVW) - a source that still carries one of these is
#     provably not current, whatever its header says.
# Either direction failing on its own is enough to call the source
# stale - a source could pass the "present" half by coincidence (it
# happens to have added DNL/L1G as new rows without ever processing the
# renames that retired IPL/SQ2/SVW, e.g. an append-only feed) while
# still failing the "absent" half, and the reverse is just as possible.
# Both together, verified independently, is what makes this the freshness
# canary the OR-based one above cannot be - see _check_asx_listed_
# companies()'s own "freshness_canary" check.
_ASX_CSV_FRESHNESS_CANARY_PRESENT = ["DNL.AX", "L1G.AX"]
_ASX_CSV_FRESHNESS_CANARY_ABSENT = ["IPL.AX", "SQ2.AX", "SVW.AX"]


def _fetch_asx_listed_companies_raw():
    """Fetch + parse only - no health check, no last-known-good
    fallback (see fetch_asx_listed_companies() below, the public
    wrapper every other function in this module actually calls).

    Every ASX-listed company - NOT an index, no membership tiering at
    all, just the full listed-company register (~2,500 rows). This is
    the replacement source for everything past Wikipedia's live ASX
    200 - see fetch_asx300()/fetch_allords() below for why asx300list.
    com/allordslist.com (a frozen 28 April 2021 snapshot, confirmed
    live 20 Sep 2026) are gone.

    Commit I (21 Sep 2026): the FIRST replacement for those
    (ASX_LISTED_COMPANIES_CSV_URL's old target, www.asx.com.au/asx/
    research/ASXListedCompanies.csv) turned out to be the exact same
    failure mode one level up - see that constant's own comment for the
    full finding (a header stamped with today's date, rows frozen
    months behind: still IPL/SQ2/SVW, never once DNL/SGH). Now points
    at the file behind asx.com.au's own company directory page instead
    (markitdigital - unofficial, ASX doesn't document it as a public
    API, so every check below still gates it exactly as before). Same
    "Company name"/"ASX code"/"GICS industry group" fields this
    function has always read (column NAMES match - see the constant's
    own comment for the exact header this source ships), just a
    different vendor serving them; also carries "Listing date"/"Market
    Cap" columns this function doesn't read yet - see _check_asx_
    listed_companies()'s new freshness_canary check (which DOES use
    company identity, not these two) and the market-cap comparison
    Commit I's own report covers separately (deliberately NOT wired
    into _rebuild_market_cap_ranking() this commit - report only).

    Header row located by CONTENT (matching on "asx code" AND "company
    name" appearing together, not a fixed skiprows count or a strict
    column order), same defensive discipline as before - a leading
    title/date line, or the two columns swapping order, can't silently
    break this. Sanity floor of 1,000 rows (the real register is
    ~2,500) so a truncated download or an HTML error page returned in
    place of the file can't be mistaken for the real thing. Fails open
    to None on any error, same convention as every other fetcher in
    this module."""
    try:
        text = _get(ASX_LISTED_COMPANIES_CSV_URL)
    except Exception:
        return None
    lines = text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines)
         if "asx code" in line.strip().lower() and "company name" in line.strip().lower()),
        None,
    )
    if header_idx is None:
        return None
    try:
        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
    except Exception:
        return None

    company_col = _find_column(df.columns, ["company name", "company"])
    ticker_col = _find_column(df.columns, ["asx code", "code"])
    sector_col = _find_column(df.columns, ["gics industry group", "gics", "industry"])
    if company_col is None or ticker_col is None:
        return None

    cols = [ticker_col, company_col] + ([sector_col] if sector_col else [])
    out = df[cols].copy()
    out.columns = ["Ticker", "Company"] + (["Sector"] if sector_col else [])
    out = out.dropna(subset=["Ticker"])
    if len(out) < 1000:
        return None

    out["Ticker"] = out["Ticker"].apply(_normalize_asx_ticker)
    if "Sector" in out.columns:
        out["Sector"] = out["Sector"].astype(str).str.strip()
    else:
        out["Sector"] = None
    return out[["Ticker", "Company", "Sector"]]


def _looks_delisted(ticker, trading_days=5):
    """True only when there is POSITIVE evidence `ticker` has genuinely
    stopped trading - False in every other case, INCLUDING a yfinance
    lookup that itself fails. Used by _check_asx_listed_companies()'s
    cross_source check to tell apart the two different reasons a live
    ASX 200 ticker can be missing from the listed-companies source:
    Wikipedia's own ASX 200 page still listing a name the market has
    actually stopped trading (a takeover delisting Wikipedia hasn't
    caught up with yet - three of Commit I's own 8 missing tickers,
    IFL/QUB/NSR, are suspected takeover delistings, not source gaps),
    versus the source genuinely missing a ticker that's still trading
    today (a real gap - the failure this whole check exists to catch).

    Two failure shapes this specifically defends against, both found
    against the SAME live evidence (a 20 Sep 2026 DB backup) while this
    function was being written, not hypothetically:

    - GHOST PRICES: yfinance keeps serving the LAST real print for some
      delisted ASX names forever, as if the market were still quoting
      it - QUB.AX's backup shows an exact 5.11 close on every single
      day from 2026-08-20 to 2026-09-17, a dead giveaway once you see
      it (a real quote moves) but indistinguishable from genuine
      trading to a check that only asks "did history() return rows at
      all". A period="5d" call against a ghost-priced ticker returns 5
      rows and would have this function say "still trading" - exactly
      backwards. Fixed by requiring actual EVIDENCE of trading, not
      just a non-empty response: at least two DISTINCT closes in the
      window, or non-zero volume on at least one of the trading days.
      Neither is present in a flat, zero-volume replay of the same
      close.
    - LOOKUP FAILURE DIRECTION: a yfinance call that raises or times
      out tells you NOTHING about whether the ticker is still trading -
      it is not evidence of delisting. Returning True here on an
      exception (as an earlier draft of this function did) would let a
      yfinance OUTAGE masquerade as proof every missing ticker had been
      delisted, silently waving a genuinely stale source through
      cross_source with a clean bill of health it never earned. This
      function therefore fails CLOSED (returns False, "not confirmed
      delisted") on any lookup problem of its own - the caller then
      treats an unconfirmed ticker as still trading, which is the
      direction that BLAMES the source rather than excusing it, exactly
      the fail-safe direction a health check needs."""
    try:
        hist = yf.Ticker(ticker).history(period=f"{trading_days}d")
    except Exception:
        return False
    if hist is None or hist.empty:
        return True
    closes = hist["Close"].dropna() if "Close" in hist.columns else None
    volumes = hist["Volume"].dropna() if "Volume" in hist.columns else None
    has_distinct_closes = closes is not None and closes.nunique() >= 2
    has_volume = volumes is not None and bool((volumes > 0).any())
    return not (has_distinct_closes or has_volume)


def _check_asx_listed_companies(df, df200):
    """Five checks against a freshly-parsed _fetch_asx_listed_companies_
    raw() frame - a row-count floor alone already proved insufficient
    (asx300list.com/allordslist.com both passed one for five years while
    frozen; ASXListedCompanies.csv then passed one too, header stamped
    with today's date, rows months stale - see ASX_LISTED_COMPANIES_
    CSV_URL's own comment). Returns {check_name: {"ok": bool, "detail":
    str}}; never raises.

    - cross_source: every ticker in the LIVE Wikipedia ASX 200 must
      appear in the source, UNLESS yfinance confirms it hasn't actually
      traded in 5 trading days (Commit I, 21 Sep 2026: the ORIGINAL
      version of this check blamed the wrong side whenever Wikipedia's
      own ASX 200 page was the stale one - e.g. still listing a takeover
      delisting - which would otherwise mark a perfectly current source
      as stale forever, for a gap that was never its own). Fails on
      every missing ticker _looks_delisted() does NOT confirm as
      delisted - i.e. "still trading" is the default whenever that
      confirmation isn't there (a real gap, or an unconfirmed lookup
      of its own - see _looks_delisted()'s own docstring for why a
      yfinance failure must count as "still trading", not "delisted").
    - wikipedia_delistings: informational only, never gates (same
      "never gates accept/reject" convention _rebuild_market_cap_
      ranking()'s own "age" check already uses) - names the missing
      tickers _looks_delisted() DID confirm, so they're still visible
      on the Admin Dashboard's Source health panel rather than silently
      dropped from view.
    - drift: row count within _ASX_CSV_DRIFT_BAND of last-known-good.
    - canary: ANY of five tickers known to have been listed after the
      old (asx300list.com-era) sources' frozen date is present
      (_ASX_CSV_CANARY_TICKERS) - OR, not AND, so one of the five being
      acquired/delisted/renamed can't permanently fail this check on
      its own (see that constant's own comment).
    - freshness_canary (Commit I): the check class that would have
      caught THIS source's own failure - a MUST-be-present pair (DNL,
      L1G - identities that only exist after 2025's renames) AND a
      MUST-be-absent trio (IPL, SQ2, SVW - the identities those renames
      retired), both required - see _ASX_CSV_FRESHNESS_CANARY_PRESENT/
      _ABSENT's own comment for why this, unlike `canary` above, is
      immune to a merely-newer-than-2021 freeze."""
    checks = {}

    if df200 is not None and not df200.empty:
        missing_200 = sorted(set(df200["Ticker"]) - set(df["Ticker"]))
        # "still trading" is the DEFAULT for a missing ticker - only one
        # confirmed delisted by _looks_delisted() moves to the other
        # bucket. This is deliberate, not an oversight: it's what makes
        # an unconfirmed lookup (a yfinance error) land on the side that
        # blames the source, per that function's own docstring.
        wikipedia_stale = [t for t in missing_200 if _looks_delisted(t)]
        still_trading = [t for t in missing_200 if t not in wikipedia_stale]
        checks["cross_source"] = {
            "ok": not still_trading,
            "detail": (
                "all live ASX 200 tickers present" if not missing_200 else
                "all missing ASX 200 ticker(s) look Wikipedia-stale (not priced by "
                "yfinance in 5 trading days), not a source gap - see wikipedia_delistings"
                if not still_trading else
                f"{len(still_trading)} ASX 200 ticker(s) missing from the source while "
                f"still trading: " + ", ".join(still_trading[:10])
                + (", ..." if len(still_trading) > 10 else "")
            ),
        }
        checks["wikipedia_delistings"] = {
            "ok": True,
            "detail": (
                "none" if not wikipedia_stale else
                f"{len(wikipedia_stale)} ASX 200 ticker(s) missing from the source AND "
                f"not priced by yfinance in 5 trading days - likely Wikipedia-stale "
                f"delistings, not this source's own problem: " + ", ".join(wikipedia_stale)
            ),
        }
    else:
        checks["cross_source"] = {"ok": True, "detail": "skipped - live ASX 200 itself unavailable"}
        checks["wikipedia_delistings"] = {"ok": True, "detail": "skipped - live ASX 200 itself unavailable"}

    prior = source_health_store.get(_ASX_CSV_SOURCE_NAME)
    prior_count = (prior or {}).get("last_good_row_count")
    if prior_count:
        lo, hi = prior_count * (1 - _ASX_CSV_DRIFT_BAND), prior_count * (1 + _ASX_CSV_DRIFT_BAND)
        ok = lo <= len(df) <= hi
        checks["drift"] = {
            "ok": ok,
            "detail": f"{len(df)} row(s) vs last-known-good {prior_count} "
                     f"(expected {int(lo)}-{int(hi)})",
        }
    else:
        checks["drift"] = {"ok": True, "detail": "skipped - no last-known-good on record yet"}

    # Commit G: passes if ANY canary ticker is present, not all - see
    # _ASX_CSV_CANARY_TICKERS' own comment for why this is OR, not AND.
    have = set(df["Ticker"])
    present_canary = [t for t in _ASX_CSV_CANARY_TICKERS if t in have]
    checks["canary"] = {
        "ok": bool(present_canary),
        "detail": (f"present: {', '.join(present_canary)}" if present_canary
                  else f"none present (checked: {', '.join(_ASX_CSV_CANARY_TICKERS)})"),
    }

    # Commit I: BOTH halves required - see _ASX_CSV_FRESHNESS_CANARY_
    # PRESENT/_ABSENT's own comment for why this is AND, not OR, unlike
    # `canary` above.
    missing_present = [t for t in _ASX_CSV_FRESHNESS_CANARY_PRESENT if t not in have]
    still_present_absent = [t for t in _ASX_CSV_FRESHNESS_CANARY_ABSENT if t in have]
    checks["freshness_canary"] = {
        "ok": not missing_present and not still_present_absent,
        "detail": (
            "current: all of " + ", ".join(_ASX_CSV_FRESHNESS_CANARY_PRESENT)
            + " present, none of " + ", ".join(_ASX_CSV_FRESHNESS_CANARY_ABSENT) + " present"
            if not missing_present and not still_present_absent else
            "; ".join(filter(None, [
                f"missing post-rename code(s): {', '.join(missing_present)}" if missing_present else "",
                f"still carries retired code(s): {', '.join(still_present_absent)}" if still_present_absent else "",
            ]))
        ),
    }

    return checks


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx_listed_companies():
    """Public wrapper around _fetch_asx_listed_companies_raw() - every
    OTHER function in this module calls this, never the raw fetcher
    directly. Runs _check_asx_listed_companies() against the fresh
    parse before trusting it:

    - Parse failed, or every check passed: fresh data flows through as
      normal. A clean pass ALSO becomes the new source_health_store
      last-known-good and clears any stale flag - a source that was
      down and has recovered goes back to reporting healthy with no
      manual reset needed.
    - Parse failed, or any check failed: does NOT return the bad data
      (or None, the way a plain row-count guard used to silently
      degrade) - falls back to source_health_store's last-known-good
      snapshot instead, and marks the source stale. Alerts the owner
      (alert_engine.send_source_health_alert) exactly once per NEW
      failure - a source already marked stale from a prior check
      doesn't re-alert every single day it stays down, same
      alert-fatigue reasoning as a real price alert's own cooldown."""
    df = _fetch_asx_listed_companies_raw()
    checks = (_check_asx_listed_companies(df, fetch_asx200()) if df is not None
             else {"parse": {"ok": False, "detail": "fetch or parse failed entirely"}})
    all_ok = df is not None and all(c["ok"] for c in checks.values())

    if all_ok:
        source_health_store.record_success(_ASX_CSV_SOURCE_NAME, df.to_dict("records"), checks)
        return df

    prior = source_health_store.get(_ASX_CSV_SOURCE_NAME)
    was_already_stale = bool(prior and prior.get("stale"))
    reason = "fetch/parse failed" if df is None else "failed health check(s)"
    source_health_store.record_failure(_ASX_CSV_SOURCE_NAME, checks, reason)
    if not was_already_stale:
        try:
            alert_engine.send_source_health_alert(_ASX_CSV_SOURCE_NAME, checks, reason)
        except Exception:
            _log.exception("scanner_engine: source-health alert send failed for %s", _ASX_CSV_SOURCE_NAME)
    _log.warning("scanner_engine: %s failed health check (%s) - serving last-known-good instead",
                _ASX_CSV_SOURCE_NAME, reason)
    return source_health_store.last_good_dataframe(_ASX_CSV_SOURCE_NAME)


# Commit D (20 Sep 2026): the market-cap ranking used to run its whole
# ~2,300-lookup pricing pass inline, the first time any caller (a web
# visitor picking ASX 300 on the Scanner page included) asked for it
# after any cold start - st.cache_data's cache is per-process/in-memory,
# so a Railway redeploy re-arms this every time. And a throttled pass
# shipped a silently partial ranking: candidates[candidates["_cap"] > 0]
# can't tell "this company genuinely has no market cap" from "yfinance
# throttled me" - Commit 2's health checks validate the CSV, not what
# gets built from it. Fixed the same way as Commit 2 fixed the CSV: all
# pricing now happens ONLY in the nightly job
# (_rebuild_market_cap_ranking(), called from nightly_scan.py before the
# per-universe scan loop) and is persisted via source_health_store under
# _MARKET_CAP_RANKING_SOURCE_NAME; _asx_non200_by_marketcap() below - the
# function every web request still goes through, via fetch_asx300()/
# fetch_allords() - is now a pure read of that file. See
# _rebuild_market_cap_ranking()'s own docstring for the incremental
# repricing / failure-handling design.

# A company ranked ~1,800th by market cap is not entering the ASX 300
# tail (ranks ~201-300) overnight - only this band of ranks just outside
# today's cut gets re-priced every night, on top of any ticker newly
# added to the CSV since the last run. Wide enough either side of the
# ~201-300/~301-500 cuts fetch_asx300()/fetch_allords() actually take to
# absorb realistic night-to-night movement.
_MARKET_CAP_BOUNDARY_LO = 150
_MARKET_CAP_BOUNDARY_HI = 700

# Reject the whole nightly ranking (keep last-known-good) if fewer than
# this fraction of the tickers actually due for pricing this run came
# back with a real answer, even after one retry - publishing a ranking
# built from a throttled minority would silently reorder or drop names
# that never got a fresh price. Same generous-but-real-signal reasoning
# as _ASX_CSV_DRIFT_BAND above.
_MARKET_CAP_PRICED_RATIO_BAND = 0.90

# Same +/-15% row-count drift guard as _ASX_CSV_DRIFT_BAND, applied to
# the published ranking's own row count vs its last-known-good.
_MARKET_CAP_ROW_COUNT_DRIFT_BAND = 0.15

# Purely informational (see _rebuild_market_cap_ranking()'s own comment
# on why this never gates accept/reject) - how many days old the
# ranking being served is allowed to get before it's worth a look, given
# the nightly cadence is meant to refresh it every single day.
_MARKET_CAP_AGE_BAND_DAYS = 4


def _price_tickers(tickers, log=None):
    """Prices `tickers` via market_cap_engine.get_market_cap_checked(),
    threaded at max_workers=8 (same polite-to-yfinance cap this module's
    other bulk lookups use), with one retry pass over whatever failed
    the first time - a single throttled response is often transient.

    Returns (caps: {ticker: market_cap}, failed: set-of-tickers) -
    `failed` holds only tickers whose lookup itself came back looking
    throttled/empty on BOTH attempts (market_cap_engine.get_market_cap_
    checked()'s ok=False) - a real, populated response reporting a
    genuine zero market cap is NOT a failure and lands in `caps` like
    any other result, exactly the distinction Commit D exists to make."""
    _log_fn = log or (lambda msg: _log.info(msg))

    def _lookup(ticker):
        cap, ok = market_cap_engine.get_market_cap_checked(ticker)
        return ticker, cap, ok

    def _price_pass(ticker_list):
        pass_caps, pass_failed = {}, set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for ticker, cap, ok in pool.map(_lookup, ticker_list):
                if ok:
                    pass_caps[ticker] = cap
                else:
                    pass_failed.add(ticker)
        return pass_caps, pass_failed

    start = time.time()
    caps, failed = _price_pass(tickers)
    if failed:
        retry_caps, retry_failed = _price_pass(sorted(failed))
        caps.update(retry_caps)
        failed = retry_failed
    _log_fn(
        f"[scanner_engine] market-cap ranking: priced {len(tickers)} "
        f"ticker(s) in {time.time() - start:.1f}s "
        f"({len(tickers) - len(failed)} ok, {len(failed)} failed after retry)"
    )
    return caps, failed


def _rebuild_market_cap_ranking(force_full=False, log=None):
    """The ONLY place the market-cap ranking is priced - called once a
    night from nightly_scan.py, before the per-universe scan loop that
    depends on it. Never call this from a web request path.

    Prices only: (a) candidates new to fetch_asx_listed_companies()
    since the last run (never priced before, for any reason - new
    listing, or newly dropped out of the live ASX 200), and (b) whatever
    is currently ranked _MARKET_CAP_BOUNDARY_LO.._MARKET_CAP_BOUNDARY_HI
    - the only band close enough to fetch_asx300()/fetch_allords()'s own
    cuts for a night-to-night market move to plausibly matter.
    force_full=True (or no persisted ranking yet at all) prices every
    candidate instead. Every candidate NOT re-priced this run keeps its
    previous market cap - carried forward, never re-derived or dropped.

    Publishes through source_health_store exactly like fetch_asx_listed_
    companies() does for the CSV: a clean pass (priced ratio and row-
    count both within band) becomes the new last-known-good, clearing
    any stale flag; a failed pass keeps serving whatever was already
    there and alerts the owner once per new failure
    (alert_engine.send_source_health_alert), never on every single day a
    failure stays unresolved.

    Bootstrap exemption (Commit F): when there's no last-known-good yet
    at all, priced_ratio can't be allowed to reject the pass - rejecting
    would mean nothing is ever published, so the NEXT run is cold again
    too (same ~2,300-candidate full pass, same throttling exposure) and
    can reject itself forever. On a first-ever pass the ranking publishes
    regardless of priced_ratio - but a pass that only got there via this
    exemption is published AND immediately marked stale (one alert, one
    consecutive_failures tick), so it's visibly not-ok on the Source
    health panel rather than silently reported healthy. A later run that
    fills in the gaps left by this one is compared against that stale
    baseline with the row-count drift check itself skipped (growth off
    a known-partial baseline is expected, not drift) - so the very next
    clean pass clears stale on its own, the normal way (a bare record_
    success() with nothing layered after it), the moment priced_ratio
    clears the band for real.

    A ranking-age check is recorded on every run too, but is NEVER part
    of the accept/reject decision - see the comment right above where
    it's computed for why gating on it could permanently wedge the
    ranking (a good fresh pass rejected because the OLD data happened to
    be stale would mean last-known-good never advances, so it stays
    stale forever).

    Returns the DataFrame that ends up being served (freshly published,
    or the retained last-known-good on a rejected/skipped run) - None
    only if the CSV/live ASX 200 themselves are unavailable, exactly
    like the old inline version's contract."""
    _log_fn = log or (lambda msg: _log.info(msg))
    listed = fetch_asx_listed_companies()
    df200 = fetch_asx200()
    if listed is None or df200 is None:
        _log_fn("[scanner_engine] market-cap ranking: CSV or live ASX 200 unavailable, skipping rebuild")
        return None

    candidates = listed[~listed["Ticker"].isin(set(df200["Ticker"]))].copy()
    if candidates.empty:
        return candidates
    candidate_tickers = set(candidates["Ticker"])
    company_by_ticker = dict(zip(candidates["Ticker"], candidates["Company"]))
    sector_by_ticker = dict(zip(candidates["Ticker"], candidates["Sector"]))

    prior = source_health_store.get(_MARKET_CAP_RANKING_SOURCE_NAME)
    prior_rows = (prior or {}).get("last_good_rows") or []
    prior_cap_by_ticker = {r["Ticker"]: r.get("MarketCap", 0) for r in prior_rows}
    prior_rank_by_ticker = {r["Ticker"]: i + 1 for i, r in enumerate(prior_rows)}

    if force_full or not prior_rows:
        to_price = sorted(candidate_tickers)
        _log_fn(f"[scanner_engine] market-cap ranking: full pass, {len(to_price)} candidate(s)")
    else:
        new_tickers = candidate_tickers - set(prior_cap_by_ticker)
        boundary = {
            t for t, rank in prior_rank_by_ticker.items()
            if _MARKET_CAP_BOUNDARY_LO <= rank <= _MARKET_CAP_BOUNDARY_HI and t in candidate_tickers
        }
        to_price = sorted(new_tickers | boundary)
        _log_fn(
            f"[scanner_engine] market-cap ranking: incremental pass, "
            f"{len(to_price)} of {len(candidate_tickers)} candidate(s) due "
            f"({len(new_tickers)} new, {len(boundary)} in boundary band "
            f"{_MARKET_CAP_BOUNDARY_LO}-{_MARKET_CAP_BOUNDARY_HI})"
        )

    caps, failed = _price_tickers(to_price, log=_log_fn) if to_price else ({}, set())

    # Every candidate's cap: freshly priced where it was due, carried
    # over from last-known-good otherwise. A candidate that was due
    # (new, never priced before) and still failed after retry is
    # excluded entirely - there is no prior cap to fall back to and
    # nothing to rank it by, same as the old inline version dropped an
    # unpriced ticker.
    final_caps = {}
    for t in candidate_tickers:
        if t in caps:
            final_caps[t] = caps[t]
        elif t in prior_cap_by_ticker:
            final_caps[t] = prior_cap_by_ticker[t]
    # A genuine zero (ok=True, cap=0) carries no ranking signal - same
    # "nothing to rank it by" treatment the old version gave an unpriced
    # ticker, but arrived at without conflating the two.
    final_caps = {t: cap for t, cap in final_caps.items() if cap and cap > 0}

    priced_ok_count = len(to_price) - len(failed)
    priced_ratio = (priced_ok_count / len(to_price)) if to_price else 1.0
    row_count = len(final_caps)
    prior_row_count = len(prior_rows)

    checks = {
        "priced_ratio": {
            "ok": priced_ratio >= _MARKET_CAP_PRICED_RATIO_BAND,
            "detail": (
                f"{priced_ok_count}/{len(to_price)} due lookup(s) ok "
                f"({priced_ratio:.0%}, band >= {_MARKET_CAP_PRICED_RATIO_BAND:.0%})"
                if to_price else "nothing was due for pricing this run"
            ),
        },
    }
    # Commit F: also skip the drift comparison when the last-known-good
    # being compared against was ITSELF a bootstrap-partial publish
    # (prior.stale) - that baseline is already known-incomplete (some
    # candidates missing because they failed to price with no fallback
    # available yet), so a later run successfully pricing those same
    # candidates for the first time is expected, wanted growth, not
    # drift. Without this, a bootstrap-partial's own recovery run would
    # get rejected by this check for the "wrong" reason (row count
    # legitimately going UP as gaps fill in), permanently keeping the
    # source stale even once pricing recovers.
    prior_was_stale = bool(prior and prior.get("stale"))
    if prior_row_count and not prior_was_stale:
        lo = prior_row_count * (1 - _MARKET_CAP_ROW_COUNT_DRIFT_BAND)
        hi = prior_row_count * (1 + _MARKET_CAP_ROW_COUNT_DRIFT_BAND)
        checks["row_count"] = {
            "ok": lo <= row_count <= hi,
            "detail": f"{row_count} row(s) vs last-known-good {prior_row_count} (expected {int(lo)}-{int(hi)})",
        }
    elif prior_was_stale:
        checks["row_count"] = {
            "ok": True,
            "detail": f"skipped - last-known-good ({prior_row_count} rows) was itself a stale/partial publish, growth is expected",
        }
    else:
        checks["row_count"] = {"ok": True, "detail": "skipped - no last-known-good on record yet"}

    # Informational only - see this function's own docstring for why age
    # never gates accept/reject.
    prior_good_at = (prior or {}).get("last_good_at")
    if prior_good_at:
        try:
            age_days = (
                datetime.now(timezone.utc) - datetime.fromisoformat(prior_good_at)
            ).total_seconds() / 86400
            checks["age"] = {
                "ok": age_days <= _MARKET_CAP_AGE_BAND_DAYS,
                "detail": f"last-known-good was {age_days:.1f} day(s) old going into this run (band <= {_MARKET_CAP_AGE_BAND_DAYS})",
            }
        except ValueError:
            checks["age"] = {"ok": True, "detail": "skipped - unreadable last-known-good timestamp"}
    else:
        checks["age"] = {"ok": True, "detail": "skipped - no last-known-good on record yet"}

    all_ok = checks["priced_ratio"]["ok"] and checks["row_count"]["ok"]

    # Commit F (bootstrap exemption): row_count already skips itself (ok
    # True) when there's no last-known-good to compare against; priced_
    # ratio had no equivalent, so a cold first pass over ~2,300 tickers -
    # exactly where yfinance throttling is most likely - could reject
    # itself, leaving nothing published. The next night starts cold
    # again (still no baseline), the cheap incremental path can never
    # kick in, and the ranking is stuck rejecting itself forever. Once a
    # baseline exists, priced_ratio still gates normally below - this
    # only ever fires on the very first publish.
    is_bootstrap = not prior_row_count
    publish = all_ok or is_bootstrap
    # A publish that only happened because of the bootstrap exemption,
    # despite priced_ratio actually failing - published (so there's
    # something on disk to serve at all) but still visibly not-ok, not
    # silently folded into a clean pass. Handled below by publishing
    # (record_success, since that's the only source_health_store
    # function that writes last_good_rows at all) and then immediately
    # marking the result stale (record_failure, which never touches
    # last_good_rows) - decoupling "was something published" from "is
    # the source reporting healthy", which all_ok alone can't express.
    bootstrap_partial = is_bootstrap and not checks["priced_ratio"]["ok"]

    ranked_df = pd.DataFrame(
        {
            "Ticker": t,
            "Company": company_by_ticker.get(t),
            "Sector": sector_by_ticker.get(t),
            "MarketCap": final_caps[t],
        }
        for t in final_caps
    )
    if not ranked_df.empty:
        ranked_df = ranked_df.sort_values("MarketCap", ascending=False).reset_index(drop=True)

    if publish:
        source_health_store.record_success(
            _MARKET_CAP_RANKING_SOURCE_NAME, ranked_df.to_dict("records"), checks
        )
        if bootstrap_partial:
            # Composes the two existing source_health_store primitives
            # rather than adding a third write path: record_success()
            # just above already persisted the partial ranking as last-
            # known-good (last_good_at/last_good_row_count/last_good_
            # rows). record_failure() never touches those three fields -
            # only stale/last_check_at/last_checks/last_failure_reason/
            # consecutive_failures - so calling it immediately after
            # layers "stale, one failure recorded" on top of the rows
            # that were just published, without touching source_health_
            # store.py at all. This is what makes the Source health
            # panel show 🔴 Stale for a bootstrap-partial publish (not
            # just a failing priced_ratio row inside an otherwise-green
            # check), and what "next incremental run clears stale" (see
            # this function's own docstring) actually refers to: the
            # next run's own clean record_success() call, with no
            # trailing record_failure(), is what clears it.
            reason = "bootstrap publish below priced-ratio band - no prior baseline to fall back to"
            source_health_store.record_failure(_MARKET_CAP_RANKING_SOURCE_NAME, checks, reason)
            _log_fn(
                f"[scanner_engine] market-cap ranking: bootstrap publish is "
                f"PARTIAL ({checks['priced_ratio']['detail']}) - published "
                f"anyway since no last-known-good existed to reject back to, "
                f"marked stale"
            )
            try:
                alert_engine.send_source_health_alert(_MARKET_CAP_RANKING_SOURCE_NAME, checks, reason)
            except Exception:
                _log.exception(
                    "scanner_engine: source-health alert send failed for %s",
                    _MARKET_CAP_RANKING_SOURCE_NAME,
                )
        else:
            _log_fn(f"[scanner_engine] market-cap ranking: published, {len(ranked_df)} row(s)")
        return ranked_df

    was_already_stale = bool(prior and prior.get("stale"))
    reason = (
        "priced ratio below band" if not checks["priced_ratio"]["ok"]
        else "row-count drift vs last-known-good"
    )
    source_health_store.record_failure(_MARKET_CAP_RANKING_SOURCE_NAME, checks, reason)
    if not was_already_stale:
        try:
            alert_engine.send_source_health_alert(_MARKET_CAP_RANKING_SOURCE_NAME, checks, reason)
        except Exception:
            _log.exception(
                "scanner_engine: source-health alert send failed for %s",
                _MARKET_CAP_RANKING_SOURCE_NAME,
            )
    _log_fn(
        f"[scanner_engine] market-cap ranking: rejected ({reason}) - "
        f"serving last-known-good instead"
    )
    return source_health_store.last_good_dataframe(_MARKET_CAP_RANKING_SOURCE_NAME)


def _asx_non200_by_marketcap():
    """Read-only web-path accessor. Every fetch_asx_listed_companies()
    row NOT already in the live Wikipedia ASX 200, ranked by market cap
    descending - the shared ranking fetch_asx300()/fetch_allords() below
    both slice from.

    Commit D (20 Sep 2026): no longer prices anything itself - see
    _rebuild_market_cap_ranking() above, the nightly-only function that
    does, and this function's own module-level comment block for why. A
    plain file read via source_health_store, same convention as fetch_
    asx_listed_companies() reading the CSV's own last-known-good: no
    network call, no thread pool, nothing that can block a web request.

    Returns the last-known-good ranking (stale or not - a stale flag
    here means the LATEST nightly attempt was rejected, not that this
    data is unusable; it's still the best real ranking on file), or None
    if nothing has ever been published yet - callers already treat None
    as "show unavailable" rather than hanging or guessing."""
    return source_health_store.last_good_dataframe(_MARKET_CAP_RANKING_SOURCE_NAME)


# ~300/~500 total once added to the live 200 - matches the real indices'
# own approximate sizes closely enough for a derived stand-in; see
# fetch_asx300()/fetch_allords()'s own "label honestly" comments.
_ASX300_TAIL_SIZE = 100
_ALLORDS_TAIL_SIZE = 200


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx300():
    """
    Rebuilt (20 Sep 2026) as a nested slice of ONE ranking, not a
    separate scrape: Wikipedia's live ASX 200 (authoritative, unchanged)
    plus the next ~100 ASX-listed companies by market cap (see
    _asx_non200_by_marketcap() above). The old primary source,
    asx300list.com, turned out to be a frozen 28 April 2021 snapshot -
    verified live 20 Sep 2026: both GQG.AX (listed since 2021) and
    GGP.AX were missing from it - so it, its Wikipedia secondary
    fallback, and both URLs are gone entirely.

    DERIVED, not real S&P/ASX 300 index membership - a market-cap
    ranking approximation past the real, live ASX 200. Labelled as such
    everywhere this universe's source is surfaced (get_universe_pool()'s
    own label string below).

    Sector now comes straight from source for every row - Wikipedia's
    own GICS sector for the 200, the ASX's own GICS industry group (a
    finer-grained tier than "sector", but real and current) for the
    rest - no separate sector-merge pass needed any more; the ~100
    names that used to show Sector=None under the old source no longer
    do.

    _asx_backfill_missing_subset_tickers() is still called at the end as
    belt-and-braces: containment is now structural (this IS the live
    ASX 200 plus more, not a second independent fetch of it), so it
    should never actually do anything, but a free defensive check
    against the unexpected costs nothing - see
    verify_au_index_containment() for the same reasoning applied as an
    explicit regression guard.
    """
    df200 = fetch_asx200()
    if df200 is None:
        return None
    ranked = _asx_non200_by_marketcap()
    if ranked is None or ranked.empty:
        return None
    tail = ranked.head(_ASX300_TAIL_SIZE)[["Ticker", "Sector"]]
    df = pd.concat([df200[["Ticker", "Sector"]], tail], ignore_index=True)
    return _asx_backfill_missing_subset_tickers(df, df200, "ASX 300", "ASX 200")


# -----------------------------------------------------------------
# Fix 8a, AI fixes round 2 (2026-08-31): new universes. See the URL
# constants' own comments above for the live-network-verification
# caveat that applies to every fetcher below.
# -----------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_dow30():
    """Dow Jones Industrial Average's own Wikipedia article carries the
    30-component table directly (no separate "List of..." page exists
    for the Dow, unlike S&P 500/400/600) - columns confirmed during
    development: Company/Exchange/Symbol/Sector/Date added/Notes/Index
    weighting. max_rows=35 (on top of min_rows=25) narrows the match
    beyond just "has a Symbol column and enough rows", since this is a
    long article with other tables on it (historical components,
    sector breakdowns) that a looser match could mistake for the real
    one."""
    try:
        html = _get(DOW30_WIKI_URL)
    except Exception:
        return None
    return _parse_table(html, ["symbol"], ["sector", "gics sector"],
                         _normalize_us_ticker, min_rows=25, max_rows=35)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_dividend_aristocrats():
    """Part 34.4: S&P 500 Dividend Aristocrats' own Wikipedia article -
    "Ticker symbol / Company / Sector" table, same _parse_table() pattern
    as every other fetcher here. min_rows=55/max_rows=85 per the
    instruction, bracketing the real 69-row count confirmed live during
    development (see DIVIDEND_ARISTOCRATS_WIKI_URL's own comment) with
    real headroom either side for routine index reconstitution."""
    try:
        html = _get(DIVIDEND_ARISTOCRATS_WIKI_URL)
    except Exception:
        return None
    return _parse_table(html, ["ticker symbol", "ticker", "symbol"], ["sector", "gics sector"],
                         _normalize_us_ticker, min_rows=55, max_rows=85)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_sp400():
    try:
        html = _get(SP400_WIKI_URL)
    except Exception:
        return None
    return _parse_table(html, ["symbol", "ticker"], ["gics sector", "sector"], _normalize_us_ticker, min_rows=350)


def _sp1500_df():
    """S&P 1500 = S&P 500 + S&P 400 + S&P 600 (Composite index definition) -
    derived from the three already-fetched pools rather than a fourth
    scrape, per the round 2 instruction doc. None if none of the three
    source pools are available at all; otherwise the union of whichever
    ones are, de-duplicated by ticker (a name occasionally appears in
    more than one Wikipedia snapshot during index-reconstitution
    windows)."""
    parts = [df for df in (fetch_sp500(), fetch_sp400(), fetch_sp600()) if df is not None]
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True).drop_duplicates(subset="Ticker", keep="first")


def _russell3000_df():
    """Part 34.3: Russell 3000 = union of Russell 1000 + Russell 2000
    (the index's own definition) - exact same derived-union pattern as
    _sp1500_df() right above, just two parents instead of three. None
    only if NEITHER parent pool is available; otherwise the union of
    whichever one(s) are, de-duplicated by ticker (Russell reconstitution
    windows can briefly show a name in both iShares exports).

    Part 52.3: calls the two full _resolve_*() chains (live -> Wikipedia/
    disk-cache -> static, per each parent's own fallback order) instead
    of the old bare fetch_russell1000()/fetch_russell2000() - so Russell
    3000 "needs no change" in the sense the instruction means: its own
    derivation logic (union + de-dup) is untouched, it simply gets each
    parent's new resilience for free by asking for the resolved parent
    rather than only its live-only attempt. The label half of each
    resolver's return is discarded here; only get_universe_pool's own
    "Russell 3000" branch needs a label, and that one is unchanged."""
    parts = [df for df, _label in (_resolve_russell1000(), _resolve_russell2000()) if df is not None]
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True).drop_duplicates(subset="Ticker", keep="first")


def _r1k_cache_path():
    """Same reasoning as _r2k_cache_path() above, for Russell 1000."""
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, "russell1000_cache.csv")


def _r1k_from_disk_cache():
    try:
        df = pd.read_csv(_r1k_cache_path())
        if "Ticker" in df.columns and len(df) > 500:
            if "Sector" not in df.columns:
                df["Sector"] = None
            return df[["Ticker", "Sector"]]
    except Exception:
        pass
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_russell1000():
    """Best-effort LIVE attempt only - mirrors fetch_russell2000() exactly
    (same iShares CSV-export mechanism, same browser-like headers + one
    retry via _fetch_ishares_csv, same shared _clean_ishares_holdings_df
    cleaning). Part 52.1: no internal fallback anymore - see
    fetch_russell2000()'s own docstring for why (_resolve_russell1000()
    below owns the full Wikipedia/disk-cache chain)."""
    try:
        text = _fetch_ishares_csv(IWB_HOLDINGS_CSV_URL)
        raw = pd.read_csv(io.StringIO(text), skiprows=9, on_bad_lines="skip")
    except Exception:
        return None

    out = _clean_ishares_holdings_df(raw)
    if out is None:
        return None
    try:
        out.to_csv(_r1k_cache_path(), index=False)
    except OSError:
        pass
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_russell1000_wikipedia():
    """Part 52.1 fallback #2 for Russell 1000, tried after the live iShares
    IWB export fails: Wikipedia's dedicated "List of Russell 1000
    companies" article (RUSSELL1000_WIKI_URL - see that constant's own
    comment for why it's NOT the "Russell 1000 Index" article, and for
    the live-verification note). Same _parse_table() convention as every
    other Wikipedia fetcher in this module: a "Company / Symbol / GICS
    Sector / GICS Sub-Industry" table, min_rows=900 leaving headroom
    below the real ~1,000-row count for ordinary reconstitution drift
    while still rejecting a wrong/empty table.

    Feeds the real GICS Sector column into sector_cache_store for free
    (Part 52.1) - the one thing this source has that the iShares CSV
    export doesn't. Also writes the shared disk cache on success (Part
    52.3), same as the live iShares path above - a Wikipedia-only
    success one night is still a real, persisted list for every later
    cold-start, not just an in-memory answer for this one render."""
    try:
        html = _get(RUSSELL1000_WIKI_URL)
    except Exception:
        return None
    df = _parse_table(html, ["symbol", "ticker"], ["gics sector", "sector"], _normalize_us_ticker, min_rows=900)
    if df is None:
        return None

    for _, row in df.iterrows():
        if row["Sector"]:
            sector_cache_store.learn(row["Ticker"], row["Sector"], source="wikipedia_r1000")

    try:
        df.to_csv(_r1k_cache_path(), index=False)
    except OSError:
        pass
    return df


def _resolve_russell1000():
    """(df, source_label) for Russell 1000, trying each source in order
    (Part 52.1): live iShares IWB CSV -> Wikipedia's "List of Russell
    1000 companies" -> yesterday's disk cache. The first two both persist
    to the disk cache on success (Part 52.3), so a single success on
    EITHER path, on any night, gives every later cold-start a real
    fallback before ever falling through to a stale cache. Shared by
    get_universe_pool's own "Russell 1000" branch (which needs the
    label) and _russell3000_df() (which only needs the df)."""
    df = fetch_russell1000()
    if df is not None:
        return df, "iShares IWB ETF holdings (live)"
    df = fetch_russell1000_wikipedia()
    if df is not None:
        return df, "Wikipedia Russell 1000 (live)"
    df = _r1k_from_disk_cache()
    if df is not None:
        return df, _disk_cache_label("Russell 1000", _r1k_cache_path())
    return None, "Web scrape unavailable"


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_allords():
    """All Ordinaries (~500 names, effectively "every ASX company big
    enough to be liquid"). Rebuilt (20 Sep 2026) the same way
    fetch_asx300() above is - a nested slice of the SAME
    _asx_non200_by_marketcap() ranking, this time taking the next ~200
    past fetch_asx300()'s own ~100 (so overall: live ASX 200, then ranks
    201-300 in fetch_asx300(), then ranks 301-500 here). The old
    allordslist.com source is gone for the same reason as
    asx300list.com - see fetch_asx300()'s own comment for the verified
    28 April 2021 snapshot / GQG.AX+GGP.AX finding.

    DERIVED, not real All Ordinaries index membership - see
    fetch_asx300()'s own "label honestly" note; same applies here, and
    get_universe_pool()'s label string below says so.

    Sector comes straight from fetch_asx300() (already source-sectored)
    for the shared rows, and the ASX CSV's own GICS industry group for
    the new ~200 - no separate sector-merge pass, same as fetch_asx300().

    _asx_backfill_missing_subset_tickers() against fetch_asx300() is
    kept as belt-and-braces - see fetch_asx300()'s own comment on why
    this should never actually fire now that containment is structural."""
    df300 = fetch_asx300()
    if df300 is None:
        return None
    ranked = _asx_non200_by_marketcap()
    if ranked is None or ranked.empty:
        return None
    already_in = set(df300["Ticker"])
    remaining = ranked[~ranked["Ticker"].isin(already_in)]
    tail = remaining.head(_ALLORDS_TAIL_SIZE)[["Ticker", "Sector"]]
    df = pd.concat([df300[["Ticker", "Sector"]], tail], ignore_index=True)
    return _asx_backfill_missing_subset_tickers(df, df300, "All Ordinaries", "ASX 300")


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx20():
    try:
        html = _get(ASX20_WIKI_URL)
    except Exception:
        return None
    return _parse_table(html, ["symbol", "code"], ["sector", "industry"],
                         _normalize_asx_ticker, min_rows=15, max_rows=25)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx50():
    try:
        html = _get(ASX50_WIKI_URL)
    except Exception:
        return None
    df = _parse_table(html, ["symbol", "code"], ["sector", "industry"],
                      _normalize_asx_ticker, min_rows=40, max_rows=55)
    return _asx_backfill_missing_subset_tickers(df, fetch_asx20(), "ASX 50", "ASX 20")


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx100():
    """No dedicated live constituent-table source could be confirmed for
    this one (see ASX100_WIKI_URL's comment) - tried anyway since it's
    free to attempt and harmless on failure; get_universe_pool() falls
    back to ASX 200 (live) if this returns None, exactly like ASX 300's
    own existing fallback chain.

    Index containment (20 Sep 2026): backfilled against fetch_asx50() -
    see _asx_backfill_missing_subset_tickers()'s own comment above."""
    try:
        html = _get(ASX100_WIKI_URL)
    except Exception:
        return None
    df = _parse_table(html, ["symbol", "code", "ticker"], ["sector", "industry"],
                      _normalize_asx_ticker, min_rows=80, max_rows=110)
    return _asx_backfill_missing_subset_tickers(df, fetch_asx50(), "ASX 100", "ASX 50")


# Smallest to largest - the standing nesting order this module's docstring
# describes (ASX 20 subset ASX 50 subset ASX 100 subset ASX 200 subset ASX
# 300 subset All Ordinaries). Named here as one list, not spread across
# five separate backfill call sites, so the regression guard below can't
# silently drift out of sync with which pair actually belongs together if
# another AU universe is ever added to the chain.
_AU_CONTAINMENT_CHAIN = [
    ("ASX 20", fetch_asx20),
    ("ASX 50", fetch_asx50),
    ("ASX 100", fetch_asx100),
    ("ASX 200", fetch_asx200),
    ("ASX 300", fetch_asx300),
    ("All Ordinaries", fetch_allords),
]


def verify_au_index_containment(log=None):
    """Index containment regression guard (20 Sep 2026): re-checks every
    adjacent pair in _AU_CONTAINMENT_CHAIN and reports any subset index
    still holding a ticker its parent doesn't - the exact GQG.AX-shaped
    bug the backfill in fetch_asx200()/fetch_asx300()/fetch_asx50()/
    fetch_asx100()/fetch_allords() above fixes. Should never fire given
    that backfill, but each of those functions unions in whatever ITS OWN
    subset fetch currently returns - a live source going stale, changing
    shape, or being swapped later could still reopen a gap here, which is
    exactly the silent-drift this guard exists to catch instead of
    absorbing quietly.

    Never raises - same fail-open convention as every fetcher in this
    module. Returns a list of (subset_name, superset_name, missing_
    tickers) violation tuples (empty = all clear); a caller that wants a
    hard failure (a CI check, say) should assert not
    verify_au_index_containment() itself rather than expecting this to
    raise on its own.

    `log`: an optional log(str) callable - nightly_scan.run_universe_
    scan()'s own convention (defaults to `print` there). Falls back to
    this module's own `_log.warning` when omitted, so this is equally
    callable from a plain script or the Python console."""
    _log_fn = log or (lambda msg: _log.warning(msg))
    frames = {name: fetch() for name, fetch in _AU_CONTAINMENT_CHAIN}
    violations = []
    chain_pairs = zip(_AU_CONTAINMENT_CHAIN, _AU_CONTAINMENT_CHAIN[1:])
    for (subset_name, _), (superset_name, _) in chain_pairs:
        subset_df, superset_df = frames[subset_name], frames[superset_name]
        if subset_df is None or superset_df is None:
            continue
        missing = sorted(set(subset_df["Ticker"]) - set(superset_df["Ticker"]))
        if missing:
            violations.append((subset_name, superset_name, missing))
            _log_fn(
                f"[scanner_engine] CONTAINMENT VIOLATION: {subset_name} has "
                f"{len(missing)} ticker(s) that {superset_name} is missing: "
                f"{', '.join(missing)}"
            )
    return violations


def _asx_small_ords_df():
    """ASX Small Ordinaries = ASX 300 minus ASX 100 (the standard
    definition) - derived from the two pools already fetched above, no
    new scrape. None if ASX 300 itself isn't available (nothing to
    subtract from); if ASX 100's own live/fallback source is
    unavailable too, falls back to ASX 300 minus ASX 200 instead (still
    a real, if less precise, "smaller names" cut) rather than returning
    the whole ASX 300 unfiltered under the Small Ordinaries name."""
    df300 = fetch_asx300()
    if df300 is None:
        return None
    df100 = fetch_asx100()
    exclude = set((df100 if df100 is not None else fetch_asx200())["Ticker"]) \
        if (df100 is not None or fetch_asx200() is not None) else set()
    return df300[~df300["Ticker"].isin(exclude)]


def _asx_alltech_df():
    """ASX All Technology, derived: ASX 300 members whose GICS sector is
    Information Technology. Per the round 2 instruction doc, this is
    the fallback path when no live "ASX All Technology" index page can
    be confirmed - which is the case here (see the URL constants'
    comment on the live-verification caveat), so it's used directly as
    the only implementation rather than as a secondary fallback behind
    an unverifiable guessed URL that risks silently parsing the WRONG
    page's table as if it were correct. Labeled "ASX 300 tech
    (derived)" everywhere it's surfaced so nothing claims to be the
    official index."""
    df300 = fetch_asx300()
    if df300 is None:
        return None
    return df300[df300["Sector"].astype(str).str.contains(
        "Information Technology|Technology", case=False, na=False)]


# Part 34.1/34.2 (11 Sep 2026): AU + US sector universes, derived from
# the nightly ASX 300 / S&P 500 scans exactly like _asx_alltech_df()
# above - filter the already-fetched parent pool's Sector column, never
# a new scrape. The real, distinct GICS sector strings each parent pool
# actually carries were printed and verified live before writing this
# mapping (see the Part 34 report for the full printout) - the one
# genuine, must-not-guess discrepancy between the two markets: ASX 300's
# Sector column (merged in from fetch_asx200()'s own live GICS data -
# see fetch_asx300()'s docstring) uses "Healthcare" (one word), while
# the S&P 500's Sector column uses "Health Care" (two words) - same
# global GICS taxonomy, genuinely different spelling on the two
# exchanges' own Wikipedia pages. Every other GICS sector name matched
# exactly between the two markets. Sectors outside each six-name list
# (AU: Energy/Utilities/Telecom/IT beyond the six named; US: none of the
# 11 GICS sectors omitted except by not being in this dict) simply
# belong to no sector universe for now, per the instruction's own "do
# NOT invent extra universes" rule - only the "All" sector filter still
# reaches them.
_ASX_SECTOR_UNIVERSE_MAP = {
    "ASX Financials": ["Financials"],
    "ASX Materials & Mining": ["Materials"],
    "ASX Health Care": ["Healthcare"],
    "ASX Consumer": ["Consumer Staples", "Consumer Discretionary"],
    "ASX Industrials": ["Industrials"],
    "ASX A-REITs": ["Real Estate"],
}

_US_SECTOR_UNIVERSE_MAP = {
    "US Technology": ["Information Technology"],
    "US Healthcare": ["Health Care"],
    "US Financials": ["Financials"],
    "US Energy": ["Energy"],
    "US Industrials": ["Industrials"],
    "US Consumer": ["Consumer Staples", "Consumer Discretionary"],
}


def _asx_sector_df(universe_name):
    """One AU sector universe (34.1) - ASX 300 members whose Sector is in
    _ASX_SECTOR_UNIVERSE_MAP[universe_name]. None if ASX 300 itself isn't
    available (nothing to filter)."""
    df300 = fetch_asx300()
    if df300 is None:
        return None
    return df300[df300["Sector"].isin(_ASX_SECTOR_UNIVERSE_MAP[universe_name])]


def _us_sector_df(universe_name):
    """One US sector universe (34.2) - S&P 500 members whose Sector is in
    _US_SECTOR_UNIVERSE_MAP[universe_name]. None if S&P 500 itself isn't
    available."""
    df500 = fetch_sp500()
    if df500 is None:
        return None
    return df500[df500["Sector"].isin(_US_SECTOR_UNIVERSE_MAP[universe_name])]


def _asx200_marketcap_map():
    """Part 34.5: a separate, isolated parse of the SAME ASX 200
    Wikipedia page fetch_asx200() already scrapes, pulling out its
    "Market Capitalisation (A$)" column into a {ticker: float} map -
    live-verified during development (via WebSearch->WebFetch) to be
    plain digit-and-comma figures, e.g. "289,174,295,462" for CBA, no
    currency symbol or bn/m suffix to strip beyond that.

    Deliberately its OWN fetch + parse, not a widened return shape on
    fetch_asx200() itself (which many existing callers already depend on
    being a plain ['Ticker','Sector'] frame) - this keeps the ASX 100
    fallback fix below fully isolated, zero risk to any existing caller.
    Returns {} (not None) on any failure, so callers can use it with a
    plain .get(ticker, 0) without a None-check.
    """
    import re
    try:
        html = _get(ASX200_WIKI_URL)
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        return {}
    for table in tables:
        ticker_col = _find_column(table.columns, ["code", "ticker", "symbol"])
        cap_col = _find_column(table.columns, ["market capitalisation", "market capitalization", "market cap"])
        if ticker_col is None or cap_col is None:
            continue
        df = table[[ticker_col, cap_col]].dropna()
        if len(df) < 150:
            continue
        out = {}
        for _, row in df.iterrows():
            ticker = _normalize_asx_ticker(row[ticker_col])
            digits = re.sub(r"[^\d.]", "", str(row[cap_col]))
            if digits:
                try:
                    out[ticker] = float(digits)
                except ValueError:
                    pass
        if out:
            return out
    return {}


def _asx_topn_by_marketcap_df(n):
    """Part 34.5: the top `n` ASX 200 members by market capitalisation
    (via _asx200_marketcap_map() above), Sector carried over from
    fetch_asx200()'s own live GICS data. None if ASX 200 itself, or the
    market-cap map, isn't available - callers fall back further from
    there exactly like every other tier in get_universe_pool()."""
    df200 = fetch_asx200()
    if df200 is None or df200.empty:
        return None
    caps = _asx200_marketcap_map()
    if not caps:
        return None
    df = df200.copy()
    df["_cap"] = df["Ticker"].map(caps)
    df = df.dropna(subset=["_cap"]).sort_values("_cap", ascending=False)
    if df.empty:
        return None
    return df.head(n)[["Ticker", "Sector"]]


def _asx_fallback_df():
    return pd.DataFrame({"Ticker": list(ASX_SECTOR_MAP.keys()), "Sector": list(ASX_SECTOR_MAP.values())})


# Fix 8a, AI fixes round 2 (2026-08-31): broader coverage, both markets.
# Derived universes (Small Ordinaries, ASX 100/50/20, S&P 1500) need no
# scan of their own - scheduler_engine builds their scan tables/
# snapshots by filtering the parent scans (Fix 8b/8c) - but they still
# belong in these two lists so the Scanner dropdown, resolve_tickers()
# and every downstream consumer (api_v1._KNOWN_UNIVERSES, mcp_server.
# _KNOWN_UNIVERSES, the sitemap/scan endpoints' "unknown universe"
# error) picks them up automatically, same as any other universe.
AUSTRALIA_UNIVERSES = [
    "ASX 200", "ASX 300", "All Ordinaries", "ASX Small Ordinaries",
    "ASX 100", "ASX 50", "ASX 20", "ASX All Technology",
    # Part 34.1 (11 Sep 2026): AU sector universes, derived from ASX 300 -
    # see _ASX_SECTOR_UNIVERSE_MAP's own comment above for the mapping.
    "ASX Financials", "ASX Materials & Mining", "ASX Health Care",
    "ASX Consumer", "ASX Industrials", "ASX A-REITs",
]
USA_UNIVERSES = [
    "S&P 500", "Nasdaq 100", "Russell 2000", "Small Caps (S&P 600)",
    "S&P 400 MidCap", "Russell 1000", "S&P 1500",
    # Part 34.3 (11 Sep 2026): Russell 3000, derived union of R1000+R2000.
    "Russell 3000",
    # Part 34.4 (11 Sep 2026): three new scanned US universes. "Nasdaq
    # Next Gen 100" is deliberately NOT added - live-verified during
    # development (WebSearch, twice) that no Wikipedia article exists
    # for it at all, so per the instruction's own explicit fallback
    # ("say so in the report and skip this universe rather than shipping
    # another empty pill") it is skipped entirely rather than shipped
    # with a guaranteed-empty fetcher.
    "S&P 500 Dividend Aristocrats",
    # Part 34.4 (11 Sep 2026): "Dow Jones 30" re-added - see
    # _DOW30_STATIC_FALLBACK's own comment above for why this is now
    # safe again (a real, dated static fallback behind the existing
    # scrape attempt, so the universe can never come back empty even
    # though the live Wikipedia scrape still doesn't resolve). This
    # reverses the 9 Sep 2026 emergency removal noted below.
    "Dow Jones 30",
    # Part 34.2 (11 Sep 2026): US sector universes, derived from S&P 500 -
    # see _US_SECTOR_UNIVERSE_MAP's own comment above for the mapping.
    "US Technology", "US Healthcare", "US Financials", "US Energy",
    "US Industrials", "US Consumer",
]
# 9 Sep 2026 (owner-reported bug): "Dow Jones 30" was removed from this
# list because fetch_dow30()'s Wikipedia scrape never resolved any
# tickers in production (the live page still carries no matching
# "Components" table - reconfirmed live during Part 34's own development,
# 11 Sep 2026). Re-added above by Part 34.4 now that a real, dated static
# fallback (_DOW30_STATIC_FALLBACK) sits behind that same scrape attempt
# in get_universe_pool() below, so the universe can never be empty again
# regardless of whether Wikipedia's page structure ever gets fixed.
# fetch_dow30()/DOW30_WIKI_URL are unchanged and still tried first.

# Part 53.1 (16 Sep 2026): every universe in AUSTRALIA_UNIVERSES/
# USA_UNIVERSES whose get_universe_pool() branch below does NOT do a live
# fetch of its own - it filters or unions a parent pool that's already
# been fetched for some OTHER universe instead (Small Ordinaries = ASX
# 300 minus ASX 100, the AU/US sector universes = a GICS-sector filter of
# ASX 300/S&P 500, S&P 1500/Russell 3000 = a union of already-fetched
# parents, etc). A derived universe never gets its own nightly scan slot
# (scheduler_engine builds its scan table by filtering the parent scan's
# rows instead - see the Fix 8a comment above), so the Admin Dashboard's
# weekly scan calendar (Part 53.1) marks these rows with a ⧉ rather than
# expecting a ✓/◦ of their own. Single source of truth here rather than a
# second hand-maintained list in app.py, so this can never drift from
# get_universe_pool()'s actual dispatch branches below.
DERIVED_UNIVERSES = frozenset([
    "ASX Small Ordinaries", "ASX 100", "ASX 50", "ASX 20", "ASX All Technology",
    "ASX Financials", "ASX Materials & Mining", "ASX Health Care",
    "ASX Consumer", "ASX Industrials", "ASX A-REITs",
    "S&P 1500", "Russell 3000",
    "US Technology", "US Healthcare", "US Financials", "US Energy",
    "US Industrials", "US Consumer",
])


def get_universes(country):
    if country == "Australia":
        return AUSTRALIA_UNIVERSES
    if country == "USA":
        return USA_UNIVERSES
    return []


def get_universe_pool(country, universe):
    """
    Returns (df['Ticker','Sector'] or None, source_label) for the chosen
    Country + Universe - each universe maps to exactly one live source, plus
    a fallback so a scan never comes back completely empty just because one
    web scrape is temporarily unavailable.
    """
    if universe == "ASX 200":
        df = fetch_asx200()
        if df is not None:
            return df, "Wikipedia S&P/ASX 200 (live)"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe == "ASX 300":
        df = fetch_asx300()
        if df is not None:
            # Index containment (20 Sep 2026): labelled "Derived", not
            # "(live)" alone - this is Wikipedia's real ASX 200 plus a
            # market-cap-ranked approximation past it, not verified S&P
            # index membership. See fetch_asx300()'s own docstring.
            return df, "Derived: Wikipedia ASX 200 (live) + next ~100 ASX-listed by market cap"
        df200 = fetch_asx200()
        if df200 is not None:
            return df200, "ASX 300 unavailable - showing ASX 200 (live) instead"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe == "S&P 500":
        df = fetch_sp500()
        if df is not None:
            return df, "Wikipedia S&P 500 (live)"
        fallback_df = pd.DataFrame({"Ticker": _SP500_STATIC_FALLBACK, "Sector": [None] * len(_SP500_STATIC_FALLBACK)})
        return fallback_df, "Web scrape unavailable - static 10-ticker fallback list"

    if universe == "Nasdaq 100":
        # Fix 10 (2026-09-01): three-level fallback chain, resolved the
        # same "always return SOMETHING" way every other universe here
        # already does - see fetch_nasdaq100()/fetch_nasdaq100_invesco()/
        # _NASDAQ100_STATIC_FALLBACK's own comments for what each level
        # tries and why the prior level failed to resolve on 3
        # consecutive nightly attempts.
        df = fetch_nasdaq100()
        if df is not None:
            return df, "Wikipedia Nasdaq-100 (live)"
        df = fetch_nasdaq100_invesco()
        if df is not None:
            return df, "Nasdaq 100 unavailable - Invesco QQQ holdings (live) instead"
        fallback_df = pd.DataFrame({
            "Ticker": _NASDAQ100_STATIC_FALLBACK,
            "Sector": [None] * len(_NASDAQ100_STATIC_FALLBACK),
        })
        return fallback_df, (
            f"Web scrape unavailable - static {len(_NASDAQ100_STATIC_FALLBACK)}-ticker "
            f"fallback list"
        )

    if universe == "Russell 2000":
        # Part 52.2: live iShares -> disk cache -> repo-committed static
        # snapshot - see _resolve_russell2000()'s own docstring.
        return _resolve_russell2000()

    if universe == "Small Caps (S&P 600)":
        df = fetch_sp600()
        return (df, "Wikipedia S&P SmallCap 600 (live)") if df is not None else (None, "Web scrape unavailable")

    # --- Fix 8a, AI fixes round 2 (2026-08-31): new universes below ---

    if universe == "All Ordinaries":
        df = fetch_allords()
        if df is not None:
            # Index containment (20 Sep 2026): same "Derived" honesty as
            # ASX 300 above - see fetch_allords()'s own docstring.
            return df, "Derived: Wikipedia ASX 200 (live) + next ~300 ASX-listed by market cap"
        df300 = fetch_asx300()
        if df300 is not None:
            return df300, "All Ordinaries unavailable - showing ASX 300 (live) instead"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe == "ASX Small Ordinaries":
        df = _asx_small_ords_df()
        if df is not None and not df.empty:
            return df, "Derived: ASX 300 minus ASX 100 (live)"
        df300 = fetch_asx300()
        if df300 is not None:
            return df300, "ASX Small Ordinaries unavailable - showing ASX 300 (live) instead"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe == "ASX 100":
        df = fetch_asx100()
        if df is not None:
            return df, "Wikipedia S&P/ASX 100 (live)"
        # Part 34.5 (11 Sep 2026): fetch_asx100() has no live source at
        # all (owner-verified: /api/v1/scan/asx-100 was silently serving
        # ASX 200's full ~200-row set under the "ASX 100" label) - rather
        # than fall straight through to the whole ASX 200 as if it WERE
        # the ASX 100, derive the top 100 ASX 200 names by market cap
        # first (a real, honestly-labelled 100-row approximation), and
        # only fall all the way through to the raw ASX 200/local fallback
        # if even THAT can't be built (e.g. the market-cap column itself
        # becomes unparseable one day too).
        df_topn = _asx_topn_by_marketcap_df(100)
        if df_topn is not None and not df_topn.empty:
            return df_topn, "top 100 of ASX 200 by market cap - membership list unavailable"
        df200 = fetch_asx200()
        if df200 is not None:
            return df200, "ASX 100 unavailable - showing ASX 200 (live) instead"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe == "ASX 50":
        df = fetch_asx50()
        if df is not None:
            return df, "Wikipedia S&P/ASX 50 (live)"
        df100 = fetch_asx100()
        if df100 is not None:
            return df100, "ASX 50 unavailable - showing ASX 100 (live) instead"
        df200 = fetch_asx200()
        if df200 is not None:
            return df200, "ASX 50 unavailable - showing ASX 200 (live) instead"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe == "ASX 20":
        df = fetch_asx20()
        if df is not None:
            return df, "Wikipedia S&P/ASX 20 (live)"
        df50 = fetch_asx50()
        if df50 is not None:
            return df50, "ASX 20 unavailable - showing ASX 50 (live) instead"
        df200 = fetch_asx200()
        if df200 is not None:
            return df200, "ASX 20 unavailable - showing ASX 200 (live) instead"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe == "ASX All Technology":
        df = _asx_alltech_df()
        if df is not None and not df.empty:
            return df, "ASX 300 tech (derived)"
        df300 = fetch_asx300()
        if df300 is not None:
            return df300, "ASX All Technology unavailable - showing ASX 300 (live) instead"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe == "Dow Jones 30":
        df = fetch_dow30()
        if df is not None:
            return df, "Wikipedia Dow Jones Industrial Average (live)"
        # Part 34.4 (11 Sep 2026): the Dow's own Wikipedia page has never
        # carried a parseable Components table (re-confirmed live during
        # this Part's development - see fetch_dow30()'s own docstring and
        # _DOW30_STATIC_FALLBACK's comment above), so this universe needs
        # a real fallback rather than the usual "next deploy might fix
        # it" - a dated static 30-ticker list, tried only after the live
        # scrape attempt above, so it never masks a future working scrape.
        fallback_df = pd.DataFrame({
            "Ticker": _DOW30_STATIC_FALLBACK, "Sector": [None] * len(_DOW30_STATIC_FALLBACK),
        })
        return fallback_df, "Web scrape unavailable - static 30-ticker fallback list (as of 29 Jun 2026)"

    if universe == "S&P 400 MidCap":
        df = fetch_sp400()
        return (df, "Wikipedia S&P 400 (live)") if df is not None else (None, "Web scrape unavailable")

    if universe == "Russell 1000":
        # Part 52.1: live iShares -> Wikipedia "List of Russell 1000
        # companies" -> disk cache - see _resolve_russell1000()'s own
        # docstring.
        return _resolve_russell1000()

    if universe == "S&P 1500":
        df = _sp1500_df()
        return (df, "Derived: S&P 500 + S&P 400 + S&P 600 (live)") if df is not None else (None, "Web scrape unavailable")

    # --- Part 34.3 (11 Sep 2026): Russell 3000, derived union ---

    if universe == "Russell 3000":
        df = _russell3000_df()
        return (df, "Derived: Russell 1000 + Russell 2000 (live)") if df is not None else (None, "Web scrape unavailable")

    # --- Part 34.4 (11 Sep 2026): S&P 500 Dividend Aristocrats ---

    if universe == "S&P 500 Dividend Aristocrats":
        df = fetch_dividend_aristocrats()
        return (df, "Wikipedia S&P 500 Dividend Aristocrats (live)") if df is not None else (None, "Web scrape unavailable")

    # --- Part 34.1/34.2 (11 Sep 2026): AU + US sector universes ---

    if universe in _ASX_SECTOR_UNIVERSE_MAP:
        df = _asx_sector_df(universe)
        if df is not None and not df.empty:
            return df, "Derived: ASX 300 filtered by sector (live)"
        df300 = fetch_asx300()
        if df300 is not None:
            return df300, f"{universe} unavailable - showing ASX 300 (live) instead"
        return _asx_fallback_df(), "Live scrape unavailable - local curated ASX 200 list instead"

    if universe in _US_SECTOR_UNIVERSE_MAP:
        df = _us_sector_df(universe)
        if df is not None and not df.empty:
            return df, "Derived: S&P 500 filtered by sector (live)"
        df500 = fetch_sp500()
        if df500 is not None:
            return df500, f"{universe} unavailable - showing S&P 500 (live) instead"
        return None, "Web scrape unavailable"

    return None, "Unknown universe"


def get_sectors(df):
    """Sorted list of sectors for the Sector filter dropdown, always
    including 'All'."""
    if df is None or df.empty or "Sector" not in df.columns:
        return ["All"]
    sectors = df["Sector"].dropna().unique().tolist()
    if len(sectors) == 0:
        return ["All"]
    return ["All"] + sorted(sectors)


# Part 47 (13 Sep 2026), extended by Part 48 (13 Sep 2026): the single
# sector-lookup helper for the three UI placements (Deep Dive header chip,
# Value Map tooltip, Scanner table second line) - deliberately NEVER
# fetches anything itself. Precedence, cheapest/most-authoritative first:
#   1. `scan_row["Sector"]` when given and non-empty - nightly_scan.
#      run_universe_scan() already attaches this to every scanned row
#      (Services batch 2, Part 2, 2026-09-01: "Sector, straight from the
#      constituent pool already fetched above - no extra call"), straight
#      off the SAME live GICS data fetch_asx200()/fetch_sp500()/etc.
#      already do for the sector-universe filters - and it survives the
#      nightly reprice pass unchanged (_reprice_row() starts from
#      `dict(row)` and only overwrites price-dependent keys). This is the
#      path the Scanner table and Value Map always use (they're only ever
#      handed real scan rows), and the path Deep Dive uses whenever the
#      ticker has a stored snapshot.
#   2. Part 48: sector_cache_store's local, persistent cache - a plain
#      SQLite read, zero fetch. This is what fills the gap step 1 leaves
#      for Small Ordinaries/All Ordinaries names (their constituent
#      source carries no Sector column at all - unlike ASX 200/S&P 500's
#      own live pool fetch): once ANY path below (or a scan on a
#      sector-carrying universe, or the nightly top-up) has ever learned
#      a ticker's sector, every later call for that ticker resolves here,
#      forever, without waiting for a rescan.
#   3. An ASX ticker with no scan_row sector and no cache row:
#      ASX_SECTOR_MAP, the static module-level dict above - already
#      resident in memory, zero fetch. Its own comment flags it as the
#      "local ASX 200 fallback... used only if the live ASX 200 scrape
#      fails" - i.e. genuinely partial coverage (~100 of several hundred
#      ASX tickers). A hit here is written through to the cache (source=
#      "static_map") so this lookup only ever has to happen once per
#      ticker, not on every render.
#   4. `company_info["sector"]` when given - the yfinance .info dict a
#      CALLER already fetched for its own purposes (e.g. Deep Dive's
#      get_ticker_info(), already invoked once per page by deep_dive_
#      engine.analyze() itself, cached 30 min - a second call in the same
#      run is a cache hit, not a new fetch). This function never fetches
#      it itself. A hit here is also written through to the cache
#      (source="deep_dive" - today's only caller that ever passes
#      company_info), so a ticker Deep Dive resolves once via this path
#      also lights up its Scanner-table/Value-Map second line/tooltip
#      from then on, with no rescan needed.
#   5. None - the ticker's sector is genuinely unknown to every source
#      above. Every call site must render nothing at all (never
#      "Unknown"/"-"), per the instruction's own hard rule.
#
# Deliberately NOT written through to the cache: a scan_row hit (step 1).
# nightly_scan.run_universe_scan() already bulk-learns every one of its
# own rows' sectors in one pass right after the scan (source="scan") -
# see that function's own comment - so re-writing the same value here on
# every single page render that happens to pass a scan_row would just be
# a redundant SQLite write for no new information.
def sector_for_ticker(ticker, scan_row=None, company_info=None):
    if scan_row:
        _s = scan_row.get("Sector")
        if isinstance(_s, str) and _s.strip():
            return _s.strip()
    _tk = str(ticker).strip().upper() if ticker else None
    if _tk:
        _cached = sector_cache_store.get(_tk)
        if _cached:
            return _cached
    if _tk and _tk.endswith(".AX"):
        _s = ASX_SECTOR_MAP.get(_tk)
        if _s:
            sector_cache_store.learn(_tk, _s, source="static_map")
            return _s
    if company_info:
        _s = company_info.get("sector")
        if isinstance(_s, str) and _s.strip():
            _s = _s.strip()
            if _tk:
                sector_cache_store.learn(_tk, _s, source="deep_dive")
            return _s
    return None


def resolve_tickers(country, universe, sector):
    """
    Final ticker list for Run Scan: the chosen universe's pool, narrowed to
    `sector` if it's not "All". Returns (ticker_list, source_label).
    """
    pool_df, source = get_universe_pool(country, universe)

    if pool_df is None or pool_df.empty:
        return [], source

    if sector != "All" and pool_df["Sector"].notna().any():
        pool_df = pool_df[pool_df["Sector"] == sector]

    tickers = sorted(pool_df["Ticker"].dropna().unique().tolist())
    return tickers, source


# ---------------------------------------------------------------------
# SECTOR HEAT - decorates the Sector dropdown's own option labels (e.g.
# "\U0001F7E2 Technology - Hot (+14.3% 12m)") with each sector's trailing
# 12-month performance, exactly like the original desktop app's Sector
# picker. Ported as-is from that app's sector_heat_engine.py - this is
# ONLY used to label the dropdown options; there is no separate detail
# table/expander on this site (that was deliberately dropped).
# ---------------------------------------------------------------------

_HEAT_EMOJI = {"HOT": "\U0001F7E2", "MEDIUM": "\U0001F7E1", "COLD": "\U0001F534"}

# Per-stock 12-month return is clipped to this band before weighting, so a
# split/data artifact (e.g. a consolidation printing +900%) can't distort a
# sector's figure even for a large name that size-weighting alone wouldn't
# suppress.
_HEAT_CLIP_LO, _HEAT_CLIP_HI = -95.0, 300.0

# Downloads are chunked to this many tickers per yf.download call, run one
# chunk at a time rather than one big batch - large concurrent yfinance
# bursts have been known to get silently killed by security software on
# some machines, which looks like an unexplained crash. This whole
# computation is cached for a day, so a little extra time paid once is a
# fine trade for not risking that.
_HEAT_CHUNK_SIZE = 15


def _heat_download_batch(tickers):
    """One yf.download call for a small batch of tickers - defensive, so a
    bad/delisted ticker yields ret=None rather than raising."""
    out = {t: {"ret": None, "weight": 0.0} for t in tickers}

    try:
        data = yf.download(
            list(tickers), period="1y", progress=False,
            group_by="ticker", threads=False, auto_adjust=True,
        )
    except Exception:
        return out

    if data is None or len(data) == 0:
        return out

    single = len(tickers) == 1

    for t in tickers:
        try:
            if single:
                close = data["Close"]
                vol = data["Volume"] if "Volume" in getattr(data, "columns", []) else None
            else:
                sub = data[t]
                close = sub["Close"]
                vol = sub["Volume"] if "Volume" in sub.columns else None

            c = pd.to_numeric(close, errors="coerce")
            cc = c.dropna()
            if len(cc) < 2:
                continue

            first = float(cc.iloc[0])
            last = float(cc.iloc[-1])
            if first <= 0:
                continue

            ret = (last - first) / first * 100.0
            ret = max(_HEAT_CLIP_LO, min(ret, _HEAT_CLIP_HI))

            # Size/liquidity weight = average daily dollar volume over the
            # year - a market-cap stand-in that costs nothing extra, since
            # the same price history already carries Volume.
            weight = 0.0
            if vol is not None:
                v = pd.to_numeric(vol, errors="coerce")
                dv = (c * v).dropna()
                if len(dv):
                    m = float(dv.mean())
                    if m > 0:
                        weight = m

            out[t] = {"ret": ret, "weight": weight}
        except Exception:
            out[t] = {"ret": None, "weight": 0.0}

    return out


def _heat_download_ticker_data(tickers):
    out = {t: {"ret": None, "weight": 0.0} for t in tickers}
    if not tickers:
        return out

    tickers = list(tickers)
    for i in range(0, len(tickers), _HEAT_CHUNK_SIZE):
        chunk = tickers[i:i + _HEAT_CHUNK_SIZE]
        try:
            out.update(_heat_download_batch(chunk))
        except Exception:
            pass
        if i + _HEAT_CHUNK_SIZE < len(tickers):
            time.sleep(0.3)

    return out


@st.cache_data(ttl=86400, show_spinner=False)
def compute_sector_heat(ticker_sector_pairs, max_per_sector=25):
    """
    ticker_sector_pairs: a hashable tuple of (ticker, sector) pairs (so the
    Streamlit cache can key on it - the exact universe/sector set). Returns
    {sector: {"return": float, "bucket": "HOT|MEDIUM|COLD", "emoji": str,
    "n": int}} - "return" is the dollar-volume-weighted average of a sample
    of each sector's constituents' (clipped) 12-month returns, so a single
    small speculative multi-bagger can't drag a whole sector's number up.

    With 3+ sectors, sectors are ranked and split into thirds (top third
    HOT, bottom third COLD, middle MEDIUM) - a relative "hot vs the rest of
    this universe" read. With only 1-2 sectors there's nothing to rank
    against, so it falls back to an absolute cut (>=+8% HOT, <=0% COLD).
    """
    pairs = list(ticker_sector_pairs)
    if not pairs:
        return {}

    by_sector = defaultdict(list)
    for ticker, sector in pairs:
        sector = str(sector).strip()
        if sector and sector.lower() not in ("none", "nan", ""):
            by_sector[sector].append(str(ticker))

    if not by_sector:
        return {}

    sample = {}
    all_tickers = set()
    for sector, tickers in by_sector.items():
        chosen = sorted(set(tickers))[:max_per_sector]
        sample[sector] = chosen
        all_tickers.update(chosen)

    tdata = _heat_download_ticker_data(sorted(all_tickers))

    sector_avg = {}
    sector_n = {}
    for sector, tickers in sample.items():
        rets, wts = [], []
        for t in tickers:
            d = tdata.get(t)
            if d and d["ret"] is not None:
                rets.append(d["ret"])
                wts.append(d["weight"])
        if not rets:
            continue

        total_w = sum(wts)
        if total_w > 0:
            weighted = sum(r * w for r, w in zip(rets, wts)) / total_w
        else:
            weighted = sum(rets) / len(rets)

        sector_avg[sector] = weighted
        sector_n[sector] = len(rets)

    if not sector_avg:
        return {}

    ranked = sorted(sector_avg.items(), key=lambda kv: kv[1])
    n = len(ranked)

    heat = {}
    for i, (sector, avg) in enumerate(ranked):
        if n >= 3:
            third = n / 3.0
            if i < third:
                bucket = "COLD"
            elif i < 2 * third:
                bucket = "MEDIUM"
            else:
                bucket = "HOT"
        else:
            if avg >= 8:
                bucket = "HOT"
            elif avg <= 0:
                bucket = "COLD"
            else:
                bucket = "MEDIUM"

        heat[sector] = {
            "return": round(avg, 1),
            "bucket": bucket,
            "emoji": _HEAT_EMOJI[bucket],
            "n": sector_n[sector],
        }

    return heat


def label_for(sector, heat):
    """Sector dropdown option label - plain name if no heat data for it
    (e.g. 'All', or a sector too small to have been sampled)."""
    if not sector or sector == "All" or sector not in heat:
        return sector
    h = heat[sector]
    # Dot only - the colored dot next to the name is the whole signal.
    return f"{h['emoji']} {sector}"


def _r2k_from_disk_cache():
    """Yesterday's (or older) Russell 2000 list from disk, if one was ever
    saved - degrading to a stale-but-real universe instead of nothing."""
    try:
        df = pd.read_csv(_r2k_cache_path())
        if "Ticker" in df.columns and len(df) > 100:
            if "Sector" not in df.columns:
                df["Sector"] = None
            return df[["Ticker", "Sector"]]
    except Exception:
        pass
    return None
