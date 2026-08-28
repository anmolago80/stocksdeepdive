import streamlit as st
import yfinance as yf
import pandas as pd
import time
import json
import html
import os
import concurrent.futures
import contextlib
from datetime import datetime, timezone, date as _date

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
import email_auth
import feedback_engine
import watchlist_store
import follow_store
import metrics_store
import positions_store
import announce_engine
import scan_store
import scanner_engine
import screen_import_store
import nightly_scan
import portfolio_store
import portfolio_health_engine
import portfolio_news_engine
import name_directory
import score_history
import blog_store
import blog_render
import compounder_ui
import auto_compounder_engine

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
# METRIC HELP (Task 2)
#
# Short, factual explanations shown via st.metric's built-in help= tooltip
# (the little "?" icon) on the Deep Dive page's headline metrics. Purely
# descriptive of how each number is computed - no signal words, no "should"/
# "buy"/"sell"/"recommend" - same wording regardless of FACTUAL_MODE, since
# describing a calculation is not itself the kind of framing FACTUAL_MODE
# gates.
# -----------------------------------

METRIC_HELP = {
    "Price": "The latest closing price used as the input to every calculation below.",
    "Intrinsic Value": (
        "A DCF (discounted cash flow) estimate built from the company's own "
        "historical free cash flow, plus a discount rate and growth "
        "assumption - shown as N/A where no usable estimate could be "
        "computed."
    ),
    "MOS": (
        "Margin of Safety: the percentage gap between Intrinsic Value and "
        "Price. Positive means Price is below the Intrinsic Value estimate; "
        "negative means it's above."
    ),
    "Value Score": (
        "A 0-100 weighted calculation: quality 35%, MOS 25%, psychology "
        "20%, discovery 20%."
    ),
    "Long Score": (
        "A 0-100 weighted calculation: quality 35%, MOS 25%, psychology "
        "20%, discovery 20%."
    ),
    "Signal": (
        "STRONG LONG / LONG / WATCHLIST / AVOID are fixed labels applied to "
        "bands of the Long Score above - a description of where the score "
        "falls, not advice."
    ),
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
# PRESENTATION MODE - factual (public) vs full (admin-only view)
#
# FACTUAL_MODE=true (the default) renders the public site as factual
# information and calculator outputs only: no BUY/AVOID pills, no
# entry/stop/target levels, no verdict words - the engines all still run,
# only what is DISPLAYED changes. Opening the site as
#   https://<site>/?admin=<ADMIN_REFRESH_KEY>
# switches THIS BROWSER SESSION to the full presentation (signals, trade
# setups, verdicts) with a visible "FULL VIEW" badge - nothing on the
# public site reveals that this exists. Set FACTUAL_MODE=false to restore
# the full presentation for everyone (e.g. once operating under an AFSL
# authorised-representative arrangement).
# -----------------------------------
_FACTUAL_DEFAULT = (os.environ.get("FACTUAL_MODE", "true").strip().lower()
                    not in ("false", "0", "no", "off"))

_admin_qp = (st.query_params.get("admin") or "").strip()
_admin_key_env = os.environ.get("ADMIN_REFRESH_KEY", "").strip()


def _admin_cookie_value() -> str:
    """A signed-ish token derived from the admin key - never the key
    itself - stored in a cookie so the unlock survives full page loads
    (typing a URL / opening a new tab), not just in-app navigation."""
    import hashlib
    return hashlib.sha256(f"sdd-fullview:{_admin_key_env}".encode()).hexdigest()[:40]


def _set_admin_cookie(clear: bool = False):
    import streamlit.components.v1 as _components
    if clear:
        _js = "document.cookie='sdd_fullview=; path=/; max-age=0; SameSite=Lax';"
    else:
        _js = (f"document.cookie='sdd_fullview={_admin_cookie_value()}; "
               "path=/; max-age=2592000; SameSite=Lax';")
    _components.html(f"<script>{_js}</script>", height=0)


if _admin_key_env:
    if _admin_qp and _admin_qp == _admin_key_env:
        st.session_state["full_view_unlocked"] = True
        st.session_state.pop("full_view_exited", None)
        _set_admin_cookie()
    elif (not st.session_state.get("full_view_unlocked")
          and not st.session_state.get("full_view_exited")):
        # "full_view_exited" matters here: after Exit, the browser's old
        # cookie is still attached to the CURRENT request (the clearing
        # script hasn't reached the browser yet), and without this flag
        # the cookie check would re-unlock the session instantly - the
        # "I can't get out" bug.
        try:
            if st.context.cookies.get("sdd_fullview") == _admin_cookie_value():
                st.session_state["full_view_unlocked"] = True
        except Exception:
            pass

# The in-page unlock/exit buttons can't write or clear the cookie
# themselves - they call st.rerun() immediately, which cancels delivery of
# the cookie <script> - so they leave a flag and the cookie is actually
# written/cleared here, at the top of the very next run.
if st.session_state.pop("_pending_admin_cookie", False):
    _set_admin_cookie()
if st.session_state.pop("_pending_admin_cookie_clear", False):
    _set_admin_cookie(clear=True)

# Email sign-in plumbing (same pending-flag pattern): flush any cookie
# write/clear queued by last run's sign-in/sign-out, then restore the
# session from the sdd_auth cookie before any page renders.
_pending_auth_tok = st.session_state.pop("_pending_auth_cookie", None)
if _pending_auth_tok:
    paywall_engine.write_auth_cookie(_pending_auth_tok)
if st.session_state.pop("_pending_auth_cookie_clear", False):
    paywall_engine.write_auth_cookie(None)
paywall_engine.restore_email_session()


def _factual() -> bool:
    """True when this session should see the factual-information
    presentation (the public default)."""
    if not _FACTUAL_DEFAULT:
        return False
    return not st.session_state.get("full_view_unlocked", False)


def _capture_first_src():
    """Records the src= query param (an article's attribution tag) once
    per browser session, the first time it's seen on ANY page - never
    overwritten afterwards, so browsing around the site later doesn't lose
    where the visitor actually came from. The Follow flow, page analytics
    and the Research page's welcome banner all key off
    st.session_state['first_src']. Called at the top of _render_header
    (every page except Home) and at the top of page_home (which renders
    its own header instead of calling _render_header)."""
    _src = st.query_params.get("src")
    if _src and not st.session_state.get("first_src"):
        st.session_state["first_src"] = _src


def _bump_page_view(page, ticker=None):
    """One first-party, aggregate page-view count per page render
    (metrics_store.py) - wrapped in try/except because analytics must
    never break a page. src is attributed to only the FIRST bump of a
    session (the "_src_counted" flag), so a visitor who arrives via one
    article link and then browses five more pages shows up as one src
    attribution, not six."""
    try:
        _src = None
        if not st.session_state.get("_src_counted"):
            st.session_state["_src_counted"] = True
            _src = st.session_state.get("first_src")
        metrics_store.bump(page, ticker=ticker, src=_src)
    except Exception:
        pass


def _render_view_badge():
    """Admin-only indicator + exit, shown ONLY in the unlocked session.
    Also carries the Stats popover (sign-up counts + first-party page-view
    counts, aggregate numbers only - no identities are ever displayed)."""
    if _FACTUAL_DEFAULT and st.session_state.get("full_view_unlocked"):
        # Write blog sits here rather than in the site nav on purpose: the
        # editor is admin-only, so its entry point belongs in the strip
        # that only an unlocked session ever sees. Taken out of the
        # trailing padding column (2 -> 1 + 1) rather than shrinking _b1
        # or _bs, so the badge and Stats popover keep their original,
        # already-tuned widths.
        _b1, _bs, _bw, _b2 = st.columns([8.4, 1.6, 1, 1])
        with _bw:
            if st.button("Write blog", key="admin_write_blog",
                         use_container_width=True):
                st.switch_page(PG_BLOG_ADMIN)
        with _b1:
            st.markdown(
                "<div style='display:inline-block;background:#4a2733;color:#fb7185;"
                "border:1px solid #fb7185;border-radius:8px;padding:3px 12px;"
                "font-size:12px;font-weight:700;letter-spacing:.5px;'>FULL VIEW "
                "(admin) - the public sees the factual presentation</div>",
                unsafe_allow_html=True,
            )
        with _bs:
            with st.popover("Stats", key="admin_signup_stats"):
                try:
                    _s = email_auth.signup_stats()
                    st.markdown(f"### {_s['total']} accounts")
                    st.markdown(
                        f"- **{_s['last_7_days']}** new in the last 7 days\n"
                        f"- **{_s['last_30_days']}** new in the last 30 days\n"
                        f"- **{_s['active_7_days']}** signed in within 7 days\n"
                        f"- Google **{_s['google']}** · Email **{_s['email']}**"
                    )
                    st.caption("Aggregate counts only - stored on the "
                               "Railway volume, survives redeploys.")
                except Exception:
                    st.caption("No sign-up data yet.")

                if st.checkbox("Show email list", key="admin_show_signup_emails"):
                    try:
                        _signup_rows = email_auth.list_signups()
                    except Exception:
                        _signup_rows = []
                    if _signup_rows:
                        _signup_df = pd.DataFrame(_signup_rows)
                        st.dataframe(_signup_df, use_container_width=True, hide_index=True)
                        st.download_button(
                            "Download as CSV",
                            _signup_df.to_csv(index=False).encode("utf-8"),
                            file_name="stocksdeepdive_signups.csv",
                            mime="text/csv",
                            key="admin_download_signup_emails",
                        )
                    else:
                        st.caption("No sign-up data yet.")
                st.markdown("---")
                st.markdown("### Page views")
                try:
                    _m = metrics_store.stats(days=30)
                    st.markdown(
                        f"- **{_m['total_7d']}** views in the last 7 days\n"
                        f"- **{_m['total_30d']}** views in the last 30 days"
                    )
                    if _m["by_page"]:
                        st.markdown("**Top pages (30d)**")
                        for _p, _v in _m["by_page"][:5]:
                            st.markdown(f"- {_p}: **{_v}**")
                    if _m["by_src"]:
                        _signup_by_src = {}
                        try:
                            _signup_by_src = email_auth.signup_counts_by_src()
                        except Exception:
                            pass
                        st.markdown("**Top src (30d) - views / sign-ups**")
                        for _src, _v in _m["by_src"][:5]:
                            st.markdown(
                                f"- {_src}: **{_v}** views / "
                                f"**{_signup_by_src.get(_src, 0)}** sign-ups"
                            )
                    st.caption(
                        "First-party, aggregate counts only - no third-party "
                        "trackers, no per-visitor identity stored."
                    )
                except Exception:
                    st.caption("No page-view data yet.")
        with _b2:
            if st.button("Exit full view", key="exit_full_view"):
                st.session_state["full_view_unlocked"] = False
                st.session_state["full_view_exited"] = True
                st.query_params.pop("admin", None)
                st.session_state["_pending_admin_cookie_clear"] = True
                st.rerun()


def _render_admin_unlock():
    """Small "RC view" popover rendered next to Sign out: typing the admin
    key switches THIS BROWSER to the full presentation (same effect as the
    ?admin= URL, cookie included). Public visitors who click it just see a
    key prompt; a wrong key gets a flat "incorrect" and nothing else."""
    if not (_FACTUAL_DEFAULT and _admin_key_env):
        return
    if st.session_state.get("full_view_unlocked"):
        return  # the FULL VIEW badge row already shows state + Exit
    with st.popover("RC view", key="rc_view_pop"):
        _key_try = st.text_input(
            "Access key", type="password", key="rc_view_key_input",
        )
        if st.button("Unlock", key="rc_view_unlock_btn", type="primary"):
            if _key_try.strip() == _admin_key_env:
                st.session_state["full_view_unlocked"] = True
                st.session_state.pop("full_view_exited", None)
                st.session_state["_pending_admin_cookie"] = True
                st.rerun()
            else:
                st.error("Incorrect key.")


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
    /* The RC view popover trigger matches the Sign In pill exactly -
       keyed selector so only this popover is styled. */
    [class*="st-key-rc_view_pop"] button {
        background-color: rgba(45, 212, 191, 0.07) !important;
        color: #2dd4bf !important;
        border: 1.5px solid #2dd4bf !important;
        border-radius: 999px !important;
        padding: 6px 18px !important;
        font-weight: 600 !important;
        font-size: 14px !important;
        box-shadow: none !important;
        white-space: nowrap !important;
    }
    [class*="st-key-rc_view_pop"] button:hover {
        background-color: #2dd4bf !important;
        color: #ffffff !important;
        border-color: #2dd4bf !important;
    }
    /* Nav buttons: keep the (nowrap) label horizontally centred even when
       it's as wide as the button - otherwise long labels like "Rational
       Compounder Analysis" hug the right edge. */
    [class*="st-key-nav_research"] button,
    [class*="st-key-nav_comparison"] button,
    [class*="st-key-nav_scanner"] button {
        display: flex !important;
        justify-content: center !important;
        padding-left: 0.5rem !important;
        padding-right: 0.5rem !important;
    }
    [class*="st-key-nav_research"] button p,
    [class*="st-key-nav_comparison"] button p,
    [class*="st-key-nav_scanner"] button p {
        text-align: center !important;
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
    /* The "Notify me" email-follow box (_render_follow_control, used on
       both the Research and Deep Dive pages) is an st.container(border=True).
       Two problems compounded here: (1) Streamlit's default container
       border is a near-white, semi-transparent line, and (2) the container
       itself has a TRANSPARENT background, while the text input inside it
       has its own solid fill (#121f36, the theme's secondaryBackgroundColor)
       - so even after fixing the border, the input still visibly "pops" as
       a lighter box-within-a-box, which reads as a stray outline around
       just the email field. Fix both: give the outer box the same
       background+border every other card on the site uses (#121f36 /
       #1f3352, see .sdd-card etc. above), so the input's fill no longer
       contrasts against its container and the whole thing reads as one
       card. Matches on any ticker suffix and both call sites
       (follow_research_box_* / follow_dd_box_*). */
    [class*="st-key-follow_"][class*="_box_"] {
        border: 1px solid #1f3352 !important;
        background: #121f36 !important;
    }
    [class*="st-key-follow_"][class*="_box_"] div[data-testid="stTextInputRootElement"] {
        background: transparent !important;
        border-color: #1f3352 !important;
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
    .sdd-fa-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:12px;
      align-items:center; }
    .sdd-stat { background:#0f1a2e; border:1px solid #1f3352; border-radius:10px; padding:8px 12px; margin-bottom:8px; }
    .sdd-stat .k { font-size:10.5px; color:#5b7290; letter-spacing:.6px; }
    .sdd-stat .v { font-family:ui-monospace,Menlo,monospace; font-size:16px; font-weight:600; color:#e6edf5; margin-top:2px; }
    .sdd-pill { font-size:11px; font-weight:700; font-family:ui-monospace,Menlo,monospace;
      padding:3px 10px; border-radius:999px; letter-spacing:.4px; display:inline-block; }
    .sdd-spark-cap { font-size:11px; color:#5b7290; margin-bottom:4px; display:flex; justify-content:space-between; }
    .sdd-strip { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
      gap:14px; margin:26px 0 8px; }
    .sdd-tile { background:#121f36; border:1px solid #1f3352; border-radius:12px; padding:13px 16px; }
    .sdd-tile .k { font-size:11px; color:#5b7290; letter-spacing:.7px; }
    .sdd-tile .v { font-family:ui-monospace,Menlo,monospace; font-size:19px; font-weight:700; color:#e6edf5; margin-top:5px; }
    .sdd-tile .d { font-size:12px; margin-top:3px; color:#5b7290; font-family:ui-monospace,Menlo,monospace; }
    .sdd-kicker { font-family:ui-monospace,Menlo,monospace; font-size:11.5px; letter-spacing:1.6px;
      color:#2dd4bf; margin-top:34px; }
    .sdd-h2 { font-size:25px; letter-spacing:-.3px; margin:8px 0 6px; font-weight:700; color:#e6edf5; }
    .sdd-secsub { color:#8aa0b8; font-size:14.5px; max-width:640px; line-height:1.5; }
    .sdd-cards5 { display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin-top:22px; }
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
      .sdd-cards5, .sdd-covgrid { grid-template-columns:1fr 1fr; }
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


def _overnight_snapshot_for_ticker(ticker):
    """Task 5: instant overnight-scan snapshot for a ticker, read straight
    off disk (scan_store - no live fetch) - so a visitor searching a ticker
    that was covered by last night's scan sees SOMETHING in under a second
    while the live analyze() call (which hits Yahoo Finance/News/Trends and
    can take several seconds) is still running. Returns
    (universe, generated_at_label, row) or None if this ticker wasn't in
    any of last night's scans (or none exist yet - a brand new deploy, or
    the scheduler hasn't run)."""
    try:
        for _country in ("Australia", "USA"):
            for _uni in scanner_engine.get_universes(_country):
                _scan = scan_store.load_scan(_uni)
                if not _scan:
                    continue
                for _row in _scan.get("rows", []):
                    if str(_row.get("Ticker", "")).strip().upper() == ticker:
                        return _uni, _scan["generated_at_label"], _row
    except Exception:
        pass
    return None


def _render_overnight_snapshot_line(ticker):
    """Task 5: one-line caption shown BEFORE the live status/spinner starts,
    from _overnight_snapshot_for_ticker - purely a "here's what last night's
    scan already found" nudge; the live analyze() call right after this
    always runs and always wins (this is never a substitute for it)."""
    _hit = _overnight_snapshot_for_ticker(ticker)
    if not _hit:
        return
    _uni, _gen_label, _row = _hit
    _score_word = "Value Score" if _factual() else "Long Score"
    _mos = _row.get("MOS %")
    _mos_txt = f"{_mos:+.1f}%" if isinstance(_mos, (int, float)) else "N/A"
    st.caption(
        f"Instant - {ticker} was in last night's {_uni} scan ({_gen_label}): "
        f"{_score_word} {_row.get('Long Score', 'N/A')}, MOS {_mos_txt}, "
        f"Quality {_row.get('Quality', 'N/A')}. Refreshing with live data now..."
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
        # Task 5: instant overnight-scan snapshot line, shown immediately -
        # before the live status/spinner below even starts.
        _render_overnight_snapshot_line(tk)
        # Task 3: st.status gives a collapsible running/complete/error state
        # instead of a bare spinner that just vanishes - analyze() itself is
        # one atomic call (fetch + every score, all in one pass), so this
        # doesn't fabricate step-by-step progress that isn't really
        # happening; it's an honest single stage that reports what it's
        # doing and then how it ended.
        with st.status(f"Analyzing {tk}...", expanded=False) as _status:
            st.write(f"Fetching price history and fundamentals for {tk}, then computing valuation, quality, psychology and discovery scores...")
            st.session_state["dd_result"] = deep_dive_engine.analyze(
                tk, get_price_history, get_ticker_info, get_cashflow_df,
                news_api_key=news_api_key, live_data=live_data,
                enable_social=enable_social,
            )
            _dd_res = st.session_state["dd_result"]
            if _dd_res.get("error"):
                _status.update(label=f"Couldn't analyze {tk}", state="error")
            else:
                _status.update(
                    label=f"Analysis complete - {tk} ({_dd_res.get('name') or tk})",
                    state="complete",
                )
        # Task 1: on a failed resolution, look up "Did you mean" suggestions
        # from the small name_directory starter dictionary so the Deep Dive
        # page can offer them as clickable chips - cleared again on any
        # successful search so a stale suggestion never lingers.
        if st.session_state["dd_result"].get("error"):
            st.session_state["search_suggestions"] = name_directory.suggest(tk)
        else:
            st.session_state.pop("search_suggestions", None)
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


def _render_suggestion_chips(key_prefix, suggestions):
    """Task 1: "Did you mean" chips for a search that failed to resolve as
    a ticker - same st.pills + "done" flag pattern as _render_example_chips
    (so a still-selected pill doesn't re-dispatch on every rerun), sourced
    from name_directory.suggest() instead of the fixed example list."""
    if not suggestions:
        return
    labels = [label for label, _ in suggestions]
    st.caption("Did you mean:")
    _sel = st.pills(
        "Did you mean:", labels, selection_mode="single",
        key=f"{key_prefix}_chips", label_visibility="collapsed",
    )
    _done_key = f"{key_prefix}_chips_done"
    if not _sel:
        st.session_state.pop(_done_key, None)
    elif st.session_state.get(_done_key) != _sel:
        st.session_state[_done_key] = _sel
        _dispatch_search(dict(suggestions)[_sel])


def _render_data_as_of(ticker):
    """Task 3: a 'data as of' stamp on the Deep Dive results - the most
    recent trading day actually present in the (already-cached, so this is
    a free cache hit almost always) price history behind this analysis,
    rather than just the wall-clock time the button was clicked - daily
    bars lag the current session until it closes."""
    try:
        _hist = get_price_history(ticker)
        if _hist is not None and not _hist.empty:
            _last_dt = _hist.index[-1]
            st.caption(f"Data as of {_last_dt.strftime('%d %b %Y')} (latest available daily close).")
    except Exception:
        pass


def _render_recent_results_banner(ticker):
    """Task 4: a freshness note - not a signal - for when the fundamentals
    behind this analysis were reported very recently (last 21 days), using
    yfinance's own mostRecentQuarter / lastFiscalYearEnd .info fields.
    Wrapped in try/except end-to-end since neither field is guaranteed to
    be present (or even a sane epoch value) for every ticker, and this is
    purely informational - it should never be able to break the page."""
    try:
        info = get_ticker_info(ticker)
        now = datetime.now(timezone.utc)
        for key, label in (
            ("mostRecentQuarter", "quarterly results"),
            ("lastFiscalYearEnd", "full-year results"),
        ):
            ts = info.get(key)
            if not ts:
                continue
            reported = datetime.fromtimestamp(ts, tz=timezone.utc)
            days_ago = (now - reported).days
            if 0 <= days_ago <= 21:
                _plural = "s" if days_ago != 1 else ""
                st.info(
                    f"This company reported {label} on "
                    f"{reported.strftime('%d %b %Y')} ({days_ago} day{_plural} "
                    f"ago) - the fundamentals below reflect that report."
                )
                return
    except Exception:
        pass


def _render_score_history_caption(ticker, current_score):
    """Task 6: a "vs 30 days ago" caption on the Deep Dive page, from the
    score_history table the overnight scan writes to nightly. Silently does
    nothing if this ticker has no stored history that far back yet (a brand
    new ticker, or fewer than ~30 days since the overnight scan started
    covering it) - a missing history row should never be shown as if it
    were a zero change."""
    try:
        past = score_history.get(ticker, 30)
        if not past or past.get("long_score") is None or current_score is None:
            return
        delta = current_score - past["long_score"]
        _word = "Value Score" if _factual() else "Long Score"
        st.caption(
            f"{_word} {delta:+.1f} vs {past['day']} "
            f"({past['long_score']:.1f} then -> {current_score:.1f} now, "
            f"from the nightly overnight scan history)."
        )
    except Exception:
        pass


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
    factual = _factual()
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
    if setup and not factual:
        # The ENTRY (trade setup) pill is a trade recommendation - admin
        # view only. Valuation and sentiment pills stay public.
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
        <text x='100' y='106' text-anchor='middle' fill='#8aa0b8' font-size='10' font-family='monospace'>{'VALUE SCORE / 100' if factual else 'LONG SCORE / 100'}</text>
      </svg>
      <div style='font-size:12px;color:#8aa0b8;'>{'a weighted calculation - see Methodology' if factual else f"Verdict: <b style='color:{vcolor};'>{verdict}</b>"}</div>
    </div>
    <div>
      <div class='sdd-stat'><div class='k'>{'INTRINSIC VALUE (DCF)' if factual else 'INTRINSIC VALUE (DCF, BASE CASE)'}</div><div class='v'>{iv_txt}</div></div>
      <div class='sdd-stat'><div class='k'>{'MOS' if factual else 'MARGIN OF SAFETY'}</div><div class='v' style='color:{mos_color};'>{mos_txt}</div></div>
      <div class='sdd-stat'><div class='k'>{'QUALITY (CALCULATED)' if factual else 'QUALITY SCORE'}</div><div class='v'>{dd['quality_score']} / 100</div></div>
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
      <a href='/blog'>Blog</a>
      <a href='/about' target='_self'>About the author</a>
      <a href='/methodology' target='_self'>How the scores work</a>
      <a href='/research' target='_self'>Rational Compounder Research</a>
      <a href='/model-history' target='_self'>Model history</a>
    </div>
    <div><h5>Tools</h5>
      <a href='/deep-dive' target='_self'>Stock Deep Dive</a>
      <a href='/comparison' target='_self'>Comparison</a>
      <a href='/scanner' target='_self'>Stock Scanner</a>
      <a href='/portfolio' target='_self'>My Portfolio (sign in)</a>
    </div>
    <div><h5>Contact</h5>
      <a href='mailto:rationalcompounder@stocksdeepdive.com'>rationalcompounder@stocksdeepdive.com</a>
      <span style='display:block;color:#8aa0b8;margin-top:6px;font-size:13px;'>or the Feedback button on any results page</span>
      <a href='/privacy' target='_self'>Privacy policy</a>
    </div>
  </div>
  <div class='sdd-disclaimer'>{disclaimer}</div>
</div>
""".format(disclaimer=(
            "<b>Factual information and calculator outputs only.</b> StocksDeepDive "
            "computes and displays data, model outputs and described calculations "
            "from stated inputs. It does not provide financial product advice, "
            "recommendations, or opinions about buying, holding or selling any "
            "security, and nothing on this site should be read as such. Model "
            "outputs depend entirely on their stated inputs and assumptions, which "
            "you can inspect &mdash; and in places override &mdash; yourself. "
            "Values shown in red rest on default or estimated inputs. Data via "
            "Yahoo Finance, Google Trends, StockTwits and NewsAPI; figures may be "
            "delayed or revised."
        ) if _factual() else (
            "<b>General information only.</b> StocksDeepDive provides factual "
            "information and general commentary generated from publicly available "
            "data. It does not take your personal objectives, financial situation "
            "or needs into account and is not financial advice. Scores, signals, "
            "entry zones and price targets are model outputs, not recommendations. "
            "Consider seeking advice from a licensed adviser before acting. Data "
            "via Yahoo Finance, Google Trends, StockTwits and NewsAPI; figures may "
            "be delayed or estimated &mdash; estimated values are shown in red "
            "throughout the site."
        )),
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


def _render_watchlist_bulk_import():
    """Task 7: paste-a-list bulk watchlist import, signed-in users only - a
    faster path than adding one ticker at a time from each Deep Dive page.
    No live validation of each ticker (same lightweight parsing _dispatch_
    search itself uses - just split, uppercase, dedupe); this only ever
    writes to watchlist_store, the same store/table the single-ticker
    Add/Remove button on the Deep Dive page already uses. Capped at 50
    tickers per paste - anything beyond that is silently dropped from THIS
    import only (never touches tickers already saved), and the user is
    told exactly how many were skipped."""
    email = paywall_engine.current_user_email()
    if not email:
        return
    with st.expander("Paste a list of tickers to add to your watchlist", expanded=False):
        _wl2_text = st.text_area(
            "Tickers (comma, space or newline separated)",
            key="wl2_bulk_text",
            placeholder="CBA.AX, BHP.AX, CSL.AX\nAAPL\nMSFT",
        )
        if st.button("Add to watchlist", key="wl2_bulk_add"):
            raw = (_wl2_text or "").replace(",", " ").replace("\n", " ").split()
            parsed = []
            for tok in raw:
                tok = tok.strip().upper()
                if tok and tok not in parsed:
                    parsed.append(tok)
            if not parsed:
                st.warning("Paste at least one ticker above.")
            else:
                capped = parsed[:50]
                skipped = len(parsed) - len(capped)
                added = 0
                try:
                    for t in capped:
                        if not watchlist_store.contains(email, t):
                            watchlist_store.add(email, t)
                            added += 1
                except Exception:
                    st.warning("Couldn't save right now - please try again.")
                else:
                    _msg = f"Added {added} new ticker{'s' if added != 1 else ''} to your watchlist."
                    if skipped:
                        _msg += f" {skipped} beyond the 50-ticker paste limit were skipped."
                    st.success(_msg)
                    st.rerun()


def _render_header(compact, page_label=None):
    _capture_first_src()
    _render_tape()
    _render_view_badge()
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
    _show_unlock = bool(
        _FACTUAL_DEFAULT and _admin_key_env
        and not st.session_state.get("full_view_unlocked")
    )
    paywall_engine.render_account_bar(
        extra_widget=feedback_widget,
        extra_widget2=_render_admin_unlock if _show_unlock else None,
    )
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

    # Symmetric 15%/15% outer margins (vs the old [2, 6, 1, 1]'s uneven
    # 20% left / 10% right) so this row is centered under the page the
    # same way the search row above it is - just over its own wider 70%
    # content band, since four text-heavy labels don't fit in the search
    # box's narrower 40%. Inside that band, each button gets a column
    # WIDTH PROPORTIONAL TO ITS OWN LABEL LENGTH (plus a fixed padding
    # allowance) rather than equal-width columns sized to fit the longest
    # label - that's what made "Stock Scanner" and "My Portfolio" render
    # as oversized boxes around short text. Streamlit still stretches
    # each button to fill its column (use_container_width=True) and
    # centers its label inside that box by default, so a column sized
    # close to the label's own width is what makes the pill look tightly
    # fitted and centered.
    _bsp1, _bmid, _bsp2 = st.columns([3, 14, 3])
    with _bmid:
        _nav_buttons = [
            ("Rational Compounder Analysis", "nav_research", PG_RESEARCH),
            ("Side-by-side Comparison", "nav_comparison", PG_COMPARISON),
            ("Stock Scanner", "nav_scanner", PG_SCANNER),
            ("My Portfolio", "nav_portfolio", PG_PORTFOLIO),
        ]
        _nav_widths = [len(_label) + 6 for _label, _key, _page in _nav_buttons]
        _nav_cols = st.columns(_nav_widths, gap="small")
        for _col, (_label, _key, _page) in zip(_nav_cols, _nav_buttons):
            with _col:
                # Signed-in-only for My Portfolio - page_portfolio() itself
                # shows the sign-in prompt when nobody's logged in, so this
                # button never needs to check auth state before navigating.
                if st.button(_label, use_container_width=True, key=_key):
                    st.switch_page(_page)

    if _searched:
        _dispatch_search(_search_text)

    _sp_margin = "10px" if compact else "20px"
    st.markdown(f"<div style='margin-bottom:{_sp_margin};'></div>", unsafe_allow_html=True)

def _dd_gauge(value, title, zones, bar_color="#e6edf5", height=260, axis_range=(0, 100)):
    """
    One consistent gauge for the Deep Dive tab's per-factor scores
    (Quality / Psychology / Discovery / Trade Setup / Margin of Safety) -
    same shape as the Long Score gauge above, parameterised so it isn't
    rebuilt each time. zones: list of (lo, hi, color) tuples covering
    axis_range. axis_range defaults to the usual 0-100 score scale; the
    Margin of Safety dial is the one caller that passes a signed range.

    The title is drawn as its own paper-space annotation pinned above an
    explicitly-shrunk gauge domain, rather than using the Indicator's
    built-in "title" - that built-in title auto-scales/positions itself
    against the gauge domain (that's what was clipping it on a full-width
    gauge earlier, and kept crowding it against the dial even after
    several margin tweaks, since margin alone doesn't reserve title
    space - the gauge just grows to fill it). Reserving the top ~28% of
    the domain for the gauge to NOT use, then placing the annotation in
    that reserved band, gives a fixed, direct gap that holds regardless
    of chart width or title length.
    """
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=value,
        number={"font": {"size": 34}},
        domain={"x": [0, 1], "y": [0, 0.72]},
        gauge={
            "axis": {"range": list(axis_range)},
            "bar": {"color": bar_color},
            "steps": [{"range": [lo, hi], "color": color} for lo, hi, color in zones],
        },
    ))
    fig.add_annotation(
        text=title, xref="paper", yref="paper", x=0.5, y=0.93,
        xanchor="center", yanchor="top", showarrow=False,
        font=dict(size=16),
    )
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=20, b=10))
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
#
# The colour vocabulary, section chart-builders, the band-gauge kit, and
# the shared render_section()/render_tabs() now live in compounder_ui.py
# (imported above) so the Deep Dive page's "Compounder View (auto)" can
# render live-computed data with the exact same look - see that module's
# docstring. Only what's specific to the hand-built workbook data (loading
# it, and the Company Potential section's own ratings/text rendering,
# which auto view never shows) stays here.
# -----------------------------------


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
        loaded = json.load(_f)

    # One-time migrations for a Railway Volume's own compounder_data.json:
    # _cp_seed_from_repo_if_missing() only ever seeds an EMPTY volume, so a
    # volume that already has its own file from a prior admin rebuild never
    # picks up fixes made to the git-tracked copy - it can be sitting on
    # stale keys/formats indefinitely. Heal each such issue here and
    # persist the fix back to the volume so it only has to run once; both
    # migrations share a single write at the end.
    _migrated = False

    # (1) the "Dividends" section was renamed to "Retained Earnings" in
    # code - an old volume still on the old key makes the section_order
    # filter below drop the tab entirely (it's neither key anymore).
    _sections = (loaded or {}).get("sections")
    if _sections and "Dividends" in _sections and "Retained Earnings" not in _sections:
        _sections["Retained Earnings"] = _sections.pop("Dividends")
        _migrated = True

    # (2) "Retained Earnings (TTM)" (Dividend Ratio!BQ - EPS minus
    # Dividend) was mistagged format "pct" by the importer; it's a dollar
    # figure and should be "cur" (see auto_compounder_engine.py's
    # _build_retained_earnings for the full story). An old volume's data
    # can still carry the "pct" tag even after this is fixed in code.
    _re_metrics = (_sections or {}).get("Retained Earnings", {}).get("metrics") if _sections else None
    for _m in (_re_metrics or []):
        if _m.get("key") == "BQ" and _m.get("label") == "Retained Earnings (TTM)" and _m.get("format") == "pct":
            _m["format"] = "cur"
            _migrated = True

    if _migrated:
        try:
            with open(_path, "w") as _f:
                json.dump(loaded, _f, indent=1)
        except OSError:
            pass  # in-memory fix still applies this run even if the write fails

    return loaded


# _cp_clean_comment, _cp_format, _cp_band, the old plotly _cp_gauge (now
# retired in favour of compounder_ui.band_gauge), and every _cp_*_chart()
# figure-builder now live in compounder_ui.py - see this module's header
# comment above and compounder_ui.py's own docstring.


_CP_HML_COLOR = {
    "good_high": {"high": "green", "medium": "amber", "low": "red"},
    "good_low": {"low": "green", "medium": "amber", "high": "red"},
    "neutral": {},
}


def _cp_pill(text, color_key=None):
    c = compounder_ui._CP_COLOR_TEXT.get(color_key, "#9db1c7")
    return (
        f"<span style='display:inline-block;margin:2px 6px 2px 0;padding:3px 10px;"
        f"border-radius:12px;font-size:12.5px;font-weight:600;"
        f"background:{c}22;color:{c};'>{text}</span>"
    )


# Render-time polarity corrections: these two were originally built as
# "neutral" (grey pills) but a High reading is good for both, so colour
# them like the other good_high ratings. Fixed here rather than only in
# build_compounder_data so already-built data (current AND archived
# snapshots) displays correctly without a rebuild.
_CP_HML_POLARITY_FIX = {"Insights": "good_high", "Market Activity": "good_high"}


# Colour convention for the short Yes/No/Medium "quick checks" now folded
# into the Ratings section (see _render_cp_section) - same green/amber/red
# vocabulary as the H/M/L ratings above, just without a per-question
# good_high/good_low polarity to key off (the workbook doesn't encode one
# for these). Note "High fixed charges?" is a genuinely double-edged
# question (high fixed costs magnify earnings on the way up too), so it's
# deliberately left uninverted here rather than guessed at - flag it if
# you'd rather it read the other way.
_CP_CHECK_COLOR = {"yes": "green", "no": "red", "medium": "amber"}


def _cp_render_hml_ratings(ratings, extra_checks=None):
    st.markdown("##### Ratings (called directly from your Low/Medium/High cells)")
    html_parts = []
    for r in ratings:
        _pol = _CP_HML_POLARITY_FIX.get(r["label"], r["polarity"])
        _val = r["value"].strip().lower()
        color_key = _CP_HML_COLOR.get(_pol, {}).get(_val)
        if color_key is None and _pol in ("good_high", "good_low"):
            # Yes/No answers sometimes live in these columns too (e.g.
            # "Ability to Change Pricing: Yes") - colour them rather than
            # falling through to a grey pill.
            color_key = {"yes": "green", "no": "red"}.get(_val)
        html_parts.append(
            f"<div style='margin-bottom:6px;'><span style='font-size:13px;color:#aebfd4;'>"
            f"{_md_safe(r['label'])}: </span>{_cp_pill(_md_safe(r['value'].strip()), color_key)}</div>"
        )
    for c in (extra_checks or []):
        _val = c["value"].strip().lower()
        color_key = _CP_CHECK_COLOR.get(_val)
        html_parts.append(
            f"<div style='margin-bottom:6px;'><span style='font-size:13px;color:#aebfd4;'>"
            f"{_md_safe(c['label'])}: </span>{_cp_pill(_md_safe(c['value'].strip()), color_key)}</div>"
        )
    # two columns of pills so a long ratings list doesn't run the whole page
    half = (len(html_parts) + 1) // 2
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("".join(html_parts[:half]), unsafe_allow_html=True)
    with col2:
        st.markdown("".join(html_parts[half:]), unsafe_allow_html=True)


# _cp_note now lives in compounder_ui.py (imported above as
# compounder_ui._cp_note) - used below by _cp_render_text_groups, the one
# remaining hand-research-only caller in this file.


def _md_safe(text):
    """Workbook-sourced free text made safe for st.markdown HTML blocks:
    HTML-escaped (a stray < or & can't break the pill markup) and with $
    converted to its HTML entity - two $ signs in markdown otherwise
    trigger LaTeX math rendering, which mangles everything between them
    (seen with 'A$750 million' in a CSL buyback answer)."""
    return html.escape(str(text)).replace("$", "&#36;").replace("~", "&#126;")


def _cp_render_text_groups(groups):
    st.markdown("##### Company Analysis")
    for g in groups:
        with st.expander(g["title"], expanded=False):
            for item in g["items"]:
                st.markdown(f"**{_md_safe(item['label'])}**")
                compounder_ui._cp_note(item["text"])


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
            st.checkbox(
                "Email followers about this update",
                value=True, key="cp_admin_announce",
                help=(
                    "Sends the new-research announcement (announce_engine.py) to "
                    "everyone following the companies that are added/updated by "
                    "this rebuild, plus anyone following all research. Untick for "
                    "a data-fix rebuild that shouldn't email anyone."
                ),
            )
            if uploaded and st.button(
                "Rebuild Rational Compounder data", type="primary", key="cp_admin_rebuild",
            ):
                # Captured BEFORE the rebuild replaces the live data, so the
                # added/updated diff below (Task 6) reflects what actually
                # changed this time, not what's already on disk after the
                # write.
                _old_tickers = set()
                try:
                    _prev_data = _load_compounder_data()
                    if _prev_data:
                        _old_tickers = set(_prev_data.get("tickers", {}).keys())
                except Exception:
                    _old_tickers = set()
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

                # New-research announcement email (Task 6) - only when the
                # admin left the checkbox ticked (default ON), so a plain
                # data-fix rebuild can still be done without emailing
                # followers. added/updated computed against the ticker set
                # captured before this rebuild started.
                if st.session_state.get("cp_admin_announce", True):
                    try:
                        _new_tickers = set(new_data.get("tickers", {}).keys())
                        _added = _new_tickers - _old_tickers
                        _updated = _new_tickers & _old_tickers
                        _announce_summary = announce_engine.announce_rebuild(_added, _updated)
                        st.success(
                            f"Announcement email sent to "
                            f"{_announce_summary.get('sent', 0)} follower(s) "
                            f"({_announce_summary.get('errors', 0)} error(s))."
                        )
                    except Exception as _announce_exc:
                        st.warning(f"Couldn't send the announcement email: {_announce_exc}")

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


def _render_follow_control(ticker, key_prefix):
    """"Follow this company" email capture (follow_store.py) - shared by
    page_research (per selected ticker) and page_deep_dive (when the
    ticker has research coverage). Not FACTUAL_MODE gated: following is
    fine in both presentations, it's data/updates, not a signal.

    Signed-in visitors get a one-click toggle. Anonymous visitors get the
    EXISTING email sign-in code flow (email_auth.py) - same session-state
    keys (email_user / email_auth_token / _pending_auth_cookie /
    _signup_recorded) that paywall_engine._render_signin_control sets on a
    successful verify_code, so the visitor ends up signed in exactly the
    same way, and the follow is recorded the instant that succeeds. This
    gives double opt-in for free (the code email) and creates an account
    in one step. key_prefix keeps widget keys unique per call site
    (e.g. "follow_research" vs "follow_dd")."""
    _email = paywall_engine.current_user_email()
    with st.container(border=True, key=f"{key_prefix}_box_{ticker}"):
        if _email:
            _following = follow_store.is_following(_email, ticker)
            _label = ("\U0001F514 Following — click to stop" if _following
                       else "\U0001F514 Email me when research updates")
            if st.button(_label, key=f"{key_prefix}_toggle_{ticker}"):
                try:
                    if _following:
                        follow_store.unfollow(_email, ticker)
                    else:
                        follow_store.follow(_email, ticker)
                except Exception:
                    st.warning("Couldn't save right now - please try again.")
                st.rerun()
            return

        st.caption(f"Get an email when {ticker}'s research updates.")
        _sent_flag = f"{key_prefix}_code_sent"
        if not st.session_state.get(_sent_flag):
            _c1, _c2 = st.columns([3, 2])
            with _c1:
                _em_in = st.text_input(
                    "Email address", key=f"{key_prefix}_email_{ticker}",
                    placeholder="you@example.com", label_visibility="collapsed",
                )
            with _c2:
                # Short label - this button now often renders in a narrow
                # column (the Research page's Stock/Deep Dive/Follow row),
                # where the old full-length "Get research updates by
                # email" overflowed past the box border.
                if st.button(
                    "Notify me",
                    key=f"{key_prefix}_submit_{ticker}", use_container_width=True,
                ):
                    if email_auth.valid_email(_em_in):
                        _ok, _msg = email_auth.send_code(_em_in)
                        if _ok:
                            st.session_state[_sent_flag] = True
                            st.session_state[f"{key_prefix}_sent_to"] = _em_in.strip().lower()
                            st.session_state[f"{key_prefix}_pending_ticker"] = ticker
                            st.success(_msg)
                        else:
                            st.error(_msg)
                    else:
                        st.error("That doesn't look like a valid email address.")
        else:
            _sent_to = st.session_state.get(f"{key_prefix}_sent_to", "")
            st.caption(f"Enter the 6-digit code sent to {_sent_to}.")
            _code = st.text_input(
                "6-digit code", key=f"{key_prefix}_code_{ticker}",
                max_chars=6, placeholder="123456",
            )
            _cv, _cr = st.columns(2)
            with _cv:
                if st.button(
                    "Verify", key=f"{key_prefix}_verify_{ticker}",
                    type="primary", use_container_width=True,
                ):
                    _tok, _msg = email_auth.verify_code(
                        _sent_to, _code, src=st.session_state.get("first_src")
                    )
                    if _tok:
                        # Same state paywall_engine._render_signin_control
                        # sets on success - the visitor is signed in exactly
                        # as they would be via the regular Sign In popover.
                        st.session_state["email_user"] = _sent_to
                        st.session_state["email_auth_token"] = _tok
                        st.session_state.pop("email_signed_out", None)
                        st.session_state["_pending_auth_cookie"] = _tok
                        # verify_code already recorded the sign-up.
                        st.session_state["_signup_recorded"] = True
                        try:
                            follow_store.follow(
                                _sent_to,
                                st.session_state.get(f"{key_prefix}_pending_ticker", ticker),
                            )
                        except Exception:
                            pass
                        st.session_state.pop(_sent_flag, None)
                        st.session_state.pop(f"{key_prefix}_pending_ticker", None)
                        st.rerun()
                    else:
                        st.error(_msg)
            with _cr:
                if st.button(
                    "Resend code", key=f"{key_prefix}_resend_{ticker}",
                    use_container_width=True,
                ):
                    _ok, _msg = email_auth.send_code(_sent_to)
                    (st.success if _ok else st.error)(_msg)


def _render_research_header_card(ticker, data, section_order):
    """Per-company header card (Task 5): styled like the site's `.sdd-card`
    divs (dark #121f36 background, #1f3352 border, rounded) - ticker large,
    industry, how many of the page's sections actually have data for this
    company, and when the underlying workbook snapshot was last rebuilt.
    The "Open the live Deep Dive" link-button used to live at the bottom of
    this card - it now renders in the Stock/Deep Dive/Follow row above,
    next to the Stock picker, so this card is just the info strip."""
    industry = (data["tickers"].get(ticker, {}) or {}).get("industry") or "—"
    section_count = sum(
        1 for _s in section_order
        if any(
            (m.get("values") or {}).get(ticker) is not None
            for m in data["sections"].get(_s, {}).get("metrics", [])
        )
    )
    last_updated_label = "—"
    _generated_at = data.get("generated_at")
    if _generated_at:
        try:
            _dt = datetime.fromisoformat(_generated_at)
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
            last_updated_label = _dt.astimezone(timezone.utc).strftime("%d %b %Y")
        except (TypeError, ValueError):
            pass
    with st.container(key=f"cp_header_card_{ticker}"):
        st.markdown(
            f"""
            <div class='sdd-card' style='margin-bottom:12px;'>
              <div style='display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:14px;'>
                <div>
                  <div style='font-family:ui-monospace,Menlo,monospace; font-size:26px; font-weight:800; color:#e6edf5;'>{html.escape(ticker)}</div>
                  <div style='color:#8aa0b8; font-size:13px; margin-top:2px;'>{html.escape(industry)}</div>
                </div>
                <div style='display:flex; gap:24px;'>
                  <div>
                    <div style='font-size:10.5px; color:#5b7290; letter-spacing:.6px;'>SECTIONS COVERED</div>
                    <div style='font-family:ui-monospace,Menlo,monospace; font-size:16px; color:#e6edf5; margin-top:2px;'>{section_count} / {len(section_order)}</div>
                  </div>
                  <div>
                    <div style='font-size:10.5px; color:#5b7290; letter-spacing:.6px;'>DATA LAST UPDATED</div>
                    <div style='font-family:ui-monospace,Menlo,monospace; font-size:16px; color:#e6edf5; margin-top:2px;'>{last_updated_label}</div>
                  </div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# Badge copy per author-position status (positions_store.py) - a missing row
# is treated as 'never'. Colour is teal for 'holds' (the one status that
# reads as noteworthy) and gold for 'never'/'closed' (was the site's neutral
# grey - too easy to lose against the dark background, per Andrew's request)
# - red/green/amber are reserved for data verdicts elsewhere on the site
# and must never appear in this disclosure feature, so gold rather than
# straight orange, which sits too close to the site's amber verdict colour.
_POS_BADGE = {
    "holds": ("#2dd4bf", "✓ Skin in the game — the author holds this "
                          "stock in his personal portfolio"),
    "never": ("#e0b050", "○ No position — the author has never held "
                          "this stock"),
    "closed": ("#e0b050", "◐ Position closed — the author previously "
                           "held this stock"),
}

_POS_STATUS_LABELS = {"holds": "Holds", "never": "Never held", "closed": "Closed"}
_POS_LABEL_STATUS = {v: k for k, v in _POS_STATUS_LABELS.items()}


def _pos_default_currency(ticker):
    return "AUD" if ticker.upper().endswith(".AX") else "USD"


def _pos_fmt_price(avg_price):
    """avg_price is stored as REAL with 0.0 meaning 'not stated' (Step 3) -
    callers only call this once they've already checked avg_price is
    truthy."""
    return f"{avg_price:.2f}"


def _position_disclosure_text(ticker, status, pos):
    """Plain-text disclosure body for the expander, built from the stored
    row (skip any clause whose field is empty) - exact wording per the
    task spec, no advice language, no forward-looking statements, no
    position size / dollar amount invested, only dates, approach, and
    per-share prices (average purchase price, and - for closed positions -
    the price sold at)."""
    pos = pos or {}
    first_purchase = (pos.get("first_purchase") or "").strip()
    exit_month = (pos.get("exit_month") or "").strip()
    entry_approach = (pos.get("entry_approach") or "").strip()
    avg_price = pos.get("avg_price") or None
    exit_price = pos.get("exit_price") or None
    currency = (pos.get("currency") or "").strip()

    if status == "holds":
        parts = [f"I hold shares of {ticker} in my personal portfolio."]
        if first_purchase:
            parts.append(f"First purchase: {first_purchase}.")
        if entry_approach:
            parts.append(f"Entry approach: {entry_approach}.")
        if avg_price:
            parts.append(
                f"Average purchase price: {_pos_fmt_price(avg_price)} {currency}."
            )
        parts.append(
            "I state this for transparency, not as a recommendation — my "
            "financial circumstances, risk tolerance, entry prices and time "
            "horizon are mine, and none of them are yours. I may buy more or "
            "sell at any time without updating this page first."
        )
        return " ".join(parts)

    if status == "closed":
        clauses = []
        if first_purchase:
            clauses.append(f"first purchase {first_purchase}")
        if exit_month:
            clauses.append(f"fully exited {exit_month}")
        if entry_approach:
            clauses.append(f"entry approach: {entry_approach}")
        if avg_price:
            clauses.append(
                f"average purchase price {_pos_fmt_price(avg_price)} {currency}"
            )
        if exit_price:
            clauses.append(
                f"sold at {_pos_fmt_price(exit_price)} {currency}"
            )
        lead = f"I previously held shares of {ticker}"
        lead += f" ({'; '.join(clauses)})." if clauses else "."
        return (
            f"{lead} I no longer hold a position and may re-enter at any "
            "time. This is stated for transparency, not as a recommendation."
        )

    # 'never' (also the fallback for a missing row / unrecognised status)
    return (
        f"I do not hold and have never held shares of {ticker}. Coverage on "
        "this site is independent of whether I personally own a company. "
        "This is stated for transparency, not as a recommendation."
    )


def _render_position_disclosure(ticker):
    """Author position disclosure strip (positions_store.py) - a badge plus
    an expander with the full wording, shown IDENTICALLY in the public
    factual view and the admin full view (this is disclosure, not a data
    verdict, so it is never gated by _factual()). The admin-only editor
    below it is gated on full_view_unlocked directly, per the task spec."""
    pos = positions_store.get_position(ticker)
    status = (pos or {}).get("status")
    if status not in ("holds", "never", "closed"):
        status = "never"

    color, badge_text = _POS_BADGE[status]
    st.markdown(
        f"""
        <div style='display:inline-block; border:1px solid {color};
                     color:{color}; border-radius:999px; padding:4px 14px;
                     font-size:12.5px; margin:6px 0 6px;'>
          {html.escape(badge_text)}
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.expander("Position disclosure", expanded=False):
        _body = _md_safe(_position_disclosure_text(ticker, status, pos)).replace("\n", "<br>")
        st.markdown(
            f"<div style='color:#8aa0b8;font-size:13px;line-height:1.65;'>{_body}</div>",
            unsafe_allow_html=True,
        )

    if not st.session_state.get("full_view_unlocked"):
        return

    with st.expander("Edit position (admin)", expanded=False):
        if not pos:
            # The one deliberate exception to this feature's no-red/green/
            # amber rule (task spec, Step 3): an amber admin-only hint that
            # nothing has been saved for this ticker yet.
            st.caption(
                ":orange[Position not set for this company — public "
                "currently sees 'No position'.]"
            )
        with st.form(key=f"pos_edit_form_{ticker}"):
            _status_label = st.radio(
                "Status",
                list(_POS_STATUS_LABELS.values()),
                index=list(_POS_STATUS_LABELS.keys()).index(status),
                key=f"pos_status_{ticker}",
            )
            _first_purchase_in = st.text_input(
                "First purchase (e.g. \"March 2024\")",
                value=(pos or {}).get("first_purchase") or "",
                key=f"pos_first_purchase_{ticker}",
            )
            _exit_month_in = st.text_input(
                "Exit month (only meaningful for Closed)",
                value=(pos or {}).get("exit_month") or "",
                key=f"pos_exit_month_{ticker}",
            )
            _entry_approach_in = st.text_input(
                "Entry approach (e.g. \"staged entry, 25% tranches\")",
                value=(pos or {}).get("entry_approach") or "",
                key=f"pos_entry_approach_{ticker}",
            )
            _avg_price_in = st.number_input(
                "Average price (0.0 = not stated)",
                value=float((pos or {}).get("avg_price") or 0.0),
                min_value=0.0, step=0.01, format="%.2f",
                key=f"pos_avg_price_{ticker}",
            )
            _exit_price_in = st.number_input(
                "Sold at (0.0 = not stated; only meaningful for Closed)",
                value=float((pos or {}).get("exit_price") or 0.0),
                min_value=0.0, step=0.01, format="%.2f",
                key=f"pos_exit_price_{ticker}",
            )
            _currency_in = st.text_input(
                "Currency",
                value=(pos or {}).get("currency") or _pos_default_currency(ticker),
                key=f"pos_currency_{ticker}",
            )
            if st.form_submit_button("Save"):
                positions_store.set_position(
                    ticker,
                    _POS_LABEL_STATUS[_status_label],
                    first_purchase=_first_purchase_in or None,
                    exit_month=_exit_month_in or None,
                    entry_approach=_entry_approach_in or None,
                    avg_price=_avg_price_in or None,
                    exit_price=_exit_price_in or None,
                    currency=_currency_in or None,
                )
                st.rerun()


def _render_cp_section(ticker, section_label, data):
    """
    Renders one Research-page section (Fundamentals / Value vs Book /
    Retained Earnings / Earnings Trends / Cost of Capital / Fair Value /
    Company Potential) for the given ticker. Extracted out of page_research() so it
    can be called once per st.tabs() tab - unlike the old single-selection
    dropdown, st.tabs() renders every tab's body on every run, so this now
    runs up to 7x per page load instead of once.

    "Company Potential" (the author's own ratings/written analysis) is
    hand-research-only and rendered directly below. The other six sections
    are computed data with charts + a metrics grid - that rendering is
    SHARED with the Deep Dive page's "Compounder View (auto)" expander via
    compounder_ui.render_section(), so the two can never visually drift
    apart (see compounder_ui.py's docstring).
    """
    section = data["sections"][section_label]

    if section_label == "Company Potential":
        if not paywall_engine.render_gate(
            "Company Potential - your own research notes",
            teaser=(
                "The author's Low/Medium/High ratings and full written "
                "analysis for every covered company."
            ),
            key_prefix=f"cp_potential_{ticker}",
        ):
            return
        ratings = section.get("hml_ratings", {}).get(ticker, [])
        checks = section.get("yesno_checks", {}).get(ticker, [])
        groups = section.get("text_groups", {}).get(ticker, [])

        # "Quick checks" used to be its own section, but its answers are two
        # different shapes: short Yes/No/Medium calls that read like more
        # ratings, and a couple of questions (Any share buybacks?,
        # Forecasted earnings possible/plausible/probable?) that sometimes
        # carry a full written explanation instead of a one-word answer.
        # Split on answer length and fold each into the section it actually
        # belongs with - short ones alongside the Low/Medium/High ratings
        # (colour-coded the same way), long ones into the Investment Case
        # write-up. This retires "Quick checks" as its own section.
        _CHECK_LONGFORM_MIN = 30
        short_checks = [c for c in checks if len(c["value"].strip()) <= _CHECK_LONGFORM_MIN]
        long_checks = [c for c in checks if len(c["value"].strip()) > _CHECK_LONGFORM_MIN]

        if long_checks:
            _ic_items = [{"label": c["label"], "text": c["value"]} for c in long_checks]
            _ic_group = next((g for g in groups if g["title"] == "The Investment Case"), None)
            if _ic_group is not None:
                _ic_group["items"] = _ic_group.get("items", []) + _ic_items
            else:
                groups = groups + [{"title": "The Investment Case", "items": _ic_items}]

        if not ratings and not short_checks and not groups:
            st.warning(f"No Company Potential notes yet for {ticker}.")
            return
        st.caption(
            "The author's Low/Medium/High calls on management, moat, risk "
            "and more - shown exactly as researched - plus the full written "
            "analysis, grouped by theme."
        )
        if ratings or short_checks:
            _cp_render_hml_ratings(ratings, short_checks)
        if groups:
            _cp_render_text_groups(groups)
        return

    # The six computed sections all render through the shared component.
    # Fair Value stays paywalled here exactly as before (same gate text/key
    # as always) - render_section() applies it before drawing anything.
    gate = None
    if section_label == "Fair Value":
        gate = (
            "Fair Value - full valuation methods breakdown",
            "Four independent valuation methods side by side, with the "
            "exact inputs behind each one.",
            f"cp_fairvalue_{ticker}",
        )
    compounder_ui.render_section(data["sections"], ticker, section_label, gate=gate)


def page_research():
    _render_header(compact=True, page_label="Rational Compounder Analysis")

    # Article-arrival welcome banner: shown only to visitors who arrived
    # via a src= link (an article) and haven't dismissed it yet this
    # session. Rendered at the very top, before the admin panel/data, so
    # it's the first thing an article reader sees.
    if (st.session_state.get("first_src")
            and not st.session_state.get("research_banner_dismissed")):
        with st.container(border=True, key="research_welcome_banner"):
            _wb1, _wb2 = st.columns([24, 1])
            with _wb1:
                st.markdown(
                    "**You've read the analysis — this is the live research "
                    "behind it.** Every chart on this page comes from the "
                    "same workbook the article was written from, refreshed "
                    "with each rebuild. Follow the company below to get the "
                    "next update by email."
                )
            with _wb2:
                if st.button("✕", key="research_banner_dismiss"):
                    st.session_state["research_banner_dismissed"] = True
                    st.rerun()

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
        # Task 9: the Model History page's "View" buttons land here with a
        # specific archive path pre-selected - written to the selectbox's
        # own session-state key BEFORE the widget is created (the one safe
        # time to do so), same pattern research_jump_ticker uses below for
        # "cp_ticker".
        _cp_snapshot_jump_path = st.session_state.pop("cp_snapshot_jump", None)
        if _cp_snapshot_jump_path:
            for _i, _s in enumerate(_snapshots):
                if _s["path"] == _cp_snapshot_jump_path:
                    st.session_state["cp_snapshot_pick"] = _snap_labels[_i + 1]
                    break
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
        _bump_page_view("research")
        return

    section_order = [
        "Fundamentals", "Value vs Book", "Retained Earnings", "Earnings Trends",
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

    # Task 9: link out to the Model History page - a changelog of when this
    # workbook has been rebuilt, not shown inline here since it applies to
    # every archive at once, not just the ones surfaced by the snapshot
    # picker above.
    if st.button("See the model's rebuild history →", key="research_to_model_history"):
        st.switch_page(PG_MODEL_HISTORY)

    st.caption(
        f"{len(data['tickers'])} companies covered in depth today - new "
        "names are added as each one's research completes. Want a stock "
        "prioritised? Say so via the Feedback button above."
    )

    tickers = sorted(data["tickers"].keys())

    def _ticker_label(t):
        industry = data["tickers"][t].get("industry")
        return f"{t} - {industry}" if industry else t

    # Deep Dive -> Research cross-link (Task 5): page_deep_dive stores the
    # ticker here instead of setting st.query_params["ticker"] before
    # st.switch_page, because st.switch_page CLEARS all non-embed query
    # params on navigation by default (confirmed against the installed
    # streamlit version's own switch_page() docstring/source) - a query
    # param set right before switch_page never actually reaches this page.
    # Popped (one-shot, then cleared) and takes PRIORITY over both the
    # query param below and any ticker already picked earlier this session,
    # since clicking that cross-link is an explicit request to jump to this
    # exact company - written to the "cp_ticker" widget key BEFORE the
    # selectbox below is created, which is the one time it's safe to poke a
    # widget's session-state key directly.
    _cp_jump_ticker = (st.session_state.pop("research_jump_ticker", None) or "").strip().upper()
    if _cp_jump_ticker and _cp_jump_ticker in tickers:
        st.session_state["cp_ticker"] = _cp_jump_ticker

    # Shareable deep links: /research?ticker=CSL.AX&section=Fair+Value opens
    # straight on that company/section - the same idea as page_deep_dive's
    # ?ticker= (see its "dd_qp_tried" one-shot guard). The index is computed
    # BEFORE the selectbox exists (never poke a widget's session-state key
    # after creation) and only from the query param when the widget's own
    # session-state key isn't set yet - once "cp_ticker"/"cp_section" exist
    # (after the first render, or the user's own pick), Streamlit uses that
    # stored value regardless of `index`, which is the one-shot guard here:
    # a bad/stale query param can't fight the user's later picks.
    _cp_ticker_index = 0
    if "cp_ticker" not in st.session_state:
        _qp_ticker = (st.query_params.get("ticker") or "").strip().upper()
        if _qp_ticker in tickers:
            _cp_ticker_index = tickers.index(_qp_ticker)
    # Section is now a row of clickable tabs (was a dropdown) - the default
    # tab still honours ?section= the same way the dropdown's `index` did,
    # but note the address bar can no longer live-sync to whichever tab is
    # currently open: st.tabs() doesn't report which tab is active back to
    # Python (only which one to open BY DEFAULT), so unlike the ticker
    # picker below, a link shared mid-session will reopen on the tab the
    # page first loaded on, not necessarily the one being looked at when
    # the link was copied.
    _cp_default_section = section_order[0]
    _qp_section = (st.query_params.get("section") or "").strip().lower()
    if _qp_section:
        for _s in section_order:
            if _s.lower() == _qp_section:
                _cp_default_section = _s
                break

    # Narrow column sized just enough for the Stock dropdown, followed by a
    # wide empty spacer column -- keeps it compact and bunched on the left
    # instead of stretching to half the page. The Stock picker, the Deep
    # Dive link-button, and the email-follow box all sit on this one row
    # now (previously the button lived at the bottom of the header card
    # and the follow box was its own full-width section below).
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
        # "Open the live Deep Dive" link-button (previously the middle
        # column here) was dropped per Andrew's request - just Stock and
        # the email-follow box now.
        pick_col1, pick_col3 = st.columns([1.3, 1.75])
        with pick_col1:
            ticker = st.selectbox(
                "Stock", tickers, format_func=_ticker_label, key="cp_ticker",
                index=_cp_ticker_index,
            )
        with pick_col3:
            # "Follow this company" email capture (Task 2) - per selected
            # ticker, open to signed-in and anonymous visitors alike.
            _render_follow_control(ticker, key_prefix="follow_research")

    # Keep the address bar shareable for the ticker, the same pattern
    # page_deep_dive uses for its ?ticker= - "section" is seeded once from
    # the tab default above and not kept live (see note above).
    st.query_params["ticker"] = ticker
    st.query_params["section"] = _cp_default_section

    _bump_page_view("research", ticker=ticker)

    # Author position disclosure strip (positions_store.py) - directly
    # after the stock/section picker and before any section content, so
    # it's visible no matter which section is selected. Shown identically
    # in the public factual view and the admin full view; the "Edit
    # position (admin)" expander inside it only renders when
    # full_view_unlocked is True.
    _render_position_disclosure(ticker)

    # Per-company header card (Task 5): ticker/industry/section-count/
    # last-updated. The link-button that used to sit at the bottom of this
    # card now renders in the row above instead (see pick_col2).
    _render_research_header_card(ticker, data, section_order)

    _cp_tabs = st.tabs(section_order, default=_cp_default_section)
    for _cp_label, _cp_tab in zip(section_order, _cp_tabs):
        with _cp_tab:
            st.markdown(f"### {ticker} - {_cp_label}")
            _render_cp_section(ticker, _cp_label, data)


def page_home():
    # page_home renders its own header (view badge + account bar) instead
    # of calling _render_header, so the src capture + view-count bump that
    # every other page gets for free from _render_header have to happen
    # here explicitly.
    _capture_first_src()
    _bump_page_view("home")
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
    _render_view_badge()
    paywall_engine.render_account_bar(
        extra_widget2=(_render_admin_unlock
                       if (_FACTUAL_DEFAULT and _admin_key_env
                           and not st.session_state.get("full_view_unlocked"))
                       else None)
    )

    # top row: logo
    st.markdown(
        """
<div class='sdd-navrow'>
  <span class='sdd-logo'>Stocks<span class='accent'>DeepDive</span></span>
</div>
""",
        unsafe_allow_html=True,
    )

    hero_l, hero_r = st.columns([11, 10], gap="large")
    with hero_l:
        if _factual():
            st.markdown(
                """
<div class='sdd-h1'>The <em>data and models</em> behind a valuation judgment.</div>
<div class='sdd-sub'>Live intrinsic values, quality calculations, psychology and discovery
readings &mdash; computed for any ASX or US stock, with <b>every input stated and every
estimate flagged</b>. The judgment stays yours.</div>
""",
                unsafe_allow_html=True,
            )
        else:
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
    _render_watchlist_bulk_import()

    # In hero_l (not full-width) on purpose: this used to be a top-level
    # container, which meant it could only start rendering below BOTH
    # hero columns - since hero_r's featured-analysis card is taller than
    # hero_l's search/chips block, that left a large dead gap under the
    # "Try one" chips before the ASX 200 / S&P 500 tiles appeared. Putting
    # it in hero_l lets it sit directly under hero_l's own content instead
    # of waiting on hero_r's height.
    _mood_box = hero_l.container()

    # ---- feature cards ----
    if _factual():
        st.markdown(
            """
<div class='sdd-kicker'>THE TOOLKIT</div>
<div class='sdd-h2'>Five ways in. One consistent model.</div>
<div class='sdd-secsub'>Every tool runs the same engine &mdash; the same DCF model, the same
quality calculation, the same psychology read &mdash; so the numbers always agree with
each other.</div>
<div class='sdd-cards5'>
  <a class='sdd-feat' href='/deep-dive' target='_self'>
    <div class='ic'>&#128269;</div><h3>Stock Deep Dive</h3>
    <p>The full picture for one ticker: intrinsic value vs today's price, what drives the
    Value Score, and psychology and discovery readings &mdash; every input stated.</p>
  </a>
  <a class='sdd-feat' href='/comparison' target='_self'>
    <div class='ic'>&#9878;&#65039;</div><h3>Side-by-side Comparison</h3>
    <p>Two or more tickers lined up on identical calculations &mdash; intrinsic value, quality
    calculation, psychology &mdash; as colour-coded data bars.</p>
  </a>
  <a class='sdd-feat' href='/scanner' target='_self'>
    <div class='ic'>&#128225;</div><h3>Stock Scanner</h3>
    <p>A whole index &mdash; ASX 200, S&amp;P 500 and more &mdash; as one sortable data table,
    computed nightly, with an optional sector filter. Sorting is arithmetic.</p>
  </a>
  <a class='sdd-feat' href='/research' target='_self'>
    <div class='ic'>&#128218;</div><h3>Rational Compounder Research</h3>
    <p>Hand-built research on selected compounders &mdash; a decade of reported earnings, four
    fair-value models, and documented company histories.</p>
  </a>
  <a class='sdd-feat' href='/portfolio' target='_self'>
    <div class='ic'>&#128188;</div><h3>My Portfolio</h3>
    <p>Track what you actually own against the price and fundamentals on the day you bought
    &mdash; private to your signed-in account, sign-in required.</p>
  </a>
</div>
""",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
<div class='sdd-kicker'>THE TOOLKIT</div>
<div class='sdd-h2'>Five ways in. One consistent model.</div>
<div class='sdd-secsub'>Every tool runs the same engine &mdash; the same DCF, the same quality
tests, the same psychology read &mdash; so the numbers always agree with each other.</div>
<div class='sdd-cards5'>
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
  <a class='sdd-feat' href='/portfolio' target='_self'>
    <div class='ic'>&#128188;</div><h3>My Portfolio</h3>
    <p>Add what you actually own and lock in the day-you-bought baseline &mdash; private to
    your signed-in account only, sign-in required.</p>
  </a>
</div>
""",
            unsafe_allow_html=True,
        )

    # ---- how it works ----
    if _factual():
        st.markdown(
            """
<div class='sdd-kicker' style='margin-top:40px;'>HOW IT WORKS</div>
<div class='sdd-h2'>Search. Compute. Inspect.</div>
<div class='sdd-steps'>
  <div class='sdd-step'><div class='n'>01</div><h4>Type any ticker</h4>
    <p>ASX (CSL.AX) or US (AAPL). Live data is pulled on the spot &mdash; prices, cash flows,
    news, search trends, social chatter.</p></div>
  <div class='sdd-step'><div class='n'>02</div><h4>Get one transparent calculation</h4>
    <p>The Value Score blends the quality calculation, MOS (the gap between price and
    intrinsic value), psychology and discovery &mdash; the same arithmetic every time,
    with every input shown.</p></div>
  <div class='sdd-step'><div class='n'>03</div><h4>See value AND psychology</h4>
    <p>Two separate calculations, never blurred: what the model computes from the business's
    own cash flows, and what the crowd has been doing to the price &mdash; both stated as
    numbers, side by side.</p></div>
</div>
<div class='sdd-honesty'><b>The red-flag rule:</b> whenever a number rests on a default or
average because real data wasn't available, it's shown in red. An estimate is never dressed up
as a fact &mdash; you always know which numbers are computed and which are assumed.</div>
""",
            unsafe_allow_html=True,
        )
    else:
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

    # ---- from the blog ----
    # The blog itself is server-rendered at /blog (see server.py); this is
    # its shop window on the app's home page - the latest posts as cards,
    # refreshed automatically whenever a post is published.
    try:
        _bposts = blog_store.list_posts(limit=3)
    except Exception:
        _bposts = []
    if _bposts:
        _post_cards = []
        for _bp in _bposts:
            _bdate = (_bp.get("published_at") or "")[:10]
            try:
                _bmins = blog_render.reading_time(_bp.get("body_md") or "")
            except Exception:
                _bmins = 1
            _bsum = html.escape((_bp.get("summary") or "")[:170])
            _post_cards.append(
                f"<a class='sdd-cov' style='text-decoration:none;' "
                f"href='/blog/{html.escape(_bp['slug'])}'>"
                f"<div style='color:#e6edf5;font-weight:600;font-size:15px;"
                f"line-height:1.4;'>{html.escape(_bp['title'])}</div>"
                f"<div style='color:#5b7290;font-size:12px;margin:6px 0;'>"
                f"{_bdate} &middot; {_bmins} min read</div>"
                f"<div style='color:#8aa0b8;font-size:13px;line-height:1.5;'>"
                f"{_bsum}</div></a>"
            )
        st.markdown(
            "<div class='sdd-kicker' style='margin-top:40px;'>FROM THE BLOG</div>"
            "<div class='sdd-h2'>Latest research notes</div>"
            "<div class='sdd-secsub'>The reasoning behind the numbers, written "
            "out in full &mdash; <a href='/blog' style='color:#2dd4bf;'>all "
            "posts &rarr;</a></div>"
            f"<div class='sdd-covgrid'>{''.join(_post_cards)}</div>",
            unsafe_allow_html=True,
        )

    # ---- CTA band ----
    st.markdown(
        """
<div class='sdd-cta'>
  <div>
    <div class='sdd-h2' style='margin:0 0 6px;'>Everything is free.</div>
    <div style='color:#8aa0b8;font-size:14.5px;max-width:560px;line-height:1.5;'>Sign in (top
    left) to save a watchlist and get the weekly {digest_word} digest.</div>
  </div>
</div>
""".format(digest_word="watchlist" if _factual() else "signal"),
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
    <b style='color:#e6edf5;'>{q1}</b> A live DCF with a per-stock discount rate,
    {a1}.<br><br>
    <b style='color:#e6edf5;'>Is it a good business?</b> A 0&ndash;100 Quality Score from
    profitability and balance-sheet tests.<br><br>
    <b style='color:#e6edf5;'>{q3}</b> {a3}
  </div>
</div>
""".format(
                    q1=("What is the intrinsic value?" if _factual()
                        else "What is it worth?"),
                    a1=("shown next to today's price with the MOS stated "
                        "as a percentage" if _factual()
                        else "plus margin of safety vs today's price"),
                    q3=("What is the crowd doing?" if _factual()
                        else "Is now a sane entry?"),
                    a3=("Psychology and discovery readings - distance from "
                        "recent highs, volume, search and news attention - "
                        "stated as numbers." if _factual()
                        else "Crowd psychology and a technical entry zone "
                             "&mdash; kept separate from the ownership question."),
                ),
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

    _bump_page_view(
        "deep_dive",
        ticker=(_dd.get("ticker") if _dd and not _dd.get("error") else None),
    )

    # The explanatory line only earns its space when there's nothing else
    # on the page yet - and it leads with outcomes, not model internals
    # (the methodology detail lives on the results themselves).
    if _dd is None or _dd.get("error"):
        if _factual():
            st.caption(
                "One ticker, the complete picture: an intrinsic value "
                "computed live from its own cash flows (DCF), a quality "
                "calculation from reported fundamentals, and psychology "
                "and discovery readings - every input stated, every "
                "estimate flagged, every factor behind each calculation "
                "charted."
            )
        else:
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
        _render_suggestion_chips("dd_err", st.session_state.get("search_suggestions") or [])
    else:
        if _dd["long_score"] > SIGNAL_THRESHOLDS["STRONG_LONG"]:
            _dd_signal = "STRONG LONG"
        elif _dd["long_score"] > SIGNAL_THRESHOLDS["LONG"]:
            _dd_signal = "LONG"
        elif _dd["long_score"] > SIGNAL_THRESHOLDS["WATCHLIST"]:
            _dd_signal = "WATCHLIST"
        else:
            _dd_signal = "AVOID"

        # Same tiering as _dd_signal, reworded for the factual/public view -
        # that view deliberately never shows the LONG/AVOID-style signal
        # words (see the Signal metric, which only appears in the full/RC
        # column layout below), so the Value Score heading uses the same
        # neutral EXCELLENT/GOOD/FAIR/WEAK vocabulary Quality already uses
        # instead of a second recommendation-flavoured label.
        if _dd["long_score"] > SIGNAL_THRESHOLDS["STRONG_LONG"]:
            _dd_value_word = "EXCELLENT"
        elif _dd["long_score"] > SIGNAL_THRESHOLDS["LONG"]:
            _dd_value_word = "GOOD"
        elif _dd["long_score"] > SIGNAL_THRESHOLDS["WATCHLIST"]:
            _dd_value_word = "FAIR"
        else:
            _dd_value_word = "WEAK"

        st.subheader(f"{_dd['ticker']} - {_dd['name']}")
        _render_data_as_of(_dd["ticker"])
        _render_recent_results_banner(_dd["ticker"])
        _render_score_history_caption(_dd["ticker"], _dd.get("long_score"))

        if _factual():
            _m1, _m2, _m3, _m4 = st.columns(4)
            _m1.metric("Price", f"{_dd['price']:,.2f} {_dd['currency']}", help=METRIC_HELP["Price"])
            _m2.metric(
                "Intrinsic Value",
                f"{_dd['intrinsic_value']:,.2f}" if _dd["intrinsic_value"] else "N/A",
                help=METRIC_HELP["Intrinsic Value"],
            )
            _m3.metric(
                "MOS",
                f"{_dd['mos']:+.1f}%" if _dd["mos"] is not None else "N/A",
                help=METRIC_HELP["MOS"],
            )
            _m4.metric("Value Score", f"{_dd['long_score']:.1f}", help=METRIC_HELP["Value Score"])
        else:
            _m1, _m2, _m3, _m4, _m5 = st.columns(5)
            _m1.metric("Price", f"{_dd['price']:,.2f} {_dd['currency']}", help=METRIC_HELP["Price"])
            _m2.metric(
                "Intrinsic Value",
                f"{_dd['intrinsic_value']:,.2f}" if _dd["intrinsic_value"] else "N/A",
                help=METRIC_HELP["Intrinsic Value"],
            )
            _m3.metric(
                "MOS", f"{_dd['mos']:+.1f}%" if _dd["mos"] is not None else "N/A",
                help=METRIC_HELP["MOS"],
            )
            _m4.metric("Long Score", f"{_dd['long_score']:.1f}", help=METRIC_HELP["Long Score"])
            _m5.metric("Signal", _dd_signal, help=METRIC_HELP["Signal"])

        # Task 10: flag it on screen whenever the DCF's base cash flow used
        # the 3-year median instead of the latest reporting year, because
        # that latest year was an outlier (see fcf_valuation_engine.
        # FCF_OUTLIER_THRESHOLD) - purely descriptive of the calculation,
        # not a signal.
        if _dd.get("dcf_base_normalized"):
            st.caption(
                "Note: the DCF's base cash flow used the median of the "
                f"last 3 years ({_dd.get('dcf_base_used')}) instead of the "
                f"latest reported year ({_dd.get('dcf_base_raw')}), which "
                "deviated by more than 40% - a smoothing step so one "
                "outlier reporting year can't dominate the valuation."
            )

        # DCF fixes: floored discount rate + FX currency conversion - same
        # provenance-flag pattern as the outlier-base caption above. Neither
        # is a signal; both are purely descriptive of what the calculation
        # actually did.
        if _dd.get("dcf_discount_floored"):
            st.caption(
                "Discount rate floored at 7.5% (low measured beta - see "
                "Methodology)."
            )
        if _dd.get("dcf_fx_converted"):
            _fx_rate_val = _dd.get("dcf_fx_rate_used")
            _fx_rate_txt = f"{_fx_rate_val:.2f}" if _fx_rate_val is not None else "N/A"
            if _dd.get("dcf_fx_fallback"):
                st.markdown(
                    "<div style='font-size:13px;color:#8aa0b8;margin:2px 0 8px;'>"
                    f"Cash flows converted {_dd['dcf_fx_converted']} at "
                    f"<span style='color:#fb7185;font-weight:600;'>{_fx_rate_txt}"
                    "</span> (fallback rate - live FX unavailable).</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.caption(
                    f"Cash flows converted {_dd['dcf_fx_converted']} at {_fx_rate_txt}."
                )

        # --- Research cross-link (Task 5): when this ticker has hand-built
        # Rational Compounder coverage, point straight at it. st.switch_page
        # clears all non-embed query params on navigation by default (see
        # the installed streamlit version's own switch_page() docstring),
        # so a ?ticker= set on st.query_params right before switch_page here
        # never survives the jump - the ticker is handed off via
        # st.session_state["research_jump_ticker"] instead, which
        # page_research() honours with priority over its own query param,
        # then clears. ---
        _cov_data_dd = _load_compounder_data()
        _dd_has_research = bool(
            _cov_data_dd and _dd["ticker"] in _cov_data_dd.get("tickers", {})
        )
        if _dd_has_research:
            if st.button(
                f"\U0001F4DA Hand-built research available for {_dd['ticker']} "
                "— open Rational Compounder Analysis",
                key=f"dd_research_xlink_{_dd['ticker']}",
            ):
                st.session_state["research_jump_ticker"] = _dd["ticker"]
                st.switch_page(PG_RESEARCH)

        # --- Watchlist (signed-in users): the sign-in carrot, and the
        # audience the weekly digest goes to. Follow (Task 2) sits in the
        # column next to it when this ticker has hand-built research
        # coverage - a lighter commitment than the watchlist, open to
        # anonymous visitors too. ---
        _wl_col, _follow_col = st.columns(2)
        with _wl_col:
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
                    "Sign in (top left) to save this stock to a watchlist and "
                    + ("get the weekly watchlist digest."
                       if _factual() else "get the weekly signal digest.")
                )
        if _dd_has_research:
            with _follow_col:
                _render_follow_control(_dd["ticker"], key_prefix="follow_dd")

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
                if _dd.get("trade_setup_entry") and not _factual():
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
        if _factual():
            # "What the model shows": neutral statements about inputs and
            # outputs - no buy/wait framing, no holding-period suggestion.
            with st.expander("What the model shows", expanded=True):
                _obs = []
                if _dd.get("intrinsic_value") and _dd.get("mos") is not None:
                    _obs.append(
                        f"The intrinsic value ({_dd['intrinsic_value']:,.2f} "
                        f"{_dd['currency']}) is {_dd['mos']:+.1f}% relative to the "
                        "current price (MOS), using the stated inputs shown on "
                        "this page."
                    )
                elif not _dd.get("intrinsic_value"):
                    _obs.append(
                        "No intrinsic value could be computed for this ticker "
                        "(no positive EPS/FCF for the DCF or P/E-based models)."
                    )
                _obs.append(
                    f"The quality calculation totals {_dd['quality_score']}/100 "
                    "from reported fundamentals - the term-by-term breakdown "
                    "is charted below."
                )
                _obs.append(
                    f"Psychology: {_dd['fear']:.1f}% below its 3-month "
                    f"high; greed/FOMO terms total {(_dd['greed'] + _dd['fomo']):.1f} "
                    "(see the psychology chart)."
                )
                _obs.append(
                    f"Discovery score: {_dd['discovery']:.1f} from price "
                    "activity, volume, search interest, news and social volume."
                )
                for _o in _obs:
                    st.markdown(f"- {_o}")
                st.caption(
                    "Statements describe model inputs and outputs only. Nothing "
                    "on this page is a recommendation to buy, hold or sell any "
                    "security."
                )
        elif _dd_thesis:
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

        # Value Score gauge stacked on top of Price vs Intrinsic Value
        # (previously side-by-side columns - moved to a single vertical
        # stack per request). Heading added to match the Quality/Discovery/
        # Psychology sections below, each of which leads with a
        # "X Score: value - LABEL" subheader before their gauge.
        if _factual():
            st.subheader(f"Value Score: {_dd['long_score']:.1f} - {_dd_value_word}")
            st.plotly_chart(
                _dd_gauge(
                    _dd["long_score"], "Value Score",
                    [
                        (0, SIGNAL_THRESHOLDS["WATCHLIST"], "#43222e"),
                        (SIGNAL_THRESHOLDS["WATCHLIST"], SIGNAL_THRESHOLDS["LONG"], "#43371c"),
                        (SIGNAL_THRESHOLDS["LONG"], SIGNAL_THRESHOLDS["STRONG_LONG"], "#1e3d34"),
                        (SIGNAL_THRESHOLDS["STRONG_LONG"], 100, "#27584a"),
                    ],
                ),
                use_container_width=True,
            )
        else:
            st.subheader(f"Long Score: {_dd['long_score']:.1f} - {_dd_signal}")
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
        if _factual():
            st.caption(
                "Value Score is a weighted calculation: quality 35%, "
                "MOS 25%, psychology 20%, discovery 20%. It is a "
                "description of data, not a recommendation."
            )
        else:
            st.caption(
                "Long Score, 0-100: business quality + margin of safety weighted, "
                f"nudged by psychology and attention. Above {SIGNAL_THRESHOLDS['LONG']} "
                f"= LONG territory, above {SIGNAL_THRESHOLDS['STRONG_LONG']} = STRONG LONG."
            )

        _score_word = "Value Score" if _factual() else "Long Score"
        st.plotly_chart(
            _dd_contrib_chart(
                _dd["contributions"],
                f"What's driving the {_score_word} (points contributed by each factor)",
                xaxis_title=f"Points toward {_score_word}",
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

        # Margin of Safety - moved here (after Discovery Score, before Trade
        # Setup) and redesigned into a gauge (same visual family as the
        # Quality/Psychology/Discovery dials above) paired with the actual
        # Price vs Intrinsic Value bar chart, same gauge+chart column layout
        # as those three sections use.
        st.divider()
        if _dd["intrinsic_value"]:
            _mos_val = _dd["mos"] if _dd["mos"] is not None else 0.0
            st.subheader(f"Margin of Safety: {_mos_val:+.1f}% - {_dd['valuation']}")
            _mos_gauge_val = max(-50, min(_mos_val, 100))
            _mos_col1, _mos_col2 = st.columns(2)
            with _mos_col1:
                st.plotly_chart(
                    _dd_gauge(
                        _mos_gauge_val, f"Margin of Safety - {_dd['valuation']}",
                        [(-50, 0, "#43222e"), (0, 25, "#43371c"),
                         (25, 50, "#1e3d34"), (50, 100, "#27584a")],
                        axis_range=(-50, 100),
                    ),
                    use_container_width=True,
                )
                if _mos_val != _mos_gauge_val:
                    st.caption("Dial capped at -50%/100% for readability.")
            with _mos_col2:
                _iv_color = "#34d399" if _dd["intrinsic_value"] > _dd["price"] else "#fb7185"
                fig_val = go.Figure(go.Bar(
                    x=[_dd["price"], _dd["intrinsic_value"]],
                    y=["Current Price", "Intrinsic Value (Base Case)"],
                    orientation="h",
                    marker_color=["#8aa0b8", _iv_color],
                    text=[f"{_dd['price']:,.2f}", f"{_dd['intrinsic_value']:,.2f}"],
                    textposition="outside",
                    cliponaxis=False,
                ))
                # The longer bar's outside label was getting clipped at the
                # right edge (its value sits right at Plotly's auto-ranged
                # axis max, with no room left to draw the text past it) -
                # cliponaxis=False stops the axis boundary from cutting the
                # text off, and padding the range ~18% past the larger of
                # the two values gives it room to actually sit outside the
                # bar instead of overlapping it.
                _mos_bar_max = max(_dd["price"], _dd["intrinsic_value"])
                fig_val.update_layout(
                    title="Price vs Intrinsic Value (the numbers behind the gauge)",
                    showlegend=False, height=260,
                    margin=dict(l=10, r=45, t=40, b=10),
                    xaxis_title=_dd["currency"],
                    xaxis=dict(range=[0, _mos_bar_max * 1.18]),
                )
                st.plotly_chart(fig_val, use_container_width=True)
            st.caption(
                "Green (25%+) = UNDERVALUED. Amber (0-25%) = FAIR. "
                "Red (below 0%) = EXPENSIVE - trading above intrinsic value."
            )
        else:
            st.subheader("Margin of Safety: Price vs Intrinsic Value")
            st.warning(
                "No intrinsic value could be computed for this ticker "
                "(DCF and P/E-blend both unavailable - likely a "
                "financial or a name with no positive EPS/FCF)."
            )

        if not _factual():
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
                cliponaxis=False,
            ))
            # Same fix as the Margin of Safety chart above: the longest bar's
            # outside label sat right at Plotly's auto-ranged axis max and
            # got clipped - cliponaxis=False plus ~18% range padding past
            # the largest value gives it room to render.
            _tt_max = max(v for v in _tt_x if v is not None)
            fig_trade.update_layout(
                title="Trade Setup - Entry / Stop Loss / Targets",
                xaxis_title=_dd["currency"], showlegend=False, height=300,
                margin=dict(l=10, r=45, t=40, b=10),
                xaxis=dict(range=[0, _tt_max * 1.18]),
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

        # --- Compounder View (auto): the same six research sections the
        # Rational Compounder Research page shows for its hand-covered
        # tickers, computed live here for WHATEVER ticker was just looked
        # up (auto_compounder_engine.py), rendered through the exact same
        # compounder_ui.render_section() the Research page itself uses -
        # one shared component, so the two views can never visually drift
        # apart. Collapsed by default so it never competes with the score
        # gauges above; cached 24h, so a second open of the same ticker is
        # instant. "Company Potential" (hand-written judgment) has no
        # live-data equivalent and is intentionally not part of this. ---
        st.divider()
        st.subheader("\U0001F4DA Compounder View (auto)")
        st.caption(
            f"The Rational Compounder research sections - Fundamentals · "
            f"Value vs Book · Retained Earnings · Earnings Trends · "
            f"Cost of Capital · Fair Value - computed live for "
            f"{_dd['ticker']}."
        )
        with st.status(
            f"Computing live research sections for {_dd['ticker']}...",
            expanded=False,
        ) as _acv_status:
            _acv_sections = auto_compounder_engine.build_sections(_dd["ticker"])
            _acv_status.update(
                label=f"Live research sections for {_dd['ticker']}",
                state="complete" if _acv_sections else "error",
            )
        if not _acv_sections:
            st.warning(
                f"Couldn't compute the auto Compounder View for "
                f"{_dd['ticker']} right now - the underlying data "
                "source may be unavailable."
            )
        else:
            _acv_section_order = [
                "Fundamentals", "Value vs Book", "Retained Earnings",
                "Earnings Trends", "Cost of Capital", "Fair Value",
            ]
            # Fair Value stays paywalled here too, same gate text/
            # convention as the Research page's own Fair Value section -
            # otherwise the paywall would be trivially bypassed by
            # opening this section on any ticker instead.
            _acv_gates = {
                "Fair Value": (
                    "Fair Value - full valuation methods breakdown",
                    "Four independent valuation methods side by side, "
                    "with the exact inputs behind each one.",
                    f"cp_fairvalue_auto_{_dd['ticker']}",
                ),
            }
            compounder_ui.render_tabs(
                _acv_sections, _dd["ticker"], _acv_section_order,
                key_prefix=f"acv_{_dd['ticker']}", gates=_acv_gates,
            )
            _acv_meta = _acv_sections.get("_meta", {}) or {}
            _acv_years = _acv_meta.get("statement_years")
            if _acv_years:
                _acv_years_note = f"{_acv_years} year(s) of statements"
                if _acv_years < 8:
                    _acv_years_note += (
                        " (adding an EODHD_API_KEY unlocks full "
                        "10-year statement depth)"
                    )
            else:
                _acv_years_note = "statement depth unavailable"
            st.caption(
                "Every value computed live from reported data - "
                "estimates shown in red - thresholds are the author's "
                f"own (see Methodology) - statement history: "
                f"{_acv_years_note} - descriptions of calculations, "
                "not recommendations."
            )
            # Same hand-built-research cross-link as above, offered a
            # second time down here since a visitor who opened this
            # section may not have scrolled back up to see it.
            if _dd_has_research:
                if st.button(
                    "\U0001F4D6 Hand-built research exists for this "
                    "company - open Rational Compounder Analysis →",
                    key=f"acv_research_xlink_{_dd['ticker']}",
                ):
                    st.session_state["research_jump_ticker"] = _dd["ticker"]
                    st.switch_page(PG_RESEARCH)


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


def _render_overnight_scan_table(universe_label, overnight):
    """The pre-computed overnight-scan table, shared by every universe on
    the Scanner page - originally inline in page_scanner(), extracted here
    unchanged (behaviour-for-behaviour) so the "Imported screen" cohort
    (see _render_screen_import_admin()) can show the exact same table
    instead of a second hand-maintained copy. `overnight` is a
    scan_store.load_scan(...) payload; `universe_label` only decorates the
    expander heading text.

    Each Ticker cell now links straight to that stock's Deep Dive (which
    carries the auto Compounder View) - it didn't before this was
    extracted; added here since the imported-screen row is otherwise a
    dead end (no sector/universe browse path back to it elsewhere), and
    every existing universe gets the same convenience for free."""
    with st.expander(
        f"Overnight {universe_label} scan - {len(overnight['rows'])} stocks "
        f"ranked by Long Score, computed {overnight['generated_at_label']}",
        expanded=st.session_state.get("scan_stocks") is None,
    ):
        if overnight.get("attention_lite", True):
            st.caption(
                "Pre-computed while nobody was waiting. Attention-lite "
                "(price/volume only - no news/trends/social inputs), same "
                "rule as live scans this size. Estimated/default values "
                "carry their own flag columns. Run a live scan below for "
                "current prices."
            )
        else:
            st.caption(
                "Pre-computed while nobody was waiting. Full attention "
                "(price/volume plus news/trends/social inputs) - this batch "
                "was small enough to get the same signals as a live scan or "
                "Deep Dive. Estimated/default values carry their own flag "
                "columns. Run a live scan below for current prices."
            )
        _on_rows_html = []
        for _orow in overnight["rows"]:
            _tk = _orow.get("Ticker") or "-"
            _tk_cell = (
                f"<a href='/deep-dive?ticker={_tk}' target='_self' "
                "style='color:inherit;text-decoration:underline;'><b>"
                f"{_tk}</b></a>" if _tk != "-" else "<b>-</b>"
            )
            _row_html = (
                "<tr>"
                + _td(_tk_cell)
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
            )
            if _factual():
                _row_html += (
                    _td(_badge_cell(_orow.get("Valuation", "-")))
                    + _td(_badge_cell(_orow.get("Trend", "-")))
                )
            else:
                _row_html += (
                    _td(_badge_cell(_orow.get("Valuation", "-")))
                    + _td(_badge_cell(_orow.get("Signal", "-")))
                    + _td(_badge_cell(_orow.get("Trend", "-")))
                    + _td(_badge_cell(_orow.get("Trade Setup", "-")))
                )
            _on_rows_html.append(_row_html + "</tr>")
        if _factual():
            _on_headers = ["Ticker", "Type", "Price", "Intrinsic Value",
                           "MOS", "Value Score", "Quality", "Psychology",
                           "Discovery", "Valuation", "Trend"]
        else:
            _on_headers = ["Ticker", "Type", "Price", "Intrinsic Value",
                           "MOS", "Long Score", "Quality", "Psychology",
                           "Discovery", "Valuation", "Signal", "Trend",
                           "Trade Setup"]
        st.markdown(
            _sdd_table(_on_headers, _on_rows_html, max_height=480),
            unsafe_allow_html=True,
        )
        if _factual():
            st.caption(
                "Sorted by Value Score - a described calculation (see "
                "Methodology). Sorting is arithmetic, not a recommendation."
            )
        st.caption(
            "Red values = computed from a default/average because real "
            "data wasn't available (the site-wide red-flag rule)."
        )
        st.caption(f"Universe source at scan time: {overnight['source']}")


def _render_screen_import_admin():
    """
    Admin-only block (Comparison page, full-view/ADMIN_REFRESH_KEY gated -
    same convention used elsewhere on this site for admin-only bits, e.g.
    the Scanner page's Signal/Trade Setup/Opportunity Details columns) for
    the TradingView screen CSV -> nightly scan queue workflow
    (screen_import_store.py). Lives on Comparison (not Scanner, where it
    originally shipped) so the owner can review an import's scanned
    stocks right where they'd actually compare them:

      1. An "Import screen CSV" expander: upload -> parse -> preview ->
         confirm.
      2. A table of existing imports, each with its own "Scan next 25
         now" (synchronous, same per-ticker scoring the nightly job
         uses) and "Delete" buttons, plus - once it has scanned tickers -
         its OWN results table right underneath it.

    Each import's results are deliberately shown in isolation, never
    merged with any other import's: the owner uploads a screen, reviews
    just that screen's stocks, and deletes it when done, then repeats -
    so mixing every import's scanned tickers into one shared table (as
    an earlier version of this did via screen_import_store.all_scanned_rows())
    would defeat the point. See screen_import_store.scanned_rows_for_import().

    Self-gates on _factual() so it's a no-op if this is ever called from
    somewhere that forgot to check first.
    """
    if _factual():
        return

    st.markdown("#### Imported screen (TradingView, admin-only)")

    with st.expander("Import screen CSV (admin)"):
        st.caption(
            "Upload a TradingView screener export (.csv). The symbol column is "
            "found by header name (“Ticker”/“Symbol”) or by "
            "EXCHANGE:CODE formatting; ASX -> .AX, "
            "NASDAQ/NYSE/AMEX/CBOE/OTC -> bare code (share classes like BRK.B "
            "become BRK-B). Unsupported exchanges are skipped, never guessed. "
            f"Capped at {screen_import_store.MAX_TICKERS_PER_IMPORT} recognised "
            "tickers per file - the nightly scan budget is the real constraint."
        )
        _import_name = st.text_input(
            "Name this import", key="scr_import_name",
            placeholder="e.g. TV small-cap lens A",
        )
        _BARE_TICKER_OPTIONS = {
            "Already Yahoo-format (e.g. AAPL, BHP.AX) - default": None,
            "ASX - add .AX to every bare code": "ASX",
            "US (NASDAQ/NYSE/AMEX/CBOE/OTC) - use as-is": "NASDAQ",
        }
        _bare_choice = st.selectbox(
            "Fallback ONLY for a bare code this file's own “Price - "
            "Currency” column doesn't cover (no such column, or a "
            "currency other than AUD/USD) - treat it as:",
            list(_BARE_TICKER_OPTIONS.keys()), key="scr_import_bare_exchange",
        )
        _default_exchange = _BARE_TICKER_OPTIONS[_bare_choice]
        st.caption(
            "Some TradingView export presets drop the exchange prefix "
            "entirely (just a bare code like “OCL”, no “ASX:”) - a bare "
            "ASX code with no “.AX” added silently fails every price "
            "lookup (shows up as “delisted”, not “wrong ticker”). This is "
            "handled automatically per row from the file's own “Price - "
            "Currency” column when it has one (AUD -> ASX, USD -> US) - "
            "correct even for a file that mixes both. The dropdown above "
            "only matters for a bare code that column can't explain."
        )
        _uploaded_csv = st.file_uploader(
            "TradingView CSV export", type=["csv"], key="scr_import_upload",
        )
        if _uploaded_csv is not None:
            try:
                _csv_text = _uploaded_csv.getvalue().decode("utf-8-sig")
            except UnicodeDecodeError:
                _csv_text = _uploaded_csv.getvalue().decode("latin-1")
            _parsed = screen_import_store.parse_tv_csv(
                _csv_text, default_exchange=_default_exchange,
            )
            if _parsed["error"]:
                st.error(_parsed["error"])
            else:
                _mapped = _parsed["mapped"]
                _n_skipped = len(_parsed["skipped_unsupported"]) + len(_parsed["skipped_unparsed"])
                _already_tracked = set(screen_import_store.all_imported_tickers())
                _n_dupe = sum(1 for t in _mapped if t in _already_tracked)
                st.info(
                    f"{len(_mapped)} ticker(s) recognised, {_n_skipped} skipped "
                    f"(unsupported exchange), {_n_dupe} already queued."
                )
                if _parsed["skipped_unsupported"]:
                    st.caption(
                        "Skipped (unsupported exchange): "
                        + ", ".join(f"{raw}" for raw, _exch in _parsed["skipped_unsupported"][:20])
                        + (" ..." if len(_parsed["skipped_unsupported"]) > 20 else "")
                    )
                if st.button("Confirm import", type="primary", key="scr_import_confirm"):
                    _saved = screen_import_store.save_import(
                        _import_name, _mapped, skipped_count=_n_skipped,
                    )
                    st.success(
                        f"Imported “{_saved['name']}”: {len(_saved['added'])} "
                        f"newly queued, {len(_saved['duplicate'])} already queued "
                        "elsewhere (left alone)."
                    )
                    st.rerun()

    # NOTE: deliberately OUTSIDE the "Import screen CSV (admin)" expander
    # above - each import's results table below opens its own st.expander
    # (via _render_overnight_scan_table), and Streamlit doesn't allow an
    # expander nested inside another expander.
    st.markdown("##### Existing imports")
    _imports = screen_import_store.list_imports()
    if not _imports:
        st.caption("No imports yet.")
    else:
        for _imp in _imports:
            _c1, _c2, _c3 = st.columns([4, 2, 2])
            with _c1:
                st.write(
                    f"**{_imp['name']}** - {_imp['ticker_count']} ticker(s), "
                    f"{_imp['scanned_count']} scanned / {_imp['pending_count']} "
                    f"pending / {_imp['failed_count']} failed "
                    f"(imported {_imp['created_at'][:10]})"
                )
            with _c2:
                if st.button(
                    "Scan next 25 now", key=f"scr_scan25_{_imp['id']}",
                    disabled=_imp["pending_count"] == 0,
                ):
                    _batch = screen_import_store.get_pending_for_import(_imp["id"], limit=25)
                    with st.status(
                        f"Scanning {len(_batch)} ticker(s) from “{_imp['name']}”...",
                        expanded=True,
                    ) as _status:
                        for _bi, _bt in enumerate(_batch):
                            _status.update(label=f"Scanning {_bt} ({_bi + 1}/{len(_batch)})...")
                            try:
                                _brow = nightly_scan.analyze_ticker_lite(_bt)
                                screen_import_store.mark_scanned(_bt, ok=bool(_brow), row=_brow)
                            except Exception as _bexc:
                                st.write(f"{_bt}: {_bexc}")
                                screen_import_store.mark_scanned(_bt, ok=False)
                        _status.update(
                            label=f"Done - {len(_batch)} ticker(s) scanned.",
                            state="complete",
                        )
                    st.rerun()
            with _c3:
                if st.button("Delete", key=f"scr_delete_{_imp['id']}"):
                    screen_import_store.delete_import(_imp["id"])
                    st.rerun()

            _imp_rows = screen_import_store.scanned_rows_for_import(_imp["id"])
            if _imp_rows:
                _imp_rows = sorted(
                    _imp_rows, key=lambda r: r.get("Long Score") or 0, reverse=True,
                )
                _render_overnight_scan_table(
                    _imp["name"],
                    {
                        "rows": _imp_rows,
                        "source": "TradingView import",
                        "generated_at_label": "so far - this import only",
                    },
                )
            st.divider()


def page_scanner():
    _render_header(compact=True, page_label="Scanner")
    _bump_page_view("scanner")

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
        _render_overnight_scan_table(universe, _overnight)

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
    _bump_page_view("comparison")

    if not _factual():
        _render_screen_import_admin()
        st.divider()

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
                    # The actual per-share free cash flow the DCF compounded
                    # from (fcf_valuation_engine's fcf_per_share) - previously
                    # computed and thrown away, with no way to answer "what
                    # FCF is this using" for a given stock without recreating
                    # the model by hand. fcf_source is the same provenance
                    # label already used for the "FCF" column further down
                    # ("ocf-normcapex" = latest OCF minus average capex,
                    # "fcf-median" = 3-year median of the reported FCF line,
                    # "info" = Yahoo's single freeCashflow figure, "manual" =
                    # your own override).
                    "FCF/Share Used": iv_meta.get("fcf_per_share_used"),
                    "FCF Source": iv_meta.get("fcf_source") or "-",
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
                _row_html = (
                    "<tr>"
                    + _td(f"<b>{_pr['Ticker']}</b>")
                    + _td(f"{_pr['Price']:,.2f}")
                    + _td(_bar_cell(_pr.get("Long Score"), SIGNAL_THRESHOLDS["WATCHLIST"],
                                    SIGNAL_THRESHOLDS["LONG"]), minw=110)
                )
                if not _factual():
                    _row_html += _td(_badge_cell(_pr.get("Investment Signal", "-")))
                _prev_rows.append(_row_html + "</tr>")
            _prev_headers = (
                ["Ticker", "Price", "Value Score"] if _factual()
                else ["Ticker", "Price", "Long Score", "Investment Signal"]
            )
            st.markdown(
                _sdd_table(_prev_headers, _prev_rows, max_height=420),
                unsafe_allow_html=True,
            )
            if _factual():
                st.caption(
                    "Value Score is a weighted calculation (see Methodology) - "
                    "a description of data, not a recommendation. Sorting is "
                    "arithmetic."
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
                _row_html = (
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
                )
                if _factual():
                    # Valuation / Sentiment / Trend labels stay in the
                    # public view; only Trade Setup (entry verdicts) is
                    # admin-only.
                    _row_html += (
                        _td(_badge_cell(r["Valuation"]))
                        + _td(_badge_cell(r["Sentiment"]))
                        + _td(_badge_cell(r["Trend"]))
                    )
                else:
                    _row_html += (
                        _td(_badge_cell(r["Valuation"]))
                        + _td(_badge_cell(r["Sentiment"]))
                        + _td(_badge_cell(r["Trend"]))
                        + _td(_badge_cell(r["Trade Setup"]))
                        + _td(_bar_cell(r["Trade Setup Score"], 45, 65), minw=90)
                    )
                _cmp_rows_html.append(_row_html + "</tr>")

            if _factual():
                _cmp_headers = [
                    "Ticker", "Type", "Price", "Intrinsic Value",
                    "MOS", "Value Score", "Quality", "Psychology", "Discovery",
                    "Valuation", "Sentiment", "Trend",
                ]
            else:
                _cmp_headers = [
                    "Ticker", "Type", "Price", "Intrinsic Value", "MOS",
                    "Long Score", "Quality Score", "Psychology", "Discovery",
                    "Valuation", "Sentiment", "Trend", "Trade Setup",
                    "Trade Setup Score",
                ]
            st.markdown(_sdd_table(_cmp_headers, _cmp_rows_html), unsafe_allow_html=True)
            if _factual():
                st.caption(
                    "Every column is a described calculation from stated inputs "
                    "(hover the Methodology page for definitions). Red values "
                    "rest on default or estimated inputs."
                )


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
            elif _factual():
                st.caption(
                    f"Highest Value Score in this scan: {top_stock['Ticker']} "
                    f"({top_stock['Long Score']}) - a sort result, not a "
                    "recommendation."
                )
            else:
                st.subheader("Top Investment Candidate")
                st.success(
                    f"{top_stock['Ticker']} - Long Score: {top_stock['Long Score']} "
                    f"(Investment: {top_stock['Investment Signal']}, "
                    f"Trade Setup: {top_stock['Trade Setup']})"
                )
            if not _factual():
                render_thesis(thesis_lookup[top_stock["Ticker"]])

            if not is_swing and not _factual():
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

            if not _factual():
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

            if _factual():
                _opp_iter = []
            else:
                st.subheader("Opportunity Details")
                _opp_iter = list(ranked.head(5).iterrows())
            for _, row in _opp_iter or []:
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
                    "the market. \"FCF/Share\" is the actual per-share free cash flow "
                    "the model compounded from (base FCF divided by shares "
                    "outstanding) - \"FCF Source\" says where that base came from: "
                    "ocf-normcapex (latest operating cash flow minus average capex), "
                    "fcf-median (3-year median of reported FCF), info (Yahoo's single "
                    "freeCashflow figure), or manual (your own override). Click "
                    "\"Enable manual override\" to type your own "
                    "value for any stock - leave a cell blank to keep it auto. Saved "
                    "overrides apply only to your own session - they reset to "
                    "defaults the moment you leave or reload this page, and never "
                    "affect what any other visitor sees."
                )
                if _factual():
                    st.caption(
                        "\"IV/Price Multiple\" is Intrinsic Value divided by Price "
                        "(e.g. 1.50 means the DCF values the stock at 1.5x today's "
                        "price; below 1.00 means the model output sits below the "
                        "market price). \"Value Score\" is color-coded on the same "
                        f"thresholds used everywhere else in this app: green = above "
                        f"{SIGNAL_THRESHOLDS['LONG']}, yellow = above "
                        f"{SIGNAL_THRESHOLDS['WATCHLIST']} but at or below "
                        f"{SIGNAL_THRESHOLDS['LONG']}, red = "
                        f"{SIGNAL_THRESHOLDS['WATCHLIST']} or below."
                    )
                else:
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
                            + _td(
                                _money_cell(_dr.get("FCF/Share Used"), fmt="{:,.2f}")
                                if _dr.get("FCF/Share Used") is not None else "-"
                            )
                            + _td(str(_dr.get("FCF Source") or "-"))
                            + _td(_bar_cell(_dr["Long Score"], SIGNAL_THRESHOLDS["WATCHLIST"],
                                            SIGNAL_THRESHOLDS["LONG"]), minw=90)
                            + "</tr>"
                        )
                    st.markdown(
                        _sdd_table(
                            ["Ticker", "Price", "Intrinsic Value", "IV/Price",
                             "Upside %", "DCF Growth", "Governor", "Discount",
                             "Perpetual", "FCF/Share", "FCF Source",
                             "Value Score" if _factual() else "Long Score"],
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
# MY PORTFOLIO - sign-in-only, per-user long-term holdings tracker
# (portfolio_store.py). This is the "Invested" entry tab: add a holding
# once and its starting snapshot LOCKS as the baseline everything else
# will eventually compare against (Health Score / Progress Score / News
# tabs are a later stage - this page is the entry point + the private
# holdings list only). Every read/write below is scoped to the signed-in
# user's own email, exactly like watchlist_store.py - nobody, including
# another signed-in visitor, can see a holding that isn't theirs.
# -----------------------------------

_PHE_PIE_COLORS = ["#2dd4bf", "#7c8cf8", "#f2a154", "#5fb0e8", "#c792ea",
                    "#f2789f", "#8bd17c", "#e8d05a", "#ef8a7a", "#79c7c0"]


def _ticker_color_map(tickers):
    """One colour per ticker, assigned in a fixed order and reused across
    every chart on the Holdings tab (allocation-at-purchase donut,
    allocation-now donut, P/L bar, dividend donut) so the same holding is
    always the same colour and allocation drift is visible at a glance."""
    seen = []
    for t in tickers:
        if t not in seen:
            seen.append(t)
    return {t: _PHE_PIE_COLORS[i % len(_PHE_PIE_COLORS)] for i, t in enumerate(seen)}


def _phe_pie(labels, values, title, color_map=None):
    _pairs = [(l, v) for l, v in zip(labels, values) if v and v > 0]
    if not _pairs:
        return None
    _labels, _values = zip(*_pairs)
    if color_map:
        _colors = [color_map.get(l, _PHE_PIE_COLORS[i % len(_PHE_PIE_COLORS)]) for i, l in enumerate(_labels)]
    else:
        _colors = [_PHE_PIE_COLORS[i % len(_PHE_PIE_COLORS)] for i in range(len(_labels))]
    fig = go.Figure(data=[go.Pie(
        labels=list(_labels), values=list(_values), hole=0.45,
        marker=dict(colors=_colors),
        textinfo="label+percent", textfont=dict(size=12),
    )])
    fig.update_layout(
        title=title, margin=dict(t=44, b=10, l=10, r=10), height=340,
        legend=dict(orientation="h", y=-0.15),
        paper_bgcolor="rgba(0,0,0,0)", font=dict(color="#c7d2e0"),
    )
    return fig


def _phe_pl_bar(tickers, pl_values, title, color_map=None):
    """Horizontal Profit/Loss-by-holding bar (the workbook's U column as a
    chart) - green for positive, red for negative, value labels outside
    the bars so they're legible against either colour."""
    _pairs = [(t, v) for t, v in zip(tickers, pl_values) if v is not None]
    if not _pairs:
        return None
    _pairs.sort(key=lambda p: p[1])
    _labels, _values = zip(*_pairs)
    _colors = ["#fb7185" if v < 0 else "#22c55e" for v in _values]
    _text = [f"A${v:,.0f}" for v in _values]
    fig = go.Figure(data=[go.Bar(
        x=list(_values), y=list(_labels), orientation="h",
        marker=dict(color=_colors), text=_text, textposition="outside",
        cliponaxis=False,
    )])
    fig.update_layout(
        title=title, margin=dict(t=44, b=10, l=10, r=40), height=max(320, 40 * len(_labels)),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c7d2e0"), showlegend=False,
        xaxis=dict(showgrid=False, zeroline=True, zerolinecolor="rgba(138,160,184,0.35)"),
        yaxis=dict(showgrid=False),
    )
    return fig


def _analyze_holding(h, email=None):
    """One holding's full analysis - live snapshot, News Intelligence, Health
    Score, and Progress Score - computed once per render and shared across
    the Holdings, Overview & P/L, Health & News, and Progress tabs so each
    holding is only scored (and its news only re-fetched/reclassified) a
    single time.

    ETFs (1b/1c) skip company News Intelligence entirely (analyze_holding_news
    is called with is_etf=True, which short-circuits before any feed fetch)
    and get the price-based Health blend instead of the fundamentals blend.
    A manual intrinsic-value override (1d), if one is on file for this
    user+ticker, is looked up and folded into the Valuation component."""
    is_etf = (h.get("kind") or "STOCK").upper() == "ETF"
    _snap = portfolio_health_engine.fetch_snapshot(h["ticker"])
    _iv_override = None
    if not is_etf and email:
        try:
            _iv_override = portfolio_store.get_iv_override(email, h["ticker"])
        except Exception:
            _iv_override = None
    try:
        _news = portfolio_news_engine.analyze_holding_news(
            h["ticker"], name=h.get("name"), thesis_drivers=h.get("thesis_drivers"),
            buy_date=h.get("buy_date"), is_etf=is_etf,
        )
    except Exception:
        _news = None
    _components = portfolio_health_engine.compute_health_components(
        _snap, h.get("kind"), baseline=h.get("baseline"), buy_date=h.get("buy_date"),
        news=_news, iv_override=_iv_override,
    )
    _progress = portfolio_health_engine.compute_progress(
        _snap, h.get("baseline"), h.get("kind"), h.get("buy_price"), buy_date=h.get("buy_date"),
    )
    _health = portfolio_health_engine.compute_health(
        _components, news=_news, is_etf=is_etf, progress_overall=_progress.get("overall"),
    )
    return {"snapshot": _snap, "news": _news, "components": _components,
            "health": _health, "progress": _progress, "is_etf": is_etf,
            "iv_override": _iv_override}


def _hkey(h):
    """Unique key for one holding ROW: (portfolio, ticker) rather than
    just ticker. A user can now hold the same ticker in more than one of
    their portfolios at once (different shares/buy price/baseline each
    time), so ticker alone is no longer a unique key once holdings from
    more than one portfolio can appear together (the "All portfolios"
    combined view)."""
    return (h.get("portfolio"), h["ticker"])


def _hlabels(_holdings):
    """Map _hkey(h) -> display label for every holding in `_holdings`:
    the bare ticker, unless that ticker appears in more than one
    portfolio among these holdings (only possible in the combined view),
    in which case it's disambiguated as "TICKER (Portfolio)" so two
    distinct positions in the same company never collapse into one chart
    slice or table row."""
    _counts = {}
    for h in _holdings:
        _counts[h["ticker"]] = _counts.get(h["ticker"], 0) + 1
    return {
        _hkey(h): (f"{h['ticker']} ({h.get('portfolio')})" if _counts[h["ticker"]] > 1 else h["ticker"])
        for h in _holdings
    }


def _pf_key(active_portfolio, name):
    """Streamlit widget key scoped to the currently active portfolio (or
    'all' in the combined view), so switching portfolios never carries
    over a stale text-input/selectbox value left behind by a different
    portfolio - Streamlit persists widget state by key across reruns,
    and these widgets' options/defaults change when the active portfolio
    does."""
    return f"{name}_{active_portfolio or 'all'}"


def _render_portfolio_switcher(email):
    """Portfolio selector + create/rename/delete, at the top of the
    Portfolio page above the four tabs. Returns (active_portfolio,
    is_combined) - active_portfolio is None exactly when the user has
    "All portfolios" selected, which every tab below treats as a
    read-only combined view (see the module's per-tab _is_combined
    checks)."""
    _portfolio_names = portfolio_store.list_portfolios(email)
    _combined_option = "📦 All portfolios"
    _options = [_combined_option] + _portfolio_names
    _choice = st.selectbox("Portfolio", _options, key="pf_active_portfolio_select")
    _active = None if _choice == _combined_option else _choice

    with st.expander("Manage portfolios"):
        st.caption("Create a new portfolio, or rename/delete the one currently selected above.")
        _nc1, _nc2 = st.columns([3, 1])
        with _nc1:
            _new_name = st.text_input("New portfolio name", key="pf_new_portfolio_name",
                                        placeholder="e.g. Personal")
        with _nc2:
            st.write("")
            if st.button("Create", key="pf_create_portfolio_btn"):
                _new_name = _new_name.strip()
                if not _new_name:
                    st.error("Enter a name.")
                elif _new_name in _portfolio_names:
                    st.error(f'You already have a portfolio named "{_new_name}".')
                else:
                    portfolio_store.create_portfolio(email, _new_name)
                    st.toast(f'Created "{_new_name}" - select it above to add holdings.', icon="✅")
                    st.rerun()

        if _active:
            st.divider()
            st.caption(f'Rename or delete **{_active}**')
            _rc1, _rc2 = st.columns([3, 1])
            with _rc1:
                _rename_to = st.text_input(
                    "Rename to", value=_active, key=_pf_key(_active, "pf_rename_input"),
                )
            with _rc2:
                st.write("")
                if st.button("Rename", key=_pf_key(_active, "pf_rename_btn")):
                    _rename_to = _rename_to.strip()
                    if not _rename_to:
                        st.error("Enter a name.")
                    elif _rename_to != _active and _rename_to in _portfolio_names:
                        st.error(f'You already have a portfolio named "{_rename_to}".')
                    elif _rename_to != _active:
                        portfolio_store.rename_portfolio(email, _active, _rename_to)
                        st.toast(f'Renamed to "{_rename_to}".', icon="✅")
                        st.rerun()

            if len(_portfolio_names) <= 1:
                st.caption("This is your only portfolio, so it can't be deleted - create another one first.")
            else:
                _confirm_key = _pf_key(_active, "pf_confirm_delete_portfolio")
                if not st.session_state.get(_confirm_key):
                    if st.button(f'Delete "{_active}"', key=_pf_key(_active, "pf_delete_portfolio_btn")):
                        st.session_state[_confirm_key] = True
                        st.rerun()
                else:
                    _n_here = len(portfolio_store.get_holdings(email, _active))
                    st.warning(
                        f'Delete "{_active}"' + (f" and its {_n_here} holding(s)" if _n_here else "")
                        + "? This can't be undone."
                    )
                    _dc1, _dc2 = st.columns(2)
                    with _dc1:
                        if st.button("Yes, delete", key=_pf_key(_active, "pf_delete_portfolio_confirm"), type="primary"):
                            portfolio_store.delete_portfolio(email, _active)
                            st.session_state.pop(_confirm_key, None)
                            st.toast(f'Deleted "{_active}".', icon="🗑️")
                            st.rerun()
                    with _dc2:
                        if st.button("Cancel", key=_pf_key(_active, "pf_delete_portfolio_cancel")):
                            st.session_state.pop(_confirm_key, None)
                            st.rerun()

    return _active


def page_portfolio():
    _render_header(compact=True, page_label="Portfolio")
    _bump_page_view("portfolio")

    st.markdown("#### My Portfolio")

    if not paywall_engine.is_logged_in():
        st.info(
            "Sign in (top left) to track your long-term holdings here. "
            "This is private to your account - nobody else, including "
            "other signed-in visitors, can see it."
        )
        return

    email = paywall_engine.current_user_email()
    portfolio_store.seed_desktop_import(email)  # no-op for everyone except
    # the one-off import owner, and only ever fires once even for them.
    portfolio_store.ensure_default_portfolio(email)  # no-op for anyone who
    # already has at least one portfolio (true for the seed owner right
    # after the line above); creates "Main" for a brand new visitor so
    # the selector below is never empty.

    _active_portfolio = _render_portfolio_switcher(email)

    if _active_portfolio:
        _holdings = portfolio_store.get_holdings(email, _active_portfolio)
    else:
        _holdings = portfolio_store.get_holdings_all(email)

    _analyses = {}
    if _holdings:
        with st.spinner("Scoring your holdings..."):
            # Each holding's analysis is dominated by network I/O (yfinance
            # price/history/cashflow, News Intelligence feeds for non-ETFs)
            # with no shared mutable state between holdings (each opens its
            # own sqlite connection, never touches st.* itself) - fetching
            # them one at a time made a 4-holding portfolio's first load
            # (cold st.cache_data) take as long as ~4 holdings' worth of
            # sequential network round-trips. Fetch them concurrently
            # instead; a cache-warm reload within the 30-min TTL stays fast
            # either way. Keyed by _hkey (portfolio, ticker), not ticker
            # alone - the same ticker can appear more than once in the
            # combined view, each occurrence with its own analysis.
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(_holdings))) as _pool:
                _futures = {_pool.submit(_analyze_holding, h, email=email): _hkey(h) for h in _holdings}
                for _fut in concurrent.futures.as_completed(_futures):
                    _analyses[_futures[_fut]] = _fut.result()

    _tab_holdings, _tab_overview, _tab_health, _tab_progress = st.tabs(
        ["💼 Holdings", "📊 Overview & P/L", "🩺 Health & News", "📈 Progress"]
    )

    with _tab_holdings:
        _render_portfolio_holdings_tab(email, _active_portfolio, _holdings, _analyses)
    with _tab_overview:
        _render_portfolio_overview_tab(email, _active_portfolio, _holdings, _analyses)
    with _tab_health:
        _render_portfolio_health_news_tab(email, _active_portfolio, _holdings, _analyses)
    with _tab_progress:
        _render_portfolio_progress_tab(_active_portfolio, _holdings, _analyses)


def _build_portfolio_rows(_holdings, _analyses):
    """The workbook's Invested-tab math (Part 2), computed once and shared
    by the Holdings and Overview & P/L tabs:
        cost S = shares x buy price (AUD)          value T = shares x price (AUD)
        profit U = T - S                            Purchased->Current % = (price-buy)/buy
        % at purchase = S / total cost               % now = T / total current value
        pot. dividend income L = dividend yield x T
    A holding whose live price is still unavailable contributes cost but
    not value/profit/% now - never a silent A$nan (1a) - and is named in
    the returned fx/price-missing list instead.
    """
    _fx_missing = []
    _price_missing = []
    _labels = _hlabels(_holdings)
    rows = []
    for h in _holdings:
        _a = _analyses.get(_hkey(h), {})
        _snap = _a.get("snapshot") or {}
        price = _snap.get("price")
        if price is None:
            _price_missing.append(h["ticker"])
        shares = h.get("shares") or 0
        buy_price = h.get("buy_price") or 0
        currency = h.get("currency")

        cost_aud = portfolio_health_engine.to_aud(shares * buy_price, currency, missing=_fx_missing)
        value_aud = (portfolio_health_engine.to_aud(shares * price, currency, missing=_fx_missing)
                     if price is not None else None)
        profit_aud = (value_aud - cost_aud) if (value_aud is not None and cost_aud is not None) else None
        return_pct = ((price - buy_price) / buy_price) if (price is not None and buy_price) else None
        div_yield = _snap.get("dividend_yield")
        pot_div_income_aud = (div_yield * value_aud) if (div_yield is not None and value_aud is not None) else None

        rows.append({
            "ticker": h["ticker"], "portfolio": h.get("portfolio"), "label": _labels[_hkey(h)],
            "name": h.get("name") or h["ticker"], "kind": h.get("kind"),
            "currency": currency, "buy_date": h.get("buy_date"), "shares": shares,
            "buy_price": buy_price, "current_price": price,
            "cost_aud": cost_aud, "value_aud": value_aud, "profit_aud": profit_aud,
            "return_pct": return_pct, "div_yield": div_yield,
            "pot_div_income_aud": pot_div_income_aud,
            "pct_at_purchase": None, "pct_now": None, "pct_of_balance": None,
        })

    total_cost = sum(r["cost_aud"] for r in rows if r["cost_aud"] is not None)
    total_value = sum(r["value_aud"] for r in rows if r["value_aud"] is not None)
    for r in rows:
        if r["cost_aud"] is not None and total_cost:
            r["pct_at_purchase"] = r["cost_aud"] / total_cost
        if r["value_aud"] is not None and total_value:
            r["pct_now"] = r["value_aud"] / total_value

    totals = {
        "cost_aud": total_cost or None,
        "value_aud": total_value or None,
        "profit_aud": (total_value - total_cost) if (total_value and total_cost) else None,
        "div_income_aud": sum(r["pot_div_income_aud"] for r in rows if r["pot_div_income_aud"] is not None) or None,
    }
    return rows, totals, _fx_missing, _price_missing


def _render_add_holding_expander(email, portfolio, _holdings):
    with st.expander("Add a holding", expanded=not _holdings):
        st.caption(f'Enter a ticker and look it up, then confirm the details. Added to **{portfolio}**.')
        _lc1, _lc2 = st.columns([3, 1])
        with _lc1:
            _lookup_ticker = st.text_input(
                "Ticker (Yahoo format, e.g. CSL.AX)", key=_pf_key(portfolio, "pf_lookup_ticker_input"),
            ).strip().upper()
        with _lc2:
            st.write("")
            _do_lookup = st.button("Look up", key=_pf_key(portfolio, "pf_lookup_btn"))

        _lookup_result_key = _pf_key(portfolio, "pf_lookup_result")
        if _do_lookup:
            if not _lookup_ticker:
                st.error("Enter a ticker first.")
                st.session_state.pop(_lookup_result_key, None)
            elif portfolio_store.has_holding(email, portfolio, _lookup_ticker):
                st.warning(f"{_lookup_ticker} is already in **{portfolio}** - its locked baseline stays as-is. "
                           "(You can still add it to a different portfolio.)")
                st.session_state.pop(_lookup_result_key, None)
            else:
                with st.spinner(f"Looking up {_lookup_ticker}..."):
                    try:
                        _snap = portfolio_health_engine.fetch_snapshot(_lookup_ticker)
                    except Exception:
                        _snap = None
                if not _snap or _snap.get("price") is None:
                    st.error(
                        f"Couldn't find price data for {_lookup_ticker} - double-check "
                        "the ticker (Yahoo format, e.g. CSL.AX for ASX, AAPL for Nasdaq)."
                    )
                    st.session_state.pop(_lookup_result_key, None)
                else:
                    st.session_state[_lookup_result_key] = {
                        "ticker": _lookup_ticker,
                        "name": _snap.get("name") or _lookup_ticker,
                        "currency": _snap.get("currency") or "AUD",
                        "price": _snap.get("price"),
                        "kind": "ETF" if _snap.get("quote_type") == "ETF" else "STOCK",
                    }

        _lr = st.session_state.get(_lookup_result_key)
        if _lr and _lr.get("ticker") == _lookup_ticker:
            st.success(f"Found **{_lr['ticker']}** · {_lr['name']} · {_lr['currency']} · current price {_lr['price']:,.4f}")
            with st.form(_pf_key(portfolio, "pf_add_form")):
                _fc1, _fc2 = st.columns(2)
                with _fc1:
                    _add_name = st.text_input("Company/fund name", value=_lr["name"], key=_pf_key(portfolio, "pf_add_name"))
                    _add_kind = st.selectbox(
                        "Type", ["STOCK", "ETF"], index=(0 if _lr["kind"] == "STOCK" else 1),
                        key=_pf_key(portfolio, "pf_add_kind"),
                    )
                    _add_currency = st.selectbox(
                        "Currency", ["AUD", "USD"], index=(0 if _lr["currency"] == "AUD" else 1),
                        key=_pf_key(portfolio, "pf_add_currency"),
                    )
                with _fc2:
                    _add_shares = st.number_input("Shares", min_value=0.0, step=1.0, key=_pf_key(portfolio, "pf_add_shares"))
                    _add_buy_price = st.number_input(
                        "Buy price (per share)", min_value=0.0, step=0.01, format="%.4f",
                        value=float(_lr["price"]), key=_pf_key(portfolio, "pf_add_price"),
                    )
                    _add_buy_date = st.date_input("Buy date", key=_pf_key(portfolio, "pf_add_date"))
                _add_thesis = st.text_area(
                    "Thesis (optional)", key=_pf_key(portfolio, "pf_add_thesis"), height=100,
                    placeholder="Why you bought it - sharpens the News Intelligence relevance check.",
                )
                if st.form_submit_button("Add holding", type="primary"):
                    if _add_shares <= 0:
                        st.error("Shares must be greater than zero.")
                    elif _add_buy_price <= 0:
                        st.error("Buy price must be greater than zero.")
                    elif not _add_name.strip():
                        st.error("Enter a company/fund name.")
                    else:
                        with st.spinner(f"Capturing today's baseline for {_lr['ticker']}..."):
                            _snap2 = portfolio_health_engine.fetch_snapshot(_lr["ticker"])
                        _baseline = portfolio_health_engine.baseline_snapshot_fields(_snap2 or {"price": _lr["price"]})
                        _today = datetime.now(timezone.utc).date().isoformat()
                        portfolio_store.add_holding(
                            email, portfolio, _lr["ticker"], name=_add_name.strip(), kind=_add_kind,
                            currency=_add_currency, shares=_add_shares, buy_price=_add_buy_price,
                            buy_date=_add_buy_date.isoformat(), thesis=_add_thesis,
                            baseline=_baseline, baseline_date=_today, source="website",
                        )
                        st.session_state.pop(_lookup_result_key, None)
                        st.toast(f"Added {_lr['ticker']} to {portfolio} - baseline locked as of today.", icon="✅")
                        st.rerun()


def _render_manage_holding(email, portfolio, h):
    _wkey = f"{portfolio}_{h['ticker']}"
    _mc1, _mc2 = st.columns([3, 1])
    with _mc1:
        with st.expander(f"Edit {h['ticker']}"):
            with st.form(f"pf_edit_form_{_wkey}"):
                _e_shares = st.number_input(
                    "Shares", min_value=0.0, step=1.0, value=float(h.get("shares") or 0),
                    key=f"pf_edit_shares_{_wkey}",
                )
                _e_buy_price = st.number_input(
                    "Buy price (per share)", min_value=0.0, step=0.01, format="%.4f",
                    value=float(h.get("buy_price") or 0), key=f"pf_edit_price_{_wkey}",
                )
                try:
                    _default_date = _date.fromisoformat(h["buy_date"]) if h.get("buy_date") else _date.today()
                except Exception:
                    _default_date = _date.today()
                _e_buy_date = st.date_input("Buy date", value=_default_date, key=f"pf_edit_date_{_wkey}")
                _e_thesis = st.text_area(
                    "Thesis", value=h.get("thesis") or "", height=100, key=f"pf_edit_thesis_{_wkey}",
                )
                if st.form_submit_button("Save changes", type="primary"):
                    if _e_shares <= 0 or _e_buy_price <= 0:
                        st.error("Shares and buy price must be greater than zero.")
                    else:
                        portfolio_store.update_position(
                            email, portfolio, h["ticker"], shares=_e_shares, buy_price=_e_buy_price,
                            buy_date=_e_buy_date.isoformat(),
                        )
                        portfolio_store.update_thesis(email, portfolio, h["ticker"], _e_thesis, thesis_drivers=h.get("thesis_drivers"))
                        st.toast(f"Saved changes to {h['ticker']}.", icon="✅")
                        st.rerun()
            st.caption(
                ("Baseline: imported from desktop app" if h.get("source") == "desktop_import"
                 else "Baseline: captured on this site") + f", locked {h.get('baseline_date') or '-'} (never changed by edits above)."
            )
    with _mc2:
        _confirm_key = f"pf_confirm_delete_{_wkey}"
        if not st.session_state.get(_confirm_key):
            if st.button("Delete", key=f"pf_delete_{_wkey}"):
                st.session_state[_confirm_key] = True
                st.rerun()
        else:
            st.warning(f"Delete {h['ticker']}? This can't be undone.")
            if st.button("Yes, delete", key=f"pf_delete_confirm_{_wkey}", type="primary"):
                portfolio_store.remove_holding(email, portfolio, h["ticker"])
                st.session_state.pop(_confirm_key, None)
                st.toast(f"Removed {h['ticker']}.", icon="🗑️")
                st.rerun()
            if st.button("Cancel", key=f"pf_delete_cancel_{_wkey}"):
                st.session_state.pop(_confirm_key, None)
                st.rerun()


def _render_portfolio_holdings_tab(email, active_portfolio, _holdings, _analyses):
    _is_combined = active_portfolio is None
    st.caption(
        "Your long-term holdings, live - the workbook's Invested tab. Add a "
        "holding once and its starting snapshot locks in as the baseline; "
        "editing its thesis or correcting shares/buy price later never "
        "changes that lock."
    )

    if _is_combined:
        st.info(
            "Viewing **all portfolios** combined - totals and charts below are "
            "summed across every portfolio you have. Switch to a single "
            "portfolio above to add, edit, or delete a holding."
        )
    else:
        _render_add_holding_expander(email, active_portfolio, _holdings)

    if not _holdings:
        st.info("No holdings yet - add one above to get started."
                 if not _is_combined else "No holdings in any portfolio yet.")
        return

    _rows, _totals, _fx_missing, _price_missing = _build_portfolio_rows(_holdings, _analyses)

    if _is_combined:
        _settings = portfolio_store.get_settings_all(email)
    else:
        _settings = portfolio_store.get_settings(email, active_portfolio)
    _transferred, _cash = _settings.get("total_transferred"), _settings.get("cash_held")
    if _transferred:
        for r in _rows:
            r["pct_of_balance"] = (r["cost_aud"] / _transferred) if r["cost_aud"] is not None else None

    if _is_combined:
        if _transferred or _cash:
            st.caption(
                f"Portfolio settings shown below are the sum across your portfolios "
                f"that have them filled in - edit an individual portfolio's settings "
                f"from its own Holdings tab."
            )
    else:
        with st.expander("Portfolio settings"):
            st.caption(
                "Optional - fill these in to see Profit-to-Transferred and each "
                "holding's % of your total balance."
            )
            _sc1, _sc2 = st.columns(2)
            with _sc1:
                _new_transferred = st.number_input(
                    "Total capital transferred (AUD)", min_value=0.0, step=100.0,
                    value=float(_transferred or 0.0), key=_pf_key(active_portfolio, "pf_settings_transferred"),
                )
            with _sc2:
                _new_cash = st.number_input(
                    "Cash held (AUD)", min_value=0.0, step=100.0,
                    value=float(_cash or 0.0), key=_pf_key(active_portfolio, "pf_settings_cash"),
                )
            if st.button("Save portfolio settings", key=_pf_key(active_portfolio, "pf_settings_save")):
                portfolio_store.set_settings(
                    email, active_portfolio, total_transferred=(_new_transferred or None), cash_held=(_new_cash or None),
                )
                st.toast("Saved.", icon="✅")
                st.rerun()

    _has_transfer_kpi = bool(_transferred)
    _kpi_cols = st.columns(5 if _has_transfer_kpi else 4)
    _kpi_cols[0].metric("Total invested", f"A${_totals['cost_aud']:,.0f}" if _totals["cost_aud"] else "–")
    _kpi_cols[1].metric("Current value", f"A${_totals['value_aud']:,.0f}" if _totals["value_aud"] else "–")
    if _totals["profit_aud"] is not None and _totals["cost_aud"]:
        _kpi_cols[2].metric("Unrealised P/L (AUD)", f"A${_totals['profit_aud']:,.0f}",
                             f"{_totals['profit_aud'] / _totals['cost_aud'] * 100:+.1f}%")
    else:
        _kpi_cols[2].metric("Unrealised P/L (AUD)", "–")
    _kpi_cols[3].metric("Est. annual dividends", f"A${_totals['div_income_aud']:,.0f}" if _totals["div_income_aud"] else "–")
    if _has_transfer_kpi:
        _pt = None
        if _totals["value_aud"] is not None and _totals["cost_aud"] is not None:
            _cash_v = _cash or 0.0
            # Owner's formula: (value + cash + (transferred - cost) - transferred) / transferred
            # - algebraically (value + cash - cost) / transferred; kept both
            # forms here so the spreadsheet expression stays traceable.
            _pt = (_totals["value_aud"] + _cash_v - _totals["cost_aud"]) / _transferred
        _kpi_cols[4].metric("Profit-to-Transferred", f"{_pt * 100:+.1f}%" if _pt is not None else "–")

    if _price_missing:
        st.caption(
            "No live price yet for: " + ", ".join(sorted(set(_price_missing)))
            + " - those holdings are left out of current value, P/L, and % now above rather than shown as A$nan."
        )
    if _fx_missing:
        st.caption(
            "Couldn't fetch a live FX rate for: " + ", ".join(sorted(set(_fx_missing)))
            + " - those holdings are left out of the AUD totals above rather than guessed."
        )

    _cmap = _ticker_color_map([r["label"] for r in _rows])

    st.markdown("##### Allocation")
    _dc1, _dc2 = st.columns(2)
    with _dc1:
        _fig_purchase = _phe_pie([r["label"] for r in _rows], [r["cost_aud"] for r in _rows],
                                  "Allocation at purchase (% of cost)", color_map=_cmap)
        if _fig_purchase:
            st.plotly_chart(_fig_purchase, use_container_width=True)
        else:
            st.caption("No cost data yet.")
    with _dc2:
        _fig_now = _phe_pie([r["label"] for r in _rows], [r["value_aud"] for r in _rows],
                             "Allocation now (% of current value)", color_map=_cmap)
        if _fig_now:
            st.plotly_chart(_fig_now, use_container_width=True)
        else:
            st.caption("No current-value data yet.")

    st.markdown("##### Profit/Loss by holding")
    _fig_pl = _phe_pl_bar([r["label"] for r in _rows], [r["profit_aud"] for r in _rows], "Profit/Loss by holding (AUD)")
    if _fig_pl:
        st.plotly_chart(_fig_pl, use_container_width=True)
    else:
        st.caption("No P/L data yet.")

    st.markdown("##### Estimated annual dividend income")
    st.caption(
        "Estimated from each holding's current dividend yield × current value (AUD) - "
        "not a record of dividends actually received (the site doesn't track that yet)."
    )
    _fig_div = _phe_pie([r["label"] for r in _rows], [r["pot_div_income_aud"] for r in _rows],
                         "Estimated annual dividend income (AUD)", color_map=_cmap)
    if _fig_div:
        st.plotly_chart(_fig_div, use_container_width=True)
    else:
        st.caption("No dividend-paying holdings yet.")

    st.markdown("##### Holdings table")
    _table_rows = []
    _raw_profit, _raw_return_pct = [], []
    for r in _rows:
        _return_pct_val = (r["return_pct"] * 100) if r["return_pct"] is not None else None
        _row = {"Ticker": r["ticker"]}
        if _is_combined:
            _row["Portfolio"] = r["portfolio"]
        _row.update({
            "Name": r["name"], "Buy date": r["buy_date"],
            "Shares": _pf_cell(r["shares"], "{:,.0f}"),
            "Buy price": _pf_cell(r["buy_price"], "{:,.2f}"),
            "Current price": _pf_cell(r["current_price"], "{:,.2f}"),
            "Purchased→Current %": _pf_cell(_return_pct_val, "{:+.2f}%"),
            "Cost (AUD)": _pf_cell(r["cost_aud"], "A${:,.2f}"),
            "Value (AUD)": _pf_cell(r["value_aud"], "A${:,.2f}"),
            "Profit (AUD)": _pf_cell(r["profit_aud"], "A${:,.2f}"),
            "% at purchase": _pf_cell((r["pct_at_purchase"] * 100) if r["pct_at_purchase"] is not None else None, "{:.2f}%"),
            "% now": _pf_cell((r["pct_now"] * 100) if r["pct_now"] is not None else None, "{:.2f}%"),
            "Div yield": _pf_cell((r["div_yield"] * 100) if r["div_yield"] is not None else None, "{:.2f}%"),
            "Pot. div income (AUD)": _pf_cell(r["pot_div_income_aud"], "A${:,.2f}"),
        })
        if _transferred:
            _row["% of balance"] = _pf_cell((r["pct_of_balance"] * 100) if r["pct_of_balance"] is not None else None, "{:.2f}%")
        _table_rows.append(_row)
        _raw_profit.append(r["profit_aud"])
        _raw_return_pct.append(_return_pct_val)

    _tdf = pd.DataFrame(_table_rows)
    # Every cell above is already its final display string (see _pf_cell) -
    # colouring below keys off the parallel raw-value lists by row
    # position instead of the (now-stringified) column values.
    st.dataframe(
        _tdf.style
        .apply(lambda col: _pf_pl_color(_raw_profit), subset=["Profit (AUD)"])
        .apply(lambda col: _pf_pl_color(_raw_return_pct), subset=["Purchased→Current %"]),
        use_container_width=True, hide_index=True,
    )

    _costed = [r for r in _rows if r["cost_aud"] is not None]
    if _costed:
        _by_cost = sorted(_costed, key=lambda r: r["cost_aud"])
        _max_r, _min_r = _by_cost[-1], _by_cost[0]
        st.caption(
            f"Max investment: **{_max_r['label']}** (A${_max_r['cost_aud']:,.0f}) · "
            f"Min investment: **{_min_r['label']}** (A${_min_r['cost_aud']:,.0f})"
        )

    if not _is_combined:
        st.markdown("##### Manage a holding")
        _mtk = st.selectbox("Choose a holding to edit or delete", [r["ticker"] for r in _rows],
                             key=_pf_key(active_portfolio, "pf_manage_ticker"))
        _mh = next(h for h in _holdings if h["ticker"] == _mtk)
        _render_manage_holding(email, active_portfolio, _mh)


def _render_portfolio_overview_tab(email, active_portfolio, _holdings, _analyses):
    _is_combined = active_portfolio is None
    st.caption(
        "Health, Progress, News Risk, and P/L for every holding at a glance, "
        "plus a second table of progress since purchase."
    )
    if not _holdings:
        st.info("Add a holding on the Holdings tab to see your Overview.")
        return

    _rows, _totals, _fx_missing, _price_missing = _build_portfolio_rows(_holdings, _analyses)
    _by_key = {(r["portfolio"], r["ticker"]): r for r in _rows}

    _table_rows, _prog_rows = [], []
    _raw_health, _raw_news_risk, _raw_pl, _raw_progress = [], [], [], []
    _thesis_intact_count = 0
    for h in _holdings:
        _a = _analyses[_hkey(h)]
        _health, _news, _progress = _a["health"], _a["news"], _a["progress"]
        _r = _by_key[_hkey(h)]
        _is_etf = bool(_a.get("is_etf"))

        _prev = portfolio_health_engine.record_health_run(
            email, h.get("portfolio"), h["ticker"], _health["overall"], news_risk=(_news or {}).get("news_risk_score"),
        )
        _delta_run = round(_health["overall"] - _prev, 1) if (_prev is not None and _health["overall"] is not None) else None

        _return_pct = (_r["return_pct"] * 100) if _r["return_pct"] is not None else None
        _thesis_breaking = bool(_health.get("thesis_breaking"))
        if not _thesis_breaking:
            _thesis_intact_count += 1

        _health_val = _health["overall"]
        _news_risk_val = (_news or {}).get("news_risk_score")
        _pl_val = _r["profit_aud"]
        _progress_val = _progress["overall"]

        _table_row = {"Ticker": h["ticker"]}
        if _is_combined:
            _table_row["Portfolio"] = h.get("portfolio")
        _table_row.update({
            "Name": h.get("name") or h["ticker"],
            "Health": _pf_cell(_health_val, "{:.0f}"), "Δ run": _pf_cell(_delta_run, "{:+.1f}"),
            "Action": _health["action"],
            "Thesis": ("N/A (ETF)" if _is_etf else ("Review" if _thesis_breaking else "Intact")),
            "Buy price": _pf_cell(h.get("buy_price"), "{:.4f}"),
            "Current price": _pf_cell(_r["current_price"], "{:.4f}"),
            "Return %": _pf_cell(_return_pct, "{:+.1f}%"),
            "Unrealised P/L (AUD)": _pf_cell(_pl_val, "A${:,.2f}"),
            # ETFs skip company News Intelligence outright (1b) - shows as
            # "–", never the misleading "100 = clean" a stub score would imply.
            "News Risk": _pf_cell(_news_risk_val, "{:.0f}"),
            "Flags": len(_health.get("red_flags") or []),
            "Weight %": _pf_cell((_r["pct_now"] * 100) if _r["pct_now"] is not None else None, "{:.1f}%"),
        })
        _table_rows.append(_table_row)
        _prog_row = {"Ticker": h["ticker"]}
        if _is_combined:
            _prog_row["Portfolio"] = h.get("portfolio")
        _prog_row.update({
            "Progress": _pf_cell(_progress_val, "{:.0f}"),
            "Return since buy %": _pf_cell(_return_pct, "{:+.1f}%"), "Verdict": _progress["verdict"],
            "Baseline date": h.get("baseline_date"),
        })
        _prog_rows.append(_prog_row)
        _raw_health.append(_health_val)
        _raw_news_risk.append(_news_risk_val)
        _raw_pl.append(_pl_val)
        _raw_progress.append(_progress_val)

    # Concentration warning (1a fix): amber when one holding exceeds 40% of
    # current value, or the top two together exceed 65% - genuinely
    # computable now that ETF prices resolve (see the fallback chain in
    # fetch_snapshot); previously Weight % never populated for this
    # portfolio's ETFs, so the warning was permanently dead ("None").
    _weighted = sorted((r for r in _rows if r["pct_now"] is not None), key=lambda r: r["pct_now"], reverse=True)
    _warn_tickers = []
    if _weighted and _weighted[0]["pct_now"] > 0.40:
        _warn_tickers = [_weighted[0]["label"]]
    elif len(_weighted) >= 2 and (_weighted[0]["pct_now"] + _weighted[1]["pct_now"]) > 0.65:
        _warn_tickers = [_weighted[0]["label"], _weighted[1]["label"]]

    _k1, _k2, _k3, _k4 = st.columns(4)
    _k1.metric("Holdings", len(_holdings))
    _k2.metric("Thesis intact", f"{_thesis_intact_count}/{len(_holdings)}")
    if _totals["profit_aud"] is not None and _totals["cost_aud"]:
        _k3.metric("Unrealised P/L (AUD)", f"A${_totals['profit_aud']:,.0f}",
                   f"{_totals['profit_aud'] / _totals['cost_aud'] * 100:+.1f}%")
    else:
        _k3.metric("Unrealised P/L (AUD)", "–")
    _k4.metric("Concentration warning", "Balanced" if not _warn_tickers else ", ".join(_warn_tickers))
    if _warn_tickers:
        st.caption(
            ("⚠️ " + ", ".join(_warn_tickers) + (" makes up over 40% of your current value."
             if len(_warn_tickers) == 1 else " together make up over 65% of your current value."))
        )

    _df = pd.DataFrame(_table_rows)
    # See _pf_cell: every cell above is already its final display string;
    # colouring keys off the parallel raw-value lists by row position.
    def _score_color(vals):
        return [(f"color: {portfolio_health_engine.score_color(v)}; font-weight:700" if v is not None else "")
                for v in vals]
    st.dataframe(
        _df.style
        .apply(lambda col: _score_color(_raw_health), subset=["Health"])
        .apply(lambda col: _score_color(_raw_news_risk), subset=["News Risk"])
        .apply(lambda col: _pf_pl_color(_raw_pl), subset=["Unrealised P/L (AUD)"])
        .map(lambda v: "color:#d03b3b;font-weight:700" if v == "Review" else "", subset=["Thesis"]),
        use_container_width=True, hide_index=True,
    )
    if _price_missing:
        st.caption(
            "No live price yet for: " + ", ".join(sorted(set(_price_missing)))
            + " - those holdings are left out of P/L and Weight % above rather than shown as A$nan."
        )
    if _fx_missing:
        st.caption(
            "Couldn't fetch a live FX rate for: " + ", ".join(sorted(set(_fx_missing)))
            + " - those holdings are left out of the P/L, concentration, and weight figures above rather than guessed."
        )

    st.markdown("##### Progress since purchase")
    _prog_df = pd.DataFrame(_prog_rows)
    st.dataframe(
        _prog_df.style
        .apply(lambda col: [(f"color: {portfolio_health_engine.score_color(v)}; font-weight:700" if v is not None else "")
                             for v in _raw_progress], subset=["Progress"]),
        use_container_width=True, hide_index=True,
    )


def _pf_pct_or_dash(v):
    return f"{v * 100:+.1f}%" if v is not None else "n/a"


def _pf_cell(v, template):
    """Format v with template, or '-' for missing - baked directly into
    the final display string rather than left as a bare None/NaN for
    pandas Styler's na_rep to hide later.

    Real bug this fixed: the deployed pandas/streamlit combo (this repo
    pins neither version, so the exact build can drift) round-trips a
    styled dataframe's raw values through `.astype(str)` before patching
    in the Styler-computed display strings - and that patch didn't
    reliably cover every None/NaN cell, so missing values rendered as the
    literal text "None" in production instead of "-". Pre-baking every
    cell into its final string here means there is never a None/NaN left
    in the DataFrame for that round-trip to leak."""
    if v is None:
        return "–"
    if isinstance(v, float) and v != v:  # NaN
        return "–"
    return template.format(v)


def _pf_pl_color(vals):
    """Green/red CSS per row from a parallel list of raw (possibly None)
    numeric values - shared by every Portfolio table that colours a P/L or
    return-% column by row position (see _pf_cell for why raw values are
    kept separately from the already-stringified display cells)."""
    return [("color:#22c55e;font-weight:700" if (v is not None and v > 0)
             else ("color:#fb7185;font-weight:700" if (v is not None and v < 0) else ""))
            for v in vals]


def _render_portfolio_health_scoring_expander():
    with st.expander("How this score works"):
        st.markdown("##### Health Score - how healthy the holding looks right now")
        st.caption(
            "A weighted blend of fundamentals (looked up against fixed score "
            "bands, below), two purchase-relative reads (price action, dividend "
            "change), Valuation/DCF, and News Intelligence - only components "
            "with data available contribute, reweighted so the rest still sum "
            "to 100%. ETFs/funds use a separate price-based blend instead (see "
            "below) since there's no fundamentals model or company thesis for a fund."
        )
        st.dataframe(
            pd.DataFrame(
                {"Component": portfolio_health_engine.COMPONENT_ORDER,
                 "Weight": [f"{portfolio_health_engine.BASE_WEIGHTS[k]*100:.0f}%"
                            for k in portfolio_health_engine.COMPONENT_ORDER]}
            ),
            use_container_width=True, hide_index=True,
        )

        st.markdown("**Score bands**")
        st.caption(
            "Each fundamental/price-action/income read is looked up on a fixed "
            "piecewise curve - unchanged from the desktop app's config.py."
        )
        _band_specs = [
            ("Growth (mean of revenue & earnings growth; also reused for FCF growth)",
             portfolio_health_engine.GROWTH_BAND, "Growth rate"),
            ("Margins (profit margin)", portfolio_health_engine.MARGIN_BAND, "Margin"),
            ("ROIC (return on equity)", portfolio_health_engine.ROE_BAND, "ROE"),
            ("Debt (debt/equity)", portfolio_health_engine.DEBT_BAND, "Debt/Equity"),
            ("Valuation (margin of safety %)", portfolio_health_engine.VAL_BAND, "MOS %"),
            ("Price Action - drawdown from the post-purchase peak",
             portfolio_health_engine.DRAWDOWN_BAND, "Drawdown %"),
            ("Price Action - position in the 52-week range",
             portfolio_health_engine.RANGE52_BAND, "Position (0-1)"),
            ("Income - dividend yield change since the baseline",
             portfolio_health_engine.INCOME_BAND, "Yield change"),
        ]
        for label, band, unit in _band_specs:
            with st.expander(label):
                st.dataframe(
                    pd.DataFrame({unit: [b[0] for b in band], "Score": [b[1] for b in band]}),
                    use_container_width=True, hide_index=True,
                )

        st.caption(
            f"Action bands: below {portfolio_health_engine.ACTION_REVIEW} = REVIEW/REDUCE, "
            f"below {portfolio_health_engine.ACTION_WATCH} = HOLD & WATCH, "
            f"{portfolio_health_engine.ACTION_STRONG}+ = HOLD or HOLD/ADD depending on valuation."
        )

        st.markdown("##### ETFs/funds - Price-based health")
        st.caption(
            "No fundamentals, DCF, Income, or News component exists for a "
            "fund, so rather than default those to a neutral read, ETF "
            "holdings get a dedicated blend: the average of Price Action "
            "(trend vs the 200-day average, position in the 52-week range, "
            "drawdown from the post-purchase peak) and the Progress score vs "
            "the locked baseline."
        )

        st.markdown("##### News Intelligence - how news moves the Health Score")
        st.caption(
            "Companies only (ETFs/funds skip this entirely - see above). Every "
            "headline is classified on two axes - severity (noise → "
            "temporary → material → thesis-breaking) and relevance (does it "
            "touch this holding's thesis?) - and only news that's BOTH relevant "
            "AND at least material can move the News Risk Score."
        )
        st.dataframe(
            pd.DataFrame([
                {"Severity": name, "Score hit": portfolio_news_engine.SEVERITY_HIT.get(key),
                 "What it means": desc, "Effect": effect}
                for name, key, desc, effect in portfolio_news_engine.SEVERITY_DEFS
            ]),
            use_container_width=True, hide_index=True,
        )
        st.code(
            "Thesis component = 0.65 x fundamentals average + 0.35 x News Risk Score\n"
            "    (capped at 30 if a thesis-breaking event is detected)\n"
            "\n"
            "News Risk Score starts at 100. For each day with relevant, at-least-\n"
            "material news: subtract severity_hit x recency_weight for that day's\n"
            "worst event, plus severity_hit x recency_weight x 0.40 for every\n"
            "additional event the same day (NEWS_PERDAY_EXTRA) - so five outlets\n"
            "covering one event in one day doesn't quintuple the penalty.\n"
            "\n"
            "Overall Health Score adjustment (only applied when material news exists):\n"
            f"    news_adjustment = -(100 - News Risk Score) x {portfolio_news_engine.NEWS_IMPACT}\n"
            "    overall = clamp(overall + news_adjustment, 0, 100)",
            language="text",
        )


def _render_portfolio_health_news_tab(email, active_portfolio, _holdings, _analyses):
    _is_combined = active_portfolio is None
    st.caption(
        "How healthy each holding looks right now - fundamentals blended with "
        "purchase-relative price action, Valuation/DCF, and News Intelligence "
        "for companies; a dedicated price-based blend for ETFs/funds."
    )
    _render_portfolio_health_scoring_expander()

    if not _holdings:
        st.info("Add a holding on the Holdings tab to see its Health Score.")
        return

    _labels = _hlabels(_holdings)
    _rows = []
    _raw_health_summary = []
    for h in _holdings:
        _a = _analyses[_hkey(h)]
        _health_val = _a["health"]["overall"]
        _row = {"Ticker": h["ticker"]}
        if _is_combined:
            _row["Portfolio"] = h.get("portfolio")
        _row.update({
            "Name": h.get("name") or h["ticker"],
            "Health": _pf_cell(_health_val, "{:.0f}"),
            "Score type": _a["health"].get("score_label", "Investment Health Score"),
            "Action": _a["health"]["action"],
            "Price": _pf_cell(_a["snapshot"].get("price"), "{:.2f}"),
            "Buy price": _pf_cell(h.get("buy_price"), "{:.4f}"),
        })
        _rows.append(_row)
        _raw_health_summary.append(_health_val)

    _df = pd.DataFrame(_rows)
    st.dataframe(
        _df.style
        .apply(lambda col: [(f"color: {portfolio_health_engine.score_color(v)}; font-weight:700" if v is not None else "")
                             for v in _raw_health_summary], subset=["Health"]),
        use_container_width=True, hide_index=True,
    )

    st.markdown("##### Holding detail")
    _tk = st.selectbox(
        "Choose a holding", [_hkey(h) for h in _holdings], format_func=lambda k: _labels[k],
        key=_pf_key(active_portfolio, "pf_health_ticker"),
    )
    _h = next(h for h in _holdings if _hkey(h) == _tk)
    _a = _analyses[_tk]
    _snap, _news, _health, _progress = _a["snapshot"], _a["news"], _a["health"], _a["progress"]
    _is_etf = bool(_a.get("is_etf"))

    _m1, _m2, _m3, _m4 = st.columns(4)
    _m1.metric("Buy price", f"{(_h.get('buy_price') or 0):,.4f}")
    _m2.metric("Current price", f"{(_snap.get('price') or 0):,.4f}" if _snap.get("price") is not None else "–")
    if _h.get("buy_price") and _snap.get("price") is not None:
        _ret = (_snap["price"] - _h["buy_price"]) / _h["buy_price"] * 100
        _m3.metric("Return since buy", f"{_ret:+.1f}%")
    else:
        _m3.metric("Return since buy", "–")
    _m4.metric("Shares", f"{(_h.get('shares') or 0):,.0f}")

    _score_label = _health.get("score_label", "Investment Health Score")
    if _is_etf:
        st.info(
            f"**{_score_label}** - this is a fund, so Health blends Price "
            "Action (trend vs the 200-day average, position in the 52-week "
            "range, drawdown from the post-purchase peak) with Progress vs "
            "baseline. Fundamentals, DCF/Valuation, Income, and News are "
            "excluded outright rather than defaulted."
        )
        _sc1, _sc2 = st.columns([1, 2])
        with _sc1:
            st.markdown(portfolio_health_engine.big_score_html(_score_label, _health["overall"]),
                         unsafe_allow_html=True)
            st.markdown(portfolio_health_engine.action_badge_html(_health["action"], _health["action_tone"]),
                         unsafe_allow_html=True)
        with _sc2:
            _pa = (_health["components"].get("Price Action") or {})
            _pa_score = _pa.get("score")
            st.markdown(
                f"**Price Action:** {_pa_score:.0f}/100" if _pa_score is not None else "**Price Action:** n/a",
            )
            st.markdown(
                f"**Progress vs baseline:** {_progress['overall']:.0f}/100"
                if _progress.get("overall") is not None else "**Progress vs baseline:** n/a",
            )
    else:
        _sc1, _sc2 = st.columns([1, 2])
        with _sc1:
            st.markdown(portfolio_health_engine.big_score_html(_score_label, _health["overall"]),
                         unsafe_allow_html=True)
            st.markdown(portfolio_health_engine.action_badge_html(_health["action"], _health["action_tone"]),
                         unsafe_allow_html=True)
        with _sc2:
            st.markdown(
                portfolio_health_engine.component_bars_html(_health["components"], portfolio_health_engine.COMPONENT_ORDER),
                unsafe_allow_html=True,
            )

    if _health["red_flags"]:
        st.warning("**Red flags:** " + "; ".join(_health["red_flags"]))

    if _h.get("thesis"):
        with st.expander("Thesis"):
            st.write(_h["thesis"])

    # Valuation / DCF block + manual override (1d)
    st.markdown("##### Valuation (DCF)")
    if _is_etf:
        st.caption(
            "No DCF/intrinsic-value model exists for ETFs/funds, so there's "
            "nothing here to override either - ETF Health is scored from "
            "price action and progress instead (see above)."
        )
    else:
        _model_iv = _snap.get("intrinsic_value")
        _cur_price = _snap.get("price")
        _mos = _snap.get("mos_pct")
        _iv_override = _a.get("iv_override")
        _vc1, _vc2, _vc3 = st.columns(3)
        _vc1.metric("Model intrinsic value", f"A${_model_iv:,.2f}" if _model_iv else "–")
        _vc2.metric("Current price", f"A${_cur_price:,.2f}" if _cur_price is not None else "–")
        _vc3.metric("Margin of safety", f"{_mos:+.1f}%" if _mos is not None else "–")
        st.caption(
            f"Key inputs: revenue growth {_pf_pct_or_dash(_snap.get('revenue_growth'))} · "
            f"earnings growth {_pf_pct_or_dash(_snap.get('earnings_growth'))} · "
            f"profit margin {_pf_pct_or_dash(_snap.get('profit_margin'))} · "
            f"ROE {_pf_pct_or_dash(_snap.get('roe'))} · "
            f"debt/equity {_snap.get('debt_to_equity'):.0f}" if _snap.get("debt_to_equity") is not None
            else "debt/equity n/a"
        )
        if _iv_override is not None:
            _badge = f"Using your manual override: A${_iv_override:,.2f}"
            if _model_iv:
                _badge += f" (model says A${_model_iv:,.2f})"
            st.success(_badge)
        # iv_overrides is keyed by (email, ticker) only, deliberately shared
        # across every portfolio that holds this ticker (see
        # portfolio_store's module docstring) - so the store calls below use
        # _h["ticker"] alone, never the (portfolio, ticker) _tk key that
        # only identifies this one row on screen.
        with st.form(_pf_key(active_portfolio, f"pf_iv_override_form_{_h['ticker']}")):
            _new_override = st.number_input(
                "My intrinsic value (manual override)", min_value=0.0, step=0.01, format="%.2f",
                value=float(_iv_override or _model_iv or 0.0),
                key=_pf_key(active_portfolio, f"pf_iv_override_{_h['ticker']}"),
            )
            _oc1, _oc2 = st.columns(2)
            with _oc1:
                _save_override = st.form_submit_button("Save override", type="primary")
            with _oc2:
                _clear_override = st.form_submit_button("Clear override")
        if _save_override:
            if _new_override <= 0:
                st.error("Enter a value greater than zero.")
            else:
                portfolio_store.set_iv_override(email, _h["ticker"], _new_override)
                st.toast(f"Saved - {_h['ticker']}'s Health will use your value (in every portfolio that holds it).", icon="✅")
                st.rerun()
        if _clear_override:
            portfolio_store.clear_iv_override(email, _h["ticker"])
            st.toast(f"Cleared - {_h['ticker']}'s Health will use the model value again.", icon="✅")
            st.rerun()

    if _is_etf:
        return

    st.markdown("##### News Intelligence")
    if _news is None:
        st.caption("News data isn't available for this holding right now - try again shortly.")
    else:
        _nc1, _nc2 = st.columns([1, 2])
        with _nc1:
            st.markdown(portfolio_health_engine.big_score_html("News Risk Score", _news["news_risk_score"]),
                         unsafe_allow_html=True)
        with _nc2:
            st.caption(_news["explain"])
            st.caption(f"{_news['all_relevant']} relevant item(s) out of {_news['scanned']} scanned.")
            if _news.get("source_counts"):
                st.caption("Sources — " + ", ".join(
                    f"{k}: {v}" for k, v in sorted(_news["source_counts"].items())))

        if _news.get("today"):
            st.markdown("**Today's critical news**")
            st.markdown(portfolio_news_engine.timeline_html(_news["today"]), unsafe_allow_html=True)

        st.markdown("**Timeline of significant events**")
        st.markdown(portfolio_news_engine.timeline_html(_news.get("timeline", []), limit=12),
                     unsafe_allow_html=True)

        st.markdown("**Summary**")
        for line in portfolio_news_engine.summarise(_news):
            st.markdown(f"- {line}")

        with st.expander(f"All classified events ({len(_news.get('timeline', []))})"):
            st.markdown(portfolio_news_engine.timeline_html(_news.get("timeline", []), limit=200),
                         unsafe_allow_html=True)


def _render_portfolio_progress_tab(active_portfolio, _holdings, _analyses):
    _is_combined = active_portfolio is None
    st.caption(
        "How each tracked metric has moved since the locked baseline snapshot "
        "taken the day a holding was added. 50 = unchanged, 100 = doubled, "
        "0 = halved (or worse), linear in between."
    )
    with st.expander("How this score works"):
        st.markdown("##### Progress Score - how it's moved since you bought")
        st.caption(
            "Each metric's % change since the locked baseline, mapped so "
            "unchanged = 50, doubled = 100, halved = 0 (clamped at those "
            "extremes), then weighted and summed."
        )
        st.dataframe(
            pd.DataFrame(
                {"Component": portfolio_health_engine.PROGRESS_ORDER,
                 "Weight": [f"{portfolio_health_engine.PROGRESS_WEIGHTS[k]*100:.0f}%"
                            for k in portfolio_health_engine.PROGRESS_ORDER]}
            ),
            use_container_width=True, hide_index=True,
        )
        st.code(
            "rel = (current - baseline) / abs(baseline)     # clamped to [-1, +1]\n"
            "                                                 # (Debt is higher_better=False, so rel is negated)\n"
            "progress_score = clamp(50 + rel * 50, 0, 100)   # unchanged=50, doubled=100, halved=0\n"
            "\n"
            "overall = weighted average of every component with data,\n"
            "          reweighted so the available components still sum to 100%",
            language="text",
        )
        st.caption(
            f"Verdict: {portfolio_health_engine.PROGRESS_VERDICT_UP}+ = improved since purchase, "
            f"below {portfolio_health_engine.PROGRESS_VERDICT_DOWN} = deteriorated, in between = roughly flat."
        )

    if not _holdings:
        st.info("Add a holding on the Holdings tab to see its Progress Score.")
        return

    _labels = _hlabels(_holdings)
    _rows = []
    _raw_progress_summary = []
    for h in _holdings:
        _progress = _analyses[_hkey(h)]["progress"]
        _progress_val = _progress["overall"]
        _row = {"Ticker": h["ticker"]}
        if _is_combined:
            _row["Portfolio"] = h.get("portfolio")
        _row.update({
            "Name": h.get("name") or h["ticker"],
            "Progress": _pf_cell(_progress_val, "{:.0f}"), "Verdict": _progress["verdict"],
            "Baseline date": h.get("baseline_date"),
        })
        _rows.append(_row)
        _raw_progress_summary.append(_progress_val)

    _df = pd.DataFrame(_rows)
    st.dataframe(
        _df.style
        .apply(lambda col: [(f"color: {portfolio_health_engine.score_color(v)}; font-weight:700" if v is not None else "")
                             for v in _raw_progress_summary], subset=["Progress"]),
        use_container_width=True, hide_index=True,
    )

    st.markdown("##### Holding detail")
    _tk = st.selectbox(
        "Choose a holding", [_hkey(h) for h in _holdings], format_func=lambda k: _labels[k],
        key=_pf_key(active_portfolio, "pf_progress_ticker"),
    )
    _h = next(h for h in _holdings if _hkey(h) == _tk)
    _a = _analyses[_tk]
    _snap, _progress = _a["snapshot"], _a["progress"]

    _sc1, _sc2 = st.columns([1, 2])
    with _sc1:
        st.markdown(portfolio_health_engine.big_score_html("Progress Score", _progress["overall"]),
                     unsafe_allow_html=True)
        st.caption(_progress["verdict"])
    with _sc2:
        st.markdown(
            portfolio_health_engine.component_bars_html(_progress["components"], portfolio_health_engine.PROGRESS_ORDER),
            unsafe_allow_html=True,
        )

    _detail_rows = []
    for name in portfolio_health_engine.PROGRESS_ORDER:
        c = _progress["components"].get(name) or {}
        _detail_rows.append({
            "Metric": name,
            "Progress score": c.get("score"),
            "Current": c.get("current"),
            "At baseline": c.get("baseline"),
        })
    st.dataframe(pd.DataFrame(_detail_rows), use_container_width=True, hide_index=True)


def _content_page_shell(title):
    _render_header(compact=True)
    st.markdown(f"## {title}")


_METHODOLOGY_FACTUAL_SWAPS = [
    # Verdict bands paragraph -> value-score description
    ("Above 70 = **STRONG LONG**, above 50 = **LONG**, above 30 = **WATCHLIST**, otherwise\n**AVOID**. If no intrinsic value could be computed at all, the signal is capped at\nWATCHLIST - a thesis whose value leg can't be verified doesn't get a full\nrecommendation.",
     "On this site the number is displayed as the **Value Score** - a weighted\ndescription of the four calculations above, shown without signal labels or\nrecommendations. Where no intrinsic value could be computed, that is stated\nplainly and the affected values are marked."),
    ("The Long Score (0\u2013100) and Investment Signal", "The Value Score (0\u2013100)"),
    # Score heading + intro question -> neutral description
    ("#### The Long Score (0\u2013100)\n\nOne number answering \"is this a good business to own at this price?\" It blends four\nfactors, each clamped to a fixed band first so no single factor can run away with the\nresult:",
     "#### The Value Score (0\u2013100)\n\nOne number summarising four calculations, each clamped to a fixed band first so no\nsingle factor can run away with the result:"),
    # Psychology row: drop the advice-flavoured sentence, keep the maths
    ("| Psychology | 20% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour. Fear scores positively - the value investor's edge is buying quality when others are anxious. |",
     "| Psychology | 20% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour; fear enters the formula with a positive sign. The sign convention is part of the stated arithmetic, not a recommendation. |"),
    # Trade Setup / two-verdicts section -> psychology-readings description
    ("#### Value vs timing - two separate verdicts\n\nThe **Investment Signal** answers \"good business to own?\" The **Trade Setup** answers\n\"is right now a sane entry?\" - support/resistance-based entry zone, stop loss and\ntargets, gated on trend safety and risk/reward. A great company can be a poor entry\ntoday; the site shows both rather than blurring them into one contradictory verdict.",
     "#### Psychology and discovery readings\n\nAlongside the valuation models, the site reports what the crowd has been doing:\ndistance below the 3-month high (fear), distance from the 50-day average and greed/\nFOMO terms, and a discovery reading built from volume, search interest, news and\nsocial chatter. These are measurements, stated as numbers - the site does not\ndisplay entry levels, targets or trade verdicts."),
]


def page_methodology():
    _content_page_shell("How the scores work")
    _bump_page_view("methodology")
    if _factual():
        st.info(
            "**Presentation note.** This site displays data, model outputs and "
            "described calculations from stated inputs. It does not provide "
            "financial product advice or recommendations - descriptions below "
            "of how each calculation works are exactly that: descriptions of "
            "arithmetic, not guidance on what to do."
        )
    _md_text = """
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

The base cash flow the DCF starts from is the latest reported year, adjusted for average
capital expenditure so a single unusually heavy investment year doesn't collapse the
estimate on its own. If that adjusted figure still deviates by more than 40% from the
median of the last three years, the median is used as the base instead - a smoothing
step so one outlier reporting year can't dominate the whole valuation. When this
happens, it's flagged directly on the Deep Dive page next to Intrinsic Value.

The discount rate is CAPM-based but floored at 7.5% (and the beta feeding it is
floored at 0.6) - a cost of capital below that, or a measured beta that low, usually
reflects a data artefact rather than a genuinely low-risk business, and the terminal
value is extremely sensitive to how close the discount rate sits to terminal growth.
When a company's reported financials are in a different currency than the one it
trades in, the cash flows are converted to the listing currency at the current
exchange rate before the per-share calculation - both of these are flagged on the
Deep Dive page whenever they apply.

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

#### Compounder View (auto)

The Deep Dive page for any ticker also has a collapsed "Compounder View (auto)"
section: the same six research sections described above (Fundamentals, Value
vs Book, Retained Earnings, Earnings Trends, Cost of Capital, Fair Value),
computed live from that company's own reported financial statements and price
history instead of from the hand-built workbook. Metrics are coloured against
the author's own threshold bands wherever a matching one exists; any figure
resting on an estimate or a fallback assumption is flagged in red, the same
convention used everywhere else on the site. Statement depth depends on the
data source in use - a handful of years by default, or up to ten years when
the site is configured with an extended data key.

#### Limitations, honestly

Data is sourced from free public feeds and can be delayed, revised or occasionally
wrong. Intrinsic value is an estimate resting on assumptions - reasonable assumptions,
shown openly, but assumptions. Scores are model outputs, not personal advice, and none
of this considers your circumstances. Use it the way it was built to be used: as the
starting point for your own judgment, not a substitute for it.
"""
    if _factual():
        for _old, _new in _METHODOLOGY_FACTUAL_SWAPS:
            _md_text = _md_text.replace(_old, _new)
    st.markdown(_md_text)


def page_about():
    _content_page_shell("About")
    _bump_page_view("about")
    if _factual():
        st.markdown(
            """
StocksDeepDive is built and run by **Andres Moreno**, a private investor in Australia.

It didn't start as a website. It started as a personal stock scanner and a very long
Excel workbook - tools built to study businesses with a Buffett/Munger-style value
lens: compute what the model says a business's cash flows are worth, test its quality
from reported fundamentals, and read what the price has been doing. Over the years the
scanner grew a DCF engine, quality calculations, psychology and discovery readings, and a
research workbook that documents one company for weeks at a time.

At some point the obvious question arrived: why not open the numbers up? So this site
is that - the same engine, the same data work, made public.

Two principles carried over from the private version, unchanged:

**The numbers must be honest.** Whenever a figure rests on a default or an average
because real data wasn't available, it's shown in red. An estimate is never dressed up
as a fact. I built that rule for myself, because fooling yourself is expensive - it
applies just as much now that you're reading the numbers too.

**Value and psychology are different measurements.** What the model computes from
a business's cash flows and what the crowd has been doing to its price are reported as
separate numbers on every page. Most tools blur them; this site states each one
plainly and lets you draw your own conclusions.

The site is free while it launches. When subscriptions open, founding members keep
launch pricing. If you want a stock added to the Rational Compounder research list, or
anything here doesn't make sense, use the Feedback button on any results page or email
[rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com) -
I read everything.

*This site presents factual information and calculator outputs only - it does not
provide financial product advice or recommendations; see the disclaimer in the footer.
I may own stocks analysed here.*
"""
        )
    else:
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

    # --- Task 8: social proof - a sign-up counter (only shown once there's
    # a real number worth stating; a "12 people signed up" counter reads as
    # thin rather than credible) and a commented-out reader-quote
    # placeholder for later, hand-vetted testimonials. No pricing wording
    # anywhere here - the site is free; a sign-up count is not a subscriber
    # count and isn't described as one. ---
    try:
        _about_stats = email_auth.signup_stats()
        if _about_stats.get("total", 0) >= 100:
            st.caption(f"Joined by {_about_stats['total']:,} readers so far.")
    except Exception:
        pass

    # Reader quotes - uncomment and fill in once real, hand-collected quotes
    # exist (with the reader's permission to publish). Keep each quote
    # short and specific to what the site helped with; never edited for
    # tone, never attributed without the reader's explicit sign-off.
    # st.markdown("---")
    # st.markdown("#### What readers say")
    # for _quote, _attribution in [
    #     ("...", "— Name, city"),
    # ]:
    #     st.markdown(f"> {_quote}\n>\n> {_attribution}")


def page_model_history():
    """Task 9: a changelog of when the Rational Compounder research
    workbook has been rebuilt - reuses the exact same archive listing
    build_compounder_data.list_archived_snapshots()/load_snapshot() already
    power the Research page's own "Data snapshot" picker, just surfaced
    here as a standalone, browsable history instead of a dropdown buried on
    one page. Deliberately NOT a performance/returns track record - see the
    disclaimer text below - so nothing here reads as a claim about how
    accurate any past or future output is."""
    _content_page_shell("Model history")
    _bump_page_view("model_history")

    st.markdown(
        "This page is a changelog of the Rational Compounder research "
        "workbook: when it was rebuilt, and how many companies it covered "
        "at each point. Open any past rebuild below to see exactly what it "
        "said at the time, alongside the current, live version."
    )
    st.caption(
        "This is not a track record of returns, and it isn't a claim about "
        "the accuracy of past or future output - it's a record of when the "
        "underlying research data was rebuilt, kept so nothing here is "
        "ever quietly rewritten."
    )

    _snapshots = build_compounder_data.list_archived_snapshots()
    _data = _load_compounder_data()
    _current_n = len(_data.get("tickers", {})) if _data else 0

    with st.container(border=True):
        _c1, _c2 = st.columns([4, 1])
        with _c1:
            st.markdown(f"**Current (latest rebuild)** — {_current_n} companies covered")
            _render_last_updated(_data.get("generated_at") if _data else None)
        with _c2:
            if st.button("View", key="mh_view_current"):
                st.switch_page(PG_RESEARCH)

    if not _snapshots:
        st.info(
            "No archived rebuilds yet - this page fills in over time as the "
            "research workbook is rebuilt."
        )
        return

    st.markdown(
        f"**{len(_snapshots)} archived rebuild{'s' if len(_snapshots) != 1 else ''}** "
        "(most recent first):"
    )
    for _snap in _snapshots:
        with st.container(border=True):
            _c1, _c2 = st.columns([4, 1])
            with _c1:
                st.markdown(
                    f"**{_snap['label']}** — {_snap.get('n_tickers', '?')} companies covered"
                )
            with _c2:
                if st.button("View", key=f"mh_view_{_snap['path']}"):
                    st.session_state["cp_snapshot_jump"] = _snap["path"]
                    st.switch_page(PG_RESEARCH)


def page_privacy():
    _content_page_shell("Privacy policy")
    _bump_page_view("privacy")
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
feedback, and (if you save a watchlist) send you the weekly watchlist digest email. We
never see your Google password.

**If you save a watchlist:** the tickers you save are stored against your email
address on our server.

**If you send feedback:** your message and, if you're signed in, your email address
are stored so we can follow up.

**If subscriptions are active and you subscribe:** payment is handled entirely by
Stripe. We never see or store your card details - we only check with Stripe whether
your email has an active subscription.

#### What we don't do

No advertising, no ad trackers, no third-party analytics or ad tech, and no selling
or sharing of your information with anyone, ever. The only cookies used are the ones
required to keep you signed in.

#### Page analytics

We keep first-party, aggregate page-view counts (which pages get visited, how many
times, per day) so we can see what's useful - no cookies are set for this, no
third-party trackers or ad tech are involved, and no per-visitor identity is stored
alongside a view.

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
# BLOG ADMIN - the writing desk for the public blog
#
# The blog itself is deliberately NOT rendered by Streamlit. Streamlit
# streams its content over a websocket, so a search crawler fetching any
# app page receives an empty shell - nothing on this site can be indexed
# from inside the app. server.py therefore serves /blog and /blog/<slug>
# as real server-rendered HTML (with per-post <title>, meta description,
# canonical URL, Open Graph card, JSON-LD article schema, sitemap.xml and
# robots.txt) straight from blog_store, and proxies every other URL
# through to this app untouched. See server.py's module docstring.
#
# This page is only the editor: it reads and writes the same blog_store
# rows the public HTML is rendered from. Gated by ADMIN_REFRESH_KEY, the
# same key as the Rational Compounder rebuild panel - with no key set on
# the deployment the page renders nothing.
# -----------------------------------

# widget key -> blog_store field
_BLOG_FIELDS = {
    "blog_f_title": "title",
    "blog_f_slug": "slug",
    "blog_f_summary": "summary",
    "blog_f_body": "body_md",
    "blog_f_tags": "tags",
    "blog_f_author": "author",
    "blog_f_hero_alt": "hero_alt",
}


def _blog_admin_unlocked() -> bool:
    """Admin gate. The full-view unlock (?admin= / the RC view popover)
    counts, so an already-unlocked admin session doesn't have to type the
    key twice; otherwise the key is asked for on this page."""
    if not _admin_key_env:
        return False
    return bool(st.session_state.get("full_view_unlocked")
                or st.session_state.get("blog_admin_unlocked"))


def _blog_load_form(post):
    """Copy a post's stored values into the editor widgets. Called only
    when the selected post CHANGES - doing it on every run would fight the
    user for control of the text boxes as they type."""
    for widget_key, field in _BLOG_FIELDS.items():
        st.session_state[widget_key] = (post or {}).get(field) or ""
    st.session_state["blog_f_status"] = (
        "Published" if (post or {}).get("status") == blog_store.STATUS_PUBLISHED
        else "Draft"
    )
    st.session_state["blog_loaded_id"] = (post or {}).get("id")


def _blog_len_hint(text, low, high, what):
    """Google truncates a title around 60 characters and a description
    around 155, so length is worth showing while writing rather than
    discovering in the search results weeks later."""
    n = len(text or "")
    if n == 0:
        return f":grey[{what}: empty]"
    if n < low:
        return f":orange[{what}: {n} chars - shorter than ideal ({low}-{high})]"
    if n > high:
        return f":orange[{what}: {n} chars - will be truncated (aim {low}-{high})]"
    return f":green[{what}: {n} chars]"


def page_blog_admin():
    _content_page_shell("Blog")

    if not _admin_key_env:
        st.info("The blog editor is unavailable on this deployment "
                "(ADMIN_REFRESH_KEY is not set).")
        return

    if not _blog_admin_unlocked():
        st.caption("Admin only.")
        _k = st.text_input("Admin key", type="password", key="blog_admin_key_in")
        if st.button("Unlock", type="primary", key="blog_admin_unlock_btn"):
            if _k.strip() == _admin_key_env:
                st.session_state["blog_admin_unlocked"] = True
                st.rerun()
            else:
                st.error("Incorrect key.")
        return

    # Streamlit refuses to let a widget's session_state key be written
    # after that widget has been created in the same run, so any control
    # that wants to change another control's value (Delete moving the
    # selection, "From title" filling the slug) parks the new value here
    # and reruns; it is applied at the top of the next run, before a single
    # widget exists.
    _pending = st.session_state.pop("_blog_pending", None)
    if _pending:
        st.session_state.update(_pending)

    posts = blog_store.list_posts(include_drafts=True)
    by_id = {p["id"]: p for p in posts}
    # The selectbox carries post IDs rather than labels: two posts can
    # share a title, and an ID stays valid when one gets renamed.
    _options = [None] + [p["id"] for p in posts]

    def _post_label(pid):
        if pid is None:
            return "+ New post"
        p = by_id.get(pid, {})
        mark = "●" if p.get("status") == blog_store.STATUS_PUBLISHED else "○"
        return f"{mark}  {p.get('title', '')}"

    _sel_col, _new_col = st.columns([4, 1])
    with _sel_col:
        choice = st.selectbox(
            "Post", _options, format_func=_post_label, key="blog_select",
            help="● published · ○ draft",
        )
    with _new_col:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if st.button("Reload", use_container_width=True, key="blog_reload"):
            st.session_state.pop("blog_loaded_id", None)
            st.rerun()

    selected_id = choice
    selected = by_id.get(selected_id)
    if st.session_state.get("blog_loaded_id", "__unset__") != selected_id:
        _blog_load_form(selected)
        st.rerun()

    _saved_msg = st.session_state.pop("_blog_saved_msg", None)
    if _saved_msg:
        st.success(_saved_msg)

    _just_saved = st.session_state.pop("blog_open_after_save", None)
    if _just_saved:
        _u = f"/blog/{_just_saved}"
        if (st.session_state.get("blog_f_status") != "Published"
                and _admin_key_env):
            _u += f"?preview={_admin_key_env}"
        st.link_button("Open the saved post ↗", _u)

    st.markdown("---")
    _edit, _side = st.columns([3, 2], gap="large")

    with _edit:
        st.text_input("Title", key="blog_f_title",
                      placeholder="Why margin of safety is the whole game")
        _title = st.session_state.get("blog_f_title", "")

        # The slug is the URL and therefore the most permanent thing about
        # a post: suggested from the title, but never silently rewritten
        # once a post exists, because changing it changes a public address.
        _slug_col, _btn_col = st.columns([3, 1])
        with _slug_col:
            st.text_input("URL slug", key="blog_f_slug",
                          placeholder="why-margin-of-safety-is-the-whole-game")
        with _btn_col:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if st.button("From title", use_container_width=True,
                         key="blog_slug_from_title"):
                st.session_state["_blog_pending"] = {
                    "blog_f_slug": blog_store.slugify(_title)}
                st.rerun()

        st.text_area(
            "Meta description", key="blog_f_summary", height=90,
            help="The sentence Google shows under the title in search "
                 "results, and the text of the link preview when the post "
                 "is shared. Left blank, the opening of the post is used.",
        )
        st.markdown(_blog_len_hint(st.session_state.get("blog_f_summary"),
                                   120, 158, "Description"))

        st.text_area(
            "Post body (Markdown)", key="blog_f_body", height=460,
            help="Markdown: ## for headings, **bold**, - for bullets, "
                 "> for quotes, tables and [links](https://…) all work. "
                 "Use ## and ### for structure - headings are a real "
                 "ranking signal and make the post skimmable.",
        )

        _c1, _c2 = st.columns(2)
        with _c1:
            st.text_input("Tags (comma separated)", key="blog_f_tags",
                          placeholder="valuation, method, asx")
        with _c2:
            st.text_input("Author", key="blog_f_author", placeholder="Andrew")

        st.radio("Status", ["Draft", "Published"], key="blog_f_status",
                 horizontal=True,
                 help="Drafts are hidden from the blog index, the sitemap "
                      "and the feed, and carry a noindex tag. They are "
                      "still viewable at their own URL with ?preview=<admin key>.")

        _hero = st.file_uploader(
            "Hero image (optional)", type=["png", "jpg", "jpeg", "webp"],
            key="blog_f_hero_file",
            help="Used as the social-share card image. 1200×630 is the "
                 "size Facebook, LinkedIn and X all crop to.",
        )
        st.text_input("Hero image alt text", key="blog_f_hero_alt",
                      placeholder="Chart of free cash flow vs price")
        _remove_hero = False
        if selected and selected.get("hero_file"):
            st.caption(f"Current image: {selected['hero_file']}")
            _remove_hero = st.checkbox("Remove the current image",
                                       key="blog_f_hero_remove")

        st.markdown("")
        _b1, _b2, _b3 = st.columns([1.2, 1, 1])
        with _b1:
            _save = st.button("Save", type="primary", use_container_width=True,
                              key="blog_save")
        with _b2:
            _save_view = st.button("Save & view", use_container_width=True,
                                   key="blog_save_view")
        with _b3:
            if selected:
                _delete = st.button("Delete", use_container_width=True,
                                    key="blog_delete")
            else:
                _delete = False

        if _delete:
            if st.session_state.get("blog_confirm_delete") == selected_id:
                blog_store.delete_post(selected_id)
                st.session_state.pop("blog_confirm_delete", None)
                st.session_state.pop("blog_loaded_id", None)
                st.session_state["_blog_pending"] = {"blog_select": None}
                st.rerun()
            else:
                st.session_state["blog_confirm_delete"] = selected_id
                st.warning("Press Delete again to confirm - this cannot be "
                           "undone.")

        if _save or _save_view:
            _title_v = (st.session_state.get("blog_f_title") or "").strip()
            if not _title_v:
                st.error("A post needs a title.")
            else:
                _status = (blog_store.STATUS_PUBLISHED
                           if st.session_state.get("blog_f_status") == "Published"
                           else blog_store.STATUS_DRAFT)
                _hero_name = Ellipsis
                if _hero is not None:
                    _hero_name = blog_store.save_media(_hero.name,
                                                       _hero.getvalue())
                elif _remove_hero:
                    _hero_name = None

                if selected_id:
                    _slug = blog_store.update_post(
                        selected_id,
                        title=_title_v,
                        slug=(st.session_state.get("blog_f_slug") or "").strip()
                             or _title_v,
                        summary=st.session_state.get("blog_f_summary") or "",
                        body_md=st.session_state.get("blog_f_body") or "",
                        tags=st.session_state.get("blog_f_tags") or "",
                        author=st.session_state.get("blog_f_author") or "",
                        hero_file=_hero_name,
                        hero_alt=st.session_state.get("blog_f_hero_alt") or "",
                        status=_status,
                    )
                    _new_id = selected_id
                else:
                    _new_id = blog_store.create_post(
                        title=_title_v,
                        slug=(st.session_state.get("blog_f_slug") or "").strip()
                             or _title_v,
                        summary=st.session_state.get("blog_f_summary") or "",
                        body_md=st.session_state.get("blog_f_body") or "",
                        tags=st.session_state.get("blog_f_tags") or "",
                        author=st.session_state.get("blog_f_author") or "",
                        hero_file=(None if _hero_name is Ellipsis else _hero_name),
                        hero_alt=st.session_state.get("blog_f_hero_alt") or "",
                        status=_status,
                    )
                    _slug = blog_store.get_post_by_id(_new_id)["slug"]

                # Keep the just-saved post selected rather than dropping
                # back to a blank "new post" form - saving is usually the
                # middle of writing, not the end of it.
                st.session_state.pop("blog_loaded_id", None)
                st.session_state["_blog_pending"] = {"blog_select": _new_id}
                st.session_state["_blog_saved_msg"] = (
                    f"Saved. Public URL: /blog/{_slug}")
                if _save_view:
                    st.session_state["blog_open_after_save"] = _slug
                st.rerun()

    with _side:
        st.markdown("##### Preview")
        _body = st.session_state.get("blog_f_body") or ""
        _slug_now = ((st.session_state.get("blog_f_slug") or "").strip()
                     or blog_store.slugify(st.session_state.get("blog_f_title")))
        if _slug_now:
            _url = f"/blog/{_slug_now}"
            _preview_url = _url
            if (st.session_state.get("blog_f_status") != "Published"
                    and _admin_key_env):
                _preview_url = f"{_url}?preview={_admin_key_env}"
            st.markdown(
                f"<div style='font-family:ui-monospace,Menlo,monospace;"
                f"font-size:12.5px;color:#8aa0b8;word-break:break-all'>"
                f"stocksdeepdive.com{html.escape(_url)}</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<a href='{html.escape(_preview_url)}' target='_blank'>"
                f"Open the real page &rarr;</a>", unsafe_allow_html=True)
        st.caption(
            f"{len(_body.split())} words · "
            f"{blog_render.reading_time(_body)} min read"
        )
        st.markdown(_blog_len_hint(st.session_state.get("blog_f_title"),
                                   30, 60, "Title"))
        st.markdown("---")
        with st.container(height=520, border=True):
            try:
                st.markdown(blog_render.md_to_html(_body),
                            unsafe_allow_html=True)
            except Exception:
                st.markdown(_body)

    st.markdown("---")
    _f1, _f2 = st.columns(2)
    with _f1:
        st.markdown(
            "<a href='/blog' target='_blank'>Open the public blog &rarr;</a>"
            "<br><a href='/sitemap.xml' target='_blank'>sitemap.xml</a> · "
            "<a href='/robots.txt' target='_blank'>robots.txt</a> · "
            "<a href='/blog/feed.xml' target='_blank'>RSS feed</a>",
            unsafe_allow_html=True,
        )
    with _f2:
        st.caption(
            f"{blog_store.count_posts()} published · "
            f"{blog_store.count_posts(include_drafts=True)} total. "
            "Posts and images are stored on the Railway volume alongside the "
            "site's other data, so they survive redeploys."
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
# Sign-in-only, private per-user long-term holdings tracker - see
# page_portfolio()'s own docstring/comment block for the full design.
PG_PORTFOLIO = st.Page(page_portfolio, title="My Portfolio", url_path="portfolio")
PG_METHODOLOGY = st.Page(page_methodology, title="How the scores work", url_path="methodology")
PG_ABOUT = st.Page(page_about, title="About", url_path="about")
PG_MODEL_HISTORY = st.Page(page_model_history, title="Model history", url_path="model-history")
PG_PRIVACY = st.Page(page_privacy, title="Privacy policy", url_path="privacy")
# The public blog lives at /blog and is served as HTML by server.py, NOT by
# Streamlit - this page is the admin-only editor behind it, and is
# Disallowed in robots.txt.
PG_BLOG_ADMIN = st.Page(page_blog_admin, title="Blog admin",
                        url_path="blog-admin")

_nav = st.navigation(
    [PG_HOME, PG_DEEP_DIVE, PG_COMPARISON, PG_RESEARCH, PG_SCANNER,
     PG_PORTFOLIO, PG_METHODOLOGY, PG_ABOUT, PG_MODEL_HISTORY, PG_PRIVACY,
     PG_BLOG_ADMIN], position="hidden"
)
_nav.run()

# Footer is rendered AFTER st.navigation has run the active page, so it
# appears on every page regardless of early returns/gates inside the page.
_render_footer()
