"""
portfolio_health_engine.py

Health Score + Progress Score for "My Portfolio" holdings - the scoring
model from the desktop "Portfolio Health Monitor" app (its
health_score_engine.py + progress_engine.py + fundamentals_engine.py +
charts_engine.py), ported and adapted to this site's own data plumbing:

  - current price / quality score / intrinsic value / margin-of-safety come
    from nightly_scan.analyze_ticker_lite() - the SAME resolver every other
    page on this site already uses. This module never imports or touches
    resolver_engine.py / capm_engine.py / fcf_valuation_engine.py /
    auto_quality_engine.py / auto_intrinsic_value_engine.py, which all exist
    on this site already under those exact filenames with different (but
    related) content from the desktop app's own copies of them.
  - the extra fundamentals the desktop app tracked (revenue/earnings
    growth, profit margin, ROE, debt/equity, free cash flow, dividend
    rate) are fetched directly from yfinance's own `.info`/`.cashflow`
    here, cached with st.cache_data the same way the rest of the site
    caches live lookups.

HEALTH SCORE = how healthy the business/fund looks right now, blending
absolute fundamental levels (Growth/Margins/ROIC/Debt/Valuation - looked up
in fixed score bands, exactly like the desktop app's config.py tables) with
two purchase-relative reads (Price Action = drawdown from the post-purchase
peak + position in the 52-week range + trend vs the 200-day average; Income
= dividend yield change since the locked baseline).

PROGRESS SCORE = how far each tracked metric has moved since the LOCKED
baseline captured the day a holding was added (see portfolio_store.py) -
same formula as the desktop app: unchanged = 50, doubled = 100, halved = 0,
linear and clamped in between.

NEWS INTELLIGENCE: ported separately, in portfolio_news_engine.py
(analyze_holding_news()). When a `news` dict from that module is passed
into compute_health_components()/compute_health() below, the Thesis
component blends in the News Risk Score and a thesis-breaking event caps
Thesis at 30, exactly like the desktop app; the overall score also takes
the same News-only adjustment. Called without `news`, both functions
still work but fall back to a fundamentals-only Thesis score.

A NOTE ON FIDELITY: health_score_engine.py and progress_engine.py's exact
aggregation formulas were fully read and are reproduced faithfully below
(weights, the 50±50 relative-change formula, verdict/action thresholds).
fundamentals_engine.py's exact per-component code was not - it's
reconstructed here from the desktop app's own documented score bands
(config.py / SCORING_SYSTEM.md) and component descriptions, which is a
faithful-by-formula but not byte-for-byte port. Every reconstruction choice
made where the source line wasn't available is called out in a comment at
that spot.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import streamlit as st
import yfinance as yf

import nightly_scan
import portfolio_news_engine as pne

# ---------------------------------------------------------------------
# Scoring tables - transcribed from the desktop Portfolio Health Monitor's
# config.py (2026-08-27). This site has no config.py of its own, so these
# names are free to use directly.
# ---------------------------------------------------------------------

STATUS_GOOD = 70
STATUS_WARN = 50

ACTION_REVIEW = 40
ACTION_WATCH = 55
ACTION_STRONG = 73

COMPONENT_ORDER = [
    "Growth", "Margins", "ROIC", "FCF", "Debt", "Valuation",
    "Price Action", "Income", "Thesis",
]
BASE_WEIGHTS = {
    "Growth": 0.13, "Margins": 0.11, "ROIC": 0.12, "FCF": 0.12, "Debt": 0.08,
    "Valuation": 0.15, "Price Action": 0.12, "Income": 0.07, "Thesis": 0.10,
}
_STOCK_ONLY = ("Growth", "Margins", "ROIC", "FCF", "Debt")
_FUND_KEYS = ("Growth", "Margins", "ROIC", "FCF", "Debt", "Valuation",
              "Price Action", "Income")

GROWTH_BAND = [(-0.20, 10), (0.0, 40), (0.05, 60), (0.10, 72), (0.20, 85), (0.35, 95)]
MARGIN_BAND = [(-0.10, 5), (0.0, 35), (0.05, 55), (0.10, 68), (0.20, 82), (0.35, 95)]
ROE_BAND = [(0.0, 20), (0.08, 50), (0.12, 65), (0.18, 80), (0.25, 92)]
DEBT_BAND = [(0, 95), (30, 85), (60, 72), (100, 55), (150, 40), (250, 20), (400, 8)]
VAL_BAND = [(-40, 10), (-20, 30), (0, 50), (15, 68), (30, 82), (50, 92)]
RET_BAND = [(-40, 15), (-20, 35), (0, 55), (10, 70), (25, 82), (50, 92)]
DRAWDOWN_BAND = [(-60, 8), (-40, 22), (-25, 40), (-15, 58), (-8, 74), (0, 90)]
RANGE52_BAND = [(0.0, 30), (0.25, 48), (0.5, 62), (0.7, 75), (0.9, 88)]
INCOME_BAND = [(-0.6, 8), (-0.3, 30), (-0.1, 52), (0.0, 68), (0.1, 82), (0.3, 92)]

PROGRESS_ORDER = [
    "Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ",
    "Intrinsic Δ", "Return", "Income Δ",
]
PROGRESS_WEIGHTS = {
    "Growth Δ": 0.14, "Margins Δ": 0.12, "ROIC Δ": 0.13,
    "FCF Δ": 0.12, "Debt Δ": 0.09, "Intrinsic Δ": 0.12,
    "Return": 0.22, "Income Δ": 0.06,
}
PROGRESS_VERDICT_UP = 55
PROGRESS_VERDICT_DOWN = 45


def _interp(band, value):
    """Piecewise-linear lookup, clamped to the band's own end values -
    identical behaviour to the desktop app's band tables."""
    if value is None:
        return None
    if value <= band[0][0]:
        return float(band[0][1])
    if value >= band[-1][0]:
        return float(band[-1][1])
    for (x0, y0), (x1, y1) in zip(band, band[1:]):
        if x0 <= value <= x1:
            return float(y0) if x1 == x0 else float(y0 + (y1 - y0) * (value - x0) / (x1 - x0))
    return float(band[-1][1])


def _mean(*vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def score_color(score):
    if score is None:
        return "#9aa0a6"
    if score >= STATUS_GOOD:
        return "#0ca30c"
    if score >= STATUS_WARN:
        return "#e0912f"
    return "#d03b3b"


# ---------------------------------------------------------------------
# Live data fetch
# ---------------------------------------------------------------------

def _normalize_dividend_yield(v):
    """See the call site's comment - Yahoo has returned dividendYield as
    both a fraction and an already-percent number depending on version/
    endpoint. No real security yields >100%, so >1 unambiguously means
    the percent form; divide it back down to a fraction."""
    if v is None:
        return None
    return v / 100.0 if v > 1 else v


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_snapshot(ticker):
    """Live fundamentals + price snapshot for one ticker: whatever
    analyze_ticker_lite() doesn't already cover (growth/margin/ROE/
    debt/dividend/FCF), fetched straight from yfinance and cached for 30
    minutes - the same caching window the desktop app used."""
    try:
        lite = nightly_scan.analyze_ticker_lite(ticker)
    except Exception:
        lite = None

    info = {}
    hist = None
    fcf_growth = None
    tk = None
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
    except Exception:
        pass
    try:
        hist = tk.history(period="2y")
    except Exception:
        hist = None
    try:
        cf = tk.cashflow
        if cf is not None and not cf.empty:
            def _fcf_for_col(col):
                ocf = cf.loc["Operating Cash Flow", col] if "Operating Cash Flow" in cf.index else None
                capex = cf.loc["Capital Expenditure", col] if "Capital Expenditure" in cf.index else None
                if ocf is None:
                    return None
                return float(ocf) + float(capex or 0)  # capex is already negative in yfinance
            cols = list(cf.columns)
            if len(cols) >= 2:
                latest, prior = _fcf_for_col(cols[0]), _fcf_for_col(cols[1])
                if latest is not None and prior not in (None, 0):
                    fcf_growth = (latest - prior) / abs(prior)
    except Exception:
        fcf_growth = None

    def _num(key):
        v = info.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    # Price fallback chain (1a fix): analyze_ticker_lite() and yfinance's
    # own `.info` frequently come back empty for ETFs (no quality/IV model,
    # and `.info` often lacks currentPrice/regularMarketPrice for them) even
    # though `.history()` almost never misses for anything actually listed -
    # so fall all the way down to the history close before giving up.
    _lite_price = (lite or {}).get("Price")
    price = _lite_price if isinstance(_lite_price, (int, float)) and _lite_price == _lite_price else None  # guard NaN
    if price is None:
        price = _num("currentPrice") or _num("regularMarketPrice") or _num("previousClose")
    if price is None and tk is not None:
        try:
            fi = tk.fast_info
            for key in ("last_price", "lastPrice", "regularMarketPreviousClose"):
                try:
                    v = fi[key] if hasattr(fi, "__getitem__") else getattr(fi, key, None)
                except Exception:
                    v = None
                if v:
                    price = float(v)
                    break
        except Exception:
            pass
    if price is None and hist is not None and not hist.empty:
        try:
            price = float(hist["Close"].iloc[-1])
        except Exception:
            pass
    # A 2y history request can come back empty for some ETFs/structured
    # products even when the ticker is live - one retry with a much
    # shorter window before giving up entirely. Kept to a single extra
    # network round-trip (not several periods in sequence) since this
    # runs on every cache-cold page load and a chain of retries per
    # holding is what made the page slow to load in the first place.
    if price is None and tk is not None and (hist is None or hist.empty):
        try:
            _short_hist = tk.history(period="5d")
        except Exception:
            _short_hist = None
        if _short_hist is not None and not _short_hist.empty:
            try:
                price = float(_short_hist["Close"].iloc[-1])
                if hist is None or hist.empty:
                    hist = _short_hist  # so 52wk/MA200 fallbacks below have something too
            except Exception:
                pass
    if price is None:
        # Every fallback exhausted - log it so the next occurrence shows up
        # in Railway's deploy logs with an actual reason instead of just a
        # silent A$nan downstream (this was previously swallowed entirely).
        try:
            print(f"[portfolio] fetch_snapshot({ticker!r}): no price from lite/info/fast_info/history. "
                  f"info keys: {sorted(info.keys())[:15] if info else '(empty)'}; "
                  f"hist empty: {hist is None or hist.empty}")
        except Exception:
            pass

    high_52wk, low_52wk = _num("fiftyTwoWeekHigh"), _num("fiftyTwoWeekLow")
    ma200 = _num("twoHundredDayAverage")
    # Same story as price: ETFs' `.info` often skips these too, but both are
    # derivable straight from the 2y history already fetched above - needed
    # for the ETF price-based health mode (1c) to have anything to score.
    if (high_52wk is None or low_52wk is None) and hist is not None and not hist.empty:
        try:
            window = hist.tail(252)
            if high_52wk is None:
                high_52wk = float(window["Close"].max())
            if low_52wk is None:
                low_52wk = float(window["Close"].min())
        except Exception:
            pass
    if ma200 is None and hist is not None and not hist.empty:
        try:
            ma200 = float(hist["Close"].tail(200).mean())
        except Exception:
            ma200 = None

    range52 = None
    if price is not None and high_52wk and low_52wk and high_52wk > low_52wk:
        range52 = (price - low_52wk) / (high_52wk - low_52wk)
    trend_vs_ma200 = (price - ma200) / ma200 if (price is not None and ma200) else None

    return {
        "ticker": ticker,
        "price": price,
        "name": info.get("longName") or info.get("shortName"),
        "quality_score": (lite or {}).get("Quality"),
        "intrinsic_value": (lite or {}).get("Intrinsic Value"),
        "mos_pct": (lite or {}).get("MOS %"),
        "valuation_label": (lite or {}).get("Valuation"),
        "long_score": (lite or {}).get("Long Score"),
        "revenue_growth": _num("revenueGrowth"),
        "earnings_growth": _num("earningsGrowth"),
        "profit_margin": _num("profitMargins"),
        "roe": _num("returnOnEquity"),
        "debt_to_equity": _num("debtToEquity"),
        "fcf_growth": fcf_growth,
        "dividend_rate": _num("dividendRate"),
        # yfinance/Yahoo has shipped both a fraction (0.0235 = 2.35%) and a
        # already-percent number (2.35 = 2.35%) under this same key across
        # versions - a real yield over 100% doesn't exist, so treat >1 as
        # the percent form and normalize down. Bug this fixed: CSL showed
        # "235.00%" div yield and a $54k "potential dividend income" on a
        # $23k holding because the raw 2.35 was used as a fraction (K=2.35
        # instead of 0.0235) in both the display and the L = K x T formula.
        "dividend_yield": _normalize_dividend_yield(_num("dividendYield")),
        "quote_type": info.get("quoteType"),
        "sector": info.get("sector"),
        "currency": info.get("currency"),
        "high_52wk": high_52wk,
        "low_52wk": low_52wk,
        "range52": range52,
        "trend_vs_ma200": trend_vs_ma200,
        "history": hist,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


@st.cache_data(ttl=3600, show_spinner=False)
def fx_to_aud(currency):
    """Best-effort live FX rate to AUD, for combining holdings priced in
    different currencies into one total without silently pretending
    1 USD = 1 AUD. Returns None (never 1.0) when the lookup fails, so
    callers can flag/exclude that holding instead of quietly mis-summing
    it - same non-guessing spirit as the rest of this module."""
    currency = (currency or "AUD").upper()
    if currency == "AUD":
        return 1.0
    try:
        h = yf.Ticker(f"{currency}AUD=X").history(period="5d")
        if h is not None and not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def to_aud(amount, currency, missing=None):
    """Convert `amount` (in `currency`) to AUD via fx_to_aud() - shared by
    the Holdings tab's pie charts and the Portfolio Overview tab's Weight %
    column so both handle a missing FX rate the same way. Returns None
    (never a guessed 1:1 rate) when the rate can't be found; if a `missing`
    list is passed, the currency is appended to it so callers can flag/
    exclude that holding instead of silently mis-summing it."""
    if amount is None:
        return None
    rate = fx_to_aud(currency)
    if rate is None:
        if missing is not None and currency not in missing:
            missing.append(currency)
        return None
    return amount * rate


# ---------------------------------------------------------------------
# Health run history - powers "Δ run" on the Portfolio Overview tab. The
# desktop app's runs were manually triggered by the user opening the
# script; this website reruns on every click, so writes are throttled to
# min_gap_hours apart while every render still returns the last WRITTEN
# score for comparison.
# ---------------------------------------------------------------------

def _health_runs_conn():
    conn = sqlite3.connect(pne.DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portfolio_health_runs (
            email TEXT NOT NULL,
            ticker TEXT NOT NULL,
            as_of TEXT NOT NULL,
            overall REAL,
            news_risk REAL
        )"""
    )
    # portfolio was added after this table first shipped, so CREATE TABLE
    # IF NOT EXISTS above is a no-op against an already-live table and
    # never adds it - do that explicitly. Existing rows (all
    # pre-multi-portfolio, so effectively one portfolio per user) default
    # to '' and simply won't match a real portfolio name any more, which
    # only means "Δ run" starts fresh for them rather than showing a
    # comparison against a run that predates portfolios existing at all.
    # No data loss - this table is a rolling convenience log, not a
    # record of anything that needs to survive forever.
    _cols = [r[1] for r in conn.execute("PRAGMA table_info(portfolio_health_runs)").fetchall()]
    if "portfolio" not in _cols:
        conn.execute("ALTER TABLE portfolio_health_runs ADD COLUMN portfolio TEXT NOT NULL DEFAULT ''")
    return conn


def record_health_run(email, portfolio, ticker, overall, news_risk=None, min_gap_hours=4):
    """Log this Health run and return the PREVIOUS run's overall score (or
    None if there isn't one yet) so callers can show 'Δ run'. Scoped by
    (email, portfolio, ticker) - the same ticker held in two different
    portfolios is scored independently (different shares/baseline/buy
    date), so its run history must stay independent too, or one
    portfolio's holding could show a 'Δ run' computed against the OTHER
    portfolio's last score for the same ticker. A new row is only written
    if the last one is older than min_gap_hours, so Streamlit's
    rerun-on-every-click model doesn't flood the table - but the previous
    score is always returned regardless of whether a write happened."""
    if not email or not portfolio or not ticker:
        return None
    now = datetime.now(timezone.utc)
    with _health_runs_conn() as conn:
        row = conn.execute(
            "SELECT as_of, overall FROM portfolio_health_runs "
            "WHERE email = ? AND portfolio = ? AND ticker = ? ORDER BY as_of DESC LIMIT 1",
            (email, portfolio, ticker),
        ).fetchone()
        prev_overall = row[1] if row else None
        should_insert = True
        if row and row[0]:
            try:
                last = datetime.fromisoformat(row[0])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last) < timedelta(hours=min_gap_hours):
                    should_insert = False
            except Exception:
                pass
        if should_insert and overall is not None:
            conn.execute(
                "INSERT INTO portfolio_health_runs (email, portfolio, ticker, as_of, overall, news_risk) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (email, portfolio, ticker, now.isoformat(), overall, news_risk),
            )
    return prev_overall


def baseline_snapshot_fields(snapshot):
    """The subset of fetch_snapshot()'s fields that make up a locked
    baseline - same field names the desktop-import seed already uses
    (portfolio_store._SEED_HOLDINGS), so a holding added on the website
    and a holding imported from the desktop app are scored identically by
    compute_progress() with no schema branching needed."""
    return {
        "schema": "website_v2",
        "price": snapshot.get("price"),
        "intrinsic_value": snapshot.get("intrinsic_value"),
        "quality_score": snapshot.get("quality_score"),
        "revenue_growth": snapshot.get("revenue_growth"),
        "earnings_growth": snapshot.get("earnings_growth"),
        "profit_margin": snapshot.get("profit_margin"),
        "roe": snapshot.get("roe"),
        "debt_to_equity": snapshot.get("debt_to_equity"),
        "fcf_growth": snapshot.get("fcf_growth"),
        "dividend_rate": snapshot.get("dividend_rate"),
    }


def _normalize_baseline(baseline):
    """Older 'website_v1' baselines (captured before this Health/Progress
    port existed) only carry analyze_ticker_lite()'s own field names -
    map them onto the common shape so old holdings degrade gracefully
    (missing components read as n/a) instead of erroring."""
    baseline = baseline or {}
    if baseline.get("schema") == "website_v1":
        return {
            "price": baseline.get("Price"),
            "intrinsic_value": baseline.get("Intrinsic Value"),
            "quality_score": baseline.get("Quality"),
            "revenue_growth": None, "earnings_growth": None,
            "profit_margin": None, "roe": None, "debt_to_equity": None,
            "fcf_growth": None, "dividend_rate": None,
        }
    return baseline


def _post_purchase_drawdown(hist, buy_date, price):
    if hist is None or hist.empty or not buy_date or price is None:
        return None
    try:
        import pandas as pd
        cutoff = pd.Timestamp(buy_date)
        idx = hist.index
        if getattr(idx, "tz", None) is not None and cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize(idx.tz)
        elif getattr(idx, "tz", None) is None and cutoff.tzinfo is not None:
            cutoff = cutoff.tz_localize(None)
        window = hist.loc[idx >= cutoff]
        if window.empty:
            window = hist
        peak = float(window["Close"].max())
        return (price - peak) / peak if peak > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------
# Health Score
# ---------------------------------------------------------------------

def compute_health_components(snapshot, kind, baseline=None, buy_date=None, news=None, iv_override=None):
    is_etf = (kind or "STOCK").upper() == "ETF"
    baseline = _normalize_baseline(baseline)
    comps = {}

    def _set(name, score, current=None, note=""):
        comps[name] = {"score": (round(score, 1) if score is not None else None),
                        "current": current, "note": note}

    if is_etf:
        for name in _STOCK_ONLY:
            _set(name, None, note="N/A for ETFs/funds")
    else:
        _set("Growth", _interp(GROWTH_BAND, _mean(snapshot.get("revenue_growth"), snapshot.get("earnings_growth"))),
             current=_mean(snapshot.get("revenue_growth"), snapshot.get("earnings_growth")))
        _set("Margins", _interp(MARGIN_BAND, snapshot.get("profit_margin")), current=snapshot.get("profit_margin"))
        _set("ROIC", _interp(ROE_BAND, snapshot.get("roe")), current=snapshot.get("roe"))
        # No standalone FCF score band exists in the source app's config -
        # reconstructed here by reusing the Growth band on FCF growth
        # (fcf_growth is tracked as a baseline field there too, so this is
        # band-compatible with how the app already treats growth rates).
        _set("FCF", _interp(GROWTH_BAND, snapshot.get("fcf_growth")), current=snapshot.get("fcf_growth"))
        _set("Debt", _interp(DEBT_BAND, snapshot.get("debt_to_equity")), current=snapshot.get("debt_to_equity"))

    if is_etf:
        # No DCF/intrinsic-value model exists for funds (analyze_ticker_lite
        # returns no MOS% for them) - excluded outright rather than scored
        # against a missing baseline of zero (1c).
        _set("Valuation", None, note="No DCF model for ETFs/funds")
    elif iv_override is not None and iv_override > 0 and snapshot.get("price") is not None:
        # Manual override (1d): the same MOS% formula nightly_scan uses
        # (mos = (intrinsic - price) / intrinsic * 100), just against the
        # user's own number instead of the model's.
        model_iv = snapshot.get("intrinsic_value")
        _mos_override = ((iv_override - snapshot["price"]) / iv_override) * 100
        _note = "Using your manual override IV (A${:,.2f})".format(iv_override)
        if model_iv:
            _note += " - model says A${:,.2f}".format(model_iv)
        _set("Valuation", _interp(VAL_BAND, _mos_override), current=_mos_override, note=_note)
    else:
        _set("Valuation", _interp(VAL_BAND, snapshot.get("mos_pct")), current=snapshot.get("mos_pct"))

    drawdown = _post_purchase_drawdown(snapshot.get("history"), buy_date, snapshot.get("price"))
    range52 = snapshot.get("range52")
    trend = snapshot.get("trend_vs_ma200")
    parts = [p for p in (
        _interp(DRAWDOWN_BAND, drawdown * 100 if drawdown is not None else None),
        _interp(RANGE52_BAND, range52),
        (50.0 + max(-50.0, min(50.0, trend * 200))) if trend is not None else None,
    ) if p is not None]
    _set("Price Action", (sum(parts) / len(parts)) if parts else None, current=drawdown)

    div_yield = snapshot.get("dividend_yield")
    base_div_rate, cur_div_rate = baseline.get("dividend_rate"), snapshot.get("dividend_rate")
    if base_div_rate not in (None, 0) and cur_div_rate is not None:
        yield_change = (cur_div_rate - base_div_rate) / abs(base_div_rate)
        _set("Income", _interp(INCOME_BAND, yield_change), current=div_yield)
    elif div_yield is not None:
        # No baseline dividend rate to compare against yet (e.g. a legacy
        # baseline) - fall back to a flat read of the current yield itself.
        _set("Income", _interp(INCOME_BAND, div_yield - 0.0), current=div_yield,
             note="No baseline dividend rate on file - showing current yield only")
    else:
        _set("Income", None, note="No dividend data")

    if is_etf:
        # A fund doesn't have a "thesis" the way a company does (no
        # management, no earnings call, nothing for News Intelligence to
        # check against) - excluded from the blend rather than defaulted
        # from whatever fundamentals happen to still be present (1c).
        _set("Thesis", None, note="Not applicable for ETFs/funds - no company thesis to score")
        return comps

    fund_scores = [comps[k]["score"] for k in _FUND_KEYS if comps.get(k, {}).get("score") is not None]
    fund_avg = round(sum(fund_scores) / len(fund_scores), 1) if fund_scores else None

    # Thesis blends fundamentals with the News Risk Score, exactly like the
    # desktop app's compute_thesis_component(): 65% fundamentals / 35% news,
    # and any thesis-breaking event caps the whole component at 30 no matter
    # how healthy the fundamentals read.
    if news is not None:
        news_risk = news.get("news_risk_score")
        thesis_breaking = bool((news.get("counts") or {}).get("thesis-threatening"))
        if fund_avg is not None and news_risk is not None:
            thesis_score = round(0.65 * fund_avg + 0.35 * news_risk, 1)
        elif news_risk is not None:
            thesis_score = news_risk
        else:
            thesis_score = fund_avg
        note = "Blends fundamentals with the News Risk Score"
        if thesis_breaking and thesis_score is not None:
            thesis_score = min(thesis_score, 30.0)
            note = "Capped at 30 - thesis-breaking news detected"
        _set("Thesis", thesis_score, note=note)
    else:
        _set("Thesis", fund_avg, note="Fundamentals-only - no news data available for this holding")

    return comps


def _recommend(overall, valuation_score):
    if overall is None:
        return "Not enough data yet", "muted"
    if overall < ACTION_REVIEW:
        return "REVIEW / REDUCE", "critical"
    if overall < ACTION_WATCH:
        return "HOLD & WATCH", "warning"
    if overall >= ACTION_STRONG:
        if valuation_score is not None and valuation_score >= 68:
            return "HOLD / ADD - attractively priced", "good"
        if valuation_score is not None and valuation_score < 45:
            return "HOLD - richly priced", "good"
        return "HOLD", "good"
    return "HOLD", "good"


def _red_flags(components):
    flags = []
    for name in COMPONENT_ORDER:
        c = components.get(name) or {}
        s = c.get("score")
        if s is not None and s < 45 and name != "Thesis":
            flags.append(f"{name} is weak ({s:.0f}/100)")
    return flags


def compute_health(components, news=None, is_etf=False, progress_overall=None):
    """
    is_etf=True switches to the ETF/fund price-based blend (1c): the
    overall score is the average of Price Action (trend vs MA200, position
    in the 52-week range, post-purchase drawdown - already blended into
    that one component) and Progress vs baseline, with fundamentals,
    Valuation/MOS, Income and News excluded outright rather than defaulted
    to a neutral read - there's nothing to default them FROM for a fund.
    """
    if is_etf:
        parts = [p for p in (
            (components.get("Price Action") or {}).get("score"),
            progress_overall,
        ) if p is not None]
        overall = round(sum(parts) / len(parts), 1) if parts else None
        action, action_tone = _recommend(overall, None)
        return {
            "components": components,
            "overall": overall,
            "action": action,
            "action_tone": action_tone,
            "red_flags": _red_flags(components),
            "thesis_breaking": False,
            "thesis_intact": True,
            "news_adjustment": None,
            "news": None,
            "is_etf": True,
            "score_label": "Price-based health (ETF)",
        }

    weighted = [(BASE_WEIGHTS[k], components[k]["score"]) for k in COMPONENT_ORDER
                if components.get(k, {}).get("score") is not None]
    overall = round(sum(w * s for w, s in weighted) / sum(w for w, _ in weighted), 1) if weighted else None

    thesis_breaking = False
    news_adjustment = None
    if news is not None:
        thesis_breaking = bool((news.get("counts") or {}).get("thesis-threatening"))
        # Same News-only adjustment as the desktop app: only material news
        # nudges the overall score, and only by how far below 100 the News
        # Risk Score has fallen, scaled by NEWS_IMPACT.
        news_risk = news.get("news_risk_score")
        if news.get("material") and news_risk is not None and overall is not None:
            news_adjustment = -(100.0 - news_risk) * pne.NEWS_IMPACT
            overall = round(max(0.0, min(100.0, overall + news_adjustment)), 1)

    action, action_tone = _recommend(overall, (components.get("Valuation") or {}).get("score"))
    red_flags = _red_flags(components)
    if thesis_breaking:
        red_flags = ["Thesis-breaking news detected - immediate review warranted"] + red_flags

    return {
        "components": components,
        "overall": overall,
        "action": action,
        "action_tone": action_tone,
        "red_flags": red_flags,
        "thesis_breaking": thesis_breaking,
        "thesis_intact": not thesis_breaking,
        "news_adjustment": news_adjustment,
        "news": news,
        "is_etf": False,
        "score_label": "Investment Health Score",
    }


# ---------------------------------------------------------------------
# Progress Score
# ---------------------------------------------------------------------

def _rel(current, baseline, higher_better=True):
    if current is None or baseline is None:
        return None
    if abs(baseline) < 1e-9:
        rel = 1.0 if current > baseline else (-1.0 if current < baseline else 0.0)
    else:
        rel = (current - baseline) / abs(baseline)
    return rel if higher_better else -rel


def _prog_score(current, baseline, higher_better=True):
    rel = _rel(current, baseline, higher_better)
    if rel is None:
        return None
    rel = max(-1.0, min(1.0, rel))
    return round(max(0.0, min(100.0, 50 + rel * 50)), 1)


def compute_progress(snapshot, baseline, kind, buy_price, buy_date=None):
    baseline = _normalize_baseline(baseline)
    is_etf = (kind or "STOCK").upper() == "ETF"
    comps = {}

    def _mk(score, current, base, note=""):
        return {"score": score, "current": current, "baseline": base, "note": note}

    if is_etf:
        for k in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ", "Intrinsic Δ"):
            comps[k] = _mk(None, None, None, note="N/A for ETFs/funds")
    else:
        growth_vals = [v for v in (
            _prog_score(snapshot.get("revenue_growth"), baseline.get("revenue_growth")),
            _prog_score(snapshot.get("earnings_growth"), baseline.get("earnings_growth")),
        ) if v is not None]
        comps["Growth Δ"] = _mk(sum(growth_vals) / len(growth_vals) if growth_vals else None,
                                      snapshot.get("revenue_growth"), baseline.get("revenue_growth"))
        comps["Margins Δ"] = _mk(_prog_score(snapshot.get("profit_margin"), baseline.get("profit_margin")),
                                       snapshot.get("profit_margin"), baseline.get("profit_margin"))
        comps["ROIC Δ"] = _mk(_prog_score(snapshot.get("roe"), baseline.get("roe")),
                                    snapshot.get("roe"), baseline.get("roe"))
        comps["FCF Δ"] = _mk(_prog_score(snapshot.get("fcf_growth"), baseline.get("fcf_growth")),
                                   snapshot.get("fcf_growth"), baseline.get("fcf_growth"))
        comps["Debt Δ"] = _mk(_prog_score(snapshot.get("debt_to_equity"), baseline.get("debt_to_equity"),
                                                higher_better=False),
                                    snapshot.get("debt_to_equity"), baseline.get("debt_to_equity"))
        comps["Intrinsic Δ"] = _mk(_prog_score(snapshot.get("intrinsic_value"), baseline.get("intrinsic_value")),
                                         snapshot.get("intrinsic_value"), baseline.get("intrinsic_value"))

    buy_ref = buy_price or baseline.get("price")
    price = snapshot.get("price")
    comps["Return"] = _mk(_prog_score(price, buy_ref) if (price is not None and buy_ref) else None, price, buy_ref)

    comps["Income Δ"] = _mk(_prog_score(snapshot.get("dividend_rate"), baseline.get("dividend_rate")),
                                  snapshot.get("dividend_rate"), baseline.get("dividend_rate"))

    weighted = [(PROGRESS_WEIGHTS[k], comps[k]["score"]) for k in PROGRESS_ORDER
                if comps.get(k, {}).get("score") is not None]
    overall = round(sum(w * s for w, s in weighted) / sum(w for w, _ in weighted), 1) if weighted else None

    if overall is None:
        verdict = "Not enough data yet"
    elif overall >= PROGRESS_VERDICT_UP:
        verdict = "Improved since purchase"
    elif overall < PROGRESS_VERDICT_DOWN:
        verdict = "Deteriorated since purchase"
    else:
        verdict = "Roughly flat since purchase"

    return {"components": comps, "overall": overall, "verdict": verdict}


# ---------------------------------------------------------------------
# Shared rendering helpers - plain HTML/CSS, not matplotlib-to-PNG (the
# desktop app renders its component bars to a temp PNG via matplotlib and
# shows that with st.image; on a web app it's simpler and faster to draw
# the same numbered, colour-banded bar list directly as HTML).
# ---------------------------------------------------------------------

def big_score_html(label, score, suffix="/100"):
    color = score_color(score)
    value = f"{score:.0f}" if score is not None else "–"
    return (
        "<div style='margin:4px 0 10px;'>"
        f"<div style='color:#8aa0b8;font-size:13px;letter-spacing:.04em;"
        f"text-transform:uppercase;'>{label}</div>"
        f"<div style='font-size:3.4rem;font-weight:800;line-height:1.05;color:{color};'>"
        f"{value}<span style='font-size:1.3rem;color:#8aa0b8;font-weight:600;'>{suffix if score is not None else ''}</span>"
        "</div></div>"
    )


_ACTION_TONE_COLOR = {"good": "#0ca30c", "warning": "#e0912f", "critical": "#d03b3b", "muted": "#9aa0a6"}


def action_badge_html(action, tone):
    color = _ACTION_TONE_COLOR.get(tone, "#9aa0a6")
    return (
        f"<span style='display:inline-block;padding:4px 12px;border-radius:999px;"
        f"background:{color}22;color:{color};font-weight:700;font-size:13px;"
        f"border:1px solid {color}55;'>{action}</span>"
    )


def component_bars_html(components, order):
    rows = []
    for i, name in enumerate(order, start=1):
        c = components.get(name) or {}
        score = c.get("score")
        color = score_color(score)
        pct = max(0.0, min(100.0, score)) if score is not None else 0.0
        label = f"{score:.0f}%" if score is not None else "n/a"
        rows.append(
            "<div style='display:flex;align-items:center;gap:10px;margin:7px 0;'>"
            f"<div style='width:18px;color:#8aa0b8;font-size:12px;text-align:right;'>{i}</div>"
            f"<div style='width:110px;color:#c7d2e0;font-size:13px;'>{name}</div>"
            "<div style='flex:1;height:10px;border-radius:5px;background:#e7e6e122;position:relative;'>"
            f"<div style='width:{pct}%;height:100%;border-radius:5px;background:{color};'></div>"
            "</div>"
            f"<div style='width:48px;text-align:right;font-weight:700;font-size:13px;color:{color};'>{label}</div>"
            "</div>"
        )
    return "<div>" + "".join(rows) + "</div>"
