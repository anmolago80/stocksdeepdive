"""
mood_engine.py

Builds a "societal mood" read for a country - Hopeful / Neutral / Anxious -
from GDELT's DOC 2.0 API: a real, pre-computed average "tone" score
(-100..+100, in practice usually -10..+10) across ALL news coverage for a
country, averaged over the last GDELT_LOOKBACK_DAYS days (10 by default).
Free, no key/signup needed. This is a genuine sentiment number computed by
GDELT over thousands of articles, not something this app guesses at with a
keyword list.

NOTE ON THE EARLIER SOURCES: this used to combine Google Trends' trending
search terms, then Reddit's "hot posts", then Google News RSS + NewsAPI
headlines - all classified locally with a keyword lexicon and accumulated
over a rolling multi-day history to smooth out thin daily data. That design
had two real problems in practice: (1) Reddit deprecated anonymous .json
access on 28 May 2026, breaking that source outright; (2) once replaced with
Google News + NewsAPI, Google News' broad "top stories for the whole
country" feed structurally skewed negative (war/disaster/crime coverage)
and, being high-volume, drowned out NewsAPI in the shared hit pool - so the
reading ended up reflecting mostly Google News, not a balanced blend. GDELT
sidesteps both problems: it's a single already-computed aggregate, not a
pool of individually-classified items competing for space, so no source can
out-vote another by volume, and there's no local keyword-classification
step to get wrong.

This is intentionally standalone and display-only - it never touches
per-ticker scoring (Long Score, Trader Score) or any trading logic.

CONFIRMED ROOT CAUSE (Aug 2026): the recurring "Unknown - couldn't fetch
trending data" was GDELT actively rejecting requests with HTTP 429 Too Many
Requests, and its own error body says why: "Please limit requests to one
every 5 seconds". This app has TWO call sites in the same script run (the
Industry tab and the Stock tab each show their own mood reading), and every
Streamlit rerun executes both - so any time those two calls landed on
different countries (or both needed a fresh fetch at the same moment, e.g.
right after the 30-minute cache expired), they fired within milliseconds of
each other, violating GDELT's 5-second pacing rule. The fix is the
module-level throttle below: every real GDELT request (across ALL
countries/tabs, for the life of the running app process) is spaced at least
GDELT_MIN_INTERVAL_SECONDS apart, sleeping first if a previous call was too
recent, and a 429 specifically now gets one on-the-spot retry after waiting
it out rather than immediately giving up.
"""

import threading
import time

# Country (as shown in the app's "Select Market" dropdown) -> GDELT's
# lowercase full-name country code, used in the sourcecountry: filter.
COUNTRY_SOURCES = {
    "Australia": {"gdelt_country": "australia"},
    "USA": {"gdelt_country": "unitedstates"},
}


GDELT_LOOKBACK_DAYS = 10

# GDELT's own 429 response body asks for at least 5 seconds between
# requests - enforced process-wide (not per-country) below, since this app
# can fire a request for Australia and one for USA within the same rerun.
GDELT_MIN_INTERVAL_SECONDS = 5.0

_gdelt_throttle_lock = threading.Lock()
_gdelt_last_call_at = 0.0


def _throttle_gdelt():
    """Blocks (briefly) if the last real GDELT request was under
    GDELT_MIN_INTERVAL_SECONDS ago, then records this call's time. Global
    to the process (not per-session/per-country) since GDELT's rate limit
    is about total request pacing, not which tab or country asked."""
    global _gdelt_last_call_at
    with _gdelt_throttle_lock:
        wait = GDELT_MIN_INTERVAL_SECONDS - (time.monotonic() - _gdelt_last_call_at)
        if wait > 0:
            time.sleep(wait)
        _gdelt_last_call_at = time.monotonic()


def _fetch_gdelt_once(gdelt_country: str):
    """Single GDELT HTTP round-trip, no throttling/retry - returns
    (tone, error_detail, http_status). http_status is the raw HTTP status
    code if the request reached GDELT at all (e.g. 429), or None if it
    never got a response (network/URL error) - the caller uses this to
    decide whether a retry is worth attempting."""
    import json
    import urllib.error
    import urllib.request

    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query=sourcecountry:{gdelt_country}"
        f"&mode=timelinetone&format=json&timespan={GDELT_LOOKBACK_DAYS}days"
    )
    req = urllib.request.Request(
        url=url,
        headers={"User-Agent": "GlobalStockScanner/1.0 (mood-engine; personal use)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            body = ""
        return None, f"GDELT HTTP {e.code} {e.reason}" + (f" - {body}" if body else ""), e.code
    except urllib.error.URLError as e:
        return None, f"GDELT unreachable ({e.reason}) - network/firewall issue", None
    except Exception as e:
        return None, f"GDELT request failed: {type(e).__name__}: {e}", None

    if status != 200:
        return None, f"GDELT HTTP {status}", status

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        snippet = raw.decode("utf-8", errors="replace")[:200]
        return None, f"GDELT response wasn't valid JSON ({e}) - raw: {snippet!r}", status

    series = payload.get("timeline") or []
    if not series:
        return None, "GDELT returned no timeline - the query likely matched zero articles", status
    points = series[0].get("data") or []
    if not points:
        return None, "GDELT returned an empty timeline - no data points for this window", status
    values = [float(p["value"]) for p in points if "value" in p]
    if not values:
        return None, "GDELT's data points had no usable 'value' field", status
    return sum(values) / len(values), None, status


def _get_gdelt_tone(gdelt_country: str):
    """Average GDELT news-tone score for a country's news coverage over the
    last GDELT_LOOKBACK_DAYS days, via GDELT's free DOC 2.0 API
    (api.gdeltproject.org) - no key, no signup. `mode=timelinetone` returns a
    time series of data points across the requested window; this averages
    ALL of them, giving a genuine multi-day read rather than a single
    instant.

    Every real request is throttled to GDELT's own stated pacing (see
    _throttle_gdelt) - if GDELT still returns 429 after that (e.g. the
    5-second gap wasn't enough, or something else on the same network is
    also hitting GDELT), this waits once more and retries a single time
    before giving up, rather than immediately surfacing the rate-limit
    error to you.

    Returns (tone, error_detail): `tone` is None on any failure (network
    issue, GDELT's fair-use rate limiting, GDELT changing its response
    format, or the query itself returning no data) - this is a nice-to-have
    signal, never something that should break the mood read. `error_detail`
    is a short human-readable string describing WHY it failed, or None on
    success - surfaced all the way up to the app's mood caption, so a
    failure is diagnosable from the app itself without needing to separately
    run gdelt_test.py."""
    _throttle_gdelt()
    tone, detail, status = _fetch_gdelt_once(gdelt_country)

    if tone is None and status == 429:
        # Honor GDELT's own request and back off before the one retry.
        time.sleep(GDELT_MIN_INTERVAL_SECONDS)
        _throttle_gdelt()
        tone, detail, status = _fetch_gdelt_once(gdelt_country)

    return tone, detail


def compute_country_mood(country: str, api_key: str = None) -> dict:
    """
    `api_key` is accepted but unused - GDELT needs no key. Kept as a
    parameter purely so app.py's existing call sites (which pass the
    NewsAPI key through) don't need to change.

    Returns:
        {
            "label": "Hopeful" | "Neutral" | "Anxious" | "Unknown",
            "score": float,           # -1..1, only meaningful if gdelt_tone is not None
            "gdelt_tone": float | None,  # raw GDELT tone, or None if unavailable
            "today_items": [ {"term", "source", "classification"} ... ],
            "error_detail": str | None,  # why gdelt_tone is None, or None on success
        }
    Never raises - falls back to "Unknown" on any unexpected error or if
    GDELT is unreachable, since this is a display-only extra, not something
    that should ever break a scan.
    """
    def _unknown(detail):
        return {
            "label": "Unknown", "score": 0.0, "gdelt_tone": None,
            "today_items": [], "error_detail": detail,
        }

    try:
        sources = COUNTRY_SOURCES.get(country)
        if not sources:
            return _unknown(f"No GDELT source configured for {country!r}")

        gdelt_tone, error_detail = _get_gdelt_tone(sources["gdelt_country"])
        if gdelt_tone is None:
            return _unknown(error_detail)

        # GDELT tone typically ranges roughly -10..+10 for a whole country's
        # coverage on an ordinary day - /5 and clip to -1..+1 keeps the
        # score on the same -1..1 scale this app uses elsewhere.
        score = max(-1.0, min(1.0, gdelt_tone / 5.0))
        if score > 0.15:
            label = "Hopeful"
        elif score < -0.15:
            label = "Anxious"
        else:
            label = "Neutral"

        today_items = [{
            "term": (
                f"GDELT news tone (country-wide average, last "
                f"{GDELT_LOOKBACK_DAYS} days): {gdelt_tone:+.2f}"
            ),
            "source": "GDELT",
            "classification": label.lower(),
        }]

        return {
            "label": label,
            "score": round(score, 3),
            "gdelt_tone": gdelt_tone,
            "today_items": today_items,
            "error_detail": None,
        }
    except Exception as e:
        return _unknown(f"Unexpected error: {type(e).__name__}: {e}")
