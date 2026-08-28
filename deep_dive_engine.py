"""
deep_dive_engine  -  single-ticker analysis for the "Stock Deep Dive" tab.

Reuses the EXACT SAME resolver/scoring functions as the main scan
(resolver_engine, ranking_engine.calculate_long_score, indicators_engine,
trade_filter_engine) so a ticker analyzed here always agrees with what the
Stock Scanner / Stock Comparison tabs would compute for it - same DCF
model, same Long Score formula, same trade filter, nothing bespoke.

One deliberate difference from the main scan: this tab always shows a
single BASE-CASE DCF (fully auto - CAPM discount, analyst-consensus-or-
history growth, currency-based terminal growth) - no bear/bull scenarios
and no per-ticker override plumbing, since this tab is a fresh, always-live
look at one name rather than part of a saved scan.

The Trade Setup section uses trade_filter_engine.evaluate_trade() - the
SAME long-term entry/stop/target model the app's Trade Filter table uses
(entry near support/MA50, stop = Support20 x 0.97, targets = Resistance20/
60[/60x1.10]) - deliberately NOT the separate Swing-mode ATR setup, since
this tab is about the long-term investment picture. evaluate_trade() itself
only returns a verdict (BUY/WATCHLIST/AVOID) plus the gate booleans that
drove it, not a continuous score, so trade_setup_contributions below just
WEIGHTS those SAME already-computed gate booleans into a 0-100 chart - it
never re-derives or re-implements the gate logic itself (only the trend/
psychology/discount/MA50 gate checks are mirrored, straight from this
module's own docstring, since evaluate_trade() doesn't return each one
individually - only the combined `passes_gate`).

The Quality breakdown is the one true formula mirror: auto_quality_engine.
get_quality_score() doesn't expose its individual weighted terms, so
_quality_breakdown() below mirrors that formula for DISPLAY only (the
actual quality_score used everywhere, including this tab's own gauge,
still comes from resolve_quality_score()). If that formula ever changes,
this mirror needs updating too - see the comment on _quality_breakdown().
"""

import pandas as pd

from resolver_engine import resolve_quality_score, resolve_intrinsic_value, resolve_stock_type
from ranking_engine import calculate_long_score, MOS_CLAMP, PSY_CLAMP, DISCOVERY_CAP
from trends_engine import get_trend_score
from news_engine import get_news_score, get_yahoo_news_score
import social_engine
import indicators_engine
import trade_filter_engine


def _quality_breakdown(info):
    """
    Mirrors auto_quality_engine.get_quality_score()'s formula (base 50 +
    weighted ROE/margin/growth terms +/- FCF +/- debt penalty) so the
    "what's driving Quality" chart can show the individual terms - that
    function itself only returns the final (score, defaulted) pair, not the
    breakdown. Deliberately NOT capped by the profitability gate here (the
    gate is a display-time note on the caller's side); this only recomputes
    the additive terms that sum to the PRE-gate score.
    """
    GROWTH_CLAMP = 0.50

    roe = info.get("returnOnEquity", 0) or 0
    profit_margin = info.get("profitMargins", 0) or 0
    revenue_growth = info.get("revenueGrowth", 0) or 0
    earnings_growth = info.get("earningsGrowth", 0) or 0
    debt_to_equity = info.get("debtToEquity", 0) or 0
    free_cash_flow = info.get("freeCashflow", 0) or 0

    revenue_growth = max(-GROWTH_CLAMP, min(revenue_growth, GROWTH_CLAMP))
    earnings_growth = max(-GROWTH_CLAMP, min(earnings_growth, GROWTH_CLAMP))

    # ROIC derived the same way auto_quality_engine does it: book equity =
    # marketCap / priceToBook, ROIC ~= net income / (equity + debt).
    net_income = info.get("netIncomeToCommon", 0) or 0
    market_cap = info.get("marketCap", 0) or 0
    price_to_book = info.get("priceToBook", 0) or 0
    total_debt = info.get("totalDebt", 0) or 0
    roic = 0.0
    if market_cap > 0 and price_to_book > 0:
        book_equity = market_cap / price_to_book
        invested_capital = book_equity + total_debt
        if invested_capital > 0:
            roic = max(-1.0, min(net_income / invested_capital, 1.0))

    return {
        "Base": 50.0,
        "ROIC": round(roic * 100 * 0.25, 2),
        "ROE": round(roe * 100 * 0.20, 2),
        "Profit Margin": round(profit_margin * 100 * 0.15, 2),
        "Revenue Growth": round(revenue_growth * 100 * 0.15, 2),
        "Earnings Growth": round(earnings_growth * 100 * 0.15, 2),
        "Free Cash Flow": 10.0 if free_cash_flow > 0 else (-5.0 if free_cash_flow < 0 else 0.0),
        "Debt Penalty": -round(min(debt_to_equity * 0.05, 15), 2),
    }


def analyze(ticker, get_price_history, get_ticker_info, get_cashflow_df,
            news_api_key=None, live_data=True, enable_social=True,
            discount_rate=None, perpetual_rate=None, growth_rate=None,
            manual_fcf=None):
    """
    Run a full single-ticker analysis and return a dict of everything the
    Deep Dive tab needs to render (metrics + chart data), or
    {"error": "..."} (with every other key None/0) if the ticker couldn't be
    analyzed at all (bad symbol, no price history).

    get_price_history / get_ticker_info / get_cashflow_df are passed in as
    callables (rather than imported directly) so this module reuses the
    SAME st.cache_data-wrapped fetchers app.py already defines - one fetch
    per ticker per session, not a second uncached copy of the same calls.

    discount_rate/perpetual_rate/growth_rate/manual_fcf: audit fix (5.1) -
    passed straight through to resolve_intrinsic_value(). Before this,
    this page's own headline Intrinsic Value/Margin of Safety gauges were
    hardcoded to fully-auto regardless of the viewer's Valuation & FCF
    Inputs settings, while the Compounder View "Rational Compounder
    Analysis (Auto)" section further down this SAME page for the SAME
    ticker already respected them (see app.py's _dcf_overrides_for()) -
    two different intrinsic values on one page with no explanation. Leave
    all four at None for the original fully-auto behaviour (used by the
    landing-page featured-card caller, which has no per-user settings to
    apply); a caller with access to the viewer's settings should pass
    _dcf_overrides_for(ticker)'s result through, the same pattern already
    used at the Compounder View/Scanner/Portfolio call sites.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"error": "Enter a ticker symbol."}

    df = get_price_history(ticker)
    if df is None or df.empty:
        return {"error": f"No price history found for '{ticker}' - check the ticker symbol "
                          f"(e.g. CSL.AX for the ASX, AAPL for the US)."}

    info = get_ticker_info(ticker)

    # Fear/Greed/Activity/Volume Ratio/MA50 - same 3-month-window formulas
    # as the main scan (see app.py's per-ticker loop for the identical
    # logic). This same ma50 also feeds the Trade Filter below, exactly as
    # it does in the main scan.
    window_3mo = df.tail(63)
    _close_series = window_3mo["Close"].dropna()
    if _close_series.empty:
        return {"error": f"No usable price data for {ticker}."}
    current_price = float(_close_series.iloc[-1])
    high_price = float(_close_series.max())
    fear_score = ((high_price - current_price) / high_price) * 100 if high_price else 0

    ma50_raw = window_3mo["Close"].rolling(50).mean().iloc[-1]
    # Real gap this masked: when there isn't 50 valid trading days in the
    # window (sparse history, a long trading halt, a recent listing), the
    # rolling average comes back NaN and this silently falls back to
    # ma50 = current_price - which forces Greed to EXACTLY 0 (price can
    # never differ from itself) whether or not the stock's real price is
    # actually above its real 50-day average. That's indistinguishable in
    # the UI from a genuine "price is at/below its 50-day average" zero,
    # unless the caller is told which case happened - see ma50_defaulted
    # in the returned dict below.
    ma50_defaulted = bool(pd.isna(ma50_raw) or ma50_raw == 0)
    ma50 = current_price if ma50_defaulted else ma50_raw
    greed_score = max(((current_price - ma50) / ma50) * 100, 0)

    keyword = ticker.split(".")[0]
    if live_data:
        trend_score = get_trend_score(keyword, api_key=news_api_key or None)
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

    # --- Fundamentals / DCF (respects the viewer's own settings - see
    # discount_rate/perpetual_rate/growth_rate/manual_fcf in this
    # function's own docstring; all None = fully auto, unchanged from
    # before) --------------------
    quality_score, quality_src, quality_default = resolve_quality_score(ticker, info=info)

    cashflow_df = get_cashflow_df(ticker)
    intrinsic_value, intrinsic_src, dcf_growth, iv_meta = resolve_intrinsic_value(
        ticker, quality_score, info=info, cashflow_df=cashflow_df,
        currency=info.get("currency"),
        discount_rate=discount_rate, perpetual_rate=perpetual_rate,
        growth_rate=growth_rate, manual_fcf=manual_fcf,
    )

    stock_type, stock_type_src, type_default = resolve_stock_type(ticker, info=info)

    if intrinsic_value > 0:
        margin_of_safety = ((intrinsic_value - current_price) / intrinsic_value) * 100
    else:
        margin_of_safety = 0

    if len(_close_series) >= 6 and _close_series.iloc[-6] != 0:
        weekly_change = ((current_price - _close_series.iloc[-6]) / _close_series.iloc[-6]) * 100
    else:
        weekly_change = 0

    fomo_score = max(greed_score + max(weekly_change, 0), 0)
    psychology_score = fear_score - greed_score - fomo_score
    activity_score = abs(weekly_change)

    avg_volume = window_3mo["Volume"].mean()
    _vol_series = window_3mo["Volume"].dropna()
    latest_volume = float(_vol_series.iloc[-1]) if len(_vol_series) else 0.0
    volume_ratio = (latest_volume / avg_volume) if avg_volume > 0 else 0

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

    if intrinsic_value <= 0:
        valuation = "N/A"
    elif margin_of_safety >= 25:
        valuation = "UNDERVALUED"
    elif margin_of_safety < 0:
        valuation = "EXPENSIVE"
    else:
        valuation = "FAIR"

    # Same clamps ranking_engine applies internally, surfaced here so the
    # chart can show exactly what each factor CONTRIBUTED (in points) to the
    # final Long Score - not just its raw, differently-scaled value.
    mos_c = max(-MOS_CLAMP, min(margin_of_safety, MOS_CLAMP))
    psy_c = max(-PSY_CLAMP, min(psychology_score, PSY_CLAMP))
    disc_c = max(0.0, min(discovery_score, DISCOVERY_CAP))

    # --- Quality breakdown (display-only mirror - see module docstring) -----
    if quality_src == "manual":
        quality_components = None
    else:
        quality_components = _quality_breakdown(info)

    if quality_score >= 80:
        quality_label = "EXCELLENT"
    elif quality_score >= 60:
        quality_label = "GOOD"
    elif quality_score >= 40:
        quality_label = "FAIR"
    else:
        quality_label = "WEAK"

    # --- Psychology: same sentiment bands app.py's main scan uses -----------
    if psychology_score > 20:
        psychology_sentiment = "FEARFUL"
    elif psychology_score > 5:
        psychology_sentiment = "CALM"
    elif psychology_score < -20:
        psychology_sentiment = "OVERHEATED"
    elif psychology_score < -5:
        psychology_sentiment = "GREEDY"
    else:
        psychology_sentiment = "NEUTRAL"

    # 0-100 gauge value (50 + clamped score).
    psychology_gauge = round(50 + psy_c, 2)
    # Signed so the chart shows how each term actually combines
    # (fear - greed - fomo = psychology_score, exactly).
    psychology_contributions = {
        "Fear": round(fear_score, 2),
        "Greed": round(-greed_score, 2),
        "FOMO": round(-fomo_score, 2),
    }

    # --- Discovery: 0-100 gauge (clamped) + labelled band --------------------
    if disc_c >= 75:
        discovery_label = "HOT"
    elif disc_c >= 50:
        discovery_label = "ACTIVE"
    elif disc_c >= 25:
        discovery_label = "BUILDING"
    else:
        discovery_label = "QUIET"

    discovery_contributions = {
        "Activity": round(activity_score, 2),
        "Volume Ratio x10": round(volume_ratio * 10, 2),
        "Trend": round(trend_score, 2),
        "News": round(news_score, 2),
        "Social": round(social_score, 2),
    }

    # --- Trade Setup (Trade Filter Engine - entry / stop / targets) ---------
    sr = trade_filter_engine.calc_support_resistance(df["Close"])
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

    # Trade Setup Score (0-100) - shared with the main scan's "Trade Setup"
    # table (app.py) via trade_filter_engine.score_trade_setup(), so both
    # places compute this identically instead of maintaining two copies of
    # the same weighting.
    trade_setup_score, trade_setup_contributions = trade_filter_engine.score_trade_setup(
        trade_result, psychology_score, discovery_score, ma50, current_price,
    )

    return {
        "error": None,
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName") or ticker,
        "currency": info.get("currency") or "-",
        "price": round(current_price, 2),

        "intrinsic_value": round(intrinsic_value, 2) if intrinsic_value > 0 else None,
        "intrinsic_source": intrinsic_src,
        "mos": round(margin_of_safety, 2) if intrinsic_value > 0 else None,
        "valuation": valuation,
        "dcf_growth": round(dcf_growth * 100, 1) if dcf_growth is not None else None,
        "dcf_discount": (
            round(iv_meta.get("discount_rate_used") * 100, 1)
            if iv_meta.get("discount_rate_used") is not None else None
        ),
        "dcf_perpetual": (
            round(iv_meta.get("perpetual_rate_used") * 100, 1)
            if iv_meta.get("perpetual_rate_used") is not None else None
        ),
        "growth_governor": iv_meta.get("growth_governor") or "-",
        "value_default": bool(iv_meta.get("value_default")),
        # Task 10: pure passthrough of resolver_engine's own provenance
        # flag (itself a passthrough of fcf_valuation_engine's) - no scoring
        # logic added or changed here, purely so the Deep Dive page can flag
        # it on screen next to Intrinsic Value.
        "dcf_base_normalized": bool(iv_meta.get("fcf_base_normalized")),
        "dcf_base_raw": iv_meta.get("fcf_base_raw"),
        "dcf_base_used": iv_meta.get("fcf_base_used"),
        # DCF fixes: same pure passthrough, no scoring logic touched - see
        # resolver_engine.py's identical comment on these same keys.
        "dcf_discount_floored": bool(iv_meta.get("discount_floored")),
        "dcf_fx_converted": iv_meta.get("fx_converted"),
        "dcf_fx_rate_used": iv_meta.get("fx_rate_used"),
        "dcf_fx_fallback": bool(iv_meta.get("fx_fallback")),

        "quality_score": quality_score,
        "quality_default": bool(quality_default),
        "quality_label": quality_label,
        "quality_components": quality_components,
        "stock_type": stock_type,

        "fear": round(fear_score, 2),
        "greed": round(greed_score, 2),
        "ma50": round(ma50, 2),
        "ma50_defaulted": ma50_defaulted,
        "fomo": round(fomo_score, 2),
        "psychology": round(psychology_score, 2),
        "psychology_sentiment": psychology_sentiment,
        "psychology_gauge": psychology_gauge,
        "psychology_contributions": psychology_contributions,

        "activity": round(activity_score, 2),
        "volume_ratio": round(volume_ratio, 2),
        "trend_score": round(trend_score, 2),
        "news_score": round(news_score, 2),
        "social_score": round(social_score, 2),
        "discovery": round(discovery_score, 2),
        "discovery_gauge": round(disc_c, 2),
        "discovery_label": discovery_label,
        "discovery_contributions": discovery_contributions,

        "long_score": round(long_score, 2),

        # Points each factor actually contributed to the final Long Score
        # (clamped value x its weight) - the four bars sum to long_score.
        "contributions": {
            "Quality": round(quality_score * 0.35, 2),
            "Valuation (MOS)": round(mos_c * 0.25, 2),
            "Psychology": round(psy_c * 0.20, 2),
            "Discovery": round(disc_c * 0.20, 2),
        },

        "trade_setup_signal": trade_result["signal"],
        "trade_setup_score": trade_setup_score,
        "trade_setup_contributions": trade_setup_contributions,
        # "entry_zone" = max(Support20, MA50) - the discounted buy-zone price
        # the Trade Filter engine's own gate ("near_entry_zone") checks price
        # against, and the same field the main scan's "Trade Setup (Top 10)"
        # table's "Entry Zone" column uses. Deliberately NOT entry_result's
        # "entry_price", which trade_filter_engine.py sets to today's current
        # price (used only internally for the Risk/RR math) - showing that
        # here under an "Entry" label was misleading, since it made the bar
        # chart look like it always sat right on today's price even when
        # "Near Entry Zone" was failing.
        "trade_setup_entry": trade_result["entry_zone"],
        "trade_setup_current_price": trade_result["entry_price"],
        "trade_setup_stop": trade_result["stop_loss"],
        "trade_setup_target1": trade_result["target1"],
        "trade_setup_target2": trade_result["target2"],
        "trade_setup_target3": trade_result["target3"],
        "trade_setup_rr1": trade_result["rr1"],
        "trade_setup_rr2": trade_result["rr2"],
        "trade_setup_rr3": trade_result["rr3"],
        "trade_setup_risk": trade_result["risk"],
        "trade_setup_near_entry": trade_result["near_entry_zone"],
        "trade_setup_passes_gate": trade_result["passes_gate"],
    }
