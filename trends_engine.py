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

import streamlit as st

_client = None


def _get_client():
    global _client
    if _client is None:
        from trendspy import Trends
        _client = Trends()
    return _client


@st.cache_data(ttl=1800, show_spinner=False)
def get_google_trend_score(keyword):
    """Google Trends interest-over-time value (0-100 scale) for `keyword`,
    most recent point in the last 3 months. Returns (0, False) when the
    call succeeds but genuinely has nothing to report (no data for this
    keyword) - a real reading, not a failure. Returns (0, True) only when
    the call itself raised (rate limit, broken session flow, network
    issue) - see get_trend_score's docstring for why that distinction
    matters to the caller.

    Cached (30-minute TTL, keyed by keyword) - trendspy sits on an
    unofficial Google endpoint that's already known to be fragile under
    aggressive request patterns (see module docstring), so on top of
    avoiding redundant refetches for a popular ticker, this also directly
    reduces how often this app hits that endpoint under concurrent traffic.
    """
    try:
        tr = _get_client()
        data = tr.interest_over_time([keyword])
        if data is None or data.empty or keyword not in data.columns:
            return 0, False
        return int(data[keyword].iloc[-1]), False
    except Exception:
        return 0, True


@st.cache_data(ttl=10800, show_spinner=False)
def get_news_trend_score(keyword, api_key=None):
    """Count of NewsAPI articles mentioning `keyword`, strictly in the last 7
    days - the crowd-attention signal that used to come from Reddit mentions
    (see module docstring). Needs a NewsAPI key - pass api_key through from
    app.py's "NewsAPI Key" box (same key News Score uses); returns (0,
    False) if none is available - a missing key is an expected, configured
    state, not a failure. Returns (0, True) only on an actual fetch failure
    (rate limit, network issue) - this is still a nice-to-have signal,
    never something that should break a scan, but the caller can now tell
    "fetch broke" apart from "zero real mentions."

    Cached at the same longer 3-hour TTL as news_engine.get_news_score(),
    for the same reason: this also spends NewsAPI's free-tier daily quota,
    which is shared across every visitor to this site."""
    if not api_key:
        return 0, False
    try:
        from newsapi import NewsApiClient
        client = NewsApiClient(api_key=api_key)
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        resp = client.get_everything(
            q=keyword, language="en", from_param=since, page_size=100
        )
        return len(resp.get("articles", [])), False
    except Exception:
        return 0, True


def get_trend_score(keyword, api_key=None):
    """Combined score: Google Trends interest + NewsAPI mention count (past
    7 days). `api_key`, if passed, is the NewsAPI key from app.py - without
    it, the NewsAPI half is always (0, False) and this is Google Trends
    alone.

    Returns (score, failed). `failed=True` means at least one half's fetch
    actually raised and silently fell back to 0 - as opposed to a
    legitimate zero (no search interest, no matching articles, or simply
    no NewsAPI key configured). Root-caused live on CPRT (2026-08-30): the
    Comparison table showed Discovery=68 while the Deep Dive page for the
    same ticker, computed moments later, showed 89 - a fresh scan matched
    Deep Dive exactly, confirming the 68 reading was this exact silent
    fallback (Trend Score's Google half briefly failed and returned 0
    instead of its real ~21), not a bug in the Discovery formula or the
    caching architecture. Callers use `failed` to disclose that instead of
    presenting a fetch failure as if it were a confirmed reading."""
    google_value, google_failed = get_google_trend_score(keyword)
    news_value, news_failed = get_news_trend_score(keyword, api_key=api_key)
    return google_value + news_value, (google_failed or news_failed)
