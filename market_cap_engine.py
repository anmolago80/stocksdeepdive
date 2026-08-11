import yfinance as yf


def get_market_cap(ticker, info=None):

    try:

        if info is None:
            info = yf.Ticker(ticker).info

        # yfinance frequently returns an explicit None (not a missing key)
        # for marketCap, so `.get(key, 0)` alone isn't enough - `or 0`
        # catches that case too.
        return info.get("marketCap", 0) or 0

    except Exception:

        return 0


def get_market_cap_bucket(ticker, info=None):

    market_cap = get_market_cap(ticker, info=info)

    if market_cap > 100_000_000_000:
        return "LARGE"

    elif market_cap > 10_000_000_000:
        return "MID"

    else:
        return "SMALL"
