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

  Australia: ASX 200, ASX 300, All Ordinaries, ASX Small Ordinaries
             (derived), ASX 100, ASX 50, ASX 20, ASX All Technology
             (derived)
  USA:       S&P 500, Nasdaq 100, Russell 2000, Small Caps (S&P SmallCap
             600), Dow Jones 30, S&P 400 MidCap, Russell 1000, S&P 1500
             (derived)

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
"""

import io
import os
import time
from collections import defaultdict

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StocksDeepDiveBot/1.0; +https://stocksdeepdive.com)"}

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP600_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
ASX200_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_200"
ASX300_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_300"

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

# allordslist.com mirrors asx300list.com's own naming/URL pattern (root
# page IS the list, same as ASX300LIST_URL below) and was confirmed to
# exist via web search during development ("All Ords List - Company
# Data for All Ordinaries Index") but, per the caveat above, could not
# be fetched and test-parsed directly - best-effort by direct analogy
# to the already-working asx300list.com fetcher.
ALLORDSLIST_URL = "https://www.allordslist.com/"

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

# Wikipedia's own S&P/ASX 300 page does not carry a real ~300-row constituent
# table (only a ~10-row "Top Ten Companies" table) - asx300list.com does, and
# is used as the primary source; Wikipedia is kept only as a secondary
# fallback (with a min_rows guard so that small "Top Ten" table can never be
# silently mistaken for the real thing).
ASX300LIST_URL = "https://www.asx300list.com/"

# iShares Russell 2000 ETF (IWM) public holdings export. Best-effort - iShares
# occasionally changes this URL format.
IWM_HOLDINGS_CSV_URL = (
    "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
)

# Static, tiny emergency fallback if the S&P 500 scrape itself is down -
# better than returning nothing at all.
_SP500_STATIC_FALLBACK = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "BRK-B", "UNH"]

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


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_nasdaq100():
    try:
        html = _get(NASDAQ100_WIKI_URL)
    except Exception:
        return None
    return _parse_table(html, ["ticker", "symbol"], ["gics sector", "sector"], _normalize_us_ticker, min_rows=90)


def _r2k_cache_path():
    """Last-good Russell 2000 constituent list, persisted to the Railway
    Volume (or this directory locally) - iShares occasionally changes its
    holdings-export URL, and the fallback for that shouldn't be an empty
    universe when yesterday's list is sitting right there."""
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, "russell2000_cache.csv")


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_russell2000():
    """
    Best-effort: iShares publishes IWM's full holdings as a downloadable CSV.
    The export has several metadata rows before the real header. Returns a
    ['Ticker','Sector'] frame (Sector is always None - this export doesn't
    carry GICS sector) or None if unavailable.
    """
    try:
        resp = requests.get(IWM_HOLDINGS_CSV_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        raw = pd.read_csv(io.StringIO(resp.text), skiprows=9, on_bad_lines="skip")
    except Exception:
        return _r2k_from_disk_cache()

    ticker_col = _find_column(raw.columns, ["ticker"])
    if ticker_col is None:
        return _r2k_from_disk_cache()

    df = raw[[ticker_col]].copy()
    df.columns = ["Ticker"]
    df = df.dropna(subset=["Ticker"])
    # Drop cash/FX placeholder rows only. (An earlier version also dropped
    # any ticker containing "-", which silently removed legitimate class
    # shares - small-cap indices do contain them.)
    _t = df["Ticker"].astype(str).str.strip().str.upper()
    df = df[~_t.str.contains("CASH|USD", na=False) & (_t.str.len() <= 6)]
    if df.empty:
        return None

    df["Ticker"] = df["Ticker"].apply(_normalize_us_ticker)
    df["Sector"] = None
    out = df[["Ticker", "Sector"]]
    try:
        out.to_csv(_r2k_cache_path(), index=False)
    except OSError:
        pass
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx200():
    try:
        html = _get(ASX200_WIKI_URL)
    except Exception:
        return None
    df = _parse_table(html, ["code", "ticker", "symbol"], ["sector", "industry"], _normalize_asx_ticker, min_rows=150)
    if df is not None and df["Sector"].notna().sum() == 0:
        return None
    return df


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx300():
    """
    Primary source: asx300list.com (a real ~300+ row Code/Company table).
    Wikipedia's own S&P/ASX 300 page is kept only as a secondary fallback -
    it doesn't carry the full constituent table, only a ~10-row "Top Ten"
    one, so the min_rows=100 guard makes sure that's never silently
    accepted as if it were the whole universe.

    Neither source carries a Sector column for the full 300, so sector data
    is merged in afterwards from fetch_asx200()'s live GICS sectors (covers
    the ~200 largest names; the remaining ~100 names show Sector=None and
    are only reachable via the "All" sector filter).
    """
    df = None
    try:
        html = _get(ASX300LIST_URL)
        df = _parse_table(html, ["code"], ["sector", "industry"], _normalize_asx_ticker, min_rows=100)
    except Exception:
        df = None

    if df is None:
        try:
            html = _get(ASX300_WIKI_URL)
            df = _parse_table(
                html, ["code", "ticker", "symbol"], ["sector", "industry"], _normalize_asx_ticker, min_rows=100
            )
        except Exception:
            df = None

    if df is None:
        return None

    df200 = fetch_asx200()
    if df200 is not None:
        sector_by_ticker = dict(zip(df200["Ticker"], df200["Sector"]))
        df = df.copy()
        df["Sector"] = df["Ticker"].map(sector_by_ticker).combine_first(df["Sector"])

    return df


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
    """Mirrors fetch_russell2000() exactly (same iShares CSV-export
    mechanism, same disk-cache-on-failure fallback) - see that
    function's own docstring for the shared reasoning."""
    try:
        resp = requests.get(IWB_HOLDINGS_CSV_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        raw = pd.read_csv(io.StringIO(resp.text), skiprows=9, on_bad_lines="skip")
    except Exception:
        return _r1k_from_disk_cache()

    ticker_col = _find_column(raw.columns, ["ticker"])
    if ticker_col is None:
        return _r1k_from_disk_cache()

    df = raw[[ticker_col]].copy()
    df.columns = ["Ticker"]
    df = df.dropna(subset=["Ticker"])
    _t = df["Ticker"].astype(str).str.strip().str.upper()
    df = df[~_t.str.contains("CASH|USD", na=False) & (_t.str.len() <= 6)]
    if df.empty:
        return None

    df["Ticker"] = df["Ticker"].apply(_normalize_us_ticker)
    df["Sector"] = None
    out = df[["Ticker", "Sector"]]
    try:
        out.to_csv(_r1k_cache_path(), index=False)
    except OSError:
        pass
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_allords():
    """All Ordinaries (~500 names, effectively "every ASX company big
    enough to be liquid"). Primary source allordslist.com, by direct
    analogy to fetch_asx300()'s own asx300list.com fetcher (see
    ALLORDSLIST_URL's comment for the verification caveat). Sector is
    merged in afterwards from fetch_asx200()'s live GICS sectors, same
    as fetch_asx300() does - neither the All Ords nor ASX 300 list
    sources carry their own Sector column for the full membership."""
    try:
        html = _get(ALLORDSLIST_URL)
        df = _parse_table(html, ["code"], ["sector", "industry"], _normalize_asx_ticker, min_rows=400)
    except Exception:
        df = None

    if df is None:
        return None

    df200 = fetch_asx200()
    if df200 is not None:
        sector_by_ticker = dict(zip(df200["Ticker"], df200["Sector"]))
        df = df.copy()
        df["Sector"] = df["Ticker"].map(sector_by_ticker).combine_first(df["Sector"])

    return df


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
    return _parse_table(html, ["symbol", "code"], ["sector", "industry"],
                         _normalize_asx_ticker, min_rows=40, max_rows=55)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_asx100():
    """No dedicated live constituent-table source could be confirmed for
    this one (see ASX100_WIKI_URL's comment) - tried anyway since it's
    free to attempt and harmless on failure; get_universe_pool() falls
    back to ASX 200 (live) if this returns None, exactly like ASX 300's
    own existing fallback chain."""
    try:
        html = _get(ASX100_WIKI_URL)
    except Exception:
        return None
    return _parse_table(html, ["symbol", "code", "ticker"], ["sector", "industry"],
                         _normalize_asx_ticker, min_rows=80, max_rows=110)


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
]
USA_UNIVERSES = [
    "S&P 500", "Nasdaq 100", "Russell 2000", "Small Caps (S&P 600)",
    "Dow Jones 30", "S&P 400 MidCap", "Russell 1000", "S&P 1500",
]


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
            return df, "asx300list.com S&P/ASX 300 constituent list (live)"
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
        df = fetch_nasdaq100()
        return (df, "Wikipedia Nasdaq-100 (live)") if df is not None else (None, "Web scrape unavailable")

    if universe == "Russell 2000":
        df = fetch_russell2000()
        return (df, "iShares IWM ETF holdings (live)") if df is not None else (None, "Web scrape unavailable")

    if universe == "Small Caps (S&P 600)":
        df = fetch_sp600()
        return (df, "Wikipedia S&P SmallCap 600 (live)") if df is not None else (None, "Web scrape unavailable")

    # --- Fix 8a, AI fixes round 2 (2026-08-31): new universes below ---

    if universe == "All Ordinaries":
        df = fetch_allords()
        if df is not None:
            return df, "allordslist.com All Ordinaries constituent list (live)"
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
        return (df, "Wikipedia Dow Jones Industrial Average (live)") if df is not None else (None, "Web scrape unavailable")

    if universe == "S&P 400 MidCap":
        df = fetch_sp400()
        return (df, "Wikipedia S&P 400 (live)") if df is not None else (None, "Web scrape unavailable")

    if universe == "Russell 1000":
        df = fetch_russell1000()
        return (df, "iShares IWB ETF holdings (live)") if df is not None else (None, "Web scrape unavailable")

    if universe == "S&P 1500":
        df = _sp1500_df()
        return (df, "Derived: S&P 500 + S&P 400 + S&P 600 (live)") if df is not None else (None, "Web scrape unavailable")

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
