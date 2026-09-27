"""
etf_insights.py

Next-batch instruction, Part 2 ("My ETFs" tab): fund facts, computed
returns/yield/correlations, and cross-portfolio overlap for every
holding the portfolio already classifies as an ETF/fund (the same
`kind == "ETF"` flag app.py's _analyze_holding()/portfolio_health_engine
already use to exempt a holding from Quality/Moat scoring - this module
never re-derives that classification, it only consumes it).

Two kinds of numbers live here, and they are computed differently on
purpose:

  - Fund FACTS (expense ratio, category, sector weights, top-10
    holdings) come from Yahoo's fund-specific quoteSummary modules via
    yfinance's `Ticker(ticker).funds_data` accessor (quoteType,
    summaryProfile, fundProfile, topHoldings - see yfinance's own
    scrapers/funds.py). Every field is fetched and cached independently
    of every other field: a fund missing sector weights (common for a
    plain-vanilla index ETF's simplified profile) still gets its
    expense ratio and top-10 shown, per the instruction's "every field
    optional - missing renders '-' plus an issuer link" rule. Cached 7
    days in the shared stocksdeepdive.db (same volume-resolution rule as
    every other store in this app, see explain_cache_store.py's
    docstring for why SQLite over a hand-rolled JSON file) - a fund's
    published facts change on the order of months, not days, so a
    week-old cache is never stale in any way a visitor could notice, and
    it keeps this tab from ever adding a per-pageview Yahoo fund-profile
    call.

  - Computed RETURNS/YIELD/CORRELATION are derived entirely from price
    history the site already fetches (stress_engine.get_long_history(),
    @st.cache_data/volume-backed) - never a second network call. Since
    a fund's published NAV/close price already has its own expense
    ratio baked in every trading day, a total-return figure computed
    from that price series is automatically "net of the fund's
    internal fee" without this module ever needing to know the fee to
    compute it - the MER shown alongside it is a separate,
    independently-sourced fact for context, not an input to the return
    calculation.

    MEGA-BATCH PART 14 (2026-09-06): total_return_pa() used to add the
    window's raw per-share Dividends on top of the price growth to get
    "total return" - correct back when the underlying Close was a bare
    (non-split-adjusted) price. stress_engine.get_long_history() now
    fetches with auto_adjust=True, so Close is ALREADY a total-return
    series (Yahoo's own back-adjustment folds reinvested distributions
    into the price) - total_return_pa() now uses plain price growth on
    that Close and no longer adds Dividends on top, which would double-
    count every distribution. ttm_distribution_yield() below is
    unaffected - it correctly wants the RAW per-share amount (not a
    total-return figure) for a yield calc, and yfinance's Dividends
    column stays raw regardless of auto_adjust.

IMPORTANT - this module was written and unit-tested against fixture
data shaped exactly like yfinance 1.6.0's real FundsData return types
(a DataFrame for top_holdings/fund_operations, a plain dict for
sector_weightings/fund_overview - see yfinance/scrapers/funds.py's own
_parse_top_holdings()/_parse_fund_profile()), but the sandbox this was
built in has no live network path to Yahoo Finance (every yfinance call
here fails with a proxy CONNECT error - confirmed by direct curl test),
so the actual field-by-field availability for Andrew's real holdings
has NOT been verified live. See the Part 2 report for what to check
after this deploys.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

# -----------------------------------------------------------------
# Storage - same volume-resolution rule as every other store in this
# app (RAILWAY_VOLUME_MOUNT_PATH when a Railway Volume is attached,
# else this file's own directory - see explain_cache_store.py).
# -----------------------------------------------------------------

def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

FUND_FACTS_TTL_HOURS = 24 * 7  # a week - see module docstring


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS etf_fund_facts (
            ticker TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


def _cache_get(ticker):
    with _conn() as conn:
        row = conn.execute(
            "SELECT data_json, created_at FROM etf_fund_facts WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    if not row:
        return None
    data_json, created_at = row
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if datetime.now(timezone.utc) - created > timedelta(hours=FUND_FACTS_TTL_HOURS):
        return None
    try:
        import json
        return json.loads(data_json)
    except Exception:
        return None


def _cache_set(ticker, data):
    import json
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO etf_fund_facts (ticker, data_json, created_at)
                 VALUES (?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 data_json = excluded.data_json, created_at = excluded.created_at""",
            (ticker, json.dumps(data), now),
        )


# -----------------------------------------------------------------
# Issuer -> fund-page link. A small, deliberately conservative map:
# each entry points at that issuer's own product-finder/search page
# (stable even if the fund's own permalink scheme changes) rather than
# guessing a per-fund URL slug this module can't verify live. Unknown
# issuer -> the ticker's own Yahoo quote page, which always resolves.
# -----------------------------------------------------------------

_ISSUER_LINKS = (
    ("vanguard", "https://www.vanguard.com.au/personal/products"),
    ("ishares", "https://www.blackrock.com/au/individual/products/product-list"),
    ("blackrock", "https://www.blackrock.com/au/individual/products/product-list"),
    ("betashares", "https://www.betashares.com.au/fund/"),
    ("state street", "https://www.ssga.com/au/en_gb/individual/etfs/fund-finder"),
    ("spdr", "https://www.ssga.com/au/en_gb/individual/etfs/fund-finder"),
    ("invesco", "https://www.invesco.com/us/financial-products/etfs/product-finder"),
)


def issuer_link(ticker, family_name):
    """Best-effort link to the fund's own page: a known issuer's
    product-finder page, else the ticker's Yahoo quote page - always a
    valid, resolvable URL either way, per the spec's "missing renders
    '-' plus an issuer link" rule (the issuer link itself never goes
    missing, even when every other field does)."""
    fam = (family_name or "").lower()
    for needle, url in _ISSUER_LINKS:
        if needle in fam:
            return url
    return f"https://finance.yahoo.com/quote/{ticker}"


# -----------------------------------------------------------------
# Fund facts - MER, category, sector weights, top-10 holdings.
# -----------------------------------------------------------------

# Audit A2-follow-up (27 Sep 2026, owner-directed): a sane-value gate on
# whatever yfinance's own MER parsing below produces, per the owner's
# own "0 < MER < 3%" rule - every real ASX/US index or sector ETF's fee
# sits well under 3%, so a value outside that band is a scaling error
# (either direction: Yahoo's raw `annualReportExpenseRatio.raw` field
# treated as a fraction when it was already a percent, or vice versa -
# see _fetch_fund_facts_live()'s own comment on that unresolved
# ambiguity), not a real fee - reject it rather than display a number
# that reads as either "free" or "10x too expensive".
MER_SANE_MIN_PCT = 0.0
MER_SANE_MAX_PCT = 3.0


def _mer_is_sane(mer):
    return mer is not None and MER_SANE_MIN_PCT < mer < MER_SANE_MAX_PCT


# Hand-maintained fallback for a small, explicitly-verified set of ASX
# ETFs whose Yahoo-derived MER has been found empty or out of the sane
# band above. Owner's own instruction: "Do NOT add any other ETF unless
# you verify it on the issuer's own page in this session and record the
# URL. Never guess a fee." - every entry here was verified by the
# owner's advisor directly on the issuer's own page on the "as_of" date
# given; do not add or edit an entry without the same verification.
MER_FALLBACK_TABLE = {
    "IVV.AX": {"fee_pct": 0.04, "source_url": "https://www.blackrock.com/au/products/275304/", "as_of": "2026-09-28"},
    "QRE.AX": {"fee_pct": 0.34, "source_url": "https://www.betashares.com.au/fund/resources-sector-etf-betashares/", "as_of": "2026-09-28"},
    "VAP.AX": {"fee_pct": 0.23, "source_url": "https://fund-docs.vanguard.com/AU_ETF_Profile_flyer_VAP.pdf", "as_of": "2026-09-28"},
}

def _fetch_fund_facts_live(ticker):
    """One yfinance funds_data() round trip, every field parsed and
    failed independently so one missing/malformed field never blanks
    the rest. Returns a plain-JSON-able dict (no DataFrames/NaN) so it
    can go straight into the sqlite cache."""
    out = {
        "mer": None, "category": None, "family": None,
        "sector_weights": None, "top_holdings": None,
        "fetched_ok": False,
    }
    try:
        fd = yf.Ticker(ticker).funds_data
    except Exception:
        return out
    out["fetched_ok"] = True

    try:
        overview = fd.fund_overview or {}
        out["category"] = overview.get("categoryName")
        out["family"] = overview.get("family")
    except Exception:
        pass

    try:
        ops = fd.fund_operations
        if ops is not None and "Annual Report Expense Ratio" in ops.index and ticker in ops.columns:
            val = ops.loc["Annual Report Expense Ratio", ticker]
            if val is not None and not (isinstance(val, float) and np.isnan(val)):
                # Yahoo reports this as a fraction (0.0007 = 0.07%) -
                # same "fraction vs already-percent" ambiguity the site's
                # own dividend-yield parsing already guards against
                # elsewhere (portfolio_health_engine._normalize_dividend_
                # yield), so apply the same >1-means-already-percent test.
                # Data-correctness audit A2 (27 Sep 2026): flagged by the
                # owner as sharing the SAME ambiguity-guard pattern that
                # proved wrong for dividendYield (a >1 threshold can't
                # tell a genuinely low value in one format from a
                # genuinely low value in the other) - but this reads
                # `fund_operations` (a completely different yfinance data
                # source from `info["dividendYield"]`, scraped
                # separately), and this sandboxed session has no live
                # network access to confirm whether ITS format has
                # actually drifted the same way under yfinance>=1.7.0.
                # Every realistic MER (roughly 0.03%-3%, i.e. 0.0003-0.03
                # as a fraction) stays comfortably under the >1 threshold
                # either way, so this branch is very likely dead code in
                # practice regardless - left unchanged pending a real
                # check against a live ETF (see the audit's own "Verify
                # on ... one ETF" instruction) rather than guess-fixing a
                # field I can't verify.
                mer = float(val)
                out["mer"] = mer if mer > 1 else mer * 100.0
    except Exception:
        pass

    try:
        sw = fd.sector_weightings
        if sw:
            out["sector_weights"] = {str(k): float(v) for k, v in sw.items() if v is not None}
    except Exception:
        pass

    try:
        th = fd.top_holdings
        if th is not None and not th.empty:
            rows = []
            for sym, row in th.iterrows():
                try:
                    rows.append({
                        "symbol": str(sym),
                        "name": str(row.get("Name") or sym),
                        "weight": float(row.get("Holding Percent")) * 100.0
                        if row.get("Holding Percent") is not None else None,
                    })
                except Exception:
                    continue
            out["top_holdings"] = rows[:10]
    except Exception:
        pass

    return out


def _apply_mer_fallback(ticker, data):
    """Audit A2-follow-up (27 Sep 2026, owner-directed): order is a sane
    Yahoo value (MER_SANE_MIN_PCT < mer < MER_SANE_MAX_PCT) first, else
    the hand-maintained MER_FALLBACK_TABLE, else leave mer=None (the
    caller shows "n/a" with a "fee not available" tooltip - never the
    site's usual "-", which reads as "no fee" rather than "unknown
    fee"). Sets "mer_source" ("yahoo" | "table" | None) and, only when
    the table was used, "mer_source_url"/"mer_asof" so the UI can show
    a "source: issuer, as of <date>" note. Mutates and returns `data`;
    never raises."""
    data = dict(data)
    if _mer_is_sane(data.get("mer")):
        data["mer_source"] = "yahoo"
        data["mer_source_url"] = None
        data["mer_asof"] = None
        return data
    fallback = MER_FALLBACK_TABLE.get(ticker)
    if fallback:
        data["mer"] = fallback["fee_pct"]
        data["mer_source"] = "table"
        data["mer_source_url"] = fallback["source_url"]
        data["mer_asof"] = fallback["as_of"]
        return data
    data["mer"] = None
    data["mer_source"] = None
    data["mer_source_url"] = None
    data["mer_asof"] = None
    return data


def get_fund_facts(ticker, force_refresh=False):
    """Fund facts for one ETF ticker - a 7-day cache hit almost always,
    a live yfinance funds_data() call otherwise. Never raises: any
    failure (network, parsing, an unrecognised fund shape) returns the
    same all-None shape _fetch_fund_facts_live() does on total failure,
    so a caller can always safely read every key with .get().

    The MER fallback (_apply_mer_fallback() above) is applied AFTER the
    cache lookup, every call, not baked into what gets cached - the
    cache still stores only the raw Yahoo-derived fields (same 7-day
    TTL, same shape as before this fix), so the hand-maintained table
    takes effect immediately for an already-cached ticker with no cache
    bust needed, and updating the table later needs no cache changes
    either."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _apply_mer_fallback(ticker, {
            "mer": None, "category": None, "family": None,
            "sector_weights": None, "top_holdings": None, "fetched_ok": False,
        })
    if not force_refresh:
        cached = _cache_get(ticker)
        if cached is not None:
            return _apply_mer_fallback(ticker, cached)
    data = _fetch_fund_facts_live(ticker)
    try:
        _cache_set(ticker, data)
    except Exception:
        pass
    return _apply_mer_fallback(ticker, data)


def fee_cost_pa(mer_pct, value_aud):
    """Portfolio ETF table, Task 2 (27 Sep 2026, owner-directed): annual
    fee cost in AUD = position value x MER. None (never 0) when either
    input is None - a missing MER must never be silently treated as a
    0% fee, the exact "n/a means unknown, not free" rule MER_FALLBACK_
    TABLE's own tooltip already applies to the rate itself."""
    if mer_pct is None or value_aud is None:
        return None
    return value_aud * (mer_pct / 100.0)


# -----------------------------------------------------------------
# Computed returns / yield / correlation - from price history the
# site already fetched (a pandas DataFrame with Close + Dividends
# columns, exactly get_price_history()'s / portfolio_health_engine's
# snapshot history shape). Zero new network calls.
# -----------------------------------------------------------------

def _monthly_returns(hist):
    """Month-end Close -> a Series of monthly simple returns, or None
    if there's under 2 months of data. Used only for correlation (a
    fixed cadence both series can be aligned on), not for the CAGR
    figures below (which use the full daily series' first/last price)."""
    if hist is None or hist.empty or "Close" not in hist:
        return None
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        return None
    monthly = closes.resample("ME").last().dropna()
    if len(monthly) < 2:
        return None
    return monthly.pct_change().dropna()


def total_return_pa(hist, years):
    """Annualised total return over the last `years` years of `hist`
    (a Close+Dividends price-history DataFrame from stress_engine.
    get_long_history() - Close is fetched with auto_adjust=True, so it
    is ALREADY a total-return series; see this module's docstring's
    Part 14 note for why no per-share Dividends are added here any
    more). None if there isn't at least ~80% of that window's worth of
    data (a fund listed 2 years ago can't report a 5y return - the
    caller falls back to a shorter window and labels it, per the
    spec)."""
    if hist is None or hist.empty or "Close" not in hist:
        return None
    closes = hist["Close"].dropna()
    if closes.empty:
        return None
    end_dt = closes.index[-1]
    start_dt = end_dt - pd.Timedelta(days=int(365.25 * years))
    window = closes[closes.index >= start_dt]
    if len(window) < 2:
        return None
    span_days = (window.index[-1] - window.index[0]).days
    if span_days < 365.25 * years * 0.8:
        return None
    start_price = float(window.iloc[0])
    end_price = float(window.iloc[-1])
    if start_price <= 0:
        return None
    total_growth = end_price / start_price
    if total_growth <= 0:
        return None
    actual_years = span_days / 365.25
    return (total_growth ** (1.0 / actual_years) - 1.0) * 100.0


def ttm_distribution_yield(hist):
    """Trailing-12-month distributions / current price, as a percent.
    None with no Dividends column or no price."""
    if hist is None or hist.empty or "Close" not in hist:
        return None
    closes = hist["Close"].dropna()
    if closes.empty:
        return None
    price = float(closes.iloc[-1])
    if price <= 0 or "Dividends" not in hist.columns:
        return None
    end_dt = closes.index[-1]
    start_dt = end_dt - pd.Timedelta(days=365)
    ttm_divs = float(hist["Dividends"][hist.index >= start_dt].sum())
    if ttm_divs <= 0:
        return 0.0
    return (ttm_divs / price) * 100.0


def correlation_vs(hist, benchmark_hist, min_months=24):
    """Correlation of monthly returns between `hist` and
    `benchmark_hist`, or None with fewer than `min_months` overlapping
    months (the spec's own threshold - a handful of overlapping months
    produces a correlation number with no real statistical meaning).

    SAME-MARKET series only (e.g. an ASX-listed fund vs ^AXJO, both
    fetched via yfinance so both carry that exchange's own tz-aware
    index) - `pd.concat(..., join="inner")` joins on exact index
    (timestamp) equality, which two SAME-tz series' own month-end
    resample lands on identically. For a CROSS-market pair (an
    ASX-listed fund vs ^GSPC), use correlation_vs_us_benchmark() below
    instead - see its own docstring for why this function silently
    returns None for that case rather than a wrong number."""
    a = _monthly_returns(hist)
    b = _monthly_returns(benchmark_hist)
    if a is None or b is None:
        return None
    aligned = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(aligned) < min_months:
        return None
    corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
    if corr is None or (isinstance(corr, float) and np.isnan(corr)):
        return None
    return float(corr)


def _daily_returns_naive(hist):
    """Close -> a Series of daily simple returns, indexed by a plain
    (tz-naive, midnight-normalized) calendar date - never a tz-aware
    Timestamp. None with under 2 valid closes.

    Bug this exists to fix (27 Sep 2026, owner-reported: "Corr. vs
    S&P 500" empty for every ASX-listed ETF while "Corr. vs ASX 200"
    worked for the same rows): yfinance's `.history()` index is
    tz-AWARE, localized to each ticker's OWN exchange (Australia/Sydney
    for a .AX fund, America/New_York for ^GSPC). correlation_vs()'s
    `_monthly_returns()` never stripped that tz, so its own
    `pd.concat(..., join="inner")` joins on exact tz-aware Timestamp
    equality - two SAME-market series (both Sydney-tz, e.g. a .AX fund
    vs ^AXJO) land on identical month-end instants and join fine, but
    an ASX fund's Sydney-tz month-end and ^GSPC's New-York-tz month-end
    are DIFFERENT Timestamp objects even for "the same" calendar month
    end - zero rows ever match, hence "Corr. vs S&P 500" was always
    empty for every .AX holding, confirmed from this exact tz mechanism
    (not a live network failure - fetching ^GSPC's history and
    computing ITS OWN monthly returns both work fine in isolation, the
    join is what silently produces zero overlap). Stripping tz and
    normalizing to a plain calendar date here, rather than at the
    yfinance-history-fetch call site, keeps every other caller of this
    module's price-history plumbing (which never had this problem,
    since they only ever compare a series against itself or a
    same-market benchmark) untouched."""
    if hist is None or hist.empty or "Close" not in hist:
        return None
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        return None
    idx = closes.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    closes = closes.copy()
    closes.index = pd.DatetimeIndex(idx).normalize()
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()
    returns = closes.pct_change().dropna()
    return returns if len(returns) >= 1 else None


def correlation_vs_us_benchmark(target_hist, us_hist, min_days=60):
    """Correlation between a non-US-listed fund's (e.g. .AX) daily
    returns and a US benchmark's (^GSPC) PRECEDING completed session -
    the fix for correlation_vs()'s tz-join failure on a cross-market
    pair (see _daily_returns_naive()'s own docstring for the exact
    mechanism), plus the owner's own "an ASX trading day reflects the
    PREVIOUS US session" lag: by the time the ASX opens, the US market
    that already closed hours earlier is the economically comparable
    session, not a same-calendar-date one (the US market is usually
    still MID-session, or not yet open, at any point during the ASX's
    own trading day).

    Method: DAILY simple returns (not monthly - a lag this short needs
    day-level resolution; also naturally gives many more overlapping
    points than a 24-months-minimum monthly window would, since ETF
    price history is typically kept for ~2 years - see
    app.py._etf_benchmark_history()), joined via `pd.merge_asof(...,
    direction="backward", allow_exact_matches=False)`: for each target
    trading date T, the US return used is from the US market's own most
    recent COMPLETED session strictly BEFORE T - ordinarily US date T-1,
    but correctly falls back further over a US holiday/weekend rather
    than a hardcoded "-1 calendar day" that would misalign across one.
    min_days=60 (~3 trading months) - short enough that ~2y of daily
    history clears it easily, long enough that a handful of overlapping
    days isn't mistaken for a real correlation (same spirit as
    correlation_vs()'s own min_months, scaled from a monthly to a daily
    return series)."""
    a = _daily_returns_naive(target_hist)
    b = _daily_returns_naive(us_hist)
    if a is None or b is None:
        return None
    a_df = a.rename("target").reset_index()
    a_df.columns = ["date", "target"]
    b_df = b.rename("us").reset_index()
    b_df.columns = ["date", "us"]
    merged = pd.merge_asof(
        a_df.sort_values("date"), b_df.sort_values("date"),
        on="date", direction="backward", allow_exact_matches=False,
    )
    merged = merged.dropna(subset=["target", "us"])
    if len(merged) < min_days:
        return None
    corr = merged["target"].corr(merged["us"])
    if corr is None or (isinstance(corr, float) and np.isnan(corr)):
        return None
    return float(corr)


def benchmark_ticker_for(ticker):
    """ASX 200 for a .AX-listed fund, S&P 500 otherwise - the same
    per-listing benchmark convention the Portfolio Progress tab already
    uses (see app.py's own comment: 'ASX 200 for .AX tickers, S&P 500
    for US tickers')."""
    return "^AXJO" if (ticker or "").upper().endswith(".AX") else "^GSPC"


# -----------------------------------------------------------------
# Overlap - direct holdings vs each ETF's own top-10, cross-referenced.
# -----------------------------------------------------------------

def compute_overlap(direct_holdings, etf_holdings):
    """
    direct_holdings: {ticker: value_aud} for every DIRECT (non-ETF)
        holding in the portfolio.
    etf_holdings: {etf_ticker: (etf_value_aud, [{"symbol","weight"}, ...])}
        - one entry per ETF holding, its current AUD value and its
        top-10 (symbol, weight%) list from get_fund_facts().

    Returns {"matches": [...], "top5": [...]} - `matches` is one row per
    (direct ticker, ETF) pair where the direct ticker appears in that
    ETF's top-10, each with the indirect exposure this creates
    (etf_value * weight%) and the combined (direct + indirect) exposure
    for that underlying; `top5` is the 5 largest COMBINED exposures
    across every underlying touched by the portfolio at all (direct-
    only underlyings included, so a large direct holding with no ETF
    overlap can still rank if it's simply a big position).
    """
    matches = []
    combined = {t: float(v) for t, v in (direct_holdings or {}).items()}

    for etf_ticker, (etf_value, holdings) in (etf_holdings or {}).items():
        for h in holdings or []:
            sym = (h.get("symbol") or "").upper()
            weight = h.get("weight")
            if not sym or weight is None or sym not in direct_holdings:
                continue
            indirect_value = float(etf_value or 0) * (weight / 100.0)
            direct_value = float(direct_holdings.get(sym, 0))
            matches.append({
                "underlying": sym,
                "etf": etf_ticker,
                "weight_pct": weight,
                "direct_value": direct_value,
                "indirect_value": indirect_value,
                "combined_value": direct_value + indirect_value,
            })
            combined[sym] = combined.get(sym, direct_value) + indirect_value

    top5 = sorted(
        ({"underlying": t, "combined_value": v} for t, v in combined.items()),
        key=lambda r: r["combined_value"], reverse=True,
    )[:5]
    return {"matches": matches, "top5": top5}


# -----------------------------------------------------------------
# What-if projector - a plain compound-growth calculation, explicitly
# not a forecast (the caller is responsible for the "not a forecast"
# label; this just does the arithmetic).
# -----------------------------------------------------------------

def what_if_projection(current_value, years, annual_rate_pct):
    """current_value compounded annually at annual_rate_pct for `years`
    years. No storage, no persistence - purely a per-render calculation
    from whatever the visitor currently has the sliders/inputs set to."""
    try:
        return float(current_value) * ((1.0 + float(annual_rate_pct) / 100.0) ** float(years))
    except Exception:
        return None


_SECTOR_LABELS = {
    "realestate": "Real Estate", "consumer_cyclical": "Consumer Cyclical",
    "basic_materials": "Basic Materials", "consumer_defensive": "Consumer Defensive",
    "technology": "Technology", "communication_services": "Communication Services",
    "financial_services": "Financial Services", "utilities": "Utilities",
    "industrials": "Industrials", "energy": "Energy", "healthcare": "Healthcare",
}


def sector_label(key):
    """Yahoo's raw sector-weighting keys (e.g. 'financial_services') to
    a display label - falls back to a title-cased version of the raw
    key for any sector Yahoo adds that isn't in this fixed map yet, so
    a new sector never disappears silently, just renders less prettily
    until this map is updated."""
    return _SECTOR_LABELS.get(key, key.replace("_", " ").title())
