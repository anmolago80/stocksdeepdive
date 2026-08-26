"""
auto_compounder_engine.py

Computes the Rational Compounder Research page's six DATA sections
(Fundamentals, Value vs Book, Retained Earnings, Earnings Trends, Cost of
Capital, Fair Value) LIVE, for any ticker, from fundamentals_data.py's
statement/price/dividend bundle - so the Deep Dive page's "Compounder View
(auto)" section can show the same kind of research compounder_data.json
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
    "_meta": {"generated_at", "statement_years", "source", "flags",
              "engine_version"},
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

FORMULA SOURCE (2025 revision): every formula in this module below the
"decoded from the workbook" comments was transcribed directly from the real
Excel cells of Andrew's source workbook ("Shares - Invested and
Investigation" - sheets Valuation, IV2BV, Dividend Ratio, Earnings
Analysis, Cost of Capital Analysis, Stock Analysis), not reverse-engineered
or guessed. Where a formula still has no decoded workbook source (e.g. some
edge-case fallbacks), it remains a documented best-effort reconstruction
and is flagged accordingly.
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

# Bumped whenever a formula in this module changes - a cached section whose
# "_meta.engine_version" doesn't match is treated as expired, so a formula
# fix doesn't sit invisible behind a stale 24h cache entry (or worse, a
# stale Railway Volume file from before a redeploy).
ENGINE_VERSION = 2


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
        meta = obj.get("_meta") or {}
        if meta.get("engine_version") != ENGINE_VERSION:
            return None
        fetched_at = meta.get("generated_at")
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
    "revenue": ["Total Revenue", "Revenue", "Operating Revenue", "totalRevenue"],
    "net_income": ["Net Income", "Net Income Common Stockholders", "Net Income Continuous Operations", "netIncome"],
    "operating_income": ["Operating Income", "Total Operating Income As Reported"],
    "pretax_income": ["Pretax Income", "Income Before Tax", "incomeBeforeTax"],
    "tax_provision": ["Tax Provision", "Income Tax Expense", "incomeTaxExpense"],
    "interest_expense": ["Interest Expense", "Interest Expense Non Operating", "interestExpense"],
    "basic_eps": ["Basic EPS", "Diluted EPS"],
    "total_assets": ["Total Assets", "totalAssets"],
    "current_assets": ["Current Assets", "Total Current Assets", "totalCurrentAssets"],
    "current_liabilities": ["Current Liabilities", "Total Current Liabilities", "totalCurrentLiabilities"],
    "inventory": ["Inventory", "inventory"],
    "cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments", "cash"],
    "total_debt": ["Total Debt", "shortLongTermDebtTotal"],
    "long_term_debt": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation", "longTermDebt"],
    "total_liabilities": ["Total Liabilities Net Minority Interest", "Total Liab", "totalLiab"],
    "stockholders_equity": ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest", "totalStockholderEquity"],
    "goodwill_and_intangibles": ["Goodwill And Other Intangible Assets", "goodWill", "intangibleAssets"],
    "working_capital": ["Working Capital", "netWorkingCapital"],
    "operating_cash_flow": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities", "totalCashFromOperatingActivities"],
    "capex": ["Capital Expenditure", "Purchase Of PPE", "capitalExpenditures"],
    "free_cash_flow": ["Free Cash Flow", "freeCashFlow"],
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
    """[(year_label, value_or_None), ...] newest-first for a mapped row.
    `key` may be a registered _ROW_ALIASES key, or any literal row-name
    string (falls back to searching for that literal name, with the same
    exact-then-fuzzy-substring matching _find_row always does)."""
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


def _eps_series(bundle):
    """[(year, eps_or_None), ...] newest-first, FISCAL YEARS ONLY (never
    TTM - that's layered on separately by every caller that needs it).

    EODHD's statements carry no EPS row at all, so when this bundle's
    statements came from EODHD, EPS is computed as net_income / current
    shares outstanding instead (an approximation - historical share counts
    aren't available - consistent with every other place in this module
    that has to reuse today's share count for a historical year)."""
    row = _find_row(bundle["income"], _ROW_ALIASES.get("basic_eps", ["basic_eps"]))
    if row is not None:
        return _series(bundle["income"], "basic_eps")
    if (bundle.get("meta") or {}).get("source") == "eodhd":
        shares = (bundle.get("info") or {}).get("sharesOutstanding")
        if shares:
            return [
                (y, (v / shares) if v is not None else None)
                for y, v in _series(bundle["income"], "net_income")
            ]
    return []


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


def _dividend_ttm_window(dividends):
    """Trailing-365-day sum of individual dividend payments - only used as
    a fallback now (see _dividend_ttm) when the data provider's own TTM
    dividend rate isn't available, since a rolling payment-date window can
    over/under-count by one payment depending on exactly where "today"
    falls relative to the company's payment cadence."""
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
            if cutoff <= dt <= now:
                total += a
                found = True
        return total if found else None
    except Exception:
        return None


def _dividend_ttm(bundle):
    """(div_ttm, flagged). Primary source: the data provider's own TTM
    dividend-per-share figure (info["trailingAnnualDividendRate"]) - this
    is what the workbook itself is built on. Only falls back to summing
    the raw payment history over a trailing 365-day window when that
    field is missing (flagged, since a rolling window is a payment-date
    fluke risk); a ticker with literally no dividend history at all gets
    a plain, unflagged 0.0 - that's a real fact about the company, not an
    assumption, and previously caused Retained Earnings (TTM), Ratio
    P/(E-D) and the whole value_created chart to vanish for every
    non-payer."""
    info = bundle.get("info") or {}
    dividends = bundle.get("dividends") or {}
    provider_rate = info.get("trailingAnnualDividendRate")
    if provider_rate is not None:
        return float(provider_rate), False
    if not (dividends.get("dates") or []):
        return 0.0, False
    window_total = _dividend_ttm_window(dividends)
    if window_total is not None:
        return window_total, True
    return 0.0, True


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


def _avg_pe_3pt(bundle):
    """"PE Ratio Average": the workbook's real AU = AVERAGE(PE_TTM,
    PE_lastFY, PE_prevFY) - the 3 most recent P/E points only, NOT an
    all-years average. The previous all-years average badly inflated
    high-multiple stocks (the primary cause of Fair Value being reported
    as unreasonably large before this fix). Skips non-positive-EPS years/
    TTM; falls back to info["trailingPE"] only if nothing is computable."""
    info = bundle.get("info") or {}
    price_now = info.get("currentPrice") or info.get("regularMarketPrice")
    trailing_eps = info.get("trailingEps")
    year_end_prices = _year_end_prices(bundle["prices_10y"])
    pes = []
    if price_now and trailing_eps and trailing_eps > 0:
        pes.append(price_now / trailing_eps)
    for y, eps in _eps_series(bundle)[:2]:
        if eps is not None and eps > 0:
            p = year_end_prices.get(y)
            if p is not None:
                pes.append(p / eps)
    if pes:
        return sum(pes) / len(pes)
    return info.get("trailingPE")


def _pe_avg_3y_by_eps(bundle):
    """"PE Ratio Average 3 Years": current price divided by the AVERAGE OF
    THE 3 MOST RECENT EPS VALUES (EPS_TTM, EPS_lastFY, EPS_prevFY) - a
    different figure from _avg_pe_3pt ("PE Ratio Average", an average of 3
    separate P/E ratios). Both are real, distinct workbook metrics."""
    info = bundle.get("info") or {}
    price_now = info.get("currentPrice") or info.get("regularMarketPrice")
    trailing_eps = info.get("trailingEps")
    eps_points = [trailing_eps] if trailing_eps is not None else []
    eps_points += [v for _, v in _eps_series(bundle)[:2] if v is not None]
    if not price_now or not eps_points:
        return None
    avg_eps = sum(eps_points) / len(eps_points)
    if not avg_eps:
        return None
    return price_now / avg_eps


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
    long_term_debt = _latest(balance, "long_term_debt")
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

    # Workbook divides by Long Term Debt (V4/W4), not Total Debt - falls
    # back (flagged) to Total Debt only when the balance sheet doesn't
    # break the two out separately.
    ltd_for_wc = long_term_debt if long_term_debt is not None else total_debt
    wc_debt = (working_capital / ltd_for_wc) if (working_capital is not None and ltd_for_wc) else None
    add("Working Capital  to Debt", wc_debt, "x", flagged=(long_term_debt is None))

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

    # Invested Capital = Equity + Total Debt (capital employed) - NOT
    # net of cash. An earlier version of this subtracted cash too, which
    # is fine for a normally-levered company but blows up into a
    # meaningless 100%+ ROIC for a cash-rich, low-debt name where cash
    # roughly offsets equity+debt (confirmed on OCL.AX: subtracting cash
    # gave ~250% here vs TradingView/the user's own spreadsheet showing
    # ~25-40%, while dropping the cash subtraction lands in that same
    # band - cross-checked against real numbers, not a guess).
    invested_capital = (equity + (total_debt or 0)) if equity is not None else None
    nopat = (operating_income * (1 - (tax_rate if tax_rate is not None else 0.25))) if operating_income is not None else None
    add("ROIC", (nopat / invested_capital) if (nopat is not None and invested_capital) else None, "pct",
        flagged=True, fallback="NOPAT (operating income after an estimated tax rate) divided by invested capital (equity + total debt).")

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

def _mini_dcf_iv(fcf_base, g_earn, d_dcf, p, shares):
    """One year's per-share intrinsic value from the workbook's own IV2BV
    mini-DCF (decoded from the real cell formula): 9 explicit years of
    growth at g_earn discounted at d_dcf, then a Gordon terminal value
    applied to the (already-discounted) year-9 flow - that reuse of the
    discounted year-9 term, rather than an undiscounted one, is a real
    quirk of the workbook's own formula, transcribed faithfully rather
    than "corrected"."""
    if not fcf_base or g_earn is None or d_dcf is None or not shares or d_dcf <= p:
        return None
    pv_explicit = sum(
        fcf_base * ((1 + g_earn) ** k) / ((1 + d_dcf) ** k) for k in range(1, 10)
    )
    year9_discounted = fcf_base * ((1 + g_earn) ** 9) / ((1 + d_dcf) ** 9)
    terminal = year9_discounted * (1 + p) / (d_dcf - p)
    return (pv_explicit + terminal) / shares


def _iv_bv_series(bundle, dcf_result, bvps_ttm):
    """Per-year IV/BV using the workbook's real per-year IV formula (a
    mini-DCF on that year's own Free Cash Flow - see _mini_dcf_iv), not
    EPS x average P/E (the old approach here, now removed). Uses the SAME
    growth/discount/perpetual-rate the site's own DCF used for this
    ticker, so the series stays internally consistent with the DCF the
    site actually shows. BVPS_y uses TODAY's share count for every
    historical year (historical share counts aren't available)."""
    g_earn = dcf_result.get("growth")
    d_dcf = dcf_result.get("discount_rate")
    p = dcf_result.get("perpetual_rate")
    if p is None:
        p = 0.03
    shares = (bundle.get("info") or {}).get("sharesOutstanding")

    years, ratios = [], []
    if g_earn is not None and d_dcf is not None and shares and d_dcf > p:
        fcf_series = [(y, v) for y, v in _series(bundle["cashflow"], "free_cash_flow") if v is not None]
        if not fcf_series:
            ocf_s = dict(_series(bundle["cashflow"], "operating_cash_flow"))
            capex_s = dict(_series(bundle["cashflow"], "capex"))
            fcf_series = [
                (y, ocf_s[y] - abs(capex_s[y]))
                for y in ocf_s
                if y in capex_s and ocf_s[y] is not None and capex_s[y] is not None
            ]
        equity_series = dict(_series(bundle["balance"], "stockholders_equity"))
        for y, fcf_y in fcf_series:
            eq_y = equity_series.get(y)
            if eq_y is None:
                continue
            bvps_y = eq_y / shares
            if not bvps_y:
                continue
            iv_y = _mini_dcf_iv(fcf_y, g_earn, d_dcf, p, shares)
            if iv_y is None:
                continue
            years.append(y)
            ratios.append(iv_y / bvps_y)

        # TTM point: TTM FCF (info's own freeCashflow when present, else
        # the latest statement year) over current BVPS - the same mini-DCF
        # shape as every other point, for internal consistency of the
        # chart (the headline "IV/BV" metric below stays the site DCF's
        # own value, unrelated to this per-year series).
        info = bundle.get("info") or {}
        fcf_ttm = info.get("freeCashflow")
        if fcf_ttm is None:
            fcf_ttm = _latest(bundle["cashflow"], "free_cash_flow")
        if fcf_ttm is None:
            ocf_ttm = _latest(bundle["cashflow"], "operating_cash_flow")
            capex_ttm = _latest(bundle["cashflow"], "capex")
            if ocf_ttm is not None and capex_ttm is not None:
                fcf_ttm = ocf_ttm - abs(capex_ttm)
        iv_ttm_point = _mini_dcf_iv(fcf_ttm, g_earn, d_dcf, p, shares) if fcf_ttm else None
        if iv_ttm_point is not None and bvps_ttm:
            years = ["TTM"] + years
            ratios = [iv_ttm_point / bvps_ttm] + ratios

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

    # Headline IV/BV metric stays the site DCF's own value ÷ current BVPS
    # (the site DCF is the normalised version of the same model) -
    # unrelated to the per-year mini-DCF series below.
    iv = dcf_result.get("value")
    bvps = _bvps(bundle)
    add("IV/BV", (iv / bvps) if (iv is not None and bvps) else None, "x", flagged=dcf_result.get("flagged", False))

    iv_bv_bar = {"price": price, "iv": iv, "bv": bvps} if (price is not None and iv is not None and bvps is not None) else None
    iv_bv_series = _iv_bv_series(bundle, dcf_result, bvps)

    return {
        "metrics": metrics,
        "iv_bv_bar": {ticker: iv_bv_bar} if iv_bv_bar else {},
        "iv_bv_series": {ticker: iv_bv_series} if iv_bv_series else {},
    }


# -----------------------------------
# Retained Earnings
# -----------------------------------

def _value_created(bundle, retained_ttm, price_now):
    """Value Created per $ Retained, at 2Y/5Y/10Y/TTM horizons - decoded
    from the workbook (Dividend Ratio!CB:CM). Each horizon's "retained"
    figure is a sum of (EPS_y - DPS_y) over FISCAL YEARS ONLY (never
    TTM) - except the "TTM" horizon itself, which the workbook actually
    calls "11Y ... (From TTM)": TTM's own retained earnings PLUS the last
    10 FYs. The market-value change is the year-end share-price
    difference between the horizon's start and end years (a mcap change
    ÷ today's share count collapses to a plain price difference since
    the same share count cancels on both sides).

    With yfinance's usual 4-5 years of statement depth, 2Y computes
    cleanly; 5Y/10Y/TTM sums cover only the years actually available
    (flagged when short of the full horizon - also noted in the
    section's own statement-depth caption)."""
    eps_series = [(y, v) for y, v in _eps_series(bundle) if v is not None]
    dps_by_year = _dividends_per_share_by_year(bundle.get("dividends") or {})
    year_end_prices = _year_end_prices(bundle["prices_10y"])

    out = {}
    for label, n_years in (("2Y", 2), ("5Y", 5), ("10Y", 10)):
        slice_ = eps_series[:n_years]
        if len(slice_) < 2:
            continue
        re_val = sum(eps - dps_by_year.get(y, 0.0) for y, eps in slice_)
        end_year, start_year = slice_[0][0], slice_[-1][0]
        end_price, start_price = year_end_prices.get(end_year), year_end_prices.get(start_year)
        if re_val in (None, 0) or end_price is None or start_price is None:
            continue
        out[label] = {
            "retained_earnings": re_val,
            "value_created": (end_price - start_price) / re_val,
        }

    # TTM horizon = the workbook's own "11Y (From TTM)": TTM retained plus
    # the last 10 FYs, value change measured from today's price back to
    # the oldest year in that 10Y window.
    ttm_slice = eps_series[:10]
    if retained_ttm is not None and ttm_slice:
        re_val = retained_ttm + sum(eps - dps_by_year.get(y, 0.0) for y, eps in ttm_slice)
        start_price = year_end_prices.get(ttm_slice[-1][0])
        if re_val not in (None, 0) and start_price is not None and price_now is not None:
            out["TTM"] = {
                "retained_earnings": re_val,
                "value_created": (price_now - start_price) / re_val,
            }

    return out or None


def _ten_year_retained(bundle):
    """(value, years_used). Sum of (EPS_y - DPS_y) over up to 10 fiscal
    years, excluding TTM."""
    eps_series = [(y, v) for y, v in _eps_series(bundle) if v is not None][:10]
    if not eps_series:
        return None, 0
    dps_by_year = _dividends_per_share_by_year(bundle.get("dividends") or {})
    total = sum(eps - dps_by_year.get(y, 0.0) for y, eps in eps_series)
    return total, len(eps_series)


def _build_retained_earnings(bundle, ticker, ref):
    b = _basics(bundle)
    info, price, mcap = b["info"], b["price"], b["market_cap"]
    trailing_eps = info.get("trailingEps")
    div_ttm, div_ttm_flagged = _dividend_ttm(bundle)

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
    add("Dividend (TTM)", div_ttm, "cur", flagged=div_ttm_flagged)
    add("Ratio P/(E-D)", ratio_p_ed, "x", flagged=div_ttm_flagged)
    add("Dividend Yield (TTM)", (div_ttm / price) if (div_ttm is not None and price) else None, "pct", flagged=div_ttm_flagged)
    # Confirmed against a covered ticker: the workbook's "Retained Earnings
    # (TTM)" is the raw per-share dollar figure (EPS minus Dividend), run
    # through the "pct" formatter like the variance metric below - not
    # retained-as-a-fraction-of-EPS, despite the "pct" tag. A negative
    # value here is a real, possible result (trailing payout > 100%), not
    # necessarily a bug.
    add("Retained Earnings (TTM)", retained_ttm, "pct", flagged=True)
    ten_yr_re, ten_yr_n = _ten_year_retained(bundle)
    add("10Y Retained Earnings (From Last FY)", ten_yr_re, "cur", flagged=(ten_yr_n < 10))

    value_created = _value_created(bundle, retained_ttm, price)
    return {"metrics": metrics, "value_created": {ticker: value_created} if value_created else {}}


# -----------------------------------
# Earnings Trends
# -----------------------------------

def _build_earnings_trends(bundle, ticker, ref):
    info = bundle.get("info") or {}
    fy_eps = [(y, v) for y, v in _eps_series(bundle) if v is not None]  # fiscal years only, no TTM
    trailing_eps = info.get("trailingEps")
    full_eps = ([("TTM", trailing_eps)] if trailing_eps is not None else []) + fy_eps
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

    fy_vals = [v for _, v in fy_eps]
    n_fy = len(fy_vals)
    all_vals = [v for _, v in full_eps if v is not None]  # Max/Min DO include TTM - workbook's own K:U range

    metrics = []

    def add(label, value, fmt, flagged=False, fallback=""):
        if value is None:
            return
        metrics.append(_metric(ref, "Earnings Trends", label, ticker, value, fmt, flagged=flagged, fallback_comment=fallback))

    ten_avg = sum(fy_vals) / n_fy if n_fy else None
    four_avg = sum(fy_vals[:4]) / min(4, n_fy) if n_fy else None
    add("10y Average Earnings", ten_avg, "cur", flagged=(n_fy < 10),
        fallback=f"Average EPS over the {n_fy} fiscal year(s) of statements available (excludes TTM).")
    add("4y Average Earnings", four_avg, "cur")
    if all_vals:
        add("Max Earnings", max(all_vals), "cur")
        add("Min Earnings", min(all_vals), "cur")

    if n_fy >= 2:
        variance = sum((v - ten_avg) ** 2 for v in fy_vals) / n_fy
        sd = math.sqrt(variance)
        # The hand-built workbook's own "10y EPS Variance" is the raw
        # population variance itself (confirmed against a covered ticker),
        # run through the site's "pct" formatter (value*100 with a % sign)
        # rather than "cur"/"num".
        add("10y EPS  Variance", variance, "pct")
        add("10y EPS SD", sd, "cur")
        add("10y AVG+SD", ten_avg + sd, "x")
        four_vals = fy_vals[:4]
        if len(four_vals) >= 2:
            four_var = sum((v - four_avg) ** 2 for v in four_vals) / len(four_vals)
            four_sd = math.sqrt(four_var)
            add("4y EPS SD", four_sd, "cur")
            add("4y AVG+SD", four_avg + four_sd, "x")

    # "Average 10 Year Growth" = the arithmetic MEAN of the year-over-year
    # EPS growth series above (which correctly includes the TTM-vs-lastFY
    # point) - NOT a CAGR. Decoded from the workbook's own
    # AVERAGEIF(AE:AN).
    if eps_growth:
        growth_vals = [g for _, g in eps_growth]
        add("Average 10 Year Growth", sum(growth_vals) / len(growth_vals), "pct",
            flagged=(len(growth_vals) < 9))

    # "10Y Growth (3Y AVG)" = TOTAL (not annualised) growth between the
    # newest-3 average (including TTM) and the oldest-3 average.
    if len(full_eps) >= 4:
        newest3 = [v for _, v in full_eps[:3] if v is not None]
        oldest3 = [v for _, v in full_eps[-3:] if v is not None]
        overlap = len(full_eps) < 6  # newest-3 and oldest-3 share points below 6 total
        if newest3 and oldest3:
            mean_new = sum(newest3) / len(newest3)
            mean_old = sum(oldest3) / len(oldest3)
            if mean_old:
                add("10Y Growth (3Y AVG)", (mean_new - mean_old) / abs(mean_old), "pct", flagged=overlap)

    avg_pe = _avg_pe_3pt(bundle)
    pe_avg_3y_by_eps = _pe_avg_3y_by_eps(bundle)
    add("PE Ratio Average", avg_pe, "x")
    add("PE Ratio Average 3 Years", pe_avg_3y_by_eps, "x")

    pe_ratio_refs = None
    if pe_avg_3y_by_eps is not None or avg_pe is not None:
        pe_ratio_refs = {"avg_3y": pe_avg_3y_by_eps, "overall_avg": avg_pe}

    # "ETP% Vs AA Bond" is simply the 3-year-average earnings yield - the
    # AA-bond figure is only the threshold/comment COMPARISON baseline
    # (already attached via the label lookup against compounder_data.json);
    # the metric's VALUE never subtracts it. No FRED fetch needed here at
    # all any more.
    if pe_avg_3y_by_eps:
        add("ETP% Vs AA Bond", 1 / pe_avg_3y_by_eps, "pct")

    bvps = _bvps(bundle)
    if pe_avg_3y_by_eps is not None and bvps and price_now:
        ptb = price_now / bvps
        add("AVG PE 3Y*PTB Ratio", pe_avg_3y_by_eps * ptb, "num", flagged=True,
            fallback="3-year average P/E multiplied by price-to-book.")

    return {
        "metrics": metrics,
        "series": {ticker: series} if series else {},
        "pe_ratio_refs": {ticker: pe_ratio_refs} if pe_ratio_refs else {},
    }


# -----------------------------------
# Cost of Capital
# -----------------------------------

def _year_points():
    """['TTM', 'YYYY', 'YYYY', 'YYYY'] for current_year-2, -4, -5 -
    computed from the current date (never hardcoded), generalising the
    workbook's own (hand-built, so hardcoded when it was built) period
    list to always be relative to today."""
    y = datetime.datetime.now(datetime.timezone.utc).year
    return ["TTM", str(y - 2), str(y - 4), str(y - 5)]


def _avg_invested_capital_for_year(equity_by_year, debt_by_year, cash_by_year, years_desc, target_year):
    """Average (equity + total debt) for target_year and the year
    immediately prior in the statement's own year list - the standard
    ROIC "average capital employed over the period" convention,
    generalised to any statement year (not just the latest two).

    Deliberately NOT net of cash (cash_by_year is accepted but unused -
    kept in the signature so both call sites don't need touching again).
    Subtracting cash here matched the hand-covered tickers (AUB/CSL/RMD,
    all normally levered) but breaks down for a cash-rich, low-debt name:
    on OCL.AX, subtracting cash left an invested-capital base of ~$10-14M
    against ~$25M of NOPAT - a ~250% ROIC - while TradingView and the
    user's own spreadsheet both show ~25-40% for the same ticker, which
    is what (equity + debt) with no cash netting reproduces."""
    if target_year not in years_desc:
        return None
    idx = years_desc.index(target_year)
    points = []
    for y in years_desc[idx:idx + 2]:
        eq = equity_by_year.get(y)
        if eq is None:
            continue
        points.append(eq + (debt_by_year.get(y) or 0))
    if not points:
        return None
    return sum(points) / len(points)


def _avg_invested_capital(bundle):
    """TTM-point convenience wrapper around
    _avg_invested_capital_for_year, using the newest statement year and
    the one before it."""
    equity_s = _series(bundle["balance"], "stockholders_equity")
    if not equity_s:
        return None
    years_desc = [y for y, _ in equity_s]
    equity_by_year = dict(equity_s)
    debt_by_year = dict(_series(bundle["balance"], "total_debt"))
    cash_by_year = dict(_series(bundle["balance"], "cash"))
    return _avg_invested_capital_for_year(equity_by_year, debt_by_year, cash_by_year, years_desc, years_desc[0])


def _total_investments(balance_df):
    """"Total Investments (TTM)": a real balance-sheet investments line in
    the workbook, NOT total debt (a crude placeholder used before this
    fix). Summed across whichever of these rows the statement actually
    has; None (metric dropped, not shown as total debt) if none exist."""
    names = ["Investments And Advances", "Other Short Term Investments", "Long Term Equity Investment"]
    total, found = 0.0, False
    for name in names:
        v = _latest(balance_df, name)
        if v is not None:
            total += v
            found = True
    return total if found else None


def _build_cost_of_capital(bundle, ticker, ref):
    b = _basics(bundle)
    info, mcap, ccy = b["info"], b["market_cap"], b["currency"]
    shares = b["shares"]
    total_debt = _latest(bundle["balance"], "total_debt")
    long_term_debt = _latest(bundle["balance"], "long_term_debt")
    interest_expense = _latest(bundle["income"], "interest_expense")
    pretax_income = _latest(bundle["income"], "pretax_income")
    tax_provision = _latest(bundle["income"], "tax_provision")
    operating_income = _latest(bundle["income"], "operating_income")
    equity = _latest(bundle["balance"], "stockholders_equity")
    cash = _latest(bundle["balance"], "cash")
    tax_ttm = (tax_provision / pretax_income) if (tax_provision is not None and pretax_income) else 0.25

    ev = (mcap + (total_debt or 0) - (cash or 0)) if mcap is not None else None

    # Cost of equity is ONE constant across every year - the workbook does
    # exactly this (a single hand-set cost-of-equity cell feeds every
    # period), so this is computed once here, not per-year.
    ce_result = _safe(capm_engine.resolve_discount_rate, info, ccy)
    cost_of_equity, ce_meta = ce_result if ce_result else (None, {})
    ce_flagged = bool((ce_meta or {}).get("defaulted"))

    equity_by_year = dict(_series(bundle["balance"], "stockholders_equity"))
    debt_by_year = dict(_series(bundle["balance"], "total_debt"))
    ltd_by_year = dict(_series(bundle["balance"], "long_term_debt"))
    cash_by_year = dict(_series(bundle["balance"], "cash"))
    interest_by_year = dict(_series(bundle["income"], "interest_expense"))
    op_income_by_year = dict(_series(bundle["income"], "operating_income"))
    years_desc = [y for y, _ in _series(bundle["balance"], "stockholders_equity")]
    year_end_prices = _year_end_prices(bundle["prices_10y"])

    def _wacc_for(e_val, ltd_val, int_val, ltd_flagged):
        """WACC_Y = E/(E+LTD)*CoE + LTD/(E+LTD)*(Int/LTD)*(1-tax_ttm) -
        decoded from Cost of Capital Analysis. LTD 0/None -> 100% equity
        weight (flagged)."""
        if cost_of_equity is None or e_val is None:
            return None, False
        if not ltd_val:
            return cost_of_equity, True
        weight_e = e_val / (e_val + ltd_val)
        weight_d = ltd_val / (e_val + ltd_val)
        cost_of_debt = (abs(int_val) / ltd_val) * (1 - tax_ttm) if int_val is not None else 0.0
        return weight_e * cost_of_equity + weight_d * cost_of_debt, (ltd_flagged or int_val is None)

    def _roic_for(year):
        op_inc = op_income_by_year.get(year)
        if op_inc is None:
            return None
        nopat = op_inc * (1 - tax_ttm)
        ic = _avg_invested_capital_for_year(equity_by_year, debt_by_year, cash_by_year, years_desc, year)
        if not ic:
            return None
        return nopat / ic

    periods, wacc_vals, roic_vals = [], [], []

    # TTM point
    ltd_ttm = long_term_debt if long_term_debt is not None else total_debt
    ltd_ttm_flagged = long_term_debt is None
    wacc_ttm, wacc_ttm_flagged = _wacc_for(mcap, ltd_ttm, interest_expense, ltd_ttm_flagged)
    roic_ttm = _roic_for(years_desc[0]) if years_desc else None
    periods.append("TTM")
    wacc_vals.append(wacc_ttm)
    roic_vals.append(roic_ttm)

    # current_year-2, -4, -5 points - only years the statements actually
    # cover; with yfinance's usual 4-yr depth, -5 (and often -4) will
    # simply be missing, which is fine.
    for target_year in _year_points()[1:]:
        if target_year not in years_desc:
            continue
        if years_desc and target_year == years_desc[0]:
            # y-2/-4/-5 is relative to TODAY's calendar year, but the TTM
            # point above is really "the latest fiscal year on file" - for
            # a ticker whose most recent filed FY is already 2 (or more)
            # years behind today (a common lag, not just a stale-data
            # edge case), one of those computed years can land on the
            # exact same fiscal year as TTM. ROIC would then be a bit-for-
            # bit duplicate of the TTM bar (it doesn't use market price at
            # all), which reads as a rendering glitch rather than real
            # data - skip it rather than show the same year twice.
            continue
        e_y = None
        py = year_end_prices.get(target_year)
        if py is not None and shares:
            e_y = py * shares  # flagged implicitly - today's share count on a historical year
        ltd_y = ltd_by_year.get(target_year)
        ltd_y_flagged = ltd_y is None
        if ltd_y is None:
            ltd_y = debt_by_year.get(target_year)
        int_y = interest_by_year.get(target_year)
        wacc_y, wacc_y_flagged = _wacc_for(e_y, ltd_y, int_y, ltd_y_flagged)
        roic_y = _roic_for(target_year)
        if wacc_y is None and roic_y is None:
            continue
        periods.append(target_year)
        wacc_vals.append(wacc_y)
        roic_vals.append(roic_y)

    metrics = []

    def add(label, value, fmt, flagged=False):
        if value is None:
            return
        metrics.append(_metric(ref, "Cost of Capital", label, ticker, value, fmt, flagged=flagged))

    add("Market Cap (TTM)", mcap, "cur")
    add("Enterprise Value (TTM)", ev, "cur")
    add("Long Term Debt (TTM)", ltd_ttm, "cur", flagged=ltd_ttm_flagged)
    add("Interest Expense (TTM)", interest_expense, "cur")
    add("ROIC (TTM)", roic_ttm, "pct", flagged=True)
    add("Income Before Tax (TTM)", pretax_income, "cur")
    add("WACC", wacc_ttm, "pct", flagged=wacc_ttm_flagged or ce_flagged)
    add("Total Investments (TTM)", _total_investments(bundle["balance"]), "cur", flagged=True)

    wacc_roic_series = None
    if any(v is not None for v in wacc_vals) or any(v is not None for v in roic_vals):
        wacc_roic_series = {
            "wacc": {"periods": periods, "values": wacc_vals},
            "roic": {"periods": periods, "values": roic_vals},
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


def _equity_10y_method(bundle, g_earn):
    """The workbook's real "Equity Method 10y" (Valuation!U = DE x fx,
    decoded from the actual cell): equity net of this year's earnings,
    compounded at the historical EQUITY growth rate for 10 years, PLUS net
    income projected as a growing annuity over 10 years at the
    OWNER-EARNINGS growth rate the site's own DCF used for this ticker -
    both discounted at a flat 3% ("inflation for this calc only", matching
    the workbook's own CT cell and the hand-built data's displayed 3.0%
    discount rate) - per share.

        equity_10y = ( (E - NI) * (1+g_eq)^10 / (1.03)^10
                     +  NI * ((1+g_earn)^10 - 1) / g_earn / (1.03)^10
                     ) / shares

    g_earn is dcf_result["growth"] - the workbook feeds the same "Owner
    Earnings Growth Rate" cell into both the DCF and this method, so the
    method is dropped entirely if the DCF has no growth figure to share.
    (E - NI) is deliberately NOT clamped at zero - it can legitimately go
    negative for a high-ROE company, and the workbook doesn't clamp it
    either."""
    if g_earn is None:
        return None
    equity = _latest(bundle["balance"], "stockholders_equity")
    net_income = _latest(bundle["income"], "net_income")
    shares = (bundle.get("info") or {}).get("sharesOutstanding")
    g_eq = _equity_growth_rate(bundle)
    if equity is None or net_income is None or not shares or g_eq is None:
        return None
    discount = 0.03
    disc10 = (1 + discount) ** 10
    equity_term = (equity - net_income) * ((1 + g_eq) ** 10) / disc10
    if abs(g_earn) < 1e-9:
        # Limit of the growing-annuity FV formula as g -> 0.
        annuity_fv = net_income * 10
    else:
        annuity_fv = net_income * (((1 + g_earn) ** 10) - 1) / g_earn
    earnings_term = annuity_fv / disc10
    value_per_share = (equity_term + earnings_term) / shares
    return value_per_share, g_eq, discount


def _pe_forward_method(bundle, g_earn):
    """Workbook's real pe_forward (Valuation!J = Forecast EPS(5y) x Actual
    P/E x fx): Forecast EPS(5y) = eps_ttm x (1+g_earn)^5 (net income
    compounded 5y / shares, i.e. trailing EPS grown at the DCF's own
    owner-earnings growth rate); Actual P/E = price / eps_ttm (today's
    actual multiple, NOT an average). Returns
    (value, forecast_eps_5y, actual_pe) or None if trailing EPS/price/
    g_earn aren't available."""
    if g_earn is None:
        return None
    info = bundle.get("info") or {}
    trailing_eps = info.get("trailingEps")
    price_now = info.get("currentPrice") or info.get("regularMarketPrice")
    if trailing_eps is None or trailing_eps <= 0 or not price_now:
        return None
    forecast_eps_5y = trailing_eps * ((1 + g_earn) ** 5)
    actual_pe = price_now / trailing_eps
    return forecast_eps_5y * actual_pe, forecast_eps_5y, actual_pe


def _build_fair_value(bundle, ticker, dcf_result):
    b = _basics(bundle)
    info, price = b["info"], b["price"]
    trailing_eps = info.get("trailingEps")
    g_earn = dcf_result.get("growth")
    avg_pe = _avg_pe_3pt(bundle)

    pe_forward_result = _safe(_pe_forward_method, bundle, g_earn)
    pe_forward_value, forecast_eps_5y, actual_pe = pe_forward_result if pe_forward_result else (None, None, None)

    pe_trailing_value = (trailing_eps * avg_pe) if (trailing_eps is not None and avg_pe is not None) else None
    dcf_value = dcf_result.get("value")

    equity_10y_result = _safe(_equity_10y_method, bundle, g_earn)
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
            {"label": "Forecast EPS (5y)", "value": forecast_eps_5y, "format": "cur"},
            {"label": "Actual P/E", "value": actual_pe, "format": "x"},
        ]
    if "pe_trailing" in valuation_methods:
        valuation_inputs["pe_trailing"] = [
            {"label": "EPS (Trailing/Diluted)", "value": trailing_eps, "format": "cur"},
            {"label": "Average P/E", "value": avg_pe, "format": "x"},
        ]
    if "dcf" in valuation_methods:
        valuation_inputs["dcf"] = [
            {"label": "Perpetual Rate", "value": dcf_result.get("perpetual_rate"), "format": "pct"},
            {"label": "Discount Rate", "value": dcf_result.get("discount_rate"), "format": "pct"},
            {"label": "Base Case Growth", "value": dcf_result.get("growth"), "format": "pct"},
        ]
    if "equity_10y" in valuation_methods:
        # No "Average P/E" here - matches the hand-built workbook's own
        # equity_10y inputs (Equity Growth + Discount Rate only), and the
        # real formula (see _equity_10y_method) never uses it either.
        valuation_inputs["equity_10y"] = [
            {"label": "Equity Growth", "value": equity_growth, "format": "pct"},
            {"label": "Discount Rate (this calc)", "value": equity_discount, "format": "pct"},
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
    provenance (source, statement year depth, fetch time, engine version,
    flags) for the Deep Dive page's disclosure caption and for cache
    invalidation - render_tabs() simply never looks at that key, so its
    presence is harmless."""
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
        "engine_version": ENGINE_VERSION,
    }

    _write_cache(ticker, sections)
    return sections
