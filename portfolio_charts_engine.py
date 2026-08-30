"""
portfolio_charts_engine.py

Computations behind four "My Portfolio" additions - value-over-time vs
index, real dividends received, and the data the cheap-vs-healthy map and
table upgrades need beyond what portfolio_health_engine.py / app.py's
_build_portfolio_rows() already compute. app.py only renders what these
functions hand back (Chart discipline / ground rule #6 in the spec this
module implements).

Kept separate from portfolio_health_engine.py because these are largely
long-range TIME-SERIES fetches (multi-year daily price history, dividend
payment history) rather than the point-in-time Health/Progress scoring
that module owns - splitting them keeps each file's cache keys and
concerns distinct. Nothing here touches st.session_state or renders
anything; only @st.cache_data is used, the same caching convention
portfolio_health_engine.py already uses.

FX NOTE (shared by every function below that touches AUD): every AUD
conversion in this module uses TODAY's live FX rate for the whole
requested date range - never a historical rate. This matches the rest of
the site's AUD conversions (portfolio_health_engine.to_aud/fx_to_aud) and
is called out explicitly wherever a caller-facing caption should say so.
"""

import base64
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import yfinance as yf

import portfolio_health_engine as phe


# ---------------------------------------------------------------------
# Shared long-range history fetch
# ---------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_history_from(ticker, start_date_iso):
    """Daily OHLCV for `ticker` from start_date_iso to today. Separate
    from portfolio_health_engine.fetch_snapshot()'s own history (a fixed
    2y window used for 52wk-range/MA200/drawdown) - the value-over-time
    chart needs the FULL range back to a holding's buy date, which is
    routinely older than 2y. A ticker listed after start_date_iso just
    returns whatever Yahoo has from its own listing date onward; callers
    reindex/ffill onto a shared date axis and treat anything still
    missing as "not owned yet" rather than an error."""
    try:
        h = yf.Ticker(ticker).history(start=start_date_iso)
    except Exception:
        return pd.DataFrame()
    if h is None or h.empty:
        return pd.DataFrame()
    h = h.copy()
    if h.index.tz is not None:
        h.index = h.index.tz_localize(None)
    h = h[~h.index.duplicated(keep="last")]
    return h


def _benchmark_for(ticker, currency):
    """Same AU/US split the Swing scanner's market-regime check already
    uses (app.py: benchmark_ticker = ^AXJO if AU else ^GSPC) - each
    purchase is compared to ITS OWN listing's index, so a mixed AU/US
    portfolio's index line is an honest blend rather than one index
    pretending to represent both markets."""
    if (ticker or "").strip().upper().endswith(".AX") or (currency or "").upper() == "AUD":
        return "^AXJO", "ASX 200"
    return "^GSPC", "S&P 500"


# ---------------------------------------------------------------------
# 1. Portfolio value over time + vs-index (Holdings tab centrepiece)
# ---------------------------------------------------------------------

def compute_value_vs_index_series(holdings):
    """V(t) portfolio value / C(t) cost basis / I(t) same-dollars index,
    daily AUD, from the earliest buy date among `holdings` to today.

      V(t) = sum over holdings owned at t of shares x close(t) x fx_now
      C(t) = step function, jumps by that holding's AUD cost at its buy date
      I(t) = sum over holdings of (that purchase's AUD cost / that day's
             benchmark close) x benchmark close(t) - "index units" bought
             with the same dollars, each holding priced off its own
             listing's benchmark (see _benchmark_for)

    fx is TODAY's live rate applied across the whole series (no historical
    FX fetch - see module docstring).

    Returns None when there's nothing to chart (no holding has both a buy
    date and shares > 0). Otherwise:
      dates: DatetimeIndex
      value / cost / index: pd.Series aligned to `dates`, AUD
      buy_events: [{"date", "ticker", "portfolio", "cost_aud"}, ...] sorted
      excluded: [ticker, ...] - left out of V()/I() (no history or no FX)
      benchmarks_used: {"ASX 200", ...} - for the caption
      lead_pp: float or None - (V/C - 1) - (I/C - 1) in percentage points
    """
    priced = [h for h in holdings if h.get("buy_date") and (h.get("shares") or 0) > 0]
    if not priced:
        return None

    buy_dates = [pd.Timestamp(h["buy_date"]).normalize() for h in priced]
    start = min(buy_dates)
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    if start > today:
        return None
    full_idx = pd.date_range(start, today, freq="D")

    value = pd.Series(0.0, index=full_idx)
    cost = pd.Series(0.0, index=full_idx)
    index_val = pd.Series(0.0, index=full_idx)
    buy_events, excluded, benchmarks_used = [], [], set()

    for h in priced:
        ticker = h["ticker"]
        shares = h.get("shares") or 0
        buy_price = h.get("buy_price") or 0
        currency = h.get("currency") or "AUD"
        buy_date = pd.Timestamp(h["buy_date"]).normalize()

        fx = phe.fx_to_aud(currency)
        if fx is None:
            excluded.append(ticker)
            continue

        hist = fetch_history_from(ticker, buy_date.date().isoformat())
        if hist.empty:
            excluded.append(ticker)
            continue

        closes = hist["Close"].reindex(full_idx).ffill()
        closes = closes.where(full_idx >= buy_date)
        contrib = (shares * closes * fx).fillna(0.0)
        value = value.add(contrib, fill_value=0.0)

        cost_aud = shares * buy_price * fx
        step = pd.Series(0.0, index=full_idx)
        step[full_idx >= buy_date] = cost_aud
        cost = cost.add(step, fill_value=0.0)
        buy_events.append({
            "date": buy_date, "ticker": ticker, "portfolio": h.get("portfolio"),
            "cost_aud": cost_aud,
        })

        bench_ticker, bench_label = _benchmark_for(ticker, currency)
        bench_hist = fetch_history_from(bench_ticker, buy_date.date().isoformat())
        if not bench_hist.empty:
            b_series = bench_hist["Close"].reindex(full_idx).bfill().ffill()
            b_at_buy = b_series.loc[buy_date] if buy_date in b_series.index else None
            if pd.notna(b_at_buy) and b_at_buy:
                units = cost_aud / b_at_buy
                bcontrib = (units * b_series).where(full_idx >= buy_date, 0.0).fillna(0.0)
                index_val = index_val.add(bcontrib, fill_value=0.0)
                benchmarks_used.add(bench_label)

    if not buy_events:
        return None

    buy_events.sort(key=lambda e: e["date"])

    lead_pp = None
    c_today, v_today, i_today = cost.iloc[-1], value.iloc[-1], index_val.iloc[-1]
    if c_today:
        v_ret = (v_today / c_today) - 1
        i_ret = ((i_today / c_today) - 1) if i_today else None
        if i_ret is not None:
            lead_pp = (v_ret - i_ret) * 100

    return {
        "dates": full_idx, "value": value, "cost": cost, "index": index_val,
        "buy_events": buy_events, "excluded": sorted(set(excluded)),
        "benchmarks_used": benchmarks_used, "lead_pp": lead_pp,
    }


# ---------------------------------------------------------------------
# 3. Dividends - actually received vs potential
# ---------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_dividend_history(ticker):
    """Per-share dividend payment history (ex-dividend date -> amount),
    same source (`yf.Ticker(...).dividends`) fundamentals_data.py already
    uses for the Deep Dive page's dividend chart."""
    try:
        d = yf.Ticker(ticker).dividends
    except Exception:
        return pd.Series(dtype=float)
    if d is None or d.empty:
        return pd.Series(dtype=float)
    d = d.copy()
    if d.index.tz is not None:
        d.index = d.index.tz_localize(None)
    return d


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_next_ex_dividend(ticker):
    """Next ex-dividend date if Yahoo has one on file AND it's in the
    future - a past exDividendDate (Yahoo keeps the most recent one on
    `.info` even long after it's passed) is not "next" anything, so it's
    filtered out here rather than left for the caller to re-check."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    ts = info.get("exDividendDate")
    if not ts:
        return None
    try:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    except Exception:
        return None
    return d.isoformat() if d >= datetime.now(timezone.utc).date() else None


def dividends_received(ticker, currency, shares, buy_date_iso):
    """(received_aud, trailing_12m_per_share) for one holding.

    received_aud = sum of per-share payments with ex-date >= buy_date,
    times shares, AUD-converted at today's live FX (see module docstring)
    - assumes constant shares since purchase (the store has no
    partial-buy history). None (not 0.0) when the ticker has NO dividend
    history at all - callers show "no distributions" only for that case,
    distinct from a real 0.0 (dividend payer, just nothing has landed
    since a recent purchase).

    trailing_12m_per_share = payments in the last 365 days, for
    yield-on-cost (trailing_12m_per_share / buy_price) at the call site.
    """
    div = fetch_dividend_history(ticker)
    if div.empty:
        return None, 0.0

    try:
        buy_ts = pd.Timestamp(buy_date_iso).normalize()
    except Exception:
        buy_ts = None
    since_buy = div[div.index.normalize() >= buy_ts] if buy_ts is not None else div
    per_share_total = float(since_buy.sum())

    fx = phe.fx_to_aud(currency)
    received_aud = (per_share_total * shares * fx) if fx is not None else None

    cutoff = pd.Timestamp(datetime.now(timezone.utc).date()) - pd.Timedelta(days=365)
    trailing = div[div.index.normalize() >= cutoff]
    trailing_12m_per_share = float(trailing.sum())

    return received_aud, trailing_12m_per_share


# ---------------------------------------------------------------------
# 4. Overview table upgrades - small rendering helpers shared with app.py
# ---------------------------------------------------------------------

def sparkline_data_uri(values):
    """A tiny (120x32) inline SVG line, base64 data-URI, for the Overview
    table's Health-trend column (via st.column_config.ImageColumn - a
    per-row-colourable sparkline isn't possible with the built-in
    LineChartColumn, which is one fixed colour for the whole column).
    Green if the series ended >= where it started, red if it declined.
    Fewer than 2 points -> a flat muted dash in the same image footprint,
    so the column stays one consistent type instead of mixing images and
    plain text."""
    values = [v for v in (values or []) if v is not None]
    w, h, pad = 120, 32, 4
    if len(values) < 2:
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<line x1="{w/2-12}" y1="{h/2}" x2="{w/2+12}" y2="{h/2}" '
            f'stroke="#5b6b80" stroke-width="2" stroke-linecap="round"/></svg>'
        )
    else:
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1.0
        n = len(values)
        xs = [pad + i * (w - 2 * pad) / (n - 1) for i in range(n)]
        ys = [h - pad - (v - lo) / span * (h - 2 * pad) for v in values]
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        color = "#22c55e" if values[-1] >= values[0] else "#fb7185"
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
