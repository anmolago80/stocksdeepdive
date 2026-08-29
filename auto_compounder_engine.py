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
                       "iv_bv_series": {ticker: {...}},
                       "fcf_growth": {ticker: {...}},
                       "book_value_growth": {ticker: {...}}},
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
import hashlib
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
ENGINE_VERSION = 29


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


def _overrides_signature(overrides):
    """Short stable suffix for the cache filename when non-default DCF
    overrides are in play - '' for the common case (pure auto, no
    overrides), so existing cache entries for that case keep working
    unchanged. A ticker viewed with two different override combinations
    (e.g. Auto vs a manual discount rate) needs two separate cache
    entries, since the resulting Fair Value numbers genuinely differ -
    a single per-ticker cache key would silently serve one caller's
    settings to another."""
    if not any(v is not None for v in overrides):
        return ""
    raw = json.dumps(overrides, sort_keys=True, default=str)
    return "_" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def _cache_path(ticker, overrides=()):
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in ticker.upper())
    return os.path.join(_cache_dir(), f"{safe}{_overrides_signature(overrides)}.json")


def _read_cache(ticker, overrides=()):
    path = _cache_path(ticker, overrides)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        meta = obj.get("_meta") or {}
        if meta.get("engine_version") != ENGINE_VERSION:
            return None
        # Audit fix (2.2): a bundle-format/logic change in fundamentals_data.py
        # bumps BUNDLE_VERSION, but this section cache previously had no idea
        # that number existed - a bundle fix with no ACCOMPANYING
        # ENGINE_VERSION bump (easy to forget; nothing enforced doing both)
        # meant every already-cached section kept serving results computed
        # from the OLD, broken bundle for up to 24h, since build_sections()
        # never even calls get_bundle() on a cache hit (see build_sections()'s
        # own comment) to notice anything changed. Tying BUNDLE_VERSION into
        # this cache's own validity check closes that gap structurally,
        # instead of relying on remembering to bump two constants by hand
        # every time. Old cache entries written before this field existed
        # (bundle_version missing) are correctly treated as stale - fetching
        # them once to fill in the size is the honest choice.
        if meta.get("bundle_version") != fundamentals_data.BUNDLE_VERSION:
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


def _write_cache(ticker, sections, overrides=()):
    path = _cache_path(ticker, overrides)
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


def _metric(ref, section, label, ticker, value, fmt, key=None, flagged=False, fallback_comment="", thresholds=None):
    """thresholds: normally left None, in which case this pulls the
    workbook's own thresholds for `label` (if any) - pass an explicit list
    of (lo, hi, color, band_label) tuples here only for a metric that has
    no hand-built workbook equivalent at all (e.g. "EBIT to FCF
    Conversion" in _build_fundamentals), where there's no workbook
    convention to transcribe and Andrew's own bands are used instead."""
    r = (ref.get(section) or {}).get(label)
    if thresholds is None:
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


def _year_end_prices(prices_10y, statement_df=None):
    """{fiscal_year_label: price as of (on or just before) that fiscal
    year's own real period-end date}.

    Bug fixed here: the previous version bucketed monthly closes by
    CALENDAR year and took the last one seen ("2024" -> the December 2024
    close) - correct only for a December-fiscal-year-end company. Every
    _eps_series()/_series() year label in this module comes from the
    statement column's own end-of-period date (e.g. "2024" from a
    "2024-06-30" column), so for any company with a non-calendar fiscal
    year - a 30 June year end is the norm for ASX-listed companies,
    Objective Corporation (OCL.AX) included - the old lookup paired a
    fiscal year's EPS with a closing price up to ~6 months after that
    fiscal year actually ended, producing a materially wrong PE (and
    everything downstream of it: PE Ratio Average, Fair Value's PE-based
    method, Retained Earnings' Value Created chart, Cost of Capital's
    WACC by year). Confirmed live: OCL.AX's "2024 PE" was computed from a
    December-2024 close against FY2024 (ended June 2024) EPS.

    When `statement_df` is given, each fiscal year label is instead
    matched to the nearest available monthly close ON OR BEFORE that
    column's own real end date (falling back to the earliest available
    price only if the series starts after the fiscal year-end). Without
    a statement_df, falls back to the old calendar-year bucketing -
    still needed by callers that only have year labels, not statement
    dates, in scope."""
    dates = prices_10y.get("dates") or []
    prices = prices_10y.get("prices") or []

    if statement_df is not None and not statement_df.empty:
        price_points = []
        for d, p in zip(dates, prices):
            try:
                price_points.append((datetime.date.fromisoformat(d[:10]), p))
            except (ValueError, TypeError):
                continue
        price_points.sort(key=lambda t: t[0])
        out = {}
        for c in statement_df.columns:
            m = re.search(r"(19|20)\d{2}-\d{2}-\d{2}", str(c))
            if not m:
                continue
            try:
                end_date = datetime.date.fromisoformat(m.group(0))
            except ValueError:
                continue
            on_or_before = [p for d, p in price_points if d <= end_date]
            candidate = on_or_before[-1] if on_or_before else (
                price_points[0][1] if price_points else None
            )
            if candidate is not None:
                out[str(end_date.year)] = candidate
        if out:
            return out

    # Fallback: old calendar-year-end bucketing (no statement dates given,
    # or none of them parsed) - the monthly series is chronological
    # ascending, so the last month seen per calendar year is that year's
    # most recent available close.
    out = {}
    for d, p in zip(dates, prices):
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


def _plausible_interest_for_debt(candidate, debt):
    """(value, estimated) - a real-world interest expense lands within
    roughly 0.5%-30% of the debt that generates it (implied_rate =
    interest / debt). Used both for the TTM interest figure
    (_interest_expense_ttm below) and per-year in the Cost of Capital
    WACC loop, where each historical year's own reported interest
    expense is checked against that same year's own debt.

    yfinance's raw "Interest Expense" row is, for some tickers (OCL.AX
    confirmed), off from the company's real finance costs by 1-2 orders
    of magnitude - REGARDLESS of which row it's read from (annual or
    quarterly). No amount of row-picking rescues a bad source value, so
    when the candidate fails this plausibility window (or is simply
    missing) this falls back to a flat 6%-of-debt estimate - a
    conservative placeholder borrowing cost, always marked estimated so
    callers can flag/red it - rather than propagate a number that's
    wrong by 100x+ into Interest Coverage and WACC.

    No debt to check against (0/None) -> the candidate is returned
    as-is, unestimated (there's nothing to gate it against; callers that
    need "no debt" to mean "omit this metric" handle that themselves -
    see _interest_expense_ttm)."""
    if not debt:
        return candidate, False
    if candidate is not None:
        implied_rate = abs(candidate) / debt
        if 0.005 <= implied_rate <= 0.30:
            return candidate, False
    return debt * 0.06, True


def _plausible_operating_income(candidate, revenue, info):
    """(value, estimated) - cross-checks the income-statement-row-derived
    operating income against operatingMargins, a ratio yfinance's `info`
    dict reports directly (not read off a row in the statement table), the
    same "second, independent source + plausibility band, fall back and
    flag" pattern _plausible_interest_for_debt already uses for interest
    expense.

    Confirmed necessary on FID.AX (Fiducian Group): the statement-row
    lookup returned an operating income implying a ~2% margin, while three
    independent sources (TradingView, stockanalysis.com, Stockopedia) all
    put this company's real operating margin at roughly 30%, and yfinance's
    own operatingMargins agreed with that - so the statement ROW was the
    bad value, not the company's actual reported data. Same failure mode
    as OCL.AX's interest expense (see _plausible_interest_for_debt), just
    showing up in a different row/ticker.

    No revenue, or operatingMargins missing from info -> candidate is
    returned as-is, unestimated (nothing independent to check it
    against)."""
    margin = (info or {}).get("operatingMargins")
    if not revenue or margin is None:
        return candidate, False
    expected = margin * revenue
    if candidate is not None:
        implied_margin = candidate / revenue
        margin_close = abs(implied_margin - margin) <= 0.25
        ratio_close = expected != 0 and 0.5 <= (candidate / expected) <= 2.0
        if margin_close or ratio_close:
            return candidate, False
    return expected, True


def _statement_col_dates(df):
    """[(column_label, datetime.date), ...] for statement columns whose
    label carries a parseable YYYY-MM-DD end date, newest first. Column
    labels look like '2026-06-30 00:00:00' (yfinance) or '2026-06-30'
    (EODHD/cache round-trip)."""
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    for c in df.columns:
        m = re.search(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", str(c))
        if not m:
            continue
        try:
            out.append((c, datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
        except ValueError:
            continue
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def _sum_q_row(income_q, key, cols):
    """Sum a mapped row over the given quarterly columns; None if the row
    is missing or any cell in the window is missing (a partial sum would
    silently understate a trailing-twelve-month figure)."""
    row = _find_row(income_q, _ROW_ALIASES.get(key, [key]))
    if row is None:
        return None
    total = 0.0
    for c, _ in cols:
        try:
            v = income_q.loc[row][c]
        except Exception:
            return None
        if v is None or (isinstance(v, float) and v != v):
            return None
        total += float(v)
    return total


def _q_row_value(income_q, key, col):
    """Single cell of a mapped row at one quarterly column; None if the
    row or cell is missing."""
    row = _find_row(income_q, _ROW_ALIASES.get(key, [key]))
    if row is None:
        return None
    try:
        v = income_q.loc[row][col]
    except Exception:
        return None
    if v is None or (isinstance(v, float) and v != v):
        return None
    return float(v)


def _half_year_cumulative_ttm(income_q, q_cols, last_annual_end=None):
    """TTM basic EPS for a half-yearly (ASX-style) reporter, reconstructed
    correctly from CUMULATIVE columns - or None when there isn't enough
    to work with (see _eps_ttm: for this reporting cadence, unlike a true
    discrete-quarter reporter, there is no safe fallback when this
    returns None - every column here is cumulative, so naively summing
    two of them is never correct, only sometimes wrong by more).

    yfinance's "quarterly" statement for a half-yearly reporter isn't
    made of independent discrete slices: each column is cumulative SINCE
    THAT FISCAL YEAR'S START - a 6-month total at the interim mark, a
    12-month total at the full-year close. (An earlier pass here cited
    specific CPU.AX column values as "confirmed" - a live diagnostic run
    on 2026-08-29 found CPU.AX's quarterly income statement actually came
    back EMPTY that day (q_cols=[], meta flag "quarterly_income_unavailable"),
    so this function was never even reached for CPU.AX and those numbers
    were never verified against real data. CPU.AX's actual TTM EPS bug
    turned out to be unrelated to this function entirely - see
    fundamentals_data.get_bundle()'s currency-conversion fix. This
    function's calendar-based classification is left in place as
    correct, defensive handling for the tickers where quarterly data IS
    available - it just wasn't CPU.AX's bug.)

    Bug fix: this used to classify the newest column by the RATIO of its
    value against the column before it (~1.5x-3.0x of its predecessor ->
    a full-year total; otherwise -> a fresh interim). That band is an
    assumption about a company's own H1-vs-H2 (or FY-vs-H1) split, and
    ordinary business reality - an uneven half, a one-off item, a weak
    or strong period on either side - can push the ratio outside it for
    reasons that have nothing to do with which case actually applies.
    When that happened, the function returned None and the caller fell
    straight through into the exact double-counting sum-of-both-columns
    bug this function exists to prevent (confirmed: CPU.AX's own ratio
    landed just above the 3.0x ceiling and hit this path).

    Classification is now by CALENDAR POSITION against the last ANNUAL
    column on file (`last_annual_end`), which is deterministic rather
    than sensitive to how the two halves happened to compare in size:
      * the newest column's period-end is ~12 months (330-400 days,
        tolerant of reporting-date drift) after the last annual column's
        end date -> the newest column is itself a new full fiscal year's
        cumulative total (its predecessor is that same year's own first
        half) -> TTM is that column's value ALONE, no summing.
      * ~6 months (150-215 days) after it -> the newest column is a
        fresh interim for a year that hasn't closed yet, and the
        predecessor is LAST year's full cumulative total -> TTM = newest
        interim + (last year's total - last year's own interim), i.e.
        this half plus the discrete other half of the trailing year
        (needs a 3rd column for that last term).
      * a gap in neither window -> don't guess, return None.
    Only when there's no annual column at all to anchor on
    (`last_annual_end` is None) does this fall back to the old
    value-ratio heuristic as a last resort, rather than as the primary
    signal."""
    if len(q_cols) < 2:
        return None
    newest, prior = q_cols[0], q_cols[1]
    v_newest = _q_row_value(income_q, "basic_eps", newest[0])
    v_prior = _q_row_value(income_q, "basic_eps", prior[0])
    if v_newest is None or v_prior is None:
        return None

    def _reconstruct_interim():
        if len(q_cols) < 3:
            return None
        v_prior_prior = _q_row_value(income_q, "basic_eps", q_cols[2][0])
        if v_prior_prior is None:
            return None
        return v_newest + (v_prior - v_prior_prior)

    if last_annual_end is not None:
        gap = (newest[1] - last_annual_end).days
        if 330 <= gap <= 400:
            return v_newest
        if 150 <= gap <= 215:
            return _reconstruct_interim()
        # Doesn't match either recognizable shape against the known
        # annual close - don't guess, let the caller fall back.
        return None

    if v_prior == 0:
        return None
    ratio = v_newest / v_prior
    if 1.5 <= ratio <= 3.0:
        return v_newest
    if ratio < 0 or ratio > 3.0:
        # Doesn't match either recognizable shape (e.g. a loss period
        # flipping the sign) - don't guess, let the caller fall back.
        return None
    return _reconstruct_interim()


def _eps_ttm(bundle, ticker=None):
    """(value, flagged). The TTM EPS every TTM-consuming metric uses.

    `ticker` is accepted (and otherwise unused) only as a ready hook for a
    future ticker-gated diagnostic print, the way CPU.AX's TTM EPS bug was
    root-caused on 2026-08-29 - see fundamentals_data.get_bundle()'s
    currency-conversion comment for what that investigation actually
    found (info["trailingEps"] was being converted twice, not a bug in
    this function's own classification logic).

    Yahoo's info["trailingEps"] can lag a just-reported fiscal year by
    weeks. Verified on OCL.AX (30 June FY end, FY26 reported ~Aug 2026):
    the workbook's Wise TTM EPS already showed $0.29 while trailingEps
    still said $0.38 - which made Retained Earnings (TTM) read $0.12
    against the workbook's $0.03 even though the EPS-minus-dividend
    subtraction itself was right.

    So: when the QUARTERLY income statement has columns ending AFTER the
    newest annual column, a genuine trailing-12-month EPS is summed from
    it instead - the last 4 quarterly or last 2 semiannual columns
    (ASX names report half-yearly; detected from the column spacing).
    Falls back to net-income-per-share over the same window when the
    quarterly statement carries no EPS row (flagged - today's share
    count), and to info["trailingEps"] when there is no fresher
    quarterly data at all (e.g. its own quarterly statement is
    unavailable that day - a routine yfinance gap, flagged
    "quarterly_income_unavailable" in the bundle's meta)."""
    info = bundle.get("info") or {}
    fallback = info.get("trailingEps")
    income_q = bundle.get("income_q")
    q_cols = _statement_col_dates(income_q)
    a_cols = _statement_col_dates(bundle.get("income"))
    # trailingEps is only as fresh as the statements behind it - when the
    # newest ANNUAL column is over a year old (a full new fiscal year has
    # ended and Yahoo hasn't ingested it yet - OCL.AX right after its Aug
    # 2026 FY26 release is the verified case: Yahoo's own quote page
    # still showed the FY25-based 0.38 while the market was already
    # trading the new numbers), the fallback figure is flagged red as a
    # stale estimate rather than presented as current.
    stale_annual = bool(a_cols) and (datetime.date.today() - a_cols[0][1]).days > 365
    if not q_cols or (a_cols and q_cols[0][1] <= a_cols[0][1]):
        return fallback, stale_annual
    gap_days = (q_cols[0][1] - q_cols[1][1]).days if len(q_cols) >= 2 else None
    n_needed = 2 if (gap_days is not None and gap_days > 135) else 4
    take = q_cols[:n_needed]
    period_days = gap_days if gap_days is not None else 90
    window_days = ((take[0][1] - take[-1][1]).days + period_days) if take else 0
    short_window = window_days < 330 or len(take) < n_needed

    if n_needed == 2:
        last_annual_end = a_cols[0][1] if a_cols else None
        cumulative_ttm = _half_year_cumulative_ttm(income_q, q_cols, last_annual_end)
        if cumulative_ttm is not None:
            return cumulative_ttm, short_window
        # Every column in a half-yearly reporter's quarterly statement is
        # CUMULATIVE (see _half_year_cumulative_ttm's docstring) - there
        # is no safe naive-sum fallback the way there is for a genuine
        # discrete-quarter reporter below: summing two cumulative columns
        # always double-counts the first half, regardless of which case
        # actually applies. Bug fix: this used to fall through into
        # exactly that `_sum_q_row` summing when the reconstruction
        # above returned None - reproducing the same "TTM ~45% too high"
        # bug the reconstruction exists to prevent, just for whichever
        # inputs happened to fall outside its classification. Falling
        # back to the flagged annual figure instead is honest about not
        # having a reliable fresh number, rather than guessing wrong.
        return fallback, True

    eps_sum = _sum_q_row(income_q, "basic_eps", take)
    if eps_sum is not None:
        return eps_sum, short_window

    ni_sum = _sum_q_row(income_q, "net_income", take)
    shares = info.get("sharesOutstanding")
    if ni_sum is not None and shares:
        return ni_sum / shares, True

    return fallback, stale_annual


def _interest_expense_ttm(bundle):
    """(value, flagged, estimated). Two layers of defense against a bad
    interest-expense figure:

    1) WHICH statement row: a genuine trailing-4-quarter sum, not "the
       latest annual column" (every other TTM figure in this app's
       convention) - interest expense specifically breaks that
       convention badly right after a company takes on new debt
       mid-fiscal-year, when the last completed annual report can still
       predate the debt (near-zero interest expense) while the real
       run-rate has already jumped. Falls back (flagged) to the annual
       column when quarterly data isn't available at all.

    2) WHETHER the resulting value is even plausible against the
       company's total debt (see _plausible_interest_for_debt) - added
       because step 1 alone did NOT rescue OCL.AX: its quarterly income
       statement has no usable interest row, so it fell back to the
       same bad annual column (~$15K, vs real finance costs of roughly
       $1-2M), producing an Interest Coverage of ~2,024x. The problem
       was the source VALUE, not which row it came from.

    No total debt on the balance sheet at all -> (None, True, False):
    there's nothing to estimate a borrowing cost against, so
    interest-based metrics are simply omitted by their callers."""
    income_q = bundle.get("income_q")
    candidate, candidate_flagged = None, True
    if income_q is not None and not income_q.empty:
        q_series = [(y, v) for y, v in _series(income_q, "interest_expense") if v is not None]
        if q_series:
            last4 = q_series[:4]
            candidate = sum(abs(v) for _, v in last4)
            candidate_flagged = len(last4) < 4
    if candidate is None:
        annual = _latest(bundle["income"], "interest_expense")
        if annual is not None:
            candidate = annual
            candidate_flagged = True

    total_debt = _latest(bundle["balance"], "total_debt")
    if not total_debt:
        return None, True, False

    value, estimated = _plausible_interest_for_debt(candidate, total_debt)
    return value, (candidate_flagged or estimated), estimated


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
    """{"years": [...], "values": [...], "ytd_year": str|None} - each
    COMPLETED year's bar is (avg price that year - avg price the year
    before) / avg price the year before, matching the hand-built
    workbook's own convention exactly (confirmed against real workbook
    output - AUB.AX/CSL.AX/RMD.AX all show this same average-vs-average
    shape, including a large swing on their own still-in-progress year).

    The one exception is the LATEST year when it's still in progress
    (fewer than 12 monthly points): averaging a partial year against a
    full prior year lets whichever months happened to occur so far
    (e.g. an early-year slump) dominate the whole bar, even while the
    stock has since recovered - confirmed on CPU.AX, whose 2026 bar read
    -7.7% (average-vs-average, dragged down by a Jan-Mar trough) while
    the actual price was +17-22% since the 2025 close/2026 open by
    August. For that one open year only, use a plain start-of-year-close
    vs latest-close return instead - a real YTD number, not blended
    against last year's now-closed average. Every completed year is
    untouched."""
    dates, prices = prices_10y.get("dates") or [], prices_10y.get("prices") or []
    if len(dates) < 24:
        return None
    by_year = {}
    for d, p in zip(dates, prices):
        by_year.setdefault(d[:4], []).append(p)
    years_sorted = sorted(by_year)
    avgs = {y: sum(v) / len(v) for y, v in by_year.items()}
    latest_year = years_sorted[-1]
    latest_is_partial = len(by_year[latest_year]) < 12
    out_years, out_values, ytd_year = [], [], None
    for i in range(1, len(years_sorted)):
        y0, y1 = years_sorted[i - 1], years_sorted[i]
        if y1 == latest_year and latest_is_partial:
            year_prices = by_year[y1]
            if year_prices[0]:
                out_years.append(y1)
                out_values.append((year_prices[-1] - year_prices[0]) / year_prices[0])
                ytd_year = y1
            continue
        if avgs[y0]:
            out_years.append(y1)
            out_values.append((avgs[y1] - avgs[y0]) / avgs[y0])
    if not out_years:
        return None
    return {
        "years": list(reversed(out_years)),
        "values": list(reversed(out_values)),
        "ytd_year": ytd_year,
    }


def _cov_corr(a_series, b_series):
    """(cov, corr, var_a) - covariance, correlation and variance of `a`'s
    (the stock's) raw monthly closing PRICES against `b`'s (the S&P
    500's) - i.e. Excel's COVARIANCE.S(priceRange, priceRange) / CORREL /
    VAR.S applied directly to the two price series, NOT to returns.

    This used to run on month-over-month % returns instead, which is the
    more standard finance-textbook definition - but it doesn't match this
    site's own reference: the hand-built workbook's real output (Andrew's
    own numbers, in compounder_data.json) shows Covariance (SP500) values
    of 11,112 / 22,311 / 13,261 for AUB.AX / CSL.AX / RMD.AX - three and
    four DIGITS, only possible when both series are raw price LEVELS
    (a stock trading in the hundreds against an index in the thousands).
    A returns-based covariance is a small fraction (typically
    0.0001-0.01) and would round to "0.00" on every single ticker - which
    is exactly the bug reported (0.00 / 0.36x / 0.01 shown for a live
    ticker, when the workbook's own real tickers all read in the
    thousands and the correlations spread from ~0.2 to ~0.9). Switched
    to price levels to match; Variance (the stock's own var_a) follows
    the same basis so Correlation = Cov / sqrt(Var_a * Var_b) stays
    internally consistent."""
    a_by_date = dict(zip(a_series.get("dates") or [], a_series.get("prices") or []))
    b_by_date = dict(zip(b_series.get("dates") or [], b_series.get("prices") or []))
    common = sorted(set(a_by_date) & set(b_by_date))
    if len(common) < 4:
        return None, None, None
    a_vals = [a_by_date[d] for d in common]
    b_vals = [b_by_date[d] for d in common]
    n = len(a_vals)
    mean_a, mean_b = sum(a_vals) / n, sum(b_vals) / n
    cov = sum((a_vals[i] - mean_a) * (b_vals[i] - mean_b) for i in range(n)) / (n - 1)
    var_a = sum((x - mean_a) ** 2 for x in a_vals) / (n - 1)
    var_b = sum((x - mean_b) ** 2 for x in b_vals) / (n - 1)
    corr = cov / math.sqrt(var_a * var_b) if var_a > 0 and var_b > 0 else None
    return cov, corr, var_a


def _avg_pe_3pt(bundle):
    """"PE Ratio Average": the workbook's real AU = AVERAGE(PE_TTM,
    PE_lastFY, PE_prevFY) - the 3 most recent P/E points only, NOT an
    all-years average. The previous all-years average badly inflated
    high-multiple stocks (the primary cause of Fair Value being reported
    as unreasonably large before this fix). Skips non-positive-EPS years/
    TTM; falls back to info["trailingPE"] only if nothing is computable."""
    info = bundle.get("info") or {}
    price_now = info.get("currentPrice") or info.get("regularMarketPrice")
    trailing_eps, _ = _eps_ttm(bundle)
    year_end_prices = _year_end_prices(bundle["prices_10y"], bundle["income"])
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
    trailing_eps, _ = _eps_ttm(bundle)
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

def _run_dcf(bundle, ticker, discount_rate=None, perpetual_rate=None, growth_rate=None, manual_fcf=None):
    """Runs the SAME dcf_intrinsic_value() the main site's Deep Dive page
    uses for its own headline Intrinsic Value/Margin of Safety - not a
    separate valuation model. Left with every override at None (the
    default), this auto-resolves exactly like the main site's Deep Dive
    page does whenever ITS Auto mode is on and no per-ticker override is
    set: CAPM discount rate, min(analyst 5y estimate, historical FCF
    CAGR) growth, market-cap-tiered ceiling.

    When a caller passes the same discount_rate/perpetual_rate/
    growth_rate/manual_fcf the main site is currently using for this
    ticker (its global Valuation & FCF Inputs settings, resolved against
    any per-ticker override - see app.py's _dcf_overrides_for()), the
    resulting Fair Value DCF genuinely matches the main site's own
    number for the same ticker, rather than the two silently diverging
    whenever the main site's Auto mode is off or a manual override is in
    play. g_earn used by PE Forward, the Rational Compounder Method's
    earnings term, and the Value vs Book IV/BV series all come from this
    same result (see _build_fair_value/_build_value_vs_book), so this
    one change is what keeps every growth-driven auto figure aligned
    with whichever settings actually produced it."""
    info = bundle.get("info") or {}
    currency = info.get("currency") or "USD"

    # Double-FX-conversion fix: fundamentals_data.get_bundle() already
    # converts every statement DataFrame in this bundle (including
    # "cashflow") from the company's reporting currency to its listing
    # currency whenever they differ - e.g. CSL.AX/RMD.AX-style ASX names
    # that report in USD despite trading in AUD (see
    # _convert_statement_currency in fundamentals_data.py). That's correct
    # for every OTHER section built from this bundle (Fundamentals,
    # Retained Earnings, the IV/BV series's own TTM FCF point, etc.), which
    # read the DataFrame directly and have no FX logic of their own.
    #
    # dcf_intrinsic_value() is different: it does its OWN
    # financialCurrency-vs-currency conversion internally (designed for the
    # main site's caller, which always passes a RAW, un-converted
    # cashflow_df). Handed this bundle's already-converted cashflow_df
    # alongside the unmodified info["financialCurrency"], it can't tell the
    # conversion already happened - it sees the same currency mismatch and
    # applies the SAME fx rate a second time, squaring the effective
    # conversion for any ticker where the two currencies differ. Passing a
    # copy of `info` with financialCurrency overridden to match the
    # listing currency (only for this one call - the bundle's real info,
    # used everywhere else, is untouched) tells it "already converted, skip
    # it" instead.
    dcf_info = info
    if (info.get("financialCurrency") or "").upper() != currency.upper():
        dcf_info = dict(info)
        dcf_info["financialCurrency"] = currency

    result = _safe(
        fcf_valuation_engine.dcf_intrinsic_value,
        ticker, info=dcf_info, cashflow_df=bundle.get("cashflow"), currency=currency,
        discount_rate=discount_rate, perpetual_rate=perpetual_rate,
        growth_rate=growth_rate, manual_fcf=manual_fcf,
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
    operating_income, operating_income_estimated = _plausible_operating_income(
        operating_income, revenue, bundle.get("info")
    )
    pretax_income = _latest(income, "pretax_income")
    tax_provision = _latest(income, "tax_provision")
    interest_expense, interest_expense_flagged, interest_expense_estimated = _interest_expense_ttm(bundle)

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

    def add(label, value, fmt, key=None, flagged=False, fallback="", thresholds=None):
        if value is None:
            return
        metrics.append(_metric(ref, "Fundamentals", label, ticker, value, fmt, key=key, flagged=flagged,
                                fallback_comment=fallback, thresholds=thresholds))

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
    add("Interest Coverage", interest_coverage, "x",
        flagged=(interest_expense is None or interest_expense_flagged or interest_expense_estimated
                 or operating_income_estimated),
        fallback="Operating income divided by a trailing-4-quarter interest expense; when either "
                  "figure is implausible against total debt or the company's own reported margin, "
                  "it's estimated from a second, independent source and shown in red.")

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

    # EBIT to FCF Conversion: how much of operating earnings actually
    # shows up as free cash flow - Andrew's own formula/bands (no workbook
    # equivalent exists for this one, so there's no cell to transcribe;
    # confirmed via compounder_data.json - nothing under this or a similar
    # label anywhere in the hand-built data). FCF uses the same
    # OCF-minus-CapEx-or-statement-row convention as every other FCF
    # figure already on this page (Free Cash Flow (TTM), Free Cash Flow
    # Yield, PFCF Ratio) rather than a second definition.
    #
    # Flagged when EBIT is zero/negative or itself estimated
    # (operating_income_estimated) - the ratio flips sign/meaning in a
    # misleading way once the denominator goes negative (e.g. a small
    # negative FCF over a small negative EBIT can read as a big *positive*
    # percentage that says nothing about cash-conversion quality), so it's
    # shown with the same red-asterisk "don't take this number at face
    # value" treatment as every other fragile ratio on this page rather
    # than silently presented as a normal band.
    ebit_fcf_conversion = (fcf / operating_income) if (fcf is not None and operating_income) else None
    add("EBIT to FCF Conversion", ebit_fcf_conversion, "pct",
        flagged=bool(operating_income_estimated or (operating_income is not None and operating_income <= 0)),
        fallback="Free Cash Flow divided by Operating Income (EBIT). Above 80% = exceptional cash "
                  "conversion, very low capital intensity. 50-80% = normal - some earnings absorbed by "
                  "working capital or capex. Below 50% = weak - earnings aren't converting well to cash.",
        thresholds=[
            [None, 0.50, "red", "Weak - earnings heavily tied up in working capital or capex"],
            [0.50, 0.80, "amber", "Average - normal operating cash generation"],
            [0.80, None, "green", "Exceptional - converts almost all EBIT into cash"],
        ])
    add("Intangibles To Total Assets", (goodwill_intangibles / total_assets) if (goodwill_intangibles is not None and total_assets) else None, "pct")
    add("Price to Equity Ratio", (mcap / equity) if (mcap and equity) else None, "x")

    rota = (net_income / (total_assets - (goodwill_intangibles or 0))) if (net_income is not None and total_assets is not None) else None
    add("Return on Tangible Assets", rota, "pct")
    add("ROE", (net_income / equity) if (net_income is not None and equity) else None, "pct")
    add("Operating Income Ratio", (operating_income / revenue) if (operating_income is not None and revenue) else None, "pct",
        flagged=operating_income_estimated,
        fallback="Operating income divided by revenue; when the statement figure is implausible "
                  "against the company's own reported operating margin, it's estimated from that "
                  "margin instead and shown in red.")
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

    cov, corr, var_a = _cov_corr(bundle["prices_10y"], bundle["spx_prices_10y"])
    # Audit fix 1.6: this fallback text said "monthly returns", but
    # _cov_corr() deliberately computes on raw monthly price LEVELS (see
    # its own docstring for why - matches the reference workbook). The
    # code was already correct; only this caption was describing the
    # opposite of what it actually shows.
    add("Covariance (SP500)", cov, "num", fallback="Covariance of monthly closing price levels vs the S&P 500 over the available price history.")
    add("Correlation (SP500)", corr, "x", fallback="Correlation of monthly closing price levels vs the S&P 500 over the available price history.")
    add("Variance", var_a, "num", fallback="Variance of the stock's own monthly closing price levels over the same price history used for "
                                            "Covariance and Correlation above.")

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

        # TTM point: TTM FCF from the SAME source/convention as every
        # other point on this chart AND as the "Free Cash Flow (TTM)"
        # metric card shown right next to it - the latest statement
        # year's FCF (or OCF-capex fallback), never info["freeCashflow"].
        # That info-blob field was tried first here previously; for some
        # tickers (MKTX confirmed) it disagrees badly with the statement
        # figure - almost certainly a different convention (Yahoo's
        # "freeCashflow" reads as a levered figure, after financing
        # activity, vs the unlevered OCF-minus-capex convention used
        # everywhere else in this app). Fed through this mini-DCF's 9-year
        # growth compounding plus a Gordon terminal value, that mismatch
        # didn't just nudge the TTM point, it produced a wildly negative
        # "intrinsic value" for TTM alone while every FY bar (which never
        # used info["freeCashflow"]) looked normal. MKTX's real TTM free
        # cash flow was, per outside sources, a healthy positive figure -
        # down sharply from the prior year, but nowhere near negative.
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


def _fcf_growth_entry(bundle):
    """{"years": [...], "values": [...]} newest-first - year-over-year Free
    Cash Flow growth, one bar per pair of consecutive points, over a TTM
    point (when available) plus up to the most recent 9 fiscal years of
    statement data - up to 10 total data points, same "as much history as
    the statements have, capped at 10" convention as _ten_year_retained
    and every other "10y ..." metric in this module, giving up to 9 growth
    bars (a ticker with only 4-5 years of statements on file correctly
    gets fewer, not a padded or fabricated 9). Same (v - v0) / abs(v0)
    year-over-year convention, and the same TTM-vs-most-recent-FY handling,
    as the Earnings Trends tab's own "EPS Growth by Year" chart (see
    _build_earnings_trends's full_eps/eps_growth) - just for FCF instead
    of EPS. Bug fixed here: this originally never included a TTM point at
    all (fcf_series was fiscal years only), so a ticker with N years of
    statements silently showed only N-1 growth bars instead of the N bars
    a TTM-vs-newest-FY point would add - inconsistent with the IV/BV
    series chart directly above this one on the same tab, which does
    include TTM and so visibly covered one more year than this chart did.
    FCF read convention (FCF-row-or-OCF-minus-CapEx fallback, and the TTM
    point specifically using the statement figure rather than
    info["freeCashflow"] - see _iv_bv_series's own comment on why) is
    shared with _iv_bv_series/_build_fundamentals/_build_value_vs_book."""
    fcf_series = [(y, v) for y, v in _series(bundle["cashflow"], "free_cash_flow") if v is not None]
    if not fcf_series:
        ocf_s = dict(_series(bundle["cashflow"], "operating_cash_flow"))
        capex_s = dict(_series(bundle["cashflow"], "capex"))
        fcf_series = [
            (y, ocf_s[y] - abs(capex_s[y]))
            for y in ocf_s
            if y in capex_s and ocf_s[y] is not None and capex_s[y] is not None
        ]

    fcf_ttm = _latest(bundle["cashflow"], "free_cash_flow")
    if fcf_ttm is None:
        ocf_ttm = _latest(bundle["cashflow"], "operating_cash_flow")
        capex_ttm = _latest(bundle["cashflow"], "capex")
        if ocf_ttm is not None and capex_ttm is not None:
            fcf_ttm = ocf_ttm - abs(capex_ttm)
    full_fcf = ([("TTM", fcf_ttm)] if fcf_ttm is not None else []) + fcf_series
    full_fcf = full_fcf[:10]

    out_years, out_values = [], []
    for i in range(len(full_fcf) - 1):
        y, v = full_fcf[i]
        _, v0 = full_fcf[i + 1]
        if v0:
            out_years.append(y)
            out_values.append((v - v0) / abs(v0))
    if not out_years:
        return None
    return {"years": out_years, "values": out_values}


def _book_value_growth_entry(bundle):
    """{"years": [...], "values": [...]} newest-first - year-over-year Book
    Value (stockholders' equity, on a per-share basis - see below) growth,
    one bar per pair of consecutive fiscal years, over up to the most
    recent 10 fiscal years of statement data. Same (v - v0) / abs(v0)
    year-over-year convention and up-to-10/9-bars cap as _fcf_growth_entry.

    No separate TTM point here, unlike _fcf_growth_entry/eps_growth: this
    bundle only ever carries an ANNUAL balance sheet (see fundamentals_
    data.py's own bundle-shape comment - there's no quarterly balance
    sheet to derive a true trailing-twelve-month figure from, the way
    income/cashflow's TTM is summed from real quarterly statements). The
    newest annual column already IS the most current equity snapshot this
    bundle has - the same "latest" value _bvps() itself uses for the
    Fundamentals tab's own "Book Value Per Share" card - so it's already
    the first (newest) point in equity_series below, not a value distinct
    from it worth listing twice.

    Computed on raw stockholders' equity rather than per-share BVPS
    explicitly - immaterial to the output, not a simplification: BVPS_y =
    equity_y / shares uses TODAY's constant share count for every
    historical year (historical share counts aren't available - same
    convention _iv_bv_series/_eps_series already use), so that constant
    shares term cancels out of the (v - v0) / abs(v0) growth ratio
    exactly, whether v/v0 are raw equity or equity/shares. Left as raw
    equity so this reads directly off the balance sheet with no shares
    lookup that could itself be missing and silently drop the whole
    series."""
    equity_series = [(y, v) for y, v in _series(bundle["balance"], "stockholders_equity") if v is not None]
    equity_series = equity_series[:10]

    out_years, out_values = [], []
    for i in range(len(equity_series) - 1):
        y, v = equity_series[i]
        _, v0 = equity_series[i + 1]
        if v0:
            out_years.append(y)
            out_values.append((v - v0) / abs(v0))
    if not out_years:
        return None
    return {"years": out_years, "values": out_values}


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
    fcf_growth = _fcf_growth_entry(bundle)
    book_value_growth = _book_value_growth_entry(bundle)

    return {
        "metrics": metrics,
        "iv_bv_bar": {ticker: iv_bv_bar} if iv_bv_bar else {},
        "iv_bv_series": {ticker: iv_bv_series} if iv_bv_series else {},
        "fcf_growth": {ticker: fcf_growth} if fcf_growth else {},
        "book_value_growth": {ticker: book_value_growth} if book_value_growth else {},
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

    With FULL statement depth (>= 10 FYs) the four workbook horizons
    render under their own labels, exactly as the hand-built data does.

    With SHALLOW depth (yfinance's usual 4-5 years) the horizons the data
    can't cover aren't faked and aren't hidden - per the owner's own
    choice ("we can only do 4y, 2y and 1y for this one"): the chart
    switches to the honest windows the data DOES support:
      "1Y"  - the TTM year alone: TTM retained earnings vs the price
              change since the last fiscal year-end.
      "2Y"  - the true 2-FY window (same formula as the workbook's 2Y).
      "NY*" - one cumulative max-available bar (e.g. "4Y*"): TTM retained
              plus every available FY, price change from the oldest
              year-end to today - starred, with "_years_available" carried
              so the UI caption can spell the span out. Skipped when it
              would duplicate 2Y.
    A negative value on a covered window is real data and still renders -
    the workbook allows it too."""
    eps_series = [(y, v) for y, v in _eps_series(bundle) if v is not None]
    dps_by_year = _dividends_per_share_by_year(bundle.get("dividends") or {})
    year_end_prices = _year_end_prices(bundle["prices_10y"], bundle["income"])
    n_avail = len(eps_series)

    # Fiscal-year-end dates by year label, for the "measured from -> to"
    # annotation each horizon carries (rendered as a caption under the
    # chart by compounder_ui) - so a bar anchored at "Jun 2025 -> today"
    # can never be mistaken for a calendar-year window.
    fy_dates = {}
    for c, d in _statement_col_dates(bundle.get("income")):
        m = re.search(r"(19|20)\d{2}", str(c))
        if m and m.group(0) not in fy_dates:
            fy_dates[m.group(0)] = d

    def _fy_end(y):
        d = fy_dates.get(y)
        return d.strftime("%b %Y") if d else f"FY{y} end"

    def _fy_window(n_years, key):
        """Workbook-style fixed-FY horizon (2Y/5Y/10Y formula)."""
        slice_ = eps_series[:n_years]
        if len(slice_) < n_years:
            return None
        re_val = sum(eps - dps_by_year.get(y, 0.0) for y, eps in slice_)
        end_price = year_end_prices.get(slice_[0][0])
        start_price = year_end_prices.get(slice_[-1][0])
        if re_val in (None, 0) or end_price is None or start_price is None:
            return None
        return {
            "retained_earnings": re_val,
            "value_created": (end_price - start_price) / re_val,
            "window": f"{_fy_end(slice_[-1][0])} \u2192 {_fy_end(slice_[0][0])}",
        }

    out = {}

    if n_avail >= 10:
        # Full workbook horizons, exactly as the hand-built data.
        for label, n_years in (("2Y", 2), ("5Y", 5), ("10Y", 10)):
            entry = _fy_window(n_years, label)
            if entry:
                out[label] = entry
        # TTM = the workbook's "11Y (From TTM)": TTM retained plus the
        # last 10 FYs, price change from that window's oldest year-end
        # to today.
        ttm_slice = eps_series[:10]
        if retained_ttm is not None:
            re_val = retained_ttm + sum(eps - dps_by_year.get(y, 0.0) for y, eps in ttm_slice)
            start_price = year_end_prices.get(ttm_slice[-1][0])
            if re_val not in (None, 0) and start_price is not None and price_now is not None:
                out["TTM"] = {
                    "retained_earnings": re_val,
                    "value_created": (price_now - start_price) / re_val,
                    "window": f"{_fy_end(ttm_slice[-1][0])} \u2192 today",
                }
    else:
        # Shallow depth: 1Y / 2Y / max-available cumulative.
        if retained_ttm not in (None, 0) and eps_series and price_now is not None:
            last_fy_price = year_end_prices.get(eps_series[0][0])
            if last_fy_price is not None:
                out["1Y"] = {
                    "retained_earnings": retained_ttm,
                    "value_created": (price_now - last_fy_price) / retained_ttm,
                    "window": f"{_fy_end(eps_series[0][0])} \u2192 today",
                }
        entry = _fy_window(2, "2Y")
        if entry:
            out["2Y"] = entry
        if n_avail > 2 and retained_ttm is not None and price_now is not None:
            re_val = retained_ttm + sum(eps - dps_by_year.get(y, 0.0) for y, eps in eps_series)
            start_price = year_end_prices.get(eps_series[-1][0])
            if re_val not in (None, 0) and start_price is not None:
                out[f"{n_avail}Y*"] = {
                    "retained_earnings": re_val,
                    "value_created": (price_now - start_price) / re_val,
                    "window": f"{_fy_end(eps_series[-1][0])} \u2192 today",
                }

    if not out:
        return None
    out["_years_available"] = n_avail
    return out


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
    trailing_eps, eps_ttm_flagged = _eps_ttm(bundle, ticker=ticker)
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
    add("EPS (TTM)", trailing_eps, "cur", flagged=eps_ttm_flagged)
    add("Dividend (TTM)", div_ttm, "cur", flagged=div_ttm_flagged)
    add("Ratio P/(E-D)", ratio_p_ed, "x", flagged=div_ttm_flagged or eps_ttm_flagged)
    add("Dividend Yield (TTM)", (div_ttm / price) if (div_ttm is not None and price) else None, "pct", flagged=div_ttm_flagged)
    # "Retained Earnings (TTM)" is the raw per-share dollar figure (EPS
    # minus Dividend) - format "cur", NOT "pct". The workbook's own BQ
    # cell (Dividend Ratio sheet) is genuinely currency-formatted
    # ($0.00); the "pct" tag on this metric was an importer mistake
    # (PLAIN_EXTRA["Dividend Ratio"] had ("BQ", "pct")) that this engine
    # inherited without re-checking the workbook's actual cell format. A
    # round-1 comment here claimed the pct formatter was "confirmed
    # against a covered ticker" - only the VALUE matched (AUB.AX:
    # 1.62 - 0.93 = 0.69), the format never did. A negative value here is
    # a real, possible result (trailing payout > 100%), not a bug.
    add("Retained Earnings (TTM)", retained_ttm, "cur", flagged=True)
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
    trailing_eps, _ = _eps_ttm(bundle, ticker=ticker)
    full_eps = ([("TTM", trailing_eps)] if trailing_eps is not None else []) + fy_eps
    year_end_prices = _year_end_prices(bundle["prices_10y"], bundle["income"])
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
    # Every "10y ..." metric below is built from the SAME fy_vals as this
    # one - when statement depth is short (n_fy < 10, e.g. a name with
    # only 4 years of filings on record), they're all silently computed
    # over fewer years than their label claims, not just this average.
    # Previously only this one metric carried the flag/caption explaining
    # that; the other three ("10y EPS  Variance", "10y EPS SD", "10y
    # AVG+SD") showed with no indicator at all, which is exactly what
    # made a 4-statement-year ticker's "4y EPS SD" and "10y EPS SD" come
    # out byte-identical with no explanation (4y's own window IS the
    # full n_fy=4 available, so four_vals == fy_vals exactly in that
    # case - correct, not a coincidence, but invisible without the flag).
    ten_y_flag = n_fy < 10
    ten_y_fallback = f"Computed over the {n_fy} fiscal year(s) of statements available (excludes TTM), not the full 10."
    four_y_flag = n_fy < 4
    four_y_fallback = f"Computed over the {min(4, n_fy)} fiscal year(s) of statements available (excludes TTM), not the full 4."
    add("10y Average Earnings", ten_avg, "cur", flagged=ten_y_flag,
        fallback=f"Average EPS over the {n_fy} fiscal year(s) of statements available (excludes TTM).")
    add("4y Average Earnings", four_avg, "cur", flagged=four_y_flag, fallback=four_y_fallback)
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
        add("10y EPS  Variance", variance, "pct", flagged=ten_y_flag, fallback=ten_y_fallback)
        add("10y EPS SD", sd, "cur", flagged=ten_y_flag, fallback=ten_y_fallback)
        add("10y AVG+SD", ten_avg + sd, "x", flagged=ten_y_flag, fallback=ten_y_fallback)
        four_vals = fy_vals[:4]
        if len(four_vals) >= 2:
            four_var = sum((v - four_avg) ** 2 for v in four_vals) / len(four_vals)
            four_sd = math.sqrt(four_var)
            add("4y EPS SD", four_sd, "cur", flagged=four_y_flag, fallback=four_y_fallback)
            add("4y AVG+SD", four_avg + four_sd, "x", flagged=four_y_flag, fallback=four_y_fallback)

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
    computed from the current date (never hardcoded).

    compounder_data.json's real hand-built periods are TTM/2025/2021/2016
    (i.e. -1/-5/-10 relative to when that workbook was generated) - a
    prior version of this function matched that spacing exactly. But that
    workbook is built on a paid data source with 10+ years of statement
    history; this app's free yfinance feed typically only has ~4 years on
    file, so -1 collides with the latest fiscal year (skipped by the
    duplicate-period guard below) and -5/-10 fall outside the data
    entirely - every computed year got skipped, leaving only the TTM bar
    on the WACC vs ROIC chart for every free-tier ticker.

    Andrew (the site owner) explicitly chose -2/-4/-5 instead, specifically
    because it fits within the free feed's real depth (confirmed: OCL.AX's
    2022-2025 statements yield TTM + 2024 + 2022 bars, with 2021 appearing
    once a 5th year of data exists). This is a deliberate product decision
    for the auto (free-tier) view, not a formula-accuracy bug to be
    "corrected" back to the workbook's own spacing - do not revert this."""
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
    interest_expense, interest_expense_flagged, interest_expense_estimated = _interest_expense_ttm(bundle)
    pretax_income = _latest(bundle["income"], "pretax_income")
    tax_provision = _latest(bundle["income"], "tax_provision")
    revenue = _latest(bundle["income"], "revenue")
    operating_income, _operating_income_estimated = _plausible_operating_income(
        _latest(bundle["income"], "operating_income"), revenue, info
    )
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
    year_end_prices = _year_end_prices(bundle["prices_10y"], bundle["income"])

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
    wacc_ttm, wacc_ttm_flagged = _wacc_for(
        mcap, ltd_ttm, interest_expense,
        ltd_ttm_flagged or interest_expense_flagged or interest_expense_estimated,
    )
    roic_ttm = _roic_for(years_desc[0]) if years_desc else None
    if roic_ttm is None and operating_income is not None and years_desc:
        # _roic_for() looks operating income up by the BALANCE SHEET's own
        # newest year label, in a dict keyed by the INCOME STATEMENT's own
        # year labels - normally the same year, but when the two
        # statements' fiscal year-end dates land in different calendar
        # years (seen on FID.AX) that lookup misses and silently returns
        # None, taking the whole TTM bar (and often the whole chart, if
        # every historical year has the same mismatch) down with it - not
        # a bad VALUE this time, a lookup that finds nothing. Recover
        # using the plausibility-checked operating_income above (same
        # value/fallback the Fundamentals tab's ROIC now uses) paired with
        # the TTM invested capital, rather than leave the TTM point
        # missing whenever the two statements' years don't line up.
        ic_ttm = _avg_invested_capital_for_year(
            equity_by_year, debt_by_year, cash_by_year, years_desc, years_desc[0]
        )
        if ic_ttm:
            roic_ttm = (operating_income * (1 - tax_ttm)) / ic_ttm
    # Per the owner's choice: a period only renders when BOTH bars can be
    # computed - a WACC-only (or ROIC-only) bar reads as a rendering
    # glitch rather than a real, deliberate data gap. Same rule applied
    # to the historical-year loop below, for the identical reason.
    if wacc_ttm is not None and roic_ttm is not None:
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
        # Same plausibility gate as the TTM figure (_plausible_interest_
        # for_debt), applied per-year: that year's own reported interest
        # expense checked against that year's own debt, estimated at 6%
        # of debt when it fails the window.
        int_y, int_y_estimated = _plausible_interest_for_debt(interest_by_year.get(target_year), ltd_y)
        wacc_y, wacc_y_flagged = _wacc_for(e_y, ltd_y, int_y, ltd_y_flagged or int_y_estimated)
        roic_y = _roic_for(target_year)
        # Confirmed on a real ticker (YELP, 2021): WACC can compute for a
        # year (it only needs price/shares/debt/interest) while ROIC
        # can't (a specific year's Operating Income cell is missing from
        # the statement, even though the column itself exists) - leaving
        # a WACC bar with no ROIC partner, which reads as a chart bug
        # rather than the real data gap it is. Skip the whole period
        # unless BOTH compute, so every bar shown always has its pair.
        if wacc_y is None or roic_y is None:
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
    add("Interest Expense (TTM)", interest_expense, "cur",
        flagged=interest_expense_flagged or interest_expense_estimated)
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
    """Historical per-share stockholders'-equity CAGR, gated the same way
    fcf_valuation_engine.estimate_growth() gates the DCF's own growth rate -
    the raw endpoint-to-endpoint CAGR alone let a thin, noisy equity base on
    a micro-cap compound into a fantasy valuation (84%+ raw on a name like
    ETST).

    Two backstops, applied independently:
      - CLEAN check: trusted only when the per-share series isn't too
        volatile (coefficient of variation <= 0.60, same threshold and same
        helper the DCF's own historical-CAGR trust check uses). A noisy
        series (a capital raise, an impairment) makes the CAGR endpoints
        meaningless regardless of sign, so an unclean series returns
        unavailable entirely (None) rather than a number - equity growth
        has no second source to fall back to the way the DCF does, and this
        method already degrades gracefully to "not shown" for missing
        balance-sheet data, so that's the natural failure mode here too.
      - FLOOR/CEILING: a clean rate is then clamped to [0, ceiling], where
        ceiling is the SAME market-cap-tiered ceiling the DCF uses
        (fcf_valuation_engine.growth_ceiling_for - 8%/12%/16%/20% by size)
        rather than a second, separately-tuned number. The floor stops a
        shrinking-equity name from producing a negative fair value; the
        ceiling stops a hot-but-clean CAGR from compounding into the same
        kind of fantasy number for 10 years.

    Returns (growth_rate, capped) - capped is True only when the ceiling
    actually bound (mirrors the DCF's own governor="Cap"), so callers can
    flag the number as an estimate the same way the DCF does. Returns
    (None, False) whenever the series isn't usable at all.
    """
    equity_series = [(y, v) for y, v in _series(bundle["balance"], "stockholders_equity") if v]
    info = bundle.get("info") or {}
    shares = info.get("sharesOutstanding")
    if len(equity_series) < 2 or not shares:
        return None, False
    per_share = [v / shares for _y, v in equity_series]
    newest, oldest = per_share[0], per_share[-1]
    n = len(equity_series) - 1
    if oldest <= 0 or n <= 0:
        return None, False
    try:
        raw = (newest / oldest) ** (1 / n) - 1
    except (ValueError, ZeroDivisionError):
        return None, False
    if fcf_valuation_engine._coeff_of_variation(per_share) > 0.60:
        return None, False  # too volatile to trust the CAGR at all
    ceiling = fcf_valuation_engine.growth_ceiling_for(info, info.get("currency"))
    capped = raw > ceiling
    g_eq = max(fcf_valuation_engine.GROWTH_FLOOR, min(raw, ceiling))
    return g_eq, capped


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
    either. g_eq itself IS gated - see _equity_growth_rate()."""
    if g_earn is None:
        return None
    equity = _latest(bundle["balance"], "stockholders_equity")
    net_income = _latest(bundle["income"], "net_income")
    shares = (bundle.get("info") or {}).get("sharesOutstanding")
    g_eq, g_eq_capped = _equity_growth_rate(bundle)
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
    return value_per_share, g_eq, discount, g_eq_capped


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
    trailing_eps, _ = _eps_ttm(bundle)
    price_now = info.get("currentPrice") or info.get("regularMarketPrice")
    if trailing_eps is None or trailing_eps <= 0 or not price_now:
        return None
    forecast_eps_5y = trailing_eps * ((1 + g_earn) ** 5)
    actual_pe = price_now / trailing_eps
    return forecast_eps_5y * actual_pe, forecast_eps_5y, actual_pe


def _build_fair_value(bundle, ticker, dcf_result):
    b = _basics(bundle)
    info, price = b["info"], b["price"]
    trailing_eps, _ = _eps_ttm(bundle)
    g_earn = dcf_result.get("growth")
    avg_pe = _avg_pe_3pt(bundle)

    pe_forward_result = _safe(_pe_forward_method, bundle, g_earn)
    pe_forward_value, forecast_eps_5y, actual_pe = pe_forward_result if pe_forward_result else (None, None, None)

    pe_trailing_value = (trailing_eps * avg_pe) if (trailing_eps is not None and avg_pe is not None) else None
    dcf_value = dcf_result.get("value")

    equity_10y_result = _safe(_equity_10y_method, bundle, g_earn)
    equity_10y_value, equity_growth, equity_discount, equity_growth_capped = (
        equity_10y_result if equity_10y_result else (None, None, None, False)
    )

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
            {
                "label": "Equity Growth", "value": equity_growth, "format": "pct",
                "flagged": equity_growth_capped,
            },
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


def build_sections(ticker, force_refresh=False, discount_rate=None,
                    perpetual_rate=None, growth_rate=None, manual_fcf=None):
    """The six computed Research sections for `ticker`, shaped exactly
    like compounder_data.json's own "sections" dict (see module docstring)
    - ready to pass straight into compounder_ui.render_section()/
    render_tabs(). Returns None if the underlying fundamentals bundle
    can't be fetched at all (e.g. an invalid ticker). A "_meta" key carries
    provenance (source, statement year depth, fetch time, engine version,
    flags) for the Deep Dive page's disclosure caption and for cache
    invalidation - render_tabs() simply never looks at that key, so its
    presence is harmless.

    discount_rate/perpetual_rate/growth_rate/manual_fcf: passed straight
    through to the DCF (see _run_dcf) - leave all None for the default
    pure-auto behaviour every caller used before this parameter existed.
    A caller that wants this section's Fair Value numbers to actually
    MATCH the main site's own Intrinsic Value for the same ticker should
    pass the same resolved values the main site is using (see app.py's
    _dcf_overrides_for()). Cached per (ticker, these override values) -
    see _overrides_signature() - so two different override combinations
    for the same ticker never collide in the cache."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None

    overrides = (discount_rate, perpetual_rate, growth_rate, manual_fcf)

    if not force_refresh:
        cached = _read_cache(ticker, overrides)
        if cached is not None:
            # Audit fix (2.1): a cache hit here used to return immediately,
            # WITHOUT ever calling fundamentals_data.get_bundle() - which is
            # the only place a fresh price gets overlaid on every call (see
            # its own docstring, written specifically so a large intraday
            # move like OCL.AX's -17% doesn't sit stale for up to 24h). That
            # meant the mechanism this cache was explicitly designed to
            # cooperate with never actually fired on a warm cache - exactly
            # the scenario it exists for. Rather than paying full
            # recomputation cost on every hit (which would defeat the point
            # of caching at all), do one cheap live-price check and only
            # fall through to a real rebuild when the price has genuinely
            # moved - same "OCL.AX" trigger condition, reachable now from a
            # warm cache instead of only a cold one.
            price_then = (cached.get("_meta") or {}).get("price_at_build")
            if price_then:
                price_now = fundamentals_data.get_live_price(ticker)
                if price_now and abs(price_now - price_then) / price_then <= 0.03:
                    return cached
                # else: no live price available (fail open - serve the
                # cache rather than block on a flaky quote), or it moved
                # >3% - fall through to a real rebuild either way below.
            else:
                return cached  # pre-fix cache entry with no baseline price recorded - nothing to compare against, serve as-is

    bundle = fundamentals_data.get_bundle(ticker, force_refresh=force_refresh)
    if not bundle:
        return None

    ref = _reference_lookup()
    dcf_result = _safe(
        _run_dcf, bundle, ticker,
        discount_rate=discount_rate, perpetual_rate=perpetual_rate,
        growth_rate=growth_rate, manual_fcf=manual_fcf,
    ) or {
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
        # Audit fixes (2.1/2.2): bundle_version ties this section cache's
        # validity to fundamentals_data.BUNDLE_VERSION (see _read_cache) so
        # a bundle-format fix there can't silently keep serving sections
        # built from the old bundle; price_at_build is the price this
        # section's numbers were actually computed against, checked on a
        # cache hit against a fresh live quote (see build_sections()'s
        # cache-hit branch above) so a big intraday move still gets picked
        # up on a warm cache, not just a cold one.
        "bundle_version": fundamentals_data.BUNDLE_VERSION,
        "price_at_build": _basics(bundle).get("price"),
    }

    _write_cache(ticker, sections, overrides)
    return sections
