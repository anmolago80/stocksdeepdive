import streamlit as st
import yfinance as yf
import pandas as pd
import time
import json
import os
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
    return mood_engine.compute_country_mood(country, api_key=api_key)


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
# SITE-WIDE BUTTON STYLE + NAV BAR
# -----------------------------------
st.markdown(
    """
    <style>
    .stButton > button, .stFormSubmitButton > button {
        border-radius: 10px !important;
        border: 1.5px solid #0d9488 !important;
        color: #0d9488 !important;
        background-color: #ffffff !important;
        font-weight: 600 !important;
        padding: 0.55rem 1.1rem !important;
        transition: all 0.15s ease !important;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover {
        background-color: #0d9488 !important;
        color: #ffffff !important;
        border-color: #0d9488 !important;
    }
    .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"],
    .stButton > button[kind="primaryFormSubmit"], .stFormSubmitButton > button[kind="primaryFormSubmit"] {
        background-color: #0d9488 !important;
        color: #ffffff !important;
        border-color: #0d9488 !important;
    }
    .stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover,
    .stButton > button[kind="primaryFormSubmit"]:hover, .stFormSubmitButton > button[kind="primaryFormSubmit"]:hover {
        background-color: #0f766e !important;
        border-color: #0f766e !important;
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


def _render_header(compact):
    st.markdown(
        f"""
        <style>
        .site-title {{
            text-align: center; font-weight: 800;
            font-family: 'Segoe UI', sans-serif; color: #0f172a;
            font-size: {"20px" if compact else "58px"};
            margin-top: {"2px" if compact else "44px"};
            margin-bottom: {"2px" if compact else "8px"};
        }}
        .site-title .accent {{ color: #0d9488; }}
        .site-title-link, .site-title-link:hover, .site-title-link:visited {{
            display: block; text-decoration: none !important; cursor: pointer;
        }}
        .site-title-link:hover .site-title {{ opacity: 0.85; }}
        .site-sub {{
            text-align: center; color: #64748b; font-size: 16px;
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
            if st.button(
                "Rational Compounder Analysis", use_container_width=True, key="nav_research"
            ):
                st.switch_page(PG_RESEARCH)
            st.caption(
                "One ticker = Deep Dive. Two or more (comma or space separated) = "
                "side-by-side Comparison. ASX (e.g. CSL.AX) and US (e.g. AAPL) "
                "tickers can be mixed freely."
            )
        else:
            if st.button(
                "Rational Compounder Analysis", key="nav_research"
            ):
                st.switch_page(PG_RESEARCH)

    if _searched:
        _raw = _search_text.replace(",", " ").replace("\n", " ").split()
        _parsed = []
        for _tok in _raw:
            _tok = _tok.strip().upper()
            if _tok and _tok not in _parsed:
                _parsed.append(_tok)
        if not _parsed:
            st.warning("Type at least one ticker above, then hit Search.")
        elif len(_parsed) == 1:
            _tk = _parsed[0]
            with st.spinner(f"Analyzing {_tk}..."):
                st.session_state["dd_result"] = deep_dive_engine.analyze(
                    _tk, get_price_history, get_ticker_info, get_cashflow_df,
                    news_api_key=news_api_key, live_data=live_data,
                    enable_social=enable_social,
                )
            st.switch_page(PG_DEEP_DIVE)
        else:
            # Swing regime benchmark only - never affects how a ticker itself
            # is resolved. Whichever suffix convention is more common in this
            # list decides it; a tie or an all-".AX" list defaults to the ASX
            # benchmark.
            _n_au = sum(1 for _tk in _parsed if _tk.endswith(".AX"))
            _stk_country = "USA" if (len(_parsed) - _n_au) > _n_au else "Australia"
            st.session_state["cmp_stocks"] = _parsed
            st.session_state["cmp_universe_source"] = f"Manual ticker list ({len(_parsed)} entered)"
            st.session_state["cmp_scan_country"] = _stk_country
            st.session_state["cmp_fresh"] = True
            st.switch_page(PG_COMPARISON)

    _sp_margin = "10px" if compact else "20px"
    st.markdown(f"<div style='margin-bottom:{_sp_margin};'></div>", unsafe_allow_html=True)

def _dd_gauge(value, title, zones, bar_color="#1f2937", height=260):
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
        marker_color=["#2ca02c" if v >= 0 else "#d62728" for v in raw_values],
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
        marker_color=["#2ca02c" if v > 0 else "#d62728" for v in raw_values],
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

_CP_COLOR_FILL = {"red": "#f7d7d7", "amber": "#fbe8c6", "green": "#b7dfba", "blue": "#bfe3ef"}
_CP_COLOR_TEXT = {"red": "#b3261e", "amber": "#8a5a00", "green": "#1e7d34", "blue": "#0f6c8a"}


@st.cache_data
def _load_compounder_data():
    _path = os.path.join(os.path.dirname(__file__), "compounder_data.json")
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
    return "\n\n".join(parts)


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
            "color": _CP_COLOR_FILL.get(color, "#e5e7eb"),
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
            "bar": {"color": "#0f172a", "thickness": 0.45},
            "steps": steps,
            "threshold": {"line": {"color": "#0f172a", "width": 2}, "thickness": 0.9, "value": value},
        },
    ))
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=22, b=8),
        title={"text": label, "font": {"size": 13, "color": "#0f172a"}, "x": 0, "xanchor": "left"},
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
        line=dict(color="#0d9488", width=2),
    ))
    if entry.get("avg_10y") is not None:
        fig.add_hline(
            y=entry["avg_10y"], line_dash="dash", line_color="#0f172a",
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
    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in values]
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
    fig.add_trace(go.Bar(x=order, y=created, name="Market Value Created for every dollar retained", marker_color="#0d9488"))
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
        colors.append(_CP_COLOR_FILL.get(band[0], "#0d9488") if band else "#0d9488")
    fig = go.Figure(go.Bar(
        x=years, y=ratios, marker_color=colors,
        text=[f"{v:.2f}x" for v in ratios], textposition="outside",
    ))
    avg_ratio = sum(ratios) / len(ratios)
    fig.add_hline(
        y=avg_ratio, line_dash="dash", line_color="#0f172a",
        annotation_text=f"Average {avg_ratio:.2f}x",
        annotation_position="top left", annotation_font_size=11,
    )
    fig.update_layout(
        title="IV/BV by Year Modelled", height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="IV/BV",
        xaxis_type="category",
    )
    return fig


def _cp_year_bar_chart(ticker, series, key, title, yaxis_title, fmt="num", color="#0d9488"):
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
    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in values]
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
        x=years, y=values, marker_color="#64748b",
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

    _cp_pe_ref_line(avg_3y, "#c2410c", "dash", "3y EPS avg", avg_3y_yanchor)
    _cp_pe_ref_line(overall_avg, "#1d4ed8", "dot", "Overall avg", overall_avg_yanchor)
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
        x=periods, y=roic_vals, name="ROIC", marker_color="#0d9488",
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
    ("price", "Current Price", "#0d9488"),
    ("pe_forward", "PE Forward", "#94a3b8"),
    ("pe_trailing", "PE Trailing", "#64748b"),
    ("dcf", "DCF (10y FCF)", "#1e7d34"),
    ("equity_10y", "Rational Compounder Method 10y", "#4c9f70"),
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
            st.markdown(f"<div style='text-align:center;font-size:12px;font-weight:600;color:#334155;'>{label}</div>", unsafe_allow_html=True)
            # "Current Price" is the actual market price, not a valuation
            # estimate - skip the "Intrinsic Value" line for that one bar.
            lines = [] if key == "price" else [f"Intrinsic Value: {_cp_format(method_values.get(key), 'cur')}"]
            for item in entry.get(key, []):
                lines.append(f"{item['label']}: {_cp_format(item['value'], item['format'])}")
            if lines:
                st.markdown(
                    "<div style='text-align:center;font-size:11.5px;color:#64748b;line-height:1.6;'>"
                    + "<br>".join(lines) + "</div>",
                    unsafe_allow_html=True,
                )


_CP_HML_COLOR = {
    "good_high": {"high": "green", "medium": "amber", "low": "red"},
    "good_low": {"low": "green", "medium": "amber", "high": "red"},
    "neutral": {},
}


def _cp_pill(text, color_key=None):
    c = _CP_COLOR_TEXT.get(color_key, "#475569")
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
            f"<div style='margin-bottom:6px;'><span style='font-size:13px;color:#334155;'>"
            f"{r['label']}: </span>{_cp_pill(r['value'].strip(), color_key)}</div>"
        )
    # two columns of pills so a long ratings list doesn't run the whole page
    half = (len(html_parts) + 1) // 2
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("".join(html_parts[:half]), unsafe_allow_html=True)
    with col2:
        st.markdown("".join(html_parts[half:]), unsafe_allow_html=True)


def _cp_render_yesno_checks(checks):
    st.markdown("##### Quick checks")
    html = "".join(
        f"<span style='display:inline-block;margin:2px 8px 8px 0;padding:4px 10px;"
        f"border-radius:8px;font-size:12.5px;background:#f1f5f9;color:#334155;'>"
        f"{c['label']}: <b>{c['value'].strip()}</b></span>"
        for c in checks
    )
    st.markdown(html, unsafe_allow_html=True)


def _cp_render_text_groups(groups):
    st.markdown("##### Your notes, grouped")
    for g in groups:
        with st.expander(g["title"], expanded=False):
            for item in g["items"]:
                st.markdown(f"**{item['label']}**")
                st.caption(item["text"])


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

    Important: rebuilding writes compounder_data.json to THIS process's
    local disk, so the refreshed data shows up on this page immediately
    (great for previewing the result before committing to anything) -- but
    Railway's filesystem is ephemeral, so that write does NOT survive the
    next redeploy/restart on its own. The download button below lets you
    grab the freshly-built file and commit it to the repo, exactly the
    workflow build_compounder_data.py's own docstring already documents --
    that commit is what makes an update permanent.
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
            if uploaded and st.button(
                "Rebuild Rational Compounder data", type="primary", key="cp_admin_rebuild",
            ):
                with st.spinner("Rebuilding from the uploaded workbook..."):
                    tmp_path = os.path.join(os.path.dirname(__file__), "_admin_upload_tmp.xlsx")
                    out_path = os.path.join(os.path.dirname(__file__), "compounder_data.json")
                    corrections_path = build_compounder_data.CP_CORRECTIONS_PATH
                    try:
                        with open(tmp_path, "wb") as f:
                            f.write(uploaded.getbuffer())
                        fresh_data = build_compounder_data.build(tmp_path)

                        # Merge over whatever was already on disk (the
                        # currently-live data) rather than replacing it
                        # outright -- so re-uploading a newer/leaner
                        # workbook (Andrew rebuilds this from scratch every
                        # 10-12 months) can't silently wipe out a ticker,
                        # or a Company Potential answer, that just wasn't
                        # re-typed into the new file this round. See
                        # merge_compounder_data()'s own docstring for the
                        # full rationale.
                        previous_data = None
                        if os.path.exists(out_path):
                            try:
                                with open(out_path) as f:
                                    previous_data = json.load(f)
                            except (json.JSONDecodeError, OSError):
                                previous_data = None
                        new_data = build_compounder_data.merge_compounder_data(
                            fresh_data, previous_data
                        )

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
                st.success(
                    f"Rebuilt: {len(new_data.get('tickers', {}))} tickers now in the data"
                    f"{carried_over_note}. Now showing below. This is live on THIS running "
                    "instance only -- download BOTH files below and commit them to the repo "
                    "to make the update permanent (survives the next redeploy)."
                )
                for w in fresh_data.get("build_warnings", []):
                    st.warning(w)
                st.download_button(
                    "Download compounder_data.json to commit",
                    data=json.dumps(new_data, indent=1),
                    file_name="compounder_data.json",
                    mime="application/json",
                    key="cp_admin_download",
                )
                if os.path.exists(corrections_path):
                    with open(corrections_path) as f:
                        corrections_text = f.read()
                    st.download_button(
                        "Download company_potential_corrections.json to commit",
                        data=corrections_text,
                        file_name="company_potential_corrections.json",
                        mime="application/json",
                        key="cp_admin_download_corrections",
                        help=(
                            "Only needed if the grammar check ran (ANTHROPIC_API_KEY set) - "
                            "this is its cache of already-checked text. Skipping this commit "
                            "doesn't lose any data, it just means already-correct text gets "
                            "re-checked (a small extra API cost) on the next rebuild instead "
                            "of being remembered for free."
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
            f'<div style="text-align:right; color:#64748b; font-size:13px; '
            f'margin-bottom:6px;">Last updated on {label}</div>',
            unsafe_allow_html=True,
        )


def page_research():
    _render_header(compact=True)

    _render_compounder_admin_panel()

    data = _load_compounder_data()
    if not data or not data.get("tickers"):
        st.info(
            "Rational Compounder Analysis - the research data file hasn't been "
            "built yet. Run build_compounder_data.py against the watchlist "
            "workbook to generate compounder_data.json, then reload this page."
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
        "Every chart, threshold and colour band here comes straight from your "
        "own notes on the watchlist workbook - pick a stock, then a section."
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
        ratings = section.get("hml_ratings", {}).get(ticker, [])
        checks = section.get("yesno_checks", {}).get(ticker, [])
        groups = section.get("text_groups", {}).get(ticker, [])
        if not ratings and not checks and not groups:
            st.warning(f"No Company Potential notes yet for {ticker}.")
            return
        st.caption(
            "Your own Low/Medium/High cells, shown directly, plus your "
            "free-text answers merged into a few themed groups (the "
            "grouping is my call, not something you specified)."
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
                        f"font-size:12px; color:{_CP_COLOR_TEXT.get(color, '#334155')};'>"
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
    _render_header(compact=False)


def page_deep_dive():
    _render_header(compact=True)
    _dd = st.session_state.get("dd_result")

    # The long explanatory paragraph only earns its space when there's
    # nothing else on the page yet - once real results are showing, it's
    # just extra scrolling between the search box and the numbers you
    # searched for.
    if _dd is None or _dd.get("error"):
        st.caption(
            "Full graphical breakdown for one ticker: the base-case DCF "
            "(auto-calculated CAPM discount rate, analyst-consensus-or-history "
            "growth, currency-based terminal growth - the same model as every "
            "other table in this app) alongside every factor that feeds its Long "
            "Score, plus a dedicated Trade Setup (the same entry/stop/target "
            "model the Trade Filter table uses), so you can see at a glance "
            "what's driving the call. Always fetches fresh numbers for "
            "whichever ticker you searched, and always shows the fully "
            "auto-calculated base case (no bear/bull scenarios yet - this tab "
            "will keep growing in later updates)."
        )

    if _dd is None:
        st.info("Search a ticker above to see its Deep Dive.")
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

        _dd_col1, _dd_col2 = st.columns(2)

        with _dd_col1:
            if _dd["intrinsic_value"]:
                _iv_color = "#2ca02c" if _dd["intrinsic_value"] > _dd["price"] else "#d62728"
                fig_val = go.Figure(go.Bar(
                    x=[_dd["price"], _dd["intrinsic_value"]],
                    y=["Current Price", "Intrinsic Value (Base Case)"],
                    orientation="h",
                    marker_color=["#6c757d", _iv_color],
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
                        (0, SIGNAL_THRESHOLDS["WATCHLIST"], "#f7d7d7"),
                        (SIGNAL_THRESHOLDS["WATCHLIST"], SIGNAL_THRESHOLDS["LONG"], "#fbe8c6"),
                        (SIGNAL_THRESHOLDS["LONG"], SIGNAL_THRESHOLDS["STRONG_LONG"], "#d7ecd9"),
                        (SIGNAL_THRESHOLDS["STRONG_LONG"], 100, "#b7dfba"),
                    ],
                ),
                use_container_width=True,
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
        st.subheader(f"Quality Score: {_dd['quality_score']} - {_dd['quality_label']}")
        _q_col1, _q_col2 = st.columns(2)
        with _q_col1:
            st.plotly_chart(
                _dd_gauge(
                    _dd["quality_score"], f"Quality - {_dd['quality_label']}",
                    [(0, 40, "#f7d7d7"), (40, 60, "#fbe8c6"),
                     (60, 80, "#d7ecd9"), (80, 100, "#b7dfba")],
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
                    [(0, 30, "#f7d7d7"), (30, 45, "#fbe8c6"), (45, 55, "#e9ecef"),
                     (55, 70, "#d7ecd9"), (70, 100, "#b7dfba")],
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
                    [(0, 25, "#f7d7d7"), (25, 50, "#fbe8c6"),
                     (50, 75, "#d7ecd9"), (75, 100, "#b7dfba")],
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
                    [(0, 45, "#f7d7d7"), (45, 65, "#fbe8c6"), (65, 100, "#b7dfba")],
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
        _tt_colors = ["#d62728", "#6c757d", "#d7ecd9"]
        if _dd["trade_setup_target2"] is not None:
            _tt_x.append(_dd["trade_setup_target2"])
            _tt_y.append("Target 2")
            _tt_colors.append("#8fce8f")
        if _dd["trade_setup_target3"] is not None:
            _tt_x.append(_dd["trade_setup_target3"])
            _tt_y.append("Target 3 (breakout)")
            _tt_colors.append("#2ca02c")

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

def page_comparison():
    _render_header(compact=True)

    # The search box on every page is the only thing that ever populates a
    # Comparison request - it stores the parsed ticker list + a one-shot
    # "fresh" flag in session_state right before switching to this page
    # (see _render_header above), rather than this page owning its own
    # ticker-entry widget.
    _fresh_scan = st.session_state.pop("cmp_fresh", False)
    stocks = st.session_state.get("cmp_stocks")
    universe_source = st.session_state.get("cmp_universe_source", "")
    scan_country = st.session_state.get("cmp_scan_country", "Australia")

    if not stocks:
        st.info("Search two or more tickers above to run a Comparison.")
        return

    # The universe Stock Scanner and Auto-Trading were both removed from
    # this public deployment, so Comparison is the only scan type left -
    # its results cache always lives under this one fixed key.
    _active_cache_name = "stock"
    _cache_key = "last_scan_data_stock"

    # True only right on the page load that immediately follows a Search
    # submission (see the one-shot "cmp_fresh" flag popped above) - tells
    # "a real new scan was just requested" apart from "some OTHER widget on
    # this results page (e.g. saving a DCF override) triggered this rerun",
    # so those reuse the already-computed results instead of re-fetching
    # everything from scratch.
    _need_fresh_scan = _fresh_scan

    _scan_area = st.container(key="scan_results_area")
    with _scan_area:
        # -----------------------------------
        # PRE-SCAN SUMMARY
        # -----------------------------------

        st.write("Stocks to scan:", len(stocks))
        st.caption(f"Source: {universe_source}")

        if len(stocks) == 0:
            st.warning("No stocks to scan.")
            st.stop()

        if len(stocks) > 150 and _need_fresh_scan:
            st.warning(
                f"{len(stocks)} stocks matched - this scan may take a while. "
                "Picking a narrower universe or sector will speed it up."
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

                current_price = window_3mo["Close"].iloc[-1]

                high_price = window_3mo["Close"].max()

                fear_score = (
                    (high_price - current_price) / high_price
                ) * 100 if high_price else 0

                ma50 = window_3mo["Close"].rolling(50).mean().iloc[-1]

                if pd.isna(ma50) or ma50 == 0:
                    ma50 = current_price

                greed_score = max(((current_price - ma50) / ma50) * 100, 0)

                keyword = ticker.split(".")[0]

                if live_data:
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

                if enable_social:
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

                if len(window_3mo) >= 6 and window_3mo["Close"].iloc[-6] != 0:
                    weekly_change = ((current_price - window_3mo["Close"].iloc[-6]) / window_3mo["Close"].iloc[-6]) * 100
                else:
                    weekly_change = 0

                fomo_score = max(greed_score + max(weekly_change, 0), 0)
                psychology_score = fear_score - greed_score - fomo_score

                activity_score = abs(weekly_change)

                avg_volume = window_3mo["Volume"].mean()
                latest_volume = window_3mo["Volume"].iloc[-1]
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
                "Showing "
                + ("Stock Scanner's" if _active_cache_name == "industry"
                   else "Stock Comparison's" if _active_cache_name == "stock"
                   else "the")
                + " last completed scan - something elsewhere on the page (e.g. "
                "the Auto-Trading tab, or just switching tabs) triggered this "
                "refresh, not a new scan. Click Run Scan / Run Comparison again "
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

        _DEFAULT_RED = "color: #d62728; font-weight: 600"
        _INTRINSIC_ABOVE_PRICE = "color: #2ca02c; font-weight: 600"   # green - looks undervalued
        _INTRINSIC_BELOW_PRICE = "color: #d62728; font-weight: 600"   # red - looks overvalued

        # Long Score traffic-light colors, keyed off the SAME SIGNAL_THRESHOLDS gates
        # used to set the Investment Signal (STRONG LONG / LONG / WATCHLIST / AVOID)
        # everywhere else in the app - see the DCF Parameters table caption for the
        # exact cutoffs shown to the user.
        _LONG_SCORE_STRONG = "color: #2ca02c; font-weight: 600"   # green - above LONG gate
        _LONG_SCORE_WATCH = "color: #b8860b; font-weight: 600"    # amber - above WATCHLIST, at/below LONG
        _LONG_SCORE_AVOID = "color: #d62728; font-weight: 600"    # red - at/below WATCHLIST gate

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

            return display_df.style.apply(_apply, axis=None)


        st.write("Rows found:", len(data))

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

            st.metric("Stocks Scanned", len(results))

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

            st.subheader("Sector Rankings")
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
            st.subheader("Trade Setup")
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
            trade_rows = []
            for _, row in results.iterrows():
                t = trade_lookup.get(row["Ticker"])
                if not t:
                    continue
                trade_rows.append({
                    "Ticker": row["Ticker"],
                    "Trade Setup": t["signal"],
                    "Trade Setup Score": trade_score_lookup.get(row["Ticker"], "-"),
                    "Trend": t.get("trend", "-"),
                    "Entry Zone": t["entry_zone"],
                    "Stop Loss": t["stop_loss"],
                    "Target 1": t["target1"],
                    "Target 2": t["target2"],
                    "Target 3": t["target3"] if t["target3"] is not None else "-",
                    "Risk": t["risk"],
                    "RR1": t["rr1"] if t["rr1"] is not None else "-",
                    "RR2": t["rr2"] if t["rr2"] is not None else "-",
                    "RR3": t["rr3"] if t["rr3"] is not None else "-",
                    "Early Exit Watch": "Yes" if t["early_exit_watch"] else "No",
                })
            st.dataframe(
                pd.DataFrame(trade_rows),
                width="stretch",
                hide_index=True,
                column_config={
                    "Trade Setup Score": st.column_config.NumberColumn(
                        "Trade Setup Score",
                        help=(
                            "0-100 weighting of the same gates behind the Trade "
                            "Setup verdict (Trend Safety, Near Entry Zone, Risk/"
                            "Reward, Price vs MA50, Psychology Momentum, Discovery "
                            "Momentum). Same score/weights as the Stock Deep Dive "
                            "tab's Trade Setup gauge - not a separate formula."
                        ),
                    ),
                    "Entry Zone": st.column_config.NumberColumn(
                        "Entry Zone",
                        help=(
                            "Trade Filter's technical entry level (support/resistance "
                            "based) - answers 'is now a sane place to buy'. "
                            "Independent of the DCF valuation Entry ('Full Stock "
                            "Database') and the ATR-based Swing Entry ('Swing "
                            "Setup') - all three are separate, unrelated numbers."
                        ),
                    ),
                    "Stop Loss": st.column_config.NumberColumn(
                        "Stop Loss",
                        help="Trade Filter's technical stop-loss - different from the ATR-based Swing Stop.",
                    ),
                    "Target 1": st.column_config.NumberColumn(
                        "Target 1", help="Trade Filter's first technical target."
                    ),
                    "Target 2": st.column_config.NumberColumn(
                        "Target 2", help="Trade Filter's second technical target."
                    ),
                    "Target 3": st.column_config.NumberColumn(
                        "Target 3", help="Trade Filter's third technical target."
                    ),
                },
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

            _BAR_RED, _BAR_AMBER, _BAR_GREEN = "#d62728", "#b8860b", "#2ca02c"

            def _bar_cell(value, low, high, suffix=""):
                """One table cell: value as text, ABOVE a small colored bar whose
                fill width reflects the value and whose color reflects which band
                (red/amber/green) it falls in. `low`/`high` are the same
                thresholds the app already uses elsewhere for this metric."""
                if value is None or value == "N/A" or (isinstance(value, float) and pd.isna(value)):
                    return "<div style='color:#888;'>N/A</div>"
                try:
                    v = float(value)
                except (TypeError, ValueError):
                    return f"<div>{value}</div>"
                color = _BAR_RED if v <= low else (_BAR_GREEN if v > high else _BAR_AMBER)
                width_pct = max(0.0, min(100.0, v))
                return (
                    f"<div style='font-size:13px;margin-bottom:2px;'>{v:,.1f}{suffix}</div>"
                    f"<div style='background:#e9ecef;border-radius:3px;height:8px;width:100%;'>"
                    f"<div style='background:{color};height:8px;border-radius:3px;"
                    f"width:{width_pct:.0f}%;'></div></div>"
                )

            # Classification columns (Type, Valuation, Sentiment, Trend, Trade
            # Setup) render as small colored pills instead of plain text - same
            # red/amber/green convention as the score bars above, not new
            # arbitrary colors. Type isn't a verdict (GROWTH/COMPOUNDER/etc. are
            # just categories), so it gets a neutral teal pill EXCEPT when the
            # sector lookup failed and it fell back to the GENERAL default - that
            # case is flagged red, same as every other defaulted cell in this app.
            _TYPE_NEUTRAL = "#0d9488"
            _BADGE_COLORS = {
                "UNDERVALUED": _BAR_GREEN, "FAIR": _BAR_AMBER, "EXPENSIVE": _BAR_RED,
                "FEARFUL": _BAR_GREEN, "GREEDY": _BAR_AMBER, "OVERHEATED": _BAR_RED,
                "CALM": "#6c757d", "NEUTRAL": "#6c757d",
                "Uptrend": _BAR_GREEN, "Ranging": _BAR_AMBER, "Downtrend": _BAR_RED,
                "BUY": _BAR_GREEN, "WATCHLIST": _BAR_AMBER, "AVOID": _BAR_RED,
            }

            def _badge_cell(text, color=None):
                """One classification label as a small colored pill."""
                _c = color or _BADGE_COLORS.get(text, "#475569")
                return (
                    f"<span style='display:inline-block;padding:3px 10px;"
                    f"border-radius:12px;font-size:12.5px;font-weight:600;"
                    f"background:{_c}22;color:{_c};'>{text}</span>"
                )

            _cmp = results.copy()
            _cmp["Trade Setup Score"] = _cmp["Ticker"].map(trade_score_lookup)

            _cmp_rows_html = []
            for _, r in _cmp.iterrows():
                _iv_text = (
                    "N/A" if r["Intrinsic Value"] == "N/A" else f"{r['Intrinsic Value']:,.2f}"
                )
                _type_color = _BAR_RED if r.get("_flag_type") else _TYPE_NEUTRAL
                _cmp_rows_html.append(
                    "<tr>"
                    f"<td style='padding:6px 10px;font-weight:600;'>{r['Ticker']}</td>"
                    f"<td style='padding:6px 10px;'>{_badge_cell(r['Type'], _type_color)}</td>"
                    f"<td style='padding:6px 10px;'>{r['Price']:,.2f}</td>"
                    f"<td style='padding:6px 10px;min-width:90px;'>{_bar_cell(r['Quality'], 40, 80)}</td>"
                    f"<td style='padding:6px 10px;'>{_iv_text}</td>"
                    f"<td style='padding:6px 10px;min-width:90px;'>{_bar_cell(r['MOS'], 0, 25, '%')}</td>"
                    f"<td style='padding:6px 10px;min-width:90px;'>{_bar_cell(r['Long Score'], 30, 50)}</td>"
                    f"<td style='padding:6px 10px;min-width:90px;'>{_bar_cell(r['Psychology'], -5, 20)}</td>"
                    f"<td style='padding:6px 10px;min-width:90px;'>{_bar_cell(r['Discovery'], 25, 50)}</td>"
                    f"<td style='padding:6px 10px;'>{_badge_cell(r['Valuation'])}</td>"
                    f"<td style='padding:6px 10px;'>{_badge_cell(r['Sentiment'])}</td>"
                    f"<td style='padding:6px 10px;'>{_badge_cell(r['Trend'])}</td>"
                    f"<td style='padding:6px 10px;'>{_badge_cell(r['Trade Setup'])}</td>"
                    f"<td style='padding:6px 10px;min-width:90px;'>{_bar_cell(r['Trade Setup Score'], 45, 65)}</td>"
                    "</tr>"
                )

            _cmp_headers = [
                "Ticker", "Type", "Price", "Quality Score", "Intrinsic Value", "MOS",
                "Long Score", "Psychology Score", "Discovery Score", "Valuation",
                "Sentiment", "Trend", "Trade Setup", "Trade Setup Score",
            ]
            _cmp_html = (
                "<table style='width:100%;border-collapse:collapse;font-size:14px;'>"
                "<thead><tr>"
                + "".join(
                    f"<th style='text-align:left;padding:6px 10px;border-bottom:2px solid #ddd;'>{h}</th>"
                    for h in _cmp_headers
                )
                + "</tr></thead><tbody>"
                + "".join(_cmp_rows_html)
                + "</tbody></table>"
            )
            st.markdown(_cmp_html, unsafe_allow_html=True)

            st.subheader("Full Stock Database")
            st.caption(
                "Every cell is computed from the stock's own sourced data. Values in "
                "red are default/average assumptions used where data was unavailable. "
                "Note: 'Entry'/'Target' here are the DCF valuation basis (Entry = "
                "80% of intrinsic value, Target = intrinsic value) - a long-term "
                "fair-value number, NOT a trade level. The Trade Filter's own Entry "
                "Zone/Stop/Targets (a technical number) live in the 'Trade Setup' "
                "table above, and the ATR-based Swing Entry/Stop/Targets (a third, "
                "independent number) live in the 'Swing Setup' table above - not "
                "here. All three are independent by design."
            )
            _hidden = [
                "Type Source", "Quality Source", "Intrinsic Source",
                "_flag_type", "_flag_quality", "_flag_intrinsic", "_flag_growth",
                "_mos_num",
                # Swing-mode fields now live in their own 'Swing Setup' table above
                # instead of being mixed in here alongside the DCF/Long-Score data.
                "Trader Score", "Trend", "RSI", "MACD Cross",
                "Swing Setup", "Setup Score", "Swing Entry", "Swing Stop",
                "Swing T1", "Swing T2", "Swing RR", "ATR Stop", "Shares",
                "Capital At Risk", "Position Value", "Regime",
                "Earnings (days)", "Earnings Warn",
            ]
            _full = results.drop(columns=[c for c in _hidden if c in results.columns])
            st.dataframe(
                style_defaults(_full, results),
                width="stretch",
                hide_index=True,
                column_config={
                    "Entry": st.column_config.NumberColumn(
                        "Entry",
                        help=(
                            "DCF valuation entry (80% of intrinsic value) - a "
                            "long-term fair-value buy-in point, NOT a technical "
                            "trade level. See 'Trade Setup' above for the Trade "
                            "Filter's technical Entry Zone, and 'Swing Setup' for "
                            "the ATR-based swing entry."
                        ),
                    ),
                    "Target": st.column_config.NumberColumn(
                        "Target",
                        help=(
                            "DCF valuation target = the computed intrinsic value "
                            "itself. Long-term fair-value basis - different from "
                            "the Trade Filter's technical targets and the Swing "
                            "T1/T2 in 'Swing Setup'."
                        ),
                    ),
                },
            )

            with st.expander("Show data source / default flags per field"):
                _src = results[[
                    "Ticker", "Type Source", "Quality Source", "Intrinsic Source",
                    "_flag_type", "_flag_quality", "_flag_intrinsic", "_flag_growth",
                ]].rename(columns={
                    "_flag_type": "Type Default",
                    "_flag_quality": "Quality Default",
                    "_flag_intrinsic": "Intrinsic Default",
                    "_flag_growth": "Growth Default",
                })
                st.dataframe(_src, width="stretch", hide_index=True)

            # -----------------------------------
            # DCF PARAMETERS  (per-stock manual override, THIS SESSION ONLY)
            # -----------------------------------
            # The old "Valuation & FCF inputs" global-defaults panel (Auto toggle +
            # flat discount/perpetual/growth sliders) was removed - Auto mode
            # (per-stock CAPM discount rate + analyst-consensus/historical growth +
            # currency-based terminal growth) stays permanently on via the
            # dcf_auto session-state default set above, and this table below still
            # covers per-stock overrides for anyone who wants to hand-set a number
            # for a specific ticker.
            # Moved here from the top "Valuation & FCF inputs" panel so the override
            # cells sit right next to the numbers they're overriding, for every
            # scanned stock at once, instead of picking one ticker from a dropdown
            # before you'd even seen the results. Saving here updates
            # st.session_state["fcf_overrides"] exactly like the old per-ticker panel
            # did, so every table above (Intrinsic Value, MOS, Long Score, Valuation,
            # Entry/Target...) still picks it up the next time you click Run Scan -
            # only WHERE you set the override has changed, not how it propagates.
            st.subheader("DCF Parameters (Growth / Discount / Perpetual)")
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
                ["Ticker", "Price", "Intrinsic Value", "IV/Price Multiple", "DCF Growth %",
                 "Growth Governor", "DCF Discount %", "DCF Perpetual %", "Long Score"]
            ].copy()

            st.session_state.setdefault("scanner_override_mode", False)
            _mode_label = ("Hide manual override" if st.session_state["scanner_override_mode"]
                           else "Enable manual override")
            if st.button(_mode_label, key="toggle_scanner_override"):
                st.session_state["scanner_override_mode"] = not st.session_state["scanner_override_mode"]
                st.rerun()

            if not st.session_state["scanner_override_mode"]:
                st.dataframe(
                    style_defaults(_dcf_params_df, results, color_long_score=True),
                    width="stretch", hide_index=True,
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
            st.info(f"Scan completed in {scan_time} seconds")

        else:
            st.warning("No stock data returned.")


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

_nav = st.navigation(
    [PG_HOME, PG_DEEP_DIVE, PG_COMPARISON, PG_RESEARCH], position="hidden"
)
_nav.run()
