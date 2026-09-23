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

import admin_metrics_store
import alert_engine
import auto_compounder_engine
import fundamentals_data
import moat_engine
import peer_context
import reverse_dcf_engine
import scan_store
import score_history
import scanner_engine
import screen_import_store
import sector_cache_store
import snapshot_store
import source_health_store
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

# Part 48.2(c): the nightly sector-cache top-up is deliberately small and
# rate-limited - it exists to slowly self-heal Small Ords/All Ords
# coverage over many nights, not to backfill everything in one pass (that
# would mean tens of extra Yahoo Finance calls beyond what the scan
# itself already makes, every single night, forever). See
# run_sector_topup()'s own docstring.
SECTOR_TOPUP_PER_NIGHT = 40

# Discovery drop-and-reweight fix, Part 2 (18 Sep 2026): after an
# attention_lite universe's scan is saved, the table's actual LEADERS -
# top ATTENTION_TOPUP_PER_UNIVERSE rows by the Part-1 reweighted Long
# Score, the ones a visitor actually sees at the top of the Scanner
# table or the home page's "Tonight's top 5" - get the same three real
# attention calls a full-mode scan would have made (see
# run_attention_topup() below). Env-var configurable, same default
# order of magnitude as SECTOR_TOPUP_PER_NIGHT above (~4 lite universes
# x 40 tickers is comparable nightly cost to the sector top-up).
ATTENTION_TOPUP_PER_UNIVERSE = int(os.environ.get("ATTENTION_TOPUP_PER_UNIVERSE", "40"))

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
    # Commit J (21 Sep 2026, owner-reported): a delisted/halted/merged
    # ticker still clears every check above cleanly - yfinance keeps
    # returning rows, just the SAME row forever (QUB.AX/Qube, taken
    # over: an exact 5.11 close every day from 2026-08-20 to
    # 2026-09-17; LSF.AX/L1 Long Short Fund, merged into L1G.AX:
    # unchanged across its last several scans too - both found live in
    # a 2026-09-20 DB backup, still ranking - LSF at #12 in the ASX 200
    # scanner table with mos_pct 95.2). Computed here, once, off the
    # window this function already fetched - no second yfinance call -
    # via scanner_engine.window_shows_no_trading()'s evidence rule
    # (>=2 distinct closes, or any real volume, over the last 5 rows;
    # fewer than 5 rows is inconclusive, never guessed at). Every
    # ranking/valuation surface downstream is expected to skip a
    # flagged row - see this row's own "Trading Status" field below.
    stale_price = scanner_engine.window_shows_no_trading(window_3mo)
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

    # Discovery drop-and-reweight fix, Part 1 (18 Sep 2026): a lite
    # scan's discovery is price/volume only (see this function's own
    # docstring) - not a genuine "no attention" reading, so its weight
    # is dropped and redistributed rather than scored as a near-zero -
    # see calculate_long_score's discovery_measured docstring in
    # ranking_engine.py for the exact mechanics/fixture check.
    long_score = calculate_long_score(quality, mos, psychology, discovery,
                                       discovery_measured=not attention_lite)

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
        # Commit J: "stale" when window_shows_no_trading() found no
        # evidence of real trading in the last 5 rows above - None
        # (not "trading"/"active" - see this dict's own convention of
        # None for "not applicable") otherwise. See snapshot_store.
        # _PUBLIC_FIELD_MAP for how this reaches every public surface.
        "Trading Status": "stale" if stale_price else None,
    }


def refresh_market_cap_ranking(log=print):
    """Commit D (20 Sep 2026): the nightly-only entry point for
    scanner_engine._rebuild_market_cap_ranking() - the market-cap
    ranking fetch_asx300()/fetch_allords() both slice their tail from.
    Called once, up front, from scheduler_engine._run_nightly() before
    the per-universe scan loop starts, so both AU universes that depend
    on it see this run's freshly (or incrementally) priced ranking
    rather than a stale in-process cache. A web request never triggers
    this - see scanner_engine._asx_non200_by_marketcap()'s own
    docstring for the read-only path every visitor actually goes
    through."""
    try:
        scanner_engine._rebuild_market_cap_ranking(log=log)
    except Exception as e:
        log(f"[nightly_scan] market-cap ranking refresh failed: {e}")


def run_universe_scan(universe, max_tickers=None, log=print, run_night=None):
    """Scan every ticker in `universe` and persist the ranked result via
    scan_store. Returns the saved payload (or None if the universe couldn't
    be resolved). Goes attention-lite only when the resolved universe is
    bigger than NIGHTLY_LITE_THRESHOLD (a real index like ASX 200/S&P 500
    always will be; a hand-run scan of a small custom list won't).

    `run_night` (Commit H, 20 Sep 2026): the scheduled scan night this
    call belongs to, threaded straight through to scan_store.save_scan()
    and admin_metrics_store.bump_scan_calendar() - see save_scan()'s own
    docstring for exactly why this exists and what it fixes. None (the
    default) for a hand-run scan with no scheduler context; scheduler_
    engine._run_nightly() always passes it."""
    # Services batch 2, Part 2 (2026-09-01): calls get_universe_pool()
    # directly (what resolve_tickers() itself calls internally) instead
    # of resolve_tickers() - same ticker list, same single fetch per
    # universe, but this way the Sector column that pool already carries
    # isn't thrown away. Sector is attached to each row below
    # (_sector_by_ticker) so peer_context.py can group same-sector
    # peers/percentiles purely from the saved scan - see that module's
    # own docstring.
    # 34.8 (Part 34 addendum, 11 Sep 2026): elapsed-time logging so the
    # real nightly cost is visible in Railway logs instead of guessed -
    # see the final log line below for the "N tickers in Xm Ys" format.
    _scan_start = time.time()
    country = "Australia" if universe in scanner_engine.AUSTRALIA_UNIVERSES else "USA"
    # Index containment regression guard (20 Sep 2026): "All Ordinaries"
    # is the top of the AU nesting chain (ASX 20 subset ... subset ASX 300
    # subset All Ordinaries - see scanner_engine.py's own comment above
    # _AU_CONTAINMENT_CHAIN), so its scan is the natural point at which the
    # whole chain has just been exercised for the night. Fail-open by
    # design (verify_au_index_containment() never raises) - a violation
    # only logs a warning here, it never blocks this or any other scan.
    if universe == "All Ordinaries":
        scanner_engine.verify_au_index_containment(log=log)
    pool_df, source = scanner_engine.get_universe_pool(country, universe)
    # Commit L (21 Sep 2026, owner-reported): sector-universe filter
    # health, tracked here - nightly-only, never on a web request, since
    # get_universe_pool() itself has no business writing health state on
    # every page view (same reasoning every other health-tracked fetcher
    # in scanner_engine.py already follows) - so a silently-empty match
    # (the exact failure that let XRO.AX, a software company, get saved
    # under "ASX A-REITs") shows up on the Admin Dashboard's Source
    # health panel, not just a log line. get_universe_pool()'s own
    # sector-universe branches (scanner_engine.py) never fall back to an
    # unfiltered pool any more - see that function's own comment - so
    # pool_df here is either a genuinely sector-filtered frame or None;
    # there is no third, silently-wrong case left to catch.
    _is_sector_universe = (
        universe in scanner_engine._ASX_SECTOR_UNIVERSE_MAP
        or universe in scanner_engine._US_SECTOR_UNIVERSE_MAP
    )
    if _is_sector_universe:
        _sector_health_source = f"Sector universe: {universe}"
        if pool_df is not None and not pool_df.empty:
            source_health_store.record_success(
                _sector_health_source, [],
                {"filter_match": {"ok": True, "detail": f"{len(pool_df)} row(s) matched this sector"}},
            )
        else:
            _reason = source or "sector filter failed"
            _prior = source_health_store.get(_sector_health_source)
            _was_already_stale = bool(_prior and _prior.get("stale"))
            source_health_store.record_failure(
                _sector_health_source, {"filter_match": {"ok": False, "detail": _reason}}, _reason,
            )
            if not _was_already_stale:
                try:
                    alert_engine.send_source_health_alert(
                        _sector_health_source, {"filter_match": {"ok": False, "detail": _reason}}, _reason,
                    )
                except Exception as e:
                    log(f"[nightly_scan] source-health alert send failed for {_sector_health_source}: {e}")
    if pool_df is None or pool_df.empty:
        if _is_sector_universe:
            # `source` is already shaped "sector filter matched N row(s)
            # - skipped, serving last known-good scan" (or "<parent
            # index> itself unavailable - ...") by get_universe_pool()
            # itself - read that directly rather than re-deriving the
            # count, so this log line can never disagree with the reason
            # actually recorded above.
            log(f"[nightly_scan] {universe}: {source}")
        else:
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

    # Part 48.2(a): bulk-learn every row's sector into the persistent
    # cache (source="scan") right after this universe's rows are built -
    # a plain local SQLite write on data already in hand, no extra fetch.
    # This is what lets a sector-less universe (Small Ords/All Ords -
    # see sector_cache_store.py's own docstring) benefit from a ticker
    # that ALSO happens to appear in a sector-carrying universe (ASX 200/
    # S&P 500) on some other night - one write-through here, and every
    # later sector_for_ticker() call for that ticker resolves from cache,
    # in any universe, with no rescan needed. A row with no Sector
    # (universe doesn't carry one) is simply skipped - learn() itself
    # already no-ops on a blank/None sector, but checking here avoids a
    # pointless DB round-trip for the common no-sector case.
    for _r in rows:
        _sec = _r.get("Sector")
        if _sec:
            sector_cache_store.learn(_r.get("Ticker"), _sec, source="scan")

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
                                    degraded=degraded, run_night=run_night)
    # Part 53.1: one tiny marker for the Admin Dashboard's weekly scan
    # calendar - a full scan was just SAVED for this universe tonight.
    # Commit H: credited to `run_night` (the night this run was scheduled
    # for), not whatever calendar day it happens to be when this line
    # executes - a universe queued after several smaller ones can finish
    # past 00:00 UTC, and the OLD day-at-call-time behavior would then
    # mark it on the wrong day, exactly the bug the admin calendar was
    # showing for every weekday-pinned universe. Wrapped in its own
    # try/except, same must-never-break-the-scan convention as every
    # other counting call site in this function (score_history.record
    # below) - see bump_scan_calendar()'s own docstring for why this is
    # a metrics write, not a second table.
    try:
        admin_metrics_store.bump_scan_calendar(universe, "scan", day=run_night)
    except Exception as e:
        log(f"[nightly_scan] {universe}: scan-calendar record failed: {e}")
    _scan_elapsed = time.time() - _scan_start
    _mins, _secs = divmod(int(_scan_elapsed), 60)
    log(f"[nightly_scan] {universe}: saved {len(rows)} rows, skipped {skipped_no_price} "
        f"(no price)" + (" (degraded)" if degraded else "") +
        f" - {len(tickers)} tickers in {_mins}m {_secs}s")
    try:
        score_history.record(rows)
        log(f"[nightly_scan] {universe}: recorded {len(rows)} rows to score_history")
    except Exception as e:  # history logging must never fail the scan itself
        log(f"[nightly_scan] {universe}: score_history.record failed: {e}")
    return payload


def run_sector_topup(tickers, log=print):
    """Part 48.2(c): the sector cache's self-healing top-up. Takes the
    subset of `tickers` (meant to be every ticker scanned across every
    universe tonight - scheduler_engine._run_nightly collects this) that
    sector_cache_store has no row for yet, caps it at
    SECTOR_TOPUP_PER_NIGHT, and fetches ONLY each one's yfinance .info
    sector field - the exact same company-info route analyze_ticker_lite()
    above already uses for every ticker's fundamentals (`tk.info`), just
    without the price-history/cashflow calls that route also makes, since
    a sector top-up needs none of that. Same PER_TICKER_SLEEP pacing as
    the main scan loop above (this module's own docstring: "one ticker at
    a time with a small sleep").

    Deliberately small and slow by design: this exists to gradually close
    the Small Ords/All Ords coverage gap over many nights (a ticker that
    fails or gets skipped tonight simply reappears in unknown_among() on
    a future night, cheap and automatic), not to force it shut in one
    run - that would mean up to hundreds of extra Yahoo Finance calls in
    a single night on top of what the scan itself already makes, exactly
    the kind of nightly-cost blowout this module's own docstring already
    warns about for the Trends/News/Social calls.

    Any single ticker's failure (network hiccup, no 'sector' key,
    delisted ticker, etc.) is skipped silently and never retried
    same-night - see sector_cache_store.learn()'s own no-op-on-blank
    guarantee. Returns the count actually learned, for the caller's own
    one-line log."""
    candidates = sector_cache_store.unknown_among(tickers, limit=SECTOR_TOPUP_PER_NIGHT)
    if not candidates:
        log("[nightly_scan] sector top-up: nothing to do (every scanned ticker "
            "already has a cached sector, or none were passed in)")
        return 0
    learned = 0
    for t in candidates:
        try:
            info = yf.Ticker(t).info or {}
            sector = info.get("sector")
            if isinstance(sector, str) and sector.strip():
                sector_cache_store.learn(t, sector, source="topup")
                learned += 1
        except Exception:
            pass  # skip silently - see docstring; comes up again another night
        time.sleep(PER_TICKER_SLEEP)
    log(f"[nightly_scan] sector top-up: learned {learned}/{len(candidates)} new "
        f"sector(s) ({len(tickers)} ticker(s) considered, cap {SECTOR_TOPUP_PER_NIGHT})")
    return learned


def run_attention_topup(universe, log=print):
    """Discovery drop-and-reweight fix, Part 2 (18 Sep 2026). Only
    meaningful for a universe whose CURRENTLY SAVED scan is
    attention_lite=True - a full-mode universe already had every row's
    Discovery genuinely measured by run_universe_scan()/
    analyze_ticker_lite() itself, nothing to top up here.

    Takes that scan's top ATTENTION_TOPUP_PER_UNIVERSE rows by Long
    Score (the Part-1 reweighted score - the rows a visitor actually
    sees leading the Scanner table or the home page's "Tonight's top
    5"), and fetches the same three real attention signals
    analyze_ticker_lite()'s own `if not attention_lite:` branch makes -
    same functions, same formula, same api_key=None env fallback - for
    each one. Folds them into that row's stored price/volume-only
    "Discovery (lite)" number exactly like a full-mode scan would have
    computed it in the first place, recomputes Long Score via
    calculate_long_score(..., discovery_measured=True) - a genuinely
    MEASURED Discovery now, not the lite placeholder Part 1 had to
    drop-and-reweight around - and marks the row "attention_full": True.

    Each of the three signal calls is independently try/excepted, same
    as analyze_ticker_lite - one failing (or genuinely finding nothing,
    e.g. no recent news) still lets the other two count, same as a real
    full-mode scan. Only when ALL THREE fail outright is the row left
    untouched (still Part 1's reweighted score) - a fetch failure is not
    a measured zero, the same principle this whole fix exists to
    enforce for Discovery in general; that ticker simply comes up again
    next time it leads.

    Persists via scan_store.apply_attention_topup (carries every other
    payload field over unchanged - "generated_at" must never move for
    this, it's a partial re-score of tonight's rows, not a new full
    scan) and recomputes percentiles for the WHOLE universe afterward
    (peer_context.attach_percentiles needs the full, current population -
    a topped-up row's new Long Score shifts where every OTHER row in the
    universe sits percentile-wise too, not just its own). Deliberately
    called strictly before scheduler_engine._build_derived_universes()
    in the post-scan sequence, so a derived universe (ASX 100/Small
    Ords/Russell 3000/etc.) and the home page's "Tonight's top 5" both
    read the topped-up rows straight off disk, not the pre-topup ones -
    see that function's docstring for how it re-reads scan_store fresh
    per parent. Returns the count of rows actually updated, for the
    caller's log line - same shape as run_sector_topup's own return
    above."""
    payload = scan_store.load_scan_raw(universe)
    if not payload or not payload.get("rows"):
        log(f"[nightly_scan] {universe}: attention top-up skipped, no saved scan")
        return 0
    if not payload.get("attention_lite"):
        log(f"[nightly_scan] {universe}: attention top-up skipped, already full-mode")
        return 0

    rows = payload["rows"]
    leaders = sorted(rows, key=lambda r: r.get("Long Score") or 0, reverse=True)
    leaders = leaders[:ATTENTION_TOPUP_PER_UNIVERSE]

    updated = 0
    for row in leaders:
        ticker = row.get("Ticker")
        if not ticker:
            continue
        keyword = ticker.split(".")[0]
        trend_score, trend_ok = 0, False
        try:
            trend_score, _ = get_trend_score(keyword)
            trend_ok = True
        except Exception:
            pass
        news_score, news_ok = 0, False
        try:
            news_score = get_news_score(keyword) + get_yahoo_news_score(ticker)
            news_ok = True
        except Exception:
            pass
        social_score, social_ok = 0, False
        try:
            social_score, _social_detail = social_engine.get_social_score(ticker)
            social_ok = True
        except Exception:
            pass

        if not (trend_ok or news_ok or social_ok):
            # All three failed outright - a fetch failure, not a
            # measured zero (failed != measured-zero). Leave Part 1's
            # reweighted score untouched.
            time.sleep(PER_TICKER_SLEEP)
            continue

        full_discovery = (row.get("Discovery (lite)") or 0) + trend_score + news_score + social_score
        mos_for_calc = row.get("MOS %")
        if mos_for_calc is None:
            mos_for_calc = 0.0
        long_score = calculate_long_score(
            row.get("Quality"), mos_for_calc, row.get("Psychology"),
            full_discovery, discovery_measured=True,
        )
        row["Discovery (lite)"] = round(full_discovery, 1)
        row["Long Score"] = round(long_score, 1)
        row["attention_full"] = True
        updated += 1
        time.sleep(PER_TICKER_SLEEP)

    if updated:
        try:
            peer_context.attach_percentiles(rows)
        except Exception as e:
            log(f"[nightly_scan] {universe}: attention top-up attach_percentiles failed: {e}")
        rows.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)
        scan_store.apply_attention_topup(universe, rows, topped_up_count=updated)

    log(f"[nightly_scan] {universe}: attention top-up measured {updated}/{len(leaders)} "
        f"leader(s) (cap {ATTENTION_TOPUP_PER_UNIVERSE})")
    return updated


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


# ---------------------------------------------------------------------
# Part 34 addendum 34.7 (11 Sep 2026): nightly reprice pass.
#
# A weekly- (or rotation-) cadence universe must not show week-old
# prices between its full scans - price sits inside MOS and the value
# score, so staleness there compounds. scheduler_engine._run_nightly()
# calls reprice_universe() below, once per real (non-derived) universe
# that did NOT get a full scan tonight, AFTER the night's full scans and
# before _build_derived_universes() rebuilds the sector/union universes
# from the freshly-repriced parents.
# ---------------------------------------------------------------------

REPRICE_CHUNK_SIZE = 175  # tickers per yf.download() batch call - "chunks
# of ~150-200 tickers per request" per the addendum; NEVER per-ticker -
# see reprice_universe() below, which is the only loop in this whole
# pass that touches the network, and it always calls yf.download() on a
# whole chunk at once (same multi-ticker download mechanism scanner_
# engine._heat_download_batch() already uses elsewhere in this app).
REPRICE_CHUNK_SLEEP = 2.0  # seconds between chunks - Yahoo etiquette for
# the one bulk request per chunk (this pass can touch 2000+ tickers in a
# night for a universe like Russell 2000, so a small pause per chunk,
# not per ticker, keeps that polite without materially slowing the run).


def _reprice_row(row, hist_df, universe_attention_lite=True):
    """Recomputes ONLY the price-dependent outputs of one already-scanned
    row, given its freshly batch-downloaded 6-month OHLCV history
    `hist_df` (one ticker's slice from the chunked yf.download() call in
    reprice_universe() below) - through the EXACT SAME formulas
    analyze_ticker_lite() uses for these same fields above. Deliberately
    duplicated here rather than factored into a shared helper both
    functions call: analyze_ticker_lite() is also used standalone by
    digest_engine's weekly watchlist email and must not change shape for
    this batch-oriented caller.

    Returns a NEW dict (the input `row` is never mutated) with Price/
    MOS %/Valuation/Psychology/Discovery (lite)/Long Score/Signal/
    Dividend Yield % updated, or None if `hist_df` has no usable close -
    the caller then keeps `row` exactly as it was (the addendum's "a
    ticker that fails in the batch keeps its previous values" rule).

    Deliberately does NOT touch: Quality, Quality Default, Intrinsic
    Value, Intrinsic Default, Type, Company Name, Moat/Moat Erosion/Moat
    Mode, Payout Ratio %, Dividend TTM, Next Ex-Div Date, Trend, Trade
    Setup, Implied Growth %, Model Growth %. The addendum's own line is
    "recompute ONLY the price-dependent outputs... price, MOS..,
    valuation label, psychology.., discovery.., and the value score" -
    that word "ONLY" is read here as an exhaustive list, not an
    illustrative one, so Trend/Trade Setup (technical-indicator fields)
    and the reverse-DCF growth figures are price-dependent too but stay
    untouched by this pass, refreshing on that universe's own next full
    scan. Dividend Yield % IS recomputed even though it isn't itself
    named in that list - it's a direct, same-row arithmetic function of
    Price (Dividend TTM / Price) and the instruction explicitly puts
    Price in scope, so leaving it stale would make the row internally
    inconsistent with its own new Price. Both calls are flagged in the
    Part 34 report as the interpretive judgment this pass makes.

    Second addendum fix (18 Sep 2026): the ORIGINAL version of this
    function always overwrote Discovery with the price/volume-only
    formula and re-blended Long Score with discovery_measured defaulting
    to True - silently WRONG on the (previously undocumented) assumption
    that "this pass only ever runs against universes that were
    attention_lite the last time they were scanned." A catch-up run (the
    scheduler recovering a missed night by scanning only a subset of
    universes, e.g. just Russell 1000/2000) breaks that assumption: every
    OTHER real universe - including full-attention ones like Dow 30/
    Nasdaq 100/ASX All Tech - reads as "not scanned tonight" and gets
    repriced too, which used to blow away their genuinely-measured
    Discovery and understate their Long Score exactly like the bug Part
    1/2 fixed for lite scans, just via a different code path. Fixed by
    only ever recomputing the price/volume PART of Discovery here
    (`fresh_pv` below - this pass has no cheap way to re-fetch Trends/
    News/StockTwits for a repriced ticker) and, for a row this function
    determines is full-attention, preserving whatever attention remainder
    the stored Discovery carried on top of that. `universe_attention_lite`
    is reprice_universe()'s caller-supplied payload-level flag (the
    scan's own attention_lite - preserved unchanged across reprices, see
    scan_store.reprice_scan()) for exactly this purpose."""
    if hist_df is None or hist_df.empty or "Close" not in hist_df.columns:
        return None
    window_3mo = hist_df.tail(63)
    close_series = window_3mo["Close"].dropna()
    if close_series.empty:
        return None
    current_price = float(close_series.iloc[-1])
    if not math.isfinite(current_price) or current_price <= 0:
        return None
    high_price = float(close_series.max())
    fear = ((high_price - current_price) / high_price) * 100 if high_price else 0.0
    ma50 = close_series.rolling(50).mean().iloc[-1]
    if pd.isna(ma50) or ma50 == 0:
        ma50 = current_price
    greed = max(((current_price - ma50) / ma50) * 100, 0)

    intrinsic = row.get("Intrinsic Value")
    mos = ((intrinsic - current_price) / intrinsic) * 100 if intrinsic and intrinsic > 0 else None

    if len(close_series) >= 6 and close_series.iloc[-6] != 0:
        weekly = ((current_price - close_series.iloc[-6]) / close_series.iloc[-6]) * 100
    else:
        weekly = 0.0
    fomo = max(greed + max(weekly, 0), 0)
    psychology = fear - greed - fomo

    activity = abs(weekly)
    avg_vol = window_3mo["Volume"].mean() if "Volume" in window_3mo.columns else 0
    vol_ratio = (window_3mo["Volume"].iloc[-1] / avg_vol) if avg_vol and avg_vol > 0 else 0
    # The price/volume-only PART of Discovery - genuinely all this pass
    # can recompute (see the second addendum note in this function's
    # docstring above for why this used to be treated as the WHOLE of
    # Discovery, unconditionally, and why that was wrong for a
    # full-attention row caught up in a catch-up-shaped reprice run).
    fresh_pv = activity + vol_ratio * 10

    # Full-attention determination: Part 2's own per-row top-up marker
    # first, then the payload-level "this universe's scan wasn't
    # attention_lite" flag, then - only if neither marker is available,
    # e.g. legacy stored data from before this fix - a heuristic: if the
    # stored Discovery is bigger than price/volume alone would produce,
    # that surplus can only have come from real attention signals.
    stored_discovery = row.get("Discovery (lite)")
    row_full_attention = bool(row.get("attention_full")) or not universe_attention_lite
    if not row_full_attention and stored_discovery is not None:
        row_full_attention = (stored_discovery - fresh_pv) > 0

    if row_full_attention:
        # Preserve the attention remainder on top of the freshly
        # recomputed price/volume part - never negative, a fresh_pv that
        # now exceeds the old stored value just means price/volume moved,
        # not that attention shrank.
        remainder = max(0.0, (stored_discovery - fresh_pv)) if stored_discovery is not None else 0.0
        discovery = fresh_pv + remainder
        discovery_measured = True
    else:
        discovery = fresh_pv
        discovery_measured = False

    quality = row.get("Quality") or 0
    long_score = calculate_long_score(quality, mos if mos is not None else 0.0, psychology, discovery,
                                       discovery_measured=discovery_measured)

    if not intrinsic or intrinsic <= 0:
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

    new_row = dict(row)
    new_row["Price"] = round(current_price, 2)
    new_row["MOS %"] = round(mos, 1) if mos is not None else None
    new_row["Psychology"] = round(psychology, 1)
    new_row["Discovery (lite)"] = round(discovery, 1)
    new_row["Long Score"] = round(long_score, 1)
    new_row["Valuation"] = valuation
    new_row["Signal"] = signal
    _div_ttm = row.get("Dividend TTM")
    if _div_ttm:
        new_row["Dividend Yield %"] = round(_div_ttm / current_price * 100, 2) if current_price else None
    return new_row


def _reprice_download_chunk(tickers):
    """One yf.download() call for a chunk of tickers (~150-200 - see
    REPRICE_CHUNK_SIZE), returning {ticker: DataFrame} - the SAME batch-
    download shape scanner_engine._heat_download_batch() already uses
    elsewhere in this app for a multi-ticker history pull, including its
    single-vs-multi-ticker column handling (yf.download returns a flat
    frame for a 1-ticker request, a ticker-keyed MultiIndex for more than
    one). Never raises - a chunk that fails outright just yields no
    history for any ticker in it, which reprice_universe() below treats
    exactly like any other per-ticker failure (row kept as-is)."""
    out = {}
    try:
        data = yf.download(
            list(tickers), period="6mo", progress=False,
            group_by="ticker", threads=True, auto_adjust=True,
        )
    except Exception:
        return out
    if data is None or len(data) == 0:
        return out
    single = len(tickers) == 1
    for t in tickers:
        try:
            out[t] = data if single else data[t]
        except Exception:
            pass
    return out


def reprice_universe(universe, log=print, run_night=None):
    """Part 34 addendum 34.7: refreshes ONE universe's stored scan rows
    in place with tonight's prices, via chunked batch downloads (never
    per-ticker loops) - see _reprice_row()'s own docstring for exactly
    which fields this does and doesn't touch, and REPRICE_CHUNK_SIZE's
    for the batching itself. Only ever called (from scheduler_engine.
    _run_nightly()) for a universe that has an EXISTING stored scan but
    did NOT get a full scan tonight. No-op (returns None) if there's no
    prior scan on disk at all - a universe with nothing scanned yet has
    nothing for this pass to reprice; it gets its first real content
    from its own full-scan cadence instead.

    `run_night` (Commit H, 20 Sep 2026): the scheduled scan night,
    passed straight to the admin-calendar marker below - same reasoning
    as run_universe_scan()'s own `run_night`, just for the "reprice"
    marker instead of "scan".

    Processes one chunk's downloaded history at a time (never holds every
    universe's full history in memory at once - 34.8's memory guard) and
    logs elapsed time + repriced/kept-stale counts (34.8's duration-
    logging guard) in the same "[[nightly_scan] ...: N tickers in Xm Ys"
    style the rest of this module already uses."""
    start = time.time()
    existing = scan_store.load_scan_raw(universe)
    if existing is None:
        log(f"[nightly_scan] reprice {universe}: no prior scan on disk, skipping")
        return None

    rows_by_ticker = {}
    ordered_tickers = []
    for row in existing.get("rows") or []:
        t = (row.get("Ticker") or "").strip()
        if t and t not in rows_by_ticker:
            rows_by_ticker[t] = row
            ordered_tickers.append(t)

    # Second addendum fix (18 Sep 2026): the scan's own attention_lite
    # flag, carried over unchanged across reprices (scan_store.
    # reprice_scan()) - _reprice_row() uses this to tell a genuinely
    # lite universe apart from a full-attention one caught up in this
    # reprice pass by a catch-up-shaped run.
    universe_attention_lite = existing.get("attention_lite", True)

    repriced_count = 0
    kept_stale_count = 0
    new_rows = []
    n_chunks = 0
    for i in range(0, len(ordered_tickers), REPRICE_CHUNK_SIZE):
        chunk = ordered_tickers[i:i + REPRICE_CHUNK_SIZE]
        n_chunks += 1
        try:
            hist_by_ticker = _reprice_download_chunk(chunk)
        except Exception as e:
            log(f"[nightly_scan] reprice {universe}: chunk {n_chunks} download failed: {e}")
            hist_by_ticker = {}
        for t in chunk:
            row = rows_by_ticker[t]
            try:
                new_row = _reprice_row(row, hist_by_ticker.get(t), universe_attention_lite)
            except Exception as e:
                log(f"[nightly_scan] reprice {universe} {t}: {e}")
                new_row = None
            if new_row is not None:
                new_rows.append(new_row)
                repriced_count += 1
            else:
                new_rows.append(row)  # kept exactly as-is - never blanked
                kept_stale_count += 1
        if i + REPRICE_CHUNK_SIZE < len(ordered_tickers):
            time.sleep(REPRICE_CHUNK_SLEEP)

    new_rows.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)
    payload = scan_store.reprice_scan(
        universe, new_rows, repriced_count=repriced_count, kept_stale_count=kept_stale_count)
    # Part 53.1: same weekly-scan-calendar marker as run_universe_scan()
    # above, but "reprice" - this universe wasn't fully rescanned tonight,
    # just refreshed in place. Only recorded once reprice_scan() actually
    # returned a payload (the `existing is None` early-return above never
    # reaches here, so this only fires on a genuine reprice).
    if payload:
        try:
            admin_metrics_store.bump_scan_calendar(universe, "reprice", day=run_night)
        except Exception as e:
            log(f"[nightly_scan] reprice {universe}: scan-calendar record failed: {e}")
    elapsed = time.time() - start
    mins, secs = divmod(int(elapsed), 60)
    log(f"[nightly_scan] reprice {universe}: {len(ordered_tickers)} tickers in "
        f"{n_chunks} chunk(s) of <={REPRICE_CHUNK_SIZE}, {repriced_count} repriced, "
        f"{kept_stale_count} kept-stale, in {mins}m {secs}s")

    # Rebuild the public /s/<TICKER> snapshot for this universe from its
    # freshly-repriced rows, same as a full scan does in scheduler_
    # engine._run_nightly() - otherwise the snapshot/API surfaces would
    # keep showing this universe's pre-reprice prices indefinitely.
    if payload and payload.get("rows"):
        try:
            import snapshot_store
            snapshot_store.build_snapshots_from_scan(universe, payload["rows"], log=log)
        except Exception as e:
            log(f"[nightly_scan] reprice {universe}: snapshot rebuild failed: {e}")

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


def _commit_l_cleanup_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".commitL_sector_pollution_cleanup.done")


def cleanup_sector_universe_pollution(log=print):
    """One-off, idempotent, marker-file-guarded boot-time cleanup for
    Commit L (21 Sep 2026, owner-reported) - same pattern as cleanup_
    fix9_nan_data() above, for a different root cause: discards any
    saved scan for one of the 12 sector universes (scanner_engine.
    _ASX_SECTOR_UNIVERSE_MAP/_US_SECTOR_UNIVERSE_MAP) whose OWN rows
    don't actually belong to that sector - the exact shape of pollution
    get_universe_pool()'s OLD empty-sector-filter fallback could
    produce (the whole unfiltered ~300/500-company parent pool, saved
    under a sector universe's own name) - this is how XRO.AX, a
    software company, ended up saved under "ASX A-REITs". See get_
    universe_pool()'s own sector-universe branches (scanner_engine.py)
    for the fix that stops this happening again; this function is the
    one-off cleanup for whatever ALREADY got saved before that fix
    existed.

    A scan counts as polluted when FEWER THAN HALF its own saved rows'
    "Sector" field is actually in that universe's own expected sector
    set (scanner_engine._ASX_SECTOR_UNIVERSE_MAP[universe]/_US_SECTOR_
    UNIVERSE_MAP[universe]) - a real, correctly-filtered sector scan
    has ~100% of its rows match by construction; the old bug's own
    failure mode (an entire unfiltered parent pool saved as-is) would
    only have the genuinely-in-sector minority matching by chance,
    nowhere near half - not a threshold chosen to be clever, just wide
    enough either side of "obviously polluted" vs "obviously fine" that
    it can't misfire on ordinary data.

    Invalidates (deletes) a polluted scan outright via scan_store.
    invalidate() - the scheduler's own "missing file = needs rescan"
    logic (scan_store.load_scan() returning None) picks it up for a
    fresh rescan on its next tick, the same mechanism cleanup_fix9_nan_
    data() above already relies on - so a visitor sees "no data yet"
    instead of the wrong data (Xero filed as a REIT) until a real,
    correctly-filtered scan lands, rather than silently continuing to
    serve pollution until its own 72h staleness cutoff happens to
    expire on its own.

    Owner-requested (21 Sep 2026): logs one line PER UNIVERSE, every
    run - rows checked, rows matched, and the outcome (kept / no saved
    scan / INVALIDATED) - not just the trailing one-line summary, so
    exactly what this did is readable straight from the Railway logs
    without reading the code.

    Guarded by a marker file, same convention as cleanup_fix9_nan_data()
    above - this only ever needs to run once against whatever's already
    on disk; every scan saved AFTER this deploy already goes through
    the fixed get_universe_pool(), so a second run would correctly find
    nothing left to clean but would still pay for 12 scan_store reads
    on every boot forever without the marker.

    Called unconditionally from server.py's lifespan(), wrapped in
    `with suppress(Exception)` there - never allowed to stop the site
    serving, same rule as cleanup_fix9_nan_data()."""
    marker = _commit_l_cleanup_marker_path()
    if os.path.exists(marker):
        return
    checked = []
    invalidated = []
    sector_universes = dict(scanner_engine._ASX_SECTOR_UNIVERSE_MAP)
    sector_universes.update(scanner_engine._US_SECTOR_UNIVERSE_MAP)
    for universe, expected_sectors in sector_universes.items():
        checked.append(universe)
        try:
            payload = scan_store.load_scan_raw(universe)
            if not payload or not payload.get("rows"):
                # Owner-requested (21 Sep 2026): a per-universe log line
                # every run, not just when something gets invalidated -
                # so tomorrow's Railway logs show exactly what this
                # checked, not just a one-line summary.
                log(f"[nightly_scan] commitL cleanup: {universe}: no saved scan on file - nothing to check")
                continue
            rows = payload["rows"]
            matching = sum(1 for r in rows if r.get("Sector") in expected_sectors)
            if matching < len(rows) / 2:
                was_invalidated = scan_store.invalidate(universe)
                if was_invalidated:
                    invalidated.append(f"{universe} ({matching}/{len(rows)} rows matched)")
                log(f"[nightly_scan] commitL cleanup: {universe}: checked {len(rows)} row(s), "
                    f"{matching} matched expected sector(s) {expected_sectors} - "
                    f"{'INVALIDATED' if was_invalidated else 'below threshold but nothing on disk to invalidate'}")
            else:
                log(f"[nightly_scan] commitL cleanup: {universe}: checked {len(rows)} row(s), "
                    f"{matching} matched expected sector(s) {expected_sectors} - kept, not polluted")
        except Exception as e:
            log(f"[nightly_scan] commitL cleanup: {universe} check failed: {e}")
    log(f"[nightly_scan] commitL cleanup: checked {len(checked)} sector universe(s), "
        f"invalidated: {', '.join(invalidated) if invalidated else 'none'}")
    try:
        with open(marker, "w") as f:
            f.write(f"commitL sector-pollution cleanup ran {datetime.now(timezone.utc).isoformat()}\n")
    except OSError as e:
        log(f"[nightly_scan] commitL cleanup: could not write marker file: {e}")


def _ebit_switch_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".ebit_from_pretax_switch_state")


def check_ebit_switch_flip(log=print):
    """Commit O (21 Sep 2026, owner-verified EBIT-from-pretax fix): the
    EBIT_FROM_PRETAX switch (auto_compounder_engine.EBIT_FROM_PRETAX) is
    a Railway env var, not a code change - flipping it does NOT bump
    auto_compounder_engine.ENGINE_VERSION or moat_engine.MOAT_ENGINE_
    VERSION (those bumped once, in the same deploy this function shipped
    in, to invalidate whatever was cached under the OLD formula; they
    have no way to know the switch itself later flips a second time on a
    running deploy). Without this check, flipping the env var on Railway
    would sit invisible behind each ticker's existing 24h moat_cache/
    auto_cv_sections entry for up to 24h - "flip it on, wait up to a day
    to see it" is exactly the "not a day later" the task this shipped
    under ruled out.

    Unlike cleanup_fix9_nan_data()/cleanup_sector_universe_pollution()
    above, this is NOT a marker-file "ran once, never again" guard - the
    marker here stores the LAST-OBSERVED switch state ("0"/"1"), compared
    against the live env var on every call. Different -> the switch
    genuinely flipped since the last check -> both caches this formula
    change affects (moat_cache, auto_cv_sections) are wiped outright
    (every *.json file in each directory removed, exactly like scan_
    store.invalidate() removes a stale scan - "no cached entry" is a
    state every reader already handles, a *wrong-formula* cached entry
    is not). Same -> no-op. No marker file yet at all (first boot after
    this shipped, or a fresh volume) -> just records the current state,
    doesn't wipe anything - a brand-new deploy's caches were already
    invalidated by the version bumps above, there's nothing stale here to
    correct on day one.

    A Railway env var change triggers a redeploy (a fresh boot), and this
    is called unconditionally from server.py's lifespan() alongside the
    other marker-guarded cleanups above - so a flip takes effect on that
    same boot, before the next nightly run even starts, not "the next
    time nightly_scan.py happens to run". Never allowed to stop the site
    serving, same rule as every other lifespan cleanup."""
    marker = _ebit_switch_marker_path()
    current = "1" if auto_compounder_engine.EBIT_FROM_PRETAX else "0"
    previous = None
    try:
        if os.path.exists(marker):
            with open(marker) as f:
                previous = f.read().strip()
    except OSError as e:
        log(f"[nightly_scan] ebit switch check: could not read marker file: {e}")

    if previous is not None and previous == current:
        return
    if previous is None:
        log(f"[nightly_scan] ebit switch check: no prior state on record - "
            f"recording EBIT_FROM_PRETAX={current}, nothing to invalidate on a fresh deploy")
    else:
        log(f"[nightly_scan] ebit switch check: EBIT_FROM_PRETAX flipped "
            f"{previous} -> {current} - clearing moat_cache and auto_cv_sections "
            f"so the change takes effect immediately, not after their 24h TTL")
        for cache_dir in (moat_engine._cache_dir(), os.path.join(auto_compounder_engine._data_dir(), auto_compounder_engine._CACHE_DIR_NAME)):
            cleared = 0
            try:
                for fname in os.listdir(cache_dir):
                    if fname.endswith(".json"):
                        try:
                            os.remove(os.path.join(cache_dir, fname))
                            cleared += 1
                        except OSError:
                            pass
            except OSError as e:
                log(f"[nightly_scan] ebit switch check: could not list {cache_dir}: {e}")
                continue
            log(f"[nightly_scan] ebit switch check: cleared {cleared} cached file(s) from {cache_dir}")

        # Only a genuine OFF -> ON flip needs alert suppression and a
        # score_history data-correction tag - see is_ebit_correction_
        # pending()/score_history.record()'s own comments. Flipping back
        # OFF restores the old numbers (also a real jump, also worth not
        # alerting on) but the task this shipped under only asked for
        # this on the ON transition, so that's the one case handled here.
        if previous == "0" and current == "1":
            try:
                with open(_ebit_correction_marker_path(), "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
                log("[nightly_scan] ebit switch check: flagged the next nightly run as a "
                    "data correction - its score_history rows will be tagged, and "
                    "alert_engine.send_batched_notifications() will log (not send) whatever "
                    "would have fired that night")
            except OSError as e:
                log(f"[nightly_scan] ebit switch check: could not write correction marker: {e}")

    try:
        with open(marker, "w") as f:
            f.write(current)
    except OSError as e:
        log(f"[nightly_scan] ebit switch check: could not write marker file: {e}")


def _ebit_correction_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".ebit_from_pretax_pending_correction")


def is_ebit_correction_pending():
    """True for every call made during the one nightly run right after
    EBIT_FROM_PRETAX flips OFF -> ON (see check_ebit_switch_flip()) -
    read by score_history.record() (tags that night's rows) and
    alert_engine.send_batched_notifications() (suppresses the real send,
    logs what would have fired instead). Stays True across that whole
    nightly job (every universe scan's record() call, the extra alert
    pass, and the final batched send all see the same answer) until
    consume_ebit_correction_marker() clears it at the very end of that
    job - so exactly one nightly run is affected, never a second one."""
    return os.path.exists(_ebit_correction_marker_path())


def consume_ebit_correction_marker(log=print):
    """Deletes the pending-correction marker, ending the suppression
    window - called once, by alert_engine.send_batched_notifications(),
    as the last step of the nightly job that was flagged. A no-op if
    nothing is pending (every other night)."""
    marker = _ebit_correction_marker_path()
    if not os.path.exists(marker):
        return
    try:
        os.remove(marker)
        log("[nightly_scan] ebit switch check: data-correction night complete - marker cleared")
    except OSError as e:
        log(f"[nightly_scan] ebit switch check: could not clear correction marker: {e}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "ASX 200"
    if target == IMPORTED_UNIVERSE:
        run_imported_scan()
    else:
        run_universe_scan(target)
