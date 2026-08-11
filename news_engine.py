import os

import yfinance as yf
from newsapi import NewsApiClient


def _get_api_key():
    """
    Look for the NewsAPI key in Streamlit secrets first (the normal place
    to keep it for a deployed app), then fall back to an environment
    variable. Never hardcode real keys in source - the previous version of
    this file shipped a live key in plain text, which is a leak the moment
    the repo is shared or pushed anywhere public.
    """

    try:
        import streamlit as st
        if "NEWS_API_KEY" in st.secrets:
            return st.secrets["NEWS_API_KEY"]
    except Exception:
        pass

    return os.environ.get("NEWS_API_KEY", "")


API_KEY = _get_api_key()


def get_news_score(keyword, api_key=None):
    """
    Article count from NewsAPI for a free-text keyword search - `keyword`
    is expected to be the ticker with its exchange suffix stripped (e.g.
    "CSL", not "CSL.AX"), since NewsAPI is a text search over article
    content, not a ticker-symbol lookup.

    `api_key`, if passed, overrides the module-level API_KEY (which comes
    from .streamlit/secrets.toml or the NEWS_API_KEY environment variable at
    import time) - this lets app.py's "NewsAPI Key" input box in the UI take
    priority over whatever's configured at the file/environment level,
    without requiring a restart to pick up a key typed in at runtime.

    Returns 0 (not an error) if no key is available from either source, or
    if the request fails for any reason (rate limit, network issue, bad
    response) - this is a "nice to have" signal, so a failure here should
    never break the scan.
    """

    key = api_key or API_KEY
    if not key:
        return 0

    try:

        newsapi = NewsApiClient(api_key=key)

        articles = newsapi.get_everything(
            q=keyword,
            language="en",
            page_size=50
        )

        return len(
            articles["articles"]
        )

    except Exception:

        return 0


def get_yahoo_news_score(ticker):
    """
    Article count from Yahoo Finance via yfinance (already a dependency of
    this app). Unlike get_news_score(), this needs the FULL ticker symbol
    INCLUDING the exchange suffix (e.g. "CSL.AX", not "CSL") - Yahoo
    resolves a bare "CSL" to Carlisle Companies on the NYSE, a completely
    different company from CSL Limited on the ASX, so passing the stripped
    keyword here would silently score the wrong stock's news.

    No API key needed - this is free/unauthenticated, so it still returns a
    real count even when NEWS_API_KEY isn't configured (which, in practice,
    is the more common case - NewsAPI's free tier requires signing up for a
    key that most users of this app won't have set up). Returns 0 on any
    error (network issue, no news available, or Yahoo occasionally changing
    its response schema out from under yfinance).
    """

    try:

        news = yf.Ticker(ticker).news

        return len(news) if news else 0

    except Exception:

        return 0
