"""
Swing-trading specific calculations, kept separate from the long-term
investment engines so the two modes don't entangle.

Contents:
    calculate_trader_score(...)        weights technicals/timing over value
    position_size(...)                 shares from account size + risk %
    atr_stop(...)                      volatility-scaled stop (entry - k*ATR)
    trend_alignment_score(trend)       0-100 from trend direction
    rsi_timing_score(rsi)              0-100, rewards oversold-not-overbought
    get_market_regime(index_df)        risk-on / risk-off from index vs its MA50
    earnings_warning(info, hold_days)  flags earnings inside the holding window
"""

import pandas as pd


def rsi_timing_score(rsi_value):
    """
    Map RSI to a 0-100 timing score for swing ENTRIES. We want oversold-to-
    neutral (room to run), not overbought (late). Peak reward around 35-45,
    tapering to 0 as it approaches overbought.
    """
    if rsi_value is None:
        return 50  # neutral if unknown
    if rsi_value < 30:
        return 90          # deeply oversold - strong bounce candidate
    if rsi_value < 45:
        return 100         # the sweet spot for an entry
    if rsi_value < 55:
        return 70          # neutral, still fine
    if rsi_value < 70:
        return 40          # getting extended
    return 10              # overbought - poor entry timing


def trend_alignment_score(trend):
    """Uptrend is what a swing-long wants; ranging is neutral; downtrend bad."""
    return {"UPTREND": 100, "RANGING": 50, "DOWNTREND": 10}.get(trend, 50)


def macd_score(macd_cross, macd_hist):
    """Reward a fresh bullish cross / positive histogram."""
    if macd_cross == "bullish":
        return 100
    if macd_cross == "bearish":
        return 10
    if macd_hist is not None:
        return 65 if macd_hist > 0 else 35
    return 50


def calculate_trader_score(
    psychology_score,
    discovery_score,
    rsi_value,
    trend,
    macd_cross,
    macd_hist,
    quality_score,
    margin_of_safety,
):
    """
    Swing-trade score: timing and momentum dominate, fundamentals are a
    light sanity check. Weights (sum to 100%):

        Technicals (RSI + MACD + trend)  ~60%
            RSI timing        25%
            MACD momentum     15%
            Trend alignment   20%
        Psychology (timing)   ~20%
        Discovery (attention) ~10%
        Quality (sanity)      ~5%
        Margin of Safety      ~5%

    Psychology and Discovery are clamped into 0-100-ish ranges first so they
    don't blow out the weighting (they can otherwise be unbounded/negative).
    """
    rsi_s = rsi_timing_score(rsi_value)
    macd_s = macd_score(macd_cross, macd_hist)
    trend_s = trend_alignment_score(trend)

    # Psychology can be negative; map to 0-100 where fearful (positive) is
    # good for a contrarian entry. Clamp to keep the weighting sane.
    psych_s = max(0, min(100, 50 + psychology_score))
    # Discovery is unbounded positive attention; cap at 100.
    disc_s = max(0, min(100, discovery_score))
    qual_s = max(0, min(100, quality_score))
    mos_s = max(0, min(100, margin_of_safety))

    score = (
        rsi_s * 0.25
        + macd_s * 0.15
        + trend_s * 0.20
        + psych_s * 0.20
        + disc_s * 0.10
        + qual_s * 0.05
        + mos_s * 0.05
    )
    return round(score, 2)


def pullback_score(price, ma20, atr_value, trend):
    """
    Reward price sitting NEAR the 20-day average (a healthy pullback / ride in
    an uptrend), penalise price stretched far above it (chasing). Distance is
    measured in ATR units so volatile names get proportionate room.
    Returns 0-100.
    """
    if not ma20 or ma20 <= 0 or not atr_value or atr_value <= 0:
        return 50
    atr_dist = abs(price - ma20) / atr_value
    if atr_dist <= 1.0:
        return 100          # right at the MA20 - clean entry
    if atr_dist <= 2.0:
        return 70
    if atr_dist <= 3.0:
        return 45
    return 20               # extended - poor entry timing


def swing_setup(
    current_price,
    ma20,
    ma50,
    atr_value,
    rsi_value,
    trend,
    trader_score,
    macd_cross,
    macd_hist,
    regime,
    resistance20=None,
    support20=None,
    atr_mult=2.0,
    rr_target1=2.0,
    rr_target2=3.0,
    trader_min=55.0,
    buy_cutoff=65.0,
    watch_cutoff=45.0,
):
    """
    Swing-specific BUY / WATCHLIST / AVOID - everything on ONE coherent ATR
    basis, and GRADED rather than an all-or-nothing gate.

    Entry  = current price (where you'd actually get in on a swing).
    Stop   = entry - atr_mult x ATR  (volatility-scaled; falls back to a % stop).
    Targets= fixed R-multiples: T1 = entry + rr_target1 x risk,
             T2 = entry + rr_target2 x risk. (Reward is defined RELATIVE to the
             risk you're taking - the standard swing approach - so RR is honest
             and the decision rests on setup QUALITY, not on how close a stock
             happens to sit to a resistance line.)

    Setup Score (0-100) blends: trend alignment (25), RSI timing (20),
    MACD (15), pullback proximity to MA20 (20), market regime (10),
    Trader Score sanity (10).

    Verdict:
        - DOWNTREND, or risk <= 0, or Trader Score < trader_min -> AVOID
        - RSI overbought (>70)                                  -> at most WATCHLIST
        - else BUY if score >= buy_cutoff, WATCHLIST if >= watch_cutoff, else AVOID

    Returns a dict with entry/stop/targets/risk/rr and the graded verdict.
    """
    entry = current_price
    stop = atr_stop(entry, atr_value, multiplier=atr_mult, fallback_support=support20)
    risk = entry - stop

    target1 = round(entry + rr_target1 * risk, 2) if risk > 0 else None
    target2 = round(entry + rr_target2 * risk, 2) if risk > 0 else None

    # --- graded setup score ---
    trend_s = trend_alignment_score(trend)
    rsi_s = rsi_timing_score(rsi_value)
    macd_s = macd_score(macd_cross, macd_hist)
    prox_s = pullback_score(current_price, ma20, atr_value, trend)
    regime_s = {"RISK-ON": 100, "RISK-OFF": 30}.get(regime, 60)
    trader_s = max(0, min(100, trader_score))

    setup_score = round(
        trend_s * 0.25
        + rsi_s * 0.20
        + macd_s * 0.15
        + prox_s * 0.20
        + regime_s * 0.10
        + trader_s * 0.10,
        2
    )

    # --- verdict ---
    overbought = (rsi_value is not None and rsi_value > 70)

    if trend == "DOWNTREND" or risk <= 0 or trader_score < trader_min:
        signal = "AVOID"
    elif setup_score >= buy_cutoff and not overbought:
        signal = "BUY"
    elif setup_score >= watch_cutoff:
        signal = "WATCHLIST"
    else:
        signal = "AVOID"

    # If it would otherwise BUY but price is overbought, hold at WATCHLIST
    # (wait for a pullback rather than chase).
    if overbought and signal == "BUY":
        signal = "WATCHLIST"

    return {
        "signal": signal,
        "setup_score": setup_score,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "risk": round(risk, 2) if risk > 0 else None,
        "target1": target1,
        "target2": target2,
        "rr1": rr_target1 if risk > 0 else None,
        "rr2": rr_target2 if risk > 0 else None,
        "resistance_ref": round(resistance20, 2) if resistance20 else None,
        "overbought": overbought,
    }


def atr_stop(entry_price, atr_value, multiplier=2.0, fallback_support=None):
    """
    Volatility-scaled stop: entry - (multiplier x ATR). If ATR is unavailable,
    fall back to the support-based stop (support x 0.97) the trade filter
    already used, or a flat 3% if that's also missing.
    """
    if atr_value is not None and atr_value > 0:
        return round(entry_price - multiplier * atr_value, 2)
    if fallback_support is not None and fallback_support > 0:
        return round(fallback_support * 0.97, 2)
    return round(entry_price * 0.97, 2)


def position_size(account_size, risk_pct, entry_price, stop_price):
    """
    Shares to buy so that being stopped out loses exactly risk_pct of the
    account. Returns a dict with share count and dollar risk.

    shares = (account_size x risk_pct/100) / (entry - stop)

    Guards: entry must be above stop and positive; otherwise 0 shares.
    """
    risk_per_share = entry_price - stop_price
    # NaN != NaN - a NaN entry or stop (bad feed data) must yield 0 shares,
    # not crash the int() below with "cannot convert float NaN to integer".
    if (risk_per_share != risk_per_share or entry_price != entry_price
            or risk_per_share <= 0 or entry_price <= 0 or account_size <= 0):
        return {"shares": 0, "dollar_risk": 0.0, "position_value": 0.0}

    dollar_risk_budget = account_size * (risk_pct / 100.0)
    shares = int(dollar_risk_budget // risk_per_share)

    return {
        "shares": shares,
        "dollar_risk": round(shares * risk_per_share, 2),
        "position_value": round(shares * entry_price, 2),
    }


def get_market_regime(index_df):
    """
    Risk-on if the index is above its own 50-day MA, risk-off below.
    index_df: a price-history DataFrame for the benchmark (ASX 200 / S&P 500).
    Returns (regime_str, detail_str).
    """
    if index_df is None or index_df.empty or "Close" not in index_df:
        return "UNKNOWN", "Benchmark data unavailable"

    closes = pd.to_numeric(index_df["Close"], errors="coerce").dropna()
    if len(closes) < 50:
        return "UNKNOWN", "Not enough benchmark history"

    price = float(closes.iloc[-1])
    ma50 = float(closes.rolling(50).mean().iloc[-1])

    if price >= ma50:
        return "RISK-ON", "Benchmark above its 50-day average"
    return "RISK-OFF", "Benchmark below its 50-day average"


def earnings_warning(info, hold_days=42):
    """
    Flag if the next earnings date falls within the holding window (default
    ~6 weeks, the top of the swing range). Holding through earnings is a
    coin-flip that can gap through a stop.

    yfinance exposes earnings timing inconsistently; we try a couple of
    fields and fail safe to "no warning, unknown date" rather than crashing.
    Returns (warn_bool, days_until_or_None).
    """
    import datetime

    ts = None
    # Newer yfinance: info may carry 'earningsTimestamp' (unix seconds)
    for key in ("earningsTimestamp", "earningsTimestampStart"):
        val = info.get(key)
        if val:
            ts = val
            break

    if not ts:
        return False, None

    try:
        earnings_date = datetime.datetime.fromtimestamp(ts).date()
        days_until = (earnings_date - datetime.date.today()).days
    except Exception:
        return False, None

    if days_until < 0:
        return False, None  # already passed

    return (days_until <= hold_days), days_until
