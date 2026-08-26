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
BUNDLE_VERSION = 3


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


def _convert_statement_currency(df, rate):
    """Multiply every cell of a statement DataFrame by an fx rate, coercing
    anything non-numeric to NaN first so a stray string/None cell can never
    raise - same best-effort spirit as the rest of this module. Returns the
    input unchanged if it's empty or the multiply fails outright."""
    if df is None or df.empty:
        return df
    try:
        return df.apply(pd.to_numeric, errors="coerce") * rate
    except Exception:
        return df


# -----------------------------------
# Price / dividend series (yfinance, both sources)
# -----------------------------------

def _monthly_series(hist_df):
    """yfinance history DataFrame -> ~10y of monthly closes (first trading
    close of each month), same "one point per month" shape the hand-built
    workbook's own price_history series uses."""
    if hist_df is None or hist_df.empty or "Close" not in hist_df.columns:
        return {"dates": [], "prices": []}
    try:
        monthly = hist_df["Close"].resample("MS").first().dropna()
        return {
            "dates": [d.isoformat() for d in monthly.index.to_pydatetime()],
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

def get_bundle(ticker, force_refresh=False):
    """The statement/price/dividend bundle for one ticker - see the module
    docstring for the exact shape. Cached 24h on the persisted volume;
    pass force_refresh=True to bypass the cache (e.g. an admin rebuild
    action, mirroring compounder_data.json's own rebuild pattern)."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None

    if not force_refresh:
        cached = _read_cache(ticker)
        if cached is not None:
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

    # Currency fix: statement line items are reported in the company's
    # financialCurrency, but price/market-cap-derived figures elsewhere in
    # the app are in its listing currency - for a handful of ASX-listed,
    # USD-reporting names (CSL.AX, RMD.AX, ...) those two diverge, and
    # mixing them without conversion silently produces ratios off by the
    # fx rate. Convert every statement DataFrame (and the two EPS fields
    # used directly alongside price) into the listing currency here, once,
    # so nothing downstream has to know this ever happened.
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
            for _eps_key in ("trailingEps", "forwardEps"):
                if info.get(_eps_key) is not None:
                    try:
                        info[_eps_key] = float(info[_eps_key]) * fx
                    except (TypeError, ValueError):
                        pass
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
