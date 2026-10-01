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
import logging
import math
import time

import requests
import streamlit as st
import yfinance as yf

# Growth-estimate-fetch resilience fix (owner-directed, 28 Sep 2026):
# real WARNING-level visibility for the yfinance calls in this module
# (previously bare `except: pass`, so a rate-limited or poisoned-crumb
# fetch failure - see nightly_scan.py's own module comment above
# _YF_RETRY_ATTEMPTS for the root cause - left no trace anywhere in the
# logs). Named "sdd.growth", following this codebase's established
# per-module logger convention (sdd.fetch/sdd.tools/sdd.scanner/...).
_growth_logger = logging.getLogger("sdd.growth")

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
    the same currency wait on one fetch rather than all firing at once.

    Growth-estimate-fetch resilience fix (owner-directed, 28 Sep 2026):
    this fetch used to be a bare try/except with no retry and no crumb-
    reset, so a poisoned yfinance crumb (see nightly_scan.py's own module
    comment above _YF_RETRY_ATTEMPTS) silently pinned this at the fallback
    constant for the rest of the process's life, with nothing logged.
    Reuses nightly_scan.py's own _yf_call_with_retry()/_reset_poisoned_
    yf_crumb() - the SAME helper the rest of this codebase's yfinance call
    sites already use for exactly this failure mode - rather than a new,
    third copy of the same retry/backoff/crumb-reset logic. Imported
    lazily (inside the function, not at module level): nightly_scan.py
    transitively imports capm_engine.py at ITS OWN module load time (via
    auto_compounder_engine/fcf_valuation_engine/resolver_engine), so a
    module-level `import nightly_scan` here would be circular; deferring
    it to call time is safe because every module in that chain, including
    this one, has already finished loading by the time any function here
    is actually invoked."""
    ccy = (currency or "").upper()
    ticker = _RISK_FREE_TICKERS.get(ccy)
    fallback_rate = RISK_FREE_FALLBACK.get(ccy, DEFAULT_RISK_FREE_FALLBACK)
    # Push 2 Part 2b (owner-directed, 30 Sep 2026): a USD large-cap Deep
    # Dive showed an implied risk-free rate identical to RISK_FREE_
    # FALLBACK["AUD"] (5.3%), raising the question of whether the live
    # ^TNX fetch is silently failing the same way the AU RBA fetch was
    # (see get_au_risk_free_rate_live()'s own Part 2 logging below). This
    # reason string is surfaced only for "^TNX" (the USD ticker) - the
    # "[capm] US 10y risk-free: live/FALLBACK" line settles it either
    # way from the next real production fetch's logs.
    fallback_reason = "no ticker configured"
    if ticker:
        import nightly_scan

        _last_exc = [None]

        def _fetch_hist():
            try:
                return yf.Ticker(ticker).history(period="5d")
            except Exception as e:
                _last_exc[0] = e
                raise

        hist = nightly_scan._yf_call_with_retry(
            _fetch_hist, log=lambda msg: None, ticker=ticker, label="risk_free_rate",
        )
        # hist is None only when EVERY retry attempt raised (see get_
        # growth_estimates_5y()'s own comment on this same pattern for
        # why the None check has to come before reading _last_exc[0] -
        # a later attempt succeeding after an earlier one raised must
        # not be logged as a failure).
        if hist is None and _last_exc[0] is not None:
            _growth_logger.warning(
                "get_risk_free_rate(%s): %s fetch failed after retries - %s: %s",
                ccy, ticker, type(_last_exc[0]).__name__, _last_exc[0],
            )
            fallback_reason = f"{type(_last_exc[0]).__name__}: {_last_exc[0]}"
        elif hist is None:
            fallback_reason = "empty fetch result"
        else:
            fallback_reason = "empty history" if hist.empty else "out of sanity band"
        try:
            if hist is not None and not hist.empty:
                raw = float(hist["Close"].iloc[-1])
                rate = raw / _RISK_FREE_DIVISOR.get(ticker, 100.0)
                if RISK_FREE_MIN < rate < RISK_FREE_MAX:   # sanity band - reject garbage
                    if ticker == "^TNX":
                        as_of = str(hist.index[-1].date()) if len(hist.index) else "unknown date"
                        _growth_logger.warning(
                            "[capm] US 10y risk-free: live %.2f%% (^TNX, as at %s)",
                            rate * 100, as_of,
                        )
                    return rate, "live"
        except Exception as e:
            fallback_reason = f"{type(e).__name__}: {e}"
    if ticker == "^TNX":
        _growth_logger.warning(
            "[capm] US 10y risk-free: FALLBACK %.2f%% - %s",
            fallback_rate * 100, fallback_reason,
        )
    return fallback_rate, "default"


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
    now surfaced), plus risk_free_used/market_cap_usd (Push 2, 30 Sep
    2026 - continuous size premium) so a caller can render the full
    "discount = risk-free + size premium" breakdown instead of just the
    combined rate.
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
        "risk_free_used": tiered_meta.get("risk_free_used"),
        "market_cap_usd": tiered_meta.get("market_cap_usd"),
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
# growth ceiling/end-rate interpolation (GROWTH_CEILING_ANCHORS_USD /
# GROWTH_END_RATE_ANCHORS_USD) uses (see that module for the full
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
# every band's discount rate tracks interest rates the same way the
# risk-free rate itself does - the owner's own explicit design goal.
# MIN_DISCOUNT_RATE (7.5%) still applies as a floor below - the owner
# has explicitly confirmed the top band (rf + 2%, ~7.2% for USD at
# today's rate) is EXPECTED to sit below it and floor there on
# purpose. DISCOUNT_CEIL (15%) does NOT apply to this path - the
# sub-US$300M band (rf + 6%, ~11.3% today) is the effective ceiling by
# design, so a separate flat ceiling above it would be redundant.
#
# Push 2 (owner-directed, 30 Sep 2026): the step table above used to
# jump straight from "small-cap (US$2B-10B)" to "micro-cap (< US$2B)"
# at a hard US$2B cliff, so a US$1.99B company got the harshest premium
# (+6%) and a US$2.01B company - economically identical - got +5%, a
# full percentage point of discount-rate (and therefore intrinsic-
# value) difference for a rounding error in market cap. Live symptom:
# OCL.AX (~US$0.9B, an established mid-sized industrial, not a genuine
# micro-cap) landed in the bottom tier and got the same premium as a
# speculative sub-US$300M name. Replaced with log-linear interpolation
# between these five FIXED anchor points (the exact same five values
# the old step table used, so any company sitting exactly on an old
# threshold gets the same number as before - see _interpolate_size_
# premium()'s own docstring for the boundary-exactness guarantee) on
# log10(market_cap_usd): a smooth curve with no cliffs anywhere, while
# every anchor value itself is unchanged from the owner's original A6
# design. (cap_usd, premium_over_risk_free) pairs, largest cap first.
SIZE_PREMIUM_ANCHORS_USD = [
    (200_000_000_000, 0.02),
    (50_000_000_000,  0.03),
    (10_000_000_000,  0.04),
    (2_000_000_000,   0.05),
    (300_000_000,     0.06),
]

# Display-only band names (Push 2) - a 6-way split of the old 5-tier
# label set, adding a "small-mid-cap (US$2B-10B)"/"small-cap (US$300M-
# 2B)" distinction in place of the old single "small-cap (US$2B-10B)"/
# "micro-cap (< US$2B)" pair, so a label like "small-cap (US$0.9B)"
# never implies a stock sits in the same bucket as a genuine sub-
# US$300M micro-cap. Labels only - the premium itself comes from
# _interpolate_size_premium() above, not from this table; kept
# separate so relabeling never risks touching the number.
_SIZE_BAND_LABELS = [
    (200_000_000_000, "mega-cap (>= US$200B)"),
    (50_000_000_000,  "large-cap (US$50B-200B)"),
    (10_000_000_000,  "mid-cap (US$10B-50B)"),
    (2_000_000_000,   "small-mid-cap (US$2B-10B)"),
    (300_000_000,     "small-cap (US$300M-2B)"),
    (0,               "micro-cap (< US$300M)"),
]


def _interpolate_size_premium(market_cap_usd):
    """Log-linear interpolation of the size premium (over risk-free)
    between the fixed anchors in SIZE_PREMIUM_ANCHORS_USD, on
    log10(market_cap_usd) - Push 2 (owner-directed, 30 Sep 2026). Above
    the top anchor (US$200B) the premium is flat at 2.0%; below the
    bottom anchor (US$300M, including a missing/zero market cap) it's
    flat at 6.0%; between two anchors it's a straight line in LOG-cap
    space, so a company halfway (in orders of magnitude) between two
    anchors gets a premium halfway between their two premiums.

    Boundary-exactness: at any of the five anchor values THEMSELVES,
    this returns that anchor's exact premium (frac lands on exactly 0.0
    or 1.0 - no floating-point step-function surprise) - so a company
    sitting exactly on an old step-table threshold gets the identical
    number it got before this change, only the values BETWEEN anchors
    changed (from a step to a slope)."""
    anchors = SIZE_PREMIUM_ANCHORS_USD   # descending by cap
    if market_cap_usd <= 0:
        return anchors[-1][1]
    if market_cap_usd >= anchors[0][0]:
        return anchors[0][1]
    if market_cap_usd <= anchors[-1][0]:
        return anchors[-1][1]
    log_cap = math.log10(market_cap_usd)
    for i in range(len(anchors) - 1):
        hi_cap, hi_prem = anchors[i]
        lo_cap, lo_prem = anchors[i + 1]
        if lo_cap <= market_cap_usd <= hi_cap:
            frac = (log_cap - math.log10(lo_cap)) / (math.log10(hi_cap) - math.log10(lo_cap))
            return lo_prem + frac * (hi_prem - lo_prem)
    return anchors[-1][1]   # unreachable given the short-circuits above; defensive only


def _size_band_label(market_cap_usd):
    """Display-only band name for market_cap_usd - see _SIZE_BAND_
    LABELS' own comment. Independent of _interpolate_size_premium():
    the label just names which band a company's cap falls in, the
    premium is always the smooth interpolated value, never the band's
    own old step value."""
    for threshold, label in _SIZE_BAND_LABELS:
        if market_cap_usd >= threshold:
            return label
    return _SIZE_BAND_LABELS[-1][1]

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

# Push 2 Part 2 (owner-directed, 30 Sep 2026): the RBA fetch's bare
# `except Exception: pass` meant NOTHING was ever logged for this
# function, on either the live or the fallback path - so an ASX Deep
# Dive showing exactly RISK_FREE_FALLBACK["AUD"] (5.3%) as its
# discount-rate risk-free component could mean "the live fetch is
# genuinely failing" or "5.3% happens to be the real current yield too"
# and neither this session nor the owner could tell which from the
# code alone. Logged once per real fetch (this function is cache_data-
# decorated, so the body - and therefore this log line - only runs on
# a cache miss, never on a cache hit) at WARNING (see _log_growth_
# estimate_labels_once()'s own docstring for why WARNING, not INFO, is
# the level that actually reaches the Railway logs from this process).
_au_risk_free_metadata_logged = False


def _log_au_risk_free_metadata_rows_once(rows):
    """One-time (per process) dump of the first 3 metadata rows' cell
    texts when the 10-year column can't be found at all - Push 2 Part 2
    (owner-directed, 30 Sep 2026): the FALLBACK log line alone says
    "column not found" but not what the CSV actually contained, so a
    next fix would still be guessing at the real column layout. This
    gives the next session real evidence instead."""
    global _au_risk_free_metadata_logged
    if _au_risk_free_metadata_logged:
        return
    _au_risk_free_metadata_logged = True
    try:
        _growth_logger.warning(
            "[capm] AU 10y risk-free: 10-year column not found - first 3 metadata "
            "rows: %r", [row[:8] for row in rows[:3]],
        )
    except Exception:
        pass


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
    this change's scope.

    Push 2 Part 2 (owner-directed, 30 Sep 2026): now logs the outcome
    of EVERY real fetch at WARNING - `[capm] AU 10y risk-free: live
    X.XX% (RBA F2, as at <date>)` on success, or `[capm] AU 10y risk-
    free: FALLBACK 5.30% - <reason>` on failure, with `<reason>` one of
    "HTTP <status>", the exception's own type+message, "10-year column
    not found", or "no in-band value" - see this section's own module
    comment for why this visibility matters now more than when this was
    an admin-only diagnostic."""
    fallback_rate = RISK_FREE_FALLBACK.get("AUD", DEFAULT_RISK_FREE_FALLBACK)
    reason = None
    try:
        resp = requests.get(_RBA_F2_CSV_URL, timeout=_RBA_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            reason = f"HTTP {resp.status_code}"
        else:
            rows = list(csv.reader(resp.text.splitlines()))
            # RBA statistical-table CSVs are WIDE: one column per bond
            # series (2-year, 3-year, 5-year, 10-year, ...), several
            # metadata rows (title, series ID, units, description) at
            # the top carrying each column's description, then one row
            # per date below. Find the 10-year column by a tolerant
            # substring match on ANY metadata-row cell (mirrors auto_
            # compounder_engine._find_row()'s own substring-match
            # tolerance for exactly this "don't know the exact label in
            # advance" situation) rather than assuming a label and its
            # value share one row - they don't, in this layout.
            target_col = None
            for row in rows:
                for i, cell in enumerate(row):
                    if any(hint in (cell or "").lower() for hint in _RBA_10Y_COLUMN_HINTS):
                        target_col = i
                        break
                if target_col is not None:
                    break
            if target_col is None:
                _log_au_risk_free_metadata_rows_once(rows)
                reason = "10-year column not found"
            else:
                # Scan from the LAST row upward (most recent date first -
                # RBA's date rows run oldest-to-newest, top-to-bottom)
                # for the first row with a usable numeric value in that
                # column. A blank cell (public holiday / no quote that
                # day) is skipped, not treated as a failure; a real-but-
                # out-of-band value stops the scan rather than reaching
                # further back into older, unrelated figures - same
                # "reject garbage in either direction" stance get_risk_
                # free_rate() already takes.
                found_value = False
                for row in reversed(rows):
                    if len(row) <= target_col:
                        continue
                    cell = row[target_col]
                    try:
                        raw = float(cell)
                    except (TypeError, ValueError):
                        continue
                    found_value = True
                    rate = raw / 100.0
                    if RISK_FREE_MIN < rate < RISK_FREE_MAX:   # same sanity band as get_risk_free_rate
                        as_of = row[0] if row else "unknown date"
                        _growth_logger.warning(
                            "[capm] AU 10y risk-free: live %.2f%% (RBA F2, as at %s)",
                            rate * 100, as_of,
                        )
                        return rate, "live"
                    break
                reason = "no in-band value" if found_value else "10-year column not found"
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"

    _growth_logger.warning(
        "[capm] AU 10y risk-free: FALLBACK %.2f%% - %s",
        fallback_rate * 100, reason or "unknown error",
    )
    return fallback_rate, "default"


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
        # Audit fixes Commit 4 (30 Sep 2026, owner-directed): market_cap_
        # missing already meant the tier below silently defaults to the
        # micro-cap premium (the LAST/smallest tier in MARKET_CAP_
        # DISCOUNT_TIERS - see the loop just below) purely because
        # market_cap_usd falls through every real threshold at 0 - but
        # `defaulted` was never set for this case (only for a risk-free-
        # rate fallback), so nothing downstream ever knew the discount
        # rate itself rested on an assumption. Setting it here is what
        # lets the caller (fcf_valuation_engine.dcf_intrinsic_value())
        # fire the red "estimated inputs" treatment for this case too.
        meta["defaulted"] = True
        market_cap = 0
    market_cap_usd = market_cap * _DISCOUNT_TIER_FX_TO_USD_APPROX.get(ccy, 1.0)

    # Push 2 (owner-directed, 30 Sep 2026): continuous log-linear
    # interpolation replaces the old step table - see SIZE_PREMIUM_
    # ANCHORS_USD's own comment. tier_label is now a band NAME (display
    # only, from _size_band_label()) rather than the premium's own
    # source - the premium always comes from the interpolation, never
    # from a table lookup keyed on the same threshold.
    premium = _interpolate_size_premium(market_cap_usd)
    tier_label = _size_band_label(market_cap_usd)
    meta["tier_label"] = tier_label
    meta["premium_used"] = premium
    meta["risk_free_used"] = rf
    meta["market_cap_usd"] = market_cap_usd

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


# Growth-estimate-fetch resilience fix (owner-directed, 28 Sep 2026): log
# the growth_estimates DataFrame's own index labels ONCE per process (the
# first ticker that returns a real DataFrame, whichever one that is), so
# the actual "+5y"-shaped row name Yahoo returns is visible in the logs
# instead of only inferred from the matching code below. A plain module-
# level flag, not per-ticker - this is a one-time "does the +5y row
# genuinely exist in the shape this code expects" sanity check, not a
# per-fetch diagnostic (get_growth_estimates_5y's own WARNING on an
# actual failure already covers that).
_growth_estimate_labels_logged = False


def _log_growth_estimate_labels_once(df):
    """WARNING, not INFO (30 Sep 2026, owner-directed, urgent fix): this
    used to log at INFO, which is silent by default - server.py is the
    ONLY process in this app that calls logging.basicConfig(level=INFO);
    the Streamlit subprocess, where "Rescan now" actually runs, never
    configures a handler at all, so Python's default root logger level
    (WARNING) dropped every one of these records with no trace anywhere.
    That's why this line "never appeared" for the owner despite the
    growth-never-zero rewrite explicitly depending on it - not because
    the code path wasn't hit, but because nothing was listening. All
    three growth_estimates diagnostics in this module are WARNING now
    for exactly this reason."""
    global _growth_estimate_labels_logged
    if _growth_estimate_labels_logged:
        return
    _growth_estimate_labels_logged = True
    try:
        _growth_logger.warning(
            "get_growth_estimates_5y: yfinance growth_estimates index labels (first "
            "ticker seen this process): %s", [str(i) for i in df.index],
        )
    except Exception:
        pass


# Cache-safety fix (owner-reported, 28 Sep 2026): a "fetch_failed" result
# must NOT sit at the same 30-minute TTL as a genuine "ok"/"no_coverage"
# one - a transient failure (a rate limit, a still-recovering poisoned
# crumb) should be retried again soon, not treated as settled for half
# an hour. st.cache_data's ttl is fixed per DECORATED FUNCTION, not per
# RETURN VALUE, so there is no way to give one outcome a shorter TTL
# than another while still using that decorator - this uses a manual
# per-ticker memo instead, same (result, fetched_at)-tuple/manual-expiry
# pattern as fcf_valuation_engine.fx_rate()'s own _fx_cache.
_growth_estimate_cache = {}
_GROWTH_ESTIMATE_SUCCESS_TTL_SECONDS = 1800   # 30 min - "ok"/"no_coverage" (a settled data fact)
_GROWTH_ESTIMATE_FAILURE_TTL_SECONDS = 300    # 5 min - "fetch_failed" (retry soon, not settled)


def get_growth_estimates_5y(ticker):
    """
    Analyst consensus 'Next 5 Years (per annum)' EPS growth estimate for the
    stock itself (not the industry-average column), from yfinance's
    growth_estimates table.

    Returns (value, status):
        value  - a decimal rate (e.g. 0.12 for 12%, unit-normalized by
                 _normalize_yahoo_growth_estimate() above - see its own
                 docstring), or None.
        status - "ok"           - value is a genuine, positive Yahoo
                                  estimate.
                 "non_positive" - a genuine Yahoo estimate (LTG/+5y) was
                                  found, but it's <=0 - a real DATA fact
                                  (Yahoo's own analysts expect a decline
                                  or flat growth), not a failure, but not
                                  usable as a stage-1 growth signal -
                                  see estimate_growth()'s own priority-1
                                  handling. Added 30 Sep 2026, growth-
                                  never-zero rewrite.
                 "ok_1y"        - LTG/+5y matched a row but every
                                  candidate column in it was NaN (Yahoo
                                  isn't populating long-term growth for
                                  this name - confirmed real, 30 Sep 2026,
                                  from a Dow 30/RMD.AX frame dump: the LTG
                                  row existed, stockTrend matched, value
                                  was NaN), so this falls back to the
                                  SAME account's own +1y (next fiscal
                                  year), then 0y (current fiscal year)
                                  row instead - see _fetch_growth_
                                  estimates_5y_uncached()'s own fallback
                                  comment. Same tier cap and same >0 rule
                                  as "ok" (estimate_growth()'s priority-1
                                  branch only looks at the raw value, not
                                  this status), just a shorter-horizon
                                  Yahoo figure - kept distinct so the
                                  label can honestly read "Yahoo analyst
                                  (next year)" rather than implying a
                                  genuine 5-year LTG figure was found.
                 "no_coverage"  - the fetch itself succeeded, but Yahoo
                                  has no LTG/+5y (or +1y/0y fallback)
                                  analyst estimate for this name (or the
                                  table isn't shaped as expected) - a
                                  real DATA fact, not a failure.
                 "fetch_failed" - the fetch raised (network error, rate
                                  limit, a poisoned yfinance crumb - see
                                  nightly_scan.py's own module comment
                                  above _YF_RETRY_ATTEMPTS) even after
                                  retrying - Yahoo's actual coverage for
                                  this name is UNKNOWN, not "no
                                  coverage". Callers that fall back to
                                  historical growth on a None value
                                  should show this distinction rather
                                  than silently implying "Yahoo has
                                  nothing on this stock" when the truth
                                  is "we couldn't ask Yahoo" - see
                                  fcf_valuation_engine.dcf_intrinsic_
                                  value()'s meta["yahoo_estimate_status"].

    Growth-estimate-fetch resilience fix (owner-directed, 28 Sep 2026):
    this used to be a bare try/except with no retry, so ANY transient
    failure (or a process-wide poisoned crumb caused by a COMPLETELY
    different ticker's fetch elsewhere in the app) silently and
    permanently (until this cache entry expired) fell back to "no
    coverage" - materially different from today's growth-rewrite
    behaviour (0d7ee0b/cc06b75), where Yahoo's estimate, when available,
    is now the SOLE determinant of growth rather than one input blended
    with history. Reuses nightly_scan.py's own _yf_call_with_retry()/
    _reset_poisoned_yf_crumb() (imported lazily - see get_risk_free_
    rate()'s own docstring for why a module-level import would be
    circular here) rather than a third copy of the same retry logic.

    Cached per-ticker, with a two-tier TTL - see _growth_estimate_cache's
    own comment just above for why a manual memo instead of the usual
    @st.cache_data(ttl=1800) every other per-ticker feed in this app
    uses: 30 minutes for a genuine "ok"/"no_coverage" result, only 5 for
    "fetch_failed" (so a transient failure self-heals within one page
    view or two, rather than pinning a stock to the history fallback for
    the same half hour a real data fact would earn).
    """
    cached = _growth_estimate_cache.get(ticker)
    if cached is not None:
        cached_value, cached_status, fetched_at = cached
        ttl = (
            _GROWTH_ESTIMATE_FAILURE_TTL_SECONDS if cached_status == "fetch_failed"
            else _GROWTH_ESTIMATE_SUCCESS_TTL_SECONDS
        )
        if time.time() - fetched_at < ttl:
            return cached_value, cached_status

    value, status = _fetch_growth_estimates_5y_uncached(ticker)
    _growth_estimate_cache[ticker] = (value, status, time.time())
    return value, status


def _row_value(row, columns):
    """First non-NaN value found across `columns` (in priority order) in
    a growth_estimates row, or None."""
    for col in columns:
        if col in row.index:
            v = row[col]
            if v is not None and v == v:   # not NaN
                return float(v)
    return None


# Growth-never-zero rewrite (owner-directed, 30 Sep 2026): Yahoo has
# switched the "Next 5 Years (per annum)" row's own label from "+5y"/"5y"
# to "LTG" (Long-Term Growth) - confirmed live 29 Sep 2026 (the module's
# own _log_growth_estimate_labels_once() logged the new index shape:
# ['0q','+1q','0y','+1y','LTG']). estimate_growth() and every nightly
# scan since then read NOTHING here (every universe logged "growth
# source - Yahoo 5y 0"), silently falling through to history/reported/
# default for every ticker. LTG is now tried FIRST (Yahoo's own current
# label for this exact metric); the old +5y/5y/5year labels are kept as
# a fallback in case Yahoo reverts or a different account/version still
# serves the old shape.
_LTG_LABEL_KEY = "ltg"
_FIVE_YEAR_LABEL_SUBSTR = "5year"
_FIVE_YEAR_LABEL_EXACT = ("+5y", "5y")

# LTG-fallback fix (owner-directed, 30 Sep 2026, from a Dow 30 rescan's
# RMD.AX frame dump at 09:25 UTC): confirmed NOT a parse bug - the LTG
# row exists, stockTrend is the correctly-matched column, but Yahoo
# itself returned NaN for that ticker's own long-term-growth figure
# (only indexTrend, the benchmark column, had a value). Rather than
# falling all the way through to history/reported/default (estimate_
# growth()'s priorities 2-4) whenever this happens, _fetch_growth_
# estimates_5y_uncached() below now tries this SAME account's own
# shorter-horizon analyst rows next - "+1y" (next fiscal year) first,
# then "0y" (current fiscal year) - in that priority order.
_ONE_YEAR_LABEL_EXACT = ("+1y", "0y")

# Column lookup: "stockTrend" added (30 Sep 2026) alongside the existing
# "Stock Trend" - a camelCase variant seen on some yfinance versions/
# data sources; tried before the positional df.columns[0] fallback so a
# genuinely-present, correctly-named column always wins over "whichever
# column happens to be first".
_GROWTH_ESTIMATE_VALUE_COLUMNS = ("stock", "Stock", "Stock Trend", "stockTrend")

_growth_estimate_raw_value_logged = False


def _log_growth_estimate_raw_value_once(label, raw_value):
    """One-time (per process) log of the ACTUAL raw value found for the
    first ticker that returns a usable LTG/+5y figure - unlike _log_
    growth_estimate_labels_once() (which only logs the row LABELS), this
    is what lets the owner confirm on the next real nightly run whether
    Yahoo's LTG value is a decimal fraction (0.103) or a percent number
    (10.3) - see estimate_growth() 1.1's own instruction: this sandbox
    has no network access to run that live verification script itself,
    so this log line is the mechanism that surfaces the real answer on
    the next production nightly scan instead.

    WARNING, not INFO (30 Sep 2026, owner-directed, urgent fix) - same
    reason as _log_growth_estimate_labels_once()'s own docstring: INFO
    is silently dropped outside server.py's own process, which is why
    this "never appeared" despite the underlying fetch code running
    fine."""
    global _growth_estimate_raw_value_logged
    if _growth_estimate_raw_value_logged:
        return
    _growth_estimate_raw_value_logged = True
    try:
        _growth_logger.warning(
            "get_growth_estimates_5y: raw value for label %r (first ticker seen this "
            "process): %r - confirms live LTG units (see estimate_growth() 1.1's own "
            "verification note)", label, raw_value,
        )
    except Exception:
        pass


def _infer_yahoo_growth_scale(df, columns):
    """Frame-level percent-vs-decimal scale decision (owner-directed, 30
    Sep 2026) - replaces the old PER-VALUE abs(v)>=1.5 heuristic
    (_normalize_yahoo_growth_estimate() below) for the three metrics most
    likely to sit in the SAME growth_estimates row/table as LTG: 0y, +1y,
    LTG itself. A per-value threshold can misclassify one genuinely low-
    single-digit growth figure (e.g. LTG=1.2, ambiguous on its own)
    even when its sibling metrics in the SAME table are unambiguously in
    percent units (e.g. +1y=8.4) - this looks at all three together and
    applies ONE scale decision to the whole table: if ANY of them has
    abs(value) >= 1.5, the table is in percent units (divide by 100);
    otherwise it's already a decimal fraction. Returns 100.0 (percent) or
    1.0 (decimal), or None if none of the three rows exist at all (the
    caller then falls back to the old per-value heuristic on whichever
    single value it did find)."""
    for key in ("0y", "+1y", _LTG_LABEL_KEY):
        for lbl in df.index:
            if str(lbl).lower().replace(" ", "") == key:
                v = _row_value(df.loc[lbl], columns)
                if v is not None and abs(v) >= 1.5:
                    return 100.0
    return None


_growth_estimate_raw_frame_logged = False


def _log_growth_estimate_raw_frame_once(ticker, df, ltg_labels, five_year_labels, scale, value, status):
    """One-time (per process) FULL dump of the growth_estimates frame for
    the first ticker fetched this process, plus the complete parse
    outcome - added 30 Sep 2026 (owner-directed, urgent) after a Dow 30
    rescan logged "growth source - Yahoo 5y 0" identical to the pre-fix
    29 Sep behaviour, AND neither of the two existing narrower
    diagnostics (_log_growth_estimate_labels_once/_log_growth_estimate_
    raw_value_once) appeared in the logs at all - both were INFO-level,
    silently dropped by the Streamlit subprocess's unconfigured root
    logger (see either of those functions' own docstrings for the full
    story; both are WARNING now for the same reason this one is).
    Deliberately NOT guessing at a fix beyond that logging-level bug
    until this dump shows the actual frame shape Yahoo is serving in
    production - logs repr(df) (the full frame, not just the labels),
    dtypes, columns and index together with what the parse loop below
    actually matched/inferred/returned, so the next real rescan's logs
    are enough on their own to diagnose whatever the real problem is
    (wrong column names, an unexpected frame shape, a still-poisoned
    crumb serving stale/empty data, etc.) without another round trip."""
    global _growth_estimate_raw_frame_logged
    if _growth_estimate_raw_frame_logged:
        return
    _growth_estimate_raw_frame_logged = True
    try:
        _growth_logger.warning(
            "get_growth_estimates_5y(%s): RAW FRAME DUMP (first ticker seen this "
            "process) - repr=%r dtypes=%r columns=%r index=%r || parse outcome: "
            "ltg_labels=%r five_year_labels=%r scale=%r value=%r status=%r",
            ticker, df,
            dict(df.dtypes) if hasattr(df, "dtypes") else None,
            list(df.columns) if hasattr(df, "columns") else None,
            [str(i) for i in df.index] if hasattr(df, "index") else None,
            ltg_labels, five_year_labels, scale, value, status,
        )
    except Exception:
        pass


_growth_estimate_null_row_logged = False


def _log_growth_estimate_null_row_once(ticker, label, row):
    """One-time PER UNIVERSE (changed from per-process 30 Sep 2026,
    LTG-fallback fix, owner-directed) WARNING when a matched label
    (LTG/+5y/...) has NO usable value across every candidate column in
    _GROWTH_ESTIMATE_VALUE_COLUMNS - added 30 Sep 2026 (owner-directed,
    urgent). If Yahoo's row label matching is fine but every candidate
    column name is wrong/missing for the shape this account's yfinance
    is actually serving (or, as confirmed live by the RMD.AX frame dump,
    Yahoo genuinely isn't populating the figure for that name), this is
    the ONLY diagnostic that shows that - the raw-value diagnostic never
    fires in that case, since _row_value() returned None and the loop
    just moves on to the next candidate label with no trace otherwise.
    Always includes the ticker (the %s below), so a universe-level log
    still identifies which name tripped it.

    Was a plain per-process once-flag (fired for at most one ticker in
    the entire process's lifetime, silently going quiet for every
    universe scanned after whichever one hit it first). nightly_scan.
    run_universe_scan() now calls reset_growth_null_row_log() once at
    the start of each universe's own scan, so this fires again for the
    first ticker in EACH universe that needs it - real per-universe
    coverage visibility instead of a single process-wide sample."""
    global _growth_estimate_null_row_logged
    if _growth_estimate_null_row_logged:
        return
    _growth_estimate_null_row_logged = True
    try:
        _growth_logger.warning(
            "get_growth_estimates_5y(%s): matched label %r but _row_value() found no "
            "usable value across columns %r - row repr: %r",
            ticker, label, _GROWTH_ESTIMATE_VALUE_COLUMNS, row,
        )
    except Exception:
        pass


def reset_growth_null_row_log():
    """Reset the per-universe 'matched label but NaN' one-time diagnostic
    (see _log_growth_estimate_null_row_once's own docstring) - called by
    nightly_scan.run_universe_scan() once at the start of each universe's
    own scan (LTG-fallback fix, owner-directed, 30 Sep 2026), so it fires
    at most once per universe instead of once per process."""
    global _growth_estimate_null_row_logged
    _growth_estimate_null_row_logged = False


def _fetch_growth_estimates_5y_uncached(ticker):
    """The actual yfinance fetch + retry + parsing for get_growth_
    estimates_5y() above - split out so that function's own docstring
    stays the one place callers read, and so this can be called on
    every cache miss without duplicating the memo logic inline."""
    import nightly_scan

    _last_exc = [None]

    def _fetch():
        try:
            return yf.Ticker(ticker).growth_estimates
        except Exception as e:
            _last_exc[0] = e
            raise

    df = nightly_scan._yf_call_with_retry(
        _fetch, log=lambda msg: None, ticker=ticker, label="growth_estimates_5y",
    )
    # df is None either because EVERY retry attempt raised (a genuine
    # fetch failure - _last_exc[0] is set, since _fetch() always records
    # it before re-raising) or because some attempt cleanly returned None
    # with no exception at all (_last_exc[0] stays None - a real "Yahoo
    # returned nothing" case). A non-None df here means SOME attempt
    # succeeded, even if an EARLIER one raised (and left a stale
    # _last_exc[0] behind) - that's a genuine "ok"/"no_coverage" case,
    # not a failure, so this checks df first rather than _last_exc[0].
    if df is None:
        if _last_exc[0] is not None:
            _growth_logger.warning(
                "get_growth_estimates_5y(%s): fetch failed after retries - %s: %s",
                ticker, type(_last_exc[0]).__name__, _last_exc[0],
            )
            return None, "fetch_failed"
        return None, "no_coverage"
    if getattr(df, "empty", True):
        _log_growth_estimate_raw_frame_once(ticker, df, [], [], None, None, "no_coverage")
        return None, "no_coverage"

    _log_growth_estimate_labels_once(df)

    try:
        labels = [str(i) for i in df.index]
        # Priority: LTG first (Yahoo's current label), then +5y/5y/5year
        # (the old shape). Collect ALL matching labels in each tier -
        # iterate every candidate rather than stopping at the first
        # label MATCH regardless of whether it had a usable value (the
        # OLD code's unconditional `break` after the first match: a
        # label that matched but whose row was all-NaN silently returned
        # "no_coverage" instead of trying the next candidate).
        ltg_labels = [lbl for lbl in labels if lbl.lower().replace(" ", "") == _LTG_LABEL_KEY]
        five_year_labels = [
            lbl for lbl in labels
            if _FIVE_YEAR_LABEL_SUBSTR in lbl.lower().replace(" ", "")
            or lbl.lower().replace(" ", "") in _FIVE_YEAR_LABEL_EXACT
        ]
        scale = _infer_yahoo_growth_scale(df, _GROWTH_ESTIMATE_VALUE_COLUMNS)
        result = None
        for lbl in ltg_labels + five_year_labels:
            v = _row_value(df.loc[lbl], _GROWTH_ESTIMATE_VALUE_COLUMNS)
            if v is None:
                _log_growth_estimate_null_row_once(ticker, lbl, df.loc[lbl])
                continue
            _log_growth_estimate_raw_value_once(lbl, v)
            normalized = (v / scale) if scale is not None else _normalize_yahoo_growth_estimate(v)
            # Growth-never-zero rewrite (30 Sep 2026): a real value that's
            # <=0 is a genuine DATA FACT (Yahoo's own analysts expect a
            # decline or flat growth), not a fetch problem - distinct
            # from "no_coverage" (nothing found at all). estimate_
            # growth()'s own priority 1 falls through to the next source
            # on a non-positive value, same as it always has for a plain
            # None - this status just tells the CALLER (and the nightly
            # summary line / Deep Dive caption) WHY it fell through.
            result = (normalized, "ok" if normalized > 0 else "non_positive")
            break

        # LTG fallback (owner-directed, 30 Sep 2026, from the RMD.AX
        # 09:25 UTC frame dump - see _ONE_YEAR_LABEL_EXACT's own comment
        # above). Only tried when an LTG/+5y label genuinely MATCHED
        # (ltg_labels or five_year_labels non-empty) but every one of
        # those rows was NaN across every candidate column (result is
        # still None) - the exact RMD.AX shape: the label exists, the
        # column is right, the value just isn't populated. Deliberately
        # NOT tried when NEITHER label ever matched at all (both empty) -
        # that's a genuinely different case ("Yahoo doesn't cover a 5y
        # estimate for this name at all", pre-existing "no_coverage"
        # behaviour, unchanged by this fix - see test_growth_estimate_
        # fetch_resilience.py's own "no_five_year_row_is_no_coverage"
        # check, which a broader trigger here would have silently
        # broken). A genuine non-positive LTG/+5y value already broke
        # out of the loop above as real, confirmed data - this fallback
        # never overrides that either, it only covers "Yahoo matched the
        # label but left it empty".
        if result is None and (ltg_labels or five_year_labels):
            one_year_labels = sorted(
                (lbl for lbl in labels if lbl.lower().replace(" ", "") in _ONE_YEAR_LABEL_EXACT),
                key=lambda lbl: _ONE_YEAR_LABEL_EXACT.index(lbl.lower().replace(" ", "")),
            )
            for lbl in one_year_labels:
                v = _row_value(df.loc[lbl], _GROWTH_ESTIMATE_VALUE_COLUMNS)
                if v is None:
                    continue
                normalized = (v / scale) if scale is not None else _normalize_yahoo_growth_estimate(v)
                # Same >0 rule as the LTG/+5y tier - a non-positive +1y/0y
                # figure isn't a usable stage-1 growth signal either, so
                # keep trying the next fallback label rather than
                # accepting it.
                if normalized > 0:
                    result = (normalized, "ok_1y")
                    break

        _log_growth_estimate_raw_frame_once(
            ticker, df, ltg_labels, five_year_labels, scale,
            result[0] if result else None, result[1] if result else "no_coverage",
        )
        if result is not None:
            return result
    except Exception:
        pass
    return None, "no_coverage"
