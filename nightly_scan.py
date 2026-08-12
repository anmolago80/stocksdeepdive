"""
nightly_scan.py

Standalone (no Streamlit UI) scoring pass over a whole universe, run by
scheduler_engine overnight - or by hand:

    python nightly_scan.py "ASX 200"

Writes results via scan_store so the Scanner page can serve a whole-index
ranking instantly instead of making a visitor sit through a live scan.

DELIBERATELY ATTENTION-LITE: no Google Trends, NewsAPI or StockTwits calls.
At index scale those are what blow both the clock and the shared API
quotas (one S&P 500 pass would burn a free NewsAPI day on its own), and
they contribute the least stable part of the score. Discovery here
reflects price/volume attention only - the same "attention-lite" rule the
live Scanner applies above its size threshold, so overnight and live big
scans are consistent with each other.

Yahoo Finance etiquette: one ticker at a time with a small sleep - the
whole point of running overnight is that nobody is waiting.
"""

import sys
import time

import pandas as pd
import yfinance as yf

import scan_store
import scanner_engine
import indicators_engine
import trade_filter_engine
from ranking_engine import calculate_long_score
from resolver_engine import (
    resolve_quality_score,
    resolve_intrinsic_value,
    resolve_stock_type,
)

PER_TICKER_SLEEP = 0.5


def analyze_ticker_lite(ticker):
    """Core value/quality/psychology scoring for one ticker - the same
    resolvers and Long Score the site uses, minus the attention lookups.
    Returns a plain dict, or None if no usable price data. Also used by
    digest_engine for the weekly watchlist email."""
    tk = yf.Ticker(ticker)
    try:
        df = tk.history(period="6mo")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    try:
        cashflow_df = tk.cashflow
    except Exception:
        cashflow_df = pd.DataFrame()

    window_3mo = df.tail(63)
    current_price = float(window_3mo["Close"].iloc[-1])
    high_price = float(window_3mo["Close"].max())
    fear = ((high_price - current_price) / high_price) * 100 if high_price else 0.0
    ma50 = window_3mo["Close"].rolling(50).mean().iloc[-1]
    if pd.isna(ma50) or ma50 == 0:
        ma50 = current_price
    greed = max(((current_price - ma50) / ma50) * 100, 0)

    quality, _src, quality_default = resolve_quality_score(ticker, info=info)
    intrinsic, _ivsrc, _g, iv_meta = resolve_intrinsic_value(
        ticker, quality, info=info, cashflow_df=cashflow_df,
        currency=info.get("currency"),
        discount_rate=None, perpetual_rate=None, growth_rate=None, manual_fcf=None,
    )
    stock_type, stock_type_src, _tdef = resolve_stock_type(ticker, info=info)
    if stock_type_src == "auto":
        low52 = info.get("fiftyTwoWeekLow", 0) or 0
        if low52 > 0 and current_price <= low52 * 1.15:
            stock_type = "TURNAROUND"

    mos = ((intrinsic - current_price) / intrinsic) * 100 if intrinsic > 0 else 0.0

    if len(window_3mo) >= 6 and window_3mo["Close"].iloc[-6] != 0:
        weekly = ((current_price - window_3mo["Close"].iloc[-6])
                  / window_3mo["Close"].iloc[-6]) * 100
    else:
        weekly = 0.0
    fomo = max(greed + max(weekly, 0), 0)
    psychology = fear - greed - fomo
    activity = abs(weekly)
    avg_vol = window_3mo["Volume"].mean()
    vol_ratio = (window_3mo["Volume"].iloc[-1] / avg_vol) if avg_vol > 0 else 0
    discovery = activity + vol_ratio * 10  # attention-lite: price/volume only

    long_score = calculate_long_score(quality, mos, psychology, discovery)

    if intrinsic <= 0:
        valuation = "N/A"
    elif mos >= 25:
        valuation = "UNDERVALUED"
    elif mos < 0:
        valuation = "EXPENSIVE"
    else:
        valuation = "FAIR"

    if long_score > 70:
        signal = "STRONG LONG"
    elif long_score > 50:
        signal = "LONG"
    elif long_score > 30:
        signal = "WATCHLIST"
    else:
        signal = "AVOID"
    if valuation == "N/A" and signal in ("STRONG LONG", "LONG"):
        signal = "WATCHLIST"

    trade_signal, trend = "-", "-"
    try:
        sr = trade_filter_engine.calc_support_resistance(df["Close"])
        ind = indicators_engine.compute_indicators(df)
        trade = trade_filter_engine.evaluate_trade(
            current_price=current_price, ma50=ma50,
            support20=sr["support20"], resistance20=sr["resistance20"],
            support60=sr["support60"], resistance60=sr["resistance60"],
            long_score=long_score, psychology_score=psychology,
            discovery_score=discovery, fomo_score=fomo, greed_score=greed,
            trend=ind["trend"],
        )
        trade_signal = trade["signal"]
        trend = ind["trend"].title()
    except Exception:
        pass

    return {
        "Ticker": ticker,
        "Type": stock_type,
        "Price": round(current_price, 2),
        "Quality": quality,
        "Quality Default": bool(quality_default),
        "Intrinsic Value": round(intrinsic, 2) if intrinsic > 0 else None,
        "Intrinsic Default": bool(iv_meta.get("value_default", False)),
        "MOS %": round(mos, 1) if intrinsic > 0 else None,
        "Psychology": round(psychology, 1),
        "Discovery (lite)": round(discovery, 1),
        "Long Score": round(long_score, 1),
        "Valuation": valuation,
        "Signal": signal,
        "Trend": trend,
        "Trade Setup": trade_signal,
    }


def run_universe_scan(universe, max_tickers=None, log=print):
    """Scan every ticker in `universe` and persist the ranked result via
    scan_store. Returns the saved payload (or None if the universe couldn't
    be resolved)."""
    country = "Australia" if universe in scanner_engine.AUSTRALIA_UNIVERSES else "USA"
    tickers, source = scanner_engine.resolve_tickers(country, universe, "All")
    if not tickers:
        log(f"[nightly_scan] {universe}: no tickers resolved ({source})")
        return None
    if max_tickers:
        tickers = tickers[:max_tickers]
    log(f"[nightly_scan] {universe}: scanning {len(tickers)} tickers ({source})")

    rows = []
    for i, t in enumerate(tickers):
        try:
            row = analyze_ticker_lite(t)
            if row:
                rows.append(row)
        except Exception as e:  # one bad ticker never kills the run
            log(f"[nightly_scan] {t}: {e}")
        if i % 25 == 24:
            log(f"[nightly_scan] {universe}: {i + 1}/{len(tickers)} done")
        time.sleep(PER_TICKER_SLEEP)

    rows.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)
    payload = scan_store.save_scan(universe, rows, source)
    log(f"[nightly_scan] {universe}: saved {len(rows)} rows")
    return payload


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "ASX 200"
    run_universe_scan(target)
