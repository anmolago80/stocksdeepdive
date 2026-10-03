"""
fundamentals_data.py

Source-switchable statement bundle for the auto Compounder View (see
auto_compounder_engine.py, which consumes this). Fetches one ticker's
financial statements, price history, dividends and the S&P 500's own price
history from the web, normalises them into ONE shape regardless of which
upstream source actually supplied the statements, and caches the result on
the persisted volume so the same ticker isn't re-fetched on every page
view.

get_bundle(ticker) -> {
    "info": dict,                      # yfinance .info, or {} if unavailable
    "income": DataFrame,               # annual income statement, newest column first
    "balance": DataFrame,              # annual balance sheet, newest column first
    "cashflow": DataFrame,             # annual cash flow statement, newest column first
    "prices_10y": {"dates": [...], "prices": [...]},       # ~10y monthly closes
    "dividends": {"dates": [...], "amounts": [...]},       # full dividend history
    "spx_prices_10y": {"dates": [...], "prices": [...]},   # S&P 500, same shape - for the
                                                             # Fundamentals covariance/correlation metric
    "meta": {"source": "yfinance"|"eodhd", "statement_years": int,
              "fetched_at": iso-8601 str, "flags": [str, ...]},
}

Source A (default): yfinance - .income_stmt / .balance_sheet / .cashflow /
.history() / .dividends. Annual statement depth is whatever Yahoo exposes
through yfinance - typically 4-5 years.

Source B: EODHD, used automatically when the EODHD_API_KEY environment
variable is set - GET /api/fundamentals/{SYMBOL} (".AX" tickers map to
".AU", bare US tickers map to ".US"), up to ~10 annual years, normalised
into the SAME DataFrame shape Source A produces (columns = year-end date
strings, newest first; index = line-item names) so auto_compounder_engine
never has to know which source ran. EODHD is only used when it actually
returns MORE statement years than yfinance did - a flaky or partial EODHD
response can never make the bundle worse than the yfinance-only fallback.
Prices/dividends always come from yfinance regardless of which statement
source is in use.

This module has not been exercised against live data from the sandbox this
was written in (no outbound network access there - see the module's own
test notes in the repo). Every network call is wrapped in try/except so a
partial failure degrades (fewer years, an empty DataFrame, a missing info
key) rather than raising - one flaky feed can never kill the whole bundle -
but the EODHD field-name mapping in particular should be spot-checked
against a real EODHD response the first time EODHD_API_KEY is set.
"""

import calendar
import datetime
import json
import os
import tempfile
import time
import urllib.error
import urllib.request

import pandas as pd
import yfinance as yf

import currency_risk_engine
import fcf_valuation_engine

# -----------------------------------
# Persistence - same resolution rule as every other persisted file in this
# app (see watchlist_store.py / build_compounder_data._cp_data_dir):
# prefers the attached Railway Volume, falls back to this file's own
# directory for local runs.
# -----------------------------------

_CACHE_DIR_NAME = "auto_cv_cache"
_CACHE_TTL_SECONDS = 24 * 3600

# Bumped whenever get_bundle()'s output shape/content changes in a way that
# would make an old cached bundle wrong to keep serving (e.g. the Part 6a
# currency-conversion fix below) - mirrors auto_compounder_engine's own
# ENGINE_VERSION cache-busting pattern. A cached bundle written under an
# older version is treated as a miss, same as an expired one.
#
# 5->6 (2026-08-31): the sharesOutstanding-fallback fix in
# _overlay_fresh_price()/_shares_outstanding_fallback() (CPRT's 8 vanished
# Fundamentals ratios). Confirmed live that skipping this bump means the
# fix ships invisibly: CPRT's Compounder View is cached per-ticker on the
# Railway Volume by auto_compounder_engine.py's own section cache, which
# only calls get_bundle() again (and so only re-runs this fix) once its
# stored bundle_version stops matching this constant - otherwise it keeps
# serving the pre-fix cached section for up to 24h, exactly matching
# Andrew's "still showing the same indicators, nothing changed" even
# though the deploy itself succeeded. See auto_compounder_engine.py's own
# _read_cache() comment (audit fixes 2.1/2.2) for the full mechanism this
# bump exists to trigger.
BUNDLE_VERSION = 6


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


def _cache_dir():
    path = os.path.join(_data_dir(), _CACHE_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _cache_path(ticker):
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in ticker.upper())
    return os.path.join(_cache_dir(), f"{safe}.json")


def _df_to_json(df):
    """Annual statement DataFrame -> JSON-safe dict. NaN/None become null;
    non-numeric cells are passed through as strings."""
    if df is None or df.empty:
        return None
    cols = [str(c) for c in df.columns]
    idx = [str(i) for i in df.index]
    rows = []
    for _, row in df.iterrows():
        cells = []
        for v in row.tolist():
            if v is None:
                cells.append(None)
            elif isinstance(v, float) and v != v:  # NaN
                cells.append(None)
            elif isinstance(v, (int, float)):
                cells.append(float(v))
            else:
                cells.append(str(v))
        rows.append(cells)
    return {"columns": cols, "index": idx, "data": rows}


def _df_from_json(obj):
    if not obj:
        return pd.DataFrame()
    try:
        return pd.DataFrame(obj["data"], index=obj["index"], columns=obj["columns"])
    except Exception:
        return pd.DataFrame()


def _bundle_to_cache(bundle):
    return {
        "info": bundle.get("info") or {},
        "income": _df_to_json(bundle.get("income")),
        "balance": _df_to_json(bundle.get("balance")),
        "cashflow": _df_to_json(bundle.get("cashflow")),
        "income_q": _df_to_json(bundle.get("income_q")),
        "prices_10y": bundle.get("prices_10y"),
        "dividends": bundle.get("dividends"),
        "spx_prices_10y": bundle.get("spx_prices_10y"),
        "meta": bundle.get("meta"),
    }


def _bundle_from_cache(obj):
    return {
        "info": obj.get("info") or {},
        "income": _df_from_json(obj.get("income")),
        "balance": _df_from_json(obj.get("balance")),
        "cashflow": _df_from_json(obj.get("cashflow")),
        # income_q is a newer field than some still-live cache entries -
        # .get(...) already tolerates that (None -> empty DataFrame via
        # _df_from_json), but BUNDLE_VERSION is bumped alongside this
        # anyway so old entries miss the cache and refetch with it present.
        "income_q": _df_from_json(obj.get("income_q")),
        "prices_10y": obj.get("prices_10y") or {"dates": [], "prices": []},
        "dividends": obj.get("dividends") or {"dates": [], "amounts": []},
        "spx_prices_10y": obj.get("spx_prices_10y") or {"dates": [], "prices": []},
        "meta": obj.get("meta") or {},
    }


def _read_cache(ticker):
    """Returns the cached bundle if present and younger than the 24h TTL,
    else None (including on any parse/IO error - fails through to a live
    fetch rather than raising)."""
    path = _cache_path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        cache_meta = obj.get("meta") or {}
        fetched_at = cache_meta.get("fetched_at")
        if not fetched_at:
            return None
        if cache_meta.get("bundle_version") != BUNDLE_VERSION:
            return None
        age = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.datetime.fromisoformat(fetched_at)
        ).total_seconds()
        if age > _CACHE_TTL_SECONDS or age < 0:
            return None
        return _bundle_from_cache(obj)
    except Exception:
        return None


def peek_cached_market_cap(ticker):
    """Services batch 2, Part 2 (2026-09-01): read-only, NO-FETCH peek at a
    ticker's market cap from whatever fundamentals bundle already happens
    to be cached for it on disk - used by peer_context.py, which must
    never make a live network call (it runs on every Deep Dive view).
    Unlike get_bundle()/_read_cache(), this ignores the 24h TTL and
    BUNDLE_VERSION entirely: a slightly-stale market cap is still exactly
    as useful for "which peer is closest in size", so there's no reason
    to discard a working cache entry just because the Compounder View's
    own freshness bar wouldn't accept it for real financial figures.
    Returns None if this ticker has never had a bundle cached at all, or
    the cached info has no marketCap - NEVER triggers a fetch of its own."""
    path = _cache_path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        cap = (obj.get("info") or {}).get("marketCap")
        return float(cap) if cap else None
    except Exception:
        return None


def peek_cached_bundle(ticker):
    """Read-only, NO-FETCH peek at whatever fundamentals bundle already
    happens to be cached for this ticker on disk - Commit S (21 Sep
    2026, owner-reported): the Admin Dashboard's operating-income audit
    must never make a live yfinance/EODHD call (auditing 2,000+ tickers
    live would be far too slow, and would hammer external services for
    a read-only diagnostic tool that already has no business fetching
    anything).

    Unlike get_bundle()/_read_cache(), this ignores the 24h TTL - a
    slightly stale cached bundle is still exactly as useful for
    comparing EBIT formulas against, same reasoning as peek_cached_
    market_cap() above. It still checks BUNDLE_VERSION, though (unlike
    that function): a bundle cached under an old schema could be
    missing rows or shaped differently, which is a real risk of
    silently wrong output for a numeric reconciliation tool like this
    one - not just a single scalar market-cap read.

    Returns None if this ticker has never had a bundle cached at all,
    its cache file is corrupt, or it was cached under a different
    BUNDLE_VERSION - NEVER triggers a fetch of its own. Callers (the
    audit tool) should treat None as "skip this ticker, list it as
    having no cache" rather than falling back to get_bundle()."""
    path = _cache_path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        cache_meta = obj.get("meta") or {}
        if cache_meta.get("bundle_version") != BUNDLE_VERSION:
            return None
        return _bundle_from_cache(obj)
    except Exception:
        return None


def _write_cache(ticker, bundle):
    """Atomic write (tmp file + os.replace) so a crash mid-write never
    leaves a corrupt cache file behind. Best-effort - a write failure just
    means the next view re-fetches, same as a cold cache."""
    path = _cache_path(ticker)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(_bundle_to_cache(bundle), f)
        os.replace(tmp_path, path)
    except OSError:
        pass


# Row-label substrings for the two NON-monetary row types a yfinance
# income/balance/cashflow statement can actually contain - a share count
# ("Basic Average Shares", "Diluted Average Shares", "Shares Outstanding")
# and a tax rate ("Tax Rate For Calcs"). Multiplying either by an FX rate
# would corrupt it, since neither is a dollar figure. Everything else in
# these statements (revenue, income, assets, EPS, etc.) genuinely is a
# dollar amount in the statement's own currency and does need converting.
_NON_MONETARY_ROW_PATTERNS = ("shares", "tax rate")


def _is_non_monetary_row(label):
    l = str(label).lower()
    return any(p in l for p in _NON_MONETARY_ROW_PATTERNS)


def _convert_statement_currency(df, from_ccy, to_ccy):
    """Multiply every MONETARY cell of a statement DataFrame by an fx
    rate, coercing anything non-numeric to NaN first so a stray
    string/None cell can never raise - same best-effort spirit as the
    rest of this module. Returns the input unchanged if it's empty or
    the conversion fails outright.

    Audit fix 1.7: this used to blanket-multiply the WHOLE DataFrame by
    `rate`, which would corrupt a share-count or tax-rate row if one were
    ever read from it - nothing downstream currently reads such a row
    through this path, so this was a landmine for the next metric added
    rather than a live bug (see _NON_MONETARY_ROW_PATTERNS above). Rows
    matching that list are coerced to numeric but left unconverted;
    everything else converts as before.

    B2.1 fix (27 Sep 2026, owner-directed): this used to take a single
    flat `rate` (TODAY's fx rate) and apply it to every COLUMN (fiscal
    year/quarter) alike - a statement column from 3 years ago got
    converted at today's rate, not that year's own rate. Every metric
    built from more than the newest column (historic P/E, the IV/BV
    series, Retained Earnings' multi-year windows, ...) was silently
    wrong for every year except the most recent whenever the fx rate
    had moved since. Now converts EACH column at that column's own
    period-end date, via currency_risk_engine.historical_fx_rate() -
    the same cached daily-close series the Currency Risk page already
    maintains, never a second live fetch per column."""
    if df is None or df.empty:
        return df
    try:
        numeric = df.apply(pd.to_numeric, errors="coerce")
        converted = numeric.copy()
        for col in numeric.columns:
            rate, _source = currency_risk_engine.historical_fx_rate(from_ccy, to_ccy, col)
            # Exchange-rate fallback-of-1 fix (3 Oct 2026, Director-
            # directed, Commit 6): get_bundle()'s own up-front gate
            # should already have skipped calling this function at all
            # for a pair with no basis for a rate - this is a defensive
            # backstop only (see historical_fx_rate()'s own comment on
            # why rate can still be None here in a rare edge case).
            # NaN, never a silent 1.0/None-multiply crash, for that one
            # column - same "fails soft, flags rather than invents"
            # philosophy as every other guard in this module.
            if rate is None:
                converted[col] = float("nan")
                continue
            converted[col] = numeric[col] * rate
        non_monetary = [label for label in numeric.index if _is_non_monetary_row(label)]
        if non_monetary:
            converted.loc[non_monetary] = numeric.loc[non_monetary]
        return converted
    except Exception:
        return df


# -----------------------------------
# Price / dividend series (yfinance, both sources)
# -----------------------------------

def _monthly_series(hist_df):
    """yfinance history DataFrame -> ~10y of monthly closes (the LAST
    trading close of each month, labeled at that CALENDAR month's own
    last day), same "one point per month" shape the hand-built
    workbook's own price_history series uses.

    Audit fix B2.6 (27 Sep 2026, owner-directed, CONFIRMED BUG): this used
    to be `resample("MS").first()` - the FIRST trading close of each
    month, dated to that month's start. auto_compounder_engine.
    _year_end_prices() matches each fiscal year-end to the latest point
    "on or before" that end date, so for a fiscal year ending, say,
    2024-06-30, it would land on the close from the FIRST trading day of
    June (as early as 1 Jun) rather than the close nearest the real
    30 June year-end - up to a month of price drift feeding into every
    year's PE Ratio, Fair Value's PE-based method, Retained Earnings'
    Value Created chart, and Cost of Capital's WACC-by-year, all of which
    price a fiscal year's own EPS/equity against a stale month-open quote
    instead of the closing quote that fiscal year actually ended on.
    Grouping by (year, month) and keeping each group's LAST real trading
    close (not a resample bucket's first value) fixes this without
    changing the "one point per month" granularity the rest of this
    module and _year_end_prices() rely on.

    The DATE label for that price is the calendar month's own LAST day
    (e.g. "2024-06-30"), not the real trading date the close happened on
    (which could be a few days earlier around a weekend/holiday) -
    deliberately, so a stock's monthly grid and spx_prices_10y's monthly
    grid land on IDENTICAL date strings for the same (year, month) even
    when the two exchanges' actual last trading day of that month
    differs (different public holidays). Using the true trading date as
    the label instead would silently break _cov_corr()'s date-string join
    between a stock and the S&P 500 exactly whenever their calendars
    happened to diverge - the same class of join bug the tz-strip fix
    below was already written to prevent. This still fixes the pricing
    bug above (the VALUE is always the real last trading day's close,
    never a first-of-month or interpolated one) while keeping the join
    exchange-independent.

    (Historical note this fix also resolves as a side effect: yfinance's
    `.history()` index is tz-AWARE, localized to that ticker's own
    exchange - a plain `Timestamp.isoformat()` on a tz-aware value used
    to include that offset, so the SAME calendar month produced two
    different-looking date strings depending on which exchange the
    series came from, and _cov_corr()'s date-string join between a
    stock's prices_10y and spx_prices_10y silently found zero common
    keys for every non-US ticker. Labeling by (year, month) -> calendar
    month-end directly, rather than by the tz-aware Timestamp itself,
    means every ticker's dates land on one naive, exchange-independent
    grid with no tz-stripping step needed at all.)"""
    if hist_df is None or hist_df.empty or "Close" not in hist_df.columns:
        return {"dates": [], "prices": []}
    try:
        closes = hist_df["Close"].dropna().sort_index()
        if closes.empty:
            return {"dates": [], "prices": []}
        dates, prices = [], []
        for (y, m), group in closes.groupby([closes.index.year, closes.index.month]):
            month_end = datetime.date(int(y), int(m), calendar.monthrange(int(y), int(m))[1])
            dates.append(f"{month_end.isoformat()}T00:00:00")
            prices.append(round(float(group.iloc[-1]), 4))
        return {"dates": dates, "prices": prices}
    except Exception:
        return {"dates": [], "prices": []}


# -----------------------------------
# Source A: yfinance statements
# -----------------------------------

def _fetch_yfinance_statements(tk):
    income = balance = cashflow = pd.DataFrame()
    try:
        income = tk.income_stmt
    except Exception:
        pass
    try:
        balance = tk.balance_sheet
    except Exception:
        pass
    try:
        cashflow = tk.cashflow
    except Exception:
        pass
    return (
        income if isinstance(income, pd.DataFrame) else pd.DataFrame(),
        balance if isinstance(balance, pd.DataFrame) else pd.DataFrame(),
        cashflow if isinstance(cashflow, pd.DataFrame) else pd.DataFrame(),
    )


def _fetch_yfinance_quarterly_income(tk):
    """Quarterly income statement, same shape as the annual one (columns =
    quarter-end dates, newest first) - used ONLY to build a genuine
    trailing-twelve-month figure (sum of the last 4 quarters) for line
    items where "the latest annual column" is a bad stand-in for TTM,
    e.g. interest expense right after a company takes on new debt
    mid-fiscal-year: the annual column still reflects the old, mostly
    debt-free year, while the real run-rate has already jumped. Every
    other "TTM" figure in this app still means "latest annual column" -
    this is intentionally narrow, not a wholesale TTM redefinition."""
    try:
        q = tk.quarterly_income_stmt
        return q if isinstance(q, pd.DataFrame) else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# -----------------------------------
# Source B: EODHD statements (used only when EODHD_API_KEY is set, and only
# kept if it beats yfinance's own depth for this ticker)
# -----------------------------------

def _eodhd_symbol(ticker):
    t = ticker.upper()
    if t.endswith(".AX"):
        return t[:-3] + ".AU"
    if "." not in t:
        return t + ".US"
    return t


def _eodhd_statement_to_df(payload, section):
    """EODHD's Financials.<Statement>.yearly is {date_str: {field: value,
    ...}, ...} - transpose into a DataFrame shaped like yfinance's own
    (fields as the index, up to 10 most recent year-end dates as columns,
    newest first)."""
    try:
        yearly = (((payload.get("Financials") or {}).get(section) or {}).get("yearly") or {})
        if not yearly:
            return pd.DataFrame()
        dates = sorted(yearly.keys(), reverse=True)[:10]
        skip = {"date", "filing_date", "currency_symbol"}
        fields = set()
        for d in dates:
            fields.update((yearly.get(d) or {}).keys())
        fields -= skip
        data = {}
        for d in dates:
            row = yearly.get(d) or {}
            col = {}
            for f in fields:
                v = row.get(f)
                try:
                    col[f] = float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    col[f] = None
            data[d] = col
        return pd.DataFrame(data)
    except Exception:
        return pd.DataFrame()


def _fetch_eodhd_statements(ticker, api_key):
    """Returns (income, balance, cashflow, years_found). Any piece that
    can't be parsed comes back as an empty DataFrame rather than raising."""
    symbol = _eodhd_symbol(ticker)
    url = f"https://eodhd.com/api/fundamentals/{symbol}?api_token={api_key}&fmt=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stocksdeepdive/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), 0

    income = _eodhd_statement_to_df(payload, "Income_Statement")
    balance = _eodhd_statement_to_df(payload, "Balance_Sheet")
    cashflow = _eodhd_statement_to_df(payload, "Cash_Flow")
    years_found = max(
        len(income.columns) if not income.empty else 0,
        len(balance.columns) if not balance.empty else 0,
        len(cashflow.columns) if not cashflow.empty else 0,
    )
    return income, balance, cashflow, years_found


# -----------------------------------
# Public entry point
# -----------------------------------

# Audit fix 2.7: unlike every other feed in this module, this is
# deliberately called on EVERY get_bundle() invocation AND on every
# build_sections() cache-hit check (get_live_price(), added for fix 2.1) -
# "always live" is the whole point (see the docstring below), so it can't
# use the normal 24h/section-level caching. But that also means it was an
# unthrottled synchronous Yahoo call with no backoff on any cold-cache
# path or warm-cache staleness check. A short TTL, keyed by ticker, is
# long enough to collapse a burst of concurrent renders for the same
# ticker (several visitors hitting the same popular stock within a few
# seconds) down to one live fetch, but short enough to still catch a real
# intraday move almost immediately - the exact tradeoff this whole
# mechanism exists for.
_FRESH_PRICE_CACHE = {}
_FRESH_PRICE_TTL_SECONDS = 90


def _fetch_fresh_price(tk):
    """A live-ish current price via yfinance's fast_info, which is backed by
    a lighter/faster-updating Yahoo endpoint than .info's own quoteSummary
    - unlike the rest of this bundle, this is deliberately called on EVERY
    get_bundle() invocation, cache hit or not, and patched over whatever
    price .info carries. Reason: the bundle (including .info, hence its
    price fields) is cached for 24h, and on a day with a large intraday
    move that leaves every price-derived Compounder View metric (PE-style
    ratios, market-cap-based WACC, Value vs Book) silently priced off a
    stale pre-move quote for up to 24h - confirmed live on OCL.AX, which
    dropped ~17% intraday while the cached bundle's .info still carried the
    prior close. fast_info's own several small internal requests are cheap
    enough to run unconditionally. Returns None on any failure (missing
    ticker, network hiccup, unexpected fast_info shape) - callers must
    treat that as "keep whatever price was already in info", not as a
    reason to fail the whole bundle.

    Cached for _FRESH_PRICE_TTL_SECONDS (see the constants above this
    function - audit fix 2.7) so a burst of concurrent requests for the
    same ticker collapses to one live fast_info call."""
    symbol = getattr(tk, "ticker", None)
    now = time.time()
    if symbol:
        cached = _FRESH_PRICE_CACHE.get(symbol)
        if cached is not None:
            price, fetched_at = cached
            if now - fetched_at < _FRESH_PRICE_TTL_SECONDS:
                return price

    price = None
    try:
        fi = tk.fast_info
        for key in ("last_price", "lastPrice", "regularMarketPrice"):
            val = None
            try:
                val = fi[key]
            except Exception:
                pass
            if val is None:
                val = getattr(fi, key, None)
            if isinstance(val, (int, float)) and val > 0:
                price = float(val)
                break
    except Exception:
        price = None

    if symbol:
        _FRESH_PRICE_CACHE[symbol] = (price, now)
    return price


def get_live_price(ticker):
    """Public, cheap wrapper around _fetch_fresh_price() for callers that
    need to know "has this ticker moved" WITHOUT paying for a full
    get_bundle() call (statements, dividends, 10y monthly history, ...).

    Added for auto_compounder_engine.build_sections()'s cache-hit path: its
    24h section cache was found to short-circuit BEFORE get_bundle() is
    ever called (see build_sections()'s own comment), which meant the
    "always overlay a fresh price, cache hit or not" mechanism this module
    was built around (see _fetch_fresh_price's docstring - the OCL.AX
    incident) never actually ran for a returning visitor within that 24h
    window. build_sections() now uses this to cheaply check the live price
    against the price its cached section was built against, and forces a
    rebuild when they've diverged meaningfully - same intent as
    _fetch_fresh_price, reachable from a warm cache instead of only a cold
    one. Returns None on any failure, same fail-open contract as
    _fetch_fresh_price itself."""
    try:
        return _fetch_fresh_price(yf.Ticker(ticker))
    except Exception:
        return None


def _shares_outstanding_fallback(info, income):
    """Best-effort share count for the handful of tickers where Yahoo's
    .info blob doesn't carry sharesOutstanding at all - root-caused live
    on CPRT (2026-08-31): its info blob had no sharesOutstanding, which
    silently broke far more than just the field itself. _overlay_fresh_price
    below, unable to recompute marketCap without a share count, was
    POPPING it - which cascaded into 6 Fundamentals ratios vanishing
    outright (add() drops a metric entirely when its value is None, same
    "missing" symptom the OCL.AX/CapEx-to-OCF incident hit): Price to
    Sales ratio, Market Cap/Tangible Asset Value, EV To Free Cash Flow,
    Free Cash Flow Yield, PFCF Ratio and Price to Equity Ratio all divide
    by (or build) mcap. Separately, auto_compounder_engine._bvps() reads
    info.get("sharesOutstanding") directly for Book Value Per Share/
    1.5xBV, so those two vanished too - 8 missing metrics from one absent
    upstream field, confirmed by diffing CPRT's live Fundamentals grid
    against OCL.AX's (which has the field and shows all of them).

    Two independent, already-available sources are tried before giving
    up: Yahoo's own impliedSharesOutstanding field (a separate
    quoteSummary field that's sometimes populated when sharesOutstanding
    isn't), then the income statement's own Diluted/Basic Average Shares
    row - the company's own filed share count, already trusted elsewhere
    in this codebase for the dual-class market-cap correction (see
    auto_compounder_engine.py's dual_class_mcap_fix, which reads the same
    row). Returns None (not a guess) if neither source has anything
    usable - callers keep today's existing no-marketCap fail-open
    behaviour in that case."""
    shares = info.get("sharesOutstanding")
    if isinstance(shares, (int, float)) and shares > 0:
        return shares
    implied = info.get("impliedSharesOutstanding")
    if isinstance(implied, (int, float)) and implied > 0:
        return implied
    if income is not None and not income.empty:
        for key in ("Diluted Average Shares", "Basic Average Shares"):
            if key in income.index:
                try:
                    row = income.loc[key].dropna()
                    if not row.empty:
                        val = float(row.iloc[0])
                        if val > 0:
                            return val
                except Exception:
                    pass
    return None


def _overlay_fresh_price(info, tk, income=None):
    """Patches info["currentPrice"] with a fresh fast_info quote (see
    _fetch_fresh_price) and RECOMPUTES info["marketCap"] from that fresh
    price x sharesOutstanding, rather than leaving Yahoo's own (possibly
    stale, pre-move) cached marketCap in place. Confirmed live on OCL.AX:
    after an earlier version of this fix that only patched currentPrice,
    Market Cap/TAV and Price to Equity Ratio still worked out to the old
    ~$7.46 close, because they were routing through this untouched cached
    field the whole time.

    An even earlier version of this fix just POPPED marketCap instead of
    recomputing it, on the theory that _basics() in
    auto_compounder_engine.py already recomputes price*shares itself when
    marketCap is missing - true, but that recomputed value only lives
    inside _basics()'s own returned dict, never written back into `info`.
    fcf_valuation_engine.growth_ceiling_for(info, ...) reads
    info.get("marketCap") directly (it has no such fallback), so popping
    the key meant it silently saw "no market cap" on EVERY ticker whenever
    a fresh price was available - which is essentially always, since
    fast_info rarely fails - and fell back to the flat 20% small-cap
    growth ceiling regardless of the company's actual size. Confirmed on
    CPU.AX: a large-cap (ASX-listed, market cap well over the USD $10B
    large-cap threshold) that should get the ~12% ceiling instead got 20%,
    which is why its Compounder View DCF/PE Forward/Rational Compounder
    Method/IV-BV all ran high versus the main site's own Intrinsic Value
    (which fetches `info` fresh via a separate, unmodified yf.Ticker(...).
    info call in app.py's get_ticker_info() and so was never affected).

    Recomputing (instead of popping) keeps both consumers correct: a
    stale cached marketCap is still never used, but a real, current one
    is always available.

    Stage 1a (3 Oct 2026, Director-directed, UK/Canada data layer): when
    `info["_price_quote_unit"] == "GBp"` (set by _normalize_pence_price_
    fields() when this bundle was first built - see that function's own
    docstring), the FRESH fast_info quote gets the same /100 treatment
    the rest of this ticker's price fields already got, since Yahoo's
    live feed itself is untouched by that normalisation - it still
    returns this ticker's price in pence on every call, cached bundle or
    not. Runs on EVERY call (this function's own "deliberately called
    unconditionally" contract, unchanged), including a cache-hit replay,
    which is exactly why the marker has to survive the cache round-trip
    on `info` itself rather than living only in the bundle's separate
    `meta` dict (this function never sees `meta`).

    `income` (optional, the bundle's annual income-statement DataFrame):
    passed through to _shares_outstanding_fallback() so a ticker whose
    .info blob is simply missing sharesOutstanding (confirmed live on
    CPRT - see that function's own docstring) still gets a real,
    non-estimated share count instead of losing marketCap entirely. The
    backfilled share count is also written back into
    info["sharesOutstanding"] itself, so every other direct
    info.get("sharesOutstanding") reader downstream (auto_compounder_
    engine.py's _basics()/_bvps()) benefits from the same fallback
    without needing its own copy of this logic. Falls back to popping
    marketCap only when no share count can be found anywhere, matching
    the original fail-safe intent. Mutates and returns info; a no-op
    (returns info unchanged) if no fresh price is available."""
    fresh_price = _fetch_fresh_price(tk)
    if fresh_price is not None:
        if info.get("_price_quote_unit") == "GBp":
            fresh_price = fresh_price / 100.0
        info["currentPrice"] = fresh_price
        shares = _shares_outstanding_fallback(info, income)
        if isinstance(shares, (int, float)) and shares > 0:
            info["marketCap"] = fresh_price * shares
            info["sharesOutstanding"] = shares
        else:
            info.pop("marketCap", None)
    return info


# =====================================================================
# Stage 1a (3 Oct 2026, Director-directed, UK/Canada data layer): Yahoo
# quotes most London Stock Exchange prices in PENCE, with currency ==
# "GBp" (occasionally "GBX") - detected by that EXACT currency code
# only, NEVER by magnitude (a handful of LSE names, e.g. some
# investment trusts, genuinely quote in whole pounds with currency ==
# "GBP" and must pass through untouched). Normalised ONCE, here, where
# Yahoo data enters the app - see get_bundle()'s own call site for
# exactly where in its pipeline this runs (after the fresh-price
# overlay, before the financial/listing-currency statement conversion
# that already existed).
#
# Every consumer elsewhere in this app (fcf_valuation_engine.py/
# capm_engine.py/auto_compounder_engine.py/...) already uppercases
# info.get("currency") before using it as a dict key ("GBp".upper() ==
# "GBP"), which accidentally already masks the CURRENCY-CODE half of
# this bug - a GBp ticker already gets treated as "GBP" for FX/risk-
# free/perpetual-rate lookup purposes even without this fix. The
# actual, un-masked bug is the PRICE VALUE itself: nothing anywhere
# divided by 100, so every per-share price/ratio for a GBp ticker was
# exactly 100x too high.
# =====================================================================

_PENCE_CURRENCY_CODES = ("GBp", "GBX")

# Every info-blob field that is a PRICE (per-share, in the quote
# currency) - divided by 100 for a pence-quoted ticker. Explicitly NOT
# included: marketCap, enterpriseValue, sharesOutstanding (never per-
# share prices), and anything from the financial statements (handled by
# the pre-existing _convert_statement_currency() instead - statements
# are never denominated in pence regardless of the LISTING currency's
# own pence/pounds convention). trailingEps/forwardEps/bookValue/
# dividendRate/trailingAnnualDividendRate are handled separately below
# (their unit on a GBp ticker isn't confirmed to follow this same
# plain-pence convention).
_PENCE_PRICE_INFO_FIELDS = (
    "currentPrice", "regularMarketPrice", "previousClose",
    "regularMarketPreviousClose", "open", "regularMarketOpen",
    "dayHigh", "regularMarketDayHigh", "dayLow", "regularMarketDayLow",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage",
    "twoHundredDayAverage", "bid", "ask",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice", "targetMedianPrice",
)

# dividendRate/trailingAnnualDividendRate: same plain per-share-in-
# quote-currency convention as the price fields above (Yahoo's own
# stats page always pairs these directly against the price) - so these
# DO get the same divide-by-100 treatment, unlike trailingEps/
# forwardEps/bookValue just below, whose unit isn't confirmed to follow
# that same convention.
_PENCE_DIVIDEND_INFO_FIELDS = ("dividendRate", "trailingAnnualDividendRate")

# Per-share fundamentals whose unit on a GBp ticker is NOT confirmed -
# the Director's own explicit instruction: "do not assume". Dropped
# (never left in place carrying an unconfirmed unit) rather than
# divided. trailingEps/bookValue are then re-derived from this
# ticker's own statements by _derive_eps_bookvalue_from_statements()
# below (see get_bundle()'s own call site for where); forwardEps has no
# statement-derived substitute (no forecast line in a financial
# statement) and is simply left dropped.
_PENCE_UNCONFIRMED_UNIT_INFO_FIELDS = ("trailingEps", "forwardEps", "bookValue")


def _is_pence_quoted(raw_currency):
    """True iff `raw_currency` (info.get("currency"), READ BEFORE anyone
    has upper()-cased it) is EXACTLY "GBp" or "GBX" - never a magnitude
    guess. Some LSE names genuinely quote in whole pounds (currency ==
    "GBP") and must return False here."""
    return raw_currency in _PENCE_CURRENCY_CODES


def _normalize_pence_price_fields(info):
    """Stage 1a GBp->GBP normalisation, price-field half. Mutates and
    returns (info, price_quote_unit) where price_quote_unit is "GBp" or
    None. A no-op (info unchanged, None returned) when info["currency"]
    isn't exactly "GBp"/"GBX" - see _is_pence_quoted()'s own docstring -
    so every existing USD/AUD ticker's info is completely untouched by
    this function (T7's own regression requirement).

    Divides every field in _PENCE_PRICE_INFO_FIELDS/_PENCE_DIVIDEND_
    INFO_FIELDS by 100 (only the ones actually present - a missing
    field stays missing, never fabricated), rewrites info["currency"]
    to "GBP", and sets info["_price_quote_unit"] = "GBp" - an INTERNAL
    marker (not part of this module's own public bundle shape) that
    _overlay_fresh_price() reads on every subsequent call (including a
    cache-hit replay, since `info` - and this marker with it - round-
    trips through the JSON cache file faithfully) so a fresh fast_info
    quote, which Yahoo still returns in pence regardless of this
    normalisation, gets the same /100 treatment every time."""
    raw_ccy = info.get("currency")
    if not _is_pence_quoted(raw_ccy):
        return info, None
    for field in _PENCE_PRICE_INFO_FIELDS + _PENCE_DIVIDEND_INFO_FIELDS:
        val = info.get(field)
        if isinstance(val, (int, float)) and val == val:   # not NaN
            info[field] = val / 100.0
    # Lists & display Commit 4 (3 Oct 2026, Director-directed): forwardEps
    # is about to be popped below for the same "unit not confirmed" reason
    # as trailingEps/bookValue - stashed first as an internal marker so
    # _resolve_forward_eps_unit() (called later in get_bundle(), AFTER
    # trailingEps has been re-derived in pounds from this ticker's own
    # statements) has the raw value to resolve against. Never left in
    # `info`'s own public shape under its real name until resolved.
    _raw_forward_eps = info.get("forwardEps")
    if isinstance(_raw_forward_eps, (int, float)) and _raw_forward_eps == _raw_forward_eps:
        info["_raw_forward_eps_unconfirmed"] = _raw_forward_eps
    for field in _PENCE_UNCONFIRMED_UNIT_INFO_FIELDS:
        info.pop(field, None)
    info["currency"] = "GBP"
    info["_price_quote_unit"] = "GBp"
    return info, "GBp"


def _latest_statement_row_value(df, label_hints, exclude_hints=()):
    """Tolerant substring match on a statement DataFrame's own row
    labels (same "don't know the exact label in advance" convention
    capm_engine's own RBA/BoE column-finders use) - the latest (first)
    column's value of the first row whose lowercased label contains any
    of `label_hints` and none of `exclude_hints`. None if no matching
    row has a usable (non-NaN) value in its latest column."""
    if df is None or df.empty:
        return None
    for row_label in df.index:
        label = str(row_label).lower()
        if any(h in label for h in label_hints) and not any(e in label for e in exclude_hints):
            try:
                series = df.loc[row_label].dropna()
                if not series.empty:
                    return float(series.iloc[0])
            except Exception:
                continue
    return None


def _derive_eps_bookvalue_from_statements(info, income, balance):
    """Stage 1a (3 Oct 2026, Director-directed): for a GBp-normalised
    ticker, trailingEps/bookValue were just POPPED from `info` (see
    _normalize_pence_price_fields() above) because their unit on a GBp
    ticker isn't confirmed - re-derives both directly from this
    ticker's own statements instead of trusting the raw info field.
    Mutates and returns `info`. A no-op for any ticker without
    info["_price_quote_unit"] == "GBp" - every existing USD/AUD
    ticker's info is untouched (T7's own regression requirement).

    Must run AFTER the pre-existing financial-currency->listing-
    currency statement conversion (get_bundle()'s own call site order),
    so `income`/`balance` are already in the same GBP listing currency
    _normalize_pence_price_fields() just normalised the price to.

    trailingEps: latest column's Net Income / info["sharesOutstanding"]
    - a plain basic-EPS approximation, not Yahoo's own (possibly non-
    GAAP-adjusted) trailingEps, but genuinely sourced from this
    ticker's own statements rather than an unconfirmed-unit guess.
    bookValue: latest column's Stockholders Equity / sharesOutstanding,
    same convention. Leaves a field simply absent (never fabricates a
    number) when the needed row or a usable share count can't be found
    - same fail-safe philosophy as every other derived field in this
    module."""
    if info.get("_price_quote_unit") != "GBp":
        return info
    shares = info.get("sharesOutstanding")
    if not isinstance(shares, (int, float)) or shares <= 0:
        return info

    net_income = _latest_statement_row_value(
        income, ("net income",), exclude_hints=("discontinued", "noncontrolling", "minority")
    )
    if net_income is not None:
        info["trailingEps"] = net_income / shares

    equity = _latest_statement_row_value(
        balance, ("stockholders equity", "total equity", "common stock equity")
    )
    if equity is not None:
        info["bookValue"] = equity / shares

    return info


# Lists & display Commit 4 (3 Oct 2026, Director-directed): Stage 1a
# dropped forwardEps on every GBp ticker because its unit was unconfirmed
# (same "do not assume" rule as trailingEps/bookValue), leaving PE Forward
# blank for most London stocks. Resolved here against a figure whose unit
# IS known - the statement-derived trailing EPS in pounds _derive_eps_
# bookvalue_from_statements() just computed - rather than against the
# raw, still-GBp info["trailingEps"] (which was already popped and never
# trusted in the first place).
_FWD_EPS_POUNDS_RATIO_BAND = (0.2, 5.0)
_FWD_EPS_PENCE_RATIO_BAND = (20.0, 500.0)


def _resolve_forward_eps_unit(ticker, info, log=print):
    """Resolves info["_raw_forward_eps_unconfirmed"] (stashed by
    _normalize_pence_price_fields() before the real forwardEps field was
    popped) against info["trailingEps"] - MUST run after _derive_eps_
    bookvalue_from_statements() so that figure is already the statement-
    derived, pounds-denominated one (get_bundle()'s own call order).

    r = raw forwardEps / trailing EPS (pounds):
      0.2 <= r <= 5   -> forwardEps is already in pounds, used as-is.
      20 <= r <= 500  -> forwardEps is in pence, divided by 100.
      otherwise (including a zero/negative/missing trailing EPS) ->
        forwardEps left unset (n/a) - no confident unit, no guess.

    Sets info["forwardEps"] only when confidently resolved (absent
    otherwise - never a fabricated placeholder), and always removes the
    internal "_raw_forward_eps_unconfirmed" marker either way. Logs
    exactly one "[units] <ticker> forwardEps raw=... trailing_gbp=...
    ratio=... unit=... used=..." line per call (per this task's own
    specified format) whenever there was a raw value to resolve at all -
    even on the "unknown" branch, so a future ambiguous reading is
    self-diagnosing without a follow-up investigation, same philosophy
    as _check_price_unit_guard()'s own log line.

    A no-op (info unchanged, returns (info, None)) for any ticker with no
    stashed raw forwardEps at all - every non-GBp ticker, and a GBp
    ticker whose raw info never had a forwardEps to begin with. The pre-
    existing GBp PRICE rule (currency code only, never magnitude) is
    untouched - this only ever touches forwardEps.

    Returns (info, forward_eps_unit) where forward_eps_unit is
    "pounds"|"pence"|"unknown"|None (None = nothing to resolve)."""
    raw = info.pop("_raw_forward_eps_unconfirmed", None)
    if raw is None:
        return info, None

    trailing = info.get("trailingEps")
    ratio = None
    unit = "unknown"
    used = None
    if isinstance(trailing, (int, float)) and trailing > 0:
        ratio = raw / trailing
        lo, hi = _FWD_EPS_POUNDS_RATIO_BAND
        plo, phi = _FWD_EPS_PENCE_RATIO_BAND
        if lo <= ratio <= hi:
            unit = "pounds"
            used = raw
        elif plo <= ratio <= phi:
            unit = "pence"
            used = raw / 100.0

    if used is not None:
        info["forwardEps"] = used
    if log is not None:
        _trailing_str = f"{trailing:.4g}" if isinstance(trailing, (int, float)) else "n/a"
        _ratio_str = f"{ratio:.4g}" if ratio is not None else "n/a"
        _used_str = f"{used:.4g}" if used is not None else "n/a"
        log(
            f"[units] {ticker} forwardEps raw={raw:.4g} trailing_gbp={_trailing_str} "
            f"ratio={_ratio_str} unit={unit} used={_used_str}"
        )
    return info, unit


def _normalize_and_validate_pence_dividends(dividends, info, log=print):
    """Stage 1a (3 Oct 2026, Director-directed): for a GBp-normalised
    ticker, `dividends` (the per-event history series, fetched in the
    quote currency - pence, same as the raw price fields) gets the same
    /100 treatment, THEN validated: the trailing-12-month total (from
    the now-pounds amounts) implies a dividend yield against
    info["currentPrice"] (already normalised to pounds by this point) -
    that implied yield must agree with Yahoo's own info["dividendYield"]
    within 20% relative, or the pence-normalised amounts aren't trusted
    after all (dividend_unit_suspect=True, dividends/dividendRate/
    trailingAnnualDividendRate all cleared to "n/a" by the caller - see
    get_bundle()'s own call site for exactly where that happens).

    A no-op (dividends unchanged, dividend_unit_suspect=False) for any
    non-GBp ticker, a GBp ticker with no dividend history at all
    (nothing to validate), or one with no info["dividendYield"]/price
    to compare against (can't validate either way - left as the plain
    /100-normalised amounts, not flagged, since "can't confirm" isn't
    the same claim as "confirmed wrong").

    Returns (dividends, dividend_unit_suspect)."""
    if info.get("_price_quote_unit") != "GBp":
        return dividends, False
    amounts = dividends.get("amounts") or []
    dates = dividends.get("dates") or []
    if not amounts:
        return dividends, False

    pence_amounts = [a / 100.0 for a in amounts]
    normalised = {"dates": dates, "amounts": pence_amounts}

    price = info.get("currentPrice")
    y_yield = info.get("dividendYield")
    if not (isinstance(price, (int, float)) and price > 0
            and isinstance(y_yield, (int, float)) and y_yield > 0):
        return normalised, False

    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=366)
        ttm_total = sum(
            amt for d, amt in zip(dates, pence_amounts)
            if datetime.datetime.fromisoformat(d) >= cutoff
        )
    except Exception:
        return normalised, False
    if ttm_total <= 0:
        return normalised, False

    implied_yield = ttm_total / price
    # Yahoo's own dividendYield has itself been seen in both a decimal
    # fraction (0.034) and a bare-percentage (3.4) shape elsewhere in
    # this codebase (see capm_engine._normalize_yahoo_growth_estimate's
    # own docstring for the general pattern) - normalise the same way
    # here before comparing.
    y_yield_frac = y_yield / 100.0 if abs(y_yield) >= 1.5 else y_yield
    if y_yield_frac <= 0:
        return normalised, False

    rel_diff = abs(implied_yield - y_yield_frac) / y_yield_frac
    if rel_diff > 0.20:
        log(
            f"[units] dividend_unit_suspect: implied yield {implied_yield:.4f} vs "
            f"Yahoo dividendYield {y_yield_frac:.4f} - disagree by {rel_diff:.0%}"
        )
        return {"dates": [], "amounts": []}, True

    return normalised, False


def _check_price_unit_guard(ticker, info, raw_price_before_divide, log=print):
    """Stage 1a (3 Oct 2026, Director-directed) runtime guard -
    VALIDATION, not detection (detection is the exact "GBp"/"GBX"
    currency-code check in _is_pence_quoted() above; this never decides
    WHETHER to normalise, only whether the result makes sense). For a
    ticker _normalize_pence_price_fields() just normalised, cross-checks
    price (now in pounds) x sharesOutstanding against marketCap - if
    they disagree by more than 5%, something about this normalisation
    can't be trusted, so the caller (get_bundle()) flags the whole
    bundle rather than let a silently-wrong IV/MOS compute on top of it.

    Stage 1a-fix (3 Oct 2026, Director-directed, F2): the log line now
    also carries shares/mcap/ratio, not just the (price*shares)/mcap
    ratio under the name "mcap_check" - the live SHEL.L/TSCO.L SUSPECT
    readings (ratio 0.01, not ~1.00 as expected for a ticker whose own
    marketCap is already pounds-denominated) needed those raw figures
    to actually confirm the root cause: get_bundle()'s own call site
    used to run _overlay_fresh_price() BEFORE this ticker's "_price_
    quote_unit" marker was set (see get_bundle()'s own comment at its
    call site), so on a ticker's first-ever fetch the fresh fast_info
    quote was still in pence when it got multiplied into marketCap,
    corrupting it to a 100x-too-large pence-scale figure that this
    guard's OWN price (already correctly divided by then) could never
    agree with. Fixed by reordering that call site; this log line's
    extra fields are kept so the next live SUSPECT reading (if any)
    is self-diagnosing without a follow-up investigation.

    Logs exactly one line per normalised ticker, success or failure:
    "[units] <TICKER> GBp->GBP raw=<raw> price=<normalised>
    shares=<shares> mcap=<marketCap> ratio=<ratio> ok"/"SUSPECT" -
    `raw` is the PRE-divide price (what Yahoo actually returned),
    `price` the post-divide pounds figure, `ratio` = (price * shares) /
    marketCap (1.00 == perfect agreement).

    Returns (price_unit_suspect: bool, reason: str or None). A no-op
    (False, None, nothing logged) when marketCap or sharesOutstanding
    isn't available at all - nothing to cross-check against, same
    "missing data never blocks a valuation on its own" philosophy as
    the rest of this app; this guard can only ever ADD a red flag,
    never silently clear one."""
    price = info.get("currentPrice")
    shares = info.get("sharesOutstanding")
    market_cap = info.get("marketCap")
    if not (isinstance(price, (int, float)) and isinstance(shares, (int, float))
            and isinstance(market_cap, (int, float)) and market_cap > 0 and shares > 0):
        return False, None
    ratio = (price * shares) / market_cap
    suspect = abs(ratio - 1.0) > 0.05
    log(
        f"[units] {ticker} GBp->GBP raw={raw_price_before_divide} price={price:.2f} "
        f"shares={shares:.0f} mcap={market_cap:.0f} ratio={ratio:.2f} "
        f"{'SUSPECT' if suspect else 'ok'}"
    )
    if suspect:
        return True, "price unit could not be confirmed"
    return False, None


def normalize_pence_quote(ticker, info, history_df=None, dividends=None, log=print):
    """Stage 1a-fix (3 Oct 2026, Director-directed, F1): the SAME GBp->
    GBP pence normalisation get_bundle() applies inline to its own
    bundle (see that function's own call site), exposed here as a
    public, no-fetch, pure-mutation entry point for every OTHER path in
    this codebase that reads a raw Yahoo info/.history()/.dividends()
    quote OUTSIDE get_bundle()'s own pipeline - confirmed live on
    SHEL.L/TSCO.L: the Deep Dive headline (deep_dive_engine.analyze(),
    via app.py's get_ticker_info()/get_price_history() - a raw,
    uncached-by-this-module yfinance fetch, a DIFFERENT path from
    fundamentals_data.get_bundle()'s own bundle) and nightly_scan.
    analyze_ticker_lite() (its own standalone tk.info/tk.history() fetch,
    duplicated rather than imported from app.py - see that function's
    own docstring) both showed a pence price next to a genuinely-pounds
    Intrinsic Value, because Stage 1a's own pence fix only ever reached
    get_bundle()'s bundle, never these two callers' raw fetches. Both
    now call this immediately after their own raw fetch, before any
    price/currency-derived computation (current_price, MOS, fear/greed,
    dividend_ttm, reverse DCF, ...) runs on top of it - see this fix
    commit's own report for the exact call sites.

    Mutates and returns (info, history_df, dividends, meta):
      - info: the same mutation _normalize_pence_price_fields()/_check_
        price_unit_guard() apply inside get_bundle() - price fields
        /100, currency rewritten to "GBP", trailingEps/forwardEps/
        bookValue dropped (no statement re-derivation here - unlike
        get_bundle(), these callers' own cashflow_df-based DCF already
        converts a GBp ticker's FCF correctly via fcf_valuation_
        engine's own financialCurrency->trading-currency fx_rate() call,
        independent of this function; EPS/book value are display-only
        fields on these two pages, left dropped rather than guessed,
        same "do not assume" rule as get_bundle()'s own fields).
      - history_df: a COPY with Open/High/Low/Close/Adj Close divided
        by 100 (whichever columns are actually present) - same price
        convention as the plain info fields. None in, None out.
      - dividends: a COPY of a raw yfinance dividends Series, divided
        by 100 and validated against info["dividendYield"] the same way
        _normalize_and_validate_pence_dividends() validates get_
        bundle()'s own dividend series - cleared to an empty Series on
        disagreement (dividend_unit_suspect=True). None in, None out.
      - meta: {"price_quote_unit", "price_unit_suspect",
        "price_unit_suspect_reason", "dividend_unit_suspect"} - same
        shape as get_bundle()'s own bundle["meta"].

    A no-op (info/history_df/dividends unchanged, every meta flag
    False/None) for any ticker whose raw info["currency"] isn't exactly
    "GBp"/"GBX" - see _is_pence_quoted()'s own docstring - so every
    existing USD/AUD ticker's Deep Dive headline/nightly scan row is
    completely unaffected by this function. Never raises - every step
    already fails safe on missing/malformed data, same philosophy as
    every other function in this module."""
    raw_price_for_guard = info.get("currentPrice")
    info, price_quote_unit = _normalize_pence_price_fields(info)

    meta = {
        "price_quote_unit": price_quote_unit,
        "price_unit_suspect": False,
        "price_unit_suspect_reason": None,
        "dividend_unit_suspect": False,
    }
    if price_quote_unit != "GBp":
        return info, history_df, dividends, meta

    suspect, reason = _check_price_unit_guard(ticker, info, raw_price_for_guard, log=log)
    meta["price_unit_suspect"] = suspect
    meta["price_unit_suspect_reason"] = reason

    if history_df is not None and not history_df.empty:
        history_df = history_df.copy()
        for col in ("Open", "High", "Low", "Close", "Adj Close"):
            if col in history_df.columns:
                history_df[col] = history_df[col] / 100.0

    if dividends is not None and not dividends.empty:
        divs_dict = {
            "dates": [d.isoformat() for d in dividends.index.to_pydatetime()],
            "amounts": [float(v) for v in dividends.values],
        }
        _normalised_dict, dividend_unit_suspect = _normalize_and_validate_pence_dividends(
            divs_dict, info, log=log
        )
        meta["dividend_unit_suspect"] = dividend_unit_suspect
        if dividend_unit_suspect:
            dividends = dividends.iloc[0:0]
            info.pop("dividendRate", None)
            info.pop("trailingAnnualDividendRate", None)
        else:
            dividends = dividends / 100.0

    return info, history_df, dividends, meta


def get_bundle(ticker, force_refresh=False):
    """The statement/price/dividend bundle for one ticker - see the module
    docstring for the exact shape. Cached 24h on the persisted volume;
    pass force_refresh=True to bypass the cache (e.g. an admin rebuild
    action, mirroring compounder_data.json's own rebuild pattern).

    The price fields inside the (potentially 24h-stale) cached bundle are
    always overlaid with a fresh fast_info quote before returning - see
    _fetch_fresh_price's docstring. This runs on every call, cached or not,
    so it takes effect immediately on deploy without waiting for any cache
    to expire and without needing a BUNDLE_VERSION bump."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None

    if not force_refresh:
        cached = _read_cache(ticker)
        if cached is not None:
            cached["info"] = _overlay_fresh_price(
                cached.get("info") or {}, yf.Ticker(ticker), income=cached.get("income")
            )
            return cached

    flags = []
    tk = yf.Ticker(ticker)

    try:
        info = tk.info or {}
        if not isinstance(info, dict):
            info = {}
    except Exception:
        info = {}
        flags.append("info_unavailable")

    income, balance, cashflow = _fetch_yfinance_statements(tk)
    statement_years = max(
        len(income.columns) if not income.empty else 0,
        len(balance.columns) if not balance.empty else 0,
        len(cashflow.columns) if not cashflow.empty else 0,
    )
    source = "yfinance"

    api_key = os.environ.get("EODHD_API_KEY")
    if api_key:
        e_income, e_balance, e_cashflow, e_years = _fetch_eodhd_statements(ticker, api_key)
        if e_years > statement_years:
            income, balance, cashflow, statement_years = e_income, e_balance, e_cashflow, e_years
            source = "eodhd"
        elif e_years == 0:
            flags.append("eodhd_unavailable")

    if statement_years == 0:
        flags.append("no_statements")

    # Stage 1a-fix (3 Oct 2026, Director-directed, F2): GBp->GBP pence
    # normalisation now runs BEFORE the fresh-price overlay just below -
    # reordered from Stage 1a's own original sequence (overlay, then
    # normalise), which is what produced the live SHEL.L/TSCO.L
    # mcap_check=0.01 bug. Root cause: _overlay_fresh_price() recomputes
    # info["marketCap"] = fresh_price * shares on EVERY call, and only
    # divides fresh_price by 100 first when info["_price_quote_unit"]
    # == "GBp" is ALREADY set - a marker _normalize_pence_price_fields()
    # itself sets. On a ticker's first-ever fetch (this cold-fetch path,
    # not the cache-hit path above, which already has the marker from a
    # prior run), the marker didn't exist yet when the overlay used to
    # run first, so the overlay multiplied the still-in-pence fresh
    # quote straight into marketCap - corrupting it to a 100x-too-large
    # pence-scale figure. The later currentPrice/100 division (old
    # order: normalise ran AFTER the overlay) fixed the price but never
    # touched that already-corrupted marketCap (by design - marketCap
    # is never itself divided, see _PENCE_PRICE_INFO_FIELDS's own
    # exclusion list), so _check_price_unit_guard()'s price*shares/
    # marketCap cross-check came out at price/raw_price = 1/100 = 0.01
    # instead of ~1.00. Normalising FIRST means the marker is already
    # set by the time the overlay runs, so its own fresh_price/100
    # branch fires and marketCap gets recomputed from the CORRECT,
    # already-divided price - exactly as today's T1-T3 fixtures already
    # expect (those fixtures' own fast_info raises AttributeError, so
    # _fetch_fresh_price returns None and the overlay never exercises
    # this path at all - which is why this ordering bug shipped past
    # Stage 1a's own test suite and only showed up against a real,
    # live fast_info quote). `raw_price_for_guard` is the PRE-divide
    # price - .info's own currentPrice, captured before either step
    # touches it - for the runtime guard's own log line below.
    raw_price_for_guard = info.get("currentPrice")
    info, price_quote_unit = _normalize_pence_price_fields(info)

    # Same fresh-quote overlay as the cache-hit path above (see
    # _overlay_fresh_price's docstring) - .info's own price/marketCap
    # fields can lag even on a live fetch, so this isn't just a
    # cache-staleness patch. Runs AFTER the statements fetch (so
    # _shares_outstanding_fallback() has this ticker's own income
    # statement on hand - see that function's own docstring, the CPRT
    # incident) and AFTER pence normalisation (see this block's own
    # comment just above - the F2 fix).
    info = _overlay_fresh_price(info, tk, income=income)

    price_unit_suspect, price_unit_suspect_reason = (False, None)
    if price_quote_unit == "GBp":
        price_unit_suspect, price_unit_suspect_reason = _check_price_unit_guard(
            ticker, info, raw_price_for_guard
        )
        if price_unit_suspect:
            flags.append("price_unit_suspect")

    # income_q: quarterly income statement, yfinance-only regardless of
    # which annual source won above (EODHD has no quarterly endpoint this
    # module uses) - see _fetch_yfinance_quarterly_income's docstring for
    # why this exists (a real trailing-4-quarter sum for the handful of
    # line items where the latest annual column is a bad TTM stand-in).
    # Best-effort: an empty result here degrades those specific metrics
    # back to the annual-column convention, same as before this existed.
    income_q = _fetch_yfinance_quarterly_income(tk)
    if income_q.empty:
        flags.append("quarterly_income_unavailable")

    # Currency fix: statement line items (the income/balance/cashflow/
    # income_q DataFrames) are reported in the company's financialCurrency,
    # but price/market-cap-derived figures elsewhere in the app are in its
    # listing currency - for a handful of ASX-listed, USD-reporting names
    # (CSL.AX, RMD.AX, CPU.AX, ...) those two diverge, and mixing raw
    # statement rows with the listing-currency price without conversion
    # silently produces ratios off by the fx rate. Convert every statement
    # DataFrame into the listing currency here, once, so nothing downstream
    # has to know this ever happened.
    #
    # Bug fix: this used to ALSO multiply info["trailingEps"]/["forwardEps"]
    # by the same fx rate, on the assumption that yfinance reports those two
    # quote-level fields in financialCurrency too, same as the statements.
    # Root-caused via a live production diagnostic on CPU.AX (2026-08-29):
    # its quarterly income statement was unavailable that day (a routine
    # yfinance gap - see _eps_ttm), so the app fell back to this now-doubly
    # -converted trailingEps, landing on $2.06 - a ~40% overstatement
    # against TradingView's own $1.57 AUD "Basic EPS (TTM)" figure and,
    # independently, Yahoo Finance's own Statistics page for CPU.AX, which
    # shows "Diluted EPS (ttm): 1.48" - almost exactly the PRE-multiply raw
    # value (2.0553 / 1.40 = 1.468). Both external sources display their
    # EPS figure paired directly against the AUD price, confirming
    # trailingEps/forwardEps come back from yfinance already in the
    # LISTING currency (same as currentPrice), not financialCurrency - so,
    # unlike the statement DataFrames, they must NOT be converted again
    # here. (info["currentPrice"]/["regularMarketPrice"] were never
    # converted either, for the same reason - this brings trailingEps/
    # forwardEps into line with that existing, correct assumption.)
    fin_ccy = (info.get("financialCurrency") or "").upper()
    list_ccy = (info.get("currency") or "").upper()
    fx_unavailable, fx_unavailable_reason = (False, None)
    if fin_ccy and list_ccy and fin_ccy != list_ccy:
        # Exchange-rate fallback-of-1 fix (3 Oct 2026, Director-directed,
        # Commit 6): confirm this fin_ccy->list_ccy pair can actually be
        # converted at all (live, static, or cross-rate-via-USD) BEFORE
        # converting every statement DataFrame - live evidence: "[fx]
        # GEL->GBP fallback 1 (live fetch failed)", a FTSE 100 company
        # reporting in Georgian lari valued as if 1 GEL = 1 GBP. Checked
        # once here, up front, rather than inside _convert_statement_
        # currency()'s own per-column historical_fx_rate() calls - that
        # function has many existing callers that all assume it always
        # returns a DataFrame, so this gates BEFORE calling it rather
        # than changing its return contract. A pair unresolvable today
        # is unresolvable for every column regardless of that column's
        # own period-end date (unresolvability is a property of the
        # currency pair, not the date - the live/static/cross-rate
        # checks fx_rate() runs are all date-independent).
        _fx_check, _ = fcf_valuation_engine.fx_rate(fin_ccy, list_ccy)
        if _fx_check is None:
            fx_unavailable = True
            fx_unavailable_reason = f"no exchange rate for {fin_ccy}"
            flags.append("fx_unavailable")
        else:
            # B2.1 fix: each column now fetches ITS OWN period-end rate
            # inside _convert_statement_currency() - no single up-front
            # `fx` value to gate on any more (see that function's own
            # docstring for why a flat rate here was the bug).
            income = _convert_statement_currency(income, fin_ccy, list_ccy)
            balance = _convert_statement_currency(balance, fin_ccy, list_ccy)
            cashflow = _convert_statement_currency(cashflow, fin_ccy, list_ccy)
            income_q = _convert_statement_currency(income_q, fin_ccy, list_ccy)
            flags.append("currency_converted")

    # Stage 1a (3 Oct 2026, Director-directed): trailingEps/bookValue
    # re-derivation for a GBp-normalised ticker - see _derive_eps_
    # bookvalue_from_statements()'s own docstring. Must run AFTER the
    # financial/listing-currency conversion just above, so `income`/
    # `balance` are already in the same GBP listing currency the price
    # was just normalised to. A no-op for every non-GBp ticker.
    info = _derive_eps_bookvalue_from_statements(info, income, balance)

    # Lists & display Commit 4 (3 Oct 2026, Director-directed): forward
    # P/E resolution for pence-quoted London tickers - must run AFTER the
    # trailingEps re-derivation just above (resolved against that, not
    # the raw GBp-unit info field). A no-op for every non-GBp ticker.
    info, forward_eps_unit = _resolve_forward_eps_unit(ticker, info)

    try:
        hist = tk.history(period="10y", interval="1mo")
    except Exception:
        hist = None
        flags.append("price_history_unavailable")
    prices_10y = _monthly_series(hist)
    if not prices_10y["dates"]:
        flags.append("price_history_unavailable")
    elif price_quote_unit == "GBp":
        # Same /100 treatment as the plain info price fields - this
        # ticker's own monthly close series is fetched in pence too,
        # since nothing about tk.history() itself is affected by the
        # info-blob normalisation above.
        prices_10y = {
            "dates": prices_10y["dates"],
            "prices": [p / 100.0 for p in prices_10y["prices"]],
        }

    try:
        div = tk.dividends
        if div is not None and not div.empty:
            dividends = {
                "dates": [d.isoformat() for d in div.index.to_pydatetime()],
                "amounts": [round(float(v), 6) for v in div.values],
            }
        else:
            dividends = {"dates": [], "amounts": []}
    except Exception:
        dividends = {"dates": [], "amounts": []}
        flags.append("dividends_unavailable")

    # Stage 1a (3 Oct 2026, Director-directed): dividend pence
    # normalisation + validation against info["dividendYield"] - see
    # _normalize_and_validate_pence_dividends()'s own docstring. On
    # disagreement, the dividend SERIES is cleared to n/a by that
    # function itself; dividendRate/trailingAnnualDividendRate (already
    # /100-divided by _normalize_pence_price_fields() above, on the
    # SAME unconfirmed assumption the series validation just rejected)
    # are cleared here too, for the same reason.
    dividend_unit_suspect = False
    if price_quote_unit == "GBp":
        dividends, dividend_unit_suspect = _normalize_and_validate_pence_dividends(
            dividends, info
        )
        if dividend_unit_suspect:
            info.pop("dividendRate", None)
            info.pop("trailingAnnualDividendRate", None)
            flags.append("dividend_unit_suspect")

    try:
        spx_hist = yf.Ticker("^GSPC").history(period="10y", interval="1mo")
        spx_prices_10y = _monthly_series(spx_hist)
    except Exception:
        spx_prices_10y = {"dates": [], "prices": []}
        flags.append("spx_history_unavailable")

    bundle = {
        "info": info,
        "income": income,
        "balance": balance,
        "cashflow": cashflow,
        "income_q": income_q,
        "prices_10y": prices_10y,
        "dividends": dividends,
        "spx_prices_10y": spx_prices_10y,
        "meta": {
            "source": source,
            "statement_years": statement_years,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "flags": flags,
            "bundle_version": BUNDLE_VERSION,
            # Stage 1a (3 Oct 2026, Director-directed, UK/Canada data
            # layer) - see _normalize_pence_price_fields()/_check_price_
            # unit_guard()/_normalize_and_validate_pence_dividends()'s
            # own docstrings. price_quote_unit is "GBp" or None (never a
            # non-None value for a non-GBp ticker); price_unit_suspect/
            # price_unit_suspect_reason let a consumer withhold IV/MOS
            # for a ticker whose pence normalisation didn't cross-check
            # against marketCap; dividend_unit_suspect marks the
            # dividend series/rate as not-trusted-as-pence after all.
            "price_quote_unit": price_quote_unit,
            "price_unit_suspect": price_unit_suspect,
            "price_unit_suspect_reason": price_unit_suspect_reason,
            "dividend_unit_suspect": dividend_unit_suspect,
            # Lists & display Commit 4 (3 Oct 2026, Director-directed):
            # "pounds"|"pence"|"unknown"|None (None = not a GBp ticker) -
            # see _resolve_forward_eps_unit()'s own docstring.
            "forward_eps_unit": forward_eps_unit,
            # Exchange-rate fallback-of-1 fix (3 Oct 2026, Director-
            # directed, Commit 6) - same "withhold IV/MOS for this
            # ticker" contract as price_unit_suspect above: auto_
            # compounder_engine.build_sections() checks this flag the
            # same way it already checks price_unit_suspect and returns
            # None (no Fair Value tab / Compounder View at all) when set,
            # since every monetary figure that page shows is built from
            # these now-unconverted (or inconvertible) statements.
            "fx_unavailable": fx_unavailable,
            "fx_unavailable_reason": fx_unavailable_reason,
        },
    }
    _write_cache(ticker, bundle)
    return bundle
