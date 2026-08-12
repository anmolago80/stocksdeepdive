"""
scanner_engine.py

Universe data for the Stock Scanner page: Country -> Universe -> (optional)
Sector filter -> a plain ticker list, fed straight into the same scan/
results pipeline Comparison already uses (_render_scan_page in app.py).

Ported from the original desktop app's universe_engine.py, trimmed down to
exactly the six universes the public site offers - each one maps to ONE
real index, never a blend of two (the original had several "USA Momentum
(S&P 500 + Nasdaq 100)"-style combined universes; those are deliberately
not carried over here, so picking a universe always means exactly one
real, well-known index and nothing gets double-counted):

  Australia: ASX 200, ASX 300
  USA:       S&P 500, Nasdaq 100, Russell 2000, Small Caps (S&P SmallCap 600)

Every fetcher is a live scrape (Wikipedia constituent tables, or an iShares
ETF holdings export for Russell 2000) cached for 24h via st.cache_data, with
a small static/derived fallback if the live source is unreachable or its
page structure has changed - so Run Scan always returns SOMETHING rather
than a silent empty universe. The source actually used is always shown back
to the user (see get_universe_pool's second return value) so it's never
ambiguous whether a scan is running on live or fallback data.
"""

import io

import pandas as pd
import requests
import streamlit as st

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StocksDeepDiveBot/1.0; +https://stocksdeepdive.com)"}

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP600_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
ASX200_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_200"
ASX300_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_300"

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

    "NST.AX": "Gold", "EVN.AX": "Gold", "NCM.AX": "Gold", "GOR.AX": "Gold",

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


def _parse_table(html_text, ticker_keywords, sector_keywords, normalize_fn, min_rows=1):
    """
    Scans every table on the page for the first one with a ticker-like
    column, returned as a ['Ticker','Sector'] frame. `min_rows` guards
    against a small "Top N Holdings"-style summary table (which can also
    have a ticker + sector column) being mistaken for the real, full
    constituent table - any table shorter than min_rows is skipped.
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
        return None

    ticker_col = _find_column(raw.columns, ["ticker"])
    if ticker_col is None:
        return None

    df = raw[[ticker_col]].copy()
    df.columns = ["Ticker"]
    df = df.dropna(subset=["Ticker"])
    df = df[~df["Ticker"].astype(str).str.contains("CASH|USD|-", na=False)]
    if df.empty:
        return None

    df["Ticker"] = df["Ticker"].apply(_normalize_us_ticker)
    df["Sector"] = None
    return df[["Ticker", "Sector"]]


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


def _asx_fallback_df():
    return pd.DataFrame({"Ticker": list(ASX_SECTOR_MAP.keys()), "Sector": list(ASX_SECTOR_MAP.values())})


AUSTRALIA_UNIVERSES = ["ASX 200", "ASX 300"]
USA_UNIVERSES = ["S&P 500", "Nasdaq 100", "Russell 2000", "Small Caps (S&P 600)"]


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
