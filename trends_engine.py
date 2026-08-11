"""
trends_engine.py

Per-ticker "is anyone searching/talking about this stock right now" score,
combined from two sources:
  - Google Trends search interest (via trendspy, no key needed)
  - NewsAPI mention count, past 7 days (needs the same NewsAPI key used for
    News Score - see news_engine.py)

NOTE ON THE SWITCH FROM pytrends: this used to run on `pytrends`, which was
archived in April 2025 and is now broken - Google changed its session/cookie
flow, so pytrends' first call returns a 429 before ever reaching real data.
That almost certainly means Trend Score has been silently returning 0 for a
while, the same "silent zero" issue News Score had (see news_engine.py).
`trendspy` is a maintained replacement that handles the current flow
correctly as of this writing - but it's ALSO sitting on an unofficial Google
endpoint, so if Google changes something again, this could break the same
way. Every call here fails safe (returns 0), so a future break shows up as
"Trend Score always 0" again, not a crashed app - same contract as before.

NOTE ON THE SWITCH FROM REDDIT: the Reddit-mentions half used to query
Reddit's public search.json endpoint (no key needed). Reddit deprecated
anonymous access to its .json endpoints on 28 May 2026 (see mood_engine.py's
docstring for details) - every unauthenticated request now gets a flat 403,
no workaround short of OAuth. NewsAPI mention count (last 7 days, reusing
the key already configured for News Score) took its place. This does mean
Trend Score's second half and News Score now both draw on NewsAPI - Trend
Score uses a strict rolling 7-day window (site-wide "buzz this week"), while
News Score uses NewsAPI's own default window (looser, ~30 days on the free
tier) - so they're not identical reads, but they're not fully independent
signals anymore either, unlike the old Reddit-vs-NewsAPI split.
"""

from datetime import datetime, timedelta, timezone

_client = None


def _get_client():
    global _client
    if _client is None:
        from trendspy import Trends
        _client = Trends()
    return _client


def get_google_trend_score(keyword):
    """Google Trends interest-over-time value (0-100 scale) for `keyword`,
    most recent point in the last 3 months. Returns 0 on any failure."""
    try:
        tr = _get_client()
        data = tr.interest_over_time([keyword])
        if data is None or data.empty or keyword not in data.columns:
            return 0
        return int(data[keyword].iloc[-1])
    except Exception:
        return 0


def get_news_trend_score(keyword, api_key=None):
    """Count of NewsAPI articles mentioning `keyword`, strictly in the last 7
    days - the crowd-attention signal that used to come from Reddit mentions
    (see module docstring). Needs a NewsAPI key - pass api_key through from
    app.py's "NewsAPI Key" box (same key News Score uses); returns 0 if
    none is available. Returns 0 on any failure (rate limit, network issue,
    no results) - this is a nice-to-have signal, never something that
    should break a scan."""
    if not api_key:
        return 0
    try:
        from newsapi import NewsApiClient
        client = NewsApiClient(api_key=api_key)
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        resp = client.get_everything(
            q=keyword, language="en", from_param=since, page_size=100
        )
        return len(resp.get("articles", []))
    except Exception:
        return 0


def get_trend_score(keyword, api_key=None):
    """Combined score: Google Trends interest + NewsAPI mention count (past
    7 days). `api_key`, if passed, is the NewsAPI key from app.py - without
    it, the NewsAPI half is always 0 and this is Google Trends alone."""
    return get_google_trend_score(keyword) + get_news_trend_score(keyword, api_key=api_key)
