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

import datetime
import json
import os
import tempfile
import time
import urllib.error
import urllib.request

import pandas as pd
import yfinance as yf

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


def _convert_statement_currency(df, rate):
    """Multiply every MONETARY cell of a statement DataFrame by an fx rate,
    coercing anything non-numeric to NaN first so a stray string/None cell
    can never raise - same best-effort spirit as the rest of this module.
    Returns the input unchanged if it's empty or the multiply fails
    outright.

    Audit fix 1.7: this used to blanket-multiply the WHOLE DataFrame by
    `rate`, which would corrupt a share-count or tax-rate row if one were
    ever read from it - nothing downstream currently reads such a row
    through this path, so this was a landmine for the next metric added
    rather than a live bug (see _NON_MONETARY_ROW_PATTERNS above). Rows
    matching that list are coerced to numeric but left unconverted;
    everything else converts as before."""
    if df is None or df.empty:
        return df
    try:
        numeric = df.apply(pd.to_numeric, errors="coerce")
        converted = numeric * rate
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
    """yfinance history DataFrame -> ~10y of monthly closes (first trading
    close of each month), same "one point per month" shape the hand-built
    workbook's own price_history series uses.

    yfinance's `.history()` index is tz-AWARE, localized to that ticker's
    own exchange (Australia/Sydney for a .AX stock, America/New_York for
    ^GSPC). `Timestamp.isoformat()` on a tz-aware value includes that
    offset ("...T00:00:00+11:00" vs "...T00:00:00-05:00") - so the SAME
    calendar month produced two different-looking date strings depending
    on which exchange the series came from. _cov_corr() (and anything
    else joining a stock's own prices_10y against spx_prices_10y by date
    string) built its {date: price} dicts straight off these strings, so
    for every non-US ticker the two dicts shared ZERO keys and Covariance/
    Correlation/Variance silently came back None for the entire ASX
    lineup - not a missing feature, a broken join. Strip the tz (this is
    already a monthly-first-close bucket, not an intraday timestamp, so
    the offset carries no real information) so every ticker's dates land
    on the same plain "YYYY-MM-01T00:00:00" grid regardless of exchange."""
    if hist_df is None or hist_df.empty or "Close" not in hist_df.columns:
        return {"dates": [], "prices": []}
    try:
        monthly = hist_df["Close"].resample("MS").first().dropna()
        idx = monthly.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        return {
            "dates": [d.isoformat() for d in idx.to_pydatetime()],
            "prices": [round(float(v), 4) for v in monthly.values],
        }
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
        info["currentPrice"] = fresh_price
        shares = _shares_outstanding_fallback(info, income)
        if isinstance(shares, (int, float)) and shares > 0:
            info["marketCap"] = fresh_price * shares
            info["sharesOutstanding"] = shares
        else:
            info.pop("marketCap", None)
    return info


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

    # Same fresh-quote overlay as the cache-hit path above (see
    # _overlay_fresh_price's docstring) - .info's own price/marketCap
    # fields can lag even on a live fetch, so this isn't just a
    # cache-staleness patch. Moved to run AFTER the statements fetch just
    # above (was before it) so _shares_outstanding_fallback() has this
    # ticker's own income statement on hand as a fallback share count
    # when .info's sharesOutstanding is missing (see that function's
    # docstring - the CPRT incident) - nothing else here reads `info`
    # between the old and new call sites, so the reorder is behaviour-
    # neutral apart from that fallback becoming available.
    info = _overlay_fresh_price(info, tk, income=income)

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
    if fin_ccy and list_ccy and fin_ccy != list_ccy:
        try:
            fx, fx_source = fcf_valuation_engine.fx_rate(fin_ccy, list_ccy)
        except Exception:
            fx, fx_source = 1.0, "fallback"
        if fx and fx > 0 and fx != 1.0:
            income = _convert_statement_currency(income, fx)
            balance = _convert_statement_currency(balance, fx)
            cashflow = _convert_statement_currency(cashflow, fx)
            income_q = _convert_statement_currency(income_q, fx)
            flags.append("currency_converted")

    try:
        hist = tk.history(period="10y", interval="1mo")
    except Exception:
        hist = None
        flags.append("price_history_unavailable")
    prices_10y = _monthly_series(hist)
    if not prices_10y["dates"]:
        flags.append("price_history_unavailable")

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
        },
    }
    _write_cache(ticker, bundle)
    return bundle
