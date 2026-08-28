"""
Technical indicators for swing-trade timing, computed from the price-history
DataFrame with plain pandas (no TA-Lib / no extra dependencies).

Everything here is defensive: short histories, all-NaN columns, and zero
denominators return neutral/None values rather than raising, because the
scan loop calls these on hundreds of tickers and one bad series shouldn't
kill the run.

Exposed:
    rsi(closes, period=14)                 -> float 0-100 (or None)
    macd(closes)                           -> dict {macd, signal, hist, cross}
    moving_average(closes, window)         -> float (or None)
    atr(high, low, close, period=14)       -> float (or None)
    classify_trend(closes)                 -> "UPTREND" / "DOWNTREND" / "RANGING"
    compute_indicators(df)                 -> one dict with all of the above
"""

import pandas as pd


def _clean(series):
    """Coerce to numeric, drop NaNs. Returns a pandas Series."""
    return pd.to_numeric(series, errors="coerce").dropna()


def rsi(closes, period=14):
    """Wilder's RSI. Needs > period data points; returns None if too short.

    Audit fix 1.5: this used a plain rolling SMA of gains/losses, which is
    labeled "Wilder's" in the docstring but isn't the same calculation -
    genuine Wilder's smoothing is a recursive/exponential average
    (equivalent to an EWM with alpha=1/period), which is what every
    charting platform (TradingView, etc.) actually computes. The RSI
    thresholds hard-coded downstream (e.g. in scanner_engine.py) are tuned
    against that standard definition, so the plain-SMA version was
    numerically diverging from what those thresholds assume, not just
    mislabeled. `adjust=False` matches the recursive form; a `min_periods`
    floor keeps the same "needs > period points" behavior as before."""
    c = _clean(closes)
    if len(c) < period + 1:
        return None

    delta = c.diff().dropna()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean().iloc[-1]
    avg_loss = losses.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean().iloc[-1]

    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return None

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def moving_average(closes, window):
    c = _clean(closes)
    if len(c) < window:
        return None
    return round(float(c.rolling(window).mean().iloc[-1]), 4)


def macd(closes, fast=12, slow=26, signal=9):
    """
    Standard MACD (12/26/9). Returns a dict with the macd line, signal line,
    histogram, and a 'cross' flag: 'bullish' if macd just crossed above
    signal, 'bearish' if just below, else 'none'.
    """
    c = _clean(closes)
    if len(c) < slow + signal:
        return {"macd": None, "signal": None, "hist": None, "cross": "none"}

    ema_fast = c.ewm(span=fast, adjust=False).mean()
    ema_slow = c.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line

    cross = "none"
    if len(hist) >= 2:
        prev, curr = hist.iloc[-2], hist.iloc[-1]
        if prev <= 0 < curr:
            cross = "bullish"
        elif prev >= 0 > curr:
            cross = "bearish"

    return {
        "macd": round(float(macd_line.iloc[-1]), 4),
        "signal": round(float(signal_line.iloc[-1]), 4),
        "hist": round(float(hist.iloc[-1]), 4),
        "cross": cross,
    }


def atr(high, low, close, period=14):
    """
    Average True Range - the basis for volatility-scaled stops. Needs OHLC;
    if high/low aren't available (some feeds), returns None and the caller
    should fall back to a percentage stop.

    Audit fix 1.5: same fix as rsi() above - true Wilder's ATR smooths the
    true-range series recursively (EWM, alpha=1/period), not a plain
    rolling SMA; see rsi()'s comment for why that matters here too.
    """
    h = _clean(high)
    l = _clean(low)
    c = _clean(close)
    n = min(len(h), len(l), len(c))
    if n < period + 1:
        return None

    h, l, c = h.iloc[-n:], l.iloc[-n:], c.iloc[-n:]
    prev_close = c.shift(1)

    tr = pd.concat([
        h - l,
        (h - prev_close).abs(),
        (l - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_val = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean().iloc[-1]
    if pd.isna(atr_val):
        return None
    return round(float(atr_val), 4)


def classify_trend(closes):
    """
    UPTREND / DOWNTREND / RANGING from the MA20 vs MA50 stack plus where
    price sits relative to them:
      - price > MA20 > MA50  -> UPTREND
      - price < MA20 < MA50  -> DOWNTREND
      - anything mixed       -> RANGING
    Falls back to RANGING if there isn't enough data for a 50-day MA.
    """
    c = _clean(closes)
    if len(c) < 50:
        return "RANGING"

    price = float(c.iloc[-1])
    ma20 = float(c.rolling(20).mean().iloc[-1])
    ma50 = float(c.rolling(50).mean().iloc[-1])

    if price > ma20 > ma50:
        return "UPTREND"
    if price < ma20 < ma50:
        return "DOWNTREND"
    return "RANGING"


def compute_indicators(df):
    """
    One call that returns every indicator for a ticker's price DataFrame.
    df must have a 'Close' column; 'High'/'Low' are used for ATR if present.
    """
    closes = df["Close"] if "Close" in df else pd.Series(dtype=float)
    high = df["High"] if "High" in df else None
    low = df["Low"] if "Low" in df else None

    atr_val = None
    if high is not None and low is not None:
        atr_val = atr(high, low, closes)

    macd_data = macd(closes)

    return {
        "rsi": rsi(closes),
        "macd": macd_data["macd"],
        "macd_signal": macd_data["signal"],
        "macd_cross": macd_data["cross"],
        "ma20": moving_average(closes, 20),
        "ma50": moving_average(closes, 50),
        "atr": atr_val,
        "trend": classify_trend(closes),
    }
