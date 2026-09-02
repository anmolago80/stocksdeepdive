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

import math
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

import auto_compounder_engine
import fundamentals_data
import moat_engine
import peer_context
import reverse_dcf_engine
import scan_store
import score_history
import scanner_engine
import screen_import_store
import snapshot_store
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

# Audit fix 2.3: a scan that completed for fewer than this fraction of its
# resolved universe is treated as failed/degraded rather than a
# legitimate result - the realistic cause is Yahoo rate-limiting mid-run,
# not a genuinely smaller universe (the universe is resolved up front via
# scanner_engine.resolve_tickers(), so its size is already known). Without
# this, a 100/500-ticker partial run would silently overwrite last night's
# complete 500-row ranking, "succeed", and not be retried until the next
# scheduled window because the save looks perfectly fresh.
#
# Fix 9 item 3 (2026-09-01): bumped 0.5 -> 0.6, and the row COUNT this is
# measured against is now "rows with a real finite price that survived
# the hard per-row guard in run_universe_scan()", not just "any row
# analyze_ticker_lite() happened to return" - see run_universe_scan's own
# comment for why a NaN-price run needs a stricter, not just a
# same-shaped, safety net.
SCAN_COMPLETENESS_THRESHOLD = 0.6

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


def _attach_moat(row, ticker, log=print):
    """Adds "Moat"/"Moat Erosion" to an already-built row dict, in place -
    called only from the two REAL nightly scan paths below (run_universe_
    scan/run_imported_scan), deliberately NOT from inside
    analyze_ticker_lite() itself, since that function is shared with
    digest_engine's weekly watchlist email (many users' tickers, on its
    own schedule) - attaching a full fundamentals-bundle-backed Moat
    computation there would slow/change a feature nobody asked to touch.
    Moat's own 24h per-ticker cache (moat_engine.py) means this is cheap
    on any ticker already scanned today by either path. One bad ticker
    never kills the row - Moat/erosion are simply left out of it, and the
    Scanner's own stored-value convention already renders that as "-"."""
    try:
        moat = moat_engine.compute_moat(ticker)
        row["Moat"] = moat["score"]
        row["Moat Erosion"] = moat["erosion"]
        row["Moat Mode"] = moat["mode"]
    except Exception as e:
        log(f"[nightly_scan] {ticker}: moat_engine failed: {e}")


def _attach_dividend_payout(row, ticker, log=print):
    """Adds "Payout Ratio %" to an already-built row dict, in place -
    Services batch 3, Part A1. Called only from the two REAL nightly scan
    paths below, same reasoning as _attach_moat right above: needs a full
    fundamentals bundle (for TTM EPS via auto_compounder_engine._eps_ttm),
    which the weekly digest email's per-ticker path (analyze_ticker_lite,
    shared with digest_engine) shouldn't be made to pay for. fundamentals_
    data.get_bundle() here is the SAME bundle _attach_moat's own
    moat_engine.compute_moat() call already fetched/cached for this
    ticker moments ago, so this is cheap on any ticker scanned today by
    either real path. One bad ticker never kills the row - Payout Ratio
    is simply left out (None), same "-" rendering convention as every
    other optional scan field. No-op (leaves the row's "Payout Ratio %"
    absent) when the row has no TTM dividend at all - dividing by EPS for
    a non-payer isn't a "0% payout", it's not applicable."""
    _dps = row.get("Dividend TTM")
    if not _dps:
        return
    try:
        bundle = fundamentals_data.get_bundle(ticker)
        if not bundle:
            return
        trailing_eps, _flagged = auto_compounder_engine._eps_ttm(bundle, ticker=ticker)
        if trailing_eps and trailing_eps > 0:
            row["Payout Ratio %"] = round(_dps / trailing_eps * 100, 1)
    except Exception as e:
        log(f"[nightly_scan] {ticker}: dividend payout ratio failed: {e}")


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

    # Services batch 3, Part A1: dividend headline numbers for the
    # snapshot/API/MCP surfaces (see snapshot_store._PUBLIC_FIELD_MAP).
    # Cheap enough to compute for every caller including the weekly
    # digest email (one extra `tk.dividends` call; exDividendDate comes
    # from `info`, already fetched above) - unlike Payout Ratio below,
    # which needs a full fundamentals bundle and is deliberately kept out
    # of this shared function (see _attach_dividend_payout's docstring).
    # Same trailing-365-day/next-future-exDividendDate logic as
    # portfolio_charts_engine.dividend_ttm_per_share()/
    # fetch_next_ex_dividend() - duplicated rather than imported since
    # that module is Streamlit-cache-coupled (`@st.cache_data`) and this
    # function also runs standalone, outside any Streamlit script (see
    # this module's own docstring).
    try:
        _div_hist = tk.dividends
        if _div_hist is not None and not _div_hist.empty:
            _div_hist = _div_hist.copy()
            if _div_hist.index.tz is not None:
                _div_hist.index = _div_hist.index.tz_localize(None)
            _cutoff = pd.Timestamp(datetime.now(timezone.utc).date()) - pd.Timedelta(days=365)
            dividend_ttm = float(_div_hist[_div_hist.index.normalize() >= _cutoff].sum())
        else:
            dividend_ttm = None
    except Exception:
        dividend_ttm = None

    next_ex_date = None
    _ex_ts = info.get("exDividendDate")
    if _ex_ts:
        try:
            _ex_d = datetime.fromtimestamp(_ex_ts, tz=timezone.utc).date()
            if _ex_d >= datetime.now(timezone.utc).date():
                next_ex_date = _ex_d.isoformat()
        except Exception:
            next_ex_date = None

    # Fix 9 (2026-09-01): price from the last VALID bar, not blindly
    # iloc[-1]. Root cause (confirmed via Railway logs from the 31 Aug
    # 20:00 UTC run): yfinance's most recent bar can be a still-forming
    # placeholder with Close = NaN - normally just-today's not-yet-closed
    # session (see deep_dive_engine.analyze()'s matching comment, which
    # already carries this exact fix), but for ASX tickers scanned at
    # 20:00 UTC (06:00 Brisbane, before the ASX open) EVERY ticker's
    # in-progress session bar was NaN, so every ASX row got a NaN price
    # and every downstream number computed from it (MOS/fear/greed/
    # weekly change/Long Score) was garbage - 192/197 ASX 200 rows and
    # 236/241 ASX 300 rows saved that night. Same fix already proven live
    # on deep_dive_engine's per-ticker path: drop NaN closes before
    # indexing, so a bad trailing bar is a missing row, not a poisoned
    # last row. Mirrors deep_dive_engine.analyze()'s _close_series
    # handling exactly, so the two paths agree on the same ticker.
    window_3mo = df.tail(63)
    close_series = window_3mo["Close"].dropna()
    if close_series.empty:
        # No usable close anywhere in the 3-month window - not just a bad
        # trailing bar but no real price data at all. Never fabricate a
        # row from this; the caller (run_universe_scan/run_imported_scan)
        # already treats a None return as "ticker skipped".
        return None
    current_price = float(close_series.iloc[-1])
    high_price = float(close_series.max())
    fear = ((high_price - current_price) / high_price) * 100 if high_price else 0.0
    ma50 = close_series.rolling(50).mean().iloc[-1]
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

    # Services batch 2, Part 1 (2026-09-01): "What the price implies" -
    # reverse DCF, computed here so it reaches the snapshot pages/API/MCP
    # the same way every other snapshot number does (see
    # snapshot_store._PUBLIC_FIELD_MAP and this function's own return dict
    # below). Reuses the SAME discount_rate/perpetual_rate/growth_rate/
    # manual_fcf and info/cashflow_df resolve_intrinsic_value() just used
    # above, and the same current_price, so the implied/model growth
    # figures are always internally consistent with Intrinsic Value/MOS on
    # this exact row - never a second, independently-resolved DCF. Never
    # raises: reverse_dcf_engine.compute() itself returns ok=False (no
    # exception) when there's no positive FCF base, which is the normal,
    # expected outcome for names on the P/E-blend fallback.
    try:
        _reverse_dcf = reverse_dcf_engine.compute(
            ticker, current_price, info=info, cashflow_df=cashflow_df,
            currency=info.get("currency"),
            discount_rate=discount_rate, perpetual_rate=perpetual_rate,
            growth_rate=growth_rate, manual_fcf=manual_fcf,
        )
    except Exception:
        _reverse_dcf = {"ok": False}

    # Same NaN-dropped close_series here too - iloc[-6] on the raw
    # window would count a trailing NaN placeholder bar as one of the
    # "6 trading days back" and could also land on a NaN itself.
    if len(close_series) >= 6 and close_series.iloc[-6] != 0:
        weekly = ((current_price - close_series.iloc[-6])
                  / close_series.iloc[-6]) * 100
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
            # get_trend_score now returns (score, failed) - see
            # trends_engine.py's docstring for the CPRT root-cause writeup
            # this distinction exists for. This standalone/overnight path
            # has no UI to surface a disclosure through (it just writes a
            # ranking via scan_store), so the failed half is intentionally
            # discarded here rather than threaded further - unpacking it is
            # only to keep trend_score itself a plain number, not a tuple.
            trend_score, _ = get_trend_score(keyword)
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
        # Fix 6, AI fixes round 2 (2026-08-31): same source/fallback as
        # deep_dive_engine.analyze()'s own "name" field - `info` is
        # already fetched above for quality/intrinsic resolution, so
        # this is free (no extra network call). Cached in the row so
        # snapshot_store's public surfaces never need to hit Yahoo just
        # to show a company name.
        "Company Name": info.get("longName") or info.get("shortName") or ticker,
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
        # Services batch 2, Part 1: reverse DCF - None/None for any
        # ticker reverse_dcf_engine couldn't build a positive FCF base
        # for (same names Intrinsic Value/MOS are already None for, plus
        # a P/E-blend name, since reverse DCF is a DCF-only calculation).
        "Implied Growth %": (
            round(_reverse_dcf["implied_growth"] * 100, 1)
            if _reverse_dcf.get("ok") and _reverse_dcf.get("implied_growth") is not None else None
        ),
        "Model Growth %": (
            round(_reverse_dcf["model_growth"] * 100, 1)
            if _reverse_dcf.get("ok") and _reverse_dcf.get("model_growth") is not None else None
        ),
        # Services batch 3, Part A1: dividend headline numbers - see this
        # function's own comment above where dividend_ttm/next_ex_date are
        # computed. "Payout Ratio %" is added separately, only for the two
        # real nightly-scan paths, by _attach_dividend_payout() below.
        "Dividend TTM": round(dividend_ttm, 4) if dividend_ttm else None,
        "Dividend Yield %": (
            round(dividend_ttm / current_price * 100, 2)
            if dividend_ttm and current_price else None
        ),
        "Next Ex-Div Date": next_ex_date,
    }


def run_universe_scan(universe, max_tickers=None, log=print):
    """Scan every ticker in `universe` and persist the ranked result via
    scan_store. Returns the saved payload (or None if the universe couldn't
    be resolved). Goes attention-lite only when the resolved universe is
    bigger than NIGHTLY_LITE_THRESHOLD (a real index like ASX 200/S&P 500
    always will be; a hand-run scan of a small custom list won't)."""
    # Services batch 2, Part 2 (2026-09-01): calls get_universe_pool()
    # directly (what resolve_tickers() itself calls internally) instead
    # of resolve_tickers() - same ticker list, same single fetch per
    # universe, but this way the Sector column that pool already carries
    # isn't thrown away. Sector is attached to each row below
    # (_sector_by_ticker) so peer_context.py can group same-sector
    # peers/percentiles purely from the saved scan - see that module's
    # own docstring.
    country = "Australia" if universe in scanner_engine.AUSTRALIA_UNIVERSES else "USA"
    pool_df, source = scanner_engine.get_universe_pool(country, universe)
    if pool_df is None or pool_df.empty:
        log(f"[nightly_scan] {universe}: no tickers resolved ({source})")
        return None
    tickers = sorted(pool_df["Ticker"].dropna().unique().tolist())
    # Sanitised to real strings only (or absent -> .get() gives None) -
    # pool_df["Sector"] can hold pandas NaN for a ticker with no known
    # sector, and a raw NaN surviving onto a row would be truthy in
    # Python (unlike None), silently breaking every "if sector:" gate
    # downstream (peer_context.py, this function's own percentile step).
    _sector_by_ticker = {}
    if "Sector" in pool_df.columns:
        for _tk, _sec in zip(pool_df["Ticker"], pool_df["Sector"]):
            if isinstance(_sec, str) and _sec.strip():
                _sector_by_ticker[_tk] = _sec.strip()
    if not tickers:
        log(f"[nightly_scan] {universe}: no tickers resolved ({source})")
        return None
    if max_tickers:
        tickers = tickers[:max_tickers]
    attention_lite = len(tickers) > NIGHTLY_LITE_THRESHOLD
    log(f"[nightly_scan] {universe}: scanning {len(tickers)} tickers ({source}), "
        f"attention_lite={attention_lite}")

    rows = []
    skipped_no_price = 0
    for i, t in enumerate(tickers):
        try:
            row = analyze_ticker_lite(t, attention_lite=attention_lite)
            if row:
                # Fix 9 item 2 (2026-09-01): hard backstop, on top of item
                # 1's fix inside analyze_ticker_lite() itself - a row can
                # NEVER reach scan_store/score_history with a non-finite,
                # zero, or negative price, no matter how it was built.
                # Should essentially never fire now that
                # analyze_ticker_lite() itself returns None for a ticker
                # with no usable close, but this is the actual guarantee
                # the "never let a NaN reach a public surface" rule needs,
                # not just "the one known code path that used to break it".
                _price = row.get("Price")
                if not (isinstance(_price, (int, float)) and math.isfinite(_price) and _price > 0):
                    skipped_no_price += 1
                    log(f"[nightly_scan] {t}: skipped, no valid price ({_price!r})")
                else:
                    _attach_moat(row, t, log=log)
                    _attach_dividend_payout(row, t, log=log)
                    # Services batch 2, Part 2: Sector, straight from the
                    # constituent pool already fetched above - no extra
                    # call. None (not "Unknown") for a universe whose
                    # source doesn't carry sectors at all, same as
                    # get_universe_pool's own convention.
                    row["Sector"] = _sector_by_ticker.get(t)
                    rows.append(row)
        except Exception as e:  # one bad ticker never kills the run
            log(f"[nightly_scan] {t}: {e}")
        if i % 25 == 24:
            log(f"[nightly_scan] {universe}: {i + 1}/{len(tickers)} done")
        time.sleep(PER_TICKER_SLEEP)

    rows.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)

    # Services batch 2, Part 2: percentile ranks (universe + sector),
    # computed once over this run's whole row population - see
    # peer_context.attach_percentiles()'s own docstring for why this has
    # to be a batch step here rather than something analyze_ticker_lite()
    # can compute per-ticker (it needs every OTHER row's value too).
    try:
        peer_context.attach_percentiles(rows)
    except Exception as e:
        log(f"[nightly_scan] {universe}: attach_percentiles failed: {e}")

    # Audit fix 2.3 / Fix 9 item 3 (2026-09-01): don't let a partially- or
    # badly-failed run silently clobber a complete prior scan (see
    # SCAN_COMPLETENESS_THRESHOLD above). Tightened from "only if this
    # run is worse by row count" to "ANY existing prior scan is
    # protected" - a below-threshold run can still have more raw rows
    # than a smaller-but-genuinely-good prior scan (that's exactly what
    # happened the night this fix was written: a NaN-price run "succeeded"
    # on row count while every one of those rows was garbage), so row
    # count alone was never a safe test for "is this actually better".
    # The sole exception is a universe with no prior scan at all yet -
    # saving a degraded first pass beats leaving it perpetually empty,
    # and it's clearly flagged as degraded either way.
    completeness = (len(rows) / len(tickers)) if tickers else 0.0
    degraded = completeness < SCAN_COMPLETENESS_THRESHOLD
    if degraded:
        prior = scan_store.load_scan(universe)
        prior_rows = len(prior.get("rows") or []) if prior else 0
        if prior_rows > 0:
            log(f"[nightly_scan] {universe}: only {len(rows)}/{len(tickers)} valid rows "
                f"({completeness:.0%}, below the {SCAN_COMPLETENESS_THRESHOLD:.0%} "
                f"completeness threshold) - keeping previous scan ({prior_rows} rows) rather "
                f"than overwriting it with a degraded run; this universe stays stale and will "
                f"be retried on the next scheduler check (age-based staleness check finds "
                f"nothing new here).")
            return None
        log(f"[nightly_scan] {universe}: only {len(rows)}/{len(tickers)} valid rows "
            f"({completeness:.0%}, below threshold) but no prior scan exists yet - saving "
            f"anyway, flagged degraded.")

    payload = scan_store.save_scan(universe, rows, source, attention_lite=attention_lite,
                                    degraded=degraded)
    log(f"[nightly_scan] {universe}: saved {len(rows)} rows, skipped {skipped_no_price} "
        f"(no price)" + (" (degraded)" if degraded else ""))
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
            if row:
                # Fix 9 item 2: same hard price backstop as
                # run_universe_scan() - see that function's comment.
                _price = row.get("Price")
                if not (isinstance(_price, (int, float)) and math.isfinite(_price) and _price > 0):
                    log(f"[nightly_scan] imported {t}: skipped, no valid price ({_price!r})")
                    row = None
                else:
                    _attach_moat(row, t, log=log)
                    _attach_dividend_payout(row, t, log=log)
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


# -----------------------------------------------------------------
# Fix 9 (2026-09-01) item 4: one-off cleanup of data ALREADY SAVED before
# items 1-3 above landed. Not a defence against a future run - the guards
# above already stop that - this is purely undoing the damage from one
# specific known-bad night.
# -----------------------------------------------------------------

# The night of the bad run (Railway logs: 31 Aug 20:00 UTC - 06:00
# Brisbane, before the ASX open, so every ASX ticker's in-progress
# session bar was NaN). score_history's `day` column is date-only (no
# time-of-day), so this is precise enough to scope the delete without
# needing a ticker->universe mapping that table doesn't have - see
# score_history.delete_bad_price_rows()'s own docstring.
FIX9_CLEANUP_DAY = "2026-08-31"

# The two universes confirmed affected (Railway logs + live API): ASX 200
# saved 197 rows/192 NaN, ASX 300 saved 241 rows/236 NaN. S&P 500 (503
# rows, scanned the same run) was fine, since 20:00 UTC is after the NYSE
# close - so this fix is deliberately scoped to just these two, not every
# universe, matching the doc's own evidence.
FIX9_CLEANUP_UNIVERSES = ("ASX 200", "ASX 300")


def _fix9_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".fix9_cleanup_2026_08_31.done")


def cleanup_fix9_nan_data(log=print):
    """One-off, idempotent, marker-file-guarded boot-time cleanup for Fix
    9 (2026-09-01) - removes the bad data that was saved by the 31 Aug
    20:00 UTC run BEFORE analyze_ticker_lite()/run_universe_scan()'s Fix
    9 guards (items 1-3, above) existed to stop it: root cause was a NaN
    placeholder Close bar on every ASX ticker (see analyze_ticker_lite()'s
    own comment) - 192/197 ASX 200 rows and 236/241 ASX 300 rows got a
    NaN price, and every number computed from it (MOS/fear/greed/weekly
    change/Long Score) was garbage, all now live on public surfaces
    (home "Tonight's top 5", the Scanner overnight table, /s/<ticker>,
    /api/v1/*, score_history's "vs 30 days ago" captions).

    Does three things:
      1. score_history: deletes every FIX9_CLEANUP_DAY row with a NaN/
         None price (score_history.delete_bad_price_rows()).
      2. snapshot_store: deletes every stored snapshot in
         FIX9_CLEANUP_UNIVERSES whose cached Price is NaN/None - these
         are what /s/<ticker> and /api/v1/* actually serve, so this is
         the fix for the public-surface part of the bug.
      3. scan_store: invalidates (deletes) the saved scan for each
         universe in FIX9_CLEANUP_UNIVERSES, so the Scanner page stops
         serving the garbage-ranked table and the scheduler's own
         staleness check (scan_store.load_scan() returning None, exactly
         like "no scan ever ran") picks it up for a fresh rescan on its
         next tick - the nightly attempt counter is per calendar day
         (scheduler_engine._run_nightly's `state["scan_attempts"]`, keyed
         by today's date, capped at 3/day) and is unrelated to this
         cleanup, so a rescan today is allowed regardless of when this
         runs.

    Guarded by a marker file on the same volume every other persisted
    file in this app uses (RAILWAY_VOLUME_MOUNT_PATH, falling back to
    this directory locally), so this only ever actually does its work
    once. Unlike blog_store.backfill_primary_tickers() (idempotent by
    construction - it only ever fills an already-blank field, so
    running it every boot forever is free), THIS cleanup deletes rows -
    a second run would correctly find nothing left to delete, but it
    would still pay for a full score_history query plus a full
    ASX 200 + ASX 300 snapshot scan on every single boot forever, for a
    fix that's only ever relevant once. The marker avoids that ongoing
    cost, not a correctness problem.

    Called unconditionally from server.py's lifespan(), wrapped in
    `with suppress(Exception)` there - never allowed to stop the site
    serving, same rule as backfill_primary_tickers()."""
    marker = _fix9_marker_path()
    if os.path.exists(marker):
        return
    removed_history = 0
    removed_snapshots = 0
    invalidated = []
    try:
        removed_history = score_history.delete_bad_price_rows(FIX9_CLEANUP_DAY)
    except Exception as e:
        log(f"[nightly_scan] fix9 cleanup: score_history delete failed: {e}")
    for universe in FIX9_CLEANUP_UNIVERSES:
        try:
            for entry in snapshot_store.all_snapshots(universe=universe):
                ticker = entry.get("ticker")
                if not ticker:
                    continue
                snap = snapshot_store.get_snapshot(ticker)
                if not snap:
                    continue
                price = (snap.get("data") or {}).get("Price")
                bad_price = price is None or (
                    isinstance(price, float) and not math.isfinite(price)
                )
                if bad_price:
                    snapshot_store.delete_snapshot(ticker)
                    removed_snapshots += 1
        except Exception as e:
            log(f"[nightly_scan] fix9 cleanup: {universe} snapshot scan failed: {e}")
        try:
            if scan_store.invalidate(universe):
                invalidated.append(universe)
        except Exception as e:
            log(f"[nightly_scan] fix9 cleanup: {universe} scan invalidate failed: {e}")
    log(f"[nightly_scan] fix9 cleanup: removed {removed_history} score_history row(s) "
        f"for {FIX9_CLEANUP_DAY}, removed {removed_snapshots} bad snapshot(s), "
        f"invalidated scans for: {', '.join(invalidated) if invalidated else 'none'}")
    try:
        with open(marker, "w") as f:
            f.write(f"fix9 cleanup ran {datetime.now(timezone.utc).isoformat()}\n")
    except OSError as e:
        log(f"[nightly_scan] fix9 cleanup: could not write marker file: {e}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "ASX 200"
    if target == IMPORTED_UNIVERSE:
        run_imported_scan()
    else:
        run_universe_scan(target)
