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

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

import streamlit as st
import yfinance as yf

import auto_compounder_engine
import financials_classifier
import fundamentals_data
import nightly_scan
import portfolio_news_engine as pne
import resolver_engine
import results_engine

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


def progress_color(score):
    """PART C STEP C1 (Director, 6 Oct 2026, instruction_portfolio_
    scoring_and_currency_table.md): Progress's OWN colour, driven by
    the SAME two constants as its own verdict (compute_progress()'s
    PROGRESS_VERDICT_DOWN/PROGRESS_VERDICT_UP) - never score_color()'s
    generic Health bands (STATUS_WARN=50/STATUS_GOOD=70), which could
    disagree with the verdict for any score between 45 and 50 (e.g.
    47: red under score_color() since 47 < STATUS_WARN, but "Roughly
    flat" under the verdict since 47 >= PROGRESS_VERDICT_DOWN - the
    live CSL.AX report, reproduced exactly). Health's own colouring
    (score_color(), used for Health/News Risk/Moat) is a separate
    function, completely untouched by this step."""
    if score is None:
        return "#9aa0a6"
    if score < PROGRESS_VERDICT_DOWN:
        return "#d03b3b"
    if score >= PROGRESS_VERDICT_UP:
        return "#0ca30c"
    return "#e0912f"


# ---------------------------------------------------------------------
# Live data fetch
# ---------------------------------------------------------------------

def _normalize_dividend_yield(v):
    """Audit A2 (27 Sep 2026): fallback-only now, for the case fetch_
    snapshot()'s own primary path (dividendRate / price, see its call
    site) can't use - no dividendRate on file. Kept because dividendYield
    itself is still ambiguous across yfinance versions/endpoints (a
    fraction on some, an already-percent number on others), and >1
    unambiguously means the percent form (no real security yields
    >100%) - but this heuristic alone silently mis-reads any LOW yield
    under the currently-pinned yfinance version's percent-number
    convention (see fetch_snapshot's own comment for the AAPL-shaped
    failure this caused), which is exactly why it's no longer the
    primary path."""
    if v is None:
        return None
    return v / 100.0 if v > 1 else v


def _dividend_yield_unit_uncertain(v):
    """Push 3 hardening (30 Sep 2026, owner-directed): the >1 heuristic
    above only ever resolves the HIGH-yield case unambiguously (see its
    own docstring's AAPL/CSL history) - a raw value in (0, 1] is
    genuinely indistinguishable here between "already a fraction"
    (0.0044 = 0.44%) and "a sub-1% percent number" (0.44 meaning 0.44%,
    which requirements.txt's currently-pinned yfinance version actually
    returns), and _normalize_dividend_yield() passes a value in that
    range through unchanged either way - silently picking whichever
    reading happens to be right, with no signal when it's wrong. True
    only for that same ambiguous (0, 1] band; callers use it to flag the
    reading (same red "estimated inputs" convention used elsewhere) only
    when the dividendRate primary path wasn't available to sidestep the
    ambiguity entirely - see the fetch_snapshot() call site."""
    return v is not None and 0 < v <= 1


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_snapshot(ticker, discount_rate=None, perpetual_rate=None, growth_rate=None, manual_fcf=None,
                    valuation_hash=auto_compounder_engine.VALUATION_SOURCE_HASH):
    """Live fundamentals + price snapshot for one ticker: whatever
    analyze_ticker_lite() doesn't already cover (growth/margin/ROE/
    debt/dividend/FCF), fetched straight from yfinance and cached for 30
    minutes - the same caching window the desktop app used.

    discount_rate/perpetual_rate/growth_rate/manual_fcf: optional DCF
    overrides, resolved by the caller from the viewer's own global/
    per-ticker settings (see app.py's _dcf_overrides_for()) and passed
    straight through to analyze_ticker_lite() -> resolve_intrinsic_value()
    so the Portfolio page's intrinsic value agrees with the Deep Dive
    page's for the same ticker instead of always being pure-auto.
    @st.cache_data keys on all arguments automatically, so passing
    different overrides for the same ticker naturally gets its own cache
    entry rather than colliding with the pure-auto one.

    `valuation_hash` (cache-safety fix, owner-reported, 28 Sep 2026): no
    caller passes this explicitly - its default is auto_compounder_
    engine.VALUATION_SOURCE_HASH evaluated ONCE, at this module's own
    import time (Python resolves a default argument value when the
    `def` statement runs, not on each call), so it's effectively "the
    valuation code's hash as of this process's boot". Deliberately not
    underscore-prefixed: st.cache_data excludes underscore-prefixed
    parameters from its own cache key (verified directly against
    _featured_analysis()'s matching fix in app.py), so this has to be a
    real, hashed parameter to do anything. Since this is an in-memory
    st.cache_data cache, a redeploy already clears it outright regardless
    of this parameter - this is defense-in-depth (and documents the
    intent explicitly) rather than a fix for an observed bug on this
    specific cache, same reasoning as _featured_analysis()'s own. Baking
    the hash into the DEFAULT rather than requiring every caller to pass
    it explicitly means fetch_snapshot()'s several existing call sites
    (app.py, portfolio_watchdog_engine.py) need no changes at all."""
    try:
        lite = nightly_scan.analyze_ticker_lite(
            ticker,
            discount_rate=discount_rate,
            perpetual_rate=perpetual_rate,
            growth_rate=growth_rate,
            manual_fcf=manual_fcf,
        )
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

    # Lists & display Commit 2 (3 Oct 2026, Director-directed): this
    # function's own price/52wk/MA200 fallback chain below (_num() over
    # `info`, `tk.fast_info`, raw `hist` closes) is a second, separate
    # raw fetch from `lite` (analyze_ticker_lite() above already goes
    # through normalize_pence_quote()) - a GBp-quoted London ticker
    # whose `lite` price came back None (the ETF-empty-`.info` case this
    # fallback chain exists for) would fall straight through to a raw
    # pence `info`/`hist` value, 100x too high. `info`/`hist` are
    # already in hand from the two fetches just above, so this costs no
    # extra network call.
    info, hist, _, _price_unit_meta = fundamentals_data.normalize_pence_quote(
        ticker, info, history_df=hist)

    try:
        cf = tk.cashflow
        if cf is not None and not cf.empty:
            def _fcf_for_col(col):
                ocf = cf.loc["Operating Cash Flow", col] if "Operating Cash Flow" in cf.index else None
                capex = cf.loc["Capital Expenditure", col] if "Capital Expenditure" in cf.index else None
                if ocf is None or (isinstance(ocf, float) and ocf != ocf):
                    return None
                # Audit fix B2.8 (27 Sep 2026, owner-directed, CONFIRMED
                # BUG): a MISSING Capital Expenditure row used to be
                # treated as a zero capex ("capex or 0"), silently making
                # FCF = OCF - overstating FCF for exactly the tickers
                # whose cashflow statement doesn't carry that row at all,
                # rather than leaving this figure unavailable like every
                # other missing-input case on this site.
                if capex is None or (isinstance(capex, float) and capex != capex):
                    return None
                return float(ocf) + float(capex)  # capex is already negative in yfinance
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

    # Part 44.2 (13 Sep 2026): the previous close, straight out of the SAME
    # 2y history already fetched above for the 52wk/MA200 fallbacks - zero
    # new network calls. Used by the My Portfolio pulse band's "Today"
    # tile. `info.get("previousClose")` is deliberately NOT used as a
    # fallback here: it can be a day stale relative to `hist`'s own last
    # row on some tickers, which would make "Today" disagree with the
    # price this same snapshot is using - one consistent source only.
    prev_close = None
    if hist is not None and not hist.empty and len(hist) >= 2:
        try:
            prev_close = float(hist["Close"].iloc[-2])
        except Exception:
            prev_close = None

    return {
        "ticker": ticker,
        "price": price,
        "prev_close": prev_close,
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
        # Data-correctness audit A2 (27 Sep 2026, owner-reported):
        # dividendYield's fraction-vs-percent ambiguity was never fully
        # resolved by the >1 heuristic below - requirements.txt pins
        # yfinance>=1.7.0, which returns dividendYield already as a
        # percent NUMBER (0.44 for AAPL's 0.44% yield, not 0.0044), so a
        # LOW yield under 1% (most large caps) never triggers the >1
        # branch and was left as-is - a raw 0.44 used directly as the
        # fraction K in K x holding_value (see this function's caller,
        # and app.py's own pot_div_income_aud) is a 44% yield, ~100x too
        # high. The >1 heuristic only ever correctly caught the HIGHER-
        # yield case (e.g. the original CSL 2.35 -> 235% bug it was
        # written for).
        # Fixed by computing yield directly from two unambiguous $
        # figures - dividendRate (Yahoo's own annual $/share dividend
        # rate, never a ratio, so no fraction-vs-percent question exists
        # for it) divided by price - the same "$ dividends / price"
        # shape nightly_scan.analyze_ticker_lite() already uses for its
        # own "Dividend Yield %" (TTM $ dividends / price), rather than
        # continuing to guess at dividendYield's format. Falls back to
        # the normalized dividendYield only when dividendRate is missing
        # (e.g. some ETFs) - kept for that case, not as the primary path
        # anymore.
        "dividend_yield": (
            (_num("dividendRate") / price) if (_num("dividendRate") and price)
            else _normalize_dividend_yield(_num("dividendYield"))
        ),
        # Push 3 hardening (30 Sep 2026, owner-directed): True only when
        # the dividendRate primary path above wasn't available (so this
        # fell back to the ambiguous dividendYield heuristic) AND the
        # raw value landed in that heuristic's genuinely-ambiguous (0, 1]
        # band - see _dividend_yield_unit_uncertain()'s own docstring.
        # Display-only (same red "estimated inputs" convention as the
        # rest of this module's flags) - never touches the yield number
        # itself or any downstream sum/score.
        "dividend_yield_unit_uncertain": (
            not (_num("dividendRate") and price)
            and _dividend_yield_unit_uncertain(_num("dividendYield"))
        ),
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
    # PART B STEP B3 (Director, 6 Oct 2026, instruction_portfolio_
    # scoring_and_currency_table.md): "the stored health history will
    # show a step on the day the switch is set - propose how to mark
    # it... build the smaller option." A single nullable column is
    # smaller than a separate method-change audit table: every row
    # written from here on records which method produced it ("v1"/
    # "v2"); every row written before this column existed simply has
    # method=NULL, read as "v1" by convention (every run before this
    # commit WAS v1 - there was no other method), never backfilled.
    # The trend view (wherever the Δ run history is charted) can use
    # this to mark the exact day the step happened, rather than
    # guessing from the score alone.
    if "method" not in _cols:
        conn.execute("ALTER TABLE portfolio_health_runs ADD COLUMN method TEXT")
    return conn


def record_health_run(email, portfolio, ticker, overall, news_risk=None, min_gap_hours=4,
                       method=None):
    """Log this Health run and return the PREVIOUS run's overall score (or
    None if there isn't one yet) so callers can show 'Δ run'. Scoped by
    (email, portfolio, ticker) - the same ticker held in two different
    portfolios is scored independently (different shares/baseline/buy
    date), so its run history must stay independent too, or one
    portfolio's holding could show a 'Δ run' computed against the OTHER
    portfolio's last score for the same ticker. A new row is only written
    if the last one is older than min_gap_hours, so Streamlit's
    rerun-on-every-click model doesn't flood the table - but the previous
    score is always returned regardless of whether a write happened.

    `method` (STEP B3): "v1"/"v2"/None - whichever method produced this
    `overall`/`news_risk`, written straight through with no validation
    (the caller already knows, from the same switch check that chose
    which compute_health()/compute_health_v2() call produced these
    numbers) - see _health_runs_conn()'s own docstring for why this
    column exists."""
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
                "INSERT INTO portfolio_health_runs (email, portfolio, ticker, as_of, overall, "
                "news_risk, method) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, portfolio, ticker, now.isoformat(), overall, news_risk, method),
            )
    return prev_overall


def get_recent_health_runs(email, portfolio, ticker, limit=8):
    """Last `limit` recorded Health runs for (email, portfolio, ticker),
    OLDEST first, as a plain list of scores (None entries dropped) - feeds
    the Overview tab's Health-trend sparkline column. Read-only: unlike
    record_health_run() this never writes a row, so viewing the Overview
    tab alone never fabricates run history that wasn't actually recorded
    on the Holdings/Health & News tabs' own renders."""
    if not email or not portfolio or not ticker:
        return []
    with _health_runs_conn() as conn:
        rows = conn.execute(
            "SELECT overall FROM portfolio_health_runs "
            "WHERE email = ? AND portfolio = ? AND ticker = ? "
            "ORDER BY as_of DESC LIMIT ?",
            (email, portfolio, ticker, limit),
        ).fetchall()
    return [r[0] for r in reversed(rows) if r[0] is not None]


def get_health_as_of(email, portfolio, ticker, days_ago):
    """The closest recorded Health run for (email, portfolio, ticker) at or
    before `days_ago` days back from now (UTC) - the 'N days ago' sibling
    of get_recent_health_runs() above, added for AI-readiness roadmap
    Phase 8 (the weekly brief's "portfolio health changes" section), which
    needs a specific point in time to compare against rather than "the
    last few times someone opened the Health tab": get_recent_health_runs'
    oldest-of-limit entry can be anywhere from hours to months back
    depending entirely on how often this holding's Health tab happens to
    get opened, so it is not a reliable "~7 days ago" reading on its own.
    Read-only, same as get_recent_health_runs - never writes a row.
    Returns the `overall` score (float) of the closest run at or before
    that cutoff, or None if this holding has no recorded run that far
    back yet (most holdings, most of the time - a Health run is only ever
    written when someone actually opens the Health tab)."""
    if not email or not portfolio or not ticker:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with _health_runs_conn() as conn:
        row = conn.execute(
            "SELECT overall FROM portfolio_health_runs "
            "WHERE email = ? AND portfolio = ? AND ticker = ? AND as_of <= ? "
            "ORDER BY as_of DESC LIMIT 1",
            (email, portfolio, ticker, cutoff),
        ).fetchone()
    return row[0] if row and row[0] is not None else None


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
        # TASK B (MOS% display rule): display-only, never feeds the
        # Valuation score/band above - tells display_mos_pct() to show
        # "Model unreliable" instead of this component's "current" MOS%.
        comps["Valuation"]["unreliable"] = resolver_engine.dcf_looks_unreliable(
            snapshot.get("intrinsic_value"), snapshot.get("price"), symmetric=True
        )

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
# PART B STEP B1 (Director, 6 Oct 2026, instruction_portfolio_scoring_
# and_currency_table.md): compute_health_components_v2()/compute_
# health_v2() - BESIDE compute_health_components()/compute_health()
# above, which this step does not edit by a single character. Takes
# `news` from portfolio_news_engine.analyze_holding_news_v2() (its
# "thesis_breaking_live" key is the decay-aware signal v1 never had -
# a thesis-breaking event that happened 11 months ago no longer counts
# as "live" once its own v2 weight has decayed to 0, see that
# function's own docstring). Fix #4 from this instruction's own STEP
# B0 diagnosis (news counted twice, no ceiling - the direct cause of
# CSL.AX's Health-0 report): Thesis v2 is fundamentals-only (no 65/35
# news blend at all - the ONLY thing that still touches it is the
# existing thesis-breaking cap, now gated on a LIVE event); the
# overall-score news cut is capped at NEWS_V2_CUT_CAP_DEFAULT points
# unless a live thesis-breaking event exists, in which case the cap
# widens to NEWS_V2_CUT_CAP_THESIS_BREAKING - so a pile of ordinary bad-
# quarter headlines can no longer walk the Health score to zero on its
# own, while a genuine thesis-breaking event still reaches the same
# ceiling v1 could.
# ---------------------------------------------------------------------

def compute_health_components_v2(snapshot, kind, baseline=None, buy_date=None, news=None,
                                  iv_override=None):
    """Identical to compute_health_components() for every component
    EXCEPT Thesis - see that function's own body for Growth/Margins/
    ROIC/FCF/Debt/Valuation/Price Action/Income, reproduced verbatim
    here (not factored into a shared helper, so v1's own function stays
    completely untouched and this one reads standalone, top to bottom,
    for Andrew's own side-by-side review)."""
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
        _set("FCF", _interp(GROWTH_BAND, snapshot.get("fcf_growth")), current=snapshot.get("fcf_growth"))
        _set("Debt", _interp(DEBT_BAND, snapshot.get("debt_to_equity")), current=snapshot.get("debt_to_equity"))

    if is_etf:
        _set("Valuation", None, note="No DCF model for ETFs/funds")
    elif iv_override is not None and iv_override > 0 and snapshot.get("price") is not None:
        model_iv = snapshot.get("intrinsic_value")
        _mos_override = ((iv_override - snapshot["price"]) / iv_override) * 100
        _note = "Using your manual override IV (A${:,.2f})".format(iv_override)
        if model_iv:
            _note += " - model says A${:,.2f}".format(model_iv)
        _set("Valuation", _interp(VAL_BAND, _mos_override), current=_mos_override, note=_note)
    else:
        _set("Valuation", _interp(VAL_BAND, snapshot.get("mos_pct")), current=snapshot.get("mos_pct"))
        comps["Valuation"]["unreliable"] = resolver_engine.dcf_looks_unreliable(
            snapshot.get("intrinsic_value"), snapshot.get("price"), symmetric=True
        )

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
        _set("Income", _interp(INCOME_BAND, div_yield - 0.0), current=div_yield,
             note="No baseline dividend rate on file - showing current yield only")
    else:
        _set("Income", None, note="No dividend data")

    if is_etf:
        _set("Thesis", None, note="Not applicable for ETFs/funds - no company thesis to score")
        return comps

    fund_scores = [comps[k]["score"] for k in _FUND_KEYS if comps.get(k, {}).get("score") is not None]
    fund_avg = round(sum(fund_scores) / len(fund_scores), 1) if fund_scores else None

    # Fix #4 (v2): Thesis is fundamentals ONLY - news no longer blends
    # into it at 35% (that blend is what let a pile of ordinary bad-
    # quarter news drag Thesis down on its own, on top of ALSO cutting
    # overall separately below). The thesis-breaking cap survives, but
    # now gated on news.get("thesis_breaking_live") - a decayed-away
    # thesis-breaking event (past NEWS_V2_DECAY_ZERO_DAYS["thesis-
    # breaking"] = 180 days old) no longer caps a healthy thesis score
    # forever.
    thesis_breaking_live = bool(news and news.get("thesis_breaking_live"))
    if is_etf:
        _set("Thesis", None, note="Not applicable for ETFs/funds - no company thesis to score")
    elif thesis_breaking_live and fund_avg is not None:
        _set("Thesis", min(fund_avg, 30.0), note="Capped at 30 - thesis-breaking news detected (v2 method)")
    else:
        _set("Thesis", fund_avg, note="Fundamentals only (v2 method - news no longer blends into Thesis)")

    return comps


def compute_health_v2(components, news=None, is_etf=False, progress_overall=None,
                       default_cut_limit=None):
    """v2 of compute_health() - identical ETF branch (reproduced
    verbatim; news plays no role in the ETF blend either way). The
    standard-company branch differs only in the news cut: capped at
    NEWS_V2_CUT_CAP_DEFAULT points unless a live thesis-breaking event
    exists (news.get("thesis_breaking_live")), in which case the cap
    widens to NEWS_V2_CUT_CAP_THESIS_BREAKING - and thesis_breaking/
    thesis_intact are driven by that same decay-aware flag, never v1's
    own "any thesis-breaking headline ever counted" check.

    default_cut_limit (PART 1 STEP 1.1, 8 Oct 2026, instruction_health_
    fixes_chart_and_new_markets.md): overrides pne.NEWS_V2_CUT_CAP_
    DEFAULT for THIS call only - lets the owner-only comparison panel
    show the v2 default (10, Andrew's own 8 Oct decision) beside the
    earlier proposal (15) side by side, from one function, rather than
    a second copy of it. None (every call outside that panel) reads
    pne.NEWS_V2_CUT_CAP_DEFAULT fresh, exactly as before this parameter
    existed. The thesis-breaking cap (pne.NEWS_V2_CUT_CAP_THESIS_
    BREAKING, 55) is NEVER overridden here, by design - Andrew's own
    words: "only a thesis-breaking story may push a holding into
    REVIEW / REDUCE on news alone, and that limit stays 55."

    New return keys (STEP 1.1), all for the panel's own "the health
    score before the cut" confusion the Director caught: "overall_
    before_news_cut" (the weighted nine-part score, Thesis included,
    BEFORE any news adjustment - never the same number as compute_
    health_components_v2()'s own fundamentals-only average, which
    excludes Thesis entirely); "news_cut_before_limit" (the uncapped
    point reduction news_risk_score alone implies, (100 - news_risk) *
    pne.NEWS_IMPACT - None when no cut was even evaluated, e.g. no
    news or not material); "news_cut_applied" (the actual point
    reduction subtracted, after the limit - 0.0 when news was
    evaluated but wasn't material, None when there was no news at
    all)."""
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
            "score_label": "Price-based health (ETF) (v2 method)",
            "overall_before_news_cut": overall,
            "news_cut_before_limit": None,
            "news_cut_applied": None,
        }

    weighted = [(BASE_WEIGHTS[k], components[k]["score"]) for k in COMPONENT_ORDER
                if components.get(k, {}).get("score") is not None]
    overall = round(sum(w * s for w, s in weighted) / sum(w for w, _ in weighted), 1) if weighted else None
    overall_before_news_cut = overall

    thesis_breaking = False
    news_adjustment = None
    news_cut_before_limit = None
    news_cut_applied = None
    if news is not None:
        thesis_breaking = bool(news.get("thesis_breaking_live"))
        news_risk = news.get("news_risk_score")
        news_cut_applied = 0.0
        if news.get("material") and news_risk is not None and overall is not None:
            cap = (pne.NEWS_V2_CUT_CAP_THESIS_BREAKING if thesis_breaking
                   else (default_cut_limit if default_cut_limit is not None
                         else pne.NEWS_V2_CUT_CAP_DEFAULT))
            uncapped_cut = (100.0 - news_risk) * pne.NEWS_IMPACT
            news_cut_before_limit = round(uncapped_cut, 1)
            news_cut_applied = round(min(uncapped_cut, cap), 1)
            news_adjustment = -news_cut_applied
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
        "score_label": "Investment Health Score (v2 method)",
        "overall_before_news_cut": overall_before_news_cut,
        "news_cut_before_limit": news_cut_before_limit,
        "news_cut_applied": news_cut_applied,
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
# PART C STEP C2 (Director, 6 Oct 2026, instruction_portfolio_scoring_
# and_currency_table.md): compute_progress_v2() - BESIDE compute_
# progress() above, which this step does not edit by a single
# character. STEP C0's own diagnosis: _prog_score() compares every
# component by RELATIVE change, even for rate fields (revenue_growth/
# earnings_growth/profit_margin/roe/fcf_growth, all stored as
# fractions - 0.10 = 10%, confirmed against GROWTH_BAND/MARGIN_BAND/
# ROE_BAND's own threshold scales in STEP C0's report) - so growth
# slowing from 10% to 6% reads as a 40% relative deterioration
# ((0.06-0.10)/0.10) and scores 30, when the real-world change is a
# 4-PERCENTAGE-POINT slowdown. Level fields (debt_to_equity, a
# percentage NUMBER not a fraction; intrinsic_value; price vs buy
# price; dividend_rate) were never the problem STEP C0 diagnosed, so
# v2 leaves them on the SAME relative-change formula, unchanged -
# comparing a level by percentage POINTS wouldn't even be the right
# unit for most of them (e.g. intrinsic_value is a dollar figure, not
# a rate).
# ---------------------------------------------------------------------

def is_progress_v2_live():
    """The switch - same unset/""/0/off=OFF, 1/on=ON pattern as every
    other switch in this codebase (currency_view_engine.
    is_currency_view_live(), portfolio_news_engine.is_health_news_v2_
    live(), ...). Re-read on every call. Claude Code never sets this -
    the Director does, on Railway, after Andrew has checked the PART B
    STEP B2 owner-only comparison panel's own second table (STEP C2)."""
    raw = (os.environ.get("PROGRESS_V2_LIVE") or "").strip().lower()
    return raw in ("1", "on")


# Director's own proposed scales, in percentage POINTS, for the full
# +-50 swing around the neutral 50 score - named constants so Andrew
# can see and adjust them from the STEP B2/C2 comparison panel before
# ever setting the switch.
PROGRESS_V2_GROWTH_SCALE_PP = 20.0
PROGRESS_V2_MARGIN_SCALE_PP = 10.0
PROGRESS_V2_ROIC_SCALE_PP = 10.0
PROGRESS_V2_FCF_SCALE_PP = 20.0


def _prog_score_v2_rate(current, baseline, scale_points, higher_better=True):
    """v2's own rate-field formula: score = 50 + clamp(difference in
    PERCENTAGE POINTS / scale_points, -1, 1) * 50. `current`/`baseline`
    are fractions (0.10 = 10%), so (current-baseline)*100 is the
    difference already expressed in percentage points - scale_points
    is how many points maps to the full +-50 swing (e.g. growth
    slowing by exactly 20pp scores 0; by exactly 10pp scores 25)."""
    if current is None or baseline is None:
        return None
    diff_pp = (current - baseline) * 100.0
    if not higher_better:
        diff_pp = -diff_pp
    frac = max(-1.0, min(1.0, diff_pp / scale_points))
    return round(max(0.0, min(100.0, 50 + frac * 50)), 1)


def compute_progress_v2(snapshot, baseline, kind, buy_price, buy_date=None):
    """v2 of compute_progress() - identical structure, identical
    component set/weights/verdict thresholds; only the four RATE
    components (Growth Δ/Margins Δ/ROIC Δ/FCF Δ) are scored in
    percentage points via _prog_score_v2_rate() instead of relative
    change. Debt Δ/Intrinsic Δ/Return/Income Δ (level fields) reuse
    _prog_score()/_rel() exactly, unchanged - see this section's own
    header comment for why."""
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
            _prog_score_v2_rate(snapshot.get("revenue_growth"), baseline.get("revenue_growth"),
                                 PROGRESS_V2_GROWTH_SCALE_PP),
            _prog_score_v2_rate(snapshot.get("earnings_growth"), baseline.get("earnings_growth"),
                                 PROGRESS_V2_GROWTH_SCALE_PP),
        ) if v is not None]
        comps["Growth Δ"] = _mk(sum(growth_vals) / len(growth_vals) if growth_vals else None,
                                 snapshot.get("revenue_growth"), baseline.get("revenue_growth"))
        comps["Margins Δ"] = _mk(
            _prog_score_v2_rate(snapshot.get("profit_margin"), baseline.get("profit_margin"),
                                 PROGRESS_V2_MARGIN_SCALE_PP),
            snapshot.get("profit_margin"), baseline.get("profit_margin"))
        comps["ROIC Δ"] = _mk(
            _prog_score_v2_rate(snapshot.get("roe"), baseline.get("roe"), PROGRESS_V2_ROIC_SCALE_PP),
            snapshot.get("roe"), baseline.get("roe"))
        comps["FCF Δ"] = _mk(
            _prog_score_v2_rate(snapshot.get("fcf_growth"), baseline.get("fcf_growth"),
                                 PROGRESS_V2_FCF_SCALE_PP),
            snapshot.get("fcf_growth"), baseline.get("fcf_growth"))
        # Level fields - UNCHANGED, same relative-change formula v1 uses.
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

def big_score_html(label, score, suffix="/100", color_fn=score_color):
    """color_fn (PART C STEP C1, Director, 6 Oct 2026): defaults to
    score_color() - every pre-existing caller (Health, News Risk)
    renders byte-identical to before this step. The Progress tab's own
    big score tile passes color_fn=progress_color so its colour is
    driven by the SAME bands as its own verdict, never the generic
    Health bands."""
    color = color_fn(score)
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
        # "/100" rather than "%" - each component is a 0-100 SCORE (the
        # band-lookup output), not a literal percentage of the underlying
        # metric (e.g. Debt's score of 92 comes from a debt/equity ratio
        # of 9 read against the Debt band - "92%" read as if it were the
        # debt ratio itself, which is exactly what it isn't).
        label = f"{score:.0f}/100" if score is not None else "n/a"
        rows.append(
            "<div style='display:flex;align-items:center;gap:10px;margin:7px 0;'>"
            f"<div style='width:18px;color:#8aa0b8;font-size:12px;text-align:right;'>{i}</div>"
            f"<div style='width:110px;color:#c7d2e0;font-size:13px;'>{name}</div>"
            "<div style='flex:1;height:10px;border-radius:5px;background:#e7e6e122;position:relative;'>"
            f"<div style='width:{pct}%;height:100%;border-radius:5px;background:{color};'></div>"
            "</div>"
            f"<div style='width:64px;text-align:right;font-weight:700;font-size:13px;color:{color};'>{label}</div>"
            "</div>"
        )
    return "<div>" + "".join(rows) + "</div>"


# ---------------------------------------------------------------------
# PART E: "Evolution since purchase" score (TASK 16, Director-directed,
# owner-only shadow panel, no live effect). A holding's action should
# measure whether the business's fix is WORKING since purchase, not
# today's absolute levels and not news the market had already priced in
# at purchase. This section is purely additive: it calls compute_progress()
# / compute_progress_v2() and _post_purchase_drawdown() exactly as they
# already exist, and adds one brand-new, narrower-than-portfolio_news_
# engine.py news tiering for events strictly after buy_date. Nothing here
# is read by compute_health*/compute_progress*/_recommend(), and nothing
# here is persisted to any store - it exists only to be displayed on the
# owner-only shadow panel and exported to CSV for manual review.
# ---------------------------------------------------------------------

# TASK 16b (Director-directed, 20 Oct 2026): Intrinsic Δ compares the IV
# stored at purchase against today's IV, but the valuation MODEL itself
# has changed since then (discount-rate design, growth rules, risk-free
# fixes) and purchase-day model inputs were never stored, so there is no
# way to recompute the baseline under today's model - the comparison is
# apples-to-oranges, not a real read of business change (OCL's CSV showed
# Intrinsic Δ 19.5, a ~-60% reading driven by model drift, not the
# business). Intrinsic Δ is now INFORMATION ONLY: still computed and
# shown (EVOLUTION_INTRINSIC_WEIGHT below), but weight 0 - excluded from
# EVOLUTION_WEIGHTS entirely, so it can never move the overall score. Its
# 20% is redistributed PROPORTIONALLY across the other four Fundamentals
# Δ components (12:10:10:8 -> 17:15:14:12, each rounded to the nearest
# whole point) plus Debt Δ (5 -> 7), giving Fundamentals Δ 65% total.
EVOLUTION_INTRINSIC_WEIGHT = 0.0
EVOLUTION_WEIGHTS = {
    "Growth Δ": 17.0, "Margins Δ": 15.0, "ROIC Δ": 14.0, "FCF Δ": 12.0, "Debt Δ": 7.0,
    "Price since purchase": 15.0, "Income Δ": 5.0,
    "News since purchase": 15.0,
}
# ETFs have no company fundamentals/thesis/news to score (and no
# Intrinsic Δ either way) - proposed split keeps the stock weights' own
# 15:5 (Price:Income) ratio, re-normalised to 100: not specified by the
# Director beyond "Price since purchase + Income Δ only", so this exact
# 75/25 split is this report's own proposal.
EVOLUTION_WEIGHTS_ETF = {"Price since purchase": 75.0, "Income Δ": 25.0}

# TASK 16b: Income Δ's current/baseline dividend_rate both come from the
# SAME field - fetch_snapshot()'s own "dividend_rate": _num("dividendRate")
# (see that function's own comment: Yahoo's indicated annual $/share
# dividend rate, never a trailing-12-month sum or a ratio). Shown next to
# the baseline/current figures so the owner can judge a drop against the
# field's real meaning (an indicated-rate cut reads very differently from
# a trailing-sum artifact) rather than a bare number.
EVOLUTION_INCOME_SOURCE_NOTE = "dividendRate (Yahoo Finance's indicated annual $/share dividend rate)"

EVOLUTION_ACTION_HOLD = 60
EVOLUTION_ACTION_WATCH = 45
EVOLUTION_VALUATION_MOS_ADD = 30.0

# News-since-purchase tiers for TASK 16 - deliberately NOT a reuse of
# THESIS_BREAKING/MATERIAL_RISK/POSITIVE from portfolio_news_engine.py:
# the Director's own scheme moves leadership changes/restructuring/
# demergers/CEO-CFO exits/class actions OUT of the cut-driving lists into
# a third, no-cut "Review" tier for owner judgement - a different
# grouping, so it needs its own lists. The underlying fetch/storage
# (pne._load_events_with_fetched_at) and whole-word matcher
# (pne._contains_any_v2) are reused unchanged - no new fetch, no new
# classifier infrastructure.
EVOLUTION_THESIS_BREAKING = [
    "fraud", "accounting scandal", "restates earnings", "earnings restatement",
    "material weakness", "sec investigation", "asic investigation",
    "regulator investigation", "regulatory investigation", "insolvency",
    "bankruptcy", "going concern", "chapter 11", "delisting", "delisted",
    "licence revoked", "license revoked", "covenant breach",
    "credit rating cut to junk", "downgraded to junk", "trial failed",
    "phase 3 failure", "fda rejection", "complete response letter",
]
EVOLUTION_MATERIAL_NEGATIVE = [
    "cuts guidance", "lowers guidance", "guidance cut", "slashes guidance",
    "profit warning", "misses estimates", "misses expectations",
    "downgrade", "downgraded", "impairment", "writedown", "write-down",
]
EVOLUTION_REVIEW_ONLY = [
    "ceo resigns", "cfo resigns", "ceo steps down", "cfo steps down",
    "new ceo", "leadership change", "restructuring", "demerger",
    "spin-off", "spinoff", "class action",
]
EVOLUTION_POSITIVE = [
    "raises guidance", "guidance raised", "lifts guidance",
    "beats estimates", "beats expectations", "margin improvement",
    "margins improve", "restructuring complete", "restructuring milestone",
    "debt reduced", "reduces debt", "upgrade", "upgraded",
]

EVOLUTION_NEWS_PER_HEADLINE = {"thesis-breaking": 20.0, "material-negative": 5.0, "positive": 5.0}
EVOLUTION_NEWS_CUT_CAP_THESIS = 30.0
EVOLUTION_NEWS_CUT_CAP_MATERIAL = 10.0
EVOLUTION_NEWS_ADD_CAP_POSITIVE = 10.0


def _evolution_classify_news(text):
    """Director's own narrow three-tier (+positive) scheme for TASK 16 -
    priority order: thesis-breaking first, then material-negative, then
    the no-cut review-only tier, then positive. Anything matching none of
    the four lists is not counted at all (unlike portfolio_news_engine.py's
    own classifier, which always falls through to "noise")."""
    t = (text or "").lower()
    if pne._contains_any_v2(t, EVOLUTION_THESIS_BREAKING):
        return "thesis-breaking"
    if pne._contains_any_v2(t, EVOLUTION_MATERIAL_NEGATIVE):
        return "material-negative"
    if pne._contains_any_v2(t, EVOLUTION_REVIEW_ONLY):
        return "review-only"
    if pne._contains_any_v2(t, EVOLUTION_POSITIVE):
        return "positive"
    return None


def _evolution_news_since_purchase(ticker, buy_date):
    """Reads the SAME stored news_events rows analyze_holding_news_v2()
    reads (pne._load_events_with_fetched_at - no fetch triggered here),
    but applies a strictly-after-buy_date cutoff (no analyze_holding_
    news_v2()-style buy_date-minus-3-day grace window) and the Director's
    own tiering above instead of THESIS_BREAKING/MATERIAL_RISK/POSITIVE.
    Returns {"score", "thesis_breaking": bool, "headlines": [...]}; every
    matched headline is listed with its date/tier/points even once a
    tier's cut/add-back cap is reached, so the owner can see everything
    that was counted, not just what still moved the score."""
    buy_dt = pne._parse_date(buy_date)
    if buy_dt is None or not ticker:
        return {"score": None, "thesis_breaking": False, "headlines": [],
                "note": "No buy date on file - news-since-purchase not available"}

    events = pne._load_events_with_fetched_at(ticker.upper())
    headlines = []
    thesis_cut = 0.0
    material_cut = 0.0
    positive_add = 0.0
    thesis_breaking = False

    for evt in events:
        date = evt.get("date")
        if date is None or date <= buy_dt:
            continue
        text = f"{evt.get('title') or ''} {evt.get('description') or ''}"
        tier = _evolution_classify_news(text)
        if tier is None:
            continue
        if tier == "thesis-breaking":
            thesis_breaking = True
            points = -EVOLUTION_NEWS_PER_HEADLINE["thesis-breaking"]
            if thesis_cut > -EVOLUTION_NEWS_CUT_CAP_THESIS:
                thesis_cut = max(thesis_cut + points, -EVOLUTION_NEWS_CUT_CAP_THESIS)
        elif tier == "material-negative":
            points = -EVOLUTION_NEWS_PER_HEADLINE["material-negative"]
            if material_cut > -EVOLUTION_NEWS_CUT_CAP_MATERIAL:
                material_cut = max(material_cut + points, -EVOLUTION_NEWS_CUT_CAP_MATERIAL)
        elif tier == "positive":
            points = EVOLUTION_NEWS_PER_HEADLINE["positive"]
            if positive_add < EVOLUTION_NEWS_ADD_CAP_POSITIVE:
                positive_add = min(positive_add + points, EVOLUTION_NEWS_ADD_CAP_POSITIVE)
        else:
            points = 0.0
        headlines.append({"date": date, "title": evt.get("title"), "tier": tier, "points": points})

    score = max(0.0, min(100.0, 50.0 + thesis_cut + material_cut + positive_add))
    headlines.sort(key=lambda h: h["date"], reverse=True)
    return {"score": round(score, 1), "thesis_breaking": thesis_breaking, "headlines": headlines}


def _prog_distortion_note(current, baseline):
    """Flags exactly the mechanism _rel()'s own clamp(-1, 1) saturates on -
    a near-zero or negative baseline, where an arbitrarily small absolute
    move reads as a huge relative one (the Director's own example: growth
    1% -> 3% reads as "tripled"). Not a fixed epsilon: the real signal is
    whether |current-baseline|/|baseline| has already reached 1.0, the
    exact point _rel()'s clamp saturates the score to +-50 - so this
    fires precisely when, and only when, that saturation actually
    happened, for any field on the relative-change formula."""
    if current is None or baseline is None:
        return None
    if baseline == 0:
        return ("baseline is exactly zero - the relative-change score is a "
                "sign-only saturation, not a real magnitude")
    ratio = abs(current - baseline) / abs(baseline)
    if baseline < 0:
        return f"baseline is negative ({baseline:g}) - relative-change direction can read backwards"
    if ratio >= 1.0:
        return (f"baseline ({baseline:g}) is small enough that this move saturates the "
                f"relative-change score (|change|/|baseline| = {ratio:.1f}x)")
    return None


# ---------------------------------------------------------------------
# TASK 16c (Director-directed, 22 Oct 2026): fundamentals-since-purchase
# measured from REPORTED ACCOUNTS (tk.income_stmt/balance_sheet/cashflow,
# tk.dividends), never from Yahoo's live `.info` snapshot fields that
# fetch_snapshot()/compute_progress_v2() read (profitMargins/
# returnOnEquity/debtToEquity/dividendRate). SHADOW ONLY: this section
# feeds compute_evolution() alone - fetch_snapshot(), compute_health*,
# compute_progress*, and every live caller of them are untouched; the
# OLD Yahoo-snapshot-based numbers are still computed and kept on each
# component's own "old" sub-dict for comparison, they just no longer
# drive the Evolution score (see compute_evolution()'s own "old" wiring
# below). Root causes (Director's own, 22 Oct 2026):
#   A. profit_margin/roe are STATUTORY (post one-off items) - a
#      restructuring/impairment year (CSL FY26: US$7.1B pre-tax
#      impairments + US$0.8B restructuring) scores as business failure
#      instead of a deliberate, one-off cost. Fixed by using "Normalized
#      Income" (falling back to Net Income - Total Unusual Items*(1-tax
#      rate)) for Margins/ROIC/Growth, instead of the statutory figure.
#   B. dividend_rate (Yahoo's "dividendRate") is an INDICATED rate that
#      can read differently between two baseline captures on the SAME
#      day on a name with uneven interim/final dividends (OCL: two 28
#      Aug baselines hold 0.26 and 0.21, current reads 0.13 -> a false
#      "cut" that never happened). Fixed by using the actual trailing-
#      12-month SUM of real paid dividends (tk.dividends) as of buy_date
#      vs as of today - a fact, not a snapshot of an indicated rate.
#   C. A locked baseline snapshot's exact numbers depend on WHEN in the
#      refresh cycle it was captured (OCL SMSF vs Financial Freedom
#      differ on the same capture date). Fixed by anchoring every
#      fundamentals figure to a NAMED FISCAL YEAR column (FY_base/
#      FY_now) instead of a live snapshot value.
#   D. fetch_snapshot()'s own inline OCF/Capex lookup uses literal exact
#      row names only, no fallback - CSL's fcf_growth silently returns
#      None whenever either row's real name doesn't match exactly.
#      Fixed by reusing auto_compounder_engine._find_row()'s fallback-
#      aware row-name matching (the SAME aliases the Compounder/DCF
#      tools already use), plus a "Free Cash Flow" row fallback.
# ---------------------------------------------------------------------

EVOLUTION_FY_PUBLIC_DATE_FALLBACK_DAYS = 60
EVOLUTION_FY_PUBLIC_DATE_WINDOW_DAYS = 150
EVOLUTION_DEBT_TURNS_SCALE = 1.0


def _evolution_series_by_date(df, key):
    """{date: value} for a statement row, keyed by its REAL period-end
    date rather than column position - built from auto_compounder_
    engine._series_with_dates() (never a second date parser), so
    income/balance/cashflow rows (which don't share identical column
    sets on every ticker) can be looked up by the SAME fiscal-year date
    without assuming positional alignment across the three statements."""
    if df is None:
        return {}
    try:
        pairs = auto_compounder_engine._series_with_dates(df, key)
    except Exception:
        return {}
    return {d: v for d, v in pairs if d is not None}


@st.cache_data(ttl=21600)
def _evolution_report_dates(ticker):
    """Best-effort list of past earnings/announcement dates for `ticker`
    (ISO date strings) - reuses results_engine._parse_earnings_dates_df()
    (never a second parser of yf.Ticker.get_earnings_dates()' own shape)
    purely for its reported dates, not its EPS values. Returns [] on any
    failure or if the ticker has no earnings-date history on Yahoo (e.g.
    most ETFs) - this is a best-effort "if available" lookup; the
    Director's own spec names period-end + 60 days as the explicit
    fallback for exactly this case, applied by
    _evolution_public_date()."""
    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=12)
    except Exception:
        return []
    try:
        parsed = results_engine._parse_earnings_dates_df(df)
    except Exception:
        return []
    return [e["date"] for e in (parsed.get("eps_history") or [])]


def _evolution_public_date(period_end, report_dates):
    """The public/announcement date for one fiscal-year column - the
    earliest known report date strictly after `period_end`, within
    EVOLUTION_FY_PUBLIC_DATE_WINDOW_DAYS (generous enough to cover slow
    AU/UK reporters); period_end + EVOLUTION_FY_PUBLIC_DATE_FALLBACK_DAYS
    when no report date is known in that window - the Director's own
    explicit fallback rule."""
    if period_end is None:
        return None
    best = None
    for d_str in report_dates:
        try:
            d = date.fromisoformat(str(d_str)[:10])
        except Exception:
            continue
        delta = (d - period_end).days
        if 0 <= delta <= EVOLUTION_FY_PUBLIC_DATE_WINDOW_DAYS and (best is None or d < best):
            best = d
    if best is not None:
        return best
    return period_end + timedelta(days=EVOLUTION_FY_PUBLIC_DATE_FALLBACK_DAYS)


def _evolution_normalized_income(income, fy):
    """(normalized_income, source, total_unusual_items) for one fiscal-
    year column of the income statement. "Normalized Income" row
    directly when present ("normalized" source - the one-off items are
    already excluded by the data provider); otherwise Net Income - Total
    Unusual Items*(1 - effective tax rate) ("fallback" source) - the
    Director's own exact reconstruction formula, reusing auto_
    compounder_engine.effective_tax_rate() (never a second tax-rate
    guess) for the (1 - tax rate) term. Root cause A's own fix: this is
    what keeps a one-off impairment/restructuring year from reading as
    a margin/ROIC collapse."""
    norm = _evolution_series_by_date(income, "normalized_income").get(fy)
    unusual = _evolution_series_by_date(income, "total_unusual_items").get(fy)
    if norm is not None:
        return norm, "normalized", unusual
    net_income = _evolution_series_by_date(income, "net_income").get(fy)
    if net_income is None or unusual is None:
        return None, "unavailable", unusual
    tax_provision = _evolution_series_by_date(income, "tax_provision").get(fy)
    pretax_income = _evolution_series_by_date(income, "pretax_income").get(fy)
    tax_rate = auto_compounder_engine.effective_tax_rate(tax_provision, pretax_income)
    return net_income - unusual * (1.0 - tax_rate), "fallback", unusual


def _evolution_margins(income, fy_base, fy_now):
    revenue = _evolution_series_by_date(income, "revenue")
    rev_base, rev_now = revenue.get(fy_base), revenue.get(fy_now)
    ni_base, src_base, uu_base = _evolution_normalized_income(income, fy_base)
    ni_now, src_now, uu_now = _evolution_normalized_income(income, fy_now)
    margin_base = (ni_base / rev_base) if (ni_base is not None and rev_base) else None
    margin_now = (ni_now / rev_now) if (ni_now is not None and rev_now) else None
    score = (_prog_score_v2_rate(margin_now, margin_base, PROGRESS_V2_MARGIN_SCALE_PP)
              if (margin_base is not None and margin_now is not None) else None)
    note = "" if score is not None else "Revenue or normalized income unavailable for FY_base and/or FY_now"
    return {"score": round(score, 1) if score is not None else None,
            "fy_base": str(fy_base), "fy_now": str(fy_now),
            "base": margin_base, "now": margin_now,
            "unusual_items_base": uu_base, "unusual_items_now": uu_now,
            "source": f"{src_base}/{src_now}", "note": note}


def _evolution_roic(income, balance, fy_base, fy_now, bal_dates):
    equity = _evolution_series_by_date(balance, "stockholders_equity")

    def _avg_equity(fy):
        if fy not in bal_dates:
            return equity.get(fy)
        idx = bal_dates.index(fy)
        cur = equity.get(fy)
        prior = equity.get(bal_dates[idx + 1]) if idx + 1 < len(bal_dates) else None
        vals = [v for v in (cur, prior) if v is not None]
        return (sum(vals) / len(vals)) if vals else None

    ni_base, src_base, uu_base = _evolution_normalized_income(income, fy_base)
    ni_now, src_now, uu_now = _evolution_normalized_income(income, fy_now)
    eq_base, eq_now = _avg_equity(fy_base), _avg_equity(fy_now)
    roic_base = (ni_base / eq_base) if (ni_base is not None and eq_base) else None
    roic_now = (ni_now / eq_now) if (ni_now is not None and eq_now) else None
    score = (_prog_score_v2_rate(roic_now, roic_base, PROGRESS_V2_ROIC_SCALE_PP)
              if (roic_base is not None and roic_now is not None) else None)
    note = "" if score is not None else "Average equity or normalized income unavailable for FY_base and/or FY_now"
    return {"score": round(score, 1) if score is not None else None,
            "fy_base": str(fy_base), "fy_now": str(fy_now),
            "base": roic_base, "now": roic_now,
            "unusual_items_base": uu_base, "unusual_items_now": uu_now,
            "source": f"{src_base}/{src_now}", "note": note}


def _evolution_growth(income, fy_base, fy_now, fy_dates):
    revenue = _evolution_series_by_date(income, "revenue")
    ni_series = {fy: _evolution_normalized_income(income, fy)[0] for fy in fy_dates}

    def _growth_rate(fy, series):
        if fy not in fy_dates:
            return None
        idx = fy_dates.index(fy)
        if idx + 1 >= len(fy_dates):
            return None
        cur, prior = series.get(fy), series.get(fy_dates[idx + 1])
        if cur is None or not prior:
            return None
        return (cur - prior) / abs(prior)

    rev_growth_base, rev_growth_now = _growth_rate(fy_base, revenue), _growth_rate(fy_now, revenue)
    ni_growth_base, ni_growth_now = _growth_rate(fy_base, ni_series), _growth_rate(fy_now, ni_series)
    scores = []
    if rev_growth_base is not None and rev_growth_now is not None:
        scores.append(_prog_score_v2_rate(rev_growth_now, rev_growth_base, PROGRESS_V2_GROWTH_SCALE_PP))
    if ni_growth_base is not None and ni_growth_now is not None:
        scores.append(_prog_score_v2_rate(ni_growth_now, ni_growth_base, PROGRESS_V2_GROWTH_SCALE_PP))
    score = round(sum(scores) / len(scores), 1) if scores else None
    note = "" if score is not None else "Fewer than 2 annual columns available for a year-over-year growth rate"
    return {"score": score, "fy_base": str(fy_base), "fy_now": str(fy_now),
            "base": rev_growth_base, "now": rev_growth_now,
            "income_growth_base": ni_growth_base, "income_growth_now": ni_growth_now,
            "source": "revenue_growth+normalized_income_growth", "note": note}


def _evolution_fcf(income, cashflow, fy_base, fy_now):
    revenue = _evolution_series_by_date(income, "revenue")
    ocf = _evolution_series_by_date(cashflow, "operating_cash_flow")
    capex = _evolution_series_by_date(cashflow, "capex")
    fcf_row = _evolution_series_by_date(cashflow, "free_cash_flow")

    def _fcf_margin(fy):
        rev = revenue.get(fy)
        if not rev:
            return None, "revenue_missing"
        o, c = ocf.get(fy), capex.get(fy)
        if o is not None and c is not None:
            return (o + c) / rev, "ocf_capex"
        f = fcf_row.get(fy)
        if f is not None:
            return f / rev, "free_cash_flow_row"
        missing = [n for n, v in (("Operating Cash Flow", o), ("Capital Expenditure", c),
                                    ("Free Cash Flow", f)) if v is None]
        return None, "missing: " + ", ".join(missing)

    margin_base, src_base = _fcf_margin(fy_base)
    margin_now, src_now = _fcf_margin(fy_now)
    score = (_prog_score_v2_rate(margin_now, margin_base, PROGRESS_V2_FCF_SCALE_PP)
              if (margin_base is not None and margin_now is not None) else None)
    note = "" if score is not None else f"FY_base: {src_base}; FY_now: {src_now}"
    return {"score": round(score, 1) if score is not None else None,
            "fy_base": str(fy_base), "fy_now": str(fy_now),
            "base": margin_base, "now": margin_now,
            "source": f"{src_base}/{src_now}", "note": note}


def _evolution_debt(income, balance, fy_base, fy_now):
    total_debt = _evolution_series_by_date(balance, "total_debt")
    cash = _evolution_series_by_date(balance, "cash")
    ebitda_norm = _evolution_series_by_date(income, "normalized_ebitda")
    ebitda_plain = _evolution_series_by_date(income, "ebitda")

    def _turns(fy):
        debt = total_debt.get(fy)
        if debt is None:
            return None, "total_debt_missing"
        net_debt = debt - (cash.get(fy) or 0.0)
        ebitda, src = ebitda_norm.get(fy), "normalized_ebitda"
        if ebitda is None:
            ebitda, src = ebitda_plain.get(fy), "ebitda"
        if not ebitda or ebitda <= 0:
            return None, "ebitda_missing_or_nonpositive"
        return net_debt / ebitda, src

    turns_base, src_base = _turns(fy_base)
    turns_now, src_now = _turns(fy_now)
    if turns_base is None or turns_now is None:
        score = None
    else:
        delta_turns = turns_now - turns_base
        score = round(50.0 - max(-1.0, min(1.0, delta_turns / EVOLUTION_DEBT_TURNS_SCALE)) * 50.0, 1)
    note = "" if score is not None else f"FY_base: {src_base}; FY_now: {src_now}"
    return {"score": score, "fy_base": str(fy_base), "fy_now": str(fy_now),
            "base": turns_base, "now": turns_now,
            "source": f"{src_base}/{src_now}", "note": note}


def _evolution_income_from_dividends(dividends, buy_date):
    """Income Δ (TASK 16c fix B): the trailing-12-month SUM of ACTUAL
    paid dividends (tk.dividends, via fundamentals_data.get_bundle()'s
    own "dividends" {"dates","amounts"} shape) as of buy_date vs as of
    today - never Yahoo's live "dividendRate" (an indicated rate that
    can read differently between two same-day baseline captures on a
    name with uneven interim/final payments, root cause B). Relative
    change via the EXISTING _prog_score() formula, per the Director's
    own instruction - not percentage-point scored. A holding that paid
    nothing in EITHER window (e.g. GOLD) scores None, not a saturated
    0/50/100 - there is no real change to measure on a non-payer."""
    dates, amounts = dividends.get("dates") or [], dividends.get("amounts") or []
    pairs = []
    for d_str, amt in zip(dates, amounts):
        try:
            pairs.append((date.fromisoformat(str(d_str)[:10]), amt))
        except Exception:
            continue

    def _ttm_sum(ref_date):
        if ref_date is None:
            return None
        cutoff = ref_date - timedelta(days=365)
        return sum(amt for d, amt in pairs if cutoff <= d <= ref_date)

    buy_dt = pne._parse_date(buy_date)
    base_ttm = _ttm_sum(buy_dt.date()) if buy_dt else None
    now_ttm = _ttm_sum(datetime.now(timezone.utc).date())
    if base_ttm is None:
        return {"score": None, "base": None, "now": now_ttm, "note": "No buy date on file"}
    if base_ttm == 0.0 and now_ttm == 0.0:
        return {"score": None, "base": 0.0, "now": 0.0,
                "note": "No dividends paid in either 12-month window (non-payer)"}
    return {"score": _prog_score(now_ttm, base_ttm), "base": round(base_ttm, 4), "now": round(now_ttm, 4),
            "source": "tk.dividends - trailing-12-month sum of actual paid dividends", "note": ""}


def evolution_fundamentals(ticker, buy_date):
    """TASK 16c (Director-directed, 22 Oct 2026): fundamentals-since-
    purchase measured from REPORTED ACCOUNTS, not Yahoo's `.info`
    snapshot - see this section's own module-level comment above for the
    four root causes this fixes. Used ONLY by compute_evolution().

    FY_base = the latest ANNUAL column whose results were PUBLIC on or
    before buy_date (see _evolution_public_date()). FY_now = the latest
    annual column available today, regardless of its own public date.
    If the two are the SAME column, every FY-anchored component is None
    with a "No new annual report since purchase" note - dropped from the
    weighting entirely (compute_evolution() already treats a None score
    as "not available", never as neutral/50).

    Financials-mode tickers (financials_classifier.is_financials()) -
    banks/insurers, whose Total Revenue/OCF/Capex/EBITDA don't mean the
    same thing as a standard company's - get Growth/Margins/FCF/Debt Δ
    as None with an explanatory note; only Income Δ still scores (a
    dividend is a dividend regardless of sector).

    Returns a dict with keys "Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ",
    "Debt Δ", "Income Δ" - never raises; any missing/unavailable data
    degrades to score=None with a note, never a guess."""
    _na = {"score": None, "note": "No financial statements available"}
    out = {k: dict(_na) for k in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ")}

    bundle = None
    try:
        bundle = fundamentals_data.get_bundle(ticker)
    except Exception:
        bundle = None
    if not bundle:
        out["Income Δ"] = {"score": None, "note": "No financial statements available"}
        return out

    income, balance, cashflow = bundle.get("income"), bundle.get("balance"), bundle.get("cashflow")
    info = bundle.get("info") or {}
    try:
        is_fin = financials_classifier.is_financials(info, ticker=ticker)
    except Exception:
        is_fin = False

    ni_by_date = _evolution_series_by_date(income, "net_income")
    fy_dates = sorted(ni_by_date.keys(), reverse=True)
    bal_dates = sorted(_evolution_series_by_date(balance, "stockholders_equity").keys(), reverse=True)

    buy_dt = pne._parse_date(buy_date)
    report_dates = _evolution_report_dates(ticker) if ticker else []
    fy_base = None
    if buy_dt is not None:
        buy_day = buy_dt.date()
        for d in fy_dates:
            pub = _evolution_public_date(d, report_dates)
            if pub is not None and pub <= buy_day:
                fy_base = d
                break
    fy_now = fy_dates[0] if fy_dates else None

    if not fy_dates:
        note = "No annual income statement available"
        out = {k: {"score": None, "note": note} for k in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ")}
    elif fy_base is None:
        note = "No annual report was public as of buy_date"
        out = {k: {"score": None, "fy_now": str(fy_now), "note": note}
               for k in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ")}
    elif fy_base == fy_now:
        note = "No new annual report since purchase"
        out = {k: {"score": None, "fy_base": str(fy_base), "fy_now": str(fy_now), "note": note}
               for k in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ")}
    elif is_fin:
        note = "Financials-mode ticker - standard revenue/FCF/EBITDA model not applicable"
        out = {k: {"score": None, "fy_base": str(fy_base), "fy_now": str(fy_now), "note": note}
               for k in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ")}
    else:
        out["Margins Δ"] = _evolution_margins(income, fy_base, fy_now)
        out["ROIC Δ"] = _evolution_roic(income, balance, fy_base, fy_now, bal_dates)
        out["Growth Δ"] = _evolution_growth(income, fy_base, fy_now, fy_dates)
        out["FCF Δ"] = _evolution_fcf(income, cashflow, fy_base, fy_now)
        out["Debt Δ"] = _evolution_debt(income, balance, fy_base, fy_now)

    out["Income Δ"] = _evolution_income_from_dividends(bundle.get("dividends") or {}, buy_date)
    return out


def compute_evolution(snapshot, baseline, kind, buy_price, buy_date, ticker=None,
                       valuation_mos_pct=None):
    """TASK 16/16b/16c (Director-directed, owner-only shadow score, no
    live effect): measures EVOLUTION SINCE PURCHASE - is the business's
    fix working - instead of today's absolute levels and instead of news
    the market had already priced in at purchase.

    TASK 16c fix: Growth/Margins/ROIC/FCF/Debt/Income Δ now come from
    evolution_fundamentals() - REPORTED ACCOUNTS (tk.income_stmt/
    balance_sheet/cashflow/dividends), anchored to a named fiscal-year
    column, never Yahoo's live `.info` snapshot. See that function's own
    module-level comment for the four root causes this fixes (statutory
    one-off items distorting Margins/ROIC, dividendRate's same-day-
    baseline unreliability, snapshot-timing-dependent baselines, and
    fetch_snapshot()'s own narrow OCF/Capex row lookup). The OLD Yahoo-
    snapshot-based numbers (compute_progress_v2()'s own components) are
    still computed and kept on each component's own "old" sub-dict for
    comparison - per the Director's own instruction, they no longer
    drive the score at all.

    TASK 16b fix 1 (superseded for rate fields by 16c, still true for
    Debt Δ/Return/Intrinsic Δ, which 16c doesn't touch): compute_
    progress_v2() is always used, never compute_progress() (v1),
    regardless of is_progress_v2_live() - that switch only ever controls
    the LIVE Progress tab/Health blend.

    TASK 16b fix 2: Intrinsic Δ is INFORMATION ONLY - still computed and
    returned (baseline/current/score, plus a note), but excluded from
    EVOLUTION_WEIGHTS, so it never moves `overall`.

    compute_health*/compute_progress*/_recommend() are never called or
    modified by this function, and this function never writes to any
    store - it returns a plain dict for the shadow panel to display and
    export."""
    is_etf = (kind or "STOCK").upper() == "ETF"
    baseline = baseline or {}
    is_legacy_baseline = baseline.get("schema") == "website_v1"

    progress = compute_progress_v2(snapshot, baseline, kind, buy_price, buy_date)
    prog_comps = progress.get("components", {})
    evo_fund = evolution_fundamentals(ticker, buy_date) if ticker else {}

    distortions = []
    # TASK 16c: Growth/Margins/ROIC/FCF Δ no longer read prog_comps at
    # all (see evo_fund above), so the OLD relative-change saturation
    # guard below applies only to the comparison columns, never to the
    # score - each is labelled "(old Yahoo-snapshot comparison)" so a
    # flagged OLD column is never mistaken for a live distortion in the
    # new, reported-accounts-based score. Debt Δ/Return/Income Δ's OLD
    # comparison columns stay on the relative-change formula in both v1
    # and v2 (PART C STEP C2 never touched "level" fields), so the same
    # guard still applies to them too. Intrinsic Δ is still flagged here
    # (a near-zero/negative IV baseline is a real distortion in its own
    # right) - separate from, and in addition to, its own model-drift
    # note below.
    rel_based = {"Debt Δ", "Intrinsic Δ", "Return", "Income Δ"}
    for name in rel_based:
        comp = prog_comps.get(name) or {}
        note = _prog_distortion_note(comp.get("current"), comp.get("baseline"))
        if note:
            label = name if name in ("Intrinsic Δ", "Return") else f"{name} (old Yahoo-snapshot comparison)"
            distortions.append({"component": label, "note": note})
    if is_legacy_baseline:
        distortions.append({
            "component": "baseline",
            "note": "Legacy website_v1 baseline - Growth/Margins/ROIC/FCF/Debt/Income "
                     "baseline fields are missing (read as n/a); only Price/Intrinsic "
                     "Value/Quality were captured at purchase.",
        })

    drawdown = _post_purchase_drawdown(snapshot.get("history"), buy_date, snapshot.get("price"))
    drawdown_score = _interp(DRAWDOWN_BAND, drawdown * 100 if drawdown is not None else None)
    return_score = (prog_comps.get("Return") or {}).get("score")
    price_parts = [p for p in (return_score, drawdown_score) if p is not None]
    price_since_purchase = {
        "score": round(sum(price_parts) / len(price_parts), 1) if price_parts else None,
        "return_score": return_score, "drawdown": drawdown, "drawdown_score": drawdown_score,
        "current": snapshot.get("price"), "baseline": buy_price or baseline.get("price"),
    }

    news = None if is_etf else _evolution_news_since_purchase(ticker, buy_date)

    comps = {}
    if not is_etf:
        for name in ("Growth Δ", "Margins Δ", "ROIC Δ", "FCF Δ", "Debt Δ"):
            new_c = dict(evo_fund.get(name) or {"score": None, "note": "evolution_fundamentals() unavailable"})
            new_c["old"] = prog_comps.get(name) or {"score": None, "current": None, "baseline": None}
            comps[name] = new_c
        _intrinsic = dict(prog_comps.get("Intrinsic Δ") or {"score": None, "current": None, "baseline": None})
        _intrinsic["note"] = (
            (_intrinsic.get("note") or "")
            + (" " if _intrinsic.get("note") else "")
            + "Information only (weight 0) - the valuation model has changed since "
              "purchase (discount-rate design, growth rules, risk-free fixes) and "
              "purchase-day model inputs were never stored, so this isn't a reliable "
              "like-for-like comparison."
        ).strip()
        comps["Intrinsic Δ"] = _intrinsic
    comps["Price since purchase"] = price_since_purchase
    _income_new = dict(evo_fund.get("Income Δ") or {"score": None, "note": "evolution_fundamentals() unavailable"})
    _income_new["old"] = dict(
        prog_comps.get("Income Δ") or {"score": None, "current": None, "baseline": None},
        source=EVOLUTION_INCOME_SOURCE_NOTE,
    )
    comps["Income Δ"] = _income_new
    if news is not None:
        comps["News since purchase"] = {
            "score": news["score"], "thesis_breaking": news["thesis_breaking"],
            "headlines": news["headlines"],
        }

    weights = EVOLUTION_WEIGHTS_ETF if is_etf else EVOLUTION_WEIGHTS
    weighted = [(w, comps[name]["score"]) for name, w in weights.items()
                if comps.get(name, {}).get("score") is not None]
    overall = round(sum(w * s for w, s in weighted) / sum(w for w, _ in weighted), 1) if weighted else None

    if overall is None:
        action = "Not enough data yet"
    elif overall < EVOLUTION_ACTION_WATCH:
        action = "REVIEW"
    elif overall < EVOLUTION_ACTION_HOLD:
        action = "HOLD & WATCH"
    elif valuation_mos_pct is not None and valuation_mos_pct >= EVOLUTION_VALUATION_MOS_ADD:
        action = "HOLD / ADD"
    else:
        action = "HOLD"

    return {
        "components": comps, "overall": overall, "action": action,
        "review_now": bool(news and news["thesis_breaking"]),
        "distortions": distortions, "is_etf": is_etf,
        # TASK 16b fix 1: always "v2" now - compute_evolution() calls
        # compute_progress_v2() unconditionally, independent of
        # is_progress_v2_live() (that switch only ever controls the LIVE
        # Progress tab/Health blend). Kept as a field, not removed, so
        # anything already reading it keeps a stable key.
        "progress_source": "v2",
    }
