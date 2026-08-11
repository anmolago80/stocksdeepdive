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

import yfinance as yf

EQUITY_RISK_PREMIUM = 0.05          # long-run market ERP assumption
DEFAULT_BETA = 1.0                  # market-average, used when info["beta"] is missing

DISCOUNT_FLOOR = 0.05
DISCOUNT_CEIL = 0.15

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
_RISK_FREE_DIVISOR = {"^TNX": 10.0, "AU10Y=RR": 100.0}

# Fallback risk-free rates (approximate 10-year yields), used only when the
# live bond-yield fetch fails or returns something outside a sane band.
# Update occasionally to keep these roughly current.
RISK_FREE_FALLBACK = {"USD": 0.042, "AUD": 0.043}
DEFAULT_RISK_FREE_FALLBACK = 0.04


def get_risk_free_rate(currency):
    """Live 10-year government bond yield for a currency, for use as the
    CAPM risk-free rate. Returns (rate, source) where source is "live" or
    "default" (the fallback constant above)."""
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
    """
    info = info or {}
    meta = {"beta_source": "info", "rf_source": None, "defaulted": False}

    beta = info.get("beta")
    if beta is None or beta <= 0:
        beta = DEFAULT_BETA
        meta["beta_source"] = "default"
        meta["defaulted"] = True

    rf, rf_src = get_risk_free_rate(currency)
    meta["rf_source"] = rf_src
    if rf_src == "default":
        meta["defaulted"] = True

    rate = rf + beta * EQUITY_RISK_PREMIUM
    rate = max(DISCOUNT_FLOOR, min(rate, DISCOUNT_CEIL))
    return round(rate, 4), meta


def resolve_perpetual_rate(currency, discount_rate=None):
    """Currency-based terminal growth rate, kept strictly below the discount
    rate (Gordon growth is undefined/negative otherwise)."""
    rate = PERPETUAL_GROWTH_BY_CCY.get((currency or "").upper(), DEFAULT_PERPETUAL_GROWTH)
    if discount_rate is not None and rate >= discount_rate:
        rate = max(0.0, discount_rate - 0.01)
    return round(rate, 4)


def get_growth_estimates_5y(ticker):
    """
    Analyst consensus 'Next 5 Years (per annum)' EPS growth estimate for the
    stock itself (not the industry-average column), from yfinance's
    growth_estimates table. Returns a decimal rate, or None if Yahoo has no
    analyst coverage for this name or the table isn't shaped as expected -
    degrades silently, same as every other optional feed in this app.
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
