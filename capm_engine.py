"""
capm_engine  -  market-cap-tiered cost of equity (discount rate) and a
currency-based terminal growth rate.

Discount rate (cost of equity), A6 design (owner-approved LIVE, 28 Sep
2026), replacing beta-based CAPM entirely:
    discount_rate = risk_free_rate + market_cap_tier_premium

    risk_free_rate       - the 10-year government bond yield for the
        STOCK'S OWN currency (USD -> US 10Y via yfinance "^TNX", AUD -> the
        RBA's own published 10-year series), fetched live. Falls back to a
        fixed, occasionally-updated constant per currency if the live fetch
        fails - flagged as a default.
    market_cap_tier_premium - a premium over the risk-free rate set by
        which market-cap tier the stock's own market cap falls into (mega/
        large/mid/small/micro) - see MARKET_CAP_DISCOUNT_TIERS below for
        the exact table. Floored at MIN_DISCOUNT_RATE (7.5%); no ceiling -
        see MARKET_CAP_DISCOUNT_TIERS' own comment for why.

    Beta is not read anywhere in this formula. Pre-A6 history: this used to
    be rf + beta * EQUITY_RISK_PREMIUM (a single global 5% ERP constant), a
    formula this module still fetches the same risk-free rate for - only
    the RISK premium changed, from a noisy, easily-gamed per-stock
    statistic (a measured beta over a short or unusually defensive lookback
    window - see MARKET_CAP_DISCOUNT_TIERS' own comment for a worked
    example of exactly that failure mode) to a plain, auditable fact
    (market cap) that already tracks the same "how risky is this business"
    judgment beta was a noisy proxy for. See git history (pre-28-Sep-2026)
    for the retired beta formula, and resolve_discount_rate()'s own
    docstring for how this swap was made.

Terminal / perpetual growth rate: tied to the stock's OWN CURRENCY (roughly
that economy's long-run inflation target) rather than calculated per-stock -
no company can out-grow its own economy forever in a Gordon-growth model.
See PERPETUAL_GROWTH_BY_CCY.

This app has no shared "data_engine" module (each engine calls yfinance
directly), so the risk-free-rate fetch lives here rather than in a separate
data layer - kept consistent with fcf_valuation_engine.py's own style.

Both outputs are clamped to a defensible band so a missing market cap or a
bond-yield fetch glitch can't produce a nonsense valuation.
"""

import csv

import requests
import streamlit as st
import yfinance as yf

DISCOUNT_CEIL = 0.15

# DCF-fix constants: a listed equity's cost of capital below ~7.5% is not
# defensible for valuation purposes - the Gordon-growth terminal value is
# extremely sensitive to (discount_rate - perpetual_rate), and a discount
# rate sitting only a couple of points above terminal growth can blow the
# whole DCF up by an order of magnitude even with otherwise-sane inputs
# (this is exactly what happened for CSL.AX under the old beta-based
# formula: a live beta near 0.2 pushed the CAPM rate down to a level only
# 2.5pp above AUD terminal growth). Still the effective floor under the
# A6 market-cap-tiered formula below - MARKET_CAP_DISCOUNT_TIERS' own
# comment explains why the top (mega-cap) tier is EXPECTED to floor here
# at today's rates.
MIN_DISCOUNT_RATE = 0.075

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
    Cost of equity for one stock - A6 market-cap-tiered design, owner-
    approved LIVE 28 Sep 2026. Returns (discount_rate, meta).

    This is now a thin wrapper around resolve_discount_rate_by_market_cap()
    below (see that function's own docstring for the tier table, the
    floor/no-ceiling design, and the AU risk-free source) - the swap the
    A6 module comment above MARKET_CAP_DISCOUNT_TIERS anticipated: every
    existing caller of THIS function (fcf_valuation_engine.
    dcf_intrinsic_value(), moat_engine.py's cost-of-equity/WACC call
    sites, auto_compounder_engine._build_cost_of_capital()) now gets the
    tiered rate automatically, with zero changes needed at any of those
    call sites. Beta is not read anywhere in this call chain any more.

    meta keeps the same "defaulted"/"rf_source"/"discount_floored"/
    "floored" shape every existing caller already reads (beta_source/
    beta_floored are gone - nothing outside this module ever read them),
    plus tier_label/premium_used/market_cap_missing for a caller that
    wants to disclose which tier a stock landed in (see deep_dive_engine.
    py/auto_compounder_engine.py's Fair Value section for where those are
    now surfaced).
    """
    rate, tiered_meta = resolve_discount_rate_by_market_cap(info, currency)
    meta = {
        "rf_source": tiered_meta.get("rf_source"),
        "defaulted": tiered_meta.get("defaulted", False),
        "discount_floored": tiered_meta.get("discount_floored", False),
        "floored": tiered_meta.get("discount_floored", False),
        "tier_label": tiered_meta.get("tier_label"),
        "premium_used": tiered_meta.get("premium_used"),
        "market_cap_missing": tiered_meta.get("market_cap_missing", False),
    }
    return rate, meta


def resolve_perpetual_rate(currency, discount_rate=None):
    """Currency-based terminal growth rate, kept strictly below the discount
    rate (Gordon growth is undefined/negative otherwise)."""
    rate = PERPETUAL_GROWTH_BY_CCY.get((currency or "").upper(), DEFAULT_PERPETUAL_GROWTH)
    if discount_rate is not None and rate >= discount_rate:
        rate = max(0.0, discount_rate - 0.01)
    return round(rate, 4)


# =====================================================================
# A6 (27 Sep 2026, owner-directed, FINAL SPEC; owner-approved LIVE 28 Sep
# 2026): market-cap-tiered discount rate, replacing beta.
#
# resolve_discount_rate() above now calls resolve_discount_rate_by_
# market_cap() below directly - every live valuation on the site
# (fcf_valuation_engine.dcf_intrinsic_value(), moat_engine.py's cost-of-
# equity/WACC helpers, auto_compounder_engine._build_cost_of_capital())
# goes through resolve_discount_rate(), so the swap took effect for all
# of them at once, with no call-site changes needed anywhere else - this
# function was written in its final shape from the start specifically so
# that swap would be small and mechanical. admin_data_audit.check_a6_
# discount_tiers() and app.py's bulk saved-universe A6 audit panel still
# call this function directly too, for the same side-by-side comparison
# view - now comparing the live rate against itself, which is expected
# and harmless (not worth removing a working diagnostic panel over).
# =====================================================================

# Same static USD-bucketing FX snapshot fcf_valuation_engine.py's own
# MARKET_CAP_GROWTH_CEILINGS tiers use (see that module for the full
# rationale) - duplicated here as a small local constant rather than
# imported, the same precedent fcf_valuation_engine.py itself already
# set for _FCF_LABELS/_OCF_LABELS/_CAPEX_LABELS: fcf_valuation_engine
# imports capm_engine at module level, so importing fcf_valuation_
# engine back from here would be circular.
_DISCOUNT_TIER_FX_TO_USD_APPROX = {
    "USD": 1.0,
    "AUD": 0.65,
}

# Premiums are RELATIVE TO THE RISK-FREE RATE (not a flat add-on), so
# every tier's discount rate tracks interest rates the same way the
# risk-free rate itself does - the owner's own explicit design goal.
# (min USD market cap, premium over risk-free, label) triples, largest
# threshold first - the first one a company's market cap clears wins.
# MIN_DISCOUNT_RATE (7.5%) still applies as a floor below - the owner
# has explicitly confirmed the top tier (rf + 2%, ~7.2% for USD at
# today's rate) is EXPECTED to sit below it and floor there on
# purpose. DISCOUNT_CEIL (15%) does NOT apply to this tiered path -
# the under-US$2B tier (rf + 6%, ~11.2% today) is now the effective
# ceiling by design, so a separate flat ceiling above it would be
# redundant. One named, commented table, per the owner's own
# instruction - deliberately NOT merged with fcf_valuation_engine.py's
# MARKET_CAP_GROWTH_CEILINGS: one governs the discount rate, the other
# the FCF growth ceiling, and nothing requires their tier boundaries to
# line up.
MARKET_CAP_DISCOUNT_TIERS = [
    (200_000_000_000, 0.02, "mega-cap (>= US$200B)"),
    (50_000_000_000,  0.03, "large-cap (US$50B-200B)"),
    (10_000_000_000,  0.04, "mid-cap (US$10B-50B)"),
    (2_000_000_000,   0.05, "small-cap (US$2B-10B)"),
    (0,                0.06, "micro-cap (< US$2B)"),
]

# A6 addition (27 Sep 2026, owner-directed): with beta removed, the
# risk-free rate becomes the main driver of every discount rate, so the
# AU 10-year can no longer stay a hand-set constant the way it can
# today (where it's just one input among several, dampened by beta).
# Live source: the RBA's own published daily yields, statistical table
# F2 "Capital Market Yields - Government Bonds - Daily". Fetched at
# most once a day (cached below) and falling back to the flagged
# RISK_FREE_FALLBACK["AUD"] constant on ANY failure - same degrade-
# gracefully philosophy as every other optional feed in this app.
#
# STILL UNVERIFIED FROM ANY SANDBOX THIS SESSION HAS HAD (28 Sep 2026,
# live-swap commit): no session working on this repo has had live
# network access to test the exact CSV URL/column layout below against
# the real RBA site (confirmed EGRESS_BLOCKED against www.rba.gov.au on
# a direct check, same as the finance.yahoo.com/stocksdeepdive.com
# checks this whole codebase's audit history already documents). Written
# defensively for the wide (column-per-series) layout RBA's statistical
# tables are published in (short timeout, tolerant column-label
# matching, the SAME RISK_FREE_MIN/RISK_FREE_MAX sanity band as
# get_risk_free_rate() above, and a hard fallback to the flagged
# constant on ANY failure) so a wrong guess degrades to "flagged as
# defaulted" rather than ever corrupting a rate - but this now runs on
# EVERY AUD discount-rate resolution in production (owner-approved live,
# 28 Sep 2026 - see resolve_discount_rate_by_market_cap()'s own
# docstring), not just an admin dry-run panel, so a first real-server
# check of this URL/parsing (Railway logs will show "live" vs "default"
# in meta["rf_source"] for the first AUD ticker resolved after this
# deploy) matters more now than before, not less.
_RBA_F2_CSV_URL = "https://www.rba.gov.au/statistics/tables/csv/f02d-data.csv"
_RBA_TIMEOUT_SECONDS = 6
_RBA_10Y_COLUMN_HINTS = ("10-year", "10 year", "10yr")


@st.cache_data(ttl=86400, show_spinner=False)
def get_au_risk_free_rate_live():
    """Best-effort live AU 10-year government bond yield from the RBA's
    own published table F2, cached once a day (a daily series - no
    point refetching more often, and this keeps the site polite to the
    RBA's own servers). Returns (rate, source) in the same shape as
    get_risk_free_rate() - "live" or "default" (the flagged
    RISK_FREE_FALLBACK["AUD"] constant).

    A6, owner-approved LIVE 28 Sep 2026: called from resolve_discount_
    rate_by_market_cap() for every AUD stock's discount rate now - not
    just the admin dry-run panel any more. See the module comment above
    this section for the still-unverified-against-the-real-RBA-server
    caveat, which applies with more weight now that this reaches
    production traffic. Once genuinely confirmed live on the real
    server, this could in principle replace AU10Y=RR inside
    get_risk_free_rate() itself too (the beta-CAPM formula's own AUD
    risk-free source, still used nowhere in the live discount rate any
    more, but retained for any other caller) - not done here, out of
    this change's scope."""
    try:
        resp = requests.get(_RBA_F2_CSV_URL, timeout=_RBA_TIMEOUT_SECONDS)
        resp.raise_for_status()
        rows = list(csv.reader(resp.text.splitlines()))
        # RBA statistical-table CSVs are WIDE: one column per bond series
        # (2-year, 3-year, 5-year, 10-year, ...), several metadata rows
        # (title, series ID, units, description) at the top carrying each
        # column's description, then one row per date below. Find the
        # 10-year column by a tolerant substring match on ANY metadata-row
        # cell (mirrors auto_compounder_engine._find_row()'s own
        # substring-match tolerance for exactly this "don't know the
        # exact label in advance" situation) rather than assuming a label
        # and its value share one row - they don't, in this layout.
        target_col = None
        for row in rows:
            for i, cell in enumerate(row):
                if any(hint in (cell or "").lower() for hint in _RBA_10Y_COLUMN_HINTS):
                    target_col = i
                    break
            if target_col is not None:
                break
        if target_col is not None:
            # Scan from the LAST row upward (most recent date first - RBA's
            # date rows run oldest-to-newest, top-to-bottom) for the first
            # row with a usable numeric value in that column. A blank cell
            # (public holiday / no quote that day) is skipped, not treated
            # as a failure; a real-but-out-of-band value stops the scan
            # rather than reaching further back into older, unrelated
            # figures - same "reject garbage in either direction" stance
            # get_risk_free_rate() already takes.
            for row in reversed(rows):
                if len(row) <= target_col:
                    continue
                cell = row[target_col]
                try:
                    raw = float(cell)
                except (TypeError, ValueError):
                    continue
                rate = raw / 100.0
                if RISK_FREE_MIN < rate < RISK_FREE_MAX:   # same sanity band as get_risk_free_rate
                    return rate, "live"
                break
    except Exception:
        pass
    return RISK_FREE_FALLBACK.get("AUD", DEFAULT_RISK_FREE_FALLBACK), "default"


def resolve_discount_rate_by_market_cap(info, currency):
    """A6, owner-approved LIVE 28 Sep 2026: market-cap-tiered cost of
    equity - no beta anywhere in this formula. This IS the live
    discount-rate formula now - resolve_discount_rate() above simply
    calls this and adapts the meta shape for its existing callers.
    Returns (discount_rate, meta), meta in a shape a caller can display
    (rf_source, defaulted) plus tier_label. MIN_DISCOUNT_RATE still
    applies as a floor; there is deliberately no ceiling clamp - see
    MARKET_CAP_DISCOUNT_TIERS' own comment above.

    Uses get_au_risk_free_rate_live() for AUD (not get_risk_free_rate())
    - see that function's own docstring for its own still-unverified-
    from-a-sandbox caveat. USD keeps using get_risk_free_rate() (^TNX) -
    already a live, working source; the owner's own instruction only
    asked for AUD to stop being a hand-set constant and for "any future
    market" to eventually get its own real source, not to touch a
    currency that already has one."""
    info = info or {}
    ccy = (currency or info.get("currency") or "USD").upper()
    meta = {"rf_source": None, "defaulted": False, "tier_label": None,
            "market_cap_missing": False, "discount_floored": False}

    if ccy == "AUD":
        rf, rf_src = get_au_risk_free_rate_live()
    else:
        rf, rf_src = get_risk_free_rate(ccy)
    meta["rf_source"] = rf_src
    if rf_src == "default":
        meta["defaulted"] = True

    market_cap = info.get("marketCap")
    if not market_cap or market_cap <= 0:
        meta["market_cap_missing"] = True
        market_cap = 0
    market_cap_usd = market_cap * _DISCOUNT_TIER_FX_TO_USD_APPROX.get(ccy, 1.0)

    premium, tier_label = MARKET_CAP_DISCOUNT_TIERS[-1][1], MARKET_CAP_DISCOUNT_TIERS[-1][2]
    for threshold, prem, label in MARKET_CAP_DISCOUNT_TIERS:
        if market_cap_usd >= threshold:
            premium, tier_label = prem, label
            break
    meta["tier_label"] = tier_label
    meta["premium_used"] = premium

    rate = rf + premium
    if rate < MIN_DISCOUNT_RATE:
        meta["discount_floored"] = True
    rate = max(MIN_DISCOUNT_RATE, rate)
    return round(rate, 4), meta


def _normalize_yahoo_growth_estimate(v):
    """Growth-rewrite fix (owner-directed, 28 Sep 2026): yfinance's
    growth_estimates table has been observed returning this figure in
    BOTH shapes - a decimal fraction (0.12 meaning 12%) and a bare
    percentage number (12 meaning 12%) - depending on yfinance version/
    data source, with nothing in the table itself saying which. Handled
    explicitly here instead of trusting whichever shape happens to come
    back raw (the previous behaviour): abs(v) >= 1.5 is treated as a
    percentage-point figure and divided by 100; anything smaller is
    already a decimal fraction. 1.5 (150% growth as a RAW fraction) is
    implausible enough as a genuine analyst estimate, and immediately
    re-clamped to the market-cap growth ceiling (GROWTH_CEIL, 20% at
    most) by estimate_growth() regardless of which way a misread would
    go, that this threshold can't quietly misfire on a real high-growth
    name either way."""
    if v is None:
        return None
    return v / 100.0 if abs(v) >= 1.5 else v


@st.cache_data(ttl=1800, show_spinner=False)
def get_growth_estimates_5y(ticker):
    """
    Analyst consensus 'Next 5 Years (per annum)' EPS growth estimate for the
    stock itself (not the industry-average column), from yfinance's
    growth_estimates table. Returns a decimal rate (e.g. 0.12 for 12%,
    unit-normalized by _normalize_yahoo_growth_estimate() above - see its
    own docstring), or None if Yahoo has no analyst coverage for this name
    or the table isn't shaped as expected - degrades silently, same as
    every other optional feed in this app.

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
                            return _normalize_yahoo_growth_estimate(float(v))
                break
    except Exception:
        pass
    return None
