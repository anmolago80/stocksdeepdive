"""
auto_compounder_engine.py

Computes the Rational Compounder Research page's six DATA sections
(Fundamentals, Value vs Book, Retained Earnings, Earnings Trends, Cost of
Capital, Fair Value) LIVE, for any ticker, from fundamentals_data.py's
statement/price/dividend bundle - so the Deep Dive page's "Compounder View
(auto)" expander can show the same kind of research compounder_data.json
gives the hand-covered tickers, for every other ticker on the site.

"Company Potential" (the author's own written judgment) is NOT computed
here - there is no live-data equivalent of a personal investment
thesis, and the spec this module was built from explicitly excludes it.

build_sections(ticker) -> {
    "Fundamentals": {"metrics": [...], "price_history": {ticker: {...}},
                      "share_price_growth": {ticker: {...}}},
    "Value vs Book": {"metrics": [...], "iv_bv_bar": {ticker: {...}},
                       "iv_bv_series": {ticker: {...}}},
    "Retained Earnings": {"metrics": [...], "value_created": {ticker: {...}}},
    "Earnings Trends": {"metrics": [...], "series": {ticker: {...}},
                         "pe_ratio_refs": {ticker: {...}}},
    "Cost of Capital": {"metrics": [...], "wacc_roic_series": {ticker: {...}}},
    "Fair Value": {"metrics": [], "valuation_methods": {ticker: {...}},
                    "valuation_inputs": {ticker: {...}}},
    "_meta": {"generated_at", "statement_years", "source", "flags"},
}

Each top-level section dict is shaped EXACTLY like the corresponding
section in compounder_data.json (see build_compounder_data.py) so
compounder_ui.render_section() renders it with zero changes - the whole
point of this module is that its output is indistinguishable, structurally,
from a slice of the hand-built workbook data. ("Fair Value"'s "metrics" is
always [] because compounder_ui.render_section() never reads it for that
section either - see that module.)

Every metric is looked up by LABEL against the live compounder_data.json
for its threshold bands and comment text (never invented) - a metric whose
label isn't in the hand-built workbook renders as a plain (uncoloured)
number with a short neutral definition instead. Every metric computation
is isolated (one bad input can drop that one metric, never the section),
and any value that rests on an assumption/estimate/fallback is marked
"flagged" so compounder_ui.band_gauge() shows it in red with a red asterisk
on its label, the same convention every other estimated figure on this
site already uses.

Cost of equity comes from the site's own capm_engine (unmodified); the DCF
comes from the site's own fcf_valuation_engine (unmodified) - per the
ground rule that existing scoring engines are never touched, only called.

Cached as JSON on the persisted volume (auto_cv_sections/<ticker>.json,
24h TTL) - the whole return value is plain JSON-safe data, no DataFrames.

This module could not be exercised against live data from the sandbox it
was written in (no outbound network there); the formulas follow standard
definitions and the same field names capm_engine.py/fcf_valuation_engine.py
already rely on from yfinance, but several of the "15 derived Earnings
Trends stats" and the "equity_10y" Fair Value method are reasonable
reconstructions of what their compounder_data.json labels describe, not a
byte-for-byte reproduction of the original spreadsheet formulas - worth a
spot-check against a covered ticker (where both the hand-built and auto
numbers can be compared side by side) once this is live.
"""

import datetime
import json
import math
import os
import re
import tempfile

import build_compounder_data
import capm_engine
import fcf_valuation_engine
import fundamentals_data

_CACHE_DIR_NAME = "auto_cv_sections"
_CACHE_TTL_SECONDS = 24 * 3600


# -----------------------------------
# Persistence (same pattern as fundamentals_data.py / watchlist_store.py)
# -----------------------------------

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


def _read_cache(ticker):
    path = _cache_path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        fetched_at = (obj.get("_meta") or {}).get("generated_at")
        if not fetched_at:
            return None
        age = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.datetime.fromisoformat(fetched_at)
        ).total_seconds()
        if age > _CACHE_TTL_SECONDS or age < 0:
            return None
        return obj
    except Exception:
        return None


def _write_cache(ticker, sections):
    path = _cache_path(ticker)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(sections, f)
        os.replace(tmp_path, path)
    except OSError:
        pass


# -----------------------------------
# Threshold/comment lookup - by metric LABEL, against the live hand-built
# data, never invented.
# -----------------------------------

def _reference_lookup():
    """{section_label: {metric_label: {"thresholds":..., "comment":...}}}
    read straight from the live compounder_data.json (whatever
    build_compounder_data._cp_data_dir() resolves to - the same Railway
    Volume the Research page itself reads). Returns {} on any error - a
    missing/corrupt reference file just means every metric renders
    uncoloured, not a crash."""
    try:
        build_compounder_data._cp_seed_from_repo_if_missing("compounder_data.json")
        path = os.path.join(build_compounder_data._cp_data_dir(), "compounder_data.json")
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {}
    lookup = {}
    for sec_label, sec in (data.get("sections") or {}).items():
        d = {}
        for m in sec.get("metrics", []):
            d[m["label"]] = {"thresholds": m.get("thresholds"), "comment": m.get("comment")}
        lookup[sec_label] = d
    return lookup


def _metric(ref, section, label, ticker, value, fmt, key=None, flagged=False, fallback_comment=""):
    r = (ref.get(section) or {}).get(label)
    thresholds = r["thresholds"] if r else None
    comment = (r["comment"] if r and r.get("comment") else None) or fallback_comment
    return {
        "key": key or label,
        "label": label,
        "comment": comment,
        "format": fmt,
        "thresholds": thresholds,
        "values": {ticker: value},
        "flagged": bool(flagged),
    }


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# -----------------------------------
# Statement row lookup - tolerant of the handful of label spellings
# yfinance/EODHD actually use.
# -----------------------------------

_ROW_ALIASES = {
    "revenue": ["Total Revenue", "Revenue", "Operating Revenue"],
    "net_income": ["Net Income", "Net Income Common Stockholders", "Net Income Continuous Operations"],
    "operating_income": ["Operating Income", "Total Operating Income As Reported"],
    "pretax_income": ["Pretax Income", "Income Before Tax"],
    "tax_provision": ["Tax Provision", "Income Tax Expense"],
    "interest_expense": ["Interest Expense", "Interest Expense Non Operating"],
    "basic_eps": ["Basic EPS", "Diluted EPS"],
    "total_assets": ["Total Assets"],
    "current_assets": ["Current Assets", "Total Current Assets"],
    "current_liabilities": ["Current Liabilities", "Total Current Liabilities"],
    "inventory": ["Inventory"],
    "cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    "total_debt": ["Total Debt"],
    "long_term_debt": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"],
    "total_liabilities": ["Total Liabilities Net Minority Interest", "Total Liab"],
    "stockholders_equity": ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"],
    "goodwill_and_intangibles": ["Goodwill And Other Intangible Assets"],
    "working_capital": ["Working Capital"],
    "operating_cash_flow": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "capex": ["Capital Expenditure", "Purchase Of PPE"],
    "free_cash_flow": ["Free Cash Flow"],
}


def _find_row(df, aliases):
    if df is None or df.empty:
        return None
    for name in aliases:
        if name in df.index:
            return name
    lower_idx = {str(i).lower(): i for i in df.index}
    for name in aliases:
        nl = name.lower()
        for li, orig in lower_idx.items():
            if nl in li or li in nl:
                return orig
    return None


def _statement_years(df):
    """Column labels -> year strings, newest first, tolerant of either a
    '2024-12-31 00:00:00'-style label (yfinance) or a plain '2024-12-31'
    (EODHD)."""
    years = []
    for c in df.columns:
        s = str(c)
        m = re.search(r"(19|20)\d{2}", s)
        years.append(m.group(0) if m else s)
    return years


def _series(df, key):
    """[(year_label, value_or_None), ...] newest-first for a mapped row."""
    row_name = _find_row(df, _ROW_ALIASES.get(key, [key]))
    if row_name is None:
        return []
    years = _statement_years(df)
    try:
        row = df.loc[row_name]
        out = []
        for c, y in zip(df.columns, years):
            v = row[c]
            if v is None or (isinstance(v, float) and v != v):
                out.append((y, None))
            else:
                out.append((y, float(v)))
        return out
    except Exception:
        return []


def _latest(df, key):
    for _, v in _series(df, key):
        if v is not None:
            return v
    return None


def _year_end_prices(prices_10y):
    """{year: last-seen monthly close that year} - the monthly series is
    chronological ascending, so the last month seen per year is that
    year's most recent available close (a genuine year-end close for a
    complete year, the latest available price for the current year)."""
    out = {}
    for d, p in zip(prices_10y.get("dates") or [], prices_10y.get("prices") or []):
        out[d[:4]] = p
    return out


def _bvps(bundle):
    equity = _latest(bundle["balance"], "stockholders_equity")
    shares = (bundle.get("info") or {}).get("sharesOutstanding")
    if equity is not None and shares:
        return equity / shares
    return None


def _basics(bundle):
    info = bundle.get("info") or {}
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    shares = info.get("sharesOutstanding")
    market_cap = info.get("marketCap")
    if market_cap is None and price is not None and shares:
        market_cap = price * shares
    currency = info.get("currency") or "USD"
    return {"price": price, "shares": shares, "market_cap": market_cap, "currency": currency, "info": info}


def _dividend_ttm(dividends):
    dates, amounts = dividends.get("dates") or [], dividends.get("amounts") or []
    if not dates:
        return None
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(days=365)
        total, found = 0.0, False
        for d, a in zip(dates, amounts):
            dt = datetime.datetime.fromisoformat(d)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            # Upper-bounded at "now" too - real dividend history is never
            # future-dated, but this keeps the window honestly a trailing
            # twelve months rather than an open-ended ">= cutoff".
            if cutoff <= dt <= now:
                total += a
                found = True
        return total if found else None
    except Exception:
        return None


def _dividends_per_share_by_year(dividends):
    out = {}
    for d, a in zip(dividends.get("dates") or [], dividends.get("amounts") or []):
        y = d[:4]
        out[y] = out.get(y, 0.0) + a
    return out


# -----------------------------------
# Cross-series stats (price history vs S&P 500, share price growth)
# -----------------------------------

def _price_history_entry(prices_10y):
    dates, prices = prices_10y.get("dates") or [], prices_10y.get("prices") or []
    if not dates:
        return None
    return {"dates": dates, "prices": prices, "avg_10y": sum(prices) / len(prices)}


def _share_price_growth_entry(prices_10y):
    dates, prices = prices_10y.get("dates") or [], prices_10y.get("prices") or []
    if len(dates) < 24:
        return None
    by_year = {}
    for d, p in zip(dates, prices):
        by_year.setdefault(d[:4], []).append(p)
    years_sorted = sorted(by_year)
    avgs = {y: sum(v) / len(v) for y, v in by_year.items()}
    out_years, out_values = [], []
    for i in range(1, len(years_sorted)):
        y0, y1 = years_sorted[i - 1], years_sorted[i]
        if avgs[y0]:
            out_years.append(y1)
            out_values.append((avgs[y1] - avgs[y0]) / avgs[y0])
    if not out_years:
        return None
    return {"years": list(reversed(out_years)), "values": list(reversed(out_values))}


def _cov_corr(a_series, b_series):
    a_by_date = dict(zip(a_series.get("dates") or [], a_series.get("prices") or []))
    b_by_date = dict(zip(b_series.get("dates") or [], b_series.get("prices") or []))
    common = sorted(set(a_by_date) & set(b_by_date))
    if len(common) < 4:
        return None, None
    a_vals = [a_by_date[d] for d in common]
    b_vals = [b_by_date[d] for d in common]
    a_ret = [(a_vals[i] - a_vals[i - 1]) / a_vals[i - 1] for i in range(1, len(a_vals)) if a_vals[i - 1]]
    b_ret = [(b_vals[i] - b_vals[i - 1]) / b_vals[i - 1] for i in range(1, len(b_vals)) if b_vals[i - 1]]
    n = min(len(a_ret), len(b_ret))
    if n < 3:
        return None, None
    a_ret, b_ret = a_ret[:n], b_ret[:n]
    mean_a, mean_b = sum(a_ret) / n, sum(b_ret) / n
    cov = sum((a_ret[i] - mean_a) * (b_ret[i] - mean_b) for i in range(n)) / (n - 1)
    var_a = sum((x - mean_a) ** 2 for x in a_ret) / (n - 1)
    var_b = sum((x - mean_b) ** 2 for x in b_ret) / (n - 1)
    corr = cov / math.sqrt(var_a * var_b) if var_a > 0 and var_b > 0 else None
    return cov, corr


def _overall_avg_pe(bundle):
    eps_series = [(y, v) for y, v in _series(bundle["income"], "basic_eps") if v]
    year_end_prices = _year_end_prices(bundle["prices_10y"])
    pes = [year_end_prices[y] / v for y, v in eps_series if year_end_prices.get(y) and v]
    if pes:
        return sum(pes) / len(pes)
    return (bundle.get("info") or {}).get("trailingPE")


# -----------------------------------
# DCF (existing engine - called, never modified)
# -----------------------------------

def _run_dcf(bundle, ticker):
    info = bundle.get("info") or {}
    currency = info.get("currency") or "USD"
    result = _safe(
        fcf_valuation_engine.dcf_intrinsic_value,
        ticker, info=info, cashflow_df=bundle.get("cashflow"), currency=currency,
    )
    if not result:
        return {"value": None, "growth": None, "perpetual_rate": None, "discount_rate": None, "flagged": True}
    value, growth_used, meta = result
    return {
        "value": value if value and value > 0 else None,
        "growth": growth_used,
        "perpetual_rate": meta.get("perpetual_rate_used"),
        "discount_rate": meta.get("discount_rate_used"),
        "flagged": bool(meta.get("defaulted") or meta.get("growth_default")),
    }


# -----------------------------------
# Fundamentals
# -----------------------------------

def _build_fundamentals(bundle, ticker, ref):
    b = _basics(bundle)
    price, shares, mcap = b["price"], b["shares"], b["market_cap"]
    income, balance, cashflow = bundle["income"], bundle["balance"], bundle["cashflow"]

    net_income = _latest(income, "net_income")
    revenue = _latest(income, "revenue")
    operating_income = _latest(income, "operating_income")
    pretax_income = _latest(income, "pretax_income")
    tax_provision = _latest(income, "tax_provision")
    interest_expense = _latest(income, "interest_expense")

    total_assets = _latest(balance, "total_assets")
    current_assets = _latest(balance, "current_assets")
    current_liabilities = _latest(balance, "current_liabilities")
    inventory = _latest(balance, "inventory")
    cash = _latest(balance, "cash")
    total_debt = _latest(balance, "total_debt")
    total_liabilities = _latest(balance, "total_liabilities")
    equity = _latest(balance, "stockholders_equity")
    goodwill_intangibles = _latest(balance, "goodwill_and_intangibles")
    working_capital = _latest(balance, "working_capital")
    if working_capital is None and current_assets is not None and current_liabilities is not None:
        working_capital = current_assets - current_liabilities

    ocf = _latest(cashflow, "operating_cash_flow")
    capex = _latest(cashflow, "capex")
    fcf = _latest(cashflow, "free_cash_flow")
    if fcf is None and ocf is not None and capex is not None:
        fcf = ocf - abs(capex)

    tangible_book_value = (equity - goodwill_intangibles) if (equity is not None and goodwill_intangibles is not None) else equity

    tax_rate = (tax_provision / pretax_income) if (tax_provision is not None and pretax_income) else None

    metrics = []

    def add(label, value, fmt, key=None, flagged=False, fallback=""):
        if value is None:
            return
        metrics.append(_metric(ref, "Fundamentals", label, ticker, value, fmt, key=key, flagged=flagged, fallback_comment=fallback))

    add("Earning Yield", (net_income / mcap) if (net_income is not None and mcap) else None, "pct",
        fallback="Net income divided by market cap.")
    add("Price to Sales ratio", (mcap / revenue) if (mcap and revenue) else None, "x",
        fallback="Market cap divided by revenue.")
    add("Total Current Assets", current_assets, "cur")
    add("Inventory", inventory, "cur")
    add("Working Capital", working_capital, "cur", fallback="Current assets minus current liabilities.")
    add("Tangible Asset Value", tangible_book_value, "cur",
        fallback="Shareholders' equity minus goodwill and other intangible assets.",
        flagged=goodwill_intangibles is None)

    mcap_tangible = (mcap / tangible_book_value) if (mcap and tangible_book_value not in (None, 0)) else None
    add("Market Cap/Tangible Asset Value", mcap_tangible, "x")
    add("Income Tax Expense", tax_provision, "cur")
    add("% Income Paid on Taxes", tax_rate, "pct")

    bvps = _bvps(bundle)
    add("Book Value Per Share", bvps, "cur")
    add("1.5xBV", (bvps * 1.5) if bvps is not None else None, "cur")

    interest_coverage = (operating_income / abs(interest_expense)) if (operating_income is not None and interest_expense) else None
    add("Interest Coverage", interest_coverage, "x", flagged=interest_expense is None,
        fallback="Operating income divided by interest expense.")

    wc_debt = (working_capital / total_debt) if (working_capital is not None and total_debt) else None
    add("Working Capital  to Debt", wc_debt, "x")

    ev = (mcap + (total_debt or 0) - (cash or 0)) if mcap is not None else None
    add("EV To Free Cash Flow", (ev / fcf) if (ev is not None and fcf) else None, "x")

    add("Net Income Ratio", (net_income / revenue) if (net_income is not None and revenue) else None, "pct")
    add("Free Cash Flow Yield", (fcf / mcap) if (fcf is not None and mcap) else None, "pct")
    add("Intangibles To Total Assets", (goodwill_intangibles / total_assets) if (goodwill_intangibles is not None and total_assets) else None, "pct")
    add("Price to Equity Ratio", (mcap / equity) if (mcap and equity) else None, "x")

    rota = (net_income / (total_assets - (goodwill_intangibles or 0))) if (net_income is not None and total_assets is not None) else None
    add("Return on Tangible Assets", rota, "pct")
    add("ROE", (net_income / equity) if (net_income is not None and equity) else None, "pct")
    add("Operating Income Ratio", (operating_income / revenue) if (operating_income is not None and revenue) else None, "pct")
    add("PFCF Ratio", (mcap / fcf) if (mcap and fcf) else None, "x")

    invested_capital = (equity + (total_debt or 0) - (cash or 0)) if equity is not None else None
    nopat = (operating_income * (1 - (tax_rate if tax_rate is not None else 0.25))) if operating_income is not None else None
    add("ROIC", (nopat / invested_capital) if (nopat is not None and invested_capital) else None, "pct",
        flagged=True, fallback="NOPAT (operating income after an estimated tax rate) divided by invested capital.")

    add("Debt to Assets", (total_debt / total_assets) if (total_debt is not None and total_assets) else None, "pct")
    add("Quick Ratio", ((current_assets - (inventory or 0)) / current_liabilities) if (current_assets is not None and current_liabilities) else None, "x")
    add("Current Ratio", (current_assets / current_liabilities) if (current_assets is not None and current_liabilities) else None, "x")
    add("Debt to Equity", (total_debt / equity) if (total_debt is not None and equity) else None, "x")

    cov, corr = _cov_corr(bundle["prices_10y"], bundle["spx_prices_10y"])
    add("Covariance (SP500)", cov, "num", fallback="Covariance of monthly returns vs the S&P 500 over the available price history.")
    add("Correlation (SP500)", corr, "x", fallback="Correlation of monthly returns vs the S&P 500 over the available price history.")

    return {
        "metrics": metrics,
        "price_history": {ticker: _price_history_entry(bundle["prices_10y"])} if bundle["prices_10y"].get("dates") else {},
        "share_price_growth": {ticker: e} if (e := _share_price_growth_entry(bundle["prices_10y"])) else {},
    }


# -----------------------------------
# Value vs Book
# -----------------------------------

def _iv_bv_series(bundle, iv_ttm, bvps_ttm, avg_pe):
    eps_series = [(y, v) for y, v in _series(bundle["income"], "basic_eps") if v is not None]
    equity_series = dict(_series(bundle["balance"], "stockholders_equity"))
    shares = (bundle.get("info") or {}).get("sharesOutstanding")
    if not eps_series or not shares or avg_pe is None:
        years, ratios = [], []
    else:
        years, ratios = [], []
        for y, eps in eps_series:
            eq_y = equity_series.get(y)
            if eq_y is None:
                continue
            bvps_y = eq_y / shares
            if not bvps_y:
                continue
            years.append(y)
            ratios.append((eps * avg_pe) / bvps_y)
    if iv_ttm is not None and bvps_ttm:
        years = ["TTM"] + years
        ratios = [iv_ttm / bvps_ttm] + ratios
    if not years:
        return None
    return {"years": years, "ratios": ratios}


def _build_value_vs_book(bundle, ticker, ref, dcf_result):
    b = _basics(bundle)
    price, info = b["price"], b["info"]
    fcf = _latest(bundle["cashflow"], "free_cash_flow")

    metrics = []

    def add(label, value, fmt, flagged=False):
        if value is None:
            return
        metrics.append(_metric(ref, "Value vs Book", label, ticker, value, fmt, flagged=flagged))

    add("Share Price", price, "cur")
    add("52 Week High", info.get("fiftyTwoWeekHigh"), "cur")
    add("52 Week Low", info.get("fiftyTwoWeekLow"), "cur")
    add("Free Cash Flow (TTM)", fcf, "cur")

    iv = dcf_result.get("value")
    bvps = _bvps(bundle)
    add("IV/BV", (iv / bvps) if (iv is not None and bvps) else None, "x", flagged=dcf_result.get("flagged", False))

    iv_bv_bar = {"price": price, "iv": iv, "bv": bvps} if (price is not None and iv is not None and bvps is not None) else None
    avg_pe = _overall_avg_pe(bundle)
    iv_bv_series = _iv_bv_series(bundle, iv, bvps, avg_pe)

    return {
        "metrics": metrics,
        "iv_bv_bar": {ticker: iv_bv_bar} if iv_bv_bar else {},
        "iv_bv_series": {ticker: iv_bv_series} if iv_bv_series else {},
    }


# -----------------------------------
# Retained Earnings
# -----------------------------------

def _value_created(bundle, retained_ttm, price_now):
    horizons = {"TTM": 1, "2Y": 2, "5Y": 5, "10Y": 10}
    eps_series = [(y, v) for y, v in _series(bundle["income"], "basic_eps") if v is not None]
    dps_by_year = _dividends_per_share_by_year(bundle.get("dividends") or {})
    year_end_prices = _year_end_prices(bundle["prices_10y"])
    out = {}
    for label, n_years in horizons.items():
        if label == "TTM":
            re_val = retained_ttm
            past_year = eps_series[0][0] if eps_series else None
        else:
            slice_ = eps_series[:n_years]
            if not slice_:
                continue
            re_val = sum(eps - dps_by_year.get(y, 0.0) for y, eps in slice_)
            past_year = slice_[-1][0]
        if re_val in (None, 0) or price_now is None or not past_year:
            continue
        past_price = year_end_prices.get(past_year)
        if past_price is None:
            continue
        out[label] = {"retained_earnings": re_val, "value_created": (price_now - past_price) / re_val}
    return out or None


def _ten_year_retained(bundle):
    eps_series = [(y, v) for y, v in _series(bundle["income"], "basic_eps") if v is not None][:10]
    if not eps_series:
        return None
    dps_by_year = _dividends_per_share_by_year(bundle.get("dividends") or {})
    return sum(eps - dps_by_year.get(y, 0.0) for y, eps in eps_series)


def _build_retained_earnings(bundle, ticker, ref):
    b = _basics(bundle)
    info, price, mcap = b["info"], b["price"], b["market_cap"]
    trailing_eps = info.get("trailingEps")
    div_ttm = _dividend_ttm(bundle.get("dividends") or {})

    ratio_p_ed = None
    if price is not None and trailing_eps is not None and div_ttm is not None:
        denom = trailing_eps - div_ttm
        if denom:
            ratio_p_ed = price / denom

    retained_ttm = (trailing_eps - div_ttm) if (trailing_eps is not None and div_ttm is not None) else None

    metrics = []

    def add(label, value, fmt, flagged=False):
        if value is None:
            return
        metrics.append(_metric(ref, "Retained Earnings", label, ticker, value, fmt, flagged=flagged))

    add("Share Price", price, "cur")
    add("52 Week High", info.get("fiftyTwoWeekHigh"), "cur")
    add("52 Week Low", info.get("fiftyTwoWeekLow"), "cur")
    add("Market Cap (TTM)", mcap, "cur")
    add("EPS (TTM)", trailing_eps, "cur")
    add("Dividend (TTM)", div_ttm, "cur")
    add("Ratio P/(E-D)", ratio_p_ed, "x")
    add("Dividend Yield (TTM)", (div_ttm / price) if (div_ttm is not None and price) else None, "pct")
    # Confirmed against a covered ticker: the workbook's "Retained Earnings
    # (TTM)" is the raw per-share dollar figure (EPS minus Dividend), run
    # through the "pct" formatter like the variance metric above - not
    # retained-as-a-fraction-of-EPS, despite the "pct" tag.
    add("Retained Earnings (TTM)", retained_ttm, "pct", flagged=True)
    add("10Y Retained Earnings (From Last FY)", _ten_year_retained(bundle), "cur", flagged=True)

    value_created = _value_created(bundle, retained_ttm, price)
    return {"metrics": metrics, "value_created": {ticker: value_created} if value_created else {}}


# -----------------------------------
# Earnings Trends
# -----------------------------------

def _aa_bond_rate():
    """AA corporate bond yield from FRED (series AAA, used as the closest
    freely available proxy) when FRED_API_KEY is set - None (caller flags
    and defaults to 5.5%) otherwise or on any fetch error."""
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return None
    try:
        import urllib.request
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=AAA&api_key={api_key}&file_type=json&sort_order=desc&limit=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "stocksdeepdive/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        obs = (payload.get("observations") or [])
        if obs and obs[0].get("value") not in (None, "."):
            return float(obs[0]["value"]) / 100
    except Exception:
        return None
    return None


def _build_earnings_trends(bundle, ticker, ref):
    info = bundle.get("info") or {}
    eps_series = [(y, v) for y, v in _series(bundle["income"], "basic_eps") if v is not None]
    trailing_eps = info.get("trailingEps")
    full_eps = ([("TTM", trailing_eps)] if trailing_eps is not None else []) + eps_series
    year_end_prices = _year_end_prices(bundle["prices_10y"])
    price_now = info.get("currentPrice") or info.get("regularMarketPrice")

    eps_growth = []
    for i in range(len(full_eps) - 1):
        y, v = full_eps[i]
        _, v0 = full_eps[i + 1]
        if v is not None and v0:
            eps_growth.append((y, (v - v0) / abs(v0)))

    pe_series = []
    for y, eps in full_eps:
        p = price_now if y == "TTM" else year_end_prices.get(y)
        if p is not None and eps:
            pe_series.append((y, p / eps))

    series = {}
    if full_eps:
        series["eps"] = {"years": [y for y, _ in full_eps], "values": [v for _, v in full_eps]}
    if eps_growth:
        series["eps_growth"] = {"years": [y for y, _ in eps_growth], "values": [v for _, v in eps_growth]}
    if pe_series:
        series["pe_ratio"] = {"years": [y for y, _ in pe_series], "values": [v for _, v in pe_series]}

    pe_vals = [v for _, v in pe_series]
    avg_3y = sum(pe_vals[:3]) / len(pe_vals[:3]) if pe_vals else None
    overall_avg = sum(pe_vals) / len(pe_vals) if pe_vals else None
    pe_ratio_refs = {"avg_3y": avg_3y, "overall_avg": overall_avg} if pe_vals else None

    eps_vals = [v for _, v in full_eps if v is not None]
    n = len(eps_vals)

    metrics = []

    def add(label, value, fmt, flagged=False, fallback=""):
        if value is None:
            return
        metrics.append(_metric(ref, "Earnings Trends", label, ticker, value, fmt, flagged=flagged, fallback_comment=fallback))

    ten_avg = sum(eps_vals) / n if n else None
    four_avg = sum(eps_vals[:4]) / min(4, n) if n else None
    add("10y Average Earnings", ten_avg, "cur", flagged=(n < 10),
        fallback=f"Average EPS over the {n} year(s) of statements available.")
    add("4y Average Earnings", four_avg, "cur")
    if eps_vals:
        add("Max Earnings", max(eps_vals), "cur")
        add("Min Earnings", min(eps_vals), "cur")

    if n >= 2:
        variance = sum((v - ten_avg) ** 2 for v in eps_vals) / n
        sd = math.sqrt(variance)
        # The hand-built workbook's own "10y EPS Variance" is the raw
        # population variance itself (confirmed against a covered ticker:
        # its stored value equals this section's own "10y EPS SD" squared,
        # not variance/mean) - just happens to be run through the site's
        # "pct" formatter (value*100 with a % sign) rather than "cur"/"num".
        add("10y EPS  Variance", variance, "pct")
        add("10y EPS SD", sd, "cur")
        add("10y AVG+SD", ten_avg + sd, "x")
        four_vals = eps_vals[:4]
        if len(four_vals) >= 2:
            four_var = sum((v - four_avg) ** 2 for v in four_vals) / len(four_vals)
            four_sd = math.sqrt(four_var)
            add("4y EPS SD", four_sd, "cur")
            add("4y AVG+SD", four_avg + four_sd, "x")

    if n >= 2 and eps_vals[-1] and eps_vals[-1] > 0:
        add("Average 10 Year Growth", (eps_vals[0] / eps_vals[-1]) ** (1 / (n - 1)) - 1, "pct", flagged=(n < 10))
    if len(eps_vals) >= 4 and eps_vals[3] and eps_vals[3] > 0:
        add("10Y Growth (3Y AVG)", (eps_vals[0] / eps_vals[3]) ** (1 / 3) - 1, "pct")

    add("PE Ratio Average", overall_avg, "x")
    add("PE Ratio Average 3 Years", avg_3y, "x")

    aa_bond_rate = _aa_bond_rate()
    rate_used = aa_bond_rate if aa_bond_rate is not None else 0.055
    earning_yield_ttm = (full_eps[0][1] / price_now) if (full_eps and full_eps[0][1] and price_now) else None
    add("ETP% Vs AA Bond", (earning_yield_ttm - rate_used) if earning_yield_ttm is not None else None, "pct",
        flagged=(aa_bond_rate is None),
        fallback="Earnings yield minus the AA corporate bond yield (FRED when configured, else a flagged 5.5% default).")

    bvps = _bvps(bundle)
    if avg_3y is not None and bvps and full_eps and full_eps[0][1] is not None:
        add("AVG PE 3Y*PTB Ratio", avg_3y * (full_eps[0][1] / bvps), "num", flagged=True,
            fallback="3-year average P/E multiplied by price-to-book.")

    return {
        "metrics": metrics,
        "series": {ticker: series} if series else {},
        "pe_ratio_refs": {ticker: pe_ratio_refs} if pe_ratio_refs else {},
    }


# -----------------------------------
# Cost of Capital
# -----------------------------------

def _avg_invested_capital(bundle):
    """Average of the latest two years' (equity + total debt - cash) - the
    standard ROIC convention (average capital employed over the period,
    not a single point-in-time snapshot). This is what actually
    distinguishes "ROIC (TTM)" from Fundamentals' own point-in-time
    "ROIC" - confirmed against a covered ticker, where the two are
    legitimately different numbers in the hand-built workbook, not a
    formula that should be forced to agree. Falls back to the latest
    year alone when a prior year isn't available."""
    equity_s = _series(bundle["balance"], "stockholders_equity")
    debt_s = dict(_series(bundle["balance"], "total_debt"))
    cash_s = dict(_series(bundle["balance"], "cash"))
    points = []
    for y, eq in equity_s:
        if eq is None:
            continue
        ic = eq + (debt_s.get(y) or 0) - (cash_s.get(y) or 0)
        points.append(ic)
        if len(points) == 2:
            break
    if not points:
        return None
    return sum(points) / len(points)


def _build_cost_of_capital(bundle, ticker, ref):
    b = _basics(bundle)
    info, mcap, ccy = b["info"], b["market_cap"], b["currency"]
    total_debt = _latest(bundle["balance"], "total_debt")
    long_term_debt = _latest(bundle["balance"], "long_term_debt")
    interest_expense = _latest(bundle["income"], "interest_expense")
    pretax_income = _latest(bundle["income"], "pretax_income")
    tax_provision = _latest(bundle["income"], "tax_provision")
    operating_income = _latest(bundle["income"], "operating_income")
    equity = _latest(bundle["balance"], "stockholders_equity")
    cash = _latest(bundle["balance"], "cash")
    tax_rate = (tax_provision / pretax_income) if (tax_provision is not None and pretax_income) else 0.25

    # Same "- cash" convention as Fundamentals' own Enterprise Value
    # (verified against a covered ticker's EV-to-FCF-implied EV) - Cost of
    # Capital's EV had been missing this term, overstating EV by the
    # entire cash balance.
    ev = (mcap + (total_debt or 0) - (cash or 0)) if mcap is not None else None
    invested_capital = _avg_invested_capital(bundle)
    nopat = (operating_income * (1 - tax_rate)) if operating_income is not None else None
    roic_ttm = (nopat / invested_capital) if (nopat is not None and invested_capital) else None

    ce_result = _safe(capm_engine.resolve_discount_rate, info, ccy)
    cost_of_equity, ce_meta = ce_result if ce_result else (None, {})

    cost_of_debt = (abs(interest_expense) / total_debt) * (1 - tax_rate) if (interest_expense is not None and total_debt) else None
    wacc, wacc_flagged = None, False
    if cost_of_equity is not None:
        e_weight = (mcap / (mcap + (total_debt or 0))) if mcap else 1.0
        used_cost_of_debt = cost_of_debt if cost_of_debt is not None else 0.0
        wacc_flagged = cost_of_debt is None
        wacc = e_weight * cost_of_equity + (1 - e_weight) * used_cost_of_debt

    metrics = []

    def add(label, value, fmt, flagged=False):
        if value is None:
            return
        metrics.append(_metric(ref, "Cost of Capital", label, ticker, value, fmt, flagged=flagged))

    add("Market Cap (TTM)", mcap, "cur")
    add("Enterprise Value (TTM)", ev, "cur")
    # The label says Long Term Debt specifically, not Total Debt - use the
    # dedicated statement row when it exists; only fall back to (and flag)
    # the broader Total Debt figure when the balance sheet doesn't break
    # the two out separately.
    add("Long Term Debt (TTM)", long_term_debt if long_term_debt is not None else total_debt,
        "cur", flagged=long_term_debt is None)
    add("Interest Expense (TTM)", interest_expense, "cur")
    add("ROIC (TTM)", roic_ttm, "pct", flagged=True)
    add("Income Before Tax (TTM)", pretax_income, "cur")
    add("WACC", wacc, "pct", flagged=wacc_flagged or bool((ce_meta or {}).get("defaulted")))
    add("Total Investments (TTM)", total_debt, "cur", flagged=True)

    wacc_roic_series = None
    if roic_ttm is not None or wacc is not None:
        wacc_roic_series = {
            "wacc": {"periods": ["TTM"], "values": [wacc]},
            "roic": {"periods": ["TTM"], "values": [roic_ttm]},
        }

    return {"metrics": metrics, "wacc_roic_series": {ticker: wacc_roic_series} if wacc_roic_series else {}}


# -----------------------------------
# Fair Value
# -----------------------------------

def _equity_growth_rate(bundle):
    equity_series = [(y, v) for y, v in _series(bundle["balance"], "stockholders_equity") if v]
    shares = (bundle.get("info") or {}).get("sharesOutstanding")
    if len(equity_series) >= 2 and shares:
        newest, oldest = equity_series[0][1] / shares, equity_series[-1][1] / shares
        n = len(equity_series) - 1
        if oldest > 0 and n > 0:
            try:
                return (newest / oldest) ** (1 / n) - 1
            except (ValueError, ZeroDivisionError):
                return None
    return None


def _equity_10y_method(bundle, avg_pe):
    """Mirrors the workbook's own 'Rational Compounder Method 10y': book
    value per share compounded at its own historical growth rate for 10
    years, multiplied by the average P/E to get a year-10 value, discounted
    back to today. The discount rate here is a flagged default (3%, the
    same figure seen in the hand-built workbook's own equity_10y inputs)
    since there's no per-stock equivalent computed elsewhere in this
    module - see the module docstring's note on best-effort formulas."""
    bvps = _bvps(bundle)
    growth = _equity_growth_rate(bundle)
    if bvps is None or growth is None or avg_pe is None:
        return None
    discount = 0.03
    future_value = bvps * ((1 + growth) ** 10) * avg_pe
    return future_value / ((1 + discount) ** 10), growth, discount


def _build_fair_value(bundle, ticker, dcf_result):
    b = _basics(bundle)
    info, price = b["info"], b["price"]
    trailing_eps = info.get("trailingEps")
    forward_eps = info.get("forwardEps")
    avg_pe = _overall_avg_pe(bundle)

    pe_trailing_value = (trailing_eps * avg_pe) if (trailing_eps is not None and avg_pe is not None) else None
    pe_forward_value = (forward_eps * avg_pe) if (forward_eps is not None and avg_pe is not None) else None
    dcf_value = dcf_result.get("value")

    equity_10y_result = _safe(_equity_10y_method, bundle, avg_pe)
    equity_10y_value, equity_growth, equity_discount = equity_10y_result if equity_10y_result else (None, None, None)

    valuation_methods = {}
    if price is not None:
        valuation_methods["price"] = price
    if pe_forward_value is not None:
        valuation_methods["pe_forward"] = pe_forward_value
    if pe_trailing_value is not None:
        valuation_methods["pe_trailing"] = pe_trailing_value
    if dcf_value is not None:
        valuation_methods["dcf"] = dcf_value
    if equity_10y_value is not None:
        valuation_methods["equity_10y"] = equity_10y_value

    valuation_inputs = {}
    if "pe_forward" in valuation_methods:
        valuation_inputs["pe_forward"] = [
            {"label": "Forecast EPS", "value": forward_eps, "format": "cur"},
            {"label": "Average P/E", "value": avg_pe, "format": "x"},
        ]
    if "pe_trailing" in valuation_methods:
        valuation_inputs["pe_trailing"] = [
            {"label": "EPS (Trailing)", "value": trailing_eps, "format": "cur"},
            {"label": "Average P/E", "value": avg_pe, "format": "x"},
        ]
    if "dcf" in valuation_methods:
        valuation_inputs["dcf"] = [
            {"label": "Perpetual Rate", "value": dcf_result.get("perpetual_rate"), "format": "pct"},
            {"label": "Discount Rate", "value": dcf_result.get("discount_rate"), "format": "pct"},
            {"label": "Base Case Growth", "value": dcf_result.get("growth"), "format": "pct"},
        ]
    if "equity_10y" in valuation_methods:
        valuation_inputs["equity_10y"] = [
            {"label": "Equity Growth", "value": equity_growth, "format": "pct"},
            {"label": "Discount Rate (this calc)", "value": equity_discount, "format": "pct"},
            {"label": "Average P/E", "value": avg_pe, "format": "x"},
        ]

    return {
        "metrics": [],
        "valuation_methods": {ticker: valuation_methods} if valuation_methods else {},
        "valuation_inputs": {ticker: valuation_inputs} if valuation_inputs else {},
    }


# -----------------------------------
# Public entry point
# -----------------------------------

_SECTION_BUILDERS = [
    "Fundamentals", "Value vs Book", "Retained Earnings",
    "Earnings Trends", "Cost of Capital", "Fair Value",
]


def build_sections(ticker, force_refresh=False):
    """The six computed Research sections for `ticker`, shaped exactly
    like compounder_data.json's own "sections" dict (see module docstring)
    - ready to pass straight into compounder_ui.render_section()/
    render_tabs(). Returns None if the underlying fundamentals bundle
    can't be fetched at all (e.g. an invalid ticker). A "_meta" key carries
    provenance (source, statement year depth, fetch time, flags) for the
    Deep Dive page's disclosure caption - render_tabs() simply never looks
    at that key, so its presence is harmless."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None

    if not force_refresh:
        cached = _read_cache(ticker)
        if cached is not None:
            return cached

    bundle = fundamentals_data.get_bundle(ticker, force_refresh=force_refresh)
    if not bundle:
        return None

    ref = _reference_lookup()
    dcf_result = _safe(_run_dcf, bundle, ticker) or {
        "value": None, "growth": None, "perpetual_rate": None, "discount_rate": None, "flagged": True,
    }

    builders = {
        "Fundamentals": lambda: _build_fundamentals(bundle, ticker, ref),
        "Value vs Book": lambda: _build_value_vs_book(bundle, ticker, ref, dcf_result),
        "Retained Earnings": lambda: _build_retained_earnings(bundle, ticker, ref),
        "Earnings Trends": lambda: _build_earnings_trends(bundle, ticker, ref),
        "Cost of Capital": lambda: _build_cost_of_capital(bundle, ticker, ref),
        "Fair Value": lambda: _build_fair_value(bundle, ticker, dcf_result),
    }

    sections = {}
    for label in _SECTION_BUILDERS:
        result = _safe(builders[label])
        sections[label] = result if result is not None else {"metrics": []}

    bundle_meta = bundle.get("meta") or {}
    sections["_meta"] = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "statement_years": bundle_meta.get("statement_years"),
        "source": bundle_meta.get("source"),
        "flags": bundle_meta.get("flags"),
    }

    _write_cache(ticker, sections)
    return sections
