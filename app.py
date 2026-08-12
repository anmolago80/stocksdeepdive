import streamlit as st
import yfinance as yf
import pandas as pd
import time
import json
import html
import os
import concurrent.futures
import contextlib
from datetime import datetime, timezone

from trends_engine import get_trend_score
from news_engine import get_news_score, get_yahoo_news_score
from resolver_engine import (
    resolve_quality_score,
    resolve_intrinsic_value,
    resolve_stock_type,
)
from market_cap_engine import get_market_cap_bucket
from holding_engine import get_holding_period
from ranking_engine import calculate_long_score
from thesis_engine import generate_thesis
import trade_filter_engine
import indicators_engine
import swing_engine
import social_engine
import mood_engine
import plotly.graph_objects as go
import deep_dive_engine
import build_compounder_data
import paywall_engine
import feedback_engine
import watchlist_store
import scan_store
import scanner_engine

# -----------------------------------
# PAGE SETUP
# -----------------------------------

st.set_page_config(layout="wide", page_title="StocksDeepDive", page_icon="\U0001F4C8")

# No st.title() here - the landing page below draws its own big "Stocks
# DeepDive" brand title, and the other views show the slim nav bar
# instead, so a second generic page title would just be clutter.


# -----------------------------------
# TUNABLE CONFIG
#
# Long Signal cutoffs live here so they can be adjusted in one place rather
# than hunting through the scan loop. Defaults loosened from the original
# 80/60/40 to 70/50/30 - the old thresholds were strict enough that few
# stocks ever cleared WATCHLIST. A stock scores STRONG LONG if its Long
# Score is above STRONG_LONG, LONG if above LONG, WATCHLIST if above
# WATCHLIST, otherwise AVOID.
# -----------------------------------

SIGNAL_THRESHOLDS = {
    "STRONG_LONG": 70,
    "LONG": 50,
    "WATCHLIST": 30,
}


# -----------------------------------
# NEWSAPI KEY - server-side only
#
# Public site, so there's no per-visitor key box (that only ever made sense
# for a single person running this on their own PC). The key lives once as
# a server environment variable set at deploy time - never shown to, or
# entered by, a visitor. news_engine.get_news_score() already falls back to
# a NEWS_API_KEY env var / .streamlit/secrets.toml on its own, so passing
# None here (see FEATURE DEFAULTS below) is enough to pick it up; the
# Yahoo Finance half of News Score (get_yahoo_news_score) needs no key at
# all either way.
# -----------------------------------


# -----------------------------------
# CACHED DATA FETCH
#
# Defined up top (before the selection UI) because the "Mega Cap" and
# "Turnaround" universes need to screen candidates by live info (market
# cap / 52-week low) - reusing THIS SAME cached function means that screen
# doesn't cost any extra network calls beyond what the main scan needed
# anyway, since the same tickers get looked up again later in the loop.
# -----------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def get_ticker_info(ticker):
    try:
        return yf.Ticker(ticker).info
    except Exception:
        return {}


@st.cache_data(ttl=1800, show_spinner=False)
def get_price_history(ticker):
    """
    Daily OHLCV history for `ticker`, ~6 months back - the shared feed
    behind MA50, support/resistance, Fear/Greed, and every Deep Dive/Swing
    calculation. Pulled from yfinance (the Auto-Trading/Alpaca data path
    was removed from this public deployment - Auto-Trading isn't included
    here at all).
    """
    try:
        # 6 months (not 3) so the Trade Filter's 60-day support/resistance
        # window has a comfortable buffer of real trading days behind it.
        return yf.Ticker(ticker).history(period="6mo")
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def get_cashflow_df(ticker):
    """
    Annual cash-flow statement, cached. Used to derive each stock's OWN
    historical free-cash-flow growth (CAGR) for the DCF, rather than a fixed
    or single-point growth assumption. Returns an empty DataFrame on failure
    so the DCF can fall back to info-based growth, then a flagged default.
    """
    try:
        return yf.Ticker(ticker).cashflow
    except Exception:
        return pd.DataFrame()


def _prefetch_scan_data(tickers, live_data, enable_social, news_api_key):
    """
    Warms every st.cache_data-backed lookup the scan loop below needs, for
    ALL tickers at once, concurrently - instead of the scan loop hitting
    yfinance/Google Trends/News/StockTwits one ticker at a time in sequence.
    None of get_price_history/get_ticker_info/get_cashflow_df/get_trend_score/
    get_news_score/get_yahoo_news_score/social_engine.get_social_score ever
    call a Streamlit UI function directly (only the st.cache_data decorator,
    whose cache is process-wide and safe to populate from worker threads) -
    so this is safe to run off the main thread. The scan loop further below
    is completely untouched: it just hits an already-warm cache for every
    field, so the same scoring runs without waiting on each network
    round-trip one ticker at a time. This is the single biggest lever for
    scan speed on a large universe (100s of tickers).

    Deliberately swallows every per-ticker exception here: a prefetch
    failure just means that one lookup falls through to its own normal,
    uncached call inside the loop below (which already has its own
    try/except per ticker) - never something that should abort the scan.
    """
    def _warm_one(ticker):
        try:
            get_price_history(ticker)
            get_ticker_info(ticker)
            get_cashflow_df(ticker)
            if live_data:
                keyword = ticker.split(".")[0]
                get_trend_score(keyword, api_key=news_api_key or None)
                get_news_score(keyword, api_key=news_api_key or None)
                get_yahoo_news_score(ticker)
            if enable_social:
                social_engine.get_social_score(ticker)
        except Exception:
            pass

    # Capped concurrency: high enough to meaningfully overlap network
    # latency across tickers, low enough to stay polite to yfinance/Google
    # Trends/NewsAPI/StockTwits rather than tripping their own rate limits
    # (which would just turn into more per-ticker fallback-to-zero reads,
    # not an actual error - but overshooting on workers buys no extra
    # speed once a source starts throttling).
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as _executor:
        list(_executor.map(_warm_one, tickers))


@st.cache_data(ttl=1800, show_spinner=False)
def get_country_mood(country, api_key=None):
    """
    Cached wrapper around mood_engine.compute_country_mood() - a rolling
    Hopeful/Neutral/Anxious societal-mood read for the selected market, from
    Google Trends + NewsAPI top headlines (see mood_engine.py; NewsAPI
    replaced Reddit as the second source after Reddit blocked anonymous
    .json access on 28 May 2026). `api_key` is the NewsAPI key from the
    "NewsAPI Key" box below (same one News Score uses) - included in the
    cache key, so a newly-typed key correctly busts the cache. Purely a
    display widget: it never feeds into Long Score, Trader Score, or any
    per-stock/trading logic. Cached the same as the other live lookups
    above so switching tabs or re-running an unrelated widget doesn't
    refetch it every rerun.
    """
    mood = mood_engine.compute_country_mood(country, api_key=api_key)
    if not mood or mood.get("label") in (None, "", "Unknown"):
        # Raise so FAILURES never enter the 30-minute cache - otherwise one
        # GDELT rate-limit pins "Unknown" on screen for half an hour even
        # after GDELT recovers (st.cache_data doesn't cache exceptions).
        raise RuntimeError((mood or {}).get("error_detail") or "mood unavailable")
    return mood


# -----------------------------------
# SHARED CONTROLS
#
# These apply to BOTH ways of choosing what to scan (the "By Industry" tab and
# the "By Stock" tab), so they live above the tabs. The tabs themselves only
# differ in how they produce the ticker list - everything else (mode, data
# toggles, valuation model, position sizing) is common.
# -----------------------------------

# -----------------------------------
# FEATURE DEFAULTS - run silently in the background
#
# On the original single-user desktop app these were visitor-facing
# checkboxes (Trends & News / Market Cap / Social Sentiment) plus a NewsAPI
# key box. On the public site none of that belongs in front of a visitor -
# it's just switched on, and the NewsAPI key (if you want richer News
# Score results) is set once server-side as a NEWS_API_KEY environment
# variable rather than typed in per-visit. news_api_key=None here means
# "let news_engine fall back to that env var / secrets.toml on its own".
# -----------------------------------
live_data = True
include_market_cap = True
enable_social = True
news_api_key = None

# Strategy Mode (Long-Term Investment vs Swing Trading) is no longer a
# visitor-facing choice anywhere on the site - everything runs as
# Long-Term Investment (the original Long Score: value + quality led).
mode = "Long-Term Investment"
is_swing = False

# Swing-only position-sizing inputs used to live in a form inside the old
# Stock Comparison view. That form (and any Swing Trading UI) is gone, but
# the per-ticker scan loop below still computes swing numbers
# unconditionally, so these stay defined as plain, unused-by-default
# values rather than undefined names.
account_size = 50000.0
risk_pct = 1.0

# -----------------------------------
# VALUATION & FCF INPUTS  (single, per-stock aware panel)
#
# One window replaces the old two (global "assumptions" + separate "manual
# FCF"). An "Apply to" selector at the top drives everything:
#   * "All stocks - global defaults" edits the discount / perpetual / growth
#     used for EVERY stock (the base case) - or, in Auto mode, is skipped
#     entirely in favour of a per-stock CAPM discount rate + currency-based
#     terminal growth (see capm_engine.py).
#   * A specific ticker lets you override discount, perpetual, growth AND free
#     cash flow for THAT stock only - the DCF still does the maths, it just
#     uses your inputs for that one name. Per-stock overrides win over both
#     the globals AND Auto mode.
#
# Growth (all modes): analyst consensus (Yahoo's 5-year estimate) -> each
# stock's own historical FCF CAGR -> reported growth -> a flagged default -
# unless the growth override below is set to something other than 0/auto.
# -----------------------------------
st.session_state.setdefault("dcf_global", {"discount": 0.09, "perpetual": 0.03, "growth": 0.0})
if "fcf_overrides" not in st.session_state:
    # Session-only by design on this public deployment: each visitor's DCF
    # overrides live entirely in their own browser session (Streamlit's
    # session_state is already per-session/per-visitor) and start fresh
    # empty here - nothing is written to shared disk, so one visitor's
    # edits can never affect what anyone else sees, and everything resets
    # to defaults the moment their session ends.
    st.session_state["fcf_overrides"] = {}
st.session_state.setdefault("scanned_tickers", [])
st.session_state.setdefault("dcf_auto", True)

# NOTE: the "Valuation & FCF inputs" widgets (Auto toggle + global defaults)
# used to be drawn here at the top. They're now drawn at the very bottom,
# right above the "DCF Parameters" table, so ALL manual DCF input - global
# and per-stock - lives in one place after the results, instead of split
# across the top and bottom of the page. Streamlit widgets read/write
# st.session_state regardless of where on the page they're drawn, so moving
# them doesn't change how a saved value reaches the scan below - it's read
# straight from session_state either way.

# Globals used by the scan (per-stock overrides are applied inside the loop).
# When Auto mode is on, dcf_discount/dcf_perpetual are None so every stock
# without its own override gets a per-stock CAPM/currency rate instead of one
# flat number for the whole portfolio.
dcf_discount = None if st.session_state["dcf_auto"] else st.session_state["dcf_global"]["discount"]
dcf_perpetual = None if st.session_state["dcf_auto"] else st.session_state["dcf_global"]["perpetual"]
dcf_growth_override = (
    st.session_state["dcf_global"]["growth"]
    if st.session_state["dcf_global"]["growth"] > 0 else None
)
fcf_overrides = st.session_state["fcf_overrides"]

# -----------------------------------
# BACKGROUND SCHEDULER - nightly universe scans + weekly Mailgun digest.
# In-process (daemon thread) rather than a separate Railway cron service,
# because the Railway Volume can only attach to one service and the web
# app needs it. st.cache_resource = exactly one thread per server process.
# Set SCHEDULER_ENABLED=false to turn it off entirely.
# -----------------------------------
@st.cache_resource(show_spinner=False)
def _start_background_scheduler():
    try:
        import scheduler_engine
        return scheduler_engine.start()
    except Exception:
        return None


_start_background_scheduler()


# -----------------------------------
# SITE-WIDE BUTTON STYLE + NAV BAR
# -----------------------------------
st.markdown(
    """
    <style>
    .stButton > button, .stFormSubmitButton > button,
    div[data-testid="stPopover"] > button {
        white-space: nowrap !important;
    }
    .stButton > button, .stFormSubmitButton > button {
        border-radius: 10px !important;
        border: 1.5px solid #2dd4bf !important;
        color: #2dd4bf !important;
        background-color: rgba(45, 212, 191, 0.07) !important;
        font-weight: 600 !important;
        padding: 0.55rem 1.1rem !important;
        transition: all 0.15s ease !important;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover {
        background-color: #2dd4bf !important;
        color: #ffffff !important;
        border-color: #2dd4bf !important;
    }
    .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"],
    .stButton > button[kind="primaryFormSubmit"], .stFormSubmitButton > button[kind="primaryFormSubmit"] {
        background-color: #2dd4bf !important;
        color: #ffffff !important;
        border-color: #2dd4bf !important;
    }
    .stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover,
    .stButton > button[kind="primaryFormSubmit"]:hover, .stFormSubmitButton > button[kind="primaryFormSubmit"]:hover {
        background-color: #14b8a6 !important;
        border-color: #14b8a6 !important;
    }
    /* The site search box (st.form("site_search_form")) shouldn't show its
       own outline/background -- the search input's own light-gray fill is
       enough visual grouping on its own, forced here since st.form's
       border=False param alone doesn't fully suppress it in every
       Streamlit version. */
    div[data-testid="stForm"] {
        border: none !important;
        padding: 0 !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    /* Streamlit's default block-container top padding (several rem) leaves
       a large empty gap above the account bar/header on every page. Same
       override site-wide (compact or not) so every page sits consistently
       close to the top of the window instead of just one view.
       2.5rem (not less) so the account bar row clears Streamlit's own
       fixed header bar (60px tall) - anything much smaller and the top
       few pixels of the Subscribe/Sign out row render underneath the
       header and look clipped off. */
    div[data-testid="stAppViewContainer"] .block-container {
        padding-top: 2.5rem !important;
        max-width: 1240px !important;
        margin: 0 auto !important;
    }
    /* Streamlit's own chrome (toolbar/header/menu) has no place on a
       public product page. */
    header[data-testid="stHeader"] { display: none !important; }
    #MainMenu { visibility: hidden !important; }
    /* Hard fallback so the site NEVER renders light even if the theme
       config is missing on a deploy - same palette as config.toml. */
    .stApp, [data-testid="stAppViewContainer"] {
        background-color: #0b1220 !important;
    }
    /* ---------------- StocksDeepDive dark-terminal components ---------------- */
    .sdd-tape { background:#060b14; border:1px solid #1f3352; border-radius:8px;
      overflow:hidden; white-space:nowrap;
      font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px;
      padding:7px 0; margin:0 0 14px 0; }
    .sdd-tape-inner { display:inline-block; animation:sddscroll 45s linear infinite; }
    @keyframes sddscroll { from { transform:translateX(0); } to { transform:translateX(-50%); } }
    .sdd-tk { margin:0 18px; color:#8aa0b8; }
    .sdd-tk b { color:#e6edf5; font-weight:600; }
    .sdd-tk .up { color:#34d399; } .sdd-tk .dn { color:#fb7185; }
    .sdd-navrow { display:flex; align-items:center; justify-content:space-between; padding:4px 0 18px; }
    .sdd-logo { font-size:22px; font-weight:800; letter-spacing:-.3px; color:#e6edf5; }
    .sdd-logo .accent { color:#2dd4bf; }
    .sdd-badge-free { font-family:ui-monospace,Menlo,monospace; font-size:11.5px; color:#2dd4bf;
      border:1px solid #14b8a6; border-radius:999px; padding:4px 11px; letter-spacing:.4px; }
    .sdd-h1 { font-size:38px; line-height:1.14; letter-spacing:-.6px; font-weight:800; color:#e6edf5; }
    .sdd-h1 em { font-style:normal; color:#2dd4bf; }
    .sdd-sub { color:#8aa0b8; font-size:16px; line-height:1.55; margin:14px 0 18px; max-width:540px; }
    .sdd-sub b { color:#e6edf5; }
    .sdd-card { background:#121f36; border:1px solid #1f3352; border-radius:14px; padding:18px 20px; }
    .sdd-card-tag { display:flex; justify-content:space-between; font-family:ui-monospace,Menlo,monospace;
      font-size:10.5px; letter-spacing:1.2px; color:#5b7290; margin-bottom:12px; }
    .sdd-fa-head { display:flex; align-items:baseline; gap:10px; }
    .sdd-fa-tkr { font-family:ui-monospace,Menlo,monospace; font-size:19px; font-weight:700; color:#e6edf5; }
    .sdd-fa-nm { color:#8aa0b8; font-size:13px; }
    .sdd-fa-px { margin-left:auto; font-family:ui-monospace,Menlo,monospace; font-size:16px; color:#e6edf5; }
    .sdd-fa-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:12px; }
    .sdd-stat { background:#0f1a2e; border:1px solid #1f3352; border-radius:10px; padding:8px 12px; margin-bottom:8px; }
    .sdd-stat .k { font-size:10.5px; color:#5b7290; letter-spacing:.6px; }
    .sdd-stat .v { font-family:ui-monospace,Menlo,monospace; font-size:16px; font-weight:600; color:#e6edf5; margin-top:2px; }
    .sdd-pill { font-size:11px; font-weight:700; font-family:ui-monospace,Menlo,monospace;
      padding:3px 10px; border-radius:999px; letter-spacing:.4px; display:inline-block; }
    .sdd-spark-cap { font-size:11px; color:#5b7290; margin-bottom:4px; display:flex; justify-content:space-between; }
    .sdd-strip { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:26px 0 8px; }
    .sdd-tile { background:#121f36; border:1px solid #1f3352; border-radius:12px; padding:13px 16px; }
    .sdd-tile .k { font-size:11px; color:#5b7290; letter-spacing:.7px; }
    .sdd-tile .v { font-family:ui-monospace,Menlo,monospace; font-size:19px; font-weight:700; color:#e6edf5; margin-top:5px; }
    .sdd-tile .d { font-size:12px; margin-top:3px; color:#5b7290; font-family:ui-monospace,Menlo,monospace; }
    .sdd-kicker { font-family:ui-monospace,Menlo,monospace; font-size:11.5px; letter-spacing:1.6px;
      color:#2dd4bf; margin-top:34px; }
    .sdd-h2 { font-size:25px; letter-spacing:-.3px; margin:8px 0 6px; font-weight:700; color:#e6edf5; }
    .sdd-secsub { color:#8aa0b8; font-size:14.5px; max-width:640px; line-height:1.5; }
    .sdd-cards4 { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-top:22px; }
    .sdd-feat { background:#121f36; border:1px solid #1f3352; border-radius:14px; padding:18px 20px;
      text-decoration:none !important; display:block; transition:border-color .15s; }
    .sdd-feat:hover { border-color:#14b8a6; }
    .sdd-feat .ic { width:38px; height:38px; border-radius:9px; background:rgba(45,212,191,.1);
      border:1px solid rgba(45,212,191,.25); display:flex; align-items:center; justify-content:center; font-size:18px; }
    .sdd-feat h3 { font-size:16px; margin:12px 0 8px; color:#e6edf5; }
    .sdd-feat p { color:#8aa0b8; font-size:13.3px; line-height:1.55; margin:0; }
    .sdd-steps { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-top:22px; }
    .sdd-step { border-left:2px solid #14b8a6; padding:2px 0 2px 16px; }
    .sdd-step .n { font-family:ui-monospace,Menlo,monospace; color:#2dd4bf; font-size:12px; }
    .sdd-step h4 { font-size:15px; margin:6px 0 6px; color:#e6edf5; }
    .sdd-step p { color:#8aa0b8; font-size:13.3px; line-height:1.55; margin:0; }
    .sdd-honesty { margin-top:20px; background:rgba(251,113,133,.06); border:1px solid rgba(251,113,133,.25);
      border-radius:12px; padding:13px 18px; font-size:13.5px; color:#8aa0b8; line-height:1.55; }
    .sdd-honesty b { color:#fb7185; }
    .sdd-covgrid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-top:22px; }
    .sdd-cov { background:#121f36; border:1px solid #1f3352; border-radius:14px; padding:15px 18px; }
    .sdd-cov .tkr { font-family:ui-monospace,Menlo,monospace; font-weight:700; font-size:15px; color:#e6edf5; }
    .sdd-cov .ind { color:#5b7290; font-size:12px; margin-top:2px; }
    .sdd-cov .row { display:flex; justify-content:space-between; font-size:12.5px; color:#8aa0b8; margin-top:9px; }
    .sdd-cov .row b { font-family:ui-monospace,Menlo,monospace; color:#e6edf5; }
    .sdd-cov-req { border-style:dashed; text-align:center; color:#8aa0b8; font-size:13px;
      text-decoration:none !important; display:flex; flex-direction:column; gap:8px;
      align-items:center; justify-content:center; }
    .sdd-cta { margin:44px 0 0; background:linear-gradient(135deg,#0e2b33,#123047);
      border:1px solid #14b8a6; border-radius:16px; padding:28px 34px; }
    .sdd-footer { margin-top:52px; border-top:1px solid #1f3352; padding:26px 0 10px;
      color:#5b7290; font-size:12.8px; line-height:1.6; }
    .sdd-f-cols { display:flex; gap:60px; margin-bottom:18px; }
    .sdd-f-cols h5 { color:#e6edf5; font-size:13px; margin:0 0 4px; }
    .sdd-f-cols a { display:block; color:#8aa0b8 !important; margin-top:6px; font-size:13px;
      text-decoration:none !important; }
    .sdd-f-cols a:hover { color:#e6edf5 !important; }
    .sdd-disclaimer { border-top:1px solid #1f3352; padding-top:14px; max-width:900px; }
    .sdd-disclaimer b { color:#8aa0b8; }
    @media (max-width:900px) {
      .sdd-cards4, .sdd-covgrid { grid-template-columns:1fr 1fr; }
      .sdd-steps { grid-template-columns:1fr; }
      .sdd-strip { grid-template-columns:1fr 1fr; }
      .sdd-h1 { font-size:30px; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------------
# SITE HEADER - shown on every page, and the only navigation on the site.
#
# One minimal Google-style search box: type one ticker and hit Search for
# a Deep Dive, or two-plus (comma/space/newline separated) for a
# side-by-side Comparison - no separate buttons, no mode picker. The only
# other control is the "Rational Compounder Analysis" button just beneath
# it, since that page isn't a ticker search - it's a fixed watchlist.
#
# Clicking Search or the Rational Compounder Analysis button genuinely
# navigates to a new page (st.switch_page below - the URL changes and the
# page starts fresh at the top) rather than expanding results below the
# search box on the same page. `compact` shrinks EVERYTHING about the
# header (title, spacing, the search box itself, and drops the "how this
# works" caption entirely) on every page except home, so results pages
# start right at the top of the screen with as little to scroll past as
# possible - a visitor who already searched once doesn't need the box to
# stay full-size or the instructions repeated.
# -----------------------------------


# -----------------------------------
# MARKET TAPE + SEARCH DISPATCH + LANDING-PAGE HELPERS
#
# The scrolling index/ticker tape shown at the very top of every page, the
# single shared handler behind every search box / example chip on the site,
# and the HTML builders for the landing page's hero card, mood strip,
# feature cards and footer. All data fetches degrade silently: if a quote,
# mood read, or featured analysis can't be fetched, that element simply
# doesn't render - the page never breaks because one upstream source is
# down.
# -----------------------------------

def _fetch_with_budget(jobs, budget_seconds):
    """Run `jobs` ({name: zero-arg callable}) concurrently and return
    {name: result-or-None} after AT MOST `budget_seconds`. Anything that
    hasn't finished is left as None and keeps running in the background
    (its @st.cache_data cache still fills for the next visitor) - the page
    itself never blocks on a slow upstream source. This is what keeps the
    landing page fast even on a day Yahoo or GDELT is crawling."""
    import concurrent.futures as _cf
    out = {k: None for k in jobs}
    ex = _cf.ThreadPoolExecutor(max_workers=max(len(jobs), 1))
    futs = {ex.submit(fn): name for name, fn in jobs.items()}
    done, _not_done = _cf.wait(futs, timeout=budget_seconds)
    for f in done:
        try:
            out[futs[f]] = f.result()
        except Exception:
            pass
    ex.shutdown(wait=False)
    return out


_TAPE_TICKERS = [
    ("ASX 200", "^AXJO"), ("S&P 500", "^GSPC"), ("NASDAQ", "^IXIC"),
    ("AUD/USD", "AUDUSD=X"), ("CSL.AX", "CSL.AX"), ("BHP.AX", "BHP.AX"),
    ("AAPL", "AAPL"), ("RMD.AX", "RMD.AX"),
]

_EXAMPLE_CHIPS = [
    ("CSL.AX", "CSL.AX"), ("AAPL", "AAPL"), ("BHP.AX", "BHP.AX"),
    ("RMD.AX", "RMD.AX"), ("CSL.AX vs BHP.AX", "CSL.AX BHP.AX"),
]

_FEATURED_ROTATION = ["CSL.AX", "AAPL", "BHP.AX", "RMD.AX", "MSFT", "WES.AX", "GOOGL"]


@st.cache_data(ttl=1800, show_spinner=False)
def _tape_quotes(day_key):
    """(label, last, pct_change) for the tape - cached 30 min; `day_key`
    keeps the cache honest across day boundaries. Any symbol that fails is
    simply left off the tape."""
    out = []
    for label, sym in _TAPE_TICKERS:
        try:
            h = yf.Ticker(sym).history(period="5d")["Close"].dropna()
            if len(h) >= 2:
                last, prev = float(h.iloc[-1]), float(h.iloc[-2])
                if prev:
                    out.append((label, last, (last - prev) / prev * 100.0))
        except Exception:
            continue
    return out


def _render_tape(quotes=None):
    if quotes is None:
        _day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        quotes = _fetch_with_budget(
            {"q": lambda: _tape_quotes(_day)}, budget_seconds=6
        )["q"]
    if not quotes:
        return
    items = []
    for label, last, chg in quotes:
        cls = "up" if chg >= 0 else "dn"
        arrow = "▲" if chg >= 0 else "▼"
        px = f"{last:,.4f}" if last < 10 else f"{last:,.1f}" if last > 1000 else f"{last:,.2f}"
        items.append(
            f"<span class='sdd-tk'><b>{html.escape(label)}</b> {px} "
            f"<span class='{cls}'>{arrow} {chg:+.1f}%</span></span>"
        )
    row = "".join(items)
    st.markdown(
        f"<div class='sdd-tape'><div class='sdd-tape-inner'>{row}{row}</div></div>",
        unsafe_allow_html=True,
    )


def _dispatch_search(text):
    """One shared handler behind every search box and example chip on the
    site: one ticker -> Deep Dive, two or more -> Comparison."""
    raw = (text or "").replace(",", " ").replace("\n", " ").split()
    parsed = []
    for tok in raw:
        tok = tok.strip().upper()
        if tok and tok not in parsed:
            parsed.append(tok)
    if not parsed:
        st.warning("Type at least one ticker above, then hit Search.")
        return
    if len(parsed) == 1:
        tk = parsed[0]
        with st.spinner(f"Analyzing {tk}..."):
            st.session_state["dd_result"] = deep_dive_engine.analyze(
                tk, get_price_history, get_ticker_info, get_cashflow_df,
                news_api_key=news_api_key, live_data=live_data,
                enable_social=enable_social,
            )
        st.switch_page(PG_DEEP_DIVE)
    else:
        n_au = sum(1 for tk in parsed if tk.endswith(".AX"))
        stk_country = "USA" if (len(parsed) - n_au) > n_au else "Australia"
        st.session_state["cmp_stocks"] = parsed
        st.session_state["cmp_universe_source"] = f"Manual ticker list ({len(parsed)} entered)"
        st.session_state["cmp_scan_country"] = stk_country
        st.session_state["cmp_fresh"] = True
        st.switch_page(PG_COMPARISON)


def _render_example_chips(key_prefix):
    """One-click example searches - the cheapest possible first result for a
    visitor who doesn't know ticker formats yet. st.pills gives compact
    horizontal chips; the "done" flag stops a still-selected pill from
    re-dispatching on every rerun of the same page."""
    _sel = st.pills(
        "Try one:", [label for label, _ in _EXAMPLE_CHIPS],
        selection_mode="single", key=f"{key_prefix}_chips",
    )
    _done_key = f"{key_prefix}_chips_done"
    if not _sel:
        st.session_state.pop(_done_key, None)
    elif st.session_state.get(_done_key) != _sel:
        st.session_state[_done_key] = _sel
        _dispatch_search(dict(_EXAMPLE_CHIPS)[_sel])


@st.cache_data(ttl=21600, show_spinner=False)
def _featured_analysis(ticker, day_key):
    """Attention-lite Deep Dive for the landing page's featured card -
    cached 6h. live_data/social off keeps it fast and quota-free; the card
    only shows value/quality/psychology fields anyway."""
    try:
        dd = deep_dive_engine.analyze(
            ticker, get_price_history, get_ticker_info, get_cashflow_df,
            news_api_key=None, live_data=False, enable_social=False,
        )
        if dd.get("error"):
            return None
        return dd
    except Exception:
        return None


def _spark_path(closes, width=440, height=52, pad=4):
    """SVG polyline points for a price sparkline."""
    if closes is None or len(closes) < 2:
        return None, None
    vals = list(closes)
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + (width - 2 * pad) * i / (n - 1)
        y = pad + (height - 2 * pad) * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts), (pts[-1] if pts else None)


def _featured_card_html(dd, spark_pts, ma_pts, last_pt):
    score = max(0.0, min(100.0, float(dd["long_score"])))
    if score > SIGNAL_THRESHOLDS["STRONG_LONG"]:
        verdict, vcolor = "STRONG LONG", "#34d399"
    elif score > SIGNAL_THRESHOLDS["LONG"]:
        verdict, vcolor = "LONG", "#34d399"
    elif score > SIGNAL_THRESHOLDS["WATCHLIST"]:
        verdict, vcolor = "WATCHLIST", "#fbbf24"
    else:
        verdict, vcolor = "AVOID", "#fb7185"
    import math as _math
    theta = _math.pi * score / 100.0
    ex = 100 - 85 * _math.cos(theta)
    ey = 105 - 85 * _math.sin(theta)
    arc = (
        f"<path d='M15 105 A85 85 0 0 1 {ex:.1f} {ey:.1f}' fill='none' "
        f"stroke='{vcolor}' stroke-width='13' stroke-linecap='round'/>"
    ) if score > 1 else ""
    iv = dd.get("intrinsic_value")
    mos = dd.get("mos")
    iv_txt = f"{iv:,.2f} {dd['currency']}" if iv else "N/A"
    mos_txt = f"{mos:+.1f}%" if mos is not None else "N/A"
    mos_color = "#34d399" if (mos or 0) > 0 else "#fb7185"
    pills = []
    _pillmap = {
        "UNDERVALUED": "#34d399", "FAIR": "#fbbf24", "EXPENSIVE": "#fb7185",
        "FEARFUL": "#34d399", "GREEDY": "#fbbf24", "OVERHEATED": "#fb7185",
        "CALM": "#8aa0b8", "NEUTRAL": "#8aa0b8",
        "BUY": "#34d399", "WATCHLIST": "#fbbf24", "AVOID": "#fb7185",
    }
    val = dd.get("valuation")
    if val and val != "N/A":
        pills.append((val, _pillmap.get(val, "#8aa0b8")))
    sent = dd.get("psychology_sentiment")
    if sent:
        pills.append((f"SENTIMENT: {sent}", _pillmap.get(sent, "#8aa0b8")))
    setup = dd.get("trade_setup_signal")
    if setup:
        pills.append((f"ENTRY: {setup}", _pillmap.get(setup, "#8aa0b8")))
    pills_html = "".join(
        f"<span class='sdd-pill' style='background:{c}22;color:{c};border:1px solid {c}55;'>{html.escape(t)}</span>"
        for t, c in pills
    )
    spark_html = ""
    if spark_pts:
        ma_line = (
            f"<polyline points='{ma_pts}' fill='none' stroke='#5b7290' "
            f"stroke-width='1.5' stroke-dasharray='4 4'/>"
        ) if ma_pts else ""
        dot = ""
        if last_pt:
            lx, ly = last_pt.split(",")
            dot = f"<circle cx='{lx}' cy='{ly}' r='3.5' fill='#2dd4bf' stroke='#0b1220' stroke-width='2'/>"
        spark_html = (
            "<div class='sdd-spark-cap'><span>6-month price vs 50-day average</span>"
            "<span>via Yahoo Finance</span></div>"
            f"<svg viewBox='0 0 440 52' width='100%' height='48'>"
            f"{ma_line}<polyline points='{spark_pts}' fill='none' stroke='#2dd4bf' stroke-width='2'/>{dot}</svg>"
        )
    name = html.escape(str(dd.get("name") or ""))
    return f"""
<div class='sdd-card'>
  <div class='sdd-card-tag'><span>FEATURED ANALYSIS &middot; REFRESHED DAILY</span><span style='color:#34d399;'>&#9679; LIVE</span></div>
  <div class='sdd-fa-head'>
    <span class='sdd-fa-tkr'>{html.escape(dd['ticker'])}</span>
    <span class='sdd-fa-nm'>{name}</span>
    <span class='sdd-fa-px'>{dd['price']:,.2f} {html.escape(dd['currency'])}</span>
  </div>
  <div class='sdd-fa-grid'>
    <div style='text-align:center;'>
      <svg viewBox='0 0 200 120' width='100%'>
        <path d='M15 105 A85 85 0 0 1 185 105' fill='none' stroke='#1f3352' stroke-width='13' stroke-linecap='round'/>
        {arc}
        <text x='100' y='86' text-anchor='middle' fill='#e6edf5' font-size='26' font-weight='700' font-family='monospace'>{score:.0f}</text>
        <text x='100' y='106' text-anchor='middle' fill='#8aa0b8' font-size='10' font-family='monospace'>LONG SCORE / 100</text>
      </svg>
      <div style='font-size:12px;color:#8aa0b8;'>Verdict: <b style='color:{vcolor};'>{verdict}</b></div>
    </div>
    <div>
      <div class='sdd-stat'><div class='k'>INTRINSIC VALUE (DCF, BASE CASE)</div><div class='v'>{iv_txt}</div></div>
      <div class='sdd-stat'><div class='k'>MARGIN OF SAFETY</div><div class='v' style='color:{mos_color};'>{mos_txt}</div></div>
      <div class='sdd-stat'><div class='k'>QUALITY SCORE</div><div class='v'>{dd['quality_score']} / 100</div></div>
    </div>
  </div>
  <div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;'>{pills_html}</div>
  <div style='margin-top:12px;'>{spark_html}</div>
</div>
"""


def _render_footer():
    """Site footer: navigation, contact, and the general-advice disclaimer -
    rendered on every page (called once, after st.navigation runs the page,
    so no page can accidentally skip it)."""
    st.markdown(
        """
<div class='sdd-footer'>
  <div class='sdd-f-cols'>
    <div><h5>StocksDeepDive</h5>
      <a href='/' target='_self'>Home</a>
      <a href='/about' target='_self'>About the author</a>
      <a href='/methodology' target='_self'>How the scores work</a>
      <a href='/research' target='_self'>Rational Compounder Research</a>
    </div>
    <div><h5>Tools</h5>
      <a href='/deep-dive' target='_self'>Stock Deep Dive</a>
      <a href='/comparison' target='_self'>Comparison</a>
      <a href='/scanner' target='_self'>Stock Scanner</a>
    </div>
    <div><h5>Contact</h5>
      <a href='mailto:rationalcompounder@stocksdeepdive.com'>rationalcompounder@stocksdeepdive.com</a>
      <span style='display:block;color:#8aa0b8;margin-top:6px;font-size:13px;'>or the Feedback button on any results page</span>
      <a href='/privacy' target='_self'>Privacy policy</a>
    </div>
  </div>
  <div class='sdd-disclaimer'>
    <b>General information only.</b> StocksDeepDive provides factual information and general
    commentary generated from publicly available data. It does not take your personal objectives,
    financial situation or needs into account and is not financial advice. Scores, signals, entry
    zones and price targets are model outputs, not recommendations. Consider seeking advice from a
    licensed adviser before acting. Data via Yahoo Finance, Google Trends, StockTwits and NewsAPI;
    figures may be delayed or estimated &mdash; estimated values are shown in red throughout the site.
  </div>
</div>
""",
        unsafe_allow_html=True,
    )



def _render_watchlist_row():
    """Signed-in users' saved tickers as one-click chips on the home page -
    the sign-in carrot, and the audience the weekly digest goes to."""
    email = paywall_engine.current_user_email()
    if not email:
        return
    try:
        tickers = watchlist_store.get_watchlist(email)
    except Exception:
        return
    if not tickers:
        return
    st.markdown(
        "<div style='color:#5b7290;font-size:12px;letter-spacing:1.2px;"
        "font-family:ui-monospace,Menlo,monospace;margin:18px 0 2px;'>YOUR WATCHLIST</div>",
        unsafe_allow_html=True,
    )
    cols = st.columns(min(len(tickers), 8) + 2)
    for i, t in enumerate(tickers[:8]):
        if cols[i].button(t, key=f"wl_chip_{t}"):
            _dispatch_search(t)


def _render_header(compact, page_label=None):
    _render_tape()
    # page_label is only passed on the three main service pages (Deep Dive,
    # Comparison, Rational Compounder Analysis) - Home doesn't get a
    # feedback button since there's no service content there to comment on.
    # Passed into render_account_bar as extra_widget so it renders in the
    # same row as Sign out instead of on its own row above the account bar.
    feedback_widget = None
    if page_label and feedback_engine.is_configured():
        feedback_widget = lambda: feedback_engine.render_feedback_widget(
            page_label,
            key_prefix=page_label,
            user_email=paywall_engine.current_user_email(),
        )
    paywall_engine.render_account_bar(extra_widget=feedback_widget)
    st.markdown(
        f"""
        <style>
        .site-title {{
            text-align: center; font-weight: 800;
            font-family: 'Segoe UI', sans-serif; color: #e6edf5;
            font-size: {"20px" if compact else "58px"};
            margin-top: {"2px" if compact else "44px"};
            margin-bottom: {"2px" if compact else "8px"};
        }}
        .site-title .accent {{ color: #2dd4bf; }}
        .site-title-link, .site-title-link:hover, .site-title-link:visited {{
            display: block; text-decoration: none !important; cursor: pointer;
        }}
        .site-title-link:hover .site-title {{ opacity: 0.85; }}
        .site-sub {{
            text-align: center; color: #8aa0b8; font-size: 16px;
            margin-bottom: 28px; font-family: 'Segoe UI', sans-serif;
        }}
        </style>
        <a href="/" target="_self" class="site-title-link">
            <div class="site-title">Stocks<span class="accent">DeepDive</span></div>
        </a>
        """,
        unsafe_allow_html=True,
    )
    if not compact:
        st.markdown(
            '<div class="site-sub">Research any stock in seconds.</div>',
            unsafe_allow_html=True,
        )

    # Narrowed from [1, 2, 1] / [1, 3, 1] (50% / 60% of the page) to roughly
    # match how proportionally narrow Google's own homepage search bar is,
    # rather than spanning half-plus the browser width. Same ratio on every
    # page (compact or not) now, so the box doesn't change width depending
    # on which page you're on.
    _col_ratio = [3, 4, 3]
    _sp1, _mid, _sp2 = st.columns(_col_ratio)
    with _mid:
        with st.form("site_search_form", clear_on_submit=False, border=not compact):
            _search_text = st.text_input(
                "Ticker search",
                placeholder="Input your stock ticker (e.g. CSL.AX, or CSL.AX BHP.AX to compare)",
                label_visibility="collapsed",
                key="site_search",
            )
            _searched = st.form_submit_button(
                "Search", use_container_width=True, type="primary"
            )
        if not compact:
            st.caption(
                "One ticker = Deep Dive. Two or more (comma or space separated) = "
                "side-by-side Comparison. ASX (e.g. CSL.AX) and US (e.g. AAPL) "
                "tickers can be mixed freely."
            )

    # The three page buttons get their OWN, wider row (60% of the page vs
    # the search box's 40%) - three labels, one of them long, don't fit
    # inside the narrow search column now that button text never wraps.
    _bsp1, _bmid, _bsp2 = st.columns([2, 6, 2])
    with _bmid:
        _nav_col1, _nav_col2, _nav_col3 = st.columns(3, gap="small")
        with _nav_col1:
            if st.button(
                "Rational Compounder Analysis",
                use_container_width=True, key="nav_research",
            ):
                st.switch_page(PG_RESEARCH)
        with _nav_col2:
            if st.button(
                "Side-by-side Comparison",
                use_container_width=True, key="nav_comparison",
            ):
                st.switch_page(PG_COMPARISON)
        with _nav_col3:
            if st.button(
                "Stock Scanner",
                use_container_width=True, key="nav_scanner",
            ):
                st.switch_page(PG_SCANNER)

    if _searched:
        _dispatch_search(_search_text)

    _sp_margin = "10px" if compact else "20px"
    st.markdown(f"<div style='margin-bottom:{_sp_margin};'></div>", unsafe_allow_html=True)

def _dd_gauge(value, title, zones, bar_color="#e6edf5", height=260):
    """
    One consistent 0-100 gauge for the Deep Dive tab's per-factor scores
    (Quality / Psychology / Discovery / Trade Setup) - same shape as the
    Long Score gauge above, parameterised so it isn't rebuilt 4x.
    zones: list of (lo, hi, color) tuples covering 0-100.
    """
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=value,
        title={"text": title},
        gauge={
            "axis": {"range": [0, 100]},
            "bar": {"color": bar_color},
            "steps": [{"range": [lo, hi], "color": color} for lo, hi, color in zones],
        },
    ))
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=40, b=10))
    return fig


def _dd_contrib_chart(contributions, title, xaxis_title="Points", height=260):
    """
    One consistent SIGNED horizontal contribution bar chart (green >= 0,
    red < 0) for the Deep Dive tab - same shape as the Long Score
    "what's driving it" chart above, parameterised for reuse. Use this for
    contributions that can genuinely be negative (Long Score, Psychology).
    """
    raw_values = list(contributions.values())
    # Real bug this app hit: Plotly's textposition="outside" silently fails
    # to render a label for a bar whose value is EXACTLY 0 - there's no bar
    # edge to anchor "outside" of. A genuinely-computed 0 (e.g. Fear score
    # is 0 when a stock is sitting right at its own 3-month high - nothing
    # to be fearful of by that formula) then looks like a missing/broken
    # value instead of a real zero. Nudge only the RENDERED bar length by a
    # visually-invisible epsilon so every bar has an anchor point; the text
    # label itself always shows the true, unperturbed value.
    plot_values = [v if v != 0 else 1e-6 for v in raw_values]
    fig = go.Figure(go.Bar(
        x=plot_values,
        y=list(contributions.keys()),
        orientation="h",
        marker_color=["#34d399" if v >= 0 else "#fb7185" for v in raw_values],
        text=[f"{v:+.1f}" for v in raw_values],
        textposition="outside",
    ))
    fig.update_layout(
        title=title, xaxis_title=xaxis_title, showlegend=False, height=height,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


def _dd_gate_chart(contributions, title, xaxis_title="Points", height=260):
    """
    Horizontal bar chart for PASS/FAIL gate weights (Trade Setup) - every
    value is either 0 (gate failed) or its full weight (gate passed), so
    colour is by "did it pass" (value > 0 = green) rather than by sign -
    a plain sign-based chart would show a failed (0-value) gate in the
    same green as a passed one.

    Same zero-value label bug as _dd_contrib_chart above applies here too -
    a FAILED gate (value 0) would silently lose its "FAIL" text - so this
    gets the same tiny-epsilon nudge on the rendered bar only.
    """
    raw_values = list(contributions.values())
    plot_values = [v if v != 0 else 1e-6 for v in raw_values]
    fig = go.Figure(go.Bar(
        x=plot_values,
        y=list(contributions.keys()),
        orientation="h",
        marker_color=["#34d399" if v > 0 else "#fb7185" for v in raw_values],
        text=[("PASS" if v > 0 else "FAIL") for v in raw_values],
        textposition="outside",
    ))
    fig.update_layout(
        title=title, xaxis_title=xaxis_title, showlegend=False, height=height,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


# -----------------------------------
# RATIONAL COMPOUNDER ANALYSIS - reads the pre-built compounder_data.json
# (see build_compounder_data.py - a separate offline step, not run live)
# derived from Andrew's own SMSF research workbook. Every metric shown here
# is one he personally annotated with an explanatory comment in Excel; the
# colour bands on the gauges are the red/amber/green (or 4th "very high"
# blue) ranges spelled out in his own comment text, not invented cutoffs.
# -----------------------------------

_CP_COLOR_FILL = {"red": "#43222e", "amber": "#43371c", "green": "#27584a", "blue": "#1d4356"}
_CP_COLOR_TEXT = {"red": "#fb7185", "amber": "#fbbf24", "green": "#34d399", "blue": "#5ed3f0"}


@st.cache_data
def _load_compounder_data():
    # _cp_data_dir() resolves to the attached Railway Volume when one
    # exists (so a prior admin rebuild's output is still here after a
    # redeploy), otherwise this file's own directory. _cp_seed_from_repo_
    # if_missing() copies the git-tracked copy over on a volume's first
    # ever use, so a freshly-attached empty volume doesn't show as blank.
    build_compounder_data._cp_seed_from_repo_if_missing("compounder_data.json")
    _path = os.path.join(build_compounder_data._cp_data_dir(), "compounder_data.json")
    if not os.path.exists(_path):
        return None
    with open(_path) as _f:
        return json.load(_f)


def _cp_clean_comment(text):
    """Strip the Excel 'threaded comment' boilerplate down to just what
    Andrew actually wrote, joining a 'Comment:' + any 'Reply:' follow-ups
    into one readable block."""
    if not text:
        return ""
    parts = []
    for chunk in text.split("Reply:"):
        chunk = chunk.strip()
        if chunk.startswith("[Threaded comment]"):
            idx = chunk.find("Comment:")
            chunk = chunk[idx + len("Comment:"):] if idx != -1 else ""
        if chunk.strip():
            parts.append(chunk.strip())
    # \$ stops a pair of dollar amounts in a comment from triggering
    # markdown's LaTeX math mode in st.caption.
    return "\n\n".join(parts).replace("$", "\\$")


def _cp_format(value, fmt):
    if value is None:
        return "N/A"
    if fmt == "pct":
        return f"{value * 100:,.1f}%"
    if fmt == "x":
        return f"{value:,.2f}x"
    if fmt == "cur":
        return f"${value:,.0f}" if abs(value) >= 1000 else f"${value:,.2f}"
    return f"{value:,.2f}"


def _cp_band(value, thresholds):
    if value is None or not thresholds:
        return None
    for lo, hi, color, band_label in thresholds:
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return color, band_label
    return None


def _cp_gauge(value, label, fmt, thresholds, height=130):
    breakpoints = sorted(
        {b for t in thresholds for b in (t[0], t[1]) if b is not None}
    )
    lo_bound = breakpoints[0] if breakpoints else 0.0
    hi_bound = breakpoints[-1] if breakpoints else 1.0
    span = (hi_bound - lo_bound) or (abs(hi_bound) or 1.0)
    pad = span * 0.2
    axis_min, axis_max = lo_bound - pad, hi_bound + pad
    if value < axis_min:
        axis_min = value - span * 0.1
    if value > axis_max:
        axis_max = value + span * 0.1

    steps = [
        {
            "range": [lo if lo is not None else axis_min, hi if hi is not None else axis_max],
            "color": _CP_COLOR_FILL.get(color, "#26334a"),
        }
        for lo, hi, color, _ in thresholds
    ]
    number_fmt = {"pct": ".1%", "x": ",.2f", "cur": ",.0f"}.get(fmt, ",.2f")
    number_suffix = "x" if fmt == "x" else ""
    number_prefix = "$" if fmt == "cur" else ""

    fig = go.Figure(go.Indicator(
        mode="number+gauge",
        value=value,
        number={"valueformat": number_fmt, "suffix": number_suffix, "prefix": number_prefix, "font": {"size": 20}},
        domain={"x": [0.32, 1], "y": [0.25, 0.75]},
        gauge={
            "shape": "bullet",
            "axis": {"range": [axis_min, axis_max], "tickfont": {"size": 9}},
            "bar": {"color": "#e6edf5", "thickness": 0.45},
            "steps": steps,
            "threshold": {"line": {"color": "#e6edf5", "width": 2}, "thickness": 0.9, "value": value},
        },
    ))
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=22, b=8),
        title={"text": label, "font": {"size": 13, "color": "#e6edf5"}, "x": 0, "xanchor": "left"},
    )
    return fig


def _cp_price_chart(ticker, price_history):
    """Share price history (Price Calc, ~10y monthly) with Andrew's own
    '10y Average' (Stock Analysis col CF) drawn as a flat reference line -
    this is the "price vs the median I calculated" chart he asked for."""
    entry = price_history.get(ticker)
    if not entry or not entry.get("dates"):
        return None
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=entry["dates"], y=entry["prices"], mode="lines", name="Price",
        line=dict(color="#2dd4bf", width=2),
    ))
    if entry.get("avg_10y") is not None:
        fig.add_hline(
            y=entry["avg_10y"], line_dash="dash", line_color="#e6edf5",
            annotation_text=f"10y Average ${entry['avg_10y']:,.2f}",
            annotation_position="top left", annotation_font_size=11,
        )
    fig.update_layout(
        title="Share Price vs 10-Year Average", height=320, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="Price",
    )
    return fig


def _cp_share_price_growth_chart(ticker, share_price_growth):
    """Share Price Growth by year - Andrew's own workbook figure (avg price
    this year vs avg price last year), same green/red convention as EPS
    Growth by Year on the Earnings Trends tab."""
    entry = share_price_growth.get(ticker)
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    colors = ["#34d399" if v >= 0 else "#fb7185" for v in values]
    fig = go.Figure(go.Bar(
        x=years, y=values, marker_color=colors,
        text=[f"{v * 100:+.1f}%" for v in values], textposition="outside",
    ))
    fig.update_layout(
        title="Share Price Growth by Year", height=320, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="Share Price Growth",
        yaxis_tickformat=".0%", xaxis_type="category",
    )
    return fig


def _cp_value_created_chart(ticker, value_created):
    """Andrew's own retained-earnings 'Value Created' test (Dividend Ratio
    sheet) at 2Y/5Y/10Y/TTM horizons - for every $ of earnings retained,
    how much market value did that create."""
    entry = value_created.get(ticker)
    if not entry:
        return None
    order = [h for h in ["2Y", "5Y", "10Y", "TTM"] if h in entry]
    if not order:
        return None
    retained = [entry[h]["retained_earnings"] for h in order]
    created = [entry[h]["value_created"] for h in order]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=order, y=retained, name="Retained Earnings Per Share", marker_color="#94a3b8"))
    fig.add_trace(go.Bar(x=order, y=created, name="Market Value Created for every dollar retained", marker_color="#2dd4bf"))
    fig.update_layout(
        barmode="group", title="Value Created per $ Retained, by horizon",
        height=320, margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", y=-0.2),
    )
    return fig


def _cp_iv_bv_series_chart(ticker, iv_bv_series, thresholds):
    """IV/BV for every year Andrew has modelled (X=year, Y=IV/BV) - colour
    per year uses the same red/amber/green bands as the IV/BV gauge did."""
    entry = iv_bv_series.get(ticker)
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    ratios = list(reversed(entry["ratios"]))
    colors = []
    for r in ratios:
        band = _cp_band(r, thresholds) if thresholds else None
        colors.append(_CP_COLOR_FILL.get(band[0], "#2dd4bf") if band else "#2dd4bf")
    fig = go.Figure(go.Bar(
        x=years, y=ratios, marker_color=colors,
        text=[f"{v:.2f}x" for v in ratios], textposition="outside",
    ))
    avg_ratio = sum(ratios) / len(ratios)
    fig.add_hline(
        y=avg_ratio, line_dash="dash", line_color="#e6edf5",
        annotation_text=f"Average {avg_ratio:.2f}x",
        annotation_position="top left", annotation_font_size=11,
    )
    fig.update_layout(
        title="IV/BV by Year Modelled", height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="IV/BV",
        xaxis_type="category",
    )
    return fig


def _cp_year_bar_chart(ticker, series, key, title, yaxis_title, fmt="num", color="#2dd4bf"):
    """Generic X=year bar chart for a year-series pulled straight from the
    workbook (EPS, PE Ratio, ...) - one consistent shape reused per metric."""
    entry = series.get(ticker, {}).get(key)
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    text = [_cp_format(v, fmt) for v in values]
    fig = go.Figure(go.Bar(x=years, y=values, marker_color=color, text=text, textposition="outside"))
    fig.update_layout(
        title=title, height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title=yaxis_title,
        xaxis_type="category",
    )
    return fig


def _cp_eps_growth_chart(ticker, series):
    """EPS Growth by year - diverging red/green bars (negative growth
    years read as red, same convention as the rest of the app)."""
    entry = series.get(ticker, {}).get("eps_growth")
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    colors = ["#34d399" if v >= 0 else "#fb7185" for v in values]
    fig = go.Figure(go.Bar(
        x=years, y=values, marker_color=colors,
        text=[f"{v * 100:+.1f}%" for v in values], textposition="outside",
    ))
    fig.update_layout(
        title="EPS Growth by Year", height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="EPS Growth", yaxis_tickformat=".0%",
        xaxis_type="category",
    )
    return fig


def _cp_pe_ratio_chart(ticker, series, pe_ratio_refs):
    """PE Ratio by Year, plus two reference lines Andrew asked for so they
    read at a glance against the year-by-year bars: 'PE ratio (Current
    price to 3y average EPS)' (his AV column) and the plain 'PE Ratio
    Average' (AU). Drawn as a full-width shape (edge to edge, not just from
    the first bar's centre to the last bar's centre) with its value written
    directly next to it via a plain xref="paper" annotation -- NOT
    fig.add_hline()'s own annotation_* kwargs, which silently clip the text
    when pushed out past the plot area with an xshift; a manually-added
    add_annotation() call doesn't have that problem. Whichever of the two
    lines sits lower gets its label pushed further down (and the higher one
    further up) so the two labels don't collide when the values are close."""
    entry = series.get(ticker, {}).get("pe_ratio")
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    fig = go.Figure(go.Bar(
        x=years, y=values, marker_color="#8aa0b8",
        text=[_cp_format(v, "x") for v in values], textposition="outside",
        name="PE Ratio", showlegend=False,
    ))
    refs = pe_ratio_refs.get(ticker, {})
    avg_3y = refs.get("avg_3y")
    overall_avg = refs.get("overall_avg")
    # Label placed just past the right edge of the plot (not above the last
    # bar) so it never collides with that bar's own outside value label.
    # Whichever line sits lower gets its label anchored to hang further
    # below the line, and the higher one anchored to sit further above it,
    # so the two labels open away from each other instead of colliding when
    # the two values are close together.
    if avg_3y is not None and overall_avg is not None and avg_3y <= overall_avg:
        avg_3y_yanchor, overall_avg_yanchor = "top", "bottom"
    else:
        avg_3y_yanchor, overall_avg_yanchor = "bottom", "top"

    def _cp_pe_ref_line(value, color, dash, label, yanchor):
        if value is None:
            return
        fig.add_shape(
            type="line", xref="x", x0=-0.5, x1=len(years) - 0.5, yref="y",
            y0=value, y1=value, line=dict(color=color, dash=dash, width=2),
        )
        fig.add_annotation(
            xref="paper", x=1.0, xanchor="left", xshift=10,
            yref="y", y=value, yanchor=yanchor,
            text=f"{label}: {value:.2f}x", showarrow=False,
            font=dict(color=color, size=11), align="left",
        )

    _cp_pe_ref_line(avg_3y, "#fb923c", "dash", "3y EPS avg", avg_3y_yanchor)
    _cp_pe_ref_line(overall_avg, "#60a5fa", "dot", "Overall avg", overall_avg_yanchor)
    fig.update_layout(
        title="PE Ratio by Year", height=340, showlegend=False,
        margin=dict(l=10, r=100, t=40, b=10), yaxis_title="PE Ratio",
        xaxis_type="category",
    )
    return fig


_CP_WACC_ROIC_PERIOD_ORDER = ["TTM", "2025", "2021", "2016"]


def _cp_wacc_roic_chart(ticker, wacc_roic_series):
    """WACC vs ROIC per period, as a grouped bar - lets you see at a glance
    whether the business is earning more on invested capital than its cost
    of capital (ROIC > WACC = value creation).

    WACC and ROIC don't always have data for the same periods (a #VALUE!
    cell in one series but not the other, e.g. CSL is missing WACC for
    TTM/2025) - align both series to a fixed period order and leave gaps
    (None) rather than assuming the two lists line up 1:1."""
    entry = wacc_roic_series.get(ticker)
    if not entry or not entry.get("wacc") or not entry.get("roic"):
        return None
    wacc_by_period = dict(zip(entry["wacc"]["periods"], entry["wacc"]["values"]))
    roic_by_period = dict(zip(entry["roic"]["periods"], entry["roic"]["values"]))
    periods = [p for p in _CP_WACC_ROIC_PERIOD_ORDER if p in wacc_by_period or p in roic_by_period]
    if not periods:
        return None
    wacc_vals = [wacc_by_period.get(p) for p in periods]
    roic_vals = [roic_by_period.get(p) for p in periods]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=periods, y=wacc_vals, name="WACC", marker_color="#94a3b8",
        text=[f"{v * 100:.1f}%" if v is not None else "" for v in wacc_vals], textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=periods, y=roic_vals, name="ROIC", marker_color="#2dd4bf",
        text=[f"{v * 100:.1f}%" if v is not None else "" for v in roic_vals], textposition="outside",
    ))
    fig.update_layout(
        barmode="group", title="WACC vs ROIC by Year", height=320,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="Rate", yaxis_tickformat=".0%",
        xaxis_type="category", legend=dict(orientation="h", y=-0.2),
    )
    return fig


# Fair Value bar order/labels/colours - shared between the chart and the
# "inputs under each bar" row so the two line up. "Rational Compounder
# Method 10y" was "Equity Method 10y" until Andrew asked for the rename.
_CP_VALUATION_METHOD_ORDER = [
    ("price", "Current Price", "#2dd4bf"),
    ("pe_forward", "PE Forward", "#94a3b8"),
    ("pe_trailing", "PE Trailing", "#8aa0b8"),
    ("dcf", "DCF (10y FCF)", "#34d399"),
    ("equity_10y", "Rational Compounder Method 10y", "#4cc38a"),
]


def _cp_valuation_methods_chart(ticker, valuation_methods):
    """The 4 intrinsic-value methods Andrew asked for (PE Trailing, PE
    Forward, DCF, Rational Compounder Method 10y) vs current price -
    replaces the previous general Fair Value metrics grid entirely, per his
    request. Returns (figure, [(key, label), ...] actually plotted) so the
    caller can line up the "inputs used" row underneath each bar."""
    entry = valuation_methods.get(ticker)
    if not entry:
        return None, []
    labels, values, colors, used = [], [], [], []
    for key, label, color in _CP_VALUATION_METHOD_ORDER:
        if entry.get(key) is not None:
            labels.append(label)
            values.append(entry[key])
            colors.append(color)
            used.append((key, label))
    if not values:
        return None, []
    fig = go.Figure(go.Bar(
        x=labels, y=values, marker_color=colors,
        text=[f"${v:,.2f}" for v in values], textposition="outside",
    ))
    fig.update_layout(
        title="Intrinsic Value by Method vs Current Price", height=340,
        showlegend=False, margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig, used


def _cp_render_valuation_inputs(ticker, used, valuation_inputs, valuation_methods):
    """The key inputs behind each bar, shown directly underneath it (one
    Streamlit column per bar, same left-to-right order as the chart)."""
    entry = valuation_inputs.get(ticker, {})
    method_values = valuation_methods.get(ticker, {})
    cols = st.columns(len(used))
    for col, (key, label) in zip(cols, used):
        with col:
            st.markdown(f"<div style='text-align:center;font-size:12px;font-weight:600;color:#aebfd4;'>{label}</div>", unsafe_allow_html=True)
            # "Current Price" is the actual market price, not a valuation
            # estimate - skip the "Intrinsic Value" line for that one bar.
            lines = [] if key == "price" else [f"Intrinsic Value: {_cp_format(method_values.get(key), 'cur')}"]
            for item in entry.get(key, []):
                lines.append(f"{item['label']}: {_cp_format(item['value'], item['format'])}")
            if lines:
                st.markdown(
                    "<div style='text-align:center;font-size:11.5px;color:#8aa0b8;line-height:1.6;'>"
                    + "<br>".join(lines) + "</div>",
                    unsafe_allow_html=True,
                )


_CP_HML_COLOR = {
    "good_high": {"high": "green", "medium": "amber", "low": "red"},
    "good_low": {"low": "green", "medium": "amber", "high": "red"},
    "neutral": {},
}


def _cp_pill(text, color_key=None):
    c = _CP_COLOR_TEXT.get(color_key, "#9db1c7")
    return (
        f"<span style='display:inline-block;margin:2px 6px 2px 0;padding:3px 10px;"
        f"border-radius:12px;font-size:12.5px;font-weight:600;"
        f"background:{c}22;color:{c};'>{text}</span>"
    )


def _cp_render_hml_ratings(ratings):
    st.markdown("##### Ratings (called directly from your Low/Medium/High cells)")
    html_parts = []
    for r in ratings:
        color_key = _CP_HML_COLOR.get(r["polarity"], {}).get(r["value"].strip().lower())
        html_parts.append(
            f"<div style='margin-bottom:6px;'><span style='font-size:13px;color:#aebfd4;'>"
            f"{_md_safe(r['label'])}: </span>{_cp_pill(_md_safe(r['value'].strip()), color_key)}</div>"
        )
    # two columns of pills so a long ratings list doesn't run the whole page
    half = (len(html_parts) + 1) // 2
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("".join(html_parts[:half]), unsafe_allow_html=True)
    with col2:
        st.markdown("".join(html_parts[half:]), unsafe_allow_html=True)


def _md_safe(text):
    """Workbook-sourced free text made safe for st.markdown HTML blocks:
    HTML-escaped (a stray < or & can't break the pill markup) and with $
    converted to its HTML entity - two $ signs in markdown otherwise
    trigger LaTeX math rendering, which mangles everything between them
    (seen with 'A$750 million' in a CSL buyback answer)."""
    return html.escape(str(text)).replace("$", "&#36;")


def _cp_render_yesno_checks(checks):
    st.markdown("##### Quick checks")
    _pills_html = "".join(
        f"<span style='display:inline-block;margin:2px 8px 8px 0;padding:4px 10px;"
        f"border-radius:8px;font-size:12.5px;background:#1f3352;color:#aebfd4;'>"
        f"{_md_safe(c['label'])}: <b>{_md_safe(c['value'].strip())}</b></span>"
        for c in checks
    )
    st.markdown(_pills_html, unsafe_allow_html=True)


def _cp_render_text_groups(groups):
    st.markdown("##### Your notes, grouped")
    for g in groups:
        with st.expander(g["title"], expanded=False):
            for item in g["items"]:
                st.markdown(f"**{_md_safe(item['label'])}**")
                # st.caption is a plain-markdown context (no HTML pills),
                # so only the $-math trigger needs neutralising here.
                st.caption(str(item["text"]).replace("$", "\\$"))


_ADMIN_REFRESH_KEY_ENV = "ADMIN_REFRESH_KEY"


def _render_compounder_admin_panel():
    """
    Admin-only control to rebuild compounder_data.json from an updated SMSF
    research workbook, without needing a terminal. This app has no
    accounts/login system at all, so a full "admin user" concept doesn't
    exist yet -- gating this behind a single shared secret
    (ADMIN_REFRESH_KEY, set as an environment variable on this Railway
    service, never committed to the repo) is the lightest control that
    still keeps this away from ordinary visitors, who have no reason to
    ever see or use it. If ADMIN_REFRESH_KEY isn't set, this section stays
    collapsed and inert -- nothing is exposed by default.

    Important: rebuilding writes compounder_data.json to
    build_compounder_data._cp_data_dir() -- the attached Railway Volume if
    one exists, in which case that write already IS permanent and survives
    the next redeploy/restart with no further action needed. Without a
    volume attached, it falls back to this process's local disk, which
    shows the refreshed data on this page immediately (great for
    previewing) but does NOT survive a redeploy on its own -- the download
    buttons below let you grab the freshly-built files and commit them to
    the repo, exactly the workflow build_compounder_data.py's own
    docstring documents.
    """
    admin_key = os.environ.get(_ADMIN_REFRESH_KEY_ENV, "").strip()
    if not admin_key:
        return

    # A small "☰" icon tucked in the top-left corner, not a full-width
    # labelled bar -- st.popover() with an icon-only label is Streamlit's
    # native equivalent of a menu button: closed by default, no text
    # revealing this exists to an ordinary visitor, opens a small panel on
    # click. Narrow right column is just spacing so the icon hugs the left
    # edge. The scoped CSS below strips the default button border/background
    # off just this one trigger (targeted via the container's key, so it
    # can't leak onto any other button on the page) so only the glyph shows.
    corner, _ = st.columns([1, 20])
    with corner:
        # The style tag renders as an invisible zero-height element here --
        # it just needs to reach the page's <head>/DOM once, doesn't need to
        # be nested inside the same container as the button it targets.
        st.markdown(
            """
            <style>
            div.st-key-cp_admin_trigger_wrap button {
                border: none !important;
                background: transparent !important;
                box-shadow: none !important;
                padding: 0.1rem 0.3rem !important;
                min-height: 0 !important;
            }
            div.st-key-cp_admin_trigger_wrap button:hover,
            div.st-key-cp_admin_trigger_wrap button:focus,
            div.st-key-cp_admin_trigger_wrap button:active {
                border: none !important;
                background: transparent !important;
                box-shadow: none !important;
                color: inherit !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        # key= on the container adds a "st-key-<key>" CSS class to its own
        # wrapping div, so the button inside (the popover trigger) is a
        # genuine DOM descendant the selector above can reach -- combining
        # both `with`s on one line keeps everything below at its existing
        # indentation.
        with st.container(key="cp_admin_trigger_wrap"), st.popover("☰"):
            entered = st.text_input("Admin key", type="password", key="cp_admin_key")
            if not entered:
                return
            if entered != admin_key:
                st.error("Incorrect key.")
                return

            st.success("Admin key accepted.")
            uploaded = st.file_uploader(
                "Upload the updated SMSF research workbook (.xlsx)",
                type=["xlsx"], key="cp_admin_upload",
            )
            st.checkbox(
                "Archive the current dataset before replacing it "
                "(keeps it viewable under 'Data snapshot' with its "
                "timestamp - untick to discard it instead)",
                value=True, key="cp_admin_archive",
            )
            if uploaded and st.button(
                "Rebuild Rational Compounder data", type="primary", key="cp_admin_rebuild",
            ):
                with st.spinner("Rebuilding from the uploaded workbook..."):
                    data_dir = build_compounder_data._cp_data_dir()
                    using_volume = os.path.abspath(data_dir) != os.path.abspath(
                        os.path.dirname(__file__)
                    )
                    tmp_path = os.path.join(os.path.dirname(__file__), "_admin_upload_tmp.xlsx")
                    out_path = os.path.join(data_dir, "compounder_data.json")
                    corrections_path = build_compounder_data.CP_CORRECTIONS_PATH
                    try:
                        with open(tmp_path, "wb") as f:
                            f.write(uploaded.getbuffer())
                        fresh_data = build_compounder_data.build(tmp_path)

                        # ARCHIVE the previous dataset (timestamped, kept
                        # on the volume, viewable via the Research page's
                        # snapshot picker), then REPLACE the live data
                        # with this build outright. The old behaviour
                        # merged old cells into any blanks in the new
                        # build - which meant a deliberately DELETED cell
                        # (e.g. wrong-company text removed from a row)
                        # kept resurrecting. Now: the workbook you upload
                        # is exactly what the site shows, and history
                        # lives in the archive instead of leaking into
                        # the present.
                        if st.session_state.get("cp_admin_archive", True):
                            build_compounder_data.archive_current_snapshot()
                        new_data = fresh_data

                        with open(out_path, "w") as f:
                            json.dump(new_data, f, indent=1)
                    except Exception as exc:
                        st.error(f"Rebuild failed: {exc}")
                        return
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

                # Bust the cache so the SAME script run (below, when
                # page_research() calls _load_compounder_data() again)
                # serves the freshly-built data immediately -- no restart
                # needed.
                _load_compounder_data.clear()
                carried_over = len(new_data.get("tickers", {})) - len(fresh_data.get("tickers", {}))
                carried_over_note = (
                    f" ({carried_over} of those carried over from the previous data, not in "
                    f"this upload)" if carried_over > 0 else ""
                )
                if using_volume:
                    st.success(
                        f"Rebuilt: {len(new_data.get('tickers', {}))} tickers now in the data"
                        f"{carried_over_note}. Now showing below, and saved to the attached "
                        "Volume -- this is permanent and will still be here after the next "
                        "redeploy. Nothing further to do."
                    )
                else:
                    st.success(
                        f"Rebuilt: {len(new_data.get('tickers', {}))} tickers now in the data"
                        f"{carried_over_note}. Now showing below. This is live on THIS running "
                        "instance only -- download BOTH files below and commit them to the repo "
                        "to make the update permanent (survives the next redeploy), or attach a "
                        "Railway Volume to this service so future rebuilds persist on their own."
                    )
                for w in fresh_data.get("build_warnings", []):
                    st.warning(w)
                download_label_suffix = "for your own records" if using_volume else "to commit"
                st.download_button(
                    f"Download compounder_data.json {download_label_suffix}",
                    data=json.dumps(new_data, indent=1),
                    file_name="compounder_data.json",
                    mime="application/json",
                    key="cp_admin_download",
                )
                if os.path.exists(corrections_path):
                    with open(corrections_path) as f:
                        corrections_text = f.read()
                    st.download_button(
                        f"Download company_potential_corrections.json {download_label_suffix}",
                        data=corrections_text,
                        file_name="company_potential_corrections.json",
                        mime="application/json",
                        key="cp_admin_download_corrections",
                        help=(
                            "Only needed if the grammar check ran (ANTHROPIC_API_KEY set) - "
                            "this is its cache of already-checked text. Skipping this "
                            + ("download" if using_volume else "commit")
                            + " doesn't lose any data on its own"
                            + (" (it's already saved to the Volume)" if using_volume else "")
                            + "; it just means already-correct text gets re-checked (a small "
                            "extra API cost) on the next rebuild instead of being remembered "
                            "for free."
                        ),
                    )


def _render_last_updated(generated_at):
    """'Last updated on ...' badge, top-right, above the Stock/Section
    pickers so it's visible no matter which section a visitor has open --
    this data is a workbook snapshot from whenever it was last rebuilt, not
    live, so it matters that this is easy to spot rather than buried in an
    admin-only view."""
    if not generated_at:
        return
    try:
        dt = datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        return
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    label = dt.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    _, corner = st.columns([3, 1])
    with corner:
        st.markdown(
            f'<div style="text-align:right; color:#8aa0b8; font-size:13px; '
            f'margin-bottom:6px;">Last updated on {label}</div>',
            unsafe_allow_html=True,
        )


def page_research():
    _render_header(compact=True, page_label="Rational Compounder Analysis")

    _render_compounder_admin_panel()

    data = _load_compounder_data()

    # Data snapshot picker: the author starts a fresh research workbook
    # every 6-10 months; every rebuild archives the previous dataset with
    # its timestamp, and any of them can be viewed here. Only shown when
    # archives exist.
    _snapshots = build_compounder_data.list_archived_snapshots()
    if _snapshots:
        _snap_labels = ["Current (latest rebuild)"] + [
            f"Archived - {s['label']}" for s in _snapshots
        ]
        _snap_pick = st.selectbox(
            "Data snapshot", _snap_labels, key="cp_snapshot_pick",
        )
        if _snap_pick != "Current (latest rebuild)":
            _snap = _snapshots[_snap_labels.index(_snap_pick) - 1]
            _snap_data = build_compounder_data.load_snapshot(_snap["path"])
            if _snap_data:
                data = _snap_data
                st.warning(
                    f"You're viewing an ARCHIVED snapshot ({_snap['label']}). "
                    "Numbers reflect the research workbook as of that date - "
                    "switch back to 'Current' for the latest data."
                )

    if not data or not data.get("tickers"):
        st.info(
            "Rational Compounder Analysis - the research data is being "
            "prepared. Check back shortly."
        )
        return

    section_order = [
        "Fundamentals", "Value vs Book", "Dividends", "Earnings Trends",
        "Cost of Capital", "Fair Value", "Company Potential",
    ]
    section_order = [s for s in section_order if s in data["sections"]]

    # This data is a snapshot from whenever the workbook was last uploaded
    # and rebuilt, not live -- shown once here (above the Stock/Section
    # pickers, not inside the per-section branches below) so it's visible
    # no matter which section you pick, not just one of them.
    _render_last_updated(data.get("generated_at"))

    st.caption(
        "Hand-built research, not a screen: every chart, threshold and "
        "colour band on this page comes straight from the author's own "
        "research workbook. Pick a stock, then a section."
    )

    st.caption(
        f"{len(data['tickers'])} companies covered in depth today - new "
        "names are added as each one's research completes. Want a stock "
        "prioritised? Say so via the Feedback button above."
    )

    tickers = sorted(data["tickers"].keys())

    def _ticker_label(t):
        industry = data["tickers"][t].get("industry")
        return f"{t} - {industry}" if industry else t

    # Narrow columns sized just enough for these two dropdowns, followed by
    # a wide empty spacer column -- keeps both boxes compact and bunched on
    # the left instead of stretching one to each half of the page.
    st.markdown(
        """
        <style>
        div.st-key-cp_pick_row div[data-testid="stSelectbox"] {
            max-width: 260px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="cp_pick_row"):
        pick_col1, pick_col2, _ = st.columns([1, 1, 3])
        with pick_col1:
            ticker = st.selectbox(
                "Stock", tickers, format_func=_ticker_label, key="cp_ticker"
            )
        with pick_col2:
            section_label = st.selectbox("Section", section_order, key="cp_section")

    st.markdown(f"### {ticker} - {section_label}")

    section = data["sections"][section_label]
    metrics = section["metrics"]

    if section_label == "Company Potential":
        if not paywall_engine.render_gate(
            "Company Potential - your own research notes",
            teaser=(
                "The author's Low/Medium/High ratings, quick checks, and "
                "full written analysis for every covered company."
            ),
            key_prefix=f"cp_potential_{ticker}",
        ):
            return
        ratings = section.get("hml_ratings", {}).get(ticker, [])
        checks = section.get("yesno_checks", {}).get(ticker, [])
        groups = section.get("text_groups", {}).get(ticker, [])
        if not ratings and not checks and not groups:
            st.warning(f"No Company Potential notes yet for {ticker}.")
            return
        st.caption(
            "The author's Low/Medium/High calls on management, moat, risk "
            "and more - shown exactly as researched - plus the full written "
            "analysis, grouped by theme."
        )
        if ratings:
            _cp_render_hml_ratings(ratings)
        if checks:
            _cp_render_yesno_checks(checks)
        if groups:
            _cp_render_text_groups(groups)
        return

    # Trend/comparison charts (a different shape than the single-value
    # gauges below) - price history, IV vs BV, and the retained-earnings
    # "value created" test, each per Andrew's own workbook figures.
    share_price_growth_fig = None
    if section_label == "Fundamentals":
        fig = _cp_price_chart(ticker, section.get("price_history", {}))
        if fig:
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        # Share Price Growth chart goes after Other Metrics below - render
        # it there, just building the figure now while we have the data.
        share_price_growth_fig = _cp_share_price_growth_chart(ticker, section.get("share_price_growth", {}))
    elif section_label == "Value vs Book":
        # The IV vs BV vs Price 3-bar comparison was dropped per Andrew's
        # request - the year-by-year chart below (with its new average
        # line) carries this section on its own now.
        # The IV/BV ratio column now lives in that chart instead of its own
        # gauge - grab its thresholds for the series chart's colouring,
        # then drop it from the metrics grid so it isn't shown a second time.
        ap_metric = next((m for m in metrics if m["key"] == "AP"), None)
        fig2 = _cp_iv_bv_series_chart(
            ticker, section.get("iv_bv_series", {}),
            ap_metric["thresholds"] if ap_metric else None,
        )
        if fig2:
            st.plotly_chart(fig2, use_container_width=True, config={"displayModeBar": False})
        metrics = [m for m in metrics if m["key"] != "AP"]
    elif section_label == "Dividends":
        fig = _cp_value_created_chart(ticker, section.get("value_created", {}))
        if fig:
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    elif section_label == "Earnings Trends":
        series = section.get("series", {})
        fig1 = _cp_year_bar_chart(ticker, series, "eps", "EPS by Year", "EPS", fmt="cur")
        fig2 = _cp_eps_growth_chart(ticker, series)
        fig3 = _cp_pe_ratio_chart(ticker, series, section.get("pe_ratio_refs", {}))
        chart_cols = st.columns(3)
        for col, fig in zip(chart_cols, [fig1, fig2, fig3]):
            with col:
                if fig:
                    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    elif section_label == "Cost of Capital":
        fig = _cp_wacc_roic_chart(ticker, section.get("wacc_roic_series", {}))
        if fig:
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    elif section_label == "Fair Value":
        if not paywall_engine.render_gate(
            "Fair Value - full valuation methods breakdown",
            teaser=(
                "Four independent valuation methods side by side, with the "
                "exact inputs behind each one."
            ),
            key_prefix=f"cp_fairvalue_{ticker}",
        ):
            return
        fig, used = _cp_valuation_methods_chart(ticker, section.get("valuation_methods", {}))
        if fig:
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            _cp_render_valuation_inputs(
                ticker, used, section.get("valuation_inputs", {}), section.get("valuation_methods", {})
            )
        else:
            st.warning(f"No valuation data yet for {ticker}.")
        # Andrew asked for ONLY these four methods here - the previous
        # general Fair Value metrics grid (Factor of Safety, forecast EPS,
        # discount rate, etc.) is intentionally not shown any more.
        return

    colored = [m for m in metrics if m.get("thresholds") and m["values"].get(ticker) is not None]
    plain = [m for m in metrics if not (m.get("thresholds") and m["values"].get(ticker) is not None)]

    if colored:
        st.markdown("##### Colour-coded (against your own thresholds)")
        cols = st.columns(3)
        for i, m in enumerate(colored):
            value = m["values"][ticker]
            with cols[i % 3]:
                st.plotly_chart(
                    _cp_gauge(value, m["label"], m["format"], m["thresholds"]),
                    use_container_width=True,
                    config={"displayModeBar": False},
                )
                band = _cp_band(value, m["thresholds"])
                if band:
                    color, band_label = band
                    st.markdown(
                        f"<div style='margin-top:-14px; margin-bottom:8px; "
                        f"font-size:12px; color:{_CP_COLOR_TEXT.get(color, '#aebfd4')};'>"
                        f"{band_label}</div>",
                        unsafe_allow_html=True,
                    )
                with st.expander("What this measures", expanded=False):
                    st.caption(_cp_clean_comment(m["comment"]))

    if plain:
        st.markdown("##### Other metrics")
        cols = st.columns(4)
        for i, m in enumerate(plain):
            value = m["values"].get(ticker)
            with cols[i % 4]:
                st.metric(m["label"], _cp_format(value, m["format"]))
                with st.expander("What this measures", expanded=False):
                    st.caption(_cp_clean_comment(m["comment"]))

    if not colored and not plain:
        st.warning(f"No data yet for {ticker} in {section_label}.")

    if share_price_growth_fig:
        st.plotly_chart(share_price_growth_fig, use_container_width=True, config={"displayModeBar": False})


def page_home():
    # LAYOUT FIRST, DATA SECOND. Every static element (hero, search, chips,
    # feature cards, steps, coverage, CTA) renders immediately; the three
    # remote-data elements (tape, featured analysis, mood strip) get empty
    # containers up front and are filled at the END of the run from one
    # concurrent, hard-budgeted fetch. A cold cache or a slow upstream
    # source delays those three boxes only - never the page.
    _day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _feat_ticker = _FEATURED_ROTATION[
        datetime.now(timezone.utc).timetuple().tm_yday % len(_FEATURED_ROTATION)
    ]

    _tape_box = st.container()
    paywall_engine.render_account_bar()

    # top row: logo + free-during-launch badge
    st.markdown(
        """
<div class='sdd-navrow'>
  <span class='sdd-logo'>Stocks<span class='accent'>DeepDive</span></span>
  <span class='sdd-badge-free'>FREE DURING LAUNCH</span>
</div>
""",
        unsafe_allow_html=True,
    )

    hero_l, hero_r = st.columns([11, 10], gap="large")
    with hero_l:
        st.markdown(
            """
<div class='sdd-h1'>Know what a stock is <em>worth</em> &mdash; and whether now is a sane entry.</div>
<div class='sdd-sub'>One score that combines <b>value, quality, crowd psychology and market
attention</b> &mdash; computed live for any ASX or US stock. No noise, no hidden assumptions:
every estimated number is flagged.</div>
""",
            unsafe_allow_html=True,
        )
        with st.form("home_search_form", clear_on_submit=False, border=False):
            _sc1, _sc2 = st.columns([4, 1])
            with _sc1:
                _home_text = st.text_input(
                    "Ticker search",
                    placeholder="CSL.AX  ·  or two tickers to compare: CSL.AX BHP.AX",
                    label_visibility="collapsed", key="home_search",
                )
            with _sc2:
                _home_go = st.form_submit_button("Analyze", use_container_width=True, type="primary")
        _render_example_chips("home")
        st.markdown(
            "<div style='color:#5b7290;font-size:12.5px;margin-top:6px;'>One ticker = full Deep "
            "Dive &middot; Two or more = side-by-side Comparison &middot; ASX + US mixed freely</div>",
            unsafe_allow_html=True,
        )
        if _home_go:
            _dispatch_search(_home_text)

    _feat_box = hero_r.container()

    # ---- watchlist row (signed-in users) ----
    _render_watchlist_row()

    _mood_box = st.container()

    # ---- feature cards ----
    st.markdown(
        """
<div class='sdd-kicker'>THE TOOLKIT</div>
<div class='sdd-h2'>Four ways in. One consistent model.</div>
<div class='sdd-secsub'>Every tool runs the same engine &mdash; the same DCF, the same quality
tests, the same psychology read &mdash; so the numbers always agree with each other.</div>
<div class='sdd-cards4'>
  <a class='sdd-feat' href='/deep-dive' target='_self'>
    <div class='ic'>&#128269;</div><h3>Stock Deep Dive</h3>
    <p>The full picture for one ticker: intrinsic value vs price, what drives the Long Score,
    crowd psychology, and a technical entry zone with stop &amp; targets.</p>
  </a>
  <a class='sdd-feat' href='/comparison' target='_self'>
    <div class='ic'>&#9878;&#65039;</div><h3>Side-by-side Comparison</h3>
    <p>Two or more tickers lined up on identical criteria &mdash; valuation, quality, sentiment,
    trend, trade setup &mdash; as colour-coded bars and verdict pills.</p>
  </a>
  <a class='sdd-feat' href='/scanner' target='_self'>
    <div class='ic'>&#128225;</div><h3>Stock Scanner</h3>
    <p>Rank a whole index &mdash; ASX 200, S&amp;P 500 and more &mdash; by Long Score, with an
    optional sector filter. Find what to look at, not just check what you already own.</p>
  </a>
  <a class='sdd-feat' href='/research' target='_self'>
    <div class='ic'>&#128218;</div><h3>Rational Compounder Research</h3>
    <p>Hand-built, Buffett/Munger-style research on selected compounders &mdash; a decade of
    earnings, four fair-value methods, and written judgment on every business.</p>
  </a>
</div>
""",
        unsafe_allow_html=True,
    )

    # ---- how it works ----
    st.markdown(
        """
<div class='sdd-kicker' style='margin-top:40px;'>HOW IT WORKS</div>
<div class='sdd-h2'>Search. Score. Decide.</div>
<div class='sdd-steps'>
  <div class='sdd-step'><div class='n'>01</div><h4>Type any ticker</h4>
    <p>ASX (CSL.AX) or US (AAPL). Live data is pulled on the spot &mdash; prices, cash flows,
    news, search trends, social chatter.</p></div>
  <div class='sdd-step'><div class='n'>02</div><h4>Get one honest score</h4>
    <p>The Long Score blends business quality, margin of safety, crowd psychology and market
    attention &mdash; the same value-investing maths every time, with every input shown.</p></div>
  <div class='sdd-step'><div class='n'>03</div><h4>See value AND timing</h4>
    <p>Two separate verdicts, never blurred: is this a good business to <em>own</em>, and is
    right now a sane <em>entry</em>? A great company can still be a bad buy today.</p></div>
</div>
<div class='sdd-honesty'><b>The red-flag rule:</b> whenever a number rests on a default or
average because real data wasn't available, it's shown in red. An estimate is never dressed up
as a fact &mdash; you always know which numbers are computed and which are assumed.</div>
""",
        unsafe_allow_html=True,
    )

    # ---- compounder coverage ----
    _cov_data = _load_compounder_data()
    if _cov_data and _cov_data.get("tickers"):
        _cov_cards = []
        for _t in sorted(_cov_data["tickers"].keys()):
            _ind = html.escape(str(_cov_data["tickers"][_t].get("industry") or ""))
            _nsec = sum(1 for _s in _cov_data.get("sections", {}).values()
                        if any(_m["values"].get(_t) is not None for _m in _s.get("metrics", [])))
            _cov_cards.append(
                f"<div class='sdd-cov'><div class='tkr'>{html.escape(_t)}</div>"
                f"<div class='ind'>{_ind}</div>"
                f"<div class='row'><span>Research sections</span><b>{max(_nsec, 1)}</b></div>"
                f"<div class='row'><span>Written verdict</span><b style='color:#34d399;'>&#10003;</b></div></div>"
            )
        _cov_cards.append(
            "<a class='sdd-cov sdd-cov-req' href='/research' target='_self'>"
            "<div style='font-size:22px;color:#2dd4bf;'>&#65291;</div>"
            "Which stock should be researched next?<br>"
            "<span style='color:#2dd4bf;font-weight:600;'>Tell us via Feedback &rarr;</span></a>"
        )
        st.markdown(
            "<div class='sdd-kicker' style='margin-top:40px;'>RATIONAL COMPOUNDER RESEARCH</div>"
            "<div class='sdd-h2'>Covered in depth today</div>"
            "<div class='sdd-secsub'>New companies are added as the research completes &mdash; "
            "each one takes weeks, not minutes.</div>"
            f"<div class='sdd-covgrid'>{''.join(_cov_cards)}</div>",
            unsafe_allow_html=True,
        )

    # ---- CTA band ----
    st.markdown(
        """
<div class='sdd-cta'>
  <div>
    <div class='sdd-h2' style='margin:0 0 6px;'>Everything is free while we launch.</div>
    <div style='color:#8aa0b8;font-size:14.5px;max-width:560px;line-height:1.5;'>Sign in (top
    right) to save a watchlist and get the weekly signal digest. When subscriptions open,
    founding members keep launch pricing &mdash; locked in.</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    # ---- DEFERRED DATA FILL: one concurrent, hard-budgeted fetch ----
    _home = _fetch_with_budget(
        {
            "tape": lambda: _tape_quotes(_day_key),
            "mood_au": lambda: get_country_mood("Australia"),
            "mood_us": lambda: get_country_mood("USA"),
            "featured": lambda: _featured_analysis(_feat_ticker, _day_key),
        },
        budget_seconds=10,
    )

    with _tape_box:
        _render_tape(_home["tape"])

    with _feat_box:
        _feat = _home["featured"]
        if _feat:
            _hist = get_price_history(_feat["ticker"])
            _spark_pts, _last_pt, _ma_pts = None, None, None
            try:
                _closes = _hist["Close"].dropna().tail(120)
                _spark_pts, _last_pt = _spark_path(_closes)
                _ma = _hist["Close"].rolling(50).mean().dropna().tail(120)
                if len(_ma) >= 2:
                    _ma_pts, _ = _spark_path(list(_ma))
            except Exception:
                pass
            st.markdown(
                _featured_card_html(_feat, _spark_pts, _ma_pts, _last_pt),
                unsafe_allow_html=True,
            )
            if st.button(f"Open the full {_feat['ticker']} Deep Dive →", key="feat_open",
                         use_container_width=True):
                _dispatch_search(_feat["ticker"])
        else:
            st.markdown(
                """
<div class='sdd-card'>
  <div class='sdd-card-tag'><span>WHAT YOU GET</span></div>
  <div style='font-size:15px;line-height:1.7;color:#8aa0b8;'>
    Every search answers three questions:<br><br>
    <b style='color:#e6edf5;'>What is it worth?</b> A live DCF with a per-stock discount rate,
    plus margin of safety vs today's price.<br><br>
    <b style='color:#e6edf5;'>Is it a good business?</b> A 0&ndash;100 Quality Score from
    profitability and balance-sheet tests.<br><br>
    <b style='color:#e6edf5;'>Is now a sane entry?</b> Crowd psychology and a technical entry
    zone &mdash; kept separate from the ownership question.
  </div>
</div>
""",
                unsafe_allow_html=True,
            )

    with _mood_box:
        _tiles = []
        for _mood, _k in ((_home["mood_au"], "AU"), (_home["mood_us"], "US")):
            try:
                if _mood and _mood.get("label") and _mood["label"] != "Unknown":
                    _mc = {"Hopeful": "#34d399", "Neutral": "#8aa0b8",
                           "Anxious": "#fbbf24"}.get(_mood["label"], "#8aa0b8")
                    _tiles.append(
                        f"<div class='sdd-tile'><div class='k'>{_k} MARKET MOOD</div>"
                        f"<div class='v' style='color:{_mc};'>{_mood['label'].upper()}</div>"
                        f"<div class='d'>live news-tone reading</div></div>"
                    )
            except Exception:
                pass
        for _lbl, _last, _chg in (_home["tape"] or [])[:2]:
            _cc = "#34d399" if _chg >= 0 else "#fb7185"
            _tiles.append(
                f"<div class='sdd-tile'><div class='k'>{html.escape(_lbl)}</div>"
                f"<div class='v'>{_last:,.1f}</div>"
                f"<div class='d' style='color:{_cc};'>{_chg:+.2f}% today</div></div>"
            )
        if _tiles:
            st.markdown(
                f"<div class='sdd-strip'>{''.join(_tiles[:4])}</div>", unsafe_allow_html=True
            )


def page_deep_dive():
    _render_header(compact=True, page_label="Deep Dive")
    _dd = st.session_state.get("dd_result")

    # Shareable URLs: /deep-dive?ticker=CSL.AX runs the analysis directly,
    # so a blog post or shared link opens a live result instead of an empty
    # page. Once a result is showing, the URL is updated to match it, so
    # copying the address bar always captures what's on screen. The
    # one-shot "dd_qp_tried" flag stops a ticker that errors from being
    # re-analyzed on every rerun.
    _qp_ticker = (st.query_params.get("ticker") or "").strip().upper()
    if _qp_ticker and (_dd is None or _dd.get("ticker") != _qp_ticker):
        if st.session_state.get("dd_qp_tried") != _qp_ticker:
            st.session_state["dd_qp_tried"] = _qp_ticker
            with st.spinner(f"Analyzing {_qp_ticker}..."):
                st.session_state["dd_result"] = deep_dive_engine.analyze(
                    _qp_ticker, get_price_history, get_ticker_info,
                    get_cashflow_df, news_api_key=news_api_key,
                    live_data=live_data, enable_social=enable_social,
                )
            _dd = st.session_state["dd_result"]
    if _dd is not None and not _dd.get("error") and _dd.get("ticker"):
        st.query_params["ticker"] = _dd["ticker"]

    # The explanatory line only earns its space when there's nothing else
    # on the page yet - and it leads with outcomes, not model internals
    # (the methodology detail lives on the results themselves).
    if _dd is None or _dd.get("error"):
        st.caption(
            "One ticker, the complete picture: what the stock is worth (a "
            "live DCF built from its own cash flows), whether the business "
            "is high quality, whether the crowd is fearful or greedy about "
            "it, and whether right now is a sane entry - with the exact "
            "factors behind every score charted, nothing hidden."
        )

    if _dd is None:
        st.info("Search a ticker above to see its Deep Dive - or try one of these:")
        _render_example_chips("dd_empty")
    elif _dd.get("error"):
        st.error(_dd["error"])
    else:
        if _dd["long_score"] > SIGNAL_THRESHOLDS["STRONG_LONG"]:
            _dd_signal = "STRONG LONG"
        elif _dd["long_score"] > SIGNAL_THRESHOLDS["LONG"]:
            _dd_signal = "LONG"
        elif _dd["long_score"] > SIGNAL_THRESHOLDS["WATCHLIST"]:
            _dd_signal = "WATCHLIST"
        else:
            _dd_signal = "AVOID"

        st.subheader(f"{_dd['ticker']} - {_dd['name']}")

        _m1, _m2, _m3, _m4, _m5 = st.columns(5)
        _m1.metric("Price", f"{_dd['price']:,.2f} {_dd['currency']}")
        _m2.metric(
            "Intrinsic Value",
            f"{_dd['intrinsic_value']:,.2f}" if _dd["intrinsic_value"] else "N/A",
        )
        _m3.metric("MOS", f"{_dd['mos']:+.1f}%" if _dd["mos"] is not None else "N/A")
        _m4.metric("Long Score", f"{_dd['long_score']:.1f}")
        _m5.metric("Signal", _dd_signal)

        # --- Watchlist (signed-in users): the sign-in carrot, and the
        # audience the weekly digest goes to. ---
        _wl_email = paywall_engine.current_user_email()
        if _wl_email:
            _in_wl = watchlist_store.contains(_wl_email, _dd["ticker"])
            _wl_label = ("\u2605 Remove from my watchlist" if _in_wl
                         else "\u2606 Add to my watchlist")
            if st.button(_wl_label, key=f"wl_{_dd['ticker']}"):
                try:
                    if _in_wl:
                        watchlist_store.remove(_wl_email, _dd["ticker"])
                    else:
                        watchlist_store.add(_wl_email, _dd["ticker"])
                except Exception:
                    st.warning("Couldn't save right now - please try again.")
                st.rerun()
        else:
            st.caption(
                "Sign in (top right) to save this stock to a watchlist and "
                "get the weekly signal digest."
            )

        # --- Price chart: the 6-month history behind every calculation on
        # this page, finally shown - with the 50-day average and the Trade
        # Filter's entry zone drawn on it. ---
        _px_hist = get_price_history(_dd["ticker"])
        if _px_hist is not None and not _px_hist.empty:
            _px_closes = _px_hist["Close"].dropna()
            if len(_px_closes) >= 2:
                fig_px = go.Figure()
                fig_px.add_trace(go.Scatter(
                    x=_px_closes.index, y=_px_closes.values, mode="lines",
                    name="Price", line=dict(color="#2dd4bf", width=2),
                ))
                _px_ma50 = _px_closes.rolling(50).mean()
                fig_px.add_trace(go.Scatter(
                    x=_px_closes.index, y=_px_ma50.values, mode="lines",
                    name="MA50", line=dict(color="#8aa0b8", width=1.5, dash="dash"),
                ))
                if _dd.get("trade_setup_entry"):
                    fig_px.add_hline(
                        y=_dd["trade_setup_entry"], line_dash="dot",
                        line_color="#fbbf24",
                        annotation_text=f"Entry zone {_dd['trade_setup_entry']:,.2f}",
                        annotation_position="bottom left", annotation_font_size=11,
                    )
                fig_px.update_layout(
                    title="Last 6 months", height=300,
                    margin=dict(l=10, r=10, t=40, b=10),
                    legend=dict(orientation="h", y=1.14),
                    yaxis_title=_dd["currency"],
                )
                st.plotly_chart(fig_px, use_container_width=True)

        # --- Plain-English verdict: the same thesis engine Comparison
        # uses, so every Deep Dive opens with sentences, not just gauges. ---
        try:
            _dd_thesis = generate_thesis(
                ticker=_dd["ticker"],
                stock_type=_dd.get("stock_type") or "GENERAL",
                quality_score=_dd["quality_score"],
                margin_of_safety=_dd["mos"] if _dd["mos"] is not None else 0,
                psychology_score=_dd["psychology"],
                discovery_score=_dd["discovery"],
                long_score=_dd["long_score"],
                holding_period=get_holding_period(_dd.get("stock_type") or "GENERAL"),
            )
        except Exception:
            _dd_thesis = None
        if _dd_thesis:
            with st.expander(
                "The plain-English case - why buy, why wait, the risks",
                expanded=True,
            ):
                _th1, _th2, _th3 = st.columns(3)
                for _col, _label, _points in (
                    (_th1, "Why buy", _dd_thesis["why_buy"]),
                    (_th2, "Why wait", _dd_thesis["why_wait"]),
                    (_th3, "Risks", _dd_thesis["risks"]),
                ):
                    with _col:
                        st.markdown(f"**{_label}**")
                        for _pt in (_points or ["Nothing flagged."]):
                            st.markdown(f"- {_pt}")
                st.caption(f"Suggested holding period: {_dd_thesis['holding_period']}")

        _dd_col1, _dd_col2 = st.columns(2)

        with _dd_col1:
            if _dd["intrinsic_value"]:
                _iv_color = "#34d399" if _dd["intrinsic_value"] > _dd["price"] else "#fb7185"
                fig_val = go.Figure(go.Bar(
                    x=[_dd["price"], _dd["intrinsic_value"]],
                    y=["Current Price", "Intrinsic Value (Base Case)"],
                    orientation="h",
                    marker_color=["#8aa0b8", _iv_color],
                    text=[f"{_dd['price']:,.2f}", f"{_dd['intrinsic_value']:,.2f}"],
                    textposition="outside",
                ))
                fig_val.update_layout(
                    title="Price vs Intrinsic Value",
                    showlegend=False, height=260,
                    margin=dict(l=10, r=10, t=40, b=10),
                    xaxis_title=_dd["currency"],
                )
                st.plotly_chart(fig_val, use_container_width=True)
            else:
                st.warning(
                    "No intrinsic value could be computed for this ticker "
                    "(DCF and P/E-blend both unavailable - likely a "
                    "financial or a name with no positive EPS/FCF)."
                )

        with _dd_col2:
            st.plotly_chart(
                _dd_gauge(
                    _dd["long_score"], f"Long Score - {_dd_signal}",
                    [
                        (0, SIGNAL_THRESHOLDS["WATCHLIST"], "#43222e"),
                        (SIGNAL_THRESHOLDS["WATCHLIST"], SIGNAL_THRESHOLDS["LONG"], "#43371c"),
                        (SIGNAL_THRESHOLDS["LONG"], SIGNAL_THRESHOLDS["STRONG_LONG"], "#1e3d34"),
                        (SIGNAL_THRESHOLDS["STRONG_LONG"], 100, "#27584a"),
                    ],
                ),
                use_container_width=True,
            )
            st.caption(
                "Long Score, 0-100: business quality + margin of safety weighted, "
                f"nudged by psychology and attention. Above {SIGNAL_THRESHOLDS['LONG']} "
                f"= LONG territory, above {SIGNAL_THRESHOLDS['STRONG_LONG']} = STRONG LONG."
            )

        st.plotly_chart(
            _dd_contrib_chart(
                _dd["contributions"],
                "What's driving the Long Score (points contributed by each factor)",
                xaxis_title="Points toward Long Score",
                height=280,
            ),
            use_container_width=True,
        )

        st.divider()

        if not paywall_engine.render_gate(
            "the full Deep Dive breakdown",
            teaser=(
                "Quality, Psychology, Discovery, and Trade Setup scores - the "
                "full factor breakdown behind the Long Score above."
            ),
            key_prefix=f"dd_{_dd['ticker']}",
        ):
            return

        st.subheader(f"Quality Score: {_dd['quality_score']} - {_dd['quality_label']}")
        _q_col1, _q_col2 = st.columns(2)
        with _q_col1:
            st.plotly_chart(
                _dd_gauge(
                    _dd["quality_score"], f"Quality - {_dd['quality_label']}",
                    [(0, 40, "#43222e"), (40, 60, "#43371c"),
                     (60, 80, "#1e3d34"), (80, 100, "#27584a")],
                ),
                use_container_width=True,
            )
        with _q_col2:
            if _dd["quality_components"]:
                st.plotly_chart(
                    _dd_contrib_chart(
                        _dd["quality_components"],
                        "What's driving Quality (weighted terms)",
                        xaxis_title="Points toward Quality",
                    ),
                    use_container_width=True,
                )
            else:
                st.info(
                    "Quality Score is a manual override for this ticker - "
                    "no fundamentals breakdown to chart."
                )
        if _dd["quality_default"]:
            st.caption("No fundamentals data available - this is the base/default Quality Score.")

        st.divider()
        st.subheader(f"Psychology Score: {_dd['psychology']:+.1f} - {_dd['psychology_sentiment']}")
        if _dd.get("ma50_defaulted"):
            st.warning(
                f"MA50 couldn't be computed (fewer than 50 trading days available in "
                f"the pulled history) - Greed defaulted to 0 as a result. This is a "
                f"placeholder, not a real reading of price vs. the 50-day average; "
                f"treat Greed/Psychology for this stock with caution."
            )
        else:
            st.caption(f"MA50: {_dd['currency']} {_dd['ma50']:,.2f}")
        _p_col1, _p_col2 = st.columns(2)
        with _p_col1:
            st.plotly_chart(
                _dd_gauge(
                    _dd["psychology_gauge"], f"Psychology - {_dd['psychology_sentiment']}",
                    [(0, 30, "#43222e"), (30, 45, "#43371c"), (45, 55, "#1f3352"),
                     (55, 70, "#1e3d34"), (70, 100, "#27584a")],
                ),
                use_container_width=True,
            )
        with _p_col2:
            st.plotly_chart(
                _dd_contrib_chart(
                    _dd["psychology_contributions"],
                    "What's driving Psychology (Fear - Greed - FOMO)",
                    xaxis_title="Points toward Psychology",
                ),
                use_container_width=True,
            )

        st.divider()
        st.subheader(f"Discovery Score: {_dd['discovery']:.1f} - {_dd['discovery_label']}")
        _dv_col1, _dv_col2 = st.columns(2)
        with _dv_col1:
            st.plotly_chart(
                _dd_gauge(
                    _dd["discovery_gauge"], f"Discovery - {_dd['discovery_label']}",
                    [(0, 25, "#43222e"), (25, 50, "#43371c"),
                     (50, 75, "#1e3d34"), (75, 100, "#27584a")],
                ),
                use_container_width=True,
            )
        with _dv_col2:
            st.plotly_chart(
                _dd_contrib_chart(
                    _dd["discovery_contributions"],
                    "What's driving Discovery (attention & momentum)",
                    xaxis_title="Points toward Discovery",
                ),
                use_container_width=True,
            )

        st.divider()
        st.subheader(f"Trade Setup: {_dd['trade_setup_score']} - {_dd['trade_setup_signal']}")
        _t_col1, _t_col2 = st.columns(2)
        with _t_col1:
            st.plotly_chart(
                _dd_gauge(
                    _dd["trade_setup_score"], f"Trade Setup - {_dd['trade_setup_signal']}",
                    [(0, 45, "#43222e"), (45, 65, "#43371c"), (65, 100, "#27584a")],
                ),
                use_container_width=True,
            )
        with _t_col2:
            st.plotly_chart(
                _dd_gate_chart(
                    _dd["trade_setup_contributions"],
                    "What's driving the Trade Setup Score",
                    xaxis_title="Points toward Setup Score",
                ),
                use_container_width=True,
            )

        _tt_x = [_dd["trade_setup_stop"], _dd["trade_setup_entry"], _dd["trade_setup_target1"]]
        _tt_y = ["Stop Loss", "Entry Zone", "Target 1"]
        _tt_colors = ["#fb7185", "#8aa0b8", "#1e3d34"]
        if _dd["trade_setup_target2"] is not None:
            _tt_x.append(_dd["trade_setup_target2"])
            _tt_y.append("Target 2")
            _tt_colors.append("#3f8a6e")
        if _dd["trade_setup_target3"] is not None:
            _tt_x.append(_dd["trade_setup_target3"])
            _tt_y.append("Target 3 (breakout)")
            _tt_colors.append("#34d399")

        fig_trade = go.Figure(go.Bar(
            x=_tt_x, y=_tt_y, orientation="h",
            marker_color=_tt_colors,
            text=[f"{v:,.2f}" for v in _tt_x],
            textposition="outside",
        ))
        fig_trade.update_layout(
            title="Trade Setup - Entry / Stop Loss / Targets",
            xaxis_title=_dd["currency"], showlegend=False, height=300,
            margin=dict(l=10, r=10, t=40, b=10),
        )
        st.plotly_chart(fig_trade, use_container_width=True)

        _tt_rr = f"RR1 {_dd['trade_setup_rr1']}"
        if _dd["trade_setup_rr2"] is not None:
            _tt_rr += f", RR2 {_dd['trade_setup_rr2']}"
        if _dd["trade_setup_rr3"] is not None:
            _tt_rr += f", RR3 {_dd['trade_setup_rr3']}"
        st.caption(
            f"Current price {_dd['trade_setup_current_price']:,.2f} {_dd['currency']} vs "
            f"Entry Zone {_dd['trade_setup_entry']:,.2f} {_dd['currency']} - "
            + ("currently inside the entry zone." if _dd["trade_setup_near_entry"]
               else "not yet inside the entry zone, price hasn't pulled back enough.")
            + f" Risk {_dd['trade_setup_risk']:,.2f} {_dd['currency']} per share - {_tt_rr} "
              "(risk/reward is measured from today's price, not the discounted "
              "entry zone above - the same convention the Trade Filter table uses)."
        )


def _render_country_mood_line(country):
    """
    One-line GDELT mood reading for `country` - just the headline result
    (Hopeful/Neutral/Anxious + the underlying news-tone number), no
    "what's driving this reading" expander. Purely informational, same as
    everywhere else this reading is used - never feeds Long Score or any
    per-stock scoring.
    """
    if country not in mood_engine.COUNTRY_SOURCES:
        return
    # If the reading can't be fetched (usually a GDELT rate limit), render
    # nothing at all - a public page shouldn't surface raw upstream errors,
    # and an "Unknown" banner is noise. The failed lookup isn't cached, so
    # the line reappears on its own as soon as GDELT recovers.
    try:
        _mood = get_country_mood(country)
    except Exception:
        return
    _mood_fn = {
        "Hopeful": st.success, "Neutral": st.info, "Anxious": st.warning,
    }.get(_mood["label"], st.info)
    _mood_fn(
        f"{country} is feeling **{_mood['label']}** - based on GDELT's live "
        f"news-tone reading averaged over the last 10 days "
        f"({_mood['gdelt_tone']:+.2f})."
    )


# -----------------------------------
# RED-FONT DEFAULT STYLING + INTRINSIC VS PRICE COLORING
#
# Any cell whose value had to fall back to a default/average assumption
# (rather than being computed from sourced data) is rendered in red, so the
# user can instantly see which numbers are estimates. The per-cell booleans
# come from the hidden "_flag_*" columns populated in the scan loop.
#
# Separately, wherever "Intrinsic Value" appears alongside "Price", it's
# colored green when the DCF intrinsic value is ABOVE the current price
# (looks undervalued) or red when it's BELOW (looks overvalued) - a quick
# visual echo of the Valuation/MOS columns, right on the number itself. The
# default-assumption red/bold above always wins over this on a shared cell,
# since "this number is a guess" is a data-quality flag, not a valuation
# call, and matters more.
# -----------------------------------

_DEFAULT_RED = "color: #fb7185; font-weight: 600"
_INTRINSIC_ABOVE_PRICE = "color: #34d399; font-weight: 600"   # green - looks undervalued
_INTRINSIC_BELOW_PRICE = "color: #fb7185; font-weight: 600"   # red - looks overvalued

# Long Score traffic-light colors, keyed off the SAME SIGNAL_THRESHOLDS gates
# used to set the Investment Signal (STRONG LONG / LONG / WATCHLIST / AVOID)
# everywhere else in the app - see the DCF Parameters table caption for the
# exact cutoffs shown to the user.
_LONG_SCORE_STRONG = "color: #34d399; font-weight: 600"   # green - above LONG gate
_LONG_SCORE_WATCH = "color: #fbbf24; font-weight: 600"    # amber - above WATCHLIST, at/below LONG
_LONG_SCORE_AVOID = "color: #fb7185; font-weight: 600"    # red - at/below WATCHLIST gate

_FLAG_FOR_COL = {
    "Type": "_flag_type",
    "Quality": "_flag_quality",
    "Intrinsic Value": "_flag_intrinsic",
    "MOS": "_flag_intrinsic",
    "Val Method": "_flag_growth",
    "DCF Growth %": "_flag_growth",
}


def style_defaults(display_df, source_df, color_long_score=False):
    """Return a Styler that paints defaulted cells red, and colors
    "Intrinsic Value" green/red against "Price" where both are present.
    display_df must share its index with source_df (the full results frame
    holding the _flag_* and Price columns).

    color_long_score=True additionally traffic-lights a "Long Score" column
    (green/yellow/red) using SIGNAL_THRESHOLDS - opt-in per call site rather
    than always-on, since not every table that shows Long Score asked for
    this coloring."""
    cols = list(display_df.columns)

    def _apply(_):
        styles = pd.DataFrame("", index=display_df.index, columns=cols)

        if "Intrinsic Value" in cols and "Price" in source_df.columns:
            _iv = pd.to_numeric(
                source_df["Intrinsic Value"].reindex(display_df.index), errors="coerce"
            )
            _px = pd.to_numeric(
                source_df["Price"].reindex(display_df.index), errors="coerce"
            )
            _valid = _iv.notna() & _px.notna()
            styles.loc[_valid & (_iv > _px), "Intrinsic Value"] = _INTRINSIC_ABOVE_PRICE
            styles.loc[_valid & (_iv < _px), "Intrinsic Value"] = _INTRINSIC_BELOW_PRICE

        if color_long_score and "Long Score" in cols and "Long Score" in source_df.columns:
            _ls = pd.to_numeric(
                source_df["Long Score"].reindex(display_df.index), errors="coerce"
            )
            styles.loc[_ls > SIGNAL_THRESHOLDS["LONG"], "Long Score"] = _LONG_SCORE_STRONG
            styles.loc[
                (_ls > SIGNAL_THRESHOLDS["WATCHLIST"]) & (_ls <= SIGNAL_THRESHOLDS["LONG"]),
                "Long Score",
            ] = _LONG_SCORE_WATCH
            styles.loc[
                _ls.notna() & (_ls <= SIGNAL_THRESHOLDS["WATCHLIST"]), "Long Score"
            ] = _LONG_SCORE_AVOID

        # Verdict columns (Signal / Valuation / Sentiment / Trend / Trade
        # Setup) traffic-lighted with the same green/amber/red convention
        # as every score bar on the site.
        _VERDICT_STYLE = {
            "STRONG LONG": _LONG_SCORE_STRONG, "LONG": _LONG_SCORE_STRONG,
            "BUY": _LONG_SCORE_STRONG, "UNDERVALUED": _LONG_SCORE_STRONG,
            "FEARFUL": _LONG_SCORE_STRONG, "Uptrend": _LONG_SCORE_STRONG,
            "WATCHLIST": _LONG_SCORE_WATCH, "FAIR": _LONG_SCORE_WATCH,
            "GREEDY": _LONG_SCORE_WATCH, "Ranging": _LONG_SCORE_WATCH,
            "AVOID": _LONG_SCORE_AVOID, "EXPENSIVE": _LONG_SCORE_AVOID,
            "OVERHEATED": _LONG_SCORE_AVOID, "Downtrend": _LONG_SCORE_AVOID,
        }
        for _vcol in ("Investment Signal", "Signal", "Valuation",
                      "Sentiment", "Trend", "Trade Setup", "Swing Setup"):
            if _vcol in cols:
                _vals = display_df[_vcol].astype(str)
                for _txt, _sty in _VERDICT_STYLE.items():
                    styles.loc[_vals == _txt, _vcol] = _sty

        for disp_col, flag_col in _FLAG_FOR_COL.items():
            if disp_col in cols and flag_col in source_df.columns:
                mask = (
                    source_df[flag_col]
                    .reindex(display_df.index)
                    .fillna(False)
                    .astype(bool)
                )
                styles.loc[mask, disp_col] = _DEFAULT_RED
        return styles

    # precision=2 stops the Styler's default 6-decimal float rendering
    # (11.960000) - two decimals everywhere, dashes for missing values.
    return (
        display_df.style.apply(_apply, axis=None)
        .format(precision=2, thousands=",", na_rep="-")
    )


# -----------------------------------
# HTML TABLE KIT - the side-by-side comparison's visual language (infill
# bars, verdict pills, price-relative colouring) as reusable cells, so
# EVERY table on the Comparison and Scanner pages reads the same way.
# -----------------------------------
_BAR_RED, _BAR_AMBER, _BAR_GREEN = "#fb7185", "#fbbf24", "#34d399"
_TYPE_NEUTRAL = "#2dd4bf"
_BADGE_COLORS = {
    "UNDERVALUED": _BAR_GREEN, "FAIR": _BAR_AMBER, "EXPENSIVE": _BAR_RED,
    "FEARFUL": _BAR_GREEN, "GREEDY": _BAR_AMBER, "OVERHEATED": _BAR_RED,
    "CALM": "#8aa0b8", "NEUTRAL": "#8aa0b8",
    "Uptrend": _BAR_GREEN, "Ranging": _BAR_AMBER, "Downtrend": _BAR_RED,
    "UPTREND": _BAR_GREEN, "RANGING": _BAR_AMBER, "DOWNTREND": _BAR_RED,
    "BUY": _BAR_GREEN, "WATCHLIST": _BAR_AMBER, "AVOID": _BAR_RED,
    "STRONG LONG": _BAR_GREEN, "LONG": _BAR_GREEN,
    "N/A": "#8aa0b8", "Yes": _BAR_AMBER, "No": "#8aa0b8",
}


def _bar_cell(value, low, high, suffix="", flag=False):
    """Value above a colored infill bar (red/amber/green by the same
    thresholds the app verdicts on elsewhere). flag=True renders the value
    text red - the site-wide 'this number is a default/estimate' mark."""
    if value is None or value == "N/A" or (isinstance(value, float) and pd.isna(value)):
        return "<div style='color:#8aa0b8;'>N/A</div>"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"<div>{value}</div>"
    color = _BAR_RED if v <= low else (_BAR_GREEN if v > high else _BAR_AMBER)
    width_pct = max(0.0, min(100.0, v))
    _txt_style = "color:#fb7185;font-weight:700;" if flag else ""
    return (
        f"<div style='font-size:13px;margin-bottom:2px;{_txt_style}'>{v:,.1f}{suffix}</div>"
        f"<div style='background:#1f3352;border-radius:3px;height:8px;width:100%;'>"
        f"<div style='background:{color};height:8px;border-radius:3px;"
        f"width:{width_pct:.0f}%;'></div></div>"
    )


def _badge_cell(text, color=None):
    """One classification label as a small colored pill."""
    _c = color or _BADGE_COLORS.get(str(text), "#9db1c7")
    return (
        f"<span style='display:inline-block;padding:3px 10px;"
        f"border-radius:12px;font-size:12.5px;font-weight:600;"
        f"background:{_c}22;color:{_c};'>{text}</span>"
    )


def _money_cell(value, ref=None, flag=False, fmt="{:,.2f}"):
    """A price-like number. With `ref` (usually the current price) it's
    coloured green above / red below - the intrinsic-value convention used
    site-wide. flag=True (defaulted input) always wins, in red bold."""
    if value is None or value == "N/A" or (isinstance(value, float) and pd.isna(value)):
        return "<span style='color:#8aa0b8;'>N/A</span>"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"<span>{value}</span>"
    style = ""
    if flag:
        style = f"color:{_BAR_RED};font-weight:700;"
    elif ref is not None:
        try:
            style = (f"color:{_BAR_GREEN};font-weight:600;" if v > float(ref)
                     else f"color:{_BAR_RED};font-weight:600;")
        except (TypeError, ValueError):
            style = ""
    return f"<span style='{style}'>{fmt.format(v)}</span>"


def _signed_cell(value, suffix="%"):
    """Signed number coloured by its sign (upside/downside style)."""
    if value is None or value == "N/A" or (isinstance(value, float) and pd.isna(value)):
        return "<span style='color:#8aa0b8;'>N/A</span>"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"<span>{value}</span>"
    c = _BAR_GREEN if v > 0 else (_BAR_RED if v < 0 else "#8aa0b8")
    return f"<span style='color:{c};font-weight:600;'>{v:+,.1f}{suffix}</span>"


def _rank_cell(value, column_values, fmt="{:,.2f}"):
    """Rank-coloured number: best value in the column green, worst red,
    everything between amber (used for the RR columns, where 'good' is
    relative to the other setups on screen)."""
    if value in (None, "-", "N/A") or (isinstance(value, float) and pd.isna(value)):
        return "<span style='color:#8aa0b8;'>-</span>"
    try:
        v = float(value)
        vals = [float(x) for x in column_values
                if x not in (None, "-", "N/A") and not (isinstance(x, float) and pd.isna(x))]
    except (TypeError, ValueError):
        return f"<span>{value}</span>"
    if not vals:
        return f"<span>{fmt.format(v)}</span>"
    hi, lo = max(vals), min(vals)
    if len(vals) == 1 or hi == lo:
        c = _BAR_GREEN if v >= 1.5 else (_BAR_AMBER if v >= 1.0 else _BAR_RED)
    elif v == hi:
        c = _BAR_GREEN
    elif v == lo:
        c = _BAR_RED
    else:
        c = _BAR_AMBER
    return f"<span style='color:{c};font-weight:600;'>{fmt.format(v)}</span>"


def _sdd_table(headers, rows_html, max_height=None):
    """Assemble the shared table shell (same styling as the side-by-side
    comparison). rows_html = list of '<tr>...</tr>' strings."""
    table = (
        "<table style='width:100%;border-collapse:collapse;font-size:14px;'>"
        "<thead><tr>"
        + "".join(
            f"<th style='text-align:left;padding:6px 10px;border-bottom:2px solid #1f3352;'>{h}</th>"
            for h in headers
        )
        + "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
    )
    if max_height:
        return f"<div style='max-height:{max_height}px;overflow-y:auto;'>{table}</div>"
    return table


def _td(inner, minw=None):
    _w = f"min-width:{minw}px;" if minw else ""
    return f"<td style='padding:6px 10px;{_w}'>{inner}</td>"


def page_scanner():
    _render_header(compact=True, page_label="Scanner")

    st.session_state.setdefault("scanner_country_au", True)
    st.session_state.setdefault("scanner_country_us", False)

    st.write(
        "Tick one or more countries, then pick a single universe to scan - "
        "each universe below is scanned entirely on its own (ASX 200 and "
        "ASX 300 are never blended together, and neither are any of the "
        "USA universes)."
    )

    _col_au, _col_us = st.columns(2)
    with _col_au:
        _want_au = st.checkbox("Australia", key="scanner_country_au")
    with _col_us:
        _want_us = st.checkbox("USA", key="scanner_country_us")

    if _want_au:
        _render_country_mood_line("Australia")
    if _want_us:
        _render_country_mood_line("USA")

    _universe_options = []
    if _want_au:
        _universe_options += scanner_engine.get_universes("Australia")
    if _want_us:
        _universe_options += scanner_engine.get_universes("USA")

    if not _universe_options:
        st.info("Tick at least one country above to pick a universe to scan.")
        return

    # Guard against a previously-picked universe/sector no longer being a
    # valid option (e.g. unticking USA while "S&P 500" was selected) -
    # Streamlit raises if a selectbox's session_state value isn't in its
    # current options, so this has to be fixed up BEFORE the widget below
    # is instantiated, not after.
    if st.session_state.get("scanner_universe") not in _universe_options:
        st.session_state["scanner_universe"] = _universe_options[0]

    universe = st.selectbox("Universe", _universe_options, key="scanner_universe")
    universe_country = (
        "Australia" if universe in scanner_engine.get_universes("Australia") else "USA"
    )

    _pool_df, _pool_source = scanner_engine.get_universe_pool(universe_country, universe)
    _sectors = scanner_engine.get_sectors(_pool_df)

    if st.session_state.get("scanner_sector") not in _sectors:
        st.session_state["scanner_sector"] = "All"

    # Sector heat decorates each dropdown OPTION's label (e.g. "\U0001F7E2
    # Technology - Hot (+14.3% 12m)") - it never changes the underlying
    # selected value (still the plain sector name), and there's no separate
    # detail table/expander for it on this site, unlike the original app.
    # Cached a day per exact universe/sector set, so this is only slow the
    # first time a given universe's sector list is shown.
    # Sector heat decorates each sector option with a colored dot
    # (green/amber/red = hot/medium/cold vs the rest of this universe).
    # First computation for a universe takes a while; cached for a day.
    _heat_pairs = ()
    if _pool_df is not None and not _pool_df.empty and "Sector" in _pool_df.columns:
        _heat_pairs = tuple(zip(_pool_df["Ticker"], _pool_df["Sector"]))
    _sector_heat = {}
    if _heat_pairs:
        with st.spinner("Reading sector heat (cached for the day once computed)..."):
            _sector_heat = scanner_engine.compute_sector_heat(_heat_pairs)

    sector = st.selectbox(
        "Sector (optional)", _sectors, key="scanner_sector",
        format_func=lambda s: scanner_engine.label_for(s, _sector_heat),
    )

    st.caption(f"Universe source: {_pool_source}")

    # ---- Overnight scan: served instantly when the nightly job has run
    # for this universe - the answer to "what should I look at?" without
    # a 30-minute live wait. Live scan below stays available for current
    # prices and the full attention model.
    _overnight = scan_store.load_scan(universe)
    if _overnight:
        with st.expander(
            f"Overnight {universe} scan - {len(_overnight['rows'])} stocks "
            f"ranked by Long Score, computed {_overnight['generated_at_label']}",
            expanded=st.session_state.get("scan_stocks") is None,
        ):
            st.caption(
                "Pre-computed while nobody was waiting. Attention-lite "
                "(price/volume only - no news/trends/social inputs), same "
                "rule as live scans this size. Estimated/default values "
                "carry their own flag columns. Run a live scan below for "
                "current prices."
            )
            _on_rows_html = []
            for _orow in _overnight["rows"]:
                _on_rows_html.append(
                    "<tr>"
                    + _td(f"<b>{_orow.get('Ticker', '-')}</b>")
                    + _td(_badge_cell(_orow.get("Type", "-"), _TYPE_NEUTRAL))
                    + _td(f"{(_orow.get('Price') or 0):,.2f}")
                    + _td(_money_cell(_orow.get("Intrinsic Value"),
                                      ref=_orow.get("Price"),
                                      flag=bool(_orow.get("Intrinsic Default"))))
                    + _td(_bar_cell(_orow.get("MOS %"), 0, 25, "%",
                                    flag=bool(_orow.get("Intrinsic Default"))), minw=90)
                    + _td(_bar_cell(_orow.get("Long Score"),
                                    SIGNAL_THRESHOLDS["WATCHLIST"],
                                    SIGNAL_THRESHOLDS["LONG"]), minw=90)
                    + _td(_bar_cell(_orow.get("Quality"), 40, 80,
                                    flag=bool(_orow.get("Quality Default"))), minw=90)
                    + _td(_bar_cell(_orow.get("Psychology"), -5, 20), minw=90)
                    + _td(_bar_cell(_orow.get("Discovery (lite)"), 25, 50), minw=90)
                    + _td(_badge_cell(_orow.get("Valuation", "-")))
                    + _td(_badge_cell(_orow.get("Signal", "-")))
                    + _td(_badge_cell(_orow.get("Trend", "-")))
                    + _td(_badge_cell(_orow.get("Trade Setup", "-")))
                    + "</tr>"
                )
            st.markdown(
                _sdd_table(
                    ["Ticker", "Type", "Price", "Intrinsic Value", "MOS",
                     "Long Score", "Quality", "Psychology", "Discovery",
                     "Valuation", "Signal", "Trend", "Trade Setup"],
                    _on_rows_html, max_height=480,
                ),
                unsafe_allow_html=True,
            )
            st.caption(
                "Red values = computed from a default/average because real "
                "data wasn't available (the site-wide red-flag rule)."
            )
            st.caption(f"Universe source at scan time: {_overnight['source']}")

    if st.button("Run Scan", type="primary", key="run_scanner"):
        with st.spinner("Resolving universe..."):
            _tickers, _source = scanner_engine.resolve_tickers(universe_country, universe, sector)
        if not _tickers:
            st.warning("No stocks matched this universe/sector - try a different selection.")
        else:
            st.session_state["scan_stocks"] = _tickers
            st.session_state["scan_universe_source"] = f"{universe} - {_source}"
            st.session_state["scan_scan_country"] = universe_country
            st.session_state["scan_fresh"] = True

    _render_scan_results(
        page_label="Scanner",
        state_prefix="scan",
        empty_message="Pick a country, universe, and (optionally) a sector above, then click Run Scan.",
    )


def page_comparison():
    _render_header(compact=True, page_label="Comparison")

    # Shareable URLs: /comparison?tickers=CSL.AX,BHP.AX runs the comparison
    # directly (blog posts can deep-link a specific matchup), and once
    # results exist the URL is kept in sync so the address bar is always
    # shareable. One-shot "cmp_qp_tried" stops an erroring list from
    # re-scanning on every rerun.
    _qp_tickers = (st.query_params.get("tickers") or "").strip().upper()
    if _qp_tickers and not st.session_state.get("cmp_stocks") \
            and st.session_state.get("cmp_qp_tried") != _qp_tickers:
        st.session_state["cmp_qp_tried"] = _qp_tickers
        _qp_parsed = []
        for _tok in _qp_tickers.replace(",", " ").split():
            if _tok and _tok not in _qp_parsed:
                _qp_parsed.append(_tok)
        if len(_qp_parsed) >= 2:
            _qp_au = sum(1 for _t in _qp_parsed if _t.endswith(".AX"))
            st.session_state["cmp_stocks"] = _qp_parsed
            st.session_state["cmp_universe_source"] = (
                f"Shared link ({len(_qp_parsed)} tickers)"
            )
            st.session_state["cmp_scan_country"] = (
                "USA" if (len(_qp_parsed) - _qp_au) > _qp_au else "Australia"
            )
            st.session_state["cmp_fresh"] = True
    if st.session_state.get("cmp_stocks"):
        st.query_params["tickers"] = ",".join(st.session_state["cmp_stocks"])

    _render_scan_results(
        page_label="Comparison",
        state_prefix="cmp",
        empty_message="Search two or more tickers above to run a Comparison.",
    )


def _render_scan_results(page_label, state_prefix, empty_message,
                         default_country="Australia", lite_threshold=100):
    """
    Shared scan-and-results engine behind both Comparison (manual ticker
    list, typed into the search box) and Scanner (a whole universe,
    resolved from scanner_engine.py) - same scoring, same tables, so the
    two pages always look and behave identically apart from how `stocks`
    got populated. Each caller gets its own session_state namespace
    (state_prefix) and its own results cache key, so running a scan on one
    page never overwrites what's showing on the other. Callers render their
    own header (and, for Scanner, its own selection UI) before calling this.
    """
    # The search box (Comparison) or the Run Scan button (Scanner) is the
    # only thing that ever populates a request here - each stores its
    # parsed ticker list + a one-shot "fresh" flag in session_state under
    # this page's own state_prefix before this function runs.
    _fresh_scan = st.session_state.pop(f"{state_prefix}_fresh", False)
    stocks = st.session_state.get(f"{state_prefix}_stocks")
    universe_source = st.session_state.get(f"{state_prefix}_universe_source", "")
    scan_country = st.session_state.get(f"{state_prefix}_scan_country", default_country)

    if not stocks:
        st.info(empty_message)
        if state_prefix == "cmp":
            _render_example_chips(f"{state_prefix}_empty")
        return

    # Attention-lite mode for big universe scans: per-ticker Trends/News/
    # StockTwits lookups are what blow both the clock and the shared API
    # quotas at index scale (one S&P 500 scan would exhaust a free NewsAPI
    # day on its own), so above `lite_threshold` tickers those calls are
    # skipped and Discovery reflects price/volume attention only.
    attention_lite = len(stocks) > lite_threshold

    # Comparison and Scanner each get their own fixed cache key (via
    # state_prefix) so switching between them never overwrites the other's
    # last completed scan.
    _active_cache_name = state_prefix
    _cache_key = f"last_scan_data_{state_prefix}"

    # True only right on the page load that immediately follows a Search/Run
    # Scan submission (see the one-shot "{state_prefix}_fresh" flag popped
    # above) - tells "a real new scan was just requested" apart from "some
    # OTHER widget on this results page (e.g. saving a DCF override)
    # triggered this rerun", so those reuse the already-computed results
    # instead of re-fetching everything from scratch.
    _need_fresh_scan = _fresh_scan

    _scan_area = st.container(key="scan_results_area")
    with _scan_area:
        # -----------------------------------
        # PRE-SCAN SUMMARY
        # -----------------------------------

        if len(stocks) == 0:
            st.warning("No stocks to scan.")
            st.stop()

        # Honest time expectation up front - live per-ticker analysis takes
        # real seconds per name, and pretending otherwise reads as "broken"
        # when a big scan slows down.
        if _need_fresh_scan and len(stocks) > 25:
            _est_lo = max(1, round(len(stocks) * 1.5 / 60))
            _est_hi = max(_est_lo + 1, round(len(stocks) * 4 / 60))
            st.warning(
                f"Scanning {len(stocks)} stocks live - realistically "
                f"{_est_lo}-{_est_hi} minutes. Keep this tab open; a "
                "narrower sector or universe is much faster."
                + (" Trends/news/social lookups are skipped on scans this "
                   "large, so Discovery reflects price and volume "
                   "attention only." if attention_lite else "")
            )


        # -----------------------------------
        # DATA COLLECTION
        # -----------------------------------

        data = []
        thesis_lookup = {}
        trade_lookup = {}
        trade_score_lookup = {}

        # Market regime (swing mode only): fetch the benchmark index for this market
        # once, up front, and derive risk-on / risk-off from it. Applied to every
        # row so the trader can avoid buying into a broad downtrend.
        market_regime, regime_detail = "UNKNOWN", ""
        if is_swing:
            benchmark_ticker = "^AXJO" if scan_country == "Australia" else "^GSPC"  # ASX 200 / S&P 500
            benchmark_df = get_price_history(benchmark_ticker)
            market_regime, regime_detail = swing_engine.get_market_regime(benchmark_df)

        start_time = time.time()

        # Warm every ticker's cached lookups CONCURRENTLY before the scan loop
        # below touches any of them one at a time - see _prefetch_scan_data's
        # docstring. Only worth doing for a real fresh scan (same guard as the
        # loop itself) and only once there's more than a couple of tickers,
        # since thread-pool setup isn't free and a 1-2 ticker Comparison is
        # already fast without it.
        if _need_fresh_scan and len(stocks) > 2:
            with st.spinner(f"Fetching data for {len(stocks)} stocks..."):
                _prefetch_scan_data(stocks, live_data, enable_social, news_api_key)

        # Only actually build the progress bar / iterate tickers when a fresh scan
        # is needed (see _need_fresh_scan above) - otherwise this rerun reuses the
        # already-computed data/thesis_lookup/trade_lookup restored from
        # session_state right after this loop, and the loop body below never runs.
        progress_bar = st.progress(0.0, text="Starting scan...") if _need_fresh_scan else None

        for idx, ticker in enumerate(stocks if _need_fresh_scan else []):

            progress_bar.progress(
                (idx + 1) / len(stocks),
                text=f"Scanning {ticker} ({idx + 1}/{len(stocks)})"
            )

            try:

                df = get_price_history(ticker)

                if df.empty:
                    continue

                info = get_ticker_info(ticker)

                # Fear/Greed/MA50/Volume Ratio/Activity all originally ran over a
                # 3-month window. History is now pulled as 6 months so the Trade
                # Filter's 60-day resistance/support has a full window behind it -
                # but that must NOT silently change what "recent high" or "average
                # volume" mean for the existing scores, so those still use just the
                # trailing ~63 trading days (~3 months). The full 6-month `df` is
                # only used further below, for calc_support_resistance().
                window_3mo = df.tail(63)

                # NaN-proof: some feeds return trailing NaN rows (seen on
                # NYSE:NU) - a NaN current price cascades into every score
                # and finally crashes an int() cast. Use the last REAL
                # close/volume instead, and skip the ticker if none exist.
                _close_series = window_3mo["Close"].dropna()
                if _close_series.empty:
                    continue
                current_price = float(_close_series.iloc[-1])

                high_price = float(_close_series.max())

                fear_score = (
                    (high_price - current_price) / high_price
                ) * 100 if high_price else 0

                ma50 = window_3mo["Close"].rolling(50).mean().iloc[-1]

                if pd.isna(ma50) or ma50 == 0:
                    ma50 = current_price

                greed_score = max(((current_price - ma50) / ma50) * 100, 0)

                keyword = ticker.split(".")[0]

                if live_data and not attention_lite:
                    trend_score = get_trend_score(keyword, api_key=news_api_key or None)
                    # Combined from two independent sources: NewsAPI (keyword text
                    # search, needs NEWS_API_KEY - returns 0 if that's not
                    # configured) and Yahoo Finance via yfinance (free, no key
                    # needed, but must use the full `ticker` incl. exchange suffix,
                    # not the stripped `keyword` - see get_yahoo_news_score's
                    # docstring for why). Between the two, this now returns a real
                    # signal even for users who've never set up a NewsAPI key.
                    news_score = get_news_score(keyword, api_key=news_api_key or None) + get_yahoo_news_score(ticker)
                else:
                    trend_score = 0
                    news_score = 0

                if enable_social and not attention_lite:
                    social_score, social_detail = social_engine.get_social_score(ticker)
                else:
                    social_score, social_detail = 0, {"message_count": 0, "bullish": 0,
                                                      "bearish": 0, "net_sentiment": 0,
                                                      "provider": "off"}

                # --- Fundamental Engine ---
                # Order matters: quality score must be resolved BEFORE intrinsic
                # value, since the P/E-blend fallback needs it. Every value is computed
                # from the stock's own data; each resolver also reports whether it had
                # to fall back to a default/average (flagged red in the tables).
                quality_score, quality_src, quality_default = resolve_quality_score(
                    ticker, info=info
                )

                # Per-stock overrides (from the single Valuation & FCF panel) win over
                # the global defaults: discount, perpetual, growth, and manual FCF can
                # each be set for this one ticker; anything not overridden uses the
                # global value (growth override 0/None = per-stock historical estimate).
                _override = fcf_overrides.get(ticker, {})
                _manual_fcf = _override.get("fcf")
                _t_discount = _override.get("discount") or dcf_discount
                _t_perpetual = _override.get("perpetual")
                if _t_perpetual is None:
                    _t_perpetual = dcf_perpetual
                _growth_for_ticker = _override.get("growth")
                if _growth_for_ticker is None:
                    _growth_for_ticker = dcf_growth_override

                cashflow_df = get_cashflow_df(ticker)

                intrinsic_value, intrinsic_src, dcf_growth, iv_meta = resolve_intrinsic_value(
                    ticker, quality_score, info=info, cashflow_df=cashflow_df,
                    currency=info.get("currency"),
                    discount_rate=_t_discount, perpetual_rate=_t_perpetual,
                    growth_rate=_growth_for_ticker, manual_fcf=_manual_fcf,
                )
                intrinsic_default = iv_meta.get("value_default", False)
                growth_default = iv_meta.get("growth_default", False)
                growth_source = iv_meta.get("growth_source")
                growth_governor = iv_meta.get("growth_governor") or "-"

                stock_type, stock_type_src, type_default = resolve_stock_type(
                    ticker, info=info
                )

                # Auto-reclassify as a Turnaround if the stock is trading near its
                # 52-week low - but never override a manual classification.
                if stock_type_src == "auto":
                    fifty_two_wk_low = info.get("fiftyTwoWeekLow", 0) or 0
                    if fifty_two_wk_low > 0 and current_price <= fifty_two_wk_low * 1.15:
                        stock_type = "TURNAROUND"

                holding_period = get_holding_period(stock_type)

                if intrinsic_value > 0:
                    margin_of_safety = ((intrinsic_value - current_price) / intrinsic_value) * 100
                else:
                    margin_of_safety = 0

                entry_price = round(intrinsic_value * 0.8, 2)
                target_price = intrinsic_value

                if current_price > 0 and target_price > 0:
                    upside_percent = ((target_price - current_price) / current_price) * 100
                else:
                    upside_percent = 0

                if len(_close_series) >= 6 and _close_series.iloc[-6] != 0:
                    weekly_change = ((current_price - _close_series.iloc[-6]) / _close_series.iloc[-6]) * 100
                else:
                    weekly_change = 0

                fomo_score = max(greed_score + max(weekly_change, 0), 0)
                psychology_score = fear_score - greed_score - fomo_score

                activity_score = abs(weekly_change)

                _vol_series = window_3mo["Volume"].dropna()
                avg_volume = float(_vol_series.mean()) if len(_vol_series) else 0.0
                latest_volume = float(_vol_series.iloc[-1]) if len(_vol_series) else 0.0
                volume_ratio = (latest_volume / avg_volume) if avg_volume > 0 else 0

                # Discovery is now PURE ATTENTION - "is the market noticing this
                # stock?" - and deliberately no longer includes Psychology. Mixing
                # a directional sentiment signal into an attention score meant a
                # heavily-watched stock could still score negative just because
                # sentiment leaned greedy/fearful, which made Discovery hard to
                # read. Sentiment now lives entirely in its own Psychology score
                # and the Sentiment column below. Social buzz (StockTwits) is
                # attention too, so it joins Discovery when enabled.
                discovery_score = (
                    activity_score
                    + (volume_ratio * 10)
                    + trend_score
                    + news_score
                    + social_score
                )

                long_score = calculate_long_score(
                    quality_score, margin_of_safety, psychology_score, discovery_score
                )

                # --- Valuation label: driven by MOS, audited ------------------------
                # Old mapping gated UNDERVALUED behind Quality >= 60, so RYM (MOS
                # 84.88, Quality 55) and REG (MOS 65.31) were mislabelled FAIR despite
                # a huge margin of safety. Valuation is a statement about PRICE vs
                # VALUE, so it now keys off MOS alone (Quality already has its own
                # column and its own weight in the Long Score). If we couldn't compute
                # an intrinsic value at all, MOS is meaningless -> N/A.
                if intrinsic_value <= 0:
                    valuation = "N/A"
                elif margin_of_safety >= 25:
                    valuation = "UNDERVALUED"
                elif margin_of_safety < 0:
                    valuation = "EXPENSIVE"
                else:
                    valuation = "FAIR"

                # --- Investment Signal (business/value axis) ------------------------
                # This answers "is this a good business to OWN?" It is deliberately
                # SEPARATE from the Trade Setup below, which answers "is right now a
                # sane ENTRY?" A stock can be a good long-term investment yet a poor
                # entry today - the two are shown side by side rather than as one
                # contradictory verdict.
                if long_score > SIGNAL_THRESHOLDS["STRONG_LONG"]:
                    investment_signal = "STRONG LONG"
                elif long_score > SIGNAL_THRESHOLDS["LONG"]:
                    investment_signal = "LONG"
                elif long_score > SIGNAL_THRESHOLDS["WATCHLIST"]:
                    investment_signal = "WATCHLIST"
                else:
                    investment_signal = "AVOID"

                # N/A-valuation rule: if we could not verify value (no intrinsic
                # estimate), the stock CANNOT earn a full LONG/STRONG LONG call - it is
                # capped at WATCHLIST, because a core pillar of the thesis is unproven.
                if valuation == "N/A" and investment_signal in ("STRONG LONG", "LONG"):
                    investment_signal = "WATCHLIST"
                    valuation_capped = True
                else:
                    valuation_capped = False

                # Sentiment (from Psychology) answers "which way is the crowd
                # leaning?" - independent of value.
                if psychology_score > 20:
                    sentiment = "FEARFUL"          # contrarian-friendly
                elif psychology_score > 5:
                    sentiment = "CALM"
                elif psychology_score < -20:
                    sentiment = "OVERHEATED"
                elif psychology_score < -5:
                    sentiment = "GREEDY"
                else:
                    sentiment = "NEUTRAL"

                thesis = generate_thesis(
                    ticker=ticker,
                    stock_type=stock_type,
                    quality_score=quality_score,
                    margin_of_safety=margin_of_safety,
                    psychology_score=psychology_score,
                    discovery_score=discovery_score,
                    long_score=long_score,
                    holding_period=holding_period,
                )
                thesis_lookup[ticker] = thesis

                # --- Trade Filter Engine ---
                sr = trade_filter_engine.calc_support_resistance(df["Close"])

                # Technical indicators computed HERE (moved up from below) because the
                # trade filter's new trend safety gate needs the trend classification.
                # Still cheap - pure pandas on data already fetched - and reused by the
                # swing calcs further down.
                ind = indicators_engine.compute_indicators(df)

                trade_result = trade_filter_engine.evaluate_trade(
                    current_price=current_price,
                    ma50=ma50,
                    support20=sr["support20"],
                    resistance20=sr["resistance20"],
                    support60=sr["support60"],
                    resistance60=sr["resistance60"],
                    long_score=long_score,
                    psychology_score=psychology_score,
                    discovery_score=discovery_score,
                    fomo_score=fomo_score,
                    greed_score=greed_score,
                    trend=ind["trend"],
                )
                trade_lookup[ticker] = trade_result
                trade_setup_score, _ = trade_filter_engine.score_trade_setup(
                    trade_result, psychology_score, discovery_score, ma50, current_price,
                )
                trade_score_lookup[ticker] = trade_setup_score

                # --- Swing calcs (indicators already computed above) ---
                trader_score = swing_engine.calculate_trader_score(
                    psychology_score=psychology_score,
                    discovery_score=discovery_score,
                    rsi_value=ind["rsi"],
                    trend=ind["trend"],
                    macd_cross=ind["macd_cross"],
                    macd_hist=ind["macd"] if ind["macd"] is None else (ind["macd"] - (ind["macd_signal"] or 0)),
                    quality_score=quality_score,
                    margin_of_safety=margin_of_safety,
                )

                # --- Swing Trade Setup (dedicated, ATR-based, graded) ---------------
                # A swing-specific BUY/WATCHLIST/AVOID computed on ONE coherent basis:
                # entry = current price, stop = entry - 2*ATR, targets = R-multiples,
                # verdict from a graded Setup Score. This is what the Swing table uses
                # instead of the investment trade filter (which was gating swing trades
                # on the Long Score and rejecting every uptrend).
                macd_hist_val = (
                    None if ind["macd"] is None
                    else (ind["macd"] - (ind["macd_signal"] or 0))
                )
                swing_result = swing_engine.swing_setup(
                    current_price=current_price,
                    ma20=ind["ma20"],
                    ma50=ind["ma50"],
                    atr_value=ind["atr"],
                    rsi_value=ind["rsi"],
                    trend=ind["trend"],
                    trader_score=trader_score,
                    macd_cross=ind["macd_cross"],
                    macd_hist=macd_hist_val,
                    regime=market_regime,
                    resistance20=sr["resistance20"],
                    support20=sr["support20"],
                )

                swing_entry = swing_result["entry"]
                swing_stop = swing_result["stop"]
                sizing = swing_engine.position_size(
                    account_size=account_size,
                    risk_pct=risk_pct,
                    entry_price=swing_entry,
                    stop_price=swing_stop,
                )

                earn_warn, earn_days = swing_engine.earnings_warning(info, hold_days=42)

                market_cap_bucket = (
                    get_market_cap_bucket(ticker, info=info) if include_market_cap else "N/A"
                )

                data.append({

                    "Ticker": ticker,
                    "Type": stock_type,
                    "Type Source": stock_type_src,
                    "Holding Period": holding_period,
                    "Market Cap": market_cap_bucket,

                    "Price": round(current_price, 2),

                    "Quality": quality_score,
                    "Quality Source": quality_src,

                    "Intrinsic Value": intrinsic_value if intrinsic_value > 0 else "N/A",
                    "Intrinsic Source": intrinsic_src,
                    "Val Method": intrinsic_src.upper() if (intrinsic_src and intrinsic_value > 0) else "-",
                    "DCF Growth %": round(dcf_growth * 100, 1) if dcf_growth is not None else "-",
                    "Growth Governor": growth_governor,
                    "DCF Discount %": (
                        round(iv_meta.get("discount_rate_used") * 100, 1)
                        if iv_meta.get("discount_rate_used") is not None else "-"
                    ),
                    "DCF Perpetual %": (
                        round(iv_meta.get("perpetual_rate_used") * 100, 1)
                        if iv_meta.get("perpetual_rate_used") is not None else "-"
                    ),
                    "MOS": round(margin_of_safety, 2) if intrinsic_value > 0 else "N/A",
                    "IV/Price Multiple": (
                        round(intrinsic_value / current_price, 2)
                        if intrinsic_value > 0 and current_price > 0 else "N/A"
                    ),

                    # Per-cell default flags -> rendered in red in the tables. A True
                    # here means the value rests on an assumed average rather than
                    # sourced data.
                    "_flag_type": bool(type_default),
                    "_flag_quality": bool(quality_default),
                    "_flag_intrinsic": bool(intrinsic_default),
                    "_flag_growth": bool(growth_default),

                    "Entry": entry_price,
                    "Target": target_price,
                    "Upside %": round(upside_percent, 2),

                    "Fear": round(fear_score, 2),
                    "Greed": round(greed_score, 2),
                    "FOMO": round(fomo_score, 2),

                    "Psychology": round(psychology_score, 2),
                    "Activity": round(activity_score, 2),
                    "Volume Ratio": round(volume_ratio, 2),

                    "Trend Score": trend_score,
                    "News Score": news_score,

                    "Social Score": round(social_score, 2),
                    "Social Msgs": social_detail["message_count"],
                    "Social Net": social_detail["net_sentiment"],

                    "Discovery": round(discovery_score, 2),

                    "Long Score": round(long_score, 2),

                    # Investment Signal = business/value axis ("good to OWN?").
                    "Investment Signal": investment_signal,
                    "Val Capped": "Yes" if valuation_capped else "No",

                    "Valuation": valuation,
                    "Sentiment": sentiment,

                    # Trade Setup = tactical entry axis ("sane ENTRY right now?"),
                    # deliberately separate from the Investment Signal so a good
                    # investment with a poor entry reads clearly instead of looking
                    # like two contradictory verdicts.
                    "Trade Setup": trade_result["signal"],
                    "RR1": trade_result["rr1"] if trade_result["rr1"] is not None else "-",

                    # --- Swing-mode fields (populated always, shown only in Swing) ---
                    "Trader Score": trader_score,
                    "Trend": ind["trend"].title(),
                    "RSI": ind["rsi"] if ind["rsi"] is not None else "-",
                    "MACD Cross": ind["macd_cross"],

                    # Swing Trade Setup - all on one coherent ATR basis.
                    "Swing Setup": swing_result["signal"],
                    "Setup Score": swing_result["setup_score"],
                    "Swing Entry": swing_result["entry"],
                    "Swing Stop": swing_result["stop"],
                    "Swing T1": swing_result["target1"] if swing_result["target1"] is not None else "-",
                    "Swing T2": swing_result["target2"] if swing_result["target2"] is not None else "-",
                    "Swing RR": swing_result["rr1"] if swing_result["rr1"] is not None else "-",

                    "ATR Stop": swing_stop,
                    "Shares": sizing["shares"],
                    "Capital At Risk": sizing["dollar_risk"],
                    "Position Value": sizing["position_value"],
                    "Regime": market_regime,
                    "Earnings (days)": earn_days if earn_days is not None else "-",
                    "Earnings Warn": "Yes" if earn_warn else "No",

                })

            except Exception as e:
                st.error(f"Error on {ticker}: {e}")

        if progress_bar is not None:
            progress_bar.empty()

        if _need_fresh_scan:
            # Cache everything the expensive loop just produced under THIS scan's
            # own cache key (industry vs stock - see _active_cache_name above), so
            # a later unrelated rerun (e.g. clicking something in the Auto-Trading
            # tab, or switching tabs) can redraw these exact results instantly
            # instead of re-scanning from scratch - and so Stock Scanner and
            # Stock Comparison never overwrite each other's results.
            st.session_state[_cache_key] = {
                "data": data,
                "thesis_lookup": thesis_lookup,
                "trade_lookup": trade_lookup,
                "trade_score_lookup": trade_score_lookup,
                "market_regime": market_regime,
                "regime_detail": regime_detail,
                "stocks": stocks,
                "universe_source": universe_source,
                "scan_country": scan_country,
            }
            if _active_cache_name:
                st.session_state["last_scan_type"] = _active_cache_name
        else:
            _cached_scan = st.session_state[_cache_key]
            data = _cached_scan["data"]
            thesis_lookup = _cached_scan["thesis_lookup"]
            trade_lookup = _cached_scan["trade_lookup"]
            trade_score_lookup = _cached_scan.get("trade_score_lookup", {})
            market_regime = _cached_scan["market_regime"]
            regime_detail = _cached_scan["regime_detail"]
            st.caption(
                f"Showing {page_label}'s last completed results - something "
                "on the page (e.g. saving a DCF override) triggered a "
                "refresh, not a new scan. Search again (or click Run Scan) "
                "for fresh live prices."
            )


        # -----------------------------------
        # RESULTS
        # -----------------------------------

        def render_thesis(thesis, heading_level="####"):
            st.markdown(f"{heading_level} {thesis['ticker']} - {thesis['stock_type']}")

            st.markdown("**Why Buy**")
            for point in thesis["why_buy"]:
                st.markdown(f"- {point}")

            st.markdown("**Why Wait**")
            for point in thesis["why_wait"]:
                st.markdown(f"- {point}")

            st.markdown("**Risks**")
            for point in thesis["risks"]:
                st.markdown(f"- {point}")

            st.markdown(f"**Suggested Holding Period:** {thesis['holding_period']}")




        if len(data) > 0:

            results = pd.DataFrame(data)

            # The mode decides the headline ranking metric: Trader Score for swing,
            # Long Score for long-term investment.
            sort_key = "Trader Score" if is_swing else "Long Score"
            results = results.sort_values(sort_key, ascending=False).reset_index(drop=True)

            # Remember what we scanned so the "Manual FCF override" dropdown can offer
            # exactly these tickers on the next run.
            st.session_state["scanned_tickers"] = list(results["Ticker"])

            # MOS may now be the string "N/A" for names with no intrinsic estimate, so
            # use a numeric view wherever we compare/aggregate on it.
            results["_mos_num"] = pd.to_numeric(results["MOS"], errors="coerce")

            # Free preview: identity + headline signal only, for every ticker
            # scanned - Ticker/Price plus Trader Score (swing) or Long Score +
            # Investment Signal (the normal long-term mode, the only one this
            # public deployment actually uses now that Auto-Trading/swing
            # scanning were removed - the is_swing branch is kept only in case
            # that ever comes back). Everything past this point (Sector
            # Rankings, Top Candidate detail, the Trade Setup table, and the
            # full multi-column results table further down) is the paid
            # detail - gated as a single block below rather than picked apart
            # column-by-column, since that would mean re-auditing every table
            # on this 1000+ line page for a stray un-gated column.
            _preview_cols = ["Ticker", "Price"] + (
                ["Trader Score"] if is_swing and "Trader Score" in results.columns
                else ["Long Score", "Investment Signal"]
            )
            _preview_cols = [c for c in _preview_cols if c in results.columns]
            st.subheader(f"{page_label} preview")
            _prev_rows = []
            for _, _pr in results.iterrows():
                _prev_rows.append(
                    "<tr>"
                    + _td(f"<b>{_pr['Ticker']}</b>")
                    + _td(f"{_pr['Price']:,.2f}")
                    + _td(_bar_cell(_pr.get("Long Score"), SIGNAL_THRESHOLDS["WATCHLIST"],
                                    SIGNAL_THRESHOLDS["LONG"]), minw=110)
                    + _td(_badge_cell(_pr.get("Investment Signal", "-")))
                    + "</tr>"
                )
            st.markdown(
                _sdd_table(["Ticker", "Price", "Long Score", "Investment Signal"],
                           _prev_rows, max_height=420),
                unsafe_allow_html=True,
            )

            if not paywall_engine.render_gate(
                f"the full {page_label} results",
                teaser=(
                    "Valuation (Intrinsic Value, MOS), Quality, Psychology, "
                    "Discovery, and Trade Setup detail for every stock above."
                ),
                key_prefix=state_prefix,
            ):
                return

            # ---------------- Side-by-side comparison ----------------
            # One row per ticker, the columns you actually asked for. Score
            # columns render as a colored bar (red/amber/green) instead of a
            # plain number - thresholds reuse the SAME cutoffs the rest of the
            # app already verdicts on (SIGNAL_THRESHOLDS for Long Score, the
            # Valuation label's MOS>=25 cutoff, the Trade Setup engine's
            # WATCHLIST/BUY gates), not new arbitrary numbers.
            st.subheader("Side-by-side comparison")
            st.caption(
                "The scanned names lined up for a direct read, in the order you "
                "entered them."
            )

            _cmp = results.copy()
            _cmp["Trade Setup Score"] = _cmp["Ticker"].map(trade_score_lookup)

            _cmp_rows_html = []
            for _, r in _cmp.iterrows():
                _type_color = _BAR_RED if r.get("_flag_type") else _TYPE_NEUTRAL
                _cmp_rows_html.append(
                    "<tr>"
                    + _td(f"<b>{r['Ticker']}</b>")
                    + _td(_badge_cell(r["Type"], _type_color))
                    + _td(f"{r['Price']:,.2f}")
                    + _td(_money_cell(r["Intrinsic Value"], ref=r["Price"],
                                      flag=bool(r.get("_flag_intrinsic"))))
                    + _td(_bar_cell(r["MOS"], 0, 25, "%",
                                    flag=bool(r.get("_flag_intrinsic"))), minw=90)
                    + _td(_bar_cell(r["Long Score"], SIGNAL_THRESHOLDS["WATCHLIST"],
                                    SIGNAL_THRESHOLDS["LONG"]), minw=90)
                    + _td(_bar_cell(r["Quality"], 40, 80,
                                    flag=bool(r.get("_flag_quality"))), minw=90)
                    + _td(_bar_cell(r["Psychology"], -5, 20), minw=90)
                    + _td(_bar_cell(r["Discovery"], 25, 50), minw=90)
                    + _td(_badge_cell(r["Valuation"]))
                    + _td(_badge_cell(r["Sentiment"]))
                    + _td(_badge_cell(r["Trend"]))
                    + _td(_badge_cell(r["Trade Setup"]))
                    + _td(_bar_cell(r["Trade Setup Score"], 45, 65), minw=90)
                    + "</tr>"
                )

            _cmp_headers = [
                "Ticker", "Type", "Price", "Intrinsic Value", "MOS",
                "Long Score", "Quality Score", "Psychology", "Discovery",
                "Valuation", "Sentiment", "Trend", "Trade Setup",
                "Trade Setup Score",
            ]
            st.markdown(_sdd_table(_cmp_headers, _cmp_rows_html), unsafe_allow_html=True)


            if is_swing and market_regime != "UNKNOWN":
                if market_regime == "RISK-ON":
                    st.success(f"Market Regime: {market_regime} - {regime_detail}")
                else:
                    st.warning(f"Market Regime: {market_regime} - {regime_detail}. Be selective; broad trend is down.")

            sector_summary = (
                results.groupby("Type")[sort_key].mean()
                .reset_index().sort_values(sort_key, ascending=False)
            )

            # INVESTMENT RANKING now EXCLUDES stocks with no verifiable valuation
            # (Valuation == N/A). Previously the N/A cap only changed the label, so a
            # name like MSB - Quality inflated, no intrinsic value, MOS treated as 0 -
            # could still SORT into the Top 10. A stock whose value can't be checked
            # can't be an Investment recommendation, so it's held out of the ranked
            # tables and shown separately below. (Swing mode is unaffected - it ranks
            # on the Trader Score.)
            if is_swing:
                ranked = results
                unvalued = results.iloc[0:0]
            else:
                unvalued = results[results["Valuation"] == "N/A"]
                _investable = results[results["Valuation"] != "N/A"]
                ranked = _investable if len(_investable) else results

            top_stock = ranked.iloc[0]

            if is_swing:
                best_trader = results.loc[results["Trader Score"].idxmax()]
                best_rsi = results.loc[pd.to_numeric(results["RSI"], errors="coerce").idxmin()] \
                    if pd.to_numeric(results["RSI"], errors="coerce").notna().any() else top_stock
                col1, col2, col3 = st.columns(3)
                col1.metric("Best Trader Score", best_trader["Ticker"])
                col2.metric("Most Oversold (RSI)", best_rsi["Ticker"])
                col3.metric("Market Regime", market_regime)
            else:
                best_quality = ranked.loc[ranked["Quality"].idxmax()]
                best_long = ranked.loc[ranked["Long Score"].idxmax()]
                col1, col2, col3 = st.columns(3)
                col1.metric("Highest Quality", best_quality["Ticker"])
                _mos_ranked = pd.to_numeric(ranked["MOS"], errors="coerce")
                if _mos_ranked.notna().any():
                    col2.metric("Highest MOS", ranked.loc[_mos_ranked.idxmax()]["Ticker"])
                else:
                    col2.metric("Highest MOS", "-")
                col3.metric("Best Long Score", best_long["Ticker"])

            # Only meaningful once there are enough names for a group
            # average to say anything (it groups by TYPE, not GICS sector).
            if len(results) >= 5:
                st.subheader("Average score by stock type")
                st.dataframe(sector_summary, width="stretch", hide_index=True)

            if is_swing:
                st.subheader("Top Swing Candidate")
                st.success(
                    f"{top_stock['Ticker']} - Trader Score: {top_stock['Trader Score']} "
                    f"({top_stock['Trend']}, RSI {top_stock['RSI']}, "
                    f"Setup {top_stock['Setup Score']} -> {top_stock['Swing Setup']})"
                )
            else:
                st.subheader("Top Investment Candidate")
                st.success(
                    f"{top_stock['Ticker']} - Long Score: {top_stock['Long Score']} "
                    f"(Investment: {top_stock['Investment Signal']}, "
                    f"Trade Setup: {top_stock['Trade Setup']})"
                )
            render_thesis(thesis_lookup[top_stock["Ticker"]])

            if not is_swing:
                st.info(
                    "**Two independent axes.** *Investment Signal* "
                    "(LONG / WATCHLIST / AVOID) answers \"is this a good business to "
                    "own?\" *Trade Setup* (BUY / WATCHLIST / AVOID) answers \"is right "
                    "now a sane entry?\" A stock can be a strong investment yet a poor "
                    "entry today - that is not a contradiction. Numbers shown in "
                    "**red** are default/average assumptions (data wasn't available); "
                    "everything else is computed from the stock's own sourced data."
                )

            # Top 10 / Unvalued-speculative tables removed for the public
            # Comparison tab - not meaningful for a small hand-picked ticker
            # list (see the Side-by-side comparison table below instead).

            # ---------------- Trade Setup table (full scanned list) ----------------
            with st.expander("Trade Setup - technical entry / stop-loss / targets per stock"):
                st.caption(
                    "Tactical entry layer for a long-term position - SEPARATE from the "
                    "Investment Signal. This is about entry timing, not business quality. "
                    "Every scanned stock is listed here, not just the top 10. "
                    "BUY requires: NOT in a downtrend, Psychology > 0, Discovery > 0, "
                    "Price <= MA50 x 1.05, price near the Entry Zone, and RR1 >= 1.5. "
                    "(The old Long Score >= 60 gate has been removed - business quality is "
                    "judged separately by the Investment Signal, and a confirmed downtrend "
                    "now replaces it as the safety filter so you don't buy a falling knife.) "
                    "This Entry Zone/Stop Loss/Targets come from the Trade Filter engine "
                    "(support/resistance based) - a different, independent number from "
                    "the DCF valuation Entry/Target ('Full Stock Database' below) and "
                    "the ATR-based Swing Entry/Stop ('Swing Setup' further down). "
                    "'Trade Setup Score' (0-100) is a visual weighting of the same "
                    "gate checks behind the BUY/WATCHLIST/AVOID verdict - Trend Safety "
                    "20pts, Near Entry Zone 20pts, Risk/Reward 20pts, Price vs MA50 "
                    "15pts, Psychology Momentum 12.5pts, Discovery Momentum 12.5pts - "
                    "not a re-implemented trade formula, just the same verdict shown "
                    "as a number. Hover any column header for details."
                )
                _price_by_ticker = dict(zip(results["Ticker"], results["Price"]))
                _t_rows_data = []
                for _, row in results.iterrows():
                    t = trade_lookup.get(row["Ticker"])
                    if t:
                        _t_rows_data.append((row["Ticker"], t))

                # RR columns are coloured by RANK across the rows on screen
                # (best green / worst red / rest amber) - "good risk/reward"
                # is relative to the other setups in front of you.
                _rr1_vals = [t["rr1"] for _, t in _t_rows_data]
                _rr2_vals = [t["rr2"] for _, t in _t_rows_data]
                _rr3_vals = [t["rr3"] for _, t in _t_rows_data]

                _trade_rows_html = []
                for _tk, t in _t_rows_data:
                    _cur = _price_by_ticker.get(_tk)
                    # Entry zone: green when the price is at/inside the zone
                    # (a reachable entry NOW), red when the price is still
                    # above it (you'd be paying up - wait for the pullback).
                    _near = bool(t.get("near_entry_zone"))
                    _ez_color = _BAR_GREEN if _near else _BAR_RED
                    _ez_cell = (
                        f"<span style='color:{_ez_color};font-weight:600;'>"
                        f"{t['entry_zone']:,.2f}</span>"
                    )
                    _trade_rows_html.append(
                        "<tr>"
                        + _td(f"<b>{_tk}</b>")
                        + _td(_badge_cell(t["signal"]))
                        + _td(_bar_cell(trade_score_lookup.get(_tk), 45, 65), minw=90)
                        + _td(_badge_cell(str(t.get("trend", "-")).title()))
                        + _td(_ez_cell)
                        + _td(f"{t['stop_loss']:,.2f}")
                        + _td(f"{t['target1']:,.2f}")
                        + _td(f"{t['target2']:,.2f}")
                        + _td(f"{t['target3']:,.2f}" if t["target3"] is not None else "-")
                        + _td(f"{t['risk']:,.2f}")
                        + _td(_rank_cell(t["rr1"], _rr1_vals))
                        + _td(_rank_cell(t["rr2"], _rr2_vals))
                        + _td(_rank_cell(t["rr3"], _rr3_vals))
                        + _td(_badge_cell(t["early_exit_watch"] and "Yes" or "No"))
                        + "</tr>"
                    )
                st.markdown(
                    _sdd_table(
                        ["Ticker", "Trade Setup", "Setup Score", "Trend",
                         "Entry Zone", "Stop Loss", "Target 1", "Target 2",
                         "Target 3", "Risk", "RR1", "RR2", "RR3", "Early Exit"],
                        _trade_rows_html, max_height=480,
                    ),
                    unsafe_allow_html=True,
                )
                st.caption(
                    "Entry Zone: green = price is at/inside the zone now, red = "
                    "price is still above it. RR columns: green = best "
                    "risk/reward on screen, red = worst, amber between."
                )

            with st.expander("Position management & early-exit rules (apply after entry)"):
                st.markdown("**As each target is hit:**")
                for note in trade_filter_engine.position_management_notes():
                    st.markdown(f"- {note}")
                st.markdown("**Exit the entire position early if:**")
                for note in trade_filter_engine.early_exit_notes():
                    st.markdown(f"- {note}")

            # ---------------- Swing Setup table (full scanned list) ----------------
            # These fields are computed for every scan regardless of Strategy Mode
            # (see the scan loop's "Swing-mode fields" block) - broken out into
            # their own table, same treatment as Trade Setup above, instead of
            # living inside 'Full Stock Database' where they used to sit mixed in
            # alongside the DCF/Long-Score columns.
            # Swing Setup table removed for the public Comparison tab (see
            # Side-by-side comparison below instead).

            st.subheader("Opportunity Details")
            for _, row in ranked.head(5).iterrows():
                with st.expander(
                    f"{row['Ticker']} - Long Score {row['Long Score']} "
                    f"(Investment: {row['Investment Signal']} | Trade Setup: {row['Trade Setup']})"
                ):
                    if row["Val Capped"] == "Yes":
                        st.caption(
                            "Investment Signal capped at WATCHLIST: no intrinsic value "
                            "could be computed, so valuation is unverified."
                        )
                    st.write(
                        f"Quality: {row['Quality']}  |  MOS: {row['MOS']}%  |  "
                        f"Valuation: {row['Valuation']}  |  Sentiment: {row['Sentiment']}"
                    )
                    _growth_note = (
                        f", DCF growth {row['DCF Growth %']}%"
                        if row['Val Method'] == 'DCF' else ""
                    )
                    st.write(
                        f"Intrinsic Value: {row['Intrinsic Value']}  "
                        f"(method: {row['Val Method']}{_growth_note})"
                    )
                    # Discovery is pure attention; show what's driving it (incl. social).
                    st.write(
                        f"Discovery (attention): {row['Discovery']}  =  "
                        f"Activity {row['Activity']} + Volume x10 ({row['Volume Ratio']}x) "
                        f"+ Trend {row['Trend Score']} + News {row['News Score']} "
                        f"+ Social {row['Social Score']}"
                    )
                    st.write(
                        f"Social (StockTwits): {row['Social Msgs']} messages, "
                        f"net {row['Social Net']:+d} bull/bear"
                    )
                    st.write(
                        f"Psychology (sentiment): {row['Psychology']}  |  "
                        f"Trade Setup: {row['Trade Setup']}  |  RR1: {row['RR1']}"
                    )
                    render_thesis(thesis_lookup[row["Ticker"]], heading_level="#####")

            # Full Stock Database removed - the per-column detail lives in each
            # stock's Deep Dive instead of one giant table here.

            with st.expander("DCF Parameters (Growth / Discount / Perpetual) - view or override"):
                st.caption(
                    "The growth, discount and terminal-growth rate this scan actually "
                    "used for each stock - auto-calculated (CAPM / analyst-consensus / "
                    "currency-based, or the global defaults above), or your saved "
                    "override - alongside the resulting Intrinsic Value and the latest "
                    "Price, for a quick read of which stocks the DCF calls under/over "
                    "the market. Click \"Enable manual override\" to type your own "
                    "value for any stock - leave a cell blank to keep it auto. Saved "
                    "overrides apply only to your own session - they reset to "
                    "defaults the moment you leave or reload this page, and never "
                    "affect what any other visitor sees."
                )
                st.caption(
                    "\"IV/Price Multiple\" is Intrinsic Value divided by Price (e.g. 1.50 "
                    "means the DCF values the stock at 1.5x today's price; below 1.00 "
                    "means the DCF calls it overvalued). \"Long Score\" is color-coded "
                    "using the SAME gates that set the Investment Signal everywhere else "
                    f"in this app: green = Long Score above {SIGNAL_THRESHOLDS['LONG']} "
                    f"(LONG / STRONG LONG territory), yellow = above "
                    f"{SIGNAL_THRESHOLDS['WATCHLIST']} but at or below {SIGNAL_THRESHOLDS['LONG']} "
                    f"(WATCHLIST), red = {SIGNAL_THRESHOLDS['WATCHLIST']} or below (AVOID)."
                )

                _dcf_params_df = results[
                    ["Ticker", "Price", "Intrinsic Value", "IV/Price Multiple", "Upside %",
                     "DCF Growth %", "Growth Governor", "DCF Discount %", "DCF Perpetual %",
                     "Long Score"]
                ].copy()

                st.session_state.setdefault("scanner_override_mode", False)
                _mode_label = ("Hide manual override" if st.session_state["scanner_override_mode"]
                               else "Enable manual override")
                if st.button(_mode_label, key="toggle_scanner_override"):
                    st.session_state["scanner_override_mode"] = not st.session_state["scanner_override_mode"]
                    st.rerun()

                if not st.session_state["scanner_override_mode"]:
                    _dcf_rows_html = []
                    for _, _dr in results.iterrows():
                        _ivm = _dr["IV/Price Multiple"]
                        try:
                            _ivm_v = float(_ivm)
                            _ivm_c = (_BAR_GREEN if _ivm_v > 1.0
                                      else _BAR_RED if _ivm_v < 1.0 else _BAR_AMBER)
                            _ivm_cell = (f"<span style='color:{_ivm_c};font-weight:600;'>"
                                         f"{_ivm_v:,.2f}x</span>")
                        except (TypeError, ValueError):
                            _ivm_cell = "<span style='color:#8aa0b8;'>N/A</span>"
                        _dcf_rows_html.append(
                            "<tr>"
                            + _td(f"<b>{_dr['Ticker']}</b>")
                            + _td(f"{_dr['Price']:,.2f}")
                            + _td(_money_cell(_dr["Intrinsic Value"], ref=_dr["Price"],
                                              flag=bool(_dr.get("_flag_intrinsic"))))
                            + _td(_ivm_cell)
                            + _td(_signed_cell(_dr.get("Upside %")))
                            + _td(
                                _money_cell(_dr["DCF Growth %"],
                                            flag=bool(_dr.get("_flag_growth")),
                                            fmt="{:,.1f}%")
                                if _dr["DCF Growth %"] != "-" else "-"
                            )
                            + _td(str(_dr.get("Growth Governor", "-")))
                            + _td(f"{_dr['DCF Discount %']}" + ("%" if _dr["DCF Discount %"] != "-" else ""))
                            + _td(f"{_dr['DCF Perpetual %']}" + ("%" if _dr["DCF Perpetual %"] != "-" else ""))
                            + _td(_bar_cell(_dr["Long Score"], SIGNAL_THRESHOLDS["WATCHLIST"],
                                            SIGNAL_THRESHOLDS["LONG"]), minw=90)
                            + "</tr>"
                        )
                    st.markdown(
                        _sdd_table(
                            ["Ticker", "Price", "Intrinsic Value", "IV/Price",
                             "Upside %", "DCF Growth", "Governor", "Discount",
                             "Perpetual", "Long Score"],
                            _dcf_rows_html, max_height=480,
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    _editor_rows = []
                    for _, _row in _dcf_params_df.iterrows():
                        _tk = _row["Ticker"]
                        _ov = st.session_state["fcf_overrides"].get(_tk, {})
                        _editor_rows.append({
                            "Ticker": _tk,
                            "Price": _row["Price"],
                            "Intrinsic Value (calc)": _row["Intrinsic Value"],
                            "Growth % (calc)": _row["DCF Growth %"],
                            "Growth governed by": _row["Growth Governor"],
                            "Discount % (calc)": _row["DCF Discount %"],
                            "Perpetual % (calc)": _row["DCF Perpetual %"],
                            "Growth % override": (
                                round(_ov["growth"] * 100, 2) if _ov.get("growth") is not None else None
                            ),
                            "Discount % override": (
                                round(_ov["discount"] * 100, 2) if _ov.get("discount") is not None else None
                            ),
                            "Perpetual % override": (
                                round(_ov["perpetual"] * 100, 2) if _ov.get("perpetual") is not None else None
                            ),
                        })
                    _editor_df = pd.DataFrame(_editor_rows)
                    _edited = st.data_editor(
                        _editor_df,
                        width="stretch",
                        hide_index=True,
                        disabled=["Ticker", "Price", "Intrinsic Value (calc)",
                                  "Growth % (calc)", "Discount % (calc)", "Perpetual % (calc)"],
                        column_config={
                            "Growth % override": st.column_config.NumberColumn(
                                help="Blank = stay auto/calculated.", step=0.5, format="%.2f"),
                            "Discount % override": st.column_config.NumberColumn(
                                help="Blank = stay auto/calculated.", step=0.1, format="%.2f"),
                            "Perpetual % override": st.column_config.NumberColumn(
                                help="Blank = stay auto/calculated.", step=0.1, format="%.2f"),
                        },
                        key="dcf_override_editor",
                    )

                    bcol1, bcol2 = st.columns(2)
                    with bcol1:
                        if st.button("Save overrides", type="primary", key="save_scanner_overrides"):
                            _saved = 0
                            for _, _r in _edited.iterrows():
                                _tk = _r["Ticker"]
                                _g_ov = _r["Growth % override"]
                                _d_ov = _r["Discount % override"]
                                _p_ov = _r["Perpetual % override"]
                                _has_any = any(v is not None and v == v for v in (_g_ov, _d_ov, _p_ov))
                                _existing_fcf = st.session_state["fcf_overrides"].get(_tk, {}).get("fcf")
                                if _has_any:
                                    _growth = (_g_ov / 100.0) if (_g_ov is not None and _g_ov == _g_ov) else None
                                    _discount = (_d_ov / 100.0) if (_d_ov is not None and _d_ov == _d_ov) else None
                                    _perpetual = (_p_ov / 100.0) if (_p_ov is not None and _p_ov == _p_ov) else None
                                    st.session_state["fcf_overrides"][_tk] = {
                                        "discount": _discount, "perpetual": _perpetual,
                                        "growth": _growth, "fcf": _existing_fcf,
                                    }
                                    _saved += 1
                                elif _tk in st.session_state["fcf_overrides"]:
                                    # Every override cell for this ticker was cleared/left
                                    # blank - drop it back to fully automatic.
                                    st.session_state["fcf_overrides"].pop(_tk, None)
                            st.success(
                                f"Saved {_saved} override(s) for this session - click "
                                "Run Comparison again to recalculate every table with "
                                "them. These reset to defaults once you leave the page."
                            )
                            st.rerun()
                    with bcol2:
                        if st.button("Reset ALL overrides (back to auto)", key="reset_scanner_overrides"):
                            st.session_state["fcf_overrides"] = {}
                            st.info("All overrides cleared. Click Run Comparison again to recalculate.")
                            st.rerun()

            scan_time = round(time.time() - start_time, 2)
            if _need_fresh_scan:
                st.caption(
                    f"{len(results)} stocks analyzed - live data - "
                    f"completed in {scan_time}s - {universe_source}"
                )

        else:
            st.warning("No stock data returned.")


# -----------------------------------
# CONTENT PAGES - Methodology / About / Privacy
#
# Plain-prose pages that make the site sellable: people subscribe to a
# system they believe they understand, run by a person they trust, with a
# clear statement of what happens to their data. All three are linked from
# the footer on every page.
# -----------------------------------

def _content_page_shell(title):
    _render_header(compact=True)
    st.markdown(f"## {title}")


def page_methodology():
    _content_page_shell("How the scores work")
    st.markdown(
        """
Every tool on this site runs the same engine. A ticker goes in; live data comes back
(prices and volumes, financial statements and analyst estimates via Yahoo Finance, search
interest via Google Trends, headlines via Yahoo/NewsAPI, chatter via StockTwits); and the
same value-investing maths runs every time. Nothing on this page is a black box - every
score's inputs are charted right next to it on the site.

#### The Long Score (0–100)

One number answering "is this a good business to own at this price?" It blends four
factors, each clamped to a fixed band first so no single factor can run away with the
result:

| Factor | Weight | What it measures |
|---|---|---|
| Quality | 35% | Is this a good business? Return on equity, profit margin, revenue and earnings growth, free cash flow, debt - computed from the company's own fundamentals. Loss-making, cash-burning businesses are capped: a company that doesn't make money can't score as "high quality" no matter how fast it grows. |
| Margin of Safety | 25% | Is the price below the value? The gap between our intrinsic-value estimate and today's price, clamped to ±50 so a wild discount (or premium) can move the score but never dominate it. |
| Psychology | 20% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour. Fear scores positively - the value investor's edge is buying quality when others are anxious. |
| Discovery | 20% | Is the market noticing? Price activity, unusual volume, search trends, news flow and social chatter - attention only, deliberately separate from sentiment. |

Above 70 = **STRONG LONG**, above 50 = **LONG**, above 30 = **WATCHLIST**, otherwise
**AVOID**. If no intrinsic value could be computed at all, the signal is capped at
WATCHLIST - a thesis whose value leg can't be verified doesn't get a full
recommendation.

#### Intrinsic value

The primary model is a discounted cash flow built from the company's own reported free
cash flows. The discount rate is calculated per stock (CAPM - the stock's own beta
against its market), growth comes from analyst consensus where available, then the
company's own historical FCF growth, and the terminal growth rate is set by the stock's
currency. Where a DCF isn't possible, a P/E-blend fallback is used and labelled as such.
Margin of Safety = (intrinsic value − price) ÷ intrinsic value.

A stock trading 25%+ below intrinsic value is labelled **UNDERVALUED**; above intrinsic
value, **EXPENSIVE**; between, **FAIR**.

#### Value vs timing - two separate verdicts

The **Investment Signal** answers "good business to own?" The **Trade Setup** answers
"is right now a sane entry?" - support/resistance-based entry zone, stop loss and
targets, gated on trend safety and risk/reward. A great company can be a poor entry
today; the site shows both rather than blurring them into one contradictory verdict.

#### The red-flag rule

Whenever a number rests on a default or average because real data wasn't available, it's
shown in **red**. An estimate is never dressed up as a fact - you always know which
numbers are computed and which are assumed.

#### Rational Compounder Research

The Research section is different: it isn't computed at all. It's the author's own
hand-built workbook analysis of selected quality compounders - a decade of earnings
history, four independent fair-value methods (trailing P/E, forward P/E, DCF, and a
10-year equity method), and written Buffett/Munger-style judgment on management, moat
and risk. Every threshold and colour band on those pages comes from the original
research, not a generic screen.

#### Limitations, honestly

Data is sourced from free public feeds and can be delayed, revised or occasionally
wrong. Intrinsic value is an estimate resting on assumptions - reasonable assumptions,
shown openly, but assumptions. Scores are model outputs, not personal advice, and none
of this considers your circumstances. Use it the way it was built to be used: as the
starting point for your own judgment, not a substitute for it.
"""
    )


def page_about():
    _content_page_shell("About")
    st.markdown(
        """
StocksDeepDive is built and run by **Andres Moreno**, a private investor in Australia.

It didn't start as a website. It started as a personal stock scanner and a very long
Excel workbook - the tools I built to manage my own self-managed super fund with a
Buffett/Munger-style value approach: work out what a business is actually worth, check
its quality like an owner would, and only then look at what the crowd is doing. Over
the years the scanner grew a DCF engine, quality tests, a crowd-psychology read, trade
setups, and a research workbook that interrogates one company for weeks at a time.

At some point the obvious question arrived: if I trust these numbers with my own
retirement savings, why not open them up? So this site is that - the same engine,
the same research, made public.

Two principles carried over from the private version, unchanged:

**The numbers must be honest.** Whenever a figure rests on a default or an average
because real data wasn't available, it's shown in red. An estimate is never dressed up
as a fact. I built that rule for myself, because fooling yourself is expensive - it
applies just as much now that you're reading the numbers too.

**Value and timing are different questions.** Whether a business is worth owning and
whether today is a sane day to buy it get separate verdicts on every page. Most tools
blur them; keeping them apart is half the discipline.

The site is free while it launches. When subscriptions open, founding members keep
launch pricing. If you want a stock added to the Rational Compounder research list, or
anything here doesn't make sense, use the Feedback button on any results page or email
[rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com) -
I read everything.

*Nothing on this site is financial advice - see the disclaimer in the footer. I may
own stocks analysed here.*
"""
    )


def page_privacy():
    _content_page_shell("Privacy policy")
    st.markdown(
        """
*Last updated: 13 August 2026*

StocksDeepDive ("the site", "we") is operated by Andres Moreno in Australia. This page
explains what information the site handles and what happens to it. Contact for anything
privacy-related: [rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com).

#### What we collect

**Nothing, for anonymous browsing.** You can use every analysis tool without an
account. Standard technical logs (IP address, browser type, pages requested) are kept
by our hosting provider (Railway) for security and debugging, as with any website.

**If you sign in with Google:** we receive your name and email address from Google -
nothing else. Sign-in exists so the site can remember your watchlist, attribute your
feedback, and (if you save a watchlist) send you the weekly signal digest email. We
never see your Google password.

**If you save a watchlist:** the tickers you save are stored against your email
address on our server.

**If you send feedback:** your message and, if you're signed in, your email address
are stored so we can follow up.

**If subscriptions are active and you subscribe:** payment is handled entirely by
Stripe. We never see or store your card details - we only check with Stripe whether
your email has an active subscription.

#### What we don't do

No advertising, no ad trackers, no analytics beyond the hosting provider's standard
logs, and no selling or sharing of your information with anyone, ever. The only
cookies used are the ones required to keep you signed in.

#### Emails

The weekly digest is sent (via Mailgun) only to signed-in users who have saved a
watchlist. To stop it, remove all stocks from your watchlist, or email us and we'll
remove you.

#### Data retention and deletion

Watchlists and feedback are kept while your account is active. Email us from your
sign-in address and we will delete everything we hold about you.

#### Third-party data on the site

Market data shown on the site comes from third-party sources (Yahoo Finance, Google
Trends, StockTwits, NewsAPI, GDELT). Those services receive standard requests from our
server, not information about you.

#### Changes

If this policy changes, the date above will change with it. Material changes will be
noted on the site.
"""
    )


# -----------------------------------
# PAGE ROUTING
#
# Real Streamlit pages (st.navigation/st.Page) so Search and Rational
# Compounder Analysis genuinely navigate to a new page - the URL changes
# and the page runs fresh from the top, instead of expanding content below
# the search box on the same page. position="hidden" turns off Streamlit's
# own navigation menu, since the search box in the header is the only
# navigation this site needs.
# -----------------------------------
PG_HOME = st.Page(page_home, title="StocksDeepDive", url_path="", default=True)
PG_DEEP_DIVE = st.Page(page_deep_dive, title="Deep Dive", url_path="deep-dive")
PG_COMPARISON = st.Page(page_comparison, title="Comparison", url_path="comparison")
PG_RESEARCH = st.Page(page_research, title="Rational Compounder Analysis", url_path="research")
PG_SCANNER = st.Page(page_scanner, title="Stock Scanner", url_path="scanner")
PG_METHODOLOGY = st.Page(page_methodology, title="How the scores work", url_path="methodology")
PG_ABOUT = st.Page(page_about, title="About", url_path="about")
PG_PRIVACY = st.Page(page_privacy, title="Privacy policy", url_path="privacy")

_nav = st.navigation(
    [PG_HOME, PG_DEEP_DIVE, PG_COMPARISON, PG_RESEARCH, PG_SCANNER,
     PG_METHODOLOGY, PG_ABOUT, PG_PRIVACY], position="hidden"
)
_nav.run()

# Footer is rendered AFTER st.navigation has run the active page, so it
# appears on every page regardless of early returns/gates inside the page.
_render_footer()
