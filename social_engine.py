"""
Social buzz / sentiment - a SEPARATE signal from the price-derived Psychology
engine. Psychology asks "what is price action saying about fear/greed?";
this asks "how much, and how bullishly, is the crowd talking about this
stock right now?". The two can diverge (price-fearful but socially-hyped),
which is itself informative.

Default provider: StockTwits. It's free, finance-specific, and its messages
carry self-applied Bullish/Bearish tags - better signal than raw social
noise. The public endpoint used here:
    https://api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json
returns up to ~30 recent messages per call, each optionally tagged with
sentiment.

DESIGN NOTE - pluggable providers: get_social_score() takes a `provider`
argument. Only "stocktwits" is implemented (free). An "x" / Twitter adapter
is stubbed: X shut off free API access in 2023, so it requires a paid key
and is intentionally left as a NotImplemented placeholder rather than a
broken scraper. If you obtain an X API bearer token, implement fetch_x()
and it slots straight in.

Everything fails safe: any network / parse error returns a neutral zero
score rather than raising, because the scan loop calls this per-ticker.

StockTwits also rate-limits (roughly 200 requests/hour unauthenticated), so
in the app this is behind the same opt-in "Enable Trends & News" style flag
and cached.
"""

import requests

STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockScannerBot/1.0)"}


def _strip_suffix(ticker):
    """StockTwits uses bare US-style symbols; drop the .AX suffix etc.
    (ASX coverage on StockTwits is thin, but the call still succeeds or
    fails safe.)"""
    return ticker.split(".")[0].upper()


def fetch_stocktwits(ticker):
    """
    Returns a dict:
        {message_count, bullish, bearish, net_sentiment}
    net_sentiment = bullish - bearish (tagged messages only).
    Fails safe to all-zeros on any error.
    """
    symbol = _strip_suffix(ticker)
    empty = {"message_count": 0, "bullish": 0, "bearish": 0, "net_sentiment": 0}

    try:
        resp = requests.get(
            STOCKTWITS_URL.format(symbol=symbol),
            headers=_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return empty
        payload = resp.json()
    except Exception:
        return empty

    messages = payload.get("messages", [])
    if not messages:
        return empty

    bullish = 0
    bearish = 0
    for m in messages:
        entities = m.get("entities") or {}
        sentiment = (entities.get("sentiment") or {})
        basic = sentiment.get("basic") if isinstance(sentiment, dict) else None
        if basic == "Bullish":
            bullish += 1
        elif basic == "Bearish":
            bearish += 1

    return {
        "message_count": len(messages),
        "bullish": bullish,
        "bearish": bearish,
        "net_sentiment": bullish - bearish,
    }


def fetch_x(ticker):
    """
    Placeholder for an X (Twitter) provider. X removed free API access in
    2023; a working implementation needs a paid bearer token and would call
    the v2 recent-search counts endpoint. Left unimplemented on purpose so
    the app never silently depends on a broken/ToS-violating scraper.
    """
    raise NotImplementedError(
        "X provider requires a paid API key. Implement fetch_x() with your "
        "bearer token, or use the default StockTwits provider."
    )


def get_social_score(ticker, provider="stocktwits"):
    """
    Returns (social_score, detail_dict).

    social_score is a single number blending buzz VOLUME with net bullishness:
        social_score = message_count + (net_sentiment * 2)
    i.e. attention plus a bull/bear tilt (each net bullish message counts
    extra). This feeds the Discovery (attention) score in the app.

    provider: "stocktwits" (default, free) or "x" (needs a paid key).
    """
    if provider == "x":
        try:
            data = fetch_x(ticker)
        except NotImplementedError:
            return 0, {"message_count": 0, "bullish": 0, "bearish": 0,
                       "net_sentiment": 0, "provider": "x (unavailable)"}
    else:
        data = fetch_stocktwits(ticker)

    score = data["message_count"] + (data["net_sentiment"] * 2)
    data = dict(data)
    data["provider"] = provider
    return score, data
