"""
nightly_scan.py

Standalone (no Streamlit UI) scoring pass over a whole universe, run by
scheduler_engine overnight - or by hand:

    python nightly_scan.py "ASX 200"

Writes results via scan_store so the Scanner page can serve a whole-index
ranking instantly instead of making a visitor sit through a live scan.

ATTENTION-LITE ABOVE A SIZE THRESHOLD, same rule the live Scanner already
applies (app.py's _render_scan_results(lite_threshold=100)): above
NIGHTLY_LITE_THRESHOLD tickers, Google Trends/NewsAPI/StockTwits calls are
skipped and Discovery reflects price/volume attention only - at index
scale those calls are what blow both the clock and the shared API quotas
(one S&P 500 pass would burn a free NewsAPI day on its own), and they
contribute the least stable part of the score. At or below the threshold
(every TradingView import batch, capped at IMPORTED_NIGHTLY_BATCH = 100
tickers, and any hand-run scan of a small universe), the full Trends/News/
Social signal is fetched too, so a small overnight scan's Long Score
actually agrees with what the live Scanner or Deep Dive page would show
for the same ticker on the same day - previously this module always ran
lite regardless of size, which is what its own docstring claimed NOT to
do; that mismatch is what this file now actually implements.

Yahoo Finance etiquette: one ticker at a time with a small sleep - the
whole point of running overnight is that nobody is waiting.
"""

import sys
import time

import pandas as pd
import yfinance as yf

import scan_store
import score_history
import scanner_engine
import screen_import_store
import indicators_engine
import social_engine
import trade_filter_engine
from ranking_engine import calculate_long_score
from resolver_engine import (
    resolve_quality_score,
    resolve_intrinsic_value,
    resolve_stock_type,
)
from trends_engine import get_trend_score
from news_engine import get_news_score, get_yahoo_news_score

PER_TICKER_SLEEP = 0.5

# Same breakpoint as the live Scanner's own lite_threshold (app.py) - kept
# as a separate constant here (rather than imported) since the two modules
# don't otherwise share config, but the VALUE must stay in sync so overnight
# and live scans agree about what counts as "big enough to go lite".
NIGHTLY_LITE_THRESHOLD = 100

# The TradingView-CSV import queue (screen_import_store.py) - a virtual
# "universe" that isn't a real index, resolved from the import queue
# instead of scanner_engine. See run_imported_scan() below.
IMPORTED_UNIVERSE = "imported"
IMPORTED_NIGHTLY_BATCH = 100  # per-run cap: a big CSV import (up to
# screen_import_store.MAX_TICKERS_PER_IMPORT = 500) simply spreads across
# multiple nights instead of blowing the nightly budget in one go -
# screen_import_store.mark_scanned() persists progress per ticker, so a
# partial run always resumes exactly where it left off. Capped at exactly
# NIGHTLY_LITE_THRESHOLD, so an import batch is always <= the threshold and
# therefore always gets full (not lite) attention - see run_imported_scan().


def analyze_ticker_lite(ticker, attention_lite=True, discount_rate=None,
                         perpetual_rate=None, growth_rate=None, manual_fcf=None):
    """Core value/quality/psychology scoring for one ticker - the same
    resolvers and Long Score the site uses. Returns a plain dict, or None
    if no usable price data. Also used by digest_engine for the weekly
    watchlist email (always attention_lite=True there - a watchlist digest
    can span many users' combined tickers, so it keeps the original
    quota-safe default regardless of how many tickers that turns out to
    be).

    attention_lite=True (the default) skips Google Trends/NewsAPI/
    StockTwits, matching this module's original behaviour and the live
    Scanner's own large-scan mode - Discovery is price/volume only.
    attention_lite=False additionally fetches those three live signals and
    folds them into Discovery exactly like deep_dive_engine.analyze() does
    (same functions, same formula, live_data=True/enable_social=True
    equivalent) - this is what makes a small overnight scan's Long Score
    agree with the Deep Dive page for the same ticker, rather than always
    coming in lower via the lite-only Discovery term. Callers choose which
    mode by ticker-count (see NIGHTLY_LITE_THRESHOLD).

    discount_rate/perpetual_rate/growth_rate/manual_fcf: passed straight
    through to resolve_intrinsic_value() - left at None (the default) for
    the overnight/background scan and the digest email, which have no
    per-user settings to apply. A foreground caller with access to the
    viewer's Valuation & FCF Inputs settings (see portfolio_health_engine.
    fetch_snapshot()) can pass them so this ticker's Intrinsic Value/MOS
    here actually matches what the Deep Dive page shows for the same
    ticker under the same settings, instead of always being pure-auto
    regardless of what the user has configured."""
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
        discount_rate=discount_rate, perpetual_rate=perpetual_rate,
        growth_rate=growth_rate, manual_fcf=manual_fcf,
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
    discovery = activity + vol_ratio * 10  # price/volume attention, always included

    if not attention_lite:
        # Same three calls, same formula, as deep_dive_engine.analyze()'s
        # live_data=True/enable_social=True path - api_key=None on the first
        # two falls back to NEWS_API_KEY from secrets/env, same as every
        # other caller of these functions.
        keyword = ticker.split(".")[0]
        try:
            trend_score = get_trend_score(keyword)
        except Exception:
            trend_score = 0
        try:
            news_score = get_news_score(keyword) + get_yahoo_news_score(ticker)
        except Exception:
            news_score = 0
        try:
            social_score, _social_detail = social_engine.get_social_score(ticker)
        except Exception:
            social_score = 0
        discovery += trend_score + news_score + social_score

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
    be resolved). Goes attention-lite only when the resolved universe is
    bigger than NIGHTLY_LITE_THRESHOLD (a real index like ASX 200/S&P 500
    always will be; a hand-run scan of a small custom list won't)."""
    country = "Australia" if universe in scanner_engine.AUSTRALIA_UNIVERSES else "USA"
    tickers, source = scanner_engine.resolve_tickers(country, universe, "All")
    if not tickers:
        log(f"[nightly_scan] {universe}: no tickers resolved ({source})")
        return None
    if max_tickers:
        tickers = tickers[:max_tickers]
    attention_lite = len(tickers) > NIGHTLY_LITE_THRESHOLD
    log(f"[nightly_scan] {universe}: scanning {len(tickers)} tickers ({source}), "
        f"attention_lite={attention_lite}")

    rows = []
    for i, t in enumerate(tickers):
        try:
            row = analyze_ticker_lite(t, attention_lite=attention_lite)
            if row:
                rows.append(row)
        except Exception as e:  # one bad ticker never kills the run
            log(f"[nightly_scan] {t}: {e}")
        if i % 25 == 24:
            log(f"[nightly_scan] {universe}: {i + 1}/{len(tickers)} done")
        time.sleep(PER_TICKER_SLEEP)

    rows.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)
    payload = scan_store.save_scan(universe, rows, source, attention_lite=attention_lite)
    log(f"[nightly_scan] {universe}: saved {len(rows)} rows")
    try:
        score_history.record(rows)
        log(f"[nightly_scan] {universe}: recorded {len(rows)} rows to score_history")
    except Exception as e:  # history logging must never fail the scan itself
        log(f"[nightly_scan] {universe}: score_history.record failed: {e}")
    return payload


def run_imported_scan(max_tickers=IMPORTED_NIGHTLY_BATCH, log=print):
    """
    Scan up to `max_tickers` PENDING tickers from screen_import_store (the
    TradingView CSV import queue), using the exact same per-ticker scoring
    as every other universe (analyze_ticker_lite). Unlike
    run_universe_scan (which always rescans its whole universe fresh from
    scratch), this is incremental: each ticker is scanned at most once,
    screen_import_store.mark_scanned() persists the outcome immediately,
    and a screen bigger than one night's batch just continues on the next
    call - nothing here raises or depends on the existing universes'
    per-run behaviour, which is untouched.

    After the batch, rebuilds scan_store's "imported" payload from EVERY
    successfully-scanned ticker across all imports (not just this batch),
    so the Scanner page's "Imported screen" view always reflects
    everything scanned so far, the same way a normal universe's saved scan
    always reflects its most recent full pass. Returns the saved payload,
    or None if there was nothing pending.
    """
    pending = screen_import_store.get_pending(limit=max_tickers)
    if not pending:
        log("[nightly_scan] imported: nothing pending")
        return None
    # A batch is capped at IMPORTED_NIGHTLY_BATCH (== NIGHTLY_LITE_THRESHOLD
    # by default), so this is normally always False - a TradingView import,
    # however large, gets full (not lite) attention per batch, same as any
    # other scan at or under the threshold. Computed properly rather than
    # hardcoded so a caller passing a bigger max_tickers by hand still gets
    # the right behaviour.
    attention_lite = len(pending) > NIGHTLY_LITE_THRESHOLD
    log(f"[nightly_scan] imported: scanning {len(pending)} pending ticker(s), "
        f"attention_lite={attention_lite}")

    for i, t in enumerate(pending):
        try:
            row = analyze_ticker_lite(t, attention_lite=attention_lite)
            screen_import_store.mark_scanned(t, ok=bool(row), row=row)
        except Exception as e:  # one bad ticker never kills the batch
            log(f"[nightly_scan] imported {t}: {e}")
            screen_import_store.mark_scanned(t, ok=False)
        if i % 25 == 24:
            log(f"[nightly_scan] imported: {i + 1}/{len(pending)} done")
        time.sleep(PER_TICKER_SLEEP)

    rows = screen_import_store.all_scanned_rows()
    rows.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)
    # attention_lite here describes THIS run's mode - the combined payload
    # can include rows scanned in an earlier run/under old code, so this is
    # a summary of "how the most recent pass worked", not a per-row
    # guarantee. Good enough for the Scanner page's caption; nothing
    # downstream relies on it being exact per-row.
    payload = scan_store.save_scan(IMPORTED_UNIVERSE, rows, "TradingView import", attention_lite=attention_lite)
    log(f"[nightly_scan] imported: saved {len(rows)} total scanned row(s) "
        f"({len(pending)} newly scanned this run)")
    try:
        score_history.record(rows)
        log(f"[nightly_scan] imported: recorded {len(rows)} rows to score_history")
    except Exception as e:  # history logging must never fail the scan itself
        log(f"[nightly_scan] imported: score_history.record failed: {e}")
    return payload


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "ASX 200"
    if target == IMPORTED_UNIVERSE:
        run_imported_scan()
    else:
        run_universe_scan(target)
