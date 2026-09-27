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
# quoted as a plain percentage (Yahoo's own ^TNX history shows ~5.16 for a
# 5.16% yield, not 51.6 - there is no separate x10 convention on this
# ticker). The AU equivalent is best-effort - Yahoo's coverage of non-US
# government bond yields is patchy, so this degrades to the fallback
# constant below (like every other optional feed in this app) when it
# can't be fetched.
_RISK_FREE_TICKERS = {"USD": "^TNX", "AUD": "AU10Y=RR"}
# Data-correctness audit A1 (27 Sep 2026, owner-reported): Audit fix 1.1
# (28 Aug 2026, bec711b) divided ^TNX by 1000 on the mistaken assumption
# that it's quoted as yield*10 - it isn't; Yahoo already quotes it as the
# plain percentage. Dividing a real ~5.16 reading by 1000 gave 0.00516 (a
# 0.52% "risk-free rate"), which is inside the old 0.0-0.20 sanity band
# and so was silently accepted and labelled "live" instead of being caught
# as garbage - the exact failure mode that band exists to prevent, just
# from the opposite direction (too low, not too high). Both tickers here
# are already plain-percentage quotes, so both use the same /100 divisor
# (a 5.16 reading -> 0.0516, the actual decimal fraction).
_RISK_FREE_DIVISOR = {"^TNX": 100.0, "AU10Y=RR": 100.0}

# Sanity band a live-fetched rate must clear to be trusted (rejects a
# fetch/divisor error in EITHER direction, not just implausibly high).
# Tightened from 0.0-0.20 by audit A1. 1% is a deliberately tighter floor
# than "any yield ever printed" (US 10Y briefly touched ~0.5% in 2020) -
# chosen specifically because it's ABOVE what an accidental extra /10 on
# a normal, current 3-8% yield would produce (0.03%-0.8%), so THIS class
# of divisor bug is actually rejected rather than merely discouraged; a
# genuine sub-1% yield environment would fall back to the (occasionally-
# updated) fallback constant below instead of a live read, which is an
# acceptable trade for making a repeat of this exact bug loud instead of
# silent. 15% remains comfortably above any developed-market 10-year
# print.
RISK_FREE_MIN = 0.01
RISK_FREE_MAX = 0.15

# Fallback risk-free rates (approximate 10-year yields), used only when the
# live bond-yield fetch fails or returns something outside a sane band.
# Update occasionally to keep these roughly current. Audit A1 (27 Sep
# 2026): the previous USD figure (0.042) was set when 10-year yields were
# meaningfully lower and was stale against a live ~5.16% US 10-year read
# the owner reported that day - moved to 0.050, a round, conservative
# approximation rather than pinning to one day's exact reading.
#
# AUD amended same audit, owner-supplied (27 Sep 2026): moved to 0.053,
# citing the AU 10-year yield at 5.3850% on 26 Sep 2026 (Trading
# Economics) and 5.16% on 1 Sep 2026 (ABC News) - AU10Y=RR's own live
# fetch currently 404s on every call (see get_risk_free_rate's
# docstring), so this fallback is the ONLY rate AUD stocks actually get.
# Still flagged as a defaulted (non-live) rate wherever it's used - a
# real AU 10-year source should still be found and swapped in once one
# is confirmed live (see A1 point 4 in the audit).
RISK_FREE_FALLBACK = {"USD": 0.050, "AUD": 0.053}
DEFAULT_RISK_FREE_FALLBACK = 0.04


@st.cache_data(ttl=10800, show_spinner=False)
def get_risk_free_rate(currency):
    """Live 10-year government bond yield for a currency, for use as the
    CAPM risk-free rate. Returns (rate, source) where source is "live" or
    "default" (the fallback constant above).

    Audit A1 (27 Sep 2026): "AU10Y=RR" was reported returning 404 on
    every call, meaning AUD currently ALWAYS uses RISK_FREE_FALLBACK, not
    a live read. Could not verify a working replacement ticker in this
    session (no live network access here to test candidates against
    yfinance) - the owner's own next step, with real access, should be to
    try yfinance-known alternates for the AU 10-year (candidates worth
    testing: "AU10YT=RR", "AU10Y.SG", or pulling the RBA's own published
    series if yfinance genuinely has no coverage) and swap
    _RISK_FREE_TICKERS["AUD"] once one is confirmed live. Left as the
    flagged fallback until then, per that audit's own "if not, say so"
    instruction.

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
                if RISK_FREE_MIN < rate < RISK_FREE_MAX:   # sanity band - reject garbage
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
