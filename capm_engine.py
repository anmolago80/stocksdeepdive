"""
capm_engine  -  CAPM-based cost of equity (discount rate) and a currency-based
terminal growth rate, replacing the old flat 9% / 3% constants that used to be
applied to every stock regardless of its own risk.

Discount rate (cost of equity):
    discount_rate = risk_free_rate + beta * EQUITY_RISK_PREMIUM

    risk_free_rate  - the 10-year government bond yield for the STOCK'S OWN
        currency (USD -> US 10Y via yfinance "^TNX", AUD -> AU 10Y), fetched
        live. Falls back to a fixed, occasionally-updated constant per
        currency if the live fetch fails - flagged as a default. (Not every
        government bond yield is reliably available through yfinance, so
        this degrades the same way every other feed in this app does.)
    beta            - the stock's own systematic-risk beta (yfinance
        info["beta"]). Defaults to 1.0 (market-average risk) when missing.
    EQUITY_RISK_PREMIUM - a single global constant (5%). There is no free
        live feed for this - it's a slow-moving, widely-cited long-run market
        assumption (broadly in line with published estimates such as
        Damodaran's ~4.5-5.5% range), not something re-derived per run.

Terminal / perpetual growth rate: tied to the stock's OWN CURRENCY (roughly
that economy's long-run inflation target) rather than calculated per-stock -
no company can out-grow its own economy forever in a Gordon-growth model.
See PERPETUAL_GROWTH_BY_CCY.

This app has no shared "data_engine" module (each engine calls yfinance
directly), so the risk-free-rate fetch lives here rather than in a separate
data layer - kept consistent with fcf_valuation_engine.py's own style.

Both outputs are clamped to a defensible band so a missing/bad beta or a
bond-yield fetch glitch can't produce a nonsense valuation.
"""

import streamlit as st
import yfinance as yf

EQUITY_RISK_PREMIUM = 0.05          # long-run market ERP assumption
DEFAULT_BETA = 1.0                  # market-average, used when info["beta"] is missing

DISCOUNT_FLOOR = 0.05
DISCOUNT_CEIL = 0.15

# DCF-fix constants: a listed equity's cost of capital below ~7.5% is not
# defensible for valuation purposes - the Gordon-growth terminal value is
# extremely sensitive to (discount_rate - perpetual_rate), and a discount
# rate sitting only a couple of points above terminal growth can blow the
# whole DCF up by an order of magnitude even with otherwise-sane inputs
# (this is exactly what happened for CSL.AX: a live beta near 0.2 pushed
# the CAPM rate down to the old 5% DISCOUNT_FLOOR, only 2.5pp above AUD
# terminal growth). MIN_DISCOUNT_RATE supersedes DISCOUNT_FLOOR as the
# effective floor inside resolve_discount_rate() below (DISCOUNT_FLOOR
# itself is left as-is since resolver_engine.py's bear/bull DCF scenario
# banding also clamps to it independently).
MIN_DISCOUNT_RATE = 0.075

# A measured beta below 0.6 is usually a data artefact (thin trading, a
# short or unusually defensive lookback window) rather than genuinely low
# systematic risk - flooring it keeps the CAPM discount rate defensible
# even when Yahoo's own beta figure looks implausibly low.
MIN_BETA = 0.60

# Terminal growth by the stock's OWN currency - roughly that economy's
# long-run inflation target / nominal trend growth, not the stock's own
# growth. Unknown currencies fall back to DEFAULT_PERPETUAL_GROWTH.
PERPETUAL_GROWTH_BY_CCY = {
    "AUD": 0.025,
    "USD": 0.020,
}
DEFAULT_PERPETUAL_GROWTH = 0.025

# 10-year government bond yield proxies used as the CAPM risk-free rate.
# "^TNX" (US 10Y Treasury Note yield) is a well-established yfinance ticker,
# quoted as yield*10. The AU equivalent is best-effort - Yahoo's coverage of
# non-US government bond yields is patchy, so this degrades to the fallback
# constant below (like every other optional feed in this app) when it can't
# be fetched.
_RISK_FREE_TICKERS = {"USD": "^TNX", "AUD": "AU10Y=RR"}
# ^TNX is quoted as yield*10 (a 4.2% yield reads as ~42.0), so dividing by
# 10 alone only recovers the percentage-point number (~4.2), not the
# decimal fraction (~0.042) the sanity band below expects - that value
# always failed `0.0 < rate < 0.20`, so the USD live-rate path was dead
# code (silently falling to RISK_FREE_FALLBACK on every call). Audit fix
# 1.1: divide by 1000 (10 for the quoting convention, x100 to go from a
# percentage-point number to a decimal fraction) so a live ^TNX read
# actually clears the sanity band and gets used.
_RISK_FREE_DIVISOR = {"^TNX": 1000.0, "AU10Y=RR": 100.0}

# Fallback risk-free rates (approximate 10-year yields), used only when the
# live bond-yield fetch fails or returns something outside a sane band.
# Update occasionally to keep these roughly current.
RISK_FREE_FALLBACK = {"USD": 0.042, "AUD": 0.043}
DEFAULT_RISK_FREE_FALLBACK = 0.04


@st.cache_data(ttl=10800, show_spinner=False)
def get_risk_free_rate(currency):
    """Live 10-year government bond yield for a currency, for use as the
    CAPM risk-free rate. Returns (rate, source) where source is "live" or
    "default" (the fallback constant above).

    Cached (keyed only on `currency` - there are only ever a couple of
    these in practice, USD/AUD) at a longer 3-hour TTL than the per-ticker
    feeds elsewhere in this app: a 10-year bond yield doesn't meaningfully
    move within a few hours, and EVERY stock's discount rate calls this, so
    without caching, a burst of concurrent visitors (e.g. after a big
    traffic spike) would otherwise refetch the SAME bond yield from Yahoo
    Finance once per ticker per visitor. Caching collapses that down to
    effectively one live fetch per currency per 3 hours, no matter how many
    people are browsing Deep Dive/Comparison at once - and Streamlit's
    cache_data locks per cache key, so concurrent first-time requests for
    the same currency wait on one fetch rather than all firing at once."""
    ccy = (currency or "").upper()
    ticker = _RISK_FREE_TICKERS.get(ccy)
    if ticker:
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if hist is not None and not hist.empty:
                raw = float(hist["Close"].iloc[-1])
                rate = raw / _RISK_FREE_DIVISOR.get(ticker, 100.0)
                if 0.0 < rate < 0.20:          # sanity band - reject garbage
                    return rate, "live"
        except Exception:
            pass
    return RISK_FREE_FALLBACK.get(ccy, DEFAULT_RISK_FREE_FALLBACK), "default"


def resolve_discount_rate(info, currency):
    """
    CAPM cost of equity for one stock. Returns (discount_rate, meta) where
    meta records where beta and the risk-free rate came from, and whether
    either had to fall back to a default (so the app can flag it).

    DCF fix: beta is floored at MIN_BETA before it enters the CAPM formula,
    and the resulting rate is floored at MIN_DISCOUNT_RATE (both above
    DISCOUNT_FLOOR's old, looser band) - either clamp firing is recorded in
    meta so the caller (fcf_valuation_engine.dcf_intrinsic_value) can pass
    it up to the app, which flags it on screen the same way every other
    assumption on this site is flagged.
    """
    info = info or {}
    meta = {
        "beta_source": "info", "rf_source": None, "defaulted": False,
        "beta_floored": False, "discount_floored": False, "floored": False,
    }

    beta = info.get("beta")
    if beta is None or beta <= 0:
        beta = DEFAULT_BETA
        meta["beta_source"] = "default"
        meta["defaulted"] = True

    if beta < MIN_BETA:
        beta = MIN_BETA
        meta["beta_floored"] = True

    rf, rf_src = get_risk_free_rate(currency)
    meta["rf_source"] = rf_src
    if rf_src == "default":
        meta["defaulted"] = True

    rate = rf + beta * EQUITY_RISK_PREMIUM
    if rate < MIN_DISCOUNT_RATE:
        meta["discount_floored"] = True
    rate = max(MIN_DISCOUNT_RATE, min(rate, DISCOUNT_CEIL))
    meta["floored"] = meta["beta_floored"] or meta["discount_floored"]
    return round(rate, 4), meta


def resolve_perpetual_rate(currency, discount_rate=None):
    """Currency-based terminal growth rate, kept strictly below the discount
    rate (Gordon growth is undefined/negative otherwise)."""
    rate = PERPETUAL_GROWTH_BY_CCY.get((currency or "").upper(), DEFAULT_PERPETUAL_GROWTH)
    if discount_rate is not None and rate >= discount_rate:
        rate = max(0.0, discount_rate - 0.01)
    return round(rate, 4)


@st.cache_data(ttl=1800, show_spinner=False)
def get_growth_estimates_5y(ticker):
    """
    Analyst consensus 'Next 5 Years (per annum)' EPS growth estimate for the
    stock itself (not the industry-average column), from yfinance's
    growth_estimates table. Returns a decimal rate, or None if Yahoo has no
    analyst coverage for this name or the table isn't shaped as expected -
    degrades silently, same as every other optional feed in this app.

    Cached the same way as the other per-ticker yfinance lookups in app.py
    (30-minute TTL, keyed by ticker) - previously uncached, so every single
    Deep Dive/Comparison view re-fetched this from Yahoo Finance even for a
    ticker someone else had just looked at seconds earlier.
    """
    try:
        df = yf.Ticker(ticker).growth_estimates
        if df is None or getattr(df, "empty", True):
            return None
        for label in [str(i) for i in df.index]:
            key = label.lower().replace(" ", "")
            if "5year" in key or key in ("+5y", "5y"):
                row = df.loc[label]
                for col in ("stock", "Stock", "Stock Trend", df.columns[0]):
                    if col in row.index:
                        v = row[col]
                        if v is not None and v == v:   # not NaN
                            return float(v)
                break
    except Exception:
        pass
    return None
